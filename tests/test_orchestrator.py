from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from daily_digest.config import Settings
from daily_digest.models import Channel
from daily_digest.orchestrator import _determine_cutoff
from daily_digest.state import save_last_run_started_at

_CHANNEL = Channel(key="ai", name="AI 行业日报", domain_desc="人工智能（AI）行业", topics=["其他", "无关"])


def _settings(**overrides) -> Settings:
    base = dict(
        deepseek_api_key=None,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        request_timeout=5.0,
        timezone="UTC",
        language="zh",
        max_concurrent_fetches=2,
        max_concurrent_llm_calls=2,
        articles_per_batch=6,
        include_undated_articles=False,
        max_html_fallback_links=10,
        first_run_lookback_hours=24.0,
        max_lookback_hours=72.0,
    )
    base.update(overrides)
    return Settings(**base)


def test_first_run_uses_lookback_window(tmp_path):
    now = datetime.now(ZoneInfo("UTC"))
    cutoff = _determine_cutoff(_CHANNEL, tmp_path, now, _settings(first_run_lookback_hours=24.0))
    assert cutoff == now - timedelta(hours=24)


def test_uses_last_run_when_within_max_lookback(tmp_path):
    now = datetime.now(ZoneInfo("UTC"))
    last_run = now - timedelta(hours=10)
    save_last_run_started_at(_CHANNEL.key, tmp_path, last_run)
    cutoff = _determine_cutoff(_CHANNEL, tmp_path, now, _settings(max_lookback_hours=72.0))
    assert cutoff == last_run


def test_caps_at_max_lookback_when_last_run_too_old(tmp_path):
    now = datetime.now(ZoneInfo("UTC"))
    last_run = now - timedelta(days=10)
    save_last_run_started_at(_CHANNEL.key, tmp_path, last_run)
    settings = _settings(max_lookback_hours=72.0)
    cutoff = _determine_cutoff(_CHANNEL, tmp_path, now, settings)
    assert cutoff == now - timedelta(hours=72)
