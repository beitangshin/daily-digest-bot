from datetime import datetime, timedelta
from email.utils import format_datetime
from zoneinfo import ZoneInfo

from daily_digest.config import Settings
from daily_digest.fetch import fetch_rss_articles
from daily_digest.models import Source


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


def _rss_feed(recent_pubdate: str, old_pubdate: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Fake Feed</title>
  <item>
    <title>Recent article</title>
    <link>https://example.com/recent</link>
    <pubDate>{recent_pubdate}</pubDate>
  </item>
  <item>
    <title>Old article</title>
    <link>https://example.com/old</link>
    <pubDate>{old_pubdate}</pubDate>
  </item>
</channel>
</rss>
"""


def test_fetch_rss_articles_filters_by_cutoff():
    tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    cutoff = now - timedelta(hours=24)
    recent = cutoff + timedelta(hours=1)
    old = cutoff - timedelta(hours=1)
    feed_xml = _rss_feed(format_datetime(recent), format_datetime(old))

    source = Source(name="Fake Source", url="unused", type="rss", category="测试")
    articles = fetch_rss_articles(source, feed_xml, tz, cutoff, _settings())

    assert len(articles) == 1
    assert articles[0].title == "Recent article"
    assert articles[0].url == "https://example.com/recent"


def test_fetch_rss_articles_returns_empty_for_unparseable_feed():
    tz = ZoneInfo("UTC")
    cutoff = datetime.now(tz) - timedelta(hours=24)
    source = Source(name="Fake Source", url="unused", type="rss", category="测试")
    articles = fetch_rss_articles(source, "not xml at all", tz, cutoff, _settings())
    assert articles == []
