"""Channel definitions: config/channels.yaml lists each topical digest (e.g.
"ai", "autonomous_driving"). Each channel gets its own sources file at
config/sources/<key>.yaml (see sourcesio.py) and its own topic taxonomy,
which parameterizes the map/reduce prompts in llm.py instead of those being
hardcoded to a single domain.

Adding a new channel = add an entry here + create its sources file with
`daily-digest sources add --channel <key> ...` (or hand-write the yaml).
No code changes needed elsewhere.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .config import REPO_ROOT
from .models import Channel

DEFAULT_CHANNELS_FILE = REPO_ROOT / "config" / "channels.yaml"
DEFAULT_SOURCES_DIR = REPO_ROOT / "config" / "sources"

# Every channel implicitly supports these two tags regardless of its own
# topic list: "其他" (catch-all) and "无关" (used by the map-stage prompt to
# flag articles that don't actually belong to this channel's domain, see
# llm.py -- those get filtered out before the reduce stage).
_IMPLICIT_TOPICS = ["其他", "无关"]


def load_channels(path: Path = DEFAULT_CHANNELS_FILE) -> list[Channel]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    channels = []
    for entry in raw.get("channels", []):
        topics = [t for t in entry.get("topics", []) if t not in _IMPLICIT_TOPICS]
        channels.append(
            Channel(
                key=entry["key"],
                name=entry["name"],
                domain_desc=entry["domain_desc"],
                topics=topics + _IMPLICIT_TOPICS,
            )
        )
    return channels


def get_channel(key: str, path: Path = DEFAULT_CHANNELS_FILE) -> Channel:
    for channel in load_channels(path):
        if channel.key == key:
            return channel
    available = ", ".join(c.key for c in load_channels(path)) or "(无)"
    raise ValueError(f"未知频道 {key!r}，可用频道: {available}（见 {path}）")


def sources_file_for(channel: Channel, sources_dir: Path = DEFAULT_SOURCES_DIR) -> Path:
    return sources_dir / f"{channel.key}.yaml"
