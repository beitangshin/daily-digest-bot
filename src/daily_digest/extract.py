"""Full-text + metadata extraction for a single article URL, via trafilatura.

This turns a bare (title, url) pair into clean article text suitable for
summarization, and -- for HTML-fallback candidates that had no date from a
feed -- recovers a publish date from the article page's own metadata.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import trafilatura
from dateutil import parser as dateutil_parser

from .config import Settings
from .models import Article

logger = logging.getLogger(__name__)

MAX_CHARS_PER_ARTICLE = 6000  # keeps each article within a safe LLM token budget


def enrich_article(article: Article, tz: ZoneInfo) -> Article:
    downloaded = trafilatura.fetch_url(article.url)
    if not downloaded:
        logger.warning("could not download %s; falling back to headline only", article.url)
        return article

    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False) or ""
    article.text = text[:MAX_CHARS_PER_ARTICLE]

    if article.published_at is None:
        try:
            metadata = trafilatura.extract_metadata(downloaded)
        except Exception:
            metadata = None
        date_str = getattr(metadata, "date", None) if metadata else None
        if date_str:
            try:
                parsed = dateutil_parser.parse(date_str)
                article.published_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
            except (ValueError, OverflowError):
                pass
    return article


def enrich_all(articles: list[Article], settings: Settings, tz: ZoneInfo) -> list[Article]:
    enriched: list[Article] = []
    with ThreadPoolExecutor(max_workers=settings.max_concurrent_fetches) as pool:
        futures = {pool.submit(enrich_article, a, tz): a for a in articles}
        for future in as_completed(futures):
            original = futures[future]
            try:
                enriched.append(future.result())
            except Exception:
                logger.exception("failed to enrich article %s", original.url)
                enriched.append(original)
    return enriched
