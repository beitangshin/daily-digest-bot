from daily_digest.config import Settings
from daily_digest.llm import DeepSeekUnavailable, build_digest, summarize_articles
from daily_digest.models import Article, ArticleSummary, Channel

_CHANNEL = Channel(
    key="ai",
    name="AI 行业日报",
    domain_desc="人工智能（AI）行业",
    topics=["产品应用", "其他", "无关"],
)


def _settings(**overrides) -> Settings:
    base = dict(
        deepseek_api_key="fake-key",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-chat",
        request_timeout=5.0,
        timezone="UTC",
        language="zh",
        max_concurrent_fetches=2,
        max_concurrent_llm_calls=2,
        articles_per_batch=6,
        include_undated_articles=False,
        max_html_fallback_links=10,
        first_run_lookback_hours=24.0,
        max_lookback_hours=72.0,
    )
    base.update(overrides)
    return Settings(**base)


def _article(n: int) -> Article:
    return Article(
        source_name=f"Source {n}",
        source_category="测试分类",
        title=f"Title {n}",
        url=f"https://example.com/{n}",
        published_at=None,
        text=f"Body text {n}",
    )


def test_summarize_articles_maps_ids_and_ignores_hallucinated_id():
    articles = [_article(1), _article(2)]

    def fake_chat_fn(system, user, max_tokens):
        return {
            "items": [
                {"id": articles[0].id, "headline_zh": "标题一", "summary_zh": "摘要一", "key_points_zh": [], "topic_tag": "产品应用", "importance": 4},
                {"id": articles[1].id, "headline_zh": "标题二", "summary_zh": "摘要二", "key_points_zh": [], "topic_tag": "其他", "importance": 2},
                {"id": "src-doesnotexist", "headline_zh": "幻觉条目", "summary_zh": "不应出现", "key_points_zh": [], "topic_tag": "其他", "importance": 5},
            ]
        }

    summaries = summarize_articles(articles, _CHANNEL, _settings(), chat_fn=fake_chat_fn)
    assert len(summaries) == 2
    headlines = {s.headline_zh for s in summaries}
    assert headlines == {"标题一", "标题二"}
    assert "幻觉条目" not in headlines


def test_summarize_articles_falls_back_when_chat_fn_fails():
    articles = [_article(1), _article(2)]

    def failing_chat_fn(system, user, max_tokens):
        raise DeepSeekUnavailable("boom")

    summaries = summarize_articles(articles, _CHANNEL, _settings(), chat_fn=failing_chat_fn)
    assert len(summaries) == 2
    assert all("失败" in s.summary_zh for s in summaries)


def test_build_digest_drops_items_with_only_hallucinated_source_ids():
    a1, a2 = _article(1), _article(2)
    summaries = [
        ArticleSummary(article=a1, headline_zh="标题一", summary_zh="摘要一", topic_tag="产品应用", importance=4),
        ArticleSummary(article=a2, headline_zh="标题二", summary_zh="摘要二", topic_tag="其他", importance=2),
    ]

    def fake_chat_fn(system, user, max_tokens):
        return {
            "executive_summary_zh": ["今日要点"],
            "sections": [
                {
                    "section_title_zh": "产品与应用",
                    "items": [
                        {"headline_zh": "真实条目", "synthesis_zh": "综述", "source_ids": [a1.id]},
                        {"headline_zh": "幻觉条目", "synthesis_zh": "不应出现", "source_ids": ["src-doesnotexist"]},
                    ],
                }
            ],
        }

    digest = build_digest(summaries, _CHANNEL, "2026-07-28", _settings(), chat_fn=fake_chat_fn)
    assert len(digest.sections) == 1
    headlines = [item.headline_zh for item in digest.sections[0].items]
    assert headlines == ["真实条目"]
    assert digest.channel_name == "AI 行业日报"


def test_build_digest_falls_back_to_topic_grouping_when_reduce_fails():
    a1 = _article(1)
    summaries = [ArticleSummary(article=a1, headline_zh="标题一", summary_zh="摘要一", topic_tag="产品应用", importance=4)]

    def failing_chat_fn(system, user, max_tokens):
        raise DeepSeekUnavailable("boom")

    digest = build_digest(summaries, _CHANNEL, "2026-07-28", _settings(), chat_fn=failing_chat_fn)
    assert digest.article_count == 1
    assert digest.sections[0].title_zh == "产品应用"
    assert digest.channel_name == "AI 行业日报"
