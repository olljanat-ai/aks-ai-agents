"""Change windows and freezes of ``limits.yaml``."""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WINDOW_RE = re.compile(
    r"^(?P<d1>[A-Za-z]{3})(?:-(?P<d2>[A-Za-z]{3}))?\s+(?P<t1>\d{2}:\d{2})-(?P<t2>\d{2}:\d{2})(?:\s+(?P<tz>\S+))?$"
)


def in_window(window: str, now: dt.datetime) -> bool:
    """``"Mon-Thu 08:00-15:00 Europe/Helsinki"`` – days inclusive, end time exclusive."""
    match = WINDOW_RE.match(window.strip())
    if not match:
        raise ValueError(f"invalid change window {window!r}")
    local = now.astimezone(ZoneInfo(match.group("tz") or "UTC"))
    d1 = DAYS.index(match.group("d1").lower())
    d2 = DAYS.index((match.group("d2") or match.group("d1")).lower())
    days = list(range(d1, d2 + 1)) if d1 <= d2 else list(range(d1, 7)) + list(range(0, d2 + 1))
    t1 = dt.time.fromisoformat(match.group("t1"))
    t2 = dt.time.fromisoformat(match.group("t2"))
    return local.weekday() in days and t1 <= local.time() < t2


def in_any_window(windows: list[str], now: dt.datetime) -> bool:
    return not windows or any(in_window(w, now) for w in windows)


def window_timezone(windows: list[str]) -> ZoneInfo:
    for window in windows:
        match = WINDOW_RE.match(window.strip())
        if match and match.group("tz"):
            return ZoneInfo(match.group("tz"))
    return ZoneInfo("UTC")


def in_freeze(freezes: list[str], now: dt.datetime, tz: ZoneInfo) -> str | None:
    """The freeze (``"2026-12-18/2027-01-06"``, dates inclusive) ``now`` falls in, if any."""
    today = now.astimezone(tz).date()
    for freeze in freezes:
        start, _, end = freeze.partition("/")
        if dt.date.fromisoformat(start) <= today <= dt.date.fromisoformat(end or start):
            return freeze
    return None
