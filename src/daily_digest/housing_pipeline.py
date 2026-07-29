#!/usr/bin/env python3
"""瑞典房源搜索 + AI 筛选评分流水线。

模块化设计，每部分可独立修改：

  1. SEARCH_CONFIG      — 搜索条件（城市/价格/房型/房间数）
  2. HARD_FILTERS        — 硬性过滤（楼层/价格上限/房间数下限）
  3. AI_SCORING          — DeepSeek 评分 prompt + 解析
  4. SAFETY_CHECKER      — 街区安全搜索（联网）
  5. RENDERER            — 生成 HTML 简报

用法:
    python housing_digest.py                         # 用默认搜索配置跑
    python housing_digest.py --visible               # 调试模式看浏览器
    python housing_digest.py --output my_housing.html  # 指定输出
    python housing_digest.py --dry-run                # 只抓取，不调用 AI

快捷:
    python housing_digest.py                          # 等价于上面
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ======================================================================
# [模块] SEARCH_CONFIG — 搜索条件配置
# 修改这里来调整搜什么，不需要改下方代码
# ======================================================================

SEARCH_CONFIG = {
    # === 搜索条件 ===
    # Booli 搜索关键词（不设 q = 全瑞典），靠下面的地域过滤留 Stockholms län
    "q": None,

    # 房型：villa, bostadsrätt, lägenhet, radhus, fritidshus ...
    "item_types": ["villa", "bostadsrätt", "lägenhet", "radhus", "parhus"],

    # 价格上限 SEK
    "max_price": 9_500_000,
    # 价格下限（可以设一个合理的下限过滤太便宜的）
    "min_price": 500_000,

    # 最少房间数
    "min_rooms": 4,

    # 最多房间数（可选）
    "max_rooms": None,

    # 抓取页数（每页约 15-30 条）
    "pages": 10,

    # 排序方式：pubdate_desc（最新优先）, price_asc（价格从低到高）, price_desc
    "sort": "pubdate_desc",

    # === Hemnet 搜索（可选，跟 Booli 用同一套条件） ===
    "hemnet_enabled": True,
    # Hemnet 地区名（传给 _build_search_url 的 location 参数）
    # 可选值: stockholm, gothenburg, malmo, uppsala ... 或 None（全瑞典）
    "hemnet_location": "stockholm",

    # === 地域过滤（硬过滤阶段会按此筛选） ===
    # 只保留这些城市/区域的房源（关键词匹配，不区分大小写）
    # 列表包含 Stockholms län 所有 kommun + 主要街区
    "allowed_cities": [
        # Stockholm 核心区
        "Stockholm", "Södermalm", "Östermalm", "Norrmalm", "Vasastan",
        "Kungsholmen", "Gamla stan", "Djurgården", "Liljeholmen",
        "Södra stationsområdet", "Hornstull", "Mariatorget",
        "Odenplan", "Stureplan", "Karlberg", "Fredhäll",
        # 西部
        "Bromma", "Alvik", "Stora mossen", "Äppelviken", "Nockeby",
        "Ekerö", "Drottningholm",
        # 东北部
        "Solna", "Sundbyberg", "Råsunda", "Huvudsta",
        "Lidingö", "Täby", "Danderyd", "Djursholm",
        "Sollentuna", "Edsberg", "Helenelund",
        "Upplands Väsby", "Sigtuna", "Märsta",
        "Vallentuna", "Österåker", "Åkersberga",
        "Vaxholm", "Norrtälje",
        # 东南部
        "Nacka", "Saltsjöbaden", "Fisksätra", "Järla Sjö",
        "Värmdö", "Gustavsberg", "Herrängen",
        "Tyresö", "Haninge", "Vendelsö", "Vega",
        # 南部
        "Huddinge", "Flemingsberg", "Segeltorp", "Kungens kurva",
        "Botkyrka", "Tullinge", "Tumba", "Alby",
        "Salem", "Rönninge",
        # 西南部
        "Järfälla", "Jakobsberg", "Barkarby",
        "Ekerö", "Stenhamra",
        # 北部
        "Upplands-Bro", "Kungsängen", "Bro",
        "Märsta", "Rosersberg",
    ],
    # 排除区域（即使价格/房间数符合也跳过）
    "blocked_cities": [
        "Göteborg", "Malmö", "Uppsala", "Västerås",
        "Örebro", "Linköping", "Norrköping", "Jönköping",
        "Helsingborg", "Lund", "Umeå", "Karlstad", "Gävle",
        "Halmstad", "Sundsvall", "Luleå", "Trollhättan", "Borås",
        "Eskilstuna", "Kalmar", "Kristianstad", "Karlskrona",
        "Skellefteå", "Östersund", "Falun", "Växjö",
        "Ängelholm", "Landskrona", "Trelleborg",
        "Kungsbacka", "Mölndal", "Partille",
        "Uddevalla", "Strömstad",
        "Älvdalen", "Mora", "Kiruna",
        "Södertälje",  # Södertälje is technically in Stockholm County but too far
    ],
}

# ======================================================================
# [模块] HARD_FILTERS — 硬性过滤规则
# 每条是一个 dict: {"name": ..., "enabled": True/False, "fn": (BooliListing)->bool}
# 返回 True = 保留，返回 False = 淘汰
# ======================================================================


def _filter_by_price(listing) -> bool:
    """价格必须在 [min_price, max_price] 范围内"""
    cfg = SEARCH_CONFIG
    if listing.price == 0:
        return False  # 无价格信息直接淘汰
    if cfg["min_price"] and listing.price < cfg["min_price"]:
        return False
    if cfg["max_price"] and listing.price > cfg["max_price"]:
        return False
    return True


def _filter_by_rooms(listing) -> bool:
    """房间数必须 >= min_rooms"""
    cfg = SEARCH_CONFIG
    if listing.rooms is None:
        return True  # 没有房间数信息时放行（可能是 villa 不标房间）
    if cfg["min_rooms"] and listing.rooms < cfg["min_rooms"]:
        return False
    if cfg.get("max_rooms") and listing.rooms > cfg["max_rooms"]:
        return False
    return True


def _filter_by_type(listing) -> bool:
    """房型必须在允许列表中"""
    allowed = SEARCH_CONFIG["item_types"]
    if not listing.listing_type or not allowed:
        return True
    # 模糊匹配
    lt = listing.listing_type.lower()
    for a in allowed:
        al = a.lower()
        if al in lt or lt in al:
            return True
    return False


# 楼层黑名单：不要底楼和一楼
_FLOOR_BLACKLIST = {"bottenvåning", "bottenplan", "bv", "våning 1", "våning1", "1 tr", "1tr",
                    "1 trappa", "gatuplan", "entréplan"}


def _filter_by_floor(listing) -> bool:
    """过滤底楼和一楼（通过解析房源文字）"""
    # 如果数据库里没有楼层信息，放行（无法判断）
    # 这个 filter 需要文字分析，在 AI 阶段也会再做一次
    return True  # 硬过滤不做楼层判断（因为文本解析不确定性高），交给 AI 评分


def _filter_by_city(listing) -> bool:
    """地理过滤：只保留 Stockholms län 内的房源。

    默认拒绝（只接受 allowed_cities 列表中的城市），除非城市字段
    明显匹配 Stockholm 区域。
    """
    cfg = SEARCH_CONFIG
    allowed = cfg.get("allowed_cities", [])
    blocked = cfg.get("blocked_cities", [])

    # 从 listing 中获取位置信息
    location_text = ""
    if hasattr(listing, 'city') and listing.city:
        location_text += listing.city + " "
    if hasattr(listing, 'address') and listing.address:
        location_text += listing.address + " "
    if hasattr(listing, 'title') and listing.title:
        location_text += listing.title

    location_lower = location_text.lower()

    # 黑名单优先：明显不在 Stockholm 的直接拒绝
    for b in blocked:
        if b.lower() in location_lower:
            return False

    # 白名单：明确在 Stockholms län 的接受
    for a in allowed:
        if a.lower() in location_lower:
            return True

    # 额外规则：地址中直接包含 "Stockholm" 的接受
    if "stockholm" in location_lower:
        return True

    # 不明确的：拒绝（宁缺毋滥，不要远端城市的房源冒充）
    return False


FILTERS: list[dict] = [
    {"name": "价格范围", "enabled": True, "fn": _filter_by_price},
    {"name": "房间数", "enabled": True, "fn": _filter_by_rooms},
    {"name": "房型", "enabled": True, "fn": _filter_by_type},
    {"name": "地理过滤", "enabled": True, "fn": _filter_by_city},
    {"name": "楼层过滤", "enabled": False, "fn": _filter_by_floor},
]


def apply_hard_filters(listings: list) -> list:
    """对所有房源依次应用启用的硬过滤器。"""
    result = list(listings)
    for f in FILTERS:
        if not f["enabled"]:
            continue
        before = len(result)
        new_result = []
        for l in result:
            try:
                if f["fn"](l):
                    new_result.append(l)
                elif logger.isEnabledFor(logging.DEBUG):
                    logger.debug("    %s 淘汰: [%s] city=%s addr=%s", f["name"], getattr(l, 'listing_type', '?'), getattr(l, 'city', '?'), getattr(l, 'address', '?')[:40])
            except Exception as exc:
                logger.warning("  过滤器 [%s] 异常: %s，放行该房源", f["name"], exc)
                new_result.append(l)
        result = new_result
        dropped = before - len(result)
        if dropped:
            logger.info("  过滤器 [%s] 淘汰了 %d 套", f["name"], dropped)
    return result


# ======================================================================
# [模块] SAFETY_CHECKER — 街区安全信息获取
# ======================================================================


async def check_area_safety(areas: list[str]) -> dict[str, str]:
    """对每个区域/城市搜索安全信息，返回 {区域: 安全描述}。

    用 WebSearch 搜索 "{area} Stockholm crime safety" 之类的查询。
    结果会被喂给 AI scoring 阶段。
    """
    if not areas:
        return {}

    # 去重
    unique_areas = list(dict.fromkeys(areas))
    logger.info("正在检查 %d 个区域的安全信息...", len(unique_areas))

    safety_info: dict[str, str] = {}
    for area in unique_areas:
        if not area or area == "Stockholms län":
            continue
        try:
            query = f"{area} Stockholm brottslighet trygghet 2024 2025"
            # 用 WebFetch 搜索本地安全信息
            result = await _web_search_safety(area, query)
            if result:
                safety_info[area] = result
        except Exception as exc:
            logger.debug("safety check for %s failed: %s", area, exc)

    return safety_info


async def _web_search_safety(area: str, query: str) -> str | None:
    """对单个区域做安全搜索."""
    try:
        # 尝试使用 WebFetch 获取搜索结果
        from daily_digest.fetch import _new_client
        from daily_digest.config import Settings, REPO_ROOT
        import httpx

        search_url = f"https://www.google.com/search?q={urlencode({'q': query})}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(headers=headers, timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(search_url)
            if resp.status_code == 200:
                text = resp.text
                # 提取片段
                snippets = re.findall(r'<div[^>]*class="[^"]*BNeawe[^"]*"[^>]*>(.*?)</div>', text, re.DOTALL)
                combined = " ".join(snippets[:5])
                if combined:
                    return combined[:1000]
    except Exception:
        pass
    return None


# ======================================================================
# [模块] AI_SCORING — DeepSeek 评分
# ======================================================================

# 评分系统 prompt
SCORING_SYSTEM_PROMPT = """你是一名瑞典房产评估专家。你会收到一套房源信息，请对每套房源进行全面评估。

