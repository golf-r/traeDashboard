"""Tests for the daily email report renderer + SMTP config parser.

SMTP sending itself is not unit-tested (would need a real server); we
test the data → HTML pipeline and the config validation. The render
output is asserted structurally (contains expected sections / values),
not as a full HTML snapshot — the layout will evolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trae_dashboard.config import Config, _load_email_config
from trae_dashboard.report import (
    ReportRow,
    _esc,
    _fmt_tokens,
    _quota_color,
    collect_report_rows,
    render_html,
)
from trae_dashboard.storage import Storage

# ---------- helpers ----------


def _seed_storage(storage: Storage) -> None:
    """Insert a couple of accounts with model usage for the current cycle."""
    storage.upsert_account("a@x.com", "Alice")
    storage.upsert_account("b@x.com", "Bob")
    storage.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="GLM-5.1",
        model_type="Chat",
        model_source="Trae",
        input_tokens=32_000_000,
        output_tokens=180_000,
    )
    storage.upsert_model_usage(
        email="a@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="Doubao-Seed-Code",
        model_type="Chat",
        model_source="Trae",
        input_tokens=8_000_000,
        output_tokens=100_000,
    )
    storage.upsert_model_usage(
        email="b@x.com",
        cycle_start="2026-06-10",
        cycle_end="2026-06-30",
        model_name="GLM-5-Turbo",
        model_type="Chat",
        model_source="Trae",
        input_tokens=8_000_000,
        output_tokens=80_000,
    )


def _make_config(per_account_quota: int = 50_000_000) -> Config:
    return Config(
        openapi_base="https://example",
        auth_endpoint="/token",
        app_id="x",
        app_secret="x",
        accounts=[],
        per_account_quota=per_account_quota,
        included_model_names={"GLM-5.1", "Doubao-Seed-Code", "GLM-5-Turbo"},
    )


# ---------- config parsing ----------


class TestEmailConfigParsing:
    def test_missing_section_returns_disabled(self):
        cfg = _load_email_config({})
        assert cfg.enabled is False
        assert cfg.smtp_host == ""
        assert cfg.recipients == []

    def test_disabled_section_is_ok(self):
        cfg = _load_email_config({"email": {"enabled": False}})
        assert cfg.enabled is False

    def test_enabled_requires_required_fields(self):
        # Missing smtp_host
        with pytest.raises(RuntimeError, match="missing required field"):
            _load_email_config(
                {
                    "email": {
                        "enabled": True,
                        "smtp_user": "u@x.com",
                        "from_addr": "u@x.com",
                        "recipients": ["r@x.com"],
                    }
                }
            )

    def test_enabled_requires_recipients(self):
        with pytest.raises(RuntimeError, match="recipients.*empty"):
            _load_email_config(
                {
                    "email": {
                        "enabled": True,
                        "smtp_host": "smtp.x.com",
                        "smtp_user": "u@x.com",
                        "from_addr": "u@x.com",
                        "recipients": [],
                    }
                }
            )

    def test_full_config_parses(self):
        cfg = _load_email_config(
            {
                "email": {
                    "enabled": True,
                    "smtp_host": "smtp.qq.com",
                    "smtp_port": 465,
                    "smtp_user": "me@qq.com",
                    "smtp_password_env": "SMTP_PASSWORD",
                    "from_addr": "me@qq.com",
                    "recipients": ["a@x.com", "b@x.com"],
                    "send_time": "09:00",
                }
            }
        )
        assert cfg.enabled is True
        assert cfg.smtp_host == "smtp.qq.com"
        assert cfg.smtp_port == 465
        assert cfg.smtp_password_env == "SMTP_PASSWORD"
        assert cfg.recipients == ["a@x.com", "b@x.com"]


# ---------- pure helpers ----------


class TestFmtTokens:
    def test_millions(self):
        assert _fmt_tokens(32_305_611) == "32.3M"

    def test_thousands(self):
        assert _fmt_tokens(4_497) == "4.5K"

    def test_small(self):
        assert _fmt_tokens(88) == "88"

    def test_zero(self):
        assert _fmt_tokens(0) == "0"


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
        # Two accounts seeded.
        assert len(rows) == 2
        # Sorted by consumed desc — Alice (GLM + Doubao) > Bob (GLM only).
        assert rows[0].email == "a@x.com"
        assert rows[1].email == "b@x.com"

    def test_doubao_weight_applied(self, tmp_data_dir: Path):
        """Report numbers must match dashboard (Doubao 0.5 display weight)."""
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config()
        rows = collect_report_rows(s, cfg)
        alice = next(r for r in rows if r.email == "a@x.com")
        # GLM-5.1: 32,000,000 + 180,000 = 32,180,000 (weight 1.0)
        # Doubao:  (8,000,000 + 100,000) * 0.5 = 4,050,000 (weight 0.5)
        # Total:   36,230,000
        assert alice.consumed == 36_230_000
        # Top model is GLM-5.1 (32.18M > 4.05M)
        assert alice.top_model == "GLM-5.1"
        assert alice.top_model_consumed == 32_180_000

    def test_quota_pct(self, tmp_data_dir: Path):
        s = Storage(tmp_data_dir / "test.db")
        s.init()
        _seed_storage(s)
        cfg = _make_config(per_account_quota=100_000_000)
        rows = collect_report_rows(s, cfg)
        alice = next(r for r in rows if r.email == "a@x.com")
        # 36,230,000 / 100,000,000 = 36.23%
        assert alice.quota_pct == pytest.approx(36.2, abs=0.1)

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
            ReportRow(
                display_name="Alice",
                email="a@x.com",
                input_tokens=32_000_000,
                output_tokens=180_000,
                consumed=32_180_000,
                quota_pct=64.4,
                top_model="GLM-5.1",
                top_model_consumed=32_180_000,
            ),
            ReportRow(
                display_name="Bob",
                email="b@x.com",
                input_tokens=8_000_000,
                output_tokens=80_000,
                consumed=8_080_000,
                quota_pct=16.2,
                top_model="GLM-5-Turbo",
                top_model_consumed=8_080_000,
            ),
        ]

    def test_contains_subject_and_cycle(self):
        from datetime import datetime, timezone

        cfg = _make_config()
        start = datetime(2026, 6, 10, tzinfo=timezone.utc)
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        html = render_html(self._sample_rows(), cfg, start, now)
        # Header shows cycle range
        assert "2026-06-10" in html
        assert "2026-07-06" in html
        # Title
        assert "Trae Dashboard" in html

    def test_contains_per_account_data(self):
        from datetime import datetime, timezone

        cfg = _make_config()
        start = datetime(2026, 6, 10, tzinfo=timezone.utc)
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        html = render_html(self._sample_rows(), cfg, start, now)
        assert "Alice" in html
        assert "a@x.com" in html
        assert "Bob" in html
        assert "GLM-5.1" in html

    def test_total_consumed_in_kpi_strip(self):
        from datetime import datetime, timezone

        cfg = _make_config()
        start = datetime(2026, 6, 10, tzinfo=timezone.utc)
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        html = render_html(self._sample_rows(), cfg, start, now)
        # Total = 32.18M + 8.08M = 40.26M -> formatted "40.3M"
        assert "40.3M" in html

    def test_empty_rows_renders_placeholder(self):
        from datetime import datetime, timezone

        cfg = _make_config()
        start = datetime(2026, 6, 10, tzinfo=timezone.utc)
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        html = render_html([], cfg, start, now)
        assert "本周期暂无用量数据" in html

    def test_html_escapes_user_content(self):
        """display_name with HTML must be escaped to prevent injection."""
        from datetime import datetime, timezone

        cfg = _make_config()
        start = datetime(2026, 6, 10, tzinfo=timezone.utc)
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        rows = [
            ReportRow(
                display_name="<script>alert(1)</script>",
                email="x@x.com",
                input_tokens=1,
                output_tokens=1,
                consumed=2,
                quota_pct=0.0,
                top_model="M",
                top_model_consumed=2,
            )
        ]
        html = render_html(rows, cfg, start, now)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
