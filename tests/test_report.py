"""Tests for the daily email report renderer + SMTP config parser."""
from __future__ import annotations

import email
import email.parser
import email.policy
from pathlib import Path

import pytest

from trae_dashboard.config import Config, _load_email_config
from trae_dashboard.report import (
    ReportRow,
    _esc,
    _fmt_cny,
    _quota_color,
    collect_report_rows,
    render_html,
)
from trae_dashboard.storage import Storage


def _seed_storage(storage: Storage) -> None:
    from trae_dashboard.cycle import current_cycle_window
    s_dt, e_dt = current_cycle_window()
    cycle_start = s_dt.date().isoformat()
    cycle_end = e_dt.date().isoformat()
    storage.upsert_account("a@x.com", "Alice")
    storage.upsert_account("b@x.com", "Bob")
    storage.upsert_model_usage(
        email="a@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="GLM-5.1", model_type="Chat", model_source="Trae",
        input_tokens=32_000_000, output_tokens=180_000,
        amount_total=80.0, amount_basic=60.0, amount_pay_go=20.0,
    )
    storage.upsert_model_usage(
        email="a@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="Doubao-Seed-Code", model_type="Chat", model_source="Trae",
        input_tokens=8_000_000, output_tokens=100_000,
        amount_total=20.0,
    )
    storage.upsert_model_usage(
        email="b@x.com", cycle_start=cycle_start, cycle_end=cycle_end,
        model_name="GLM-5-Turbo", model_type="Chat", model_source="Trae",
        input_tokens=8_000_000, output_tokens=80_000,
        amount_total=30.0,
    )


def _make_config(per_account_quota: float = 120.0) -> Config:
    return Config(
        openapi_base="https://example", auth_endpoint="/token",
        app_id="x", app_secret="x", accounts=[],
        per_account_quota=per_account_quota,
    )


# ---------- config parsing (unchanged) ----------

class TestEmailConfigParsing:
    def test_missing_section_returns_disabled(self):
        cfg = _load_email_config({})
        assert cfg.enabled is False
        assert cfg.smtp_host == ""

    def test_disabled_section_is_ok(self):
        cfg = _load_email_config({"email": {"enabled": False}})
        assert cfg.enabled is False

    def test_enabled_requires_required_fields(self):
        with pytest.raises(RuntimeError, match="missing required field"):
            _load_email_config({"email": {"enabled": True, "smtp_user": "u@x.com",
                "from_addr": "u@x.com", "recipients": ["r@x.com"]}})

    def test_enabled_requires_recipients(self):
        with pytest.raises(RuntimeError, match="recipients.*empty"):
            _load_email_config({"email": {"enabled": True, "smtp_host": "smtp.x.com",
                "smtp_user": "u@x.com", "from_addr": "u@x.com", "recipients": []}})

    def test_full_config_parses(self):
        cfg = _load_email_config({"email": {"enabled": True, "smtp_host": "smtp.qq.com",
            "smtp_port": 465, "smtp_user": "me@qq.com", "smtp_password_env": "SMTP_PASSWORD",
            "from_addr": "me@qq.com", "recipients": ["a@x.com", "b@x.com"], "send_time": "09:00"}})
        assert cfg.enabled is True
        assert cfg.smtp_host == "smtp.qq.com"
        assert cfg.recipients == ["a@x.com", "b@x.com"]


# ---------- pure helpers ----------

class TestFmtCny:
    def test_basic(self):
        assert _fmt_cny(1234.5) == "¥ 1,234.50"

    def test_zero(self):
        assert _fmt_cny(0) == "¥ 0.00"

    def test_large(self):
        assert _fmt_cny(1234567.89) == "¥ 1,234,567.89"


class TestQuotaColor:
    def test_high(self):
        assert _quota_color(95.0) == "#dc2626"
    def test_mid(self):
        assert _quota_color(75.0) == "#d97706"
    def test_low(self):
        assert _quota_color(50.0) == "#16a34a"


