"""Tests for trae_dashboard.api."""
from __future__ import annotations
import pytest
from datetime import datetime, timezone

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    from starlette.testclient import TestClient  # type: ignore

from trae_dashboard.api import create_app
from trae_dashboard.storage import Storage
from trae_dashboard.config import Config, Account, EmailConfig


def test_api_accounts_summary(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "A")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    # Seed model_usage for current cycle.
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="a@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=10, output_tokens=20,
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s",
        accounts=[Account("a@x.com", "A")],
        included_model_names={"M"},
    )
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["email"] == "a@x.com"
        assert data[0]["input_tokens"] == 10
        assert data[0]["output_tokens"] == 20
        assert data[0]["consumed"] == 30


def test_api_account_history(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s",
        accounts=[Account("a@x.com")],
    )
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts/a@x.com/history")
        assert r.status_code == 200
        items = r.json()
        # No model_usage row yet — empty list.
        assert items == []


def test_api_health(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
    )
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True


def test_api_accounts_default_days(tmp_data_dir):
    """Default days param should be 30."""
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com")
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[Account("a@x.com")],
        included_model_names={"M"},
    )
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts")
        assert r.status_code == 200
        # shape is a list
        assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# /api/status endpoint (T2)
# ---------------------------------------------------------------------------


def test_api_status_returns_expected_fields(tmp_data_dir):
    """/api/status returns health + data freshness indicator.

    Note: we deliberately do NOT expose `db_path` — it leaks an absolute
    filesystem path to the frontend and is unused there. Removed in the
    information-leak fix pass.
    """
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "A")
    # No snapshots yet → last_fetched_at / seconds_since_fetch are None
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[Account("a@x.com")],
        included_model_names={"M"},
    )
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200, r.text
        body = r.json()
    for key in (
        "ok", "last_fetched_at", "seconds_since_fetch",
        "total_accounts", "accounts_with_data",
        "total_quota", "total_consumed", "total_remaining",
    ):
        assert key in body, f"missing key {key} in {body}"
    # db_path must NOT be present — it leaks a filesystem path.
    assert "db_path" not in body
    assert body["ok"] is True
    assert body["last_fetched_at"] is None
    assert body["seconds_since_fetch"] is None
    assert body["total_accounts"] == 1
    assert body["accounts_with_data"] == 0


def test_api_status_reports_recent_snapshot(tmp_data_dir):
    """After saving a snapshot, /api/status reports last_fetched_at + age."""
    from trae_dashboard.cycle import current_cycle_window
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="a@x.com",
        cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=10, output_tokens=20,
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[Account("a@x.com")],
        included_model_names={"M"},
    )
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
    assert body["last_fetched_at"] is not None
    assert body["seconds_since_fetch"] is not None
    assert body["seconds_since_fetch"] >= 0
    assert body["total_accounts"] == 1
    assert body["accounts_with_data"] == 1


def test_api_status_counts_only_accounts_with_real_tokens(tmp_data_dir):
    """accounts_with_data excludes zero-token accounts."""
    from trae_dashboard.cycle import current_cycle_window
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("real@x.com", "Real")
    s.upsert_account("zero@x.com", "Zero")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="real@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=10, output_tokens=20,
    )
    # zero@x.com has a model row but with 0 tokens
    s.upsert_model_usage(
        email="zero@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=0, output_tokens=0,
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s",
        accounts=[Account("real@x.com"), Account("zero@x.com")],
        included_model_names={"M"},
    )
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        body = r.json()
    assert body["total_accounts"] == 2
    assert body["accounts_with_data"] == 1


# ---------------------------------------------------------------------------
# /api/status cycle fields (T5)
# ---------------------------------------------------------------------------


def _make_config(per_account_quota=50_000_000, included_model_names=None):
    """Default helper: include the canonical "M" model used across most tests.

    Pass an explicit `included_model_names` to override (e.g. for
    allowlist-filter tests).
    """
    if included_model_names is None:
        included_model_names = {"M"}
    return Config(
        openapi_base="x",
        auth_endpoint="/auth",
        app_id="i",
        app_secret="s",
        accounts=[Account("a@x.com", "A")],
        per_account_quota=per_account_quota,
        included_model_names=included_model_names,
    )


