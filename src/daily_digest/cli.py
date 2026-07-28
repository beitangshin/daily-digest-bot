from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


def render(payload: dict) -> str:
    title = payload.get("title", f"Daily digest — {date.today().isoformat()}")
    lines = [f"# {title}", ""]
    for section in payload.get("sections", []):
        lines += [f"## {section.get('heading', 'Updates')}"]
        lines += [f"- {item}" for item in section.get("items", [])] or ["- No updates."]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a Markdown daily digest from local JSON.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=Path("digest.md"))
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    args.output.write_text(render(payload), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
