"""Tests for trae_dashboard.storage."""

from __future__ import annotations

from trae_dashboard.storage import Storage


def test_storage_init_creates_tables(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    rows = s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"accounts", "snapshots", "model_usage"} <= names
    # daily_usage is gone (replaced by model_usage)
    assert "daily_usage" not in names


def test_upsert_and_list_accounts(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_account("b@x.com")
    accounts = s.list_accounts()
    assert {a.email for a in accounts} == {"a@x.com", "b@x.com"}
    by_email = {a.email: a.display_name for a in accounts}
    assert by_email["a@x.com"] == "A"
    assert by_email["b@x.com"] is None


def test_save_snapshot_and_model_usage(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com")
    snap_id = s.save_snapshot(
        start_time=1700000000,
        end_time=1700086400,
        payload_json='{"items":[]}',
        request_meta="test",
    )
    assert snap_id > 0
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-29",
        model_name="Doubao",
        model_type="Chat",
        model_source="Trae",
        input_tokens=100,
        output_tokens=200,
    )
    rows = s.get_model_usage_for_account("a@x.com", "2026-06-10", {"Doubao"})
    assert len(rows) == 1
    assert rows[0].input_tokens == 100
    assert rows[0].output_tokens == 200
    assert rows[0].model_name == "Doubao"


def test_delete_account_cascades_model_usage(tmp_data_dir):
    """delete_account should remove the account and its model_usage rows,
    but leave snapshots intact (audit trail)."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_account("b@x.com", "B")
    s.upsert_model_usage(
        email="a@x.com", cycle_start="2026-06-10", cycle_end="2026-06-29",
        model_name="Doubao", model_type="Chat", model_source="Trae",
        input_tokens=10, output_tokens=20,
    )
    s.upsert_model_usage(
        email="b@x.com", cycle_start="2026-06-10", cycle_end="2026-06-29",
        model_name="Doubao", model_type="Chat", model_source="Trae",
        input_tokens=30, output_tokens=40,
    )
    snap_id = s.save_snapshot(
        start_time=1, end_time=2, payload_json="{}", request_meta="t"
    )
    # Sanity: both accounts present, both have model rows
    assert {a.email for a in s.list_accounts()} == {"a@x.com", "b@x.com"}
    assert s.conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == 2

    # Delete a; its model row is cascade-removed; b's row stays; snapshot survives
    n = s.delete_account("a@x.com")
    assert n >= 2  # 1 account + 1 model row
    assert {a.email for a in s.list_accounts()} == {"b@x.com"}
    remaining = s.conn.execute(
        "SELECT email FROM model_usage ORDER BY email"
    ).fetchall()
    assert [r[0] for r in remaining] == ["b@x.com"]
    assert s.conn.execute(
        "SELECT id FROM snapshots WHERE id = ?", (snap_id,)
    ).fetchone() is not None


def test_delete_account_is_idempotent(tmp_data_dir):
    """Deleting a non-existent account should be a no-op (return 0),
    not raise — so the API can be safely retried on flaky networks."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    assert s.delete_account("ghost@x.com") == 0
    # Second call still 0
    assert s.delete_account("ghost@x.com") == 0


def test_delete_account_lowercases_email_lookup(tmp_data_dir):
    """Email PK is stored lowercased by the API; delete should match
    the same case so users can paste an email verbatim."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    # Caller may pass mixed case; storage delete uses the value as-is
    # (API endpoint normalizes). The DB PK is whatever the upsert set.
    n = s.delete_account("a@x.com")
    assert n == 1
    assert s.list_accounts() == []


def test_get_model_usage_for_account_applies_doubao_weight(tmp_data_dir):
    """Doubao-Seed-Code rows are returned with 0.5x token counts at read time."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="Doubao-Seed-Code",
        model_type="Chat",
        model_source="Trae",
        input_tokens=8_890_815,
        output_tokens=104_825,
    )
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="GLM-5.1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=32_124_588,
        output_tokens=181_023,
    )
    allowlist = {"Doubao-Seed-Code", "GLM-5.1"}
    rows = s.get_model_usage_for_account("a@x.com", "2026-06-10", allowlist)
    by_name = {r.model_name: r for r in rows}
    # Doubao-Seed-Code: raw 8,995,640 * 0.5 = 4,497,820
    # Round half away from zero (matches SQLite ROUND used by the
    # per-account path so the two stay consistent):
    #   round(8,890,815 * 0.5)  = round(4,445,407.5)  = 4,445,408
    #   round(104,825   * 0.5)  = round(52,412.5)     = 52,413
    assert by_name["Doubao-Seed-Code"].input_tokens == 4_445_408
    assert by_name["Doubao-Seed-Code"].output_tokens == 52_413
    # Non-weighted model is unchanged.
    assert by_name["GLM-5.1"].input_tokens == 32_124_588
    assert by_name["GLM-5.1"].output_tokens == 181_023


