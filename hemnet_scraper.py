#!/usr/bin/env python3
"""Hemnet 房源搜索 — 无头 Playwright CLI 快捷入口。

这是 @daily_digest 项目"瑞典房源日报"频道的外挂搜索工具。
用 Playwright 打开 Hemnet 搜索结果页，抓取房源数据。

安装:
    pip install playwright
    playwright install chromium

一键搜索:
    python hemnet_scraper.py --location stockholm --max-price 5000000 --min-rooms 3

指定自己的搜索 URL（推荐 —— 在浏览器里设好所有筛选条件，复制地址栏）:
    python hemnet_scraper.py --url "https://www.hemnet.se/bostader?location_ids%5B%5D=..."

输出 JSON:
    python hemnet_scraper.py --location gothenburg --item-types villa --pages 3 --json -o listings.json

输出 HTML 页面并自动在浏览器打开:
    python hemnet_scraper.py --location uppsala --max-price 3000000 --html --open
    python hemnet_scraper.py --url "..." -o result.html --open

调试（看看浏览器在干什么）:
    python hemnet_scraper.py --location uppsala --max-price 3000000 --visible

集成到频道:
    daily-digest sources add --channel housing --name "Hemnet - Stockholm" --url "<搜索URL>" --type playwright
    daily-digest sources check --channel housing
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sure the project's src directory is on the path
_src = Path(__file__).resolve().parent / "src"
if _src.exists():
    sys.path.insert(0, str(_src))

from daily_digest.fetch_playwright import main

if __name__ == "__main__":
    main()