def test_api_status_returns_cycle_info(tmp_data_dir):
    """`/api/status` includes cycle_start / cycle_end / per_account_quota / total_quota."""
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "A")
    app = create_app(cfg=_make_config(per_account_quota=50_000_000), storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
    for key in (
        "cycle_start", "cycle_end", "per_account_quota", "total_quota",
        "total_consumed", "total_remaining", "utilization_pct",
    ):
        assert key in body, f"missing key {key} in {body}"
    assert body["cycle_start"].startswith("20")
    assert "T" in body["cycle_start"]
    assert body["per_account_quota"] == 50_000_000
    # 1 account with data → total_quota = 50M * 1
    assert body["total_quota"] == 50_000_000


def test_api_status_calculates_consumed_and_remaining(tmp_data_dir):
    """total_consumed = sum(input+output) in cycle, remaining = quota - consumed (clamped >=0)."""
    from datetime import datetime, timezone
    from trae_dashboard.cycle import current_cycle_window

    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_account("b@x.com", "B")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s_dt, e_dt = current_cycle_window()
    cycle_start = s_dt.date().isoformat()
    cycle_end = e_dt.date().isoformat()
    s.upsert_model_usage(
        email="a@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=200,
    )
    s.upsert_model_usage(
        email="b@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=50, output_tokens=25,
    )

    app = create_app(cfg=_make_config(per_account_quota=10_000), storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        body = r.json()

    # Total consumed = (100+200) + (50+25) = 375
    assert body["total_consumed"] == 375
    # per_account_quota=10000, 2 accounts with data → total_quota = 20000
    assert body["total_quota"] == 20_000
    # remaining = 20000 - 375 = 19625 (not clamped)
    assert body["total_remaining"] == 19_625
    # utilization_pct = round(375 / 20000 * 100, 2) = 1.88
    assert body["utilization_pct"] == 1.88
    assert body["accounts_with_data"] == 2


def test_api_status_remaining_clamped_to_zero(tmp_data_dir):
    """When consumed > quota, remaining is clamped to 0 (not negative)."""
    from trae_dashboard.cycle import current_cycle_window

    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "A")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="a@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=9_000_000, output_tokens=1_000_000,
    )
    app = create_app(cfg=_make_config(per_account_quota=1_000_000), storage=s)
    with TestClient(app) as client:
        body = client.get("/api/status").json()
    assert body["total_consumed"] == 10_000_000
    assert body["total_quota"] == 1_000_000
    assert body["total_remaining"] == 0
    assert body["utilization_pct"] == 1000.0


# ---------------------------------------------------------------------------
# /api/accounts?cycle=true (T5.2)
# ---------------------------------------------------------------------------


def test_api_accounts_with_cycle_param(tmp_data_dir):
    """`/api/accounts?cycle=true` returns per-account consumed in the cycle."""
    from trae_dashboard.cycle import current_cycle_window

    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s.upsert_account("b@x.com", "Beta")
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="a@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=50,
    )
    s.upsert_model_usage(
        email="b@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=20, output_tokens=10,
    )

    app = create_app(cfg=_make_config(), storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts?cycle=true")
        assert r.status_code == 200
        data = r.json()
    by_email = {row["email"]: row for row in data}
    # Shape: each row has consumed, input_tokens, output_tokens, etc.
    assert by_email["a@x.com"]["consumed"] == 150
    assert by_email["a@x.com"]["input_tokens"] == 100
    assert by_email["a@x.com"]["output_tokens"] == 50
    assert by_email["b@x.com"]["consumed"] == 30


# Legacy ?days= path was removed in cleanup; /api/accounts now only returns
# the per-cycle model_usage totals. No days= variant is supported.


# ---------------------------------------------------------------------------
# /api/accounts honors Config.included_model_names (allowlist)
# ---------------------------------------------------------------------------


def test_api_accounts_uses_configured_model_allowlist(tmp_data_dir):
    """Legacy non-allowlisted rows are ignored by /api/accounts."""
    from trae_dashboard.cycle import current_cycle_window

    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="a@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="AllowedModel", model_type="Chat", model_source="Trae",
        input_tokens=10, output_tokens=20,
    )
    s.upsert_model_usage(
        email="a@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="OtherModel", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=200,
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s",
        accounts=[Account("a@x.com", "Alpha")],
        included_model_names={"AllowedModel"},
    )
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        data = client.get("/api/accounts").json()

    assert data[0]["input_tokens"] == 10
    assert data[0]["output_tokens"] == 20
    assert data[0]["model_count"] == 1


def test_api_account_history_uses_configured_model_allowlist(tmp_data_dir):
    """Legacy non-allowlisted rows are ignored by /api/accounts/{email}/history."""
    from trae_dashboard.cycle import current_cycle_window

    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    s.upsert_account("a@x.com", "Alpha")
    s_dt, _ = current_cycle_window()
    cycle_start = s_dt.date().isoformat()
    s.upsert_model_usage(
        email="a@x.com", cycle_start=cycle_start, cycle_end="2026-06-29",
        model_name="AllowedModel", model_type="Chat", model_source="Trae",
        input_tokens=10, output_tokens=20,
    )
    s.upsert_model_usage(
        email="a@x.com", cycle_start=cycle_start, cycle_end="2026-06-29",
        model_name="OtherModel", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=200,
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s",
        accounts=[Account("a@x.com", "Alpha")],
        included_model_names={"AllowedModel"},
    )
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        items = client.get("/api/accounts/a@x.com/history").json()

    assert [r["model_name"] for r in items] == ["AllowedModel"]


