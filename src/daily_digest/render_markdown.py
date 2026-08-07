"""Render a Digest as a portable Markdown file. Each source links to a local
archive of its extracted text (written by render_html.write_article_archives)
rather than the original external URL, so reading the digest never depends on
a source page that may since have gone behind a paywall. Articles where full
extraction failed have no archive to link to and are shown as plain text."""
from __future__ import annotations

from .models import Digest


def render_markdown(digest: Digest, archive_prefix: str = "") -> str:
    """archive_prefix: relative path from wherever this markdown is written
    to the day directory containing articles/ -- "" when writing directly
    into the day directory (digest.md), "<date_str>/" when writing to
    channel_dir/latest.md (one level up from the day directory)."""
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
                label = f"{s.article.source_name} ({time_str})"
                if s.article.text:
                    source_links.append(f"[{label}]({archive_prefix}articles/{s.article.id}.html)")
                else:
                    source_links.append(f"{label} · 仅标题（正文获取失败）")
            lines.append("来源: " + " · ".join(source_links))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
