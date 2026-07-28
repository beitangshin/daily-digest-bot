from datetime import datetime
from zoneinfo import ZoneInfo

from daily_digest.state import load_last_run_started_at, save_last_run_started_at


def test_load_missing_state_returns_none(tmp_path):
    assert load_last_run_started_at("ai", tmp_path) is None


def test_save_then_load_roundtrip(tmp_path):
    when = datetime(2026, 7, 28, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    save_last_run_started_at("ai", tmp_path, when)
    loaded = load_last_run_started_at("ai", tmp_path)
    assert loaded == when


def test_channels_have_independent_state(tmp_path):
    ai_when = datetime(2026, 7, 28, 8, 0, tzinfo=ZoneInfo("UTC"))
    auto_when = datetime(2026, 7, 28, 9, 0, tzinfo=ZoneInfo("UTC"))
    save_last_run_started_at("ai", tmp_path, ai_when)
    save_last_run_started_at("autonomous_driving", tmp_path, auto_when)
    assert load_last_run_started_at("ai", tmp_path) == ai_when
    assert load_last_run_started_at("autonomous_driving", tmp_path) == auto_when


def test_corrupt_state_file_treated_as_missing(tmp_path):
    channel_dir = tmp_path / "ai"
    channel_dir.mkdir(parents=True)
    (channel_dir / ".state.json").write_text("not json", encoding="utf-8")
    assert load_last_run_started_at("ai", tmp_path) is None
