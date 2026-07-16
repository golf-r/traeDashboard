"""End-to-end: UI-driven config write + .eml export + SMTP send paths."""
from __future__ import annotations

import email
import email.policy
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    from starlette.testclient import TestClient  # type: ignore

from trae_dashboard.api import create_app
from trae_dashboard.config import Config, EmailConfig
from trae_dashboard.storage import Storage


def test_email_enhancement_full_flow(tmp_data_dir: Path):
    db = tmp_data_dir / "test.db"
    s = Storage(db); s.init()
    s.upsert_account("a@x.com", "A")
    s.upsert_model_usage(
        email="a@x.com", cycle_start="2026-06-10", cycle_end="2026-07-06",
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=1000, output_tokens=500,
    )

    cfg_file = tmp_data_dir / "config.yaml"
    cfg_file.write_text(
        "openapi_base: x\nauth_endpoint: y\nemail:\n  enabled: false\n  recipients: []\n",
        encoding="utf-8",
    )
    env_file = tmp_data_dir / ".env"
    env_file.write_text("", encoding="utf-8")

    cfg = Config(
        openapi_base="x", auth_endpoint="/auth", app_id="i", app_secret="s", accounts=[],
        email=EmailConfig(enabled=False, smtp_password_env="SMTP_PASSWORD_E2E"),
        included_model_names={"GLM-5.1"},
    )
    app = create_app(cfg=cfg, storage=s, config_path=cfg_file, env_path=env_file)
    with TestClient(app) as c:
        # 1. Configure recipients
        r = c.put("/api/report/recipients", json={"recipients": ["r1@x.com", "r2@x.com"]})
        assert r.status_code == 200
        assert r.json()["recipients"] == ["r1@x.com", "r2@x.com"]
        # 2. Configure SMTP
        r = c.put("/api/report/smtp", json={
            "smtp_host": "smtp.x.com", "smtp_port": 465,
            "smtp_user": "u@x.com", "from_addr": "u@x.com", "send_time": "08:00",
        })
        assert r.status_code == 200
        # 3. Set password
        r = c.post("/api/report/smtp/password", json={"password": "supersecret"})
        assert r.status_code == 200
        assert r.json() == {"smtp_password_set": True}
        assert "supersecret" not in r.text
        # 4. Config now reports everything
        r = c.get("/api/report/config")
        assert r.status_code == 200
        body = r.json()
        assert body["smtp_password_set"] is True
        assert body["recipients"] == ["r1@x.com", "r2@x.com"]
        # 5. Download .eml (works even with email.enabled=false)
        r = c.post("/api/report/eml", json={"recipients": ["r1@x.com"]})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("message/rfc822")
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(r.content)
        assert msg["From"] == "u@x.com"
        assert msg["To"] == "r1@x.com"
        # 6. .env still contains the password
        env_text = env_file.read_text(encoding="utf-8")
        assert "SMTP_PASSWORD_E2E=supersecret" in env_text