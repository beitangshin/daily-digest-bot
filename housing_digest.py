#!/usr/bin/env python3
"""瑞典房源日报 — AI 搜索 + 筛选 + 评分 一键生成。

修改搜索条件 → src/daily_digest/housing_pipeline.py 里的 SEARCH_CONFIG
修改过滤规则 → 同一文件里的 FILTERS 列表
修改评分要求 → 同一文件里的 SCORING_SYSTEM_PROMPT

首次运行（推荐先 dry-run 看能抓到多少）:
    python housing_digest.py --dry-run

跑完整流程（需要 DEEPSEEK_API_KEY，.env 里已配好）:
    python housing_digest.py

指定输出:
    python housing_digest.py -o ~/Desktop/stockholm_housing.html

调试（看浏览器在做什么）:
    python housing_digest.py --visible

命令行覆盖搜索条件:
    python housing_digest.py --city "Uppsala" --max-price 8000000 --min-rooms 3
"""
from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent / "src"
if _src.exists():
    sys.path.insert(0, str(_src))

from daily_digest.housing_pipeline import main

if __name__ == "__main__":
    main()