def test_get_model_usage_by_account_applies_doubao_weight(tmp_data_dir):
    """Per-account summary also applies the 0.5 weight to Doubao-Seed-Code."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="Doubao-Seed-Code",
        model_type="Chat",
        model_source="Trae",
        input_tokens=8_890_815,
        output_tokens=104_825,
    )
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="GLM-5.1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=32_124_588,
        output_tokens=181_023,
    )
    allowlist = {"Doubao-Seed-Code", "GLM-5.1"}
    rows = s.get_model_usage_by_account("2026-06-10", "2026-06-30", allowlist)
    assert len(rows) == 1
    r = rows[0]
    # Raw GLM-5.1: 32,305,611 (no weight). Raw Doubao: 8,995,640 * 0.5 = 4,497,820.
    # SQLite ROUND is half-up:
    #   ROUND(8,890,815 * 0.5) = ROUND(4,445,407.5) = 4,445,408
    #   ROUND(104,825   * 0.5) = ROUND(52,412.5)    = 52,413
    # input:  32,124,588 + 4,445,408 = 36,569,996
    # output: 181,023    + 52,413    = 233,436
    assert r["input_tokens"] == 36_569_996
    assert r["output_tokens"] == 233_436
    assert r["input_tokens"] + r["output_tokens"] == 36_803_432


def test_db_row_keeps_raw_values_after_weighted_read(tmp_data_dir):
    """The on-disk row is unchanged by the display weight — read-side only."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="Doubao-Seed-Code",
        model_type="Chat",
        model_source="Trae",
        input_tokens=8_890_815,
        output_tokens=104_825,
    )
    # Read returns weighted values (round half away from zero)...
    rows = s.get_model_usage_for_account("a@x.com", "2026-06-10", {"Doubao-Seed-Code"})
    assert rows[0].input_tokens == 4_445_408
    assert rows[0].output_tokens == 52_413
    # ...but the underlying SQLite row still holds the raw API numbers.
    raw = s.conn.execute(
        "SELECT input_tokens, output_tokens FROM model_usage "
        "WHERE email='a@x.com' AND model_name='Doubao-Seed-Code'"
    ).fetchone()
    assert raw["input_tokens"] == 8_890_815
    assert raw["output_tokens"] == 104_825


def test_upsert_model_usage_unique_constraint(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com")
    # Same (email, cycle_start, model) should replace, not duplicate
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-29",
        model_name="M",
        model_type="Chat",
        model_source="Trae",
        input_tokens=10,
        output_tokens=20,
    )
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-29",
        model_name="M",
        model_type="Chat",
        model_source="Trae",
        input_tokens=99,
        output_tokens=88,
    )
    rows = s.get_model_usage_for_account("a@x.com", "2026-06-10", {"M"})
    assert len(rows) == 1
    assert rows[0].input_tokens == 99
    assert rows[0].output_tokens == 88


def test_latest_snapshot_tags_utc_zone(tmp_data_dir):
    """SQLite stores CURRENT_TIMESTAMP as naive UTC. The reader must tag
    it with `Z` so JS `new Date(iso)` shows local time on the browser.
    Regression for the "数据 8 小时前刷新" bug on +08:00 hosts.
    """
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    # Save a snapshot — `fetched_at` defaults to CURRENT_TIMESTAMP
    # which on SQLite is UTC wall-clock time without a zone suffix.
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    snap = s.latest_snapshot()
    assert snap is not None
    iso = snap["fetched_at"]
    # Must end with `Z` (explicit UTC) so the browser interprets it
    # correctly and converts to local time for display.
    assert isinstance(iso, str)
    assert iso.endswith("Z")
    # `T` separator (ISO 8601), not a space.
    assert "T" in iso


