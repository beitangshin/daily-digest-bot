"""Shared timezone/date helpers used by both fetching and extraction."""
from __future__ import annotations

from datetime import datetime


def is_since(dt: datetime | None, cutoff: datetime, include_undated: bool) -> bool:
    """Whether `dt` falls on or after `cutoff` (a rolling "since last run"
    boundary, not a calendar-day one -- see state.py / MAINTENANCE.md for why
    calendar-day filtering has a gap that this closes).

    An article with no known publish date (`dt is None`) is only kept when
    `include_undated` is set -- otherwise we'd risk padding the digest with
    stale evergreen content that a listing page happened to link to.
    """
    if dt is None:
        return include_undated
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cutoff.tzinfo)
    return dt >= cutoff
