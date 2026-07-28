from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from daily_digest.timeutil import is_today


def test_none_date_respects_include_undated_flag():
    tz = ZoneInfo("UTC")
    assert is_today(None, tz, include_undated=True) is True
    assert is_today(None, tz, include_undated=False) is False


def test_today_matches():
    tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    assert is_today(now, tz, include_undated=False) is True


def test_yesterday_does_not_match():
    tz = ZoneInfo("UTC")
    yesterday = datetime.now(tz) - timedelta(days=1)
    assert is_today(yesterday, tz, include_undated=False) is False


def test_naive_datetime_is_assumed_to_be_in_target_timezone():
    tz = ZoneInfo("UTC")
    naive_now = datetime.now(tz).replace(tzinfo=None)
    assert is_today(naive_now, tz, include_undated=False) is True