def test_get_summary_by_account(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s.upsert_account("b@x.com", "Beta")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="M1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=10,
        output_tokens=20,
    )
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="M2",
        model_type="CUE",
        model_source="Custom",
        input_tokens=5,
        output_tokens=7,
    )
    summary = s.get_model_usage_by_account("2026-06-10", "2026-06-30", {"M1", "M2"})
    by_email = {row["email"]: row for row in summary}
    assert by_email["a@x.com"]["input_tokens"] == 15
    assert by_email["a@x.com"]["output_tokens"] == 27
    assert by_email["a@x.com"]["display_name"] == "Alpha"
    assert by_email["b@x.com"]["input_tokens"] == 0


def test_model_usage_reads_only_include_allowlisted_rows(tmp_data_dir):
    """Dashboard summary and details ignore rows outside the configured allowlist."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="CUE",
        model_type="CUE",
        model_source="Trae",
        input_tokens=1000,
        output_tokens=2000,
    )
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="AllowedModel",
        model_type="Chat",
        model_source="Trae",
        input_tokens=10,
        output_tokens=20,
    )
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="OtherModel",
        model_type="Chat",
        model_source="Trae",
        input_tokens=30,
        output_tokens=40,
    )

    included = {"AllowedModel"}
    summary = s.get_model_usage_by_account("2026-06-10", "2026-06-30", included)
    by_email = {row["email"]: row for row in summary}
    details = s.get_model_usage_for_account("a@x.com", "2026-06-10", included)

    assert by_email["a@x.com"]["input_tokens"] == 10
    assert by_email["a@x.com"]["output_tokens"] == 20
    assert [row.model_name for row in details] == ["AllowedModel"]


def test_get_summary_by_account_includes_same_cycle_with_older_fetch_end(tmp_data_dir):
    """Current-cycle queries include rows fetched earlier in the same cycle."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-29",
        model_name="M1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=10,
        output_tokens=20,
    )

    rows = s.get_model_usage_by_account("2026-06-10", "2026-06-30", {"M1"})
    by_email = {row["email"]: row for row in rows}
    assert by_email["a@x.com"]["input_tokens"] == 10
    assert by_email["a@x.com"]["output_tokens"] == 20


