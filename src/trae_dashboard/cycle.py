"""Token cycle calculation: 每月 10 号 00:00:00 UTC 重置."""
from __future__ import annotations
from datetime import datetime, timezone

CYCLE_RESET_DAY = 10
CYCLE_RESET_HOUR = 0  # UTC


def current_cycle_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (start, end) datetime for current token cycle.

    If now.day >= 10: start = current month-10 00:00 UTC
    Else: start = last month-10 00:00 UTC
    End = now (or provided).

    Naive datetimes are treated as UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if now.day >= CYCLE_RESET_DAY:
        # Current month-10
        start = datetime(now.year, now.month, CYCLE_RESET_DAY, CYCLE_RESET_HOUR, 0, 0,
                         tzinfo=timezone.utc)
    else:
        # Previous month-10
        if now.month == 1:
            start = datetime(now.year - 1, 12, CYCLE_RESET_DAY, CYCLE_RESET_HOUR, 0, 0,
                             tzinfo=timezone.utc)
        else:
            start = datetime(now.year, now.month - 1, CYCLE_RESET_DAY, CYCLE_RESET_HOUR, 0, 0,
                             tzinfo=timezone.utc)
    return start, now


def next_cycle_reset(now: datetime | None = None) -> datetime:
    """Return the next billing cycle reset datetime (the 10th 00:00 UTC).

    If today is before the 10th, returns the 10th of the current month.
    If today is on or after the 10th, returns the 10th of next month.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if now.day < CYCLE_RESET_DAY:
        return datetime(now.year, now.month, CYCLE_RESET_DAY, CYCLE_RESET_HOUR, 0, 0,
                        tzinfo=timezone.utc)
    else:
        if now.month == 12:
            return datetime(now.year + 1, 1, CYCLE_RESET_DAY, CYCLE_RESET_HOUR, 0, 0,
                            tzinfo=timezone.utc)
        else:
            return datetime(now.year, now.month + 1, CYCLE_RESET_DAY, CYCLE_RESET_HOUR, 0, 0,
                            tzinfo=timezone.utc)


def cycle_window_unix(now: datetime | None = None) -> tuple[int, int]:
    """Return (start_unix, end_unix) ints."""
    s, e = current_cycle_window(now)
    return int(s.timestamp()), int(e.timestamp())
