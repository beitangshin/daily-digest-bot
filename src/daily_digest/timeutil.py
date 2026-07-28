"""Shared timezone/date helpers used by both fetching and extraction."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def is_today(dt: datetime | None, tz: ZoneInfo, include_undated: bool) -> bool:
    """Whether `dt` falls on today's calendar date in timezone `tz`.

    An article with no known publish date (`dt is None`) is only kept when
    `include_undated` is set -- otherwise we'd risk padding the digest with
    stale evergreen content that a listing page happened to link to.
    """
    if dt is None:
        return include_undated
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).date() == datetime.now(tz).date()