class TestEsc:
    def test_html_chars(self):
        assert _esc('a<b>&c"d') == "a&lt;b&gt;&amp;c&quot;d"


# ---------- data collection ----------

class TestCollectReportRows:
    def test_collects_per_account_rows(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config()
        rows = collect_report_rows(s, cfg)
        assert len(rows) == 2
        # Sorted by amount_total desc — Alice (100) > Bob (30)
        assert rows[0].email == "a@x.com"
        assert rows[1].email == "b@x.com"

    def test_amount_total_aggregated(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config()
        rows = collect_report_rows(s, cfg)
        alice = next(r for r in rows if r.email == "a@x.com")
        # GLM 80 + Doubao 20 = 100
        assert alice.amount_total == pytest.approx(100.0)
        # Top model is GLM-5.1 (80 > 20)
        assert alice.top_model == "GLM-5.1"
        assert alice.top_model_amount == pytest.approx(80.0)

    def test_quota_pct(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config(per_account_quota=200.0)
        rows = collect_report_rows(s, cfg)
        alice = next(r for r in rows if r.email == "a@x.com")
        # 100 / 200 = 50.0%
        assert alice.quota_pct == pytest.approx(50.0)

    def test_empty_db_returns_empty(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        cfg = _make_config()
        rows = collect_report_rows(s, cfg)
        assert rows == []


# ---------- HTML rendering ----------

class TestRenderHtml:
    def _sample_rows(self) -> list[ReportRow]:
        return [
            ReportRow(display_name="Alice", email="a@x.com",
                amount_total=100.0, quota_pct=83.3,
                top_model="GLM-5.1", top_model_amount=80.0,
                input_tokens=32_000_000, output_tokens=180_000),
            ReportRow(display_name="Bob", email="b@x.com",
                amount_total=30.0, quota_pct=25.0,
                top_model="GLM-5-Turbo", top_model_amount=30.0,
                input_tokens=8_000_000, output_tokens=80_000),
        ]

    def test_contains_per_account_data(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html(self._sample_rows(), cfg, start, now)
        assert "Alice" in html
        assert "a@x.com" in html
        assert "GLM-5.1" in html

    def test_total_consumed_in_kpi_uses_cny(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html(self._sample_rows(), cfg, start, now)
        # Total = 100 + 30 = 130 -> "¥ 130.00"
        assert "¥ 130.00" in html

    def test_total_quota_uses_row_count(self):
        from datetime import datetime, timezone
        cfg = _make_config(per_account_quota=120.0)
        rows = [ReportRow(f"u{i}@x.com", f"u{i}@x.com", 0.0, 0.0, "—", 0.0, 0, 0)
                for i in range(13)]
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html(rows, cfg, start, now)
        # 13 × 120 = 1560 -> "¥ 1,560.00"
        assert "¥ 1,560.00" in html

    def test_empty_rows_renders_placeholder(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        html = render_html([], cfg, start, now)
        assert "本周期暂无用量数据" in html

    def test_html_escapes_user_content(self):
        from datetime import datetime, timezone
        cfg = _make_config()
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        rows = [ReportRow("<script>alert(1)</script>", "x@x.com",
            1.0, 0.0, "M", 1.0, 0, 0)]
        html = render_html(rows, cfg, start, now)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


def test_build_eml_has_headers_and_html():
    from datetime import datetime, timezone
    from trae_dashboard.config import EmailConfig
    from trae_dashboard.report import build_eml
    cfg = EmailConfig(enabled=True, smtp_host="smtp.x.com", smtp_port=465,
        smtp_user="me@x.com", from_addr="me@x.com", recipients=["a@x.com", "b@x.com"])
    raw = build_eml(cfg, subject="[Trae Dashboard] 测试", html_body="<p>hello</p>")
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
    assert msg["From"] == "me@x.com"
    assert msg["To"] == "a@x.com, b@x.com"
    assert msg["Subject"] == "[Trae Dashboard] 测试"