评估维度（每项 1-10 分）：
1. 性价比：相比该区域市场价是否合理
2. 安全性：根据你对 Stockholm 各街区治安的了解，评估该区域安全程度
3. 楼层评价：底楼（bottenvåning）和一楼（våning 1 / 1 tr）扣分，中间楼层加分
4. 户型质量：房间布局是否合理、面积是否够用
5. 投资潜力：升值空间、区域发展前景

总分 = 加权平均（性价比 25%、安全性 30%、楼层 15%、户型 15%、投资 5%）

评分标准：
- 9-10 分：强烈推荐，各方面优秀
- 7-8 分：推荐，总体上好
- 5-6 分：可考虑，有明显缺点
- 3-4 分：不推荐，有严重问题
- 1-2 分：非常差，规避

对于安全性，你的判断依据：
- Stockholm 市中心部分区域（如 Rinkeby, Tensta, Husby, Jordbro）有较多治安问题，酌情扣分
- 郊区中产阶级社区（如 Bromma, Solna, Sundbyberg, Nacka）一般安全
- Stockholm 南部郊区（Huddinge, Haninge）部分区域较安全
- 如果没有明确证据表明危险，默认为安全并给 7 分以上

在楼层评估中：
- "bottenvåning"（底楼）→ 给 3-5 分
- "våning 1" / "1 tr"（底楼上一层）→ 给 4-6 分
- 中间楼层（2-5）→ 给 7-9 分
- 高层（6+）→ 给 8-10 分
- 别墅/联排不适用楼层评分，统一给 8 分

