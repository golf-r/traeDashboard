"""SQLite storage layer (WAL mode) for trae-dashboard.

Schema:
- accounts: tracked accounts (email PK, display_name)
- snapshots: every fetch's raw response payload (audit trail)
- model_usage: per-account × per-model totals for a cycle window
  (one row per (email, cycle_start, model_name); total across rows = cycle total)
"""

from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from pathlib import Path


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
  amount_total REAL DEFAULT 0,
  amount_basic REAL DEFAULT 0,
  amount_pay_go REAL DEFAULT 0,
  currency TEXT DEFAULT 'CNY',
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
    amount_total: float
    amount_basic: float
    amount_pay_go: float
    currency: str


class Storage:
    def __init__(
        self,
        db_path: str | Path,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.row_factory = sqlite3.Row

    def init(self) -> None:
        self.conn.executescript(SCHEMA)
        # Migration: add amount columns if missing (clear old data first
        # — old rows lack amount values and would skew totals).
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(model_usage)")}
        if "amount_total" not in cols:
            self.conn.execute("DELETE FROM model_usage")
            for col, decl in [
                ("amount_total", "REAL DEFAULT 0"),
                ("amount_basic", "REAL DEFAULT 0"),
                ("amount_pay_go", "REAL DEFAULT 0"),
                ("currency", "TEXT DEFAULT 'CNY'"),
            ]:
                self.conn.execute(f"ALTER TABLE model_usage ADD COLUMN {col} {decl}")

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
        model_type: str | None = None,
        model_source: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        amount_total: float = 0.0,
        amount_basic: float = 0.0,
        amount_pay_go: float = 0.0,
        currency: str = "CNY",
    ) -> None:
        """Write one row per (email, cycle_start, model_name).

        UNIQUE on (email, cycle_start, model_name) means re-fetching the
        same cycle window **overwrites** the old row (no accumulation).
        """
        self.conn.execute(
            "INSERT INTO model_usage(email, cycle_start, cycle_end, model_name, "
            "model_type, model_source, input_tokens, output_tokens, "
            "amount_total, amount_basic, amount_pay_go, currency) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(email, cycle_start, model_name) DO UPDATE SET "
            "cycle_end=excluded.cycle_end, "
            "model_type=excluded.model_type, model_source=excluded.model_source, "
            "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
            "amount_total=excluded.amount_total, amount_basic=excluded.amount_basic, "
            "amount_pay_go=excluded.amount_pay_go, currency=excluded.currency, "
            "fetched_at=CURRENT_TIMESTAMP",
            (
                email, cycle_start, cycle_end, model_name, model_type, model_source,
                input_tokens, output_tokens,
                amount_total, amount_basic, amount_pay_go, currency,
            ),
        )

    def get_model_usage_by_account(
        self, cycle_start: str, cycle_end: str
    ) -> list[dict]:
        """Per-account totals for a cycle.

        `cycle_end` is accepted for caller context, but rows are matched by
        `cycle_start` only (a row's stored cycle_end is the fetch cutoff).

        Returns list of dicts:
          { email, display_name, amount_total, input_tokens, output_tokens,
            model_count, models: [...] }
        Accounts with no model_usage rows are included with zeros.
        Only enabled accounts are listed. Sorted by amount_total desc.
        """
        rows = self.conn.execute(
            "SELECT a.email, a.display_name, "
            "COALESCE(SUM(m.amount_total), 0) AS amount_total, "
            "COALESCE(SUM(m.input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(m.output_tokens), 0) AS output_tokens, "
            "COUNT(m.model_name) AS model_count "
            "FROM accounts a "
            "LEFT JOIN model_usage m "
            "  ON a.email = m.email "
            "  AND m.cycle_start = ? "
            "WHERE a.enabled = 1 "
            "GROUP BY a.email, a.display_name",
            (cycle_start,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            models = self.conn.execute(
                "SELECT model_name, model_type, model_source, input_tokens, output_tokens, "
                "amount_total, amount_basic, amount_pay_go, currency "
                "FROM model_usage WHERE email=? AND cycle_start=? "
                "ORDER BY amount_total DESC",
                (r["email"], cycle_start),
            ).fetchall()
            out.append({
                "email": r["email"],
                "display_name": r["display_name"],
                "amount_total": float(r["amount_total"] or 0),
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "model_count": r["model_count"],
                "models": [dict(m) for m in models],
            })
        out.sort(key=lambda r: (-r["amount_total"], r["email"]))
        return out

    def get_total_amount(self, cycle_start: str) -> float:
        """当前周期所有启用账号的金额消耗总和."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(m.amount_total), 0) AS total "
            "FROM model_usage m JOIN accounts a ON a.email = m.email "
            "WHERE m.cycle_start=? AND a.enabled=1",
            (cycle_start,),
        ).fetchone()
        return float(row["total"] or 0)

    def get_model_usage_for_account(
        self, email: str, cycle_start: str
    ) -> list[ModelUsage]:
        """Per-model breakdown for one account in the given cycle.

        Returns one row per model, sorted by amount_total desc.
        """
        rows = self.conn.execute(
            "SELECT email, cycle_start, cycle_end, model_name, model_type, "
            "model_source, input_tokens, output_tokens, "
            "amount_total, amount_basic, amount_pay_go, currency "
            "FROM model_usage WHERE email=? AND cycle_start=? "
            "ORDER BY amount_total DESC, model_name",
            (email, cycle_start),
        ).fetchall()
        out: list[ModelUsage] = []
        for r in rows:
            out.append(
                ModelUsage(
                    r["email"], r["cycle_start"], r["cycle_end"],
                    r["model_name"], r["model_type"], r["model_source"],
                    int(r["input_tokens"] or 0), int(r["output_tokens"] or 0),
                    float(r["amount_total"] or 0), float(r["amount_basic"] or 0),
                    float(r["amount_pay_go"] or 0), r["currency"] or "CNY",
                )
            )
        return out

    # ------------------------------------------------------------------
    # Prune / cleanup operations
    # ------------------------------------------------------------------

    def prune_zero_data_accounts(self) -> dict:
        """Delete accounts whose total amount_total is zero.

        Cascade-deletes their model_usage rows.
        Returns: {deleted_accounts, deleted_model_rows}.
        """
        rows = self.conn.execute(
            "SELECT a.email, "
            "COALESCE(SUM(m.amount_total), 0) AS total "
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
