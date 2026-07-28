from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from daily_digest.timeutil import is_since


def test_none_date_respects_include_undated_flag():
    tz = ZoneInfo("UTC")
    cutoff = datetime.now(tz) - timedelta(hours=24)
    assert is_since(None, cutoff, include_undated=True) is True
    assert is_since(None, cutoff, include_undated=False) is False


def test_after_cutoff_matches():
    tz = ZoneInfo("UTC")
    cutoff = datetime.now(tz) - timedelta(hours=24)
    after = cutoff + timedelta(hours=1)
    assert is_since(after, cutoff, include_undated=False) is True


def test_before_cutoff_does_not_match():
    tz = ZoneInfo("UTC")
    cutoff = datetime.now(tz) - timedelta(hours=24)
    before = cutoff - timedelta(minutes=1)
    assert is_since(before, cutoff, include_undated=False) is False


def test_exactly_at_cutoff_matches():
    tz = ZoneInfo("UTC")
    cutoff = datetime.now(tz) - timedelta(hours=24)
    assert is_since(cutoff, cutoff, include_undated=False) is True


def test_naive_datetime_is_assumed_to_be_in_cutoffs_timezone():
    tz = ZoneInfo("UTC")
    cutoff = datetime.now(tz) - timedelta(hours=24)
    naive_after = (cutoff + timedelta(hours=1)).replace(tzinfo=None)
    assert is_since(naive_after, cutoff, include_undated=False) is True
