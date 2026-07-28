"""Read/write config/sources.yaml -- the user-editable list of sites to scan."""
from __future__ import annotations

from pathlib import Path

import yaml

from .models import Source

_VALID_TYPES = {"rss", "html"}


def load_sources(path: Path) -> list[Source]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("sources") or []
    sources: list[Source] = []
    for entry in entries:
        sources.append(
            Source(
                name=entry["name"],
                url=entry["url"],
                type=entry.get("type", "rss"),
                category=entry.get("category", "未分类"),
                enabled=entry.get("enabled", True),
            )
        )
    return sources


def save_sources(path: Path, sources: list[Source]) -> None:
    payload = {
        "sources": [
            {
                "name": s.name,
                "type": s.type,
                "url": s.url,
                "category": s.category,
                "enabled": s.enabled,
            }
            for s in sources
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def add_source(
    path: Path,
    name: str,
    url: str,
    type_: str = "rss",
    category: str = "未分类",
) -> list[Source]:
    if type_ not in _VALID_TYPES:
        raise ValueError(f"type must be one of {_VALID_TYPES}, got {type_!r}")
    sources = load_sources(path)
    if any(s.name == name for s in sources):
        raise ValueError(f"a source named {name!r} already exists")
    sources.append(Source(name=name, url=url, type=type_, category=category))
    save_sources(path, sources)
    return sources


def remove_source(path: Path, name: str) -> list[Source]:
    sources = load_sources(path)
    remaining = [s for s in sources if s.name != name]
    if len(remaining) == len(sources):
        raise ValueError(f"no source named {name!r} found")
    save_sources(path, remaining)
    return remaining