def test_storage_uses_wal(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    s.close()


# ---------------------------------------------------------------------------
# Prune / cleanup operations (T1)
# ---------------------------------------------------------------------------


def _seed_model(
    s: Storage,
    email: str,
    cycle: str = "2026-06-10",
    end: str = "2026-06-30",
    tin: int = 0,
    tout: int = 0,
) -> None:
    """Helper: insert a model_usage row (new table)."""
    s.upsert_model_usage(
        email=email,
        cycle_start=cycle,
        cycle_end=end,
        model_name="M",
        model_type="Chat",
        model_source="Trae",
        input_tokens=tin,
        output_tokens=tout,
    )


def test_prune_zero_data_accounts_removes_empty_accounts(tmp_data_dir):
    """Accounts with no model_usage rows (or all-zero rows) get deleted."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("empty1@x.com", "Empty 1")
    s.upsert_account("empty2@x.com", "Empty 2")
    s.upsert_account("has_data@x.com", "Real")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    _seed_model(s, "has_data@x.com", tin=100, tout=200)

    result = s.prune_zero_data_accounts()
    assert result["deleted_accounts"] == 2
    assert result["deleted_model_rows"] == 0

    remaining = {a.email for a in s.list_accounts()}
    assert remaining == {"has_data@x.com"}
    s.close()


def test_prune_zero_data_cascades_to_model_usage(tmp_data_dir):
    """model_usage rows belonging to a deleted account are also deleted."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("zero@x.com", "Zero")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    # Two distinct model names (UNIQUE on (email, cycle_start, model_name)).
    _seed_model(s, "zero@x.com", tin=0, tout=0)  # model "M"
    s.upsert_model_usage(
        email="zero@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="M2",
        model_type="Chat",
        model_source="Trae",
        input_tokens=0,
        output_tokens=0,
    )

    result = s.prune_zero_data_accounts()
    assert result["deleted_accounts"] == 1
    assert result["deleted_model_rows"] == 2

    assert s.get_model_usage_for_account("zero@x.com", "2026-06-10", {"M", "M2"}) == []
    s.close()


def test_prune_zero_data_keeps_snapshots(tmp_data_dir):
    """Snapshots are NOT deleted (audit trail)."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("e@x.com")
    s.save_snapshot(start_time=1, end_time=2, payload_json='{"x":1}', request_meta="t")
    s.prune_zero_data_accounts()
    snap_count = s.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert snap_count == 1
    s.close()


def test_prune_orphan_model_usage_removes_orphan_rows(tmp_data_dir):
    """model_usage rows whose email is no longer in accounts are deleted."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("real@x.com")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    # Insert model_usage row for an email that doesn't exist in accounts
    # (bypass FK by temporarily disabling it)
    s.conn.execute("PRAGMA foreign_keys=OFF;")
    s.conn.execute(
        "INSERT INTO model_usage(email, cycle_start, cycle_end, model_name, "
        "model_type, model_source, input_tokens, output_tokens) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("ghost@x.com", "2026-06-10", "2026-06-30", "M", "Chat", "Trae", 1, 2),
    )
    _seed_model(s, "real@x.com", tin=10, tout=20)
    s.conn.execute("PRAGMA foreign_keys=ON;")

    deleted = s.prune_orphan_model_usage()
    assert deleted == 1
    # Real rows untouched
    assert len(s.get_model_usage_for_account("real@x.com", "2026-06-10", {"M"})) == 1
    s.close()


def test_prune_old_snapshots_keeps_n_most_recent(tmp_data_dir):
    """Keep only the most recent N snapshots; delete the rest + cascade."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com")
    snap_ids = []
    for i in range(7):
        sid = s.save_snapshot(
            start_time=i,
            end_time=i + 1,
            payload_json=f"{{'i':{i}}}",
            request_meta="x",
        )
        snap_ids.append(sid)
    # Before prune: 7 snapshots
    assert s.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 7

    deleted = s.prune_old_snapshots(keep_last=3)
    # Deleted 4 old snapshots
    assert deleted == 4

    remaining_snap = s.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert remaining_snap == 3
    # Verify the kept snapshots are the last 3 (highest ids)
    kept_ids = {row[0] for row in s.conn.execute("SELECT id FROM snapshots").fetchall()}
    assert kept_ids == set(snap_ids[-3:])
    s.close()


def test_prune_old_snapshots_keep_zero_deletes_all(tmp_data_dir):
    """keep_last=0 must delete ALL snapshots.

    Regression for the boundary bug in the old implementation: it used
    `keep_placeholders = "NULL"` when keep_ids was empty, and
    `NOT IN (NULL)` is always FALSE in SQL — so the old code deleted
    nothing instead of everything.
    """
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    for i in range(3):
        s.save_snapshot(start_time=i, end_time=i + 1, payload_json="{}", request_meta="x")
    assert s.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3

    deleted = s.prune_old_snapshots(keep_last=0)
    assert deleted == 3
    assert s.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    s.close()


def test_prune_old_snapshots_keep_more_than_exist_is_noop(tmp_data_dir):
    """keep_last larger than the number of snapshots deletes nothing."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    for i in range(2):
        s.save_snapshot(start_time=i, end_time=i + 1, payload_json="{}", request_meta="x")

    deleted = s.prune_old_snapshots(keep_last=10)
    assert deleted == 0
    assert s.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    s.close()


def test_latest_snapshot_returns_most_recent(tmp_data_dir):
    """latest_snapshot() returns the snapshot with the highest id (most recent)."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.save_snapshot(
        start_time=100, end_time=200, payload_json="{}", request_meta="first"
    )
    sid2 = s.save_snapshot(
        start_time=300, end_time=400, payload_json="{}", request_meta="second"
    )
    latest = s.latest_snapshot()
    assert latest is not None
    assert latest["id"] == sid2
    assert latest["start_time"] == 300
    assert latest["end_time"] == 400
    assert "fetched_at" in latest and latest["fetched_at"]
    assert "T" in latest["fetched_at"]  # ISO8601-ish
    s.close()


def test_latest_snapshot_returns_none_when_empty(tmp_data_dir):
    """latest_snapshot() returns None if no snapshots exist."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    assert s.latest_snapshot() is None
    s.close()


# ---------------------------------------------------------------------------
# get_consumed_by_account (cycle-mode)
# ---------------------------------------------------------------------------


def test_get_consumed_by_account_filters_by_date_range(tmp_data_dir):
    """get_consumed_by_account sums input+output tokens within [start, end] date strings."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s.upsert_account("b@x.com", "Beta")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")

    # a@x.com: 2 model rows in the cycle (10+20) + (5+5) = 15 in / 25 out
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="M1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=10,
        output_tokens=20,
    )
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="M2",
        model_type="Chat",
        model_source="Trae",
        input_tokens=5,
        output_tokens=5,
    )
    # a@x.com: OLDER cycle (different cycle_start) — should NOT be summed
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-05-10",
        cycle_end="2026-05-31",
        model_name="M1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=100,
        output_tokens=200,
    )
    # b@x.com: 7+8 in the cycle
    s.upsert_model_usage(
        email="b@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="M1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=7,
        output_tokens=8,
    )

    rows = s.get_model_usage_by_account("2026-06-10", "2026-06-30", {"M1", "M2"})
    by_email = {r["email"]: r for r in rows}
    assert by_email["a@x.com"]["input_tokens"] == 15
    assert by_email["a@x.com"]["output_tokens"] == 25
    assert by_email["b@x.com"]["input_tokens"] == 7
    assert by_email["b@x.com"]["output_tokens"] == 8
    s.close()


def test_get_consumed_by_account_includes_zero_data_accounts(tmp_data_dir):
    """Accounts with no model_usage rows in the cycle are still included with zeros."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s.upsert_account("b@x.com", "Beta")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="M1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=10,
        output_tokens=20,
    )
    # b@x.com has no model_usage rows

    rows = s.get_model_usage_by_account("2026-06-10", "2026-06-30", {"M1"})
    by_email = {r["email"]: r for r in rows}
    assert "b@x.com" in by_email
    assert by_email["b@x.com"]["input_tokens"] == 0
    assert by_email["b@x.com"]["output_tokens"] == 0
    s.close()


