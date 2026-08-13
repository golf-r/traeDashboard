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


def _cfg(**kw) -> Config:
    defaults = dict(openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[])
    defaults.update(kw)
    return Config(**defaults)


def _seed_cycle(storage: Storage, email: str, *, amount_total=0.0, amount_basic=0.0, amount_pay_go=0.0, currency="CNY", input_tokens=0, output_tokens=0, model_name="M"):
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    storage.upsert_account(email, email.split("@")[0])
    storage.upsert_model_usage(
        email=email, cycle_start=s_dt.date().isoformat(), cycle_end=e_dt.date().isoformat(),
        model_name=model_name, model_type="Chat", model_source="Trae",
        input_tokens=input_tokens, output_tokens=output_tokens,
        amount_total=amount_total, amount_basic=amount_basic, amount_pay_go=amount_pay_go,
        currency=currency,
    )


def test_api_accounts_returns_amount(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    _seed_cycle(s, "a@x.com", amount_total=50.0, amount_basic=4.0, amount_pay_go=1.0, input_tokens=10, output_tokens=20)
    cfg = _cfg(accounts=[Account("a@x.com", "A")], per_account_quota=120.0)
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts")
        assert r.status_code == 200
        data = r.json()
    assert len(data) == 1
    assert data[0]["email"] == "a@x.com"
    assert data[0]["amount_total"] == pytest.approx(50.0)
    assert data[0]["per_account_quota"] == 120.0
    assert data[0]["quota_used_pct"] == pytest.approx(41.67, abs=0.01)
    # per-model breakdown includes amount_total
    assert data[0]["models"][0]["amount_total"] == pytest.approx(50.0)
    assert data[0]["models"][0]["amount_basic"] == pytest.approx(4.0)
    assert data[0]["models"][0]["amount_pay_go"] == pytest.approx(1.0)


def test_api_account_history_returns_amount(tmp_data_dir):
    db = tmp_data_dir / "test.db"
    s = Storage(db)
    s.init()
    _seed_cycle(s, "a@x.com", amount_total=30.0, amount_basic=20.0, amount_pay_go=10.0, currency="CNY")
    cfg = _cfg(accounts=[Account("a@x.com")])
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/accounts/a@x.com/history")
        assert r.status_code == 200
        items = r.json()
    assert len(items) == 1
    assert items[0]["amount_total"] == pytest.approx(30.0)
    assert items[0]["amount_basic"] == pytest.approx(20.0)
    assert items[0]["amount_pay_go"] == pytest.approx(10.0)
    assert items[0]["currency"] == "CNY"


def test_api_health(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _cfg()
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_api_status_returns_amount_fields(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    _seed_cycle(s, "a@x.com", amount_total=50.0)
    cfg = _cfg(accounts=[Account("a@x.com")], per_account_quota=120.0)
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200, r.text
        body = r.json()
    for key in ("ok", "last_fetched_at", "seconds_since_fetch",
                "total_accounts", "accounts_with_data",
                "total_quota", "total_consumed", "total_remaining", "utilization_pct"):
        assert key in body
    for key in ("cycle_start", "cycle_end", "nextResetAt", "per_account_quota"):
        assert key in body, f"missing key {key}"
    assert "db_path" not in body
    assert body["total_accounts"] == 1
    assert body["accounts_with_data"] == 1
    assert body["total_consumed"] == pytest.approx(50.0)
    assert body["total_quota"] == pytest.approx(120.0)
    assert body["total_remaining"] == pytest.approx(70.0)
    assert body["utilization_pct"] == pytest.approx(41.67, abs=0.01)


def test_api_status_excludes_zero_amount_accounts(tmp_data_dir):
    """accounts_with_data excludes zero-amount accounts."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    s.save_snapshot(start_time=1, end_time=2, payload_json="{}", request_meta="x")
    _seed_cycle(s, "real@x.com", amount_total=50.0)
    _seed_cycle(s, "zero@x.com", amount_total=0.0, input_tokens=10, output_tokens=20)
    cfg = _cfg(accounts=[Account("real@x.com"), Account("zero@x.com")], per_account_quota=120.0)
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/status")
        body = r.json()
    assert body["total_accounts"] == 2
    assert body["accounts_with_data"] == 1
    assert body["total_consumed"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# POST /api/accounts — add managed account
# ---------------------------------------------------------------------------


def test_api_post_account_creates_row(tmp_data_dir):
    """POST /api/accounts persists the new account and the next list
    read should see it. Display name defaults to the email local-part."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _cfg()
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
    cfg = _cfg()
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
    cfg = _cfg()
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
    cfg = _cfg()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.post("/api/accounts", json={"email": "not-an-email"})
    assert r.status_code == 422


def test_api_post_account_normalizes_email_case(tmp_data_dir):
    """Email is stored lowercased so subsequent lookups are case-insensitive."""
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _cfg()
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
    cfg = _cfg()
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
    cfg = _cfg()
    app = create_app(cfg=cfg, storage=s)

    with TestClient(app) as client:
        r = client.delete("/api/accounts/ghost%40x.com")
    assert r.status_code == 200
    assert r.json()["deleted_rows"] == 0


def test_api_delete_account_invalid_email_returns_422(tmp_data_dir):
    s = Storage(tmp_data_dir / "test.db")
    s.init()
    cfg = _cfg()
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
    cfg = _cfg()
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


def test_api_get_eml_returns_eml_bytes(tmp_data_dir):
    """GET /api/report/eml is the user-recoverable fallback when the JS-driven
    POST is blocked (stale server, browser cache, proxy stripping POST, etc).
    The user can paste the URL into the address bar and the browser downloads
    the .eml directly — no JS, no fetch, no preflight required.
    """
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    s.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
        email=EmailConfig(
            enabled=False,
            smtp_host="smtp.x.com", smtp_port=465,
            smtp_user="u@x.com", from_addr="u@x.com",
            recipients=[],
        ),
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
        r = client.get("/api/report/eml?recipients=alice@x.com,bob@y.com")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("message/rfc822")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd and ".eml" in cd
        import email, email.policy
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(r.content)
        assert msg["From"] == "u@x.com"
        assert msg["To"] == "alice@x.com, bob@y.com"


def test_api_get_eml_with_no_recipients_uses_configured(tmp_data_dir):
    """GET /api/report/eml without a recipients query param falls back to the
    recipients configured in config.yaml (or empty if none configured)."""
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    s.upsert_account("a@x.com", "A")
    cfg = Config(
        openapi_base="x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
        email=EmailConfig(
            enabled=False, smtp_host="h", smtp_port=465,
            smtp_user="u@x.com", from_addr="u@x.com",
            recipients=["configured@x.com"],
        ),
    )
    s.upsert_model_usage(
        email="a@x.com", cycle_start="2026-06-10", cycle_end="2026-07-06",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=10, output_tokens=20,
    )
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text("openapi_base: x\nauth_endpoint: y\n", encoding="utf-8")
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.get("/api/report/eml")
        assert r.status_code == 200
        import email, email.policy
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(r.content)
        assert msg["To"] == "configured@x.com"


def test_api_options_eml_advertises_allowed_methods(tmp_data_dir):
    """CORS preflight must list GET + POST + OPTIONS so browsers / proxies
    don't return 405 on the OPTIONS pre-check."""
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg = Config(openapi_base="x", auth_endpoint="/auth",
                 app_id="i", app_secret="s", accounts=[])
    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text("openapi_base: x\nauth_endpoint: y\n", encoding="utf-8")
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file)
    with TestClient(app) as client:
        r = client.options("/api/report/eml")
        assert r.status_code == 204
        allow = r.headers.get("allow", "")
        assert "GET" in allow and "POST" in allow


def test_api_version_reports_commit(tmp_data_dir):
    """/api/version lets users confirm which commit they're hitting."""
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    cfg = Config(openapi_base="x", auth_endpoint="/auth",
                 app_id="i", app_secret="s", accounts=[])
    app = create_app(cfg=cfg, storage=s)
    with TestClient(app) as client:
        r = client.get("/api/version")
        assert r.status_code == 200
        body = r.json()
        assert body["has_eml_endpoint"] is True
        assert body["eml_endpoint_path"] == "/api/report/eml"
        assert "POST" in body["eml_endpoint_methods"]
        assert "GET" in body["eml_endpoint_methods"]
        # commit may be "unknown" if not in a git checkout — that's OK
        assert isinstance(body["commit"], str)
