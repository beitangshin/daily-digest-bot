"""Fetching layer: RSS/Atom feeds (preferred) with fallback to HTML scraping
and Playwright-based JavaScript-rendered page scraping.

Only articles published at/after `cutoff` (a rolling "since last run"
boundary -- see state.py, not a calendar-day one) survive this stage for RSS
sources, since feed entries carry a reliable timestamp. Plain HTML sources
and Playwright sources don't, so their candidate links are date-filtered
later, once `extract.py` has pulled each article's own page (see
`orchestrator.py`).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from .config import Settings
from .models import Article, Source
from typing import Any
from .timeutil import is_since

logger = logging.getLogger(__name__)

USER_AGENT = "daily-digest-bot/0.2 (personal daily AI-news digest; low-volume)"

_FEED_LINK_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml"}
_COMMON_FEED_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml"]


def _new_client(settings: Settings) -> httpx.Client:
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=settings.request_timeout,
        follow_redirects=True,
        limits=limits,
    )


def _entry_published_at(entry, tz: ZoneInfo) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(*value[:6], tzinfo=ZoneInfo("UTC")).astimezone(tz)
            except Exception:
                continue
    for key in ("published", "updated"):
        value = entry.get(key)
        if value:
            try:
                return dateutil_parser.parse(value)
            except Exception:
                continue
    return None


def discover_feed_url(homepage_url: str, client: httpx.Client) -> str | None:
    """Best-effort discovery of an RSS/Atom feed for a plain homepage URL."""
    try:
        resp = client.get(homepage_url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("discover_feed_url: failed to fetch %s (%s)", homepage_url, exc)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    for link in soup.find_all("link"):
        if link.get("type") in _FEED_LINK_TYPES and link.get("href"):
            return urljoin(homepage_url, link["href"])

    for path in _COMMON_FEED_PATHS:
        candidate = urljoin(homepage_url, path)
        try:
            probe = client.get(candidate)
            if probe.status_code == 200 and probe.content[:200].lstrip().startswith(
                (b"<?xml", b"<rss", b"<feed")
            ):
                return candidate
        except httpx.HTTPError:
            continue
    return None


def fetch_rss_articles(
    source: Source, feed_url: str, tz: ZoneInfo, cutoff: datetime, settings: Settings, client: httpx.Client
) -> list[Article]:
    """Fetch `feed_url` (with `client`'s timeout, so a stalled server can't
    hang the whole run) and parse it into candidate articles."""
    try:
        resp = client.get(feed_url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("feed %s (%s) failed to fetch: %s", source.name, feed_url, exc)
        return []

    return parse_feed_content(source, feed_url, resp.content, tz, cutoff, settings)


def parse_feed_content(
    source: Source, feed_url: str, content: bytes | str, tz: ZoneInfo, cutoff: datetime, settings: Settings
) -> list[Article]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        logger.warning(
            "feed %s (%s) failed to parse: %s",
            source.name,
            feed_url,
            parsed.get("bozo_exception"),
        )
        return []

    articles = []
    for entry in parsed.entries:
        published = _entry_published_at(entry, tz)
        if not is_since(published, cutoff, settings.include_undated_articles):
            continue
        link = entry.get("link")
        if not link:
            continue
        articles.append(
            Article(
                source_name=source.name,
                source_category=source.category,
                title=entry.get("title", "(无标题)"),
                url=link,
                published_at=published,
            )
        )
    return articles


def fetch_html_fallback_articles(
    source: Source, client: httpx.Client, settings: Settings
) -> list[Article]:
    """No RSS available: scrape candidate article links off the homepage.

    Dates aren't on listing pages, so every candidate is carried forward
    undated (and capped by `max_html_fallback_links`); the real date filter
    happens after `extract.py` fetches each article's own page.
    """
    try:
        resp = client.get(source.url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("html fallback: failed to fetch %s (%s)", source.url, exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    seen: set[str] = set()
    articles: list[Article] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(source.url, a["href"])
        text = a.get_text(strip=True)
        if not text or len(text) < 8:
            continue
        if href in seen or not href.startswith("http"):
            continue
        if href.rstrip("/") == source.url.rstrip("/"):
            continue
        seen.add(href)
        articles.append(
            Article(
                source_name=source.name,
                source_category=source.category,
                title=text,
                url=href,
                published_at=None,
            )
        )
        if len(articles) >= settings.max_html_fallback_links:
            break
    return articles


def fetch_source(source: Source, settings: Settings, tz: ZoneInfo, cutoff: datetime) -> list[Article]:
    if source.type == "playwright":
        return _fetch_playwright(source, settings, tz)
    if source.type == "booli":
        return _fetch_booli(source, settings, tz)

    with _new_client(settings) as client:
        try:
            if source.type == "rss":
                return fetch_rss_articles(source, source.url, tz, cutoff, settings, client)

            # type == "html": try to discover a real feed first (far more
            # reliable than scraping links), fall back to link-scraping.
            feed_url = discover_feed_url(source.url, client)
            if feed_url:
                found = fetch_rss_articles(source, feed_url, tz, cutoff, settings, client)
                if found:
                    return found
            return fetch_html_fallback_articles(source, client, settings)
        except Exception:
            logger.exception("unexpected error fetching source %s", source.name)
            return []


def _fetch_playwright(source: Source, settings: Settings, tz: ZoneInfo) -> list[Article]:
    """Fetch articles using Playwright headless browser.

    The source URL is a Hemnet search results page (or similar JS-rendered
    listing site).  We launch a headless Chromium, navigate, extract cards,
    and return them as Article objects.
    """
    try:
        from .fetch_playwright import HemnetScraper
    except ImportError as exc:
        logger.error(
            "playwright source '%s' requested but not installed; "
            "run: pip install playwright && playwright install chromium",
            source.name,
        )
        raise SystemExit(
            f"Can't fetch {source.name}: playwright is not installed. "
            f"Run: pip install playwright && playwright install chromium"
        ) from exc

    import asyncio

    async def _run() -> list[Article]:
        async with HemnetScraper(headless=True, max_pages=3) as scraper:
            listings = await scraper.search(search_url=source.url)
        articles: list[Article] = []
        for listing in listings:
            # Build a descriptive title. Price shown as the "article title"
            # since Hemnet listings don't have a conventional headline.
            parts = [listing.title or listing.address]
            if listing.price:
                parts.append(f"{listing.price:,} kr".replace(",", " "))
            if listing.rooms:
                parts.append(f"{listing.rooms} rum")
            if listing.living_area:
                parts.append(f"{listing.living_area:.0f} m²")
            title = " | ".join(parts)

            articles.append(
                Article(
                    source_name=source.name,
                    source_category=source.category,
                    title=title,
                    url=listing.url,
                    published_at=None,  # Hemnet cards don't expose publish date
                )
            )
        return articles

    return asyncio.run(_run())


def _fetch_booli(source: Source, settings: Settings, tz: ZoneInfo) -> list[Article]:
    """Fetch articles using Booli Playwright scraper (same approach as Hemnet)."""
    try:
        from .fetch_booli import BooliScraper
    except ImportError as exc:
        logger.error("booli source '%s' requested but module not found", source.name)
        raise SystemExit(f"Can't fetch {source.name}: fetch_booli.py not found") from exc

    import asyncio

    async def _run() -> list[Article]:
        async with BooliScraper(headless=True, max_pages=3) as scraper:
            listings = await scraper.search(search_url=source.url)
        articles: list[Article] = []
        for listing in listings:
            parts = [listing.title or listing.address]
            if listing.price:
                parts.append(f"{listing.price:,} kr".replace(",", " "))
            if listing.rooms:
                parts.append(f"{listing.rooms} rum")
            if listing.living_area:
                parts.append(f"{listing.living_area:.0f} m²")
            title = " | ".join(parts)
            articles.append(
                Article(
                    source_name=source.name,
                    source_category=source.category,
                    title=title,
                    url=listing.url,
                    published_at=None,
                )
            )
        return articles

    return asyncio.run(_run())


def fetch_all(sources: list[Source], settings: Settings, tz: ZoneInfo, cutoff: datetime) -> list[Article]:
    """Fetch candidate articles published at/after `cutoff` from every enabled
    source, concurrently."""
    enabled = [s for s in sources if s.enabled]

    results: list[Article] = []
    with ThreadPoolExecutor(max_workers=settings.max_concurrent_fetches) as pool:
        futures = {pool.submit(fetch_source, s, settings, tz, cutoff): s for s in enabled}
        for future in as_completed(futures):
            source = futures[future]
            items = future.result()
            logger.info("%s: %d candidate article(s)", source.name, len(items))
            results.extend(items)
    return results