# ---------------------------------------------------------------------------
# POST /api/accounts — add managed account
# ---------------------------------------------------------------------------


def test_api_post_account_creates_row(tmp_data_dir):
    """POST /api/accounts persists the new account and the next list
    read should see it. Display name defaults to the email local-part."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.post(
            "/api/accounts",
            json={"email": "newbie@company.com", "display_name": "新人"},
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["email"] == "newbie@company.com"
    assert body["display_name"] == "新人"

    emails = [a.email for a in s.list_accounts()]
    assert emails == ["newbie@company.com"]


def test_api_post_account_defaults_display_name(tmp_data_dir):
    """Omitting display_name should fall back to the local-part of the email."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.post("/api/accounts", json={"email": "alice@company.com"})
    assert r.status_code == 201
    assert r.json()["display_name"] == "alice"


def test_api_post_account_duplicate_returns_409(tmp_data_dir):
    """Adding an email that's already in the accounts table must fail
    with 409 (and not silently update the row)."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "Original")
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.post(
            "/api/accounts",
            json={"email": "a@x.com", "display_name": "NewName"},
        )
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]
    # Original display_name untouched
    by_email = {a.email: a.display_name for a in s.list_accounts()}
    assert by_email["a@x.com"] == "Original"


def test_api_post_account_invalid_email_returns_422(tmp_data_dir):
    """Malformed email triggers Pydantic validation → 422."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.post("/api/accounts", json={"email": "not-an-email"})
    assert r.status_code == 422


def test_api_post_account_normalizes_email_case(tmp_data_dir):
    """Email is stored lowercased so subsequent lookups are case-insensitive."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.post("/api/accounts", json={"email": "Foo@Bar.COM"})
    assert r.status_code == 201
    assert r.json()["email"] == "foo@bar.com"
    assert s.list_accounts()[0].email == "foo@bar.com"


# ---------------------------------------------------------------------------
# DELETE /api/accounts/{email}
# ---------------------------------------------------------------------------


def test_api_delete_account_removes_row_and_cascades(tmp_data_dir):
    """DELETE removes the account and any model_usage rows, but leaves
    the snapshots table alone."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_account("b@x.com", "B")
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    s.upsert_model_usage(
        email="a@x.com", cycle_start=s_dt.date().isoformat(),
        cycle_end=e_dt.date().isoformat(),
        model_name="M", model_type="Chat", model_source="Trae",
        input_tokens=5, output_tokens=7,
    )
    snap_id = s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="t")
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.delete("/api/accounts/a%40x.com")  # %40 = '@'
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["deleted_rows"] >= 2  # account + model row

    assert {a.email for a in s.list_accounts()} == {"b@x.com"}
    # model_usage cascaded
    rows = s.conn.execute(
        "SELECT email FROM model_usage WHERE email = ?", ("a@x.com",)
    ).fetchall()
    assert rows == []
    # snapshot preserved
    assert s.conn.execute(
        "SELECT id FROM snapshots WHERE id = ?", (snap_id,)
    ).fetchone() is not None


def test_api_delete_account_idempotent(tmp_data_dir):
    """Deleting a non-existent email is a 200 (idempotent). The dashboard
    re-fetch on a flaky network should not show a misleading error."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.delete("/api/accounts/ghost%40x.com")
    assert r.status_code == 200
    assert r.json()["deleted_rows"] == 0


def test_api_delete_account_invalid_email_returns_422(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.delete("/api/accounts/not-an-email")
    assert r.status_code == 422


def test_api_delete_account_lowercases_path(tmp_data_dir):
    """The DELETE handler lowercases the path param so 'Foo@Bar.com'
    matches a row stored as 'foo@bar.com'."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.upsert_account("a@x.com", "A")
    cfg = _make_config()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.delete("/api/accounts/A%40X.COM")
    assert r.status_code == 200
    assert s.list_accounts() == []


# ---------------------------------------------------------------------------
# Email settings write-back endpoints (Task 5)
# ---------------------------------------------------------------------------


