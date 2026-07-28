"""Top-level pipeline: fetch -> extract -> filter to today -> summarize -> render -> write.

Runs one channel per call (see channels.py) -- callers (cli.py) loop over
channels when the user wants "all of them" run in one command.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .channels import load_channels, sources_file_for
from .config import DEFAULT_OUTPUT_DIR, Settings
from .extract import enrich_all
from .fetch import fetch_all
from .llm import ChatFn, build_digest, summarize_articles
from .models import Article, Channel, Digest, Source
from .render_html import render_combined_index, render_digest_html, write_day_meta
from .render_markdown import render_markdown
from .sourcesio import load_sources
from .timeutil import is_today

logger = logging.getLogger(__name__)


def run_daily(
    channel: Channel,
    settings: Settings,
    sources_file: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    dry_run: bool = False,
    chat_fn: ChatFn | None = None,
) -> Digest:
    """Run one full pass for a single channel: fetch today's articles from
    every enabled source in that channel's sources file, summarize them with
    DeepSeek, and write digest.md + digest.html under output/<channel.key>/.

    With `dry_run=True`, fetching/filtering still happens (so newly added
    sources can be sanity-checked) but no DeepSeek calls are made and nothing
    is written to disk.
    """
    sources_file = sources_file or sources_file_for(channel)
    tz = ZoneInfo(settings.timezone)
    date_str = datetime.now(tz).date().isoformat()

    sources = load_sources(sources_file)
    if not sources:
        raise SystemExit(f"频道 {channel.key!r} 没有配置任何信息源，请先编辑 {sources_file}")

    logger.info("[%s] fetching from %d source(s)...", channel.key, len(sources))
    candidates = fetch_all(sources, settings)
    logger.info("[%s] %d candidate article(s) fetched, extracting full text...", channel.key, len(candidates))

    enriched = enrich_all(candidates, settings, tz)
    articles = [
        a for a in enriched if is_today(a.published_at, tz, settings.include_undated_as_today)
    ]
    logger.info("[%s] %d article(s) confirmed as today's after extraction", channel.key, len(articles))

    if dry_run:
        _print_dry_run_report(channel, sources, articles)
        return Digest(
            date_str=date_str,
            executive_summary_zh=[],
            sections=[],
            source_count=len({a.source_name for a in articles}),
            article_count=len(articles),
            channel_name=channel.name,
        )

    if not articles:
        digest = Digest(
            date_str=date_str,
            executive_summary_zh=[],
            sections=[],
            source_count=0,
            article_count=0,
            channel_name=channel.name,
        )
        _write_outputs(digest, channel, output_dir)
        return digest

    summaries = summarize_articles(articles, channel, settings, chat_fn=chat_fn)
    on_topic = [s for s in summaries if s.topic_tag != "无关"]
    dropped = len(summaries) - len(on_topic)
    if dropped:
        logger.info("[%s] %d article(s) judged unrelated to this channel and excluded", channel.key, dropped)
    digest = build_digest(on_topic, channel, date_str, settings, chat_fn=chat_fn)

    _write_outputs(digest, channel, output_dir)
    return digest


def _print_dry_run_report(channel: Channel, sources: list[Source], articles: list[Article]) -> None:
    counts: dict[str, int] = {}
    for a in articles:
        counts[a.source_name] = counts.get(a.source_name, 0) + 1
    print(f"[dry-run:{channel.key}] 未调用 DeepSeek，仅展示抓取结果（共 {len(articles)} 篇今日文章）：")
    for s in sources:
        flag = "" if s.enabled else "（已禁用）"
        print(f"  - {s.name}{flag}: {counts.get(s.name, 0)} 篇")


def _write_outputs(digest: Digest, channel: Channel, output_dir: Path) -> None:
    channel_dir = output_dir / channel.key
    day_dir = channel_dir / digest.date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    (day_dir / "digest.md").write_text(render_markdown(digest), encoding="utf-8")
    (day_dir / "digest.html").write_text(render_digest_html(digest), encoding="utf-8")
    write_day_meta(day_dir, digest)

    (channel_dir / "latest.md").write_text(render_markdown(digest), encoding="utf-8")

    # The top-level index covers every configured channel (tabbed UI), not
    # just the one that just ran, so it always reflects the full picture.
    all_channels = load_channels()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_combined_index(output_dir, all_channels), encoding="utf-8")

    logger.info("[%s] wrote %s", channel.key, day_dir)
