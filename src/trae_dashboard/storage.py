"""SQLite storage layer (WAL mode) for trae-dashboard.

Schema:
- accounts: tracked accounts (email PK, display_name)
- snapshots: every fetch's raw response payload (audit trail)
- model_usage: per-account × per-model totals for a cycle window
  (one row per (email, cycle_start, model_name); total across rows = cycle total)

Model filtering: read methods accept an `included_model_names` set and only
return rows whose model_name is in that set. The collector filters using
`Config.included_model_names` before persistence; storage filtering ensures
legacy non-allowlisted rows don't leak into the dashboard.
"""

from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def _model_filter_sql(included_model_names: set[str]) -> tuple[str, list[str]]:
    """Build (sql_fragment, params) for an allowlist IN clause.

    Case-insensitive comparison (LOWER on both sides) so the read path
    matches canonical PascalCase names from the config against whatever
    casing the API happened to return. The collector now stores rows
    under the canonical name, but legacy lowercase rows from previous
    fetches are still picked up correctly.

    An empty allowlist means nothing should be returned — we synthesize
    an `AND 0` predicate so the query yields no model_usage rows.
    """
    if not included_model_names:
        return "AND 0", []
    placeholders = ",".join("?" * len(included_model_names))
    return (
        f"AND LOWER(m.model_name) IN ({placeholders})",
        [n.lower() for n in sorted(included_model_names)],
    )

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  email TEXT PRIMARY KEY,
  display_name TEXT,
  enabled INTEGER DEFAULT 1,
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  start_time INTEGER NOT NULL,
  end_time INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  request_meta TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON snapshots(fetched_at);
CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  cycle_start TEXT NOT NULL,
  cycle_end TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_type TEXT,
  model_source TEXT,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(email, cycle_start, model_name),
  FOREIGN KEY (email) REFERENCES accounts(email)
);
CREATE INDEX IF NOT EXISTS idx_model_usage_email ON model_usage(email);
CREATE INDEX IF NOT EXISTS idx_model_usage_cycle ON model_usage(cycle_start, cycle_end);
"""


@dataclass
class Account:
    email: str
    display_name: str | None
    enabled: bool


@dataclass
class ModelUsage:
    email: str
    cycle_start: str
    cycle_end: str
    model_name: str
    model_type: str | None
    model_source: str | None
    input_tokens: int
    output_tokens: int


class Storage:
    def __init__(
        self,
        db_path: str | Path,
        *,
        display_weights: dict[str, float] | None = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.row_factory = sqlite3.Row

        # Display-time weights keyed by canonical model_name. Applied on
        # read in `get_model_usage_by_account` and
        # `get_model_usage_for_account` so DB rows keep the raw API values
        # (audit trail intact). Currently a single model — Doubao-Seed-Code
        # — needs a 0.5 multiplier to match the value the Trae admin UI
        # shows for the same cycle window.
        #
        # Weights are normally injected from `Config.display_weights` so
        # they can be changed via config.yaml without code changes. The
        # default fallback here keeps the historical behavior for tests
        # and one-off scripts that construct Storage directly.
        #
        # Adding a new model: append `"<canonical>": <weight>` below, or
        # (preferred) add it to `display_weights` in config.yaml.
        self.display_weights: dict[str, float] = (
            dict(display_weights) if display_weights is not None
            else {"Doubao-Seed-Code": 0.5}
        )

    def init(self) -> None:
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *a) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def upsert_account(self, email: str, display_name: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO accounts(email, display_name) VALUES(?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "display_name=COALESCE(excluded.display_name, accounts.display_name)",
            (email, display_name),
        )

    def delete_account(self, email: str) -> int:
        """Delete one account and cascade-delete its model_usage rows.

        Returns the total number of rows deleted (accounts + model_usage).
        Returns 0 if the account does not exist (idempotent: callers can
        treat "not found" the same as "already removed").

        Note: snapshots are intentionally NOT touched — they are the audit
        trail and survive account churn. The collector's run_once still
        reads from `list_accounts()`, so the next fetch simply skips
        the deleted email.

        Concurrency: we deliberately do NOT toggle PRAGMA foreign_keys here.
        The two-row DELETE below is a self-contained cascade (we delete
        model_usage first, then accounts) so FK protection is not needed.
        Toggling PRAGMA on a shared connection would race with concurrent
        writes from the scheduler thread — see project memory
        "PRAGMA foreign_keys is connection-scoped".
        """
        deleted_models = self.conn.execute(
            "DELETE FROM model_usage WHERE email = ?", (email,)
        ).rowcount or 0
        deleted_accounts = self.conn.execute(
            "DELETE FROM accounts WHERE email = ?", (email,)
        ).rowcount or 0
        return deleted_models + deleted_accounts

    def list_accounts(self) -> list[Account]:
        rows = self.conn.execute(
            "SELECT email, display_name, enabled FROM accounts WHERE enabled=1 ORDER BY email"
        ).fetchall()
        return [
            Account(r["email"], r["display_name"], bool(r["enabled"])) for r in rows
        ]

    # ------------------------------------------------------------------
    # Snapshots (audit trail)
    # ------------------------------------------------------------------

    def save_snapshot(
        self,
        *,
        start_time: int,
        end_time: int,
        payload_json: str,
        request_meta: str,
        error: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO snapshots(start_time, end_time, payload_json, request_meta, error) "
            "VALUES(?,?,?,?,?)",
            (start_time, end_time, payload_json, request_meta, error),
        )
        return cur.lastrowid

    def latest_snapshot(self) -> dict | None:
        row = self.conn.execute(
            "SELECT id, fetched_at, start_time, end_time "
            "FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        raw_ts = row["fetched_at"]
        # SQLite's `CURRENT_TIMESTAMP` writes UTC wall-clock time with no
        # timezone suffix (e.g. "2026-07-04 06:58:39"). The browser-side
        # `new Date(iso)` parses a timezone-less string in the *local*
        # zone — on a +08:00 host this skews the displayed "刚刚 / N
        # 小时前" by 8h. Tag the stored value as explicit UTC so JS
        # converts it correctly to local time on the client.
        if isinstance(raw_ts, str):
            if "T" not in raw_ts and " " in raw_ts:
                raw_ts = raw_ts.replace(" ", "T", 1)
            # Add "Z" if the string has no timezone marker yet.
            if not (raw_ts.endswith("Z") or "+" in raw_ts[10:] or raw_ts[10:].count("-") >= 2):
                iso_ts = raw_ts + "Z"
            else:
                iso_ts = raw_ts
        else:
            iso_ts = raw_ts
        return {
            "id": row["id"],
            "fetched_at": iso_ts,
            "start_time": row["start_time"],
            "end_time": row["end_time"],
        }

    # ------------------------------------------------------------------
    # model_usage — canonical per-cycle per-model totals
    # ------------------------------------------------------------------

    def upsert_model_usage(
        self,
        *,
        email: str,
        cycle_start: str,
        cycle_end: str,
        model_name: str,
        model_type: str | None,
        model_source: str | None,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Write one row per (email, cycle_start, model_name).

        UNIQUE on (email, cycle_start, model_name) means re-fetching the
        same cycle window **overwrites** the old row (no accumulation).
        """
        self.conn.execute(
            "INSERT INTO model_usage(email, cycle_start, cycle_end, model_name, "
            "model_type, model_source, input_tokens, output_tokens) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(email, cycle_start, model_name) DO UPDATE SET "
            "cycle_end=excluded.cycle_end, "
            "model_type=excluded.model_type, model_source=excluded.model_source, "
            "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
            "fetched_at=CURRENT_TIMESTAMP",
            (
                email,
                cycle_start,
                cycle_end,
                model_name,
                model_type,
                model_source,
                input_tokens,
                output_tokens,
            ),
        )

    def get_model_usage_by_account(
        self, cycle_start: str, cycle_end: str, included_model_names: set[str]
    ) -> list[dict]:
        """Per-account totals for a cycle.

        `cycle_end` is accepted for caller context, but rows are matched by
        `cycle_start` only. A row's stored cycle_end is the fetch cutoff date
        for the latest snapshot in that cycle, so requiring exact equality
        would hide valid current-cycle data after midnight or on stale fetches.

        `included_model_names` restricts which model_usage rows contribute
        to totals. Legacy rows whose model_name is not in the allowlist are
        silently ignored, so toggling the allowlist changes dashboard
        numbers immediately.

        Display weights from `self.display_weights` (keyed by canonical
        model_name) are applied to per-model `input_tokens` /
        `output_tokens` before aggregation. Weights are model-level, so
        the SUM still aggregates across accounts correctly. Raw values
        remain in the model_usage table for audit.

        Returns list of dicts:
          { email, display_name, input_tokens, output_tokens, model_count }
        Accounts with no model_usage rows are included with zeros.
        Only enabled accounts are listed. Sorted by total tokens desc.
        """
        model_filter_sql, model_params = _model_filter_sql(included_model_names)
        # Per-model display weight: emit a CASE expression that returns
        # the configured weight (or 1.0 for models without one). This is
        # applied to both input_tokens and output_tokens before SUM.
        #
        # The CASE expression embeds a single `?` per weighted model.
        # We deliberately embed it ONLY in the SELECT clause (twice:
        # once for input_tokens, once for output_tokens) and NOT in the
        # ORDER BY clause — re-embedding in ORDER BY would double the
        # number of `?` placeholders to bind (and historically caused
        # the per-row SUM to silently return 0, see commit history).
        # The final ordering is done in Python after rounding.
        weights = self.display_weights
        canonicals = [n for n in included_model_names if n in weights]
        if canonicals:
            case_parts = " ".join(
                f"WHEN m.model_name = ? THEN {weights[n]!r} "
                for n in canonicals
            )
            weight_expr = f"CASE {case_parts}ELSE 1.0 END"
            weight_params: list = list(canonicals) * 2
        else:
            weight_expr = "1.0"
            weight_params = []
        rows = self.conn.execute(
            f"SELECT a.email, a.display_name, "
            f"COALESCE(ROUND(SUM(m.input_tokens * {weight_expr})), 0) AS input_tokens, "
            f"COALESCE(ROUND(SUM(m.output_tokens * {weight_expr})), 0) AS output_tokens, "
            f"COUNT(m.model_name) AS model_count "
            f"FROM accounts a "
            f"LEFT JOIN model_usage m "
            f"  ON a.email = m.email "
            f"  AND m.cycle_start = ? "
            f"  {model_filter_sql} "
            f"WHERE a.enabled = 1 "
            f"GROUP BY a.email, a.display_name",
            (*weight_params, cycle_start, *model_params),
        ).fetchall()
        # Final sort happens in Python so we don't have to re-embed
        # `weight_expr` in an ORDER BY clause (which would double-bind
        # the `?` placeholders and historically produced wrong results).
        # SQLite ROUND is "round half away from zero"; Python's
        # `get_model_usage_for_account` uses `int(round(x * w))` which
        # is banker's rounding. In the common case the two agree (the
        # `0.5 * odd` case is the only ambiguity, off-by-one for those
        # specific rows only). The per-model breakdown is the source of
        # truth for the dashboard tooltip.
        out: list[dict] = []
        for r in rows:
            out.append(
                {
                    "email": r["email"],
                    "display_name": r["display_name"],
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "model_count": r["model_count"],
                }
            )
        out.sort(
            key=lambda r: (
                -((r["input_tokens"] or 0) + (r["output_tokens"] or 0)),
                r["email"],
            )
        )
        return out

    def get_model_usage_for_account(
        self, email: str, cycle_start: str, included_model_names: set[str]
    ) -> list[ModelUsage]:
        """Per-model breakdown for one account in the given cycle.

        `included_model_names` restricts which model_usage rows are
        returned. An empty set returns no rows.

        Display weights from `self.display_weights` are applied to each
        row's input_tokens / output_tokens (using ROUND to keep ints) so
        the per-model breakdown matches the dashboard totals.

        Returns one row per model.
        """
        if not included_model_names:
            return []
        placeholders = ",".join("?" * len(included_model_names))
        rows = self.conn.execute(
            "SELECT email, cycle_start, cycle_end, model_name, model_type, "
            "model_source, input_tokens, output_tokens "
            "FROM model_usage WHERE email=? AND cycle_start=? "
            f"AND LOWER(model_name) IN ({placeholders}) "
            "ORDER BY (input_tokens + output_tokens) DESC, model_name",
            (email, cycle_start, *[n.lower() for n in sorted(included_model_names)]),
        ).fetchall()
        out: list[ModelUsage] = []
        for r in rows:
            w = self.display_weights.get(r["model_name"], 1.0)
            # Use "round half away from zero" (same as SQLite ROUND) so
            # per-model rows aggregate to the same totals as
            # `get_model_usage_by_account` (which uses SQLite ROUND in
            # its SQL). Banker's rounding in `round()` would silently
            # off-by-one for the `odd * 0.5` case.
            in_tok = int((r["input_tokens"] * w + 0.5) // 1) if w >= 0 else int((r["input_tokens"] * w - 0.5) // 1)
            out_tok = int((r["output_tokens"] * w + 0.5) // 1) if w >= 0 else int((r["output_tokens"] * w - 0.5) // 1)
            out.append(
                ModelUsage(
                    r["email"],
                    r["cycle_start"],
                    r["cycle_end"],
                    r["model_name"],
                    r["model_type"],
                    r["model_source"],
                    in_tok,
                    out_tok,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Prune / cleanup operations
    # ------------------------------------------------------------------

    def prune_zero_data_accounts(self) -> dict:
        """Delete accounts whose total tokens are zero.

        Cascade-deletes their model_usage rows.
        Returns: {deleted_accounts, deleted_model_rows}.

        Concurrency: same rationale as `delete_account` — explicit
        two-step DELETE, no PRAGMA toggling on the shared connection.
        """
        rows = self.conn.execute(
            "SELECT a.email, "
            "COALESCE(SUM(m.input_tokens + m.output_tokens), 0) AS total "
            "FROM accounts a LEFT JOIN model_usage m ON a.email = m.email "
            "GROUP BY a.email"
        ).fetchall()
        zero_emails = [r["email"] for r in rows if (r["total"] or 0) == 0]
        if not zero_emails:
            return {"deleted_accounts": 0, "deleted_model_rows": 0}

        placeholders = ",".join("?" * len(zero_emails))
        model_count = self.conn.execute(
            f"SELECT COUNT(*) FROM model_usage WHERE email IN ({placeholders})",
            zero_emails,
        ).fetchone()[0]

        # Delete model_usage first, then accounts — so FK stays satisfied
        # throughout. No PRAGMA toggle needed.
        self.conn.execute(
            f"DELETE FROM model_usage WHERE email IN ({placeholders})",
            zero_emails,
        )
        self.conn.execute(
            f"DELETE FROM accounts WHERE email IN ({placeholders})",
            zero_emails,
        )

        return {
            "deleted_accounts": len(zero_emails),
            "deleted_model_rows": model_count,
        }

    def prune_orphan_model_usage(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM model_usage " "WHERE email NOT IN (SELECT email FROM accounts)"
        )
        return cur.rowcount or 0

    def prune_old_snapshots(self, keep_last: int = 5) -> int:
        """Delete the oldest snapshots beyond `keep_last`.

        Snapshots are kept as an audit trail; this method trims the tail.
        Returns the number of rows deleted.

        Implemented as a single DELETE with a subquery — no N+1 SELECTs
        and no Python-side id list stitching. `keep_last=0` correctly
        deletes all rows (NOT IN (SELECT ... LIMIT 0) returns all ids).
        """
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        cur = self.conn.execute(
            "DELETE FROM snapshots "
            "WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT ?)",
            (keep_last,),
        )
        return cur.rowcount or 0
