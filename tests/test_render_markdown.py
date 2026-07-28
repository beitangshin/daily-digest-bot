from datetime import datetime
from zoneinfo import ZoneInfo

from daily_digest.models import Article, ArticleSummary, Digest, DigestItem, DigestSection
from daily_digest.render_markdown import render_markdown


def _make_digest() -> Digest:
    article = Article(
        source_name="TechCrunch AI",
        source_category="海外科技媒体",
        title="Example raw title",
        url="https://example.com/article-1",
        published_at=datetime(2026, 7, 28, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        text="raw article text",
    )
    summary = ArticleSummary(
        article=article,
        headline_zh="示例标题",
        summary_zh="这是示例摘要。",
        key_points_zh=["要点一"],
        topic_tag="产品应用",
        importance=4,
    )
    item = DigestItem(headline_zh="合并后的标题", synthesis_zh="合并后的综述文字。", sources=[summary])
    section = DigestSection(title_zh="产品与应用", items=[item])
    return Digest(
        date_str="2026-07-28",
        executive_summary_zh=["今日要点一"],
        sections=[section],
        source_count=1,
        article_count=1,
        channel_name="AI 行业日报",
    )


def test_render_markdown_includes_source_link_and_summary():
    md = render_markdown(_make_digest())
    assert "# AI 行业日报 · 2026-07-28" in md
    assert "今日要点一" in md
    assert "产品与应用" in md
    assert "合并后的标题" in md
    assert "[TechCrunch AI (09:30)](https://example.com/article-1)" in md


def test_render_markdown_handles_empty_digest():
    empty = Digest(date_str="2026-07-28", executive_summary_zh=[], sections=[], source_count=0, article_count=0)
    md = render_markdown(empty)
    assert "没有抓取到符合条件的资讯" in md
