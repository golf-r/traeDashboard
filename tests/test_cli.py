"""Tests for trae_dashboard.cli subcommands."""
from __future__ import annotations
import pytest
import sqlite3
from pathlib import Path

from trae_dashboard.cli import main, _build_parser


def test_cli_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "init" in combined
    assert "fetch" in combined
    assert "serve" in combined


def test_parser_lists_subcommands():
    parser = _build_parser()
    args = parser.parse_args([])
    assert args.cmd is None
    args = parser.parse_args(["init"])
    assert args.cmd == "init"
    args = parser.parse_args(["fetch", "--config", "x.yaml"])
    assert args.cmd == "fetch"
    assert args.config == "x.yaml"


def test_init_subcommand_writes_config(tmp_data_dir, monkeypatch):
    monkeypatch.chdir(tmp_data_dir)
    example = tmp_data_dir / "config.example.yaml"
    example.write_text("openapi_base: x\nauth_endpoint: /a\n", encoding="utf-8")
    target = tmp_data_dir / "config.yaml"
    main(["init", "--config", str(target)])
    assert target.exists()
    assert "openapi_base" in target.read_text(encoding="utf-8")


def test_init_subcommand_does_not_overwrite(tmp_data_dir, monkeypatch):
    monkeypatch.chdir(tmp_data_dir)
    example = tmp_data_dir / "config.example.yaml"
    example.write_text("openapi_base: from_example\n", encoding="utf-8")
    target = tmp_data_dir / "config.yaml"
    target.write_text("openapi_base: from_user\n", encoding="utf-8")
    main(["init", "--config", str(target)])
    assert "from_user" in target.read_text(encoding="utf-8")


def test_prune_command_removes_zero_amount_accounts(tmp_data_dir, monkeypatch):
    """prune removes accounts whose amount_total is zero."""
    monkeypatch.chdir(tmp_data_dir)
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")

    cfg = tmp_data_dir / "config.yaml"
    cfg.write_text(
        "openapi_base: x\nauth_endpoint: /a\n"
        "app_id_env: TRAE_APP_ID\napp_secret_env: TRAE_APP_SECRET\n"
        "db_path: data/dashboard.db\n"
        "accounts:\n  - email: keep@x.com\n    display_name: Keep\n",
        encoding="utf-8",
    )

    from trae_dashboard.storage import Storage
    db_path = tmp_data_dir / "data" / "dashboard.db"
    s = Storage(db_path)
    s.init()
    s.upsert_account("keep@x.com", "Keep")
    s.upsert_account("zero1@x.com", "Zero1")
    s.upsert_account("zero2@x.com", "Zero2")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="keep@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        amount_total=10.0,
    )
    s.close()

    from io import StringIO
    from contextlib import redirect_stdout
    buf = StringIO()
    with redirect_stdout(buf):
        main(["prune", "--config", str(cfg), "--keep-snapshots", "5"])
    output = buf.getvalue()
    assert "deleted_accounts=2" in output
    assert "zero-data accounts: 2" in output

    s2 = Storage(db_path)
    emails = {a.email for a in s2.list_accounts()}
    assert emails == {"keep@x.com"}
    s2.close()


def test_prune_dry_run_does_not_delete(tmp_data_dir, monkeypatch):
    monkeypatch.chdir(tmp_data_dir)
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")

    cfg = tmp_data_dir / "config.yaml"
    cfg.write_text(
        "openapi_base: x\nauth_endpoint: /a\n"
        "app_id_env: TRAE_APP_ID\napp_secret_env: TRAE_APP_SECRET\n"
        "db_path: data/dashboard.db\n"
        "accounts: []\n",
        encoding="utf-8",
    )

    from trae_dashboard.storage import Storage
    db_path = tmp_data_dir / "data" / "dashboard.db"
    s = Storage(db_path)
    s.init()
    s.upsert_account("zero@x.com", "Zero")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="zero@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        amount_total=0.0,
    )
    s.close()

    from io import StringIO
    from contextlib import redirect_stdout
    buf = StringIO()
    with redirect_stdout(buf):
        main(["prune", "--config", str(cfg), "--dry-run"])
    output = buf.getvalue()
    assert "[dry-run]" in output
    assert "zero-data accounts: 1" in output

    s2 = Storage(db_path)
    emails = {a.email for a in s2.list_accounts()}
    assert "zero@x.com" in emails
    s2.close()


def test_fetch_subcommand_writes_db(tmp_data_dir, monkeypatch):
    """fetch creates SQLite with snapshots and model_usage rows."""
    import httpx
    from trae_dashboard.client import TraeClient

    monkeypatch.chdir(tmp_data_dir)
    monkeypatch.setenv("TRAE_APP_ID", "test_id")
    monkeypatch.setenv("TRAE_APP_SECRET", "test_secret")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    def handler(request):
        if "/auth" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "code": 0, "message": "ok", "request_id": "r",
                "data": {"items": [
                    {"email": "a@x.com", "model_usage": [
                        {"model_name": "GLM-5.1", "model_type": "Chat", "model_source": "Trae",
                         "usage": {"input_tokens": 9, "output_tokens": 18},
                         "amount": {"total_amount": 5.0}}
                    ]}
                ]},
            },
        )

    orig_init = TraeClient.__init__
    def patched_init(self, **kwargs):
        orig_init(self, **kwargs)
        mock = httpx.Client(transport=httpx.MockTransport(handler))
        self._client = mock
        if getattr(self, "_tokens", None) is not None:
            self._tokens._client = mock
    monkeypatch.setattr(TraeClient, "__init__", patched_init)

    target_cfg = tmp_data_dir / "config.yaml"
    target_cfg.write_text(
        "openapi_base: https://api.test\nauth_endpoint: /auth\n"
        "app_id_env: TRAE_APP_ID\napp_secret_env: TRAE_APP_SECRET\n"
        "db_path: data/dashboard.db\n"
        "accounts:\n  - email: a@x.com\n    display_name: A\n",
        encoding="utf-8",
    )

    main(["fetch", "--config", str(target_cfg)])

    db_path = tmp_data_dir / "data" / "dashboard.db"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    try:
        snap_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        model_count = conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
        account_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()
    assert snap_count >= 1
    assert model_count >= 1
    assert account_count >= 1