def test_api_report_config_includes_smtp_password_set(tmp_data_dir, monkeypatch):
    """GET /api/report/config returns smtp_password_set, never the password itself."""
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
        email=EmailConfig(
            enabled=True, smtp_host="smtp.x.com", smtp_port=465,
            smtp_user="u@x.com", from_addr="u@x.com",
            recipients=["r@x.com"], smtp_password_env="SMTP_PASSWORD_TEST_X",
        ),
    )
    monkeypatch.setenv("SMTP_PASSWORD_TEST_X", "secret-pw")
    app = create_app(cfg=cfg, storage=s, config_path=tmp_data_dir / "config.yaml")
    with TestClient(app) as client:
        r = client.get("/api/report/config")
        assert r.status_code == 200
        body = r.json()
        assert body["smtp_password_set"] is True
        assert "secret-pw" not in r.text


def test_api_put_recipients_round_trip(tmp_data_dir):
    """PUT /api/report/recipients persists + in-memory update, rejects invalid."""
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  recipients: [old@x.com]\n",
        encoding="utf-8",
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
    )
    cfg.email.enabled = False
    cfg.email.recipients = ["old@x.com"]
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.put(
            "/api/report/recipients",
            json={"recipients": ["NEW@X.com", "  b@x.com  ", "bad", "b@x.com"]},
        )
        assert r.status_code == 400  # "bad" fails validation
        r = client.put(
            "/api/report/recipients",
            json={"recipients": ["NEW@X.com", "  b@x.com  ", "b@x.com"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Lowercased, trimmed, deduped, order preserved
        assert body["recipients"] == ["new@x.com", "b@x.com"]
        # In-memory updated
        r2 = client.get("/api/report/config")
        assert r2.json()["recipients"] == ["new@x.com", "b@x.com"]
        # On disk
        disk = cfg_file.read_text(encoding="utf-8")
        assert "new@x.com" in disk
        assert "b@x.com" in disk
        assert "old@x.com" not in disk


def test_api_put_smtp_persists_and_preserves_recipients(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  recipients: [keep@x.com]\n  smtp_password_env: SMTP_PASSWORD\n",
        encoding="utf-8",
    )
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
    )
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.put("/api/report/smtp", json={
            "smtp_host": "smtp.new.com", "smtp_port": 587,
            "smtp_user": "u@new.com", "from_addr": "u@new.com",
            "send_time": "08:30",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["smtp_host"] == "smtp.new.com"
        # Recipients preserved
        assert body["recipients"] == ["keep@x.com"]
        # On disk
        disk = cfg_file.read_text(encoding="utf-8")
        assert "smtp_host: smtp.new.com" in disk
        assert "smtp_port: 587" in disk
        assert "keep@x.com" in disk
        assert "smtp_password_env: SMTP_PASSWORD" in disk


def test_api_put_smtp_rejects_invalid(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text("openapi_base: x\nauth_endpoint: y\n", encoding="utf-8")
    cfg = Config(openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[])
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.put("/api/report/smtp", json={
            "smtp_host": "", "smtp_port": 465, "smtp_user": "u@x.com",
            "from_addr": "u@x.com", "send_time": "09:00",
        })
        assert r.status_code == 400


def test_api_post_smtp_password_writes_env(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg_file = tmp_data_dir / "config.yaml"
    env_file = tmp_data_dir / ".env"
    env_file.write_text("TRAE_APP_ID=abc\n", encoding="utf-8")
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  smtp_password_env: SMTP_PASSWORD\n",
        encoding="utf-8",
    )
    cfg = Config(openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[])
    cfg.email.smtp_password_env = "SMTP_PASSWORD"
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file, env_path=env_file)
    with TestClient(app) as client:
        r = client.post("/api/report/smtp/password", json={"password": "new-pw"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"smtp_password_set": True}
        assert "new-pw" not in r.text
        # .env has the value
        text = env_file.read_text(encoding="utf-8")
        assert "SMTP_PASSWORD=new-pw" in text
        # TRAE_APP_ID preserved
        assert "TRAE_APP_ID=abc" in text


def test_api_post_eml_returns_eml_bytes(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    s.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
        email=EmailConfig(
            enabled=False,  # .eml export must NOT require enabled
            smtp_host="smtp.x.com", smtp_port=465,
            smtp_user="u@x.com", from_addr="u@x.com",
            recipients=[],
        ),
        included_model_names={"GLM-5.1"},
    )
    s.upsert_model_usage(
        email="a@x.com", cycle_start="2026-06-10", cycle_end="2026-07-06",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=100, output_tokens=50,
    )
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text("openapi_base: x\nauth_endpoint: y\n", encoding="utf-8")
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.post(
            "/api/report/eml",
            json={"recipients": ["dest@x.com"]},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("message/rfc822")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert ".eml" in cd
        # Parseable
        import email, email.policy
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(r.content)
        assert msg["From"] == "u@x.com"
        assert msg["To"] == "dest@x.com"
