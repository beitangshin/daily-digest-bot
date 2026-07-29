#!/usr/bin/env python3
"""Booli API 房源搜索 — 快捷 CLI。

需要先在 .env 里配置 BOOLI_CALLER_ID 和 BOOLI_API_KEY。
注册: https://www.booli.se/api/

用法:
    python booli_scraper.py --city Stockholm --max-price 5000000
    python booli_scraper.py --city Uppsala --item-types villa --html -o booli.html --open
    python booli_scraper.py --city Göteborg --min-rooms 3 --limit 20 --json
"""
from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent / "src"
if _src.exists():
    sys.path.insert(0, str(_src))

from daily_digest.fetch_booli import main

if __name__ == "__main__":
    main()
