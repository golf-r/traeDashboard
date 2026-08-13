"""Tests for trae_dashboard.storage."""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from trae_dashboard.storage import Storage, ModelUsage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "test.db")
    s.init()
    return s


def test_init_creates_tables(storage: Storage):
    conn = storage.conn
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "accounts" in tables
    assert "model_usage" in tables
    assert "snapshots" in tables


def test_model_usage_has_amount_columns(storage: Storage):
    cols = {r["name"] for r in storage.conn.execute("PRAGMA table_info(model_usage)")}
    assert "amount_total" in cols
    assert "amount_basic" in cols
    assert "amount_pay_go" in cols
    assert "currency" in cols
    # token columns retained for tooltip
    assert "input_tokens" in cols
    assert "output_tokens" in cols


def test_upsert_and_read_model_usage(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=200,
        amount_total=12.34, amount_basic=10.0, amount_pay_go=2.34,
        currency="CNY",
    )
    rows = storage.get_model_usage_for_account("a@x.com", "2026-07-10")
    assert len(rows) == 1
    r = rows[0]
    assert r.model_name == "GLM-5.1"
    assert r.amount_total == pytest.approx(12.34)
    assert r.amount_basic == pytest.approx(10.0)
    assert r.amount_pay_go == pytest.approx(2.34)
    assert r.currency == "CNY"
    assert r.input_tokens == 100
    assert r.output_tokens == 200


def test_get_model_usage_by_account_returns_amount(storage: Storage):
    storage.upsert_account("a@x.com", "Alice")
    storage.upsert_account("b@x.com", "Bob")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        amount_total=50.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        amount_total=30.0,
    )
    rows = storage.get_model_usage_by_account("2026-07-10", "2026-08-09")
    assert len(rows) == 2
    # Sorted by amount_total DESC
    assert rows[0]["email"] == "a@x.com"
    assert rows[0]["amount_total"] == pytest.approx(50.0)
    assert rows[1]["email"] == "b@x.com"
    assert rows[1]["amount_total"] == pytest.approx(30.0)
    # Each row should include per-model details
    assert "models" in rows[0]
    assert rows[0]["models"][0]["amount_total"] == pytest.approx(50.0)


def test_get_total_amount(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_account("b@x.com", "B")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=50.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M2", model_source="Trae", amount_total=30.0,
    )
    total = storage.get_total_amount("2026-07-10")
    assert total == pytest.approx(80.0)


def test_get_total_amount_excludes_disabled_accounts(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_account("b@x.com", "B")
    # Disable b via direct SQL (upsert_account doesn't take enabled param in current code)
    storage.conn.execute("UPDATE accounts SET enabled=0 WHERE email='b@x.com'")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=50.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M2", model_source="Trae", amount_total=30.0,
    )
    total = storage.get_total_amount("2026-07-10")
    assert total == pytest.approx(50.0)


def test_prune_zero_data_accounts_uses_amount(storage: Storage):
    storage.upsert_account("keep@x.com", "Keep")
    storage.upsert_account("zero@x.com", "Zero")
    storage.upsert_model_usage(
        email="keep@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=10.0,
        input_tokens=0, output_tokens=0,  # token 为零但 amount>0,不应被删
    )
    storage.upsert_model_usage(
        email="zero@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=0.0,
        input_tokens=100, output_tokens=200,  # token 非零但 amount=0,应被删
    )
    deleted = storage.prune_zero_data_accounts()
    assert deleted == {"deleted_accounts": 1, "deleted_model_rows": 1}
    remaining = {a.email for a in storage.list_accounts()}
    assert remaining == {"keep@x.com"}


def test_migration_clears_old_data(tmp_path: Path):
    """Simulate legacy schema (no amount columns) and verify migration clears rows."""
    db_path = tmp_path / "legacy.db"
    # Create old-style table without amount columns
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE accounts (
          email TEXT PRIMARY KEY, display_name TEXT, enabled INTEGER DEFAULT 1
        );
        CREATE TABLE model_usage (
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
          UNIQUE(email, cycle_start, model_name)
        );
        INSERT INTO accounts VALUES ('a@x.com', 'A', 1);
        INSERT INTO model_usage (email, cycle_start, cycle_end, model_name, model_source, input_tokens, output_tokens)
          VALUES ('a@x.com', '2026-07-10', '2026-08-09', 'OLD', 'Trae', 999, 999);
        """
    )
    conn.commit()
    conn.close()

    # Now open with Storage — triggers migration
    s = Storage(db_path)
    s.init()
    rows = s.conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
    assert rows == 0  # old data cleared
    # New columns exist
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(model_usage)")}
    assert "amount_total" in cols
    s.close()


def test_upsert_model_usage_replaces_on_conflict(storage: Storage):
    storage.upsert_account("a@x.com", "A")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=10.0,
    )
    storage.upsert_model_usage(
        email="a@x.com", cycle_start="2026-07-10", cycle_end="2026-08-09",
        model_name="M1", model_source="Trae", amount_total=20.0,
    )
    rows = storage.get_model_usage_for_account("a@x.com", "2026-07-10")
    assert len(rows) == 1
    assert rows[0].amount_total == pytest.approx(20.0)
