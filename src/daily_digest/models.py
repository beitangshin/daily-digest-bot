"""Shared data structures for the digest pipeline."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Channel:
    """A topical digest (e.g. "AI industry", "autonomous driving"). Each
    channel has its own sources file (config/sources/<key>.yaml) and its own
    topic taxonomy used to steer the map/reduce prompts in llm.py."""

    key: str
    name: str
    domain_desc: str
    topics: list[str] = field(default_factory=list)


@dataclass
class Source:
    name: str
    url: str
    type: str = "rss"  # "rss" or "html"
    category: str = "未分类"
    enabled: bool = True


@dataclass
class Article:
    """A single article discovered from a source, before summarization."""

    source_name: str
    source_category: str
    title: str
    url: str
    published_at: datetime | None
    text: str = ""

    @property
    def id(self) -> str:
        return "src-" + hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:10]


@dataclass
class ArticleSummary:
    """Stage-1 (map) output: one structured summary per article."""

    article: Article
    headline_zh: str
    summary_zh: str
    key_points_zh: list[str] = field(default_factory=list)
    topic_tag: str = "其他"
    importance: int = 3


@dataclass
class DigestItem:
    """Stage-2 (reduce) output: one merged/deduped item in the final digest."""

    headline_zh: str
    synthesis_zh: str
    sources: list[ArticleSummary] = field(default_factory=list)


@dataclass
class DigestSection:
    title_zh: str
    items: list[DigestItem] = field(default_factory=list)


@dataclass
class Digest:
    date_str: str
    executive_summary_zh: list[str]
    sections: list[DigestSection]
    source_count: int
    article_count: int
    channel_name: str = ""
