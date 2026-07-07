"""Tests for trae_dashboard.scheduler."""
from __future__ import annotations

from trae_dashboard.scheduler import make_collector, _safe_run
from trae_dashboard.storage import Storage
from trae_dashboard.config import Config, Account
from trae_dashboard.collector import Collector


def test_make_collector_returns_collector(tmp_data_dir):
    cfg = Config(
        openapi_base="https://x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
    )
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()
    collector = make_collector(cfg, storage)
    assert isinstance(collector, Collector)


def test_safe_run_swallows_exceptions(tmp_data_dir):
    cfg = Config(
        openapi_base="https://x", auth_endpoint="/auth",
        app_id="i", app_secret="s", accounts=[],
    )
    storage = Storage(tmp_data_dir / "test.db")
    storage.init()

    class FakeCollector:
        def run_once(self):
            raise RuntimeError("boom")

    # Should not raise
    _safe_run(FakeCollector())
