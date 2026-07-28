"""Per-channel run state: just "when did this channel's last successful run
start", so the next run can fetch "everything published since then" instead
of "everything published today" (calendar-day filtering has a gap: anything
published after the scheduled run time and before midnight would never be
"today" again once the next run happens -- see MAINTENANCE.md).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _state_file(channel_key: str, output_dir: Path) -> Path:
    return output_dir / channel_key / ".state.json"


def load_last_run_started_at(channel_key: str, output_dir: Path) -> datetime | None:
    path = _state_file(channel_key, output_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(raw["last_run_started_at"])
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def save_last_run_started_at(channel_key: str, output_dir: Path, when: datetime) -> None:
    path = _state_file(channel_key, output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_run_started_at": when.isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
