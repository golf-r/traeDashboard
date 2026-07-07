"""Scheduler: wraps Collector in an APScheduler BackgroundScheduler."""
from __future__ import annotations
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config
from .storage import Storage
from .client import TraeClient
from .collector import Collector


log = logging.getLogger(__name__)


def make_collector(cfg: Config, storage: Storage) -> Collector:
    """Build a Collector wired to a TraeClient based on config."""
    client = TraeClient(
        base_url=cfg.openapi_base,
        auth_url=cfg.auth_endpoint,
        app_id=cfg.app_id,
        app_secret=cfg.app_secret,
    )
    return Collector(
        client=client,
        storage=storage,
        config=cfg,
    )


def start_scheduler(cfg: Config, storage: Storage) -> BackgroundScheduler:
    """Start a background scheduler that runs the collector on an interval."""
    collector = make_collector(cfg, storage)
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(
        _safe_run,
        "interval",
        minutes=cfg.fetch_interval_minutes,
        args=[collector],
        next_run_time=None,
        id="trae_collector",
    )
    sched.start()
    return sched


def _safe_run(collector: Collector) -> None:
    """Run a collector cycle, logging (not raising) on failure."""
    try:
        result = collector.run_once()
        log.info("collector run: %s", result)
    except Exception:
        log.exception("collector run failed")
