"""Render a Digest as a self-contained local HTML page (no CDN/network deps),
with an in-page table of contents for jumping between items, and links to a
locally-archived copy of each article's extracted text (see
write_article_archives) instead of the original external URL -- so reading
the digest never requires clicking through to a source that may since have
gone behind a paywall. Also maintains a tabbed `index.html` that lists every
configured channel's history (see channels.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .extract import MAX_CHARS_PER_ARTICLE
from .models import Channel, Digest

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "jinja"]),
)


def render_digest_html(digest: Digest, cf_beacon_token: str | None = None) -> str:
    template = _env.get_template("digest.html.jinja")
    return template.render(digest=digest, cf_beacon_token=cf_beacon_token)


def write_article_archives(day_dir: Path, digest: Digest, cf_beacon_token: str | None = None) -> None:
    """Write one standalone reading page per article that has extracted full
    text, into day_dir/articles/<article.id>.html -- so digest.html/.md can
    link to a local copy instead of the original (possibly paywalled) URL."""
    seen: set[str] = set()
    articles_dir = day_dir / "articles"
    template = _env.get_template("article_archive.html.jinja")
    for section in digest.sections:
        for item in section.items:
            for s in item.sources:
                article = s.article
                if not article.text or article.id in seen:
                    continue
                seen.add(article.id)
                articles_dir.mkdir(parents=True, exist_ok=True)
                paragraphs = [p.strip() for p in re.split(r"\n+", article.text) if p.strip()]
                truncated = len(article.text) >= MAX_CHARS_PER_ARTICLE
                html = template.render(article=article, paragraphs=paragraphs, truncated=truncated, cf_beacon_token=cf_beacon_token)
                (articles_dir / f"{article.id}.html").write_text(html, encoding="utf-8")


def write_day_meta(day_dir: Path, digest: Digest) -> None:
    """Sidecar file so the index can be rebuilt without re-running the pipeline."""
    meta = {
        "date_str": digest.date_str,
        "article_count": digest.article_count,
        "source_count": digest.source_count,
    }
    (day_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def render_combined_index(output_dir: Path, channels: list[Channel], cf_beacon_token: str | None = None) -> str:
    """output/index.html: one tab per channel, each listing that channel's
    daily digests (newest first) read straight from meta.json sidecars --
    no re-running the pipeline needed just to rebuild this page."""
    channel_data = []
    for channel in channels:
        days = []
        channel_dir = output_dir / channel.key
        if channel_dir.exists():
            for meta_file in sorted(channel_dir.glob("*/meta.json"), reverse=True):
                try:
                    days.append(json.loads(meta_file.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    continue
        channel_data.append({"key": channel.key, "name": channel.name, "days": days})
    template = _env.get_template("index.html.jinja")
    return template.render(channels=channel_data, cf_beacon_token=cf_beacon_token)
