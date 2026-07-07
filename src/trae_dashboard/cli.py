"""CLI entry point for trae-dashboard.

Subcommands:
  init   - write config.example.yaml to config.yaml (if missing)
  fetch  - run a one-shot data collection
  serve  - start the FastAPI web server with a background scheduler
  report - send the daily email report (SMTP) for the current cycle
"""

from __future__ import annotations
import argparse
import logging
from pathlib import Path

import uvicorn

from .config import load_config
from .storage import Storage
from .api import create_app
from .scheduler import make_collector, start_scheduler


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trae-dashboard",
        description="Local dashboard for Trae Enterprise token usage",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="First-time setup")
    p_init.add_argument(
        "--config", default="config.yaml", help="Path to write config.yaml"
    )

    p_fetch = sub.add_parser("fetch", help="Run one data fetch")
    p_fetch.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    p_serve = sub.add_parser("serve", help="Start web server")
    p_serve.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument(
        "--with-scheduler",
        action="store_true",
        help="Also start the background scheduler (off by default; "
        "the dashboard refreshes only when the user clicks the refresh button)",
    )

    p_prune = sub.add_parser("prune", help="Clean up redundant data")
    p_prune.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_prune.add_argument(
        "--keep-snapshots",
        type=int,
        default=5,
        help="How many recent snapshots to keep (default: 5)",
    )
    p_prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without modifying the DB",
    )

    p_report = sub.add_parser(
        "report",
        help="Send the daily email report for the current cycle",
    )
    p_report.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_report.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the email body and print it to stdout instead of sending",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "init":
        _init(Path(args.config))
    elif args.cmd == "fetch":
        _fetch(Path(args.config))
    elif args.cmd == "serve":
        _serve(Path(args.config), args.host, args.port, scheduler=args.with_scheduler)
    elif args.cmd == "prune":
        _prune(
            Path(args.config), keep_snapshots=args.keep_snapshots, dry_run=args.dry_run
        )
    elif args.cmd == "report":
        _report(Path(args.config), dry_run=args.dry_run)
    else:
        parser.print_help()
        raise SystemExit(2)


def _init(config_path: Path) -> None:
    """Write config.example.yaml to config_path if it does not exist."""
    example = Path("config.example.yaml")
    if config_path.exists():
        print(f"config already exists at {config_path}; leaving it untouched")
        return
    if not example.exists():
        raise RuntimeError(
            f"config.example.yaml not found in {Path.cwd()}; "
            "cannot scaffold a config.yaml"
        )
    config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Wrote example config to {config_path}")
    print("Next: edit config.yaml, set TRAE_APP_ID / TRAE_APP_SECRET in .env")


def _fetch(config_path: Path) -> None:
    cfg = load_config(config_path)
    storage = Storage(cfg.db_path, display_weights=cfg.display_weights)
    storage.init()
    for a in cfg.accounts:
        storage.upsert_account(a.email, a.display_name)
    collector = make_collector(cfg, storage)
    result = collector.run_once()
    print(result)


def _serve(config_path: Path, host: str, port: int, *, scheduler: bool = False) -> None:
    """Start the web server.

    By default no background scheduler runs — the dashboard only refreshes
    when the user clicks the refresh button (which calls POST /api/refresh).
    Pass --with-scheduler to opt back into periodic background fetches.
    """
    cfg = load_config(config_path)
    storage = Storage(cfg.db_path)
    storage.init()
    for a in cfg.accounts:
        storage.upsert_account(a.email, a.display_name)
    if scheduler:
        start_scheduler(cfg, storage)
    app = create_app(cfg=cfg, storage=storage)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _prune(config_path: Path, *, keep_snapshots: int, dry_run: bool) -> None:
    """Clean up the SQLite DB.

    Operations (in order):
      1. Delete accounts whose model_usage totals are zero.
      2. Delete orphan model_usage rows (email no longer in accounts).
      3. Keep only the most recent ``keep_snapshots`` snapshots.
    """
    cfg = load_config(config_path)
    storage = Storage(cfg.db_path, display_weights=cfg.display_weights)
    storage.init()
    try:
        if dry_run:
            from .cycle import current_cycle_window

            s_dt, e_dt = current_cycle_window()
            rows = storage.get_model_usage_by_account(
                s_dt.date().isoformat(),
                e_dt.date().isoformat(),
                cfg.included_model_names,
            )
            n_zero = sum(
                1
                for r in rows
                if (r["input_tokens"] or 0) == 0 and (r["output_tokens"] or 0) == 0
            )
            n_snap = storage.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[
                0
            ]
            n_orphan = storage.conn.execute(
                "SELECT COUNT(*) FROM model_usage m "
                "WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.email = m.email)"
            ).fetchone()[0]
            print(
                f"[dry-run] zero-data accounts: {n_zero}; "
                f"orphan model rows: {n_orphan}; "
                f"snapshots total: {n_snap} "
                f"(would trim to keep_last={keep_snapshots})"
            )
            return

        zero_stats = storage.prune_zero_data_accounts()
        orphan_deleted = storage.prune_orphan_model_usage()
        old_snapshots_deleted = storage.prune_old_snapshots(keep_last=keep_snapshots)

        print(
            f"deleted_accounts={zero_stats['deleted_accounts']} "
            f"deleted_model_rows={zero_stats['deleted_model_rows']} "
            f"orphan_model_deleted={orphan_deleted} "
            f"old_snapshots_deleted={old_snapshots_deleted}"
        )
        print(
            f"zero-data accounts: {zero_stats['deleted_accounts']}; "
            f"orphan model rows: {orphan_deleted}; "
            f"old snapshots trimmed: {old_snapshots_deleted} (keep_last={keep_snapshots})"
        )
    finally:
        storage.close()


def _report(config_path: Path, *, dry_run: bool = False) -> None:
    """Render and (optionally) send the daily email report.

    `--dry-run` prints the HTML body to stdout instead of sending mail,
    so you can preview the layout before wiring up SMTP credentials.
    """
    from .report import collect_report_rows, render_html
    from .cycle import current_cycle_window
    from datetime import datetime, timezone

    cfg = load_config(config_path)
    storage = Storage(cfg.db_path, display_weights=cfg.display_weights)
    storage.init()
    try:
        rows = collect_report_rows(storage, cfg)
        now = datetime.now(timezone.utc)
        start_dt, _ = current_cycle_window()
        html = render_html(rows, cfg, start_dt, now)
        subject = f"[Trae Dashboard] 周期消耗日报 {now.strftime('%Y-%m-%d')}"

        if dry_run:
            print(f"Subject: {subject}")
            print(
                f"Recipients: {cfg.email.recipients or '(none — email not configured)'}"
            )
            print(f"Rows: {len(rows)}")
            print(f"Total consumed: {sum(r.consumed for r in rows):,}")
            print("-" * 60)
            print(html)
            return

        if not cfg.email.enabled:
            print(
                "email report is disabled in config.yaml "
                "(email.enabled: false). "
                "Run with --dry-run to preview, or set email.enabled: true."
            )
            raise SystemExit(1)

        from .report import run_report

        summary = run_report(storage, cfg)
        print(
            f"sent: recipients={summary['recipient_count']} "
            f"rows={summary['rows']} "
            f"total_consumed={summary['total_consumed']:,} "
            f"subject={summary['subject']!r}"
        )
    finally:
        storage.close()


if __name__ == "__main__":
    main()
