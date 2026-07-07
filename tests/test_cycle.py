"""Tests for trae_dashboard.cycle: token cycle window calculation.

Cycle rule (user-confirmed):
- 每月 10 号 00:00:00 (UTC) 重置
- If today.day >= 10: start = current month-10 00:00
- Else: start = previous month-10 00:00
- End = now
- next_cycle_reset: returns the upcoming 10th 00:00 UTC
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest

from trae_dashboard.cycle import (
    current_cycle_window,
    next_cycle_reset,
    cycle_window_unix,
    CYCLE_RESET_DAY,
    CYCLE_RESET_HOUR,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_constants():
    assert CYCLE_RESET_DAY == 10
    assert CYCLE_RESET_HOUR == 0


# ---------------------------------------------------------------------------
# current_cycle_window()
# ---------------------------------------------------------------------------


def test_cycle_window_mid_month():
    """now = 2026-06-29 → start = 2026-06-10 00:00 UTC, end = now."""
    now = datetime(2026, 6, 29, 14, 54, 47, tzinfo=timezone.utc)
    start, end = current_cycle_window(now)
    assert start == datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    assert end == now


def test_cycle_window_before_reset():
    """now = 2026-07-01 → start = 2026-06-10 00:00 UTC (last month's 10th)."""
    now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    start, end = current_cycle_window(now)
    assert start == datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    assert end == now


def test_cycle_window_january_before_reset():
    """now = 2026-01-05 → start = 2025-12-10 00:00 UTC (crosses year)."""
    now = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    start, end = current_cycle_window(now)
    assert start == datetime(2025, 12, 10, 0, 0, 0, tzinfo=timezone.utc)
    assert end == now


def test_cycle_window_exactly_on_reset_day():
    """now = 2026-06-10 00:00:00 → start = 2026-06-10 00:00:00 (this very moment)."""
    now = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    start, end = current_cycle_window(now)
    assert start == datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    assert end == now


def test_cycle_window_uses_naive_as_utc():
    """If `now` is naive, treat it as UTC."""
    now_naive = datetime(2026, 6, 29, 14, 54, 47)
    start, end = current_cycle_window(now_naive)
    # When treated as UTC, start = 2026-06-10
    assert start == datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    # End should be tz-aware UTC equivalent
    assert end == now_naive.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# cycle_window_unix()
# ---------------------------------------------------------------------------


def test_cycle_window_unix_returns_ints():
    now = datetime(2026, 6, 29, 14, 54, 47, tzinfo=timezone.utc)
    s, e = cycle_window_unix(now)
    assert isinstance(s, int)
    assert isinstance(e, int)
    assert s < e


def test_cycle_window_unix_matches_datetime_window():
    now = datetime(2026, 6, 29, 14, 54, 47, tzinfo=timezone.utc)
    s_dt, e_dt = current_cycle_window(now)
    s_unix, e_unix = cycle_window_unix(now)
    assert s_unix == int(s_dt.timestamp())
    assert e_unix == int(e_dt.timestamp())


def test_cycle_window_end_equals_now():
    """The window's end must be the provided 'now' (or current time)."""
    now = datetime(2026, 6, 29, 14, 54, 47, tzinfo=timezone.utc)
    _, end = current_cycle_window(now)
    assert end == now


# ---------------------------------------------------------------------------
# next_cycle_reset()
# ---------------------------------------------------------------------------


def test_next_reset_before_10th():
    """2026-06-05 → next reset = 2026-06-10."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    reset = next_cycle_reset(now)
    assert reset == datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_next_reset_after_10th():
    """2026-06-29 → next reset = 2026-07-10."""
    now = datetime(2026, 6, 29, 14, 54, 47, tzinfo=timezone.utc)
    reset = next_cycle_reset(now)
    assert reset == datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_next_reset_on_10th():
    """2026-06-10 00:00 → next reset = 2026-07-10 (same instant qualifies as 'on or after')."""
    now = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    reset = next_cycle_reset(now)
    assert reset == datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_next_reset_on_10th_late():
    """2026-06-10 15:30 → next reset = 2026-07-10."""
    now = datetime(2026, 6, 10, 15, 30, 0, tzinfo=timezone.utc)
    reset = next_cycle_reset(now)
    assert reset == datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_next_reset_december():
    """2026-12-25 → next reset = 2027-01-10 (crosses year)."""
    now = datetime(2026, 12, 25, 8, 0, 0, tzinfo=timezone.utc)
    reset = next_cycle_reset(now)
    assert reset == datetime(2027, 1, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_next_reset_naive_as_utc():
    """Naive datetime → treated as UTC."""
    now_naive = datetime(2026, 6, 5, 12, 0, 0)
    reset = next_cycle_reset(now_naive)
    assert reset == datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