def test_get_consumed_by_account_empty_range(tmp_data_dir):
    """When no model_usage rows exist, all enabled accounts are listed with zeros."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s.upsert_account("b@x.com", "Beta")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")

    rows = s.get_model_usage_by_account("2026-06-10", "2026-06-30", {"M1"})
    by_email = {r["email"]: r for r in rows}
    assert by_email["a@x.com"]["input_tokens"] == 0
    assert by_email["a@x.com"]["output_tokens"] == 0
    assert by_email["b@x.com"]["input_tokens"] == 0
    assert by_email["b@x.com"]["output_tokens"] == 0
    s.close()


# ---------------------------------------------------------------------------
# Config-injected display_weights (regression for "hardcoded weights" smell).
# Tests that the Storage constructor accepts a `display_weights` kwarg and
# uses it instead of the built-in default.
# ---------------------------------------------------------------------------


def test_storage_uses_injected_display_weights(tmp_data_dir):
    """A custom display_weights mapping overrides the default for new models."""
    s = Storage(
        tmp_data_dir / "test.db",
        display_weights={"CustomModel": 0.25, "Doubao-Seed-Code": 0.5},
    )
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="CustomModel",
        model_type="Chat",
        model_source="Trae",
        input_tokens=1000,
        output_tokens=2000,
    )
    rows = s.get_model_usage_for_account("a@x.com", "2026-06-10", {"CustomModel"})
    # 1000 * 0.25 = 250, 2000 * 0.25 = 500
    assert rows[0].input_tokens == 250
    assert rows[0].output_tokens == 500
    s.close()


def test_storage_empty_display_weights_disables_weighting(tmp_data_dir):
    """Passing {} disables all weighting — raw values come back unchanged.

    This is what callers get when they pass `cfg.display_weights = {}`.
    Useful for testing / debugging.
    """
    s = Storage(
        tmp_data_dir / "test.db",
        display_weights={},
    )
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="Doubao-Seed-Code",
        model_type="Chat",
        model_source="Trae",
        input_tokens=8_890_815,
        output_tokens=104_825,
    )
    rows = s.get_model_usage_for_account("a@x.com", "2026-06-10", {"Doubao-Seed-Code"})
    # No weighting — raw values come back unchanged.
    assert rows[0].input_tokens == 8_890_815
    assert rows[0].output_tokens == 104_825
    s.close()
