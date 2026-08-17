"""Runtime settings: environment variables (.env).

Source/channel configuration lives in channels.py + sourcesio.py instead --
this module only owns secrets/knobs that come from the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "output"


@dataclass
class Settings:
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    request_timeout: float
    timezone: str
    language: str
    max_concurrent_fetches: int
    max_concurrent_llm_calls: int
    articles_per_batch: int
    include_undated_articles: bool
    max_html_fallback_links: int
    first_run_lookback_hours: float
    max_lookback_hours: float
    cf_web_analytics_token: str | None = None

    @property
    def has_api_key(self) -> bool:
        return bool(self.deepseek_api_key)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from a `.env` file (if present) plus environment variables.

    `.env` is never committed (see .gitignore) -- copy `.env.example` to `.env`
    and fill in DEEPSEEK_API_KEY before running `daily-digest run`.
    """
    load_dotenv(dotenv_path=env_file or (REPO_ROOT / ".env"))

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name)
        return int(raw) if raw else default

    def _bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    return Settings(
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        timezone=os.getenv("DIGEST_TIMEZONE", "Europe/Stockholm"),
        language=os.getenv("DIGEST_LANGUAGE", "zh"),
        max_concurrent_fetches=_int("MAX_CONCURRENT_FETCHES", 8),
        max_concurrent_llm_calls=_int("MAX_CONCURRENT_LLM_CALLS", 3),
        articles_per_batch=_int("ARTICLES_PER_LLM_BATCH", 6),
        include_undated_articles=_bool("INCLUDE_UNDATED_ARTICLES", False),
        max_html_fallback_links=_int("MAX_HTML_FALLBACK_LINKS", 20),
        first_run_lookback_hours=float(os.getenv("FIRST_RUN_LOOKBACK_HOURS", "24")),
        max_lookback_hours=float(os.getenv("MAX_LOOKBACK_HOURS", "72")),
        cf_web_analytics_token=os.getenv("CF_WEB_ANALYTICS_TOKEN") or None,
    )
