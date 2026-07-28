"""Render a Digest as a self-contained local HTML page (no CDN/network deps),
with an in-page table of contents for jumping between items, and links out to
every original source. Also maintains a tabbed `index.html` that lists every
configured channel's history (see channels.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import Channel, Digest

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "jinja"]),
)


def render_digest_html(digest: Digest) -> str:
    template = _env.get_template("digest.html.jinja")
    return template.render(digest=digest)


def write_day_meta(day_dir: Path, digest: Digest) -> None:
    """Sidecar file so the index can be rebuilt without re-running the pipeline."""
    meta = {
        "date_str": digest.date_str,
        "article_count": digest.article_count,
        "source_count": digest.source_count,
    }
    (day_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def render_combined_index(output_dir: Path, channels: list[Channel]) -> str:
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
    return template.render(channels=channel_data)
