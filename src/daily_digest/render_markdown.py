"""Render a Digest as a portable Markdown file, with a link to every original source."""
from __future__ import annotations

from .models import Digest


def render_markdown(digest: Digest) -> str:
    lines = [f"# {digest.channel_name} · {digest.date_str}", ""]
    lines.append(f"_共扫描 {digest.source_count} 个信息源，收录 {digest.article_count} 篇今日文章_")
    lines.append("")

    if digest.executive_summary_zh:
        lines.append("## 今日要点")
        lines.extend(f"- {point}" for point in digest.executive_summary_zh)
        lines.append("")

    if not digest.sections:
        lines.append("_今天没有抓取到符合条件的资讯，请检查 config/sources.yaml 中的信息源是否可用。_")
        return "\n".join(lines).rstrip() + "\n"

    for section in digest.sections:
        lines.append(f"## {section.title_zh}")
        lines.append("")
        for item in section.items:
            lines.append(f"### {item.headline_zh}")
            lines.append("")
            lines.append(item.synthesis_zh)
            lines.append("")
            source_links = []
            for s in item.sources:
                time_str = (
                    s.article.published_at.strftime("%H:%M") if s.article.published_at else "时间未知"
                )
                source_links.append(f"[{s.article.source_name} ({time_str})]({s.article.url})")
            lines.append("来源: " + " · ".join(source_links))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