严格遵守 JSON 格式输出。"""

SCORING_USER_PROMPT_TEMPLATE = """请评估以下 {count} 套房源：

{listings_json}

安全参考信息：
{safety_info}

为每套输出：
- "url": 房源链接
- "total_score": 总分 (1-10)
- "scores": {{"value": 性价比, "safety": 安全性, "floor": 楼层, "layout": 户型, "potential": 投资}}
- "verdict_zh": 一句话中文结论（推荐/不推荐+原因）
- "floor_issue": 如果有楼层问题（底楼或一楼），说明是什么楼层；否则 null
- "safety_concern": 如果有安全隐患，说明原因；否则 null

输出 JSON 数组格式：{{"items": [...]}}

请确保 items 包含所有 {count} 套房源的评估，不要遗漏。"""


def _build_scoring_input(listings: list) -> str:
    """将房源列表转为 LLM 可读的 JSON 格式。"""
    items = []
    for i, l in enumerate(listings):
        items.append({
            "index": i,
            "address": l.title or l.address,
            "city": getattr(l, 'city', ''),
            "price": f"{l.price:,} kr".replace(",", " ") if l.price else "未知",
            "rooms": l.rooms,
            "living_area": f"{l.living_area} m²" if l.living_area else "未知",
            "monthly_fee": f"{l.monthly_fee:,} kr/mån".replace(",", " ") if l.monthly_fee else "未知",
            "type": l.listing_type or "未知",
            "url": l.url,
            "all_text": "",  # 可以传给 LLM 做完整分析
        })
    return json.dumps(items, ensure_ascii=False, indent=2)


def _parse_scores(api_response: dict, count: int) -> list[dict]:
    """解析 DeepSeek 的 JSON 返回为评分列表。"""
    items = api_response.get("items", []) if isinstance(api_response, dict) else []
    if not items:
        logger.warning("AI 评分返回为空或格式异常")
        return []

    results: list[dict] = []
    for item in items:
        results.append({
            "url": item.get("url", ""),
            "total_score": max(1, min(10, int(item.get("total_score", 5)))),
            "scores": item.get("scores", {}),
            "verdict_zh": item.get("verdict_zh", ""),
            "floor_issue": item.get("floor_issue"),
            "safety_concern": item.get("safety_concern"),
        })
    return results


# ======================================================================
# [模块] PIPELINE ORCHESTRATOR
# ======================================================================


@dataclass
class ScoredListing:
    """房源 + AI 评分结果。"""
    listing: Any
    total_score: int = 5
    scores: dict = field(default_factory=dict)
    verdict_zh: str = ""
    floor_issue: str | None = None
    safety_concern: str | None = None


async def run_pipeline(
    *,
    search_config: dict | None = None,
    headless: bool = True,
    dry_run: bool = False,
    output: str | None = None,
) -> list[ScoredListing]:
    """运行整个流水线：搜索 → 硬过滤 → AI 评分 → HTML 输出。

    Args:
        search_config: 覆盖默认搜索配置
        headless: Playwright 是否无头
        dry_run: True = 只抓取不过 AI
        output: HTML 输出路径（None = 打印到终端）
    """
    cfg = {**SEARCH_CONFIG, **(search_config or {})}

    # ---- Step 1: 从 Booli + Hemnet 抓取 ----
    logger.info("=" * 50)
    logger.info("Step 1: 抓取房源（Booli + Hemnet）")
    logger.info("  搜索条件: q=%s, max_price=%s, min_rooms=%s, types=%s",
                cfg["q"], cfg["max_price"], cfg["min_rooms"], cfg["item_types"])
    logger.info("=" * 50)

    all_listings: list = []

    # --- Booli ---
    try:
        from .fetch_booli import BooliScraper, _build_search_url as booli_url
        booli_search_url = booli_url(
            q=cfg["q"],
            item_types=cfg["item_types"],
            max_price=cfg["max_price"],
            min_price=cfg.get("min_price"),
            sort=cfg.get("sort", "pubdate_desc"),
        )
        async with BooliScraper(headless=headless, max_pages=cfg["pages"]) as scraper:
            booli_listings = await scraper.search(search_url=booli_search_url)
        # 统一字段名
        for l in booli_listings:
            l.source = "booli"
        all_listings.extend(booli_listings)
        logger.info("  Booli: %d 套", len(booli_listings))
    except Exception as exc:
        logger.warning("  Booli 抓取失败: %s", exc)

    # --- Hemnet（跟 Booli 用同一套搜索条件） ---
    if cfg.get("hemnet_enabled", True):
        try:
            from .fetch_playwright import HemnetScraper, _build_search_url as hemnet_url
            hemnet_search_url = hemnet_url(
                location=cfg.get("hemnet_location"),
                item_types=cfg["item_types"],
                max_price=cfg["max_price"],
                min_price=cfg.get("min_price"),
                min_rooms=cfg["min_rooms"],
                sort="publication_time_desc",
            )
            async with HemnetScraper(headless=headless, max_pages=cfg["pages"]) as scraper:
                hemnet_listings = await scraper.search(search_url=hemnet_search_url)
            for l in hemnet_listings:
                l.source = "hemnet"
            all_listings.extend(hemnet_listings)
            logger.info("  Hemnet: %d 套", len(hemnet_listings))
        except Exception as exc:
            logger.warning("  Hemnet 抓取失败: %s", exc)
    else:
        logger.info("  Hemnet: 已禁用（跳过）")

    # --- 去重 ---
    seen_urls: set[str] = set()
    unique_listings: list = []
    for l in all_listings:
        if l.url in seen_urls:
            continue
        seen_urls.add(l.url)
        unique_listings.append(l)

    logger.info("  Booli+Hemnet 共 %d 套（去重后 %d 套）", len(all_listings), len(unique_listings))
    all_listings = unique_listings

    if not all_listings:
        logger.warning("没有抓到任何房源，请检查搜索条件")
        return []

    # ---- Step 2: 硬过滤 ----
    logger.info("=" * 50)
    logger.info("Step 2: 硬性过滤")
    logger.info("=" * 50)
    filtered = apply_hard_filters(all_listings)
    logger.info("过滤后剩余 %d 套", len(filtered))

    if not filtered:
        logger.warning("硬过滤后没有剩余房源")
        return []

    if dry_run:
        print(f"\n[Dry Run] 抓到 {len(all_listings)} 套，过滤后 {len(filtered)} 套")
        for i, l in enumerate(filtered[:10], 1):
            print(f"  {i}. {l.title or l.address} — {l.price:,} kr — {l.rooms} rum — {l.listing_type}")
        if len(filtered) > 10:
            print(f"  ... 还有 {len(filtered)-10} 套")
        # 生成简单 HTML 展示
        if output:
            _write_simple_html(filtered, output)
        return []

    # ---- Step 3: 安全检查 ----
    logger.info("=" * 50)
    logger.info("Step 3: 区域安全检查")
    logger.info("=" * 50)
    # 收集所有区域名
    areas = []
    for l in filtered:
        if hasattr(l, 'city') and l.city and l.city != "Stockholms län":
            areas.append(l.city)
    safety_info = await check_area_safety(areas)
    safety_text = "\n".join(f"- {k}: {v[:200]}" for k, v in safety_info.items()) if safety_info else "无特殊安全信息"
    logger.info("安全信息获取完成，涉及 %d 个区域", len(safety_info))

    # ---- Step 4: AI 评分 ----
    logger.info("=" * 50)
    logger.info("Step 4: DeepSeek AI 评分")
    logger.info("=" * 50)

    from .config import load_settings
    from .llm import make_chat_fn

    settings = load_settings()
    chat_fn = make_chat_fn(settings)

    # 分批评分（每批最多 15 套）
    BATCH_SIZE = 15
    scored_all: list[ScoredListing] = []
    for batch_start in range(0, len(filtered), BATCH_SIZE):
        batch = filtered[batch_start:batch_start + BATCH_SIZE]
        listings_json = _build_scoring_input(batch)
        user_prompt = SCORING_USER_PROMPT_TEMPLATE.format(
            count=len(batch),
            listings_json=listings_json,
            safety_info=safety_text,
        )

        logger.info("  正在评分第 %d-%d 套...", batch_start + 1, min(batch_start + BATCH_SIZE, len(filtered)))
        try:
            api_result = chat_fn(SCORING_SYSTEM_PROMPT, user_prompt, 4000)
            scores = _parse_scores(api_result, len(batch))
        except Exception as exc:
            logger.warning("AI 评分失败: %s，使用默认评分", exc)
            scores = []

        # 配对评分和房源
        url_to_score = {s["url"]: s for s in scores}
        for listing in batch:
            score_info = url_to_score.get(listing.url, {})
            scored_all.append(ScoredListing(
                listing=listing,
                total_score=score_info.get("total_score", 5),
                scores=score_info.get("scores", {}),
                verdict_zh=score_info.get("verdict_zh", ""),
                floor_issue=score_info.get("floor_issue"),
                safety_concern=score_info.get("safety_concern"),
            ))

    # 按评分从高到低排序
    scored_all.sort(key=lambda x: x.total_score, reverse=True)

    logger.info("评分完成，共 %d 套", len(scored_all))
    high = sum(1 for s in scored_all if s.total_score >= 7)
    mid = sum(1 for s in scored_all if 4 <= s.total_score <= 6)
    low = sum(1 for s in scored_all if s.total_score <= 3)
    logger.info("  高分(7+): %d | 中分(4-6): %d | 低分(1-3): %d", high, mid, low)

    # ---- Step 5: 生成 HTML ----
    if output:
        _write_html(scored_all, output, cfg)
        print(f"✓ 结果已保存到 {output}")

        # 同时生成日期目录 + meta.json 供首页 index.html 自动发现
        from datetime import datetime as _dt
        _date_str = _dt.now().strftime("%Y-%m-%d")
        _housing_dir = Path(output).parent / _date_str
        _housing_dir.mkdir(parents=True, exist_ok=True)
        # 复制 digest.html
        (_housing_dir / "digest.html").write_text(Path(output).read_text(encoding="utf-8"), encoding="utf-8")
        # 写 meta.json
        _meta = {
            "date_str": _date_str,
            "article_count": len(scored_all),
            "source_count": len(set(getattr(s.listing, 'source', 'booli') for s in scored_all)) or 1,
        }
        (_housing_dir / "meta.json").write_text(json.dumps(_meta, ensure_ascii=False), encoding="utf-8")

        # 重新生成首页 index.html（让它包含房源频道）
        try:
            from .render_html import render_combined_index
            from .channels import load_channels
            from .config import DEFAULT_OUTPUT_DIR
            _all_channels = load_channels()
            _index_html = render_combined_index(Path(output).parents[1], _all_channels)
            (Path(output).parents[1] / "index.html").write_text(_index_html, encoding="utf-8")
            logger.info("已刷新 output/index.html")
        except Exception as _exc:
            logger.debug("刷新 index.html 失败（不影响房源结果）: %s", _exc)
        print(f"  ↑ 首页 index.html 已更新")
    else:
        _print_summary(scored_all)

    return scored_all


# ======================================================================
# [模块] RENDERER — HTML 输出
# ======================================================================


def _write_html(scored: list[ScoredListing], path: str, cfg: dict) -> None:
    """生成带评分的房源 HTML 简报。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 按评分分组
    high = [s for s in scored if s.total_score >= 7]
    mid = [s for s in scored if 4 <= s.total_score <= 6]
    low = [s for s in scored if s.total_score <= 3]

    cards_html = ""
    for group_title, group, group_class in [
        (f"🏆 推荐 ({len(high)} 套)", high, "high"),
        (f"📋 可考虑 ({len(mid)} 套)", mid, "mid"),
        (f"⚠️ 不推荐 ({len(low)} 套)", low, "low"),
    ]:
        if not group:
            continue
        cards_html += f"<h2 class='group-title {group_class}'>{group_title}</h2>"
        cards_html += "<div class='cards'>"

        for item in group:
            l = item.listing
            price_str = f"{l.price:,} kr".replace(",", " ") if l.price else "? kr"
            rooms_str = f"{l.rooms} rum" if l.rooms else ""
            area_str = f"{l.living_area:.0f} m²" if l.living_area else ""
            fee_str = f"{l.monthly_fee:,} kr/mån".replace(",", " ") if l.monthly_fee else ""

            meta_parts = []
            if rooms_str:
                meta_parts.append(f"<span class='tag'>{rooms_str}</span>")
            if area_str:
                meta_parts.append(f"<span class='tag'>{area_str}</span>")
            if l.listing_type:
                t_icons = {"villa": "🏠", "bostadsrätt": "🏢", "radhus": "🏘️", "parhus": "🏘️", "lägenhet": "🏢"}
                icon = t_icons.get(l.listing_type, "")
                meta_parts.append(f"<span class='tag type-tag'>{icon} {l.listing_type}</span>")
            if fee_str:
                meta_parts.append(f"<span class='tag fee-tag'>{fee_str}</span>")

            # Score display
            score = item.total_score
            score_color = "#22c55e" if score >= 7 else "#eab308" if score >= 4 else "#ef4444"

            # Floor issue warning
            floor_warn = ""
            if item.floor_issue:
                floor_warn = f"<p class='warn floor-warn'>⚠️ 楼层问题: {item.floor_issue}</p>"
            safety_warn = ""
            if item.safety_concern:
                safety_warn = f"<p class='warn safety-warn'>🔒 安全问题: {item.safety_concern}</p>"

            # Image
            img_html = ""
            if l.image_url:
                img_html = f"<img src='{l.image_url}' alt='' loading='lazy'>"
            else:
                img_html = "<div class='no-img'>📷</div>"

            city_name = getattr(l, 'city', '') or ""

            cards_html += f"""
            <div class='card score-{score}'>
                <div class='card-img'>{img_html}</div>
                <div class='card-body'>
                    <div class='score-badge' style='background:{score_color}'>{score}</div>
                    <h2><a href='{l.url}' target='_blank'>{l.title or l.address}</a></h2>
                    <p class='city-line'>{city_name}</p>
                    <p class='price'>{price_str}</p>
                    <div class='meta'>{' '.join(meta_parts)}</div>
                    {floor_warn}
                    {safety_warn}
                    {f"<p class='verdict'>{item.verdict_zh}</p>" if item.verdict_zh else ""}
                </div>
            </div>"""

        cards_html += "</div>"

    # Sub-scores breakdown (expandable)
    details_rows = ""
    for s in scored:
        if s.scores:
            sc = s.scores
            details_rows += f"""
            <tr>
                <td><a href='{s.listing.url}'>{s.listing.title or s.listing.address}</a></td>
                <td><strong>{s.total_score}</strong></td>
                <td>{sc.get('value', '-')}</td>
                <td>{sc.get('safety', '-')}</td>
                <td>{sc.get('floor', '-')}</td>
                <td>{sc.get('layout', '-')}</td>
                <td>{sc.get('potential', '-')}</td>
                <td style='font-size:12px;color:#666'>{s.verdict_zh}</td>
            </tr>"""

    # Build search criteria text for the report
    criteria_parts = [
        f"📍 Stockholms län",
        f"💰 ≤ {cfg['max_price']:,} kr".replace(",", " "),
        f"🛏 ≥ {cfg['min_rooms']} rum",
        f"🏠 {'/'.join(cfg['item_types'])}",
    ]
    criteria_text = " · ".join(criteria_parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>瑞典房源日报 · AI 评分 · {now[:10]}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f5f5; color: #222; padding: 20px;
  }}
  .header {{ max-width: 1200px; margin: 0 auto 20px; }}
  .header h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .header .meta {{ font-size: .85rem; color: #888; }}
  .header .criteria {{ font-size: .85rem; color: #555; margin-top: 8px; padding: 8px 12px; background: #fff; border-radius: 8px; display: inline-block; }}
  .summary-bar {{ display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }}
  .summary-item {{ background: #fff; border-radius: 10px; padding: 12px 20px; flex: 1; min-width: 120px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .summary-item .num {{ font-size: 1.8rem; font-weight: 700; }}
  .summary-item .label {{ font-size: .8rem; color: #888; }}
  .group-title {{ font-size: 1.2rem; margin: 24px 0 12px; padding-bottom: 6px; border-bottom: 2px solid #ddd; }}
  .group-title.high {{ border-color: #22c55e; }}
  .group-title.mid {{ border-color: #eab308; }}
  .group-title.low {{ border-color: #ef4444; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); position: relative; transition: box-shadow .15s; }}
  .card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,.12); }}
  .card-img {{ width: 100%; height: 180px; overflow: hidden; background: #e0e0e0; }}
  .card-img img {{ width: 100%; height: 100%; object-fit: cover; }}
  .no-img {{ display: flex; align-items: center; justify-content: center; height: 100%; font-size: 3rem; color: #aaa; }}
  .card-body {{ padding: 14px; position: relative; }}
  .score-badge {{
    position: absolute; top: -40px; right: 12px;
    width: 36px; height: 36px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 1rem; color: #fff;
    box-shadow: 0 2px 6px rgba(0,0,0,.2);
  }}
  .card-body h2 {{ font-size: .95rem; line-height: 1.4; margin-bottom: 2px; padding-right: 40px; }}
  .card-body h2 a {{ color: #222; text-decoration: none; }}
  .card-body h2 a:hover {{ color: #2563eb; }}
  .city-line {{ font-size: .8rem; color: #888; margin-bottom: 6px; }}
  .price {{ font-size: 1.15rem; font-weight: 700; color: #db4c3f; margin-bottom: 8px; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }}
  .tag {{ display: inline-block; font-size: .75rem; padding: 2px 8px; border-radius: 4px; background: #f0f0f0; color: #555; }}
  .type-tag {{ background: #e8f0fe; color: #1967d2; }}
  .fee-tag {{ background: #fef7e0; color: #b8860b; }}
  .warn {{ font-size: .78rem; padding: 4px 8px; border-radius: 4px; margin-top: 4px; }}
  .floor-warn {{ background: #fff3cd; color: #856404; }}
  .safety-warn {{ background: #f8d7da; color: #721c24; }}
  .verdict {{ font-size: .82rem; color: #444; margin-top: 6px; font-style: italic; }}
  .details-section {{ margin-top: 32px; background: #fff; border-radius: 12px; padding: 20px; overflow-x: auto; }}
  .details-section h2 {{ font-size: 1.1rem; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f8fafc; font-weight: 600; color: #555; position: sticky; top: 0; }}
  tr:hover {{ background: #fafafa; }}
  .footer {{ margin-top: 30px; font-size: .8rem; color: #999; text-align: center; }}
  @media (max-width: 600px) {{
    .cards {{ grid-template-columns: 1fr; }}
  }}

  /* TOC sidebar */
  .layout {{ display: flex; gap: 24px; max-width: 1400px; margin: 0 auto; }}
  nav#toc {{
    width: 240px; flex-shrink: 0;
    background: #fff; border-radius: 12px; padding: 16px;
    position: sticky; top: 20px; max-height: calc(100vh - 40px); overflow-y: auto;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }}
  nav#toc h2 {{ font-size: .85rem; color: #888; margin-bottom: 12px; text-transform: uppercase; letter-spacing: .04em; }}
  nav#toc a {{ display: block; font-size: .82rem; color: #555; text-decoration: none; padding: 4px 0; }}
  nav#toc a:hover {{ color: #2563eb; }}
  nav#toc .toc-score {{ float: right; font-weight: 700; font-size: .75rem; }}
  .main-content {{ flex: 1; min-width: 0; }}
  @media (max-width: 900px) {{
    .layout {{ flex-direction: column; }}
    nav#toc {{ width: auto; max-height: none; position: static; }}
  }}
</style>
</head>
<body>

<div class="layout">
<nav id="toc">
  <h2>📋 目录</h2>
  <div style="margin-bottom:12px;font-size:.78rem;color:#888;">
    共 {len(scored)} 套房源 · 评分排序
  </div>
  {''.join(f'<div><a href="#card-{i}">{s.listing.title or s.listing.address[:30]}<span class="toc-score" style="color:{"#22c55e" if s.total_score>=7 else "#eab308" if s.total_score>=4 else "#ef4444"}">{s.total_score}</span></a></div>' for i, s in enumerate(scored[:40]))}
  {'' if len(scored) <= 40 else '<div style="color:#888;font-size:.75rem;margin-top:8px">... 还有 ' + str(len(scored)-40) + ' 套</div>'}
  <div style="margin-top:16px;padding-top:12px;border-top:1px solid #eee;">
    <a href="#details-table" style="font-size:.82rem;">📊 详细评分表</a>
  </div>
</nav>

<div class="main-content">
  <div class="header">
    <h1>🏠 瑞典房源日报</h1>
    <div class="meta">AI 评分 · {now}</div>
    <div class="criteria">{criteria_text}</div>
  </div>

  <div class="summary-bar">
    <div class="summary-item"><div class="num" style="color:#22c55e">{len(high)}</div><div class="label">推荐</div></div>
    <div class="summary-item"><div class="num" style="color:#eab308">{len(mid)}</div><div class="label">可考虑</div></div>
    <div class="summary-item"><div class="num" style="color:#ef4444">{len(low)}</div><div class="label">不推荐</div></div>
    <div class="summary-item"><div class="num">{len(scored)}</div><div class="label">总计</div></div>
  </div>

  {cards_html}

  <div class="details-section" id="details-table">
    <h2>📊 详细评分表</h2>
    <table>
      <thead>
        <tr><th>房源</th><th>总分</th><th>性价比</th><th>安全性</th><th>楼层</th><th>户型</th><th>潜力</th><th>评语</th></tr>
      </thead>
      <tbody>
        {details_rows}
      </tbody>
    </table>
  </div>

  <p class="footer">由 daily-digest-bot · Booli Playwright 抓取 · DeepSeek 评分 · {now}</p>
</div>
</div>

</body>
</html>"""

    Path(path).write_text(html, encoding="utf-8")


def _write_simple_html(listings: list, path: str) -> None:
    """Dry-run 模式输出简单 HTML。"""
    from .fetch_booli import _render_html
    html = _render_html(listings)
    Path(path).write_text(html, encoding="utf-8")
    print(f"简单结果已保存到 {path}（dry-run，无 AI 评分）")


def _print_summary(scored: list[ScoredListing]) -> None:
    """终端文字输出。"""
    print(f"\n{'='*60}")
    print(f"  瑞典房源评分结果（共 {len(scored)} 套）")
    print(f"{'='*60}")
    for i, s in enumerate(scored, 1):
        l = s.listing
        price_str = f"{l.price:,} kr".replace(",", " ") if l.price else "? kr"
        score_str = f"⭐ {s.total_score}/10"
        print(f"\n  {i:2d}. {score_str}  {l.title or l.address}")
        print(f"      {price_str} | {l.rooms} rum | {l.listing_type}")
        if s.verdict_zh:
            print(f"      💬 {s.verdict_zh}")
        if s.floor_issue:
            print(f"      ⚠️  {s.floor_issue}")
        if s.safety_concern:
            print(f"      🔒 {s.safety_concern}")


# ======================================================================
# CLI
# ======================================================================


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="housing_digest",
        description="瑞典房源搜索 + AI 筛选评分。所有搜索条件在 SEARCH_CONFIG 中修改。",
    )
    parser.add_argument("--output", "-o", default="housing_digest.html",
                        help="输出 HTML 文件路径（默认 housing_digest.html）")
    parser.add_argument("--visible", action="store_true",
                        help="有头模式（调试 Playwright）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只抓取不过 AI，用于测试搜索条件")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="详细日志")
    parser.add_argument("--city", help="临时覆盖搜索城市")
    parser.add_argument("--max-price", type=int, help="临时覆盖价格上限")
    parser.add_argument("--min-rooms", type=float, help="临时覆盖最少房间数")
    return parser


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = _build_cli()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    # 允许命令行覆盖搜索配置
    overrides = {}
    if args.city:
        overrides["q"] = args.city
    if args.max_price:
        overrides["max_price"] = args.max_price
    if args.min_rooms:
        overrides["min_rooms"] = args.min_rooms

    asyncio.run(run_pipeline(
        search_config=overrides if overrides else None,
        headless=not args.visible,
        dry_run=args.dry_run,
        output=args.output,
    ))


if __name__ == "__main__":
    main()
