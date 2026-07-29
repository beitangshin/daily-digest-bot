#!/usr/bin/env python3
"""瑞典房源搜索 + AI 筛选评分流水线。

模块化设计，每部分可独立修改：

  1. SEARCH_CONFIG      — 搜索条件（城市/价格/房型/房间数）
  2. HARD_FILTERS        — 硬性过滤（楼层/价格上限/房间数下限）
  3. AI_SCORING          — DeepSeek 评分 prompt + 解析
  4. SAFETY_CHECKER      — 街区安全搜索（联网）
  5. LISTINGS_DB         — 持久化数据库（全量跟踪，去重+变更检测）
  6. RENDERER            — 生成 HTML 简报

跟新闻管道的核心区别：房源是全量跟踪，不是增量。
- 每次抓取全部在售房源
- 对比数据库：新的标 NEW，消失的标 såld
- 属性变化的重新打分，不变的沿用旧评分（不消耗 API 额度）
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# ======================================================================
# [模块] SEARCH_CONFIG — 搜索条件配置
# 修改这里来调整搜什么，不需要改下方代码
# ======================================================================

SEARCH_CONFIG = {
    # === 搜索条件 ===
    "q": "Stockholm",
    "item_types": ["villa", "bostadsrätt", "lägenhet", "radhus", "parhus"],
    "max_price": 9_500_000,
    "min_price": 500_000,
    "min_rooms": 4,
    "max_rooms": None,
    "pages": 10,
    "sort": "pubdate_desc",

    # === Hemnet 搜索 ===
    "hemnet_enabled": True,

    # === 地域过滤 ===
    "allowed_cities": [
        "Stockholm", "Södermalm", "Östermalm", "Norrmalm", "Vasastan",
        "Kungsholmen", "Gamla stan", "Djurgården", "Liljeholmen",
        "Bromma", "Alvik", "Ekerö", "Drottningholm",
        "Solna", "Sundbyberg", "Råsunda", "Huvudsta",
        "Lidingö", "Täby", "Danderyd", "Djursholm",
        "Sollentuna", "Edsberg", "Helenelund",
        "Upplands Väsby", "Sigtuna", "Märsta",
        "Vallentuna", "Österåker", "Åkersberga",
        "Vaxholm", "Norrtälje", "Värmdö", "Gustavsberg",
        "Nacka", "Saltsjöbaden", "Fisksätra", "Järla Sjö",
        "Tyresö", "Haninge", "Vendelsö",
        "Huddinge", "Flemingsberg", "Segeltorp", "Kungens kurva",
        "Botkyrka", "Tullinge", "Tumba",
        "Salem", "Rönninge", "Järfälla", "Jakobsberg", "Barkarby",
        "Upplands-Bro", "Kungsängen",
    ],
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
        "Södertälje",
    ],
}

# ======================================================================
# [模块] HARD_FILTERS — 硬性过滤规则
# ======================================================================


def _filter_by_price(listing) -> bool:
    cfg = SEARCH_CONFIG
    if listing.price == 0:
        return False
    if cfg["min_price"] and listing.price < cfg["min_price"]:
        return False
    if cfg["max_price"] and listing.price > cfg["max_price"]:
        return False
    return True


def _filter_by_rooms(listing) -> bool:
    cfg = SEARCH_CONFIG
    if listing.rooms is None:
        return True
    if cfg["min_rooms"] and listing.rooms < cfg["min_rooms"]:
        return False
    if cfg.get("max_rooms") and listing.rooms > cfg["max_rooms"]:
        return False
    return True


def _filter_by_type(listing) -> bool:
    allowed = SEARCH_CONFIG["item_types"]
    if not listing.listing_type or not allowed:
        return True
    lt = listing.listing_type.lower()
    for a in allowed:
        if a.lower() in lt or lt in a.lower():
            return True
    return False


def _filter_by_city(listing) -> bool:
    """地理过滤：单词边界匹配，防止 'Bro' 误配 'Hällabrottet'。"""
    allowed = SEARCH_CONFIG.get("allowed_cities", [])
    blocked = SEARCH_CONFIG.get("blocked_cities", [])
    location = ((getattr(listing, 'city', '') or '') + ' ' +
                (getattr(listing, 'address', '') or '') + ' ' +
                (getattr(listing, 'title', '') or '')).lower()
    for b in blocked:
        if re.search(r"\b" + re.escape(b.lower()) + r"\b", location):
            return False
    for a in allowed:
        if re.search(r"\b" + re.escape(a.lower()) + r"\b", location):
            return True
    if re.search(r"\bstockholm\b", location):
        return True
    return False


FILTERS: list[dict] = [
    {"name": "价格范围", "enabled": True, "fn": _filter_by_price},
    {"name": "房间数", "enabled": True, "fn": _filter_by_rooms},
    {"name": "房型", "enabled": True, "fn": _filter_by_type},
    {"name": "地理过滤", "enabled": True, "fn": _filter_by_city},
    {"name": "楼层过滤", "enabled": False, "fn": lambda l: True},
]


def apply_hard_filters(listings: list) -> list:
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
            except Exception:
                new_result.append(l)
        result = new_result
        if before - len(result):
            logger.info("  过滤器 [%s] 淘汰了 %d 套", f["name"], before - len(result))
    return result


# ======================================================================
# [模块] SAFETY_CHECKER
# ======================================================================


async def check_area_safety(areas: list[str]) -> dict[str, str]:
    if not areas:
        return {}
    unique_areas = list(dict.fromkeys(areas))
    logger.info("正在检查 %d 个区域的安全信息...", len(unique_areas))
    safety_info: dict[str, str] = {}
    for area in unique_areas:
        if not area or area == "Stockholms län":
            continue
        try:
            import httpx
            from urllib.parse import urlencode
            async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(f"https://www.google.com/search?{urlencode({'q': f'{area} Stockholm brottslighet trygghet 2024 2025'})}")
                if resp.status_code == 200:
                    snippets = re.findall(r'<div[^>]*class="[^"]*BNeawe[^"]*"[^>]*>(.*?)</div>', resp.text, re.DOTALL)
                    combined = " ".join(snippets[:5])
                    if combined:
                        safety_info[area] = combined[:1000]
        except Exception:
            pass
    return safety_info


# ======================================================================
# [模块] AI_SCORING
# ======================================================================

SCORING_SYSTEM_PROMPT = """你是一名瑞典房产评估专家。你会收到一套房源信息，请对每套房源进行全面评估。

评估维度（每项 1-10 分）：
1. 性价比（25%）：相比该区域市场价是否合理
2. 安全性（30%）：根据你对 Stockholm 各街区治安的了解评估
3. 楼层评价（15%）：底楼 bottenvåning / våning 1 扣分，中间楼层加分
4. 户型质量（15%）：房间布局是否合理、面积是否够用
5. 投资潜力（5%）：升值空间、区域发展前景

总分 1-10，输出 JSON 格式。"""

SCORING_USER_PROMPT_TEMPLATE = """请评估以下 {count} 套房源：

{listings_json}

安全参考信息：
{safety_info}

为每套输出：
- "url": 房源链接
- "total_score": 总分 (1-10)
- "scores": {{"value": 性价比, "safety": 安全性, "floor": 楼层, "layout": 户型, "potential": 投资}}
- "verdict_zh": 一句话中文结论
- "floor_issue": 楼层问题或 null
- "safety_concern": 安全隐患或 null

输出 JSON: {{"items": [...]}}"""


def _build_scoring_input(listings: list) -> str:
    items = [{"index": i, "address": l.title or l.address, "city": getattr(l, 'city', ''),
              "price": f"{l.price:,} kr".replace(",", " "), "rooms": l.rooms,
              "living_area": f"{l.living_area} m²" if l.living_area else "未知",
              "type": l.listing_type or "未知", "url": l.url} for i, l in enumerate(listings)]
    return json.dumps(items, ensure_ascii=False, indent=2)


def _parse_scores(api_response: dict, count: int) -> list[dict]:
    items = api_response.get("items", []) if isinstance(api_response, dict) else []
    results = []
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
# [模块] LISTINGS DB — 持久化房源数据库
# ======================================================================


@dataclass
class ScoredListing:
    listing: Any
    total_score: int = 5
    scores: dict = field(default_factory=dict)
    verdict_zh: str = ""
    floor_issue: str | None = None
    safety_concern: str | None = None
    status: str = "active"  # active | new | updated | removed


def _db_path(output_dir: Path) -> Path:
    return output_dir / ".listings_db.json"


def _listing_key(l) -> str:
    return hashlib.sha1(l.url.encode("utf-8")).hexdigest()[:12]


def _listing_snapshot(l) -> dict:
    return {"price": getattr(l, 'price', 0), "rooms": getattr(l, 'rooms', None),
            "living_area": getattr(l, 'living_area', None), "listing_type": getattr(l, 'listing_type', ''),
            "city": getattr(l, 'city', '')}


def _load_listings_db(db_path: Path) -> dict:
    if db_path.exists():
        try:
            return json.loads(db_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"listings": {}}


def _save_listings_db(db_path: Path, db: dict) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def _dict_to_listing(d: dict) -> Any:
    return SimpleNamespace(**{k: d.get(k, '') for k in ["url", "title", "address", "price", "rooms",
        "living_area", "listing_type", "source", "city", "image_url", "monthly_fee"]})


def _merge_into_db(db: dict, current: list) -> tuple[list[ScoredListing], int, int, int]:
    """全量合并：对比数据库，标记新增/下架/变化。"""
    now = datetime.now().isoformat()
    entries = db.setdefault("listings", {})
    current_keys = {_listing_key(l) for l in current}

    new_urls: set[str] = set()
    changed_keys: set[str] = set()

    for l in current:
        key = _listing_key(l)
        snap = _listing_snapshot(l)
        ex = entries.get(key)
        if ex is None:
            new_urls.add(l.url)
            entries[key] = {"url": l.url, "title": getattr(l, 'title', '') or getattr(l, 'address', ''),
                "address": getattr(l, 'address', ''), **snap, "source": getattr(l, 'source', ''),
                "image_url": getattr(l, 'image_url', None), "monthly_fee": getattr(l, 'monthly_fee', None),
                "first_seen": now, "last_seen": now, "status": "active", "removed_at": None,
                "total_score": 5, "scores": {}, "verdict_zh": "", "floor_issue": None, "safety_concern": None}
        else:
            ex["last_seen"] = now; ex["status"] = "active"; ex["removed_at"] = None
            ex["title"] = getattr(l, 'title', '') or getattr(l, 'address', '')
            old = {k: ex.get(k) for k in snap}
            if snap != old:
                changed_keys.add(key); ex.update(snap)
                ex["total_score"] = 5; ex["scores"] = {}; ex["verdict_zh"] = ""
            ex["image_url"] = getattr(l, 'image_url', ex.get("image_url"))

    removed = 0
    for key, ex in entries.items():
        if ex.get("status") == "removed":
            continue
        if key not in current_keys:
            ex["status"] = "removed"; ex["removed_at"] = now; removed += 1

    merged = []
    for key, ex in entries.items():
        if ex.get("status") == "removed":
            continue
        status = "new" if ex["url"] in new_urls else ("updated" if key in changed_keys else "active")
        merged.append(ScoredListing(listing=_dict_to_listing(ex), total_score=ex.get("total_score", 5),
            scores=ex.get("scores", {}), verdict_zh=ex.get("verdict_zh", ""),
            floor_issue=ex.get("floor_issue"), safety_concern=ex.get("safety_concern"), status=status))

    merged.sort(key=lambda x: x.total_score, reverse=True)
    return merged, len(new_urls), removed, len(changed_keys)


# ======================================================================
# [模块] PIPELINE ORCHESTRATOR
# ======================================================================


async def run_pipeline(*, search_config: dict | None = None, headless: bool = True,
                       dry_run: bool = False, output: str | None = None) -> list[ScoredListing]:
    """全量跟踪流水线：抓取 → 硬过滤 → 对比DB → AI评分（只评新的+变化的）。"""
    cfg = {**SEARCH_CONFIG, **(search_config or {})}

    # === Step 1: 抓取（Booli + Hemnet）===
    logger.info("=" * 50)
    logger.info("Step 1: 抓取（Booli + Hemnet）")
    logger.info("  条件: q=%s  max_price=%s  min_rooms=%s  types=%s",
                cfg.get("q"), cfg["max_price"], cfg["min_rooms"], cfg["item_types"])
    logger.info("=" * 50)

    all_l: list = []

    from .fetch_booli import BooliScraper, _build_search_url as b_url
    try:
        async with BooliScraper(headless=headless, max_pages=cfg["pages"]) as s:
            for l in await s.search(search_url=b_url(q=cfg["q"], item_types=cfg["item_types"],
                    max_price=cfg["max_price"], min_price=cfg.get("min_price"), sort=cfg.get("sort", "pubdate_desc"))):
                l.source = "booli"; all_l.append(l)
        logger.info("  Booli: %d", sum(1 for l in all_l if l.source == "booli"))
    except Exception as e:
        logger.warning("  Booli 失败: %s", e)

    if cfg.get("hemnet_enabled", True):
        from .fetch_playwright import HemnetScraper, _build_search_url as h_url
        try:
            async with HemnetScraper(headless=headless, max_pages=cfg["pages"]) as s:
                for l in await s.search(search_url=h_url(q=cfg.get("q") or "Stockholm",
                        item_types=cfg["item_types"], max_price=cfg["max_price"],
                        min_price=cfg.get("min_price"), min_rooms=cfg["min_rooms"], sort="publication_time_desc")):
                    l.source = "hemnet"; all_l.append(l)
            logger.info("  Hemnet: %d", sum(1 for l in all_l if l.source == "hemnet"))
        except Exception as e:
            logger.warning("  Hemnet 失败: %s", e)

    seen, uniq = set(), []
    for l in all_l:
        if l.url not in seen:
            seen.add(l.url); uniq.append(l)
    all_l = uniq
    logger.info("  去重后: %d 套", len(all_l))
    if not all_l:
        logger.warning("无房源"); return []

    # === Step 2: 硬过滤 ===
    logger.info("Step 2: 硬过滤")
    filtered = apply_hard_filters(all_l)
    logger.info("  过滤后: %d 套", len(filtered))
    if not filtered:
        logger.warning("无剩余房源"); return []

    # === Step 3: 对比数据库 ===
    logger.info("Step 3: 对比数据库")
    out_path = Path(output) if output else Path("output/housing/latest.html")
    db = _load_listings_db(_db_path(out_path.parent))
    prev = len(db.get("listings", {}))
    merged, new_n, rem_n, chg_n = _merge_into_db(db, filtered)
    logger.info("  库原有 %d · 新增 %d · 变化 %d · 下架 %d · 活跃 %d", prev, new_n, chg_n, rem_n, len(merged))

    if dry_run:
        print(f"\n[Dry Run] 抓取 {len(all_l)} 过滤 {len(filtered)} 新增 {new_n} 下架 {rem_n} 变化 {chg_n}")
        for i, m in enumerate(merged[:10], 1):
            tag = {"new": "🆕", "updated": "📝", "active": "  "}.get(m.status, "  ")
            print(f"  {tag} {i}. {m.listing.title or m.listing.address} — {m.listing.price:,} kr — {m.listing.rooms} rum")
        if len(merged) > 10: print(f"  ... 还有 {len(merged)-10} 套")
        if output: _write_simple_html([m.listing for m in merged], output)
        return []

    # === Step 4: AI 评分（只评新+变化的）===
    from .config import load_settings
    settings = load_settings()
    if not settings.has_api_key:
        raise SystemExit("DEEPSEEK_API_KEY 未配置")

    need_score = {m.listing.url for m in merged if m.status in ("new", "updated")}
    if need_score:
        logger.info("Step 4: AI 评分（%d 套）", len(need_score))
        areas = list({getattr(l, 'city', '') for l in filtered if l.url in need_score and getattr(l, 'city', '')})
        safety_text = "无特殊安全信息"
        if areas:
            si = await check_area_safety(areas)
            safety_text = "\n".join(f"- {k}: {v[:200]}" for k, v in si.items()) or safety_text
        from .llm import make_chat_fn
        chat_fn = make_chat_fn(settings)
        to_score = [l for l in filtered if l.url in need_score]
        scored_map: dict[str, ScoredListing] = {}
        for start in range(0, len(to_score), 15):
            batch = to_score[start:start+15]
            try:
                r = chat_fn(SCORING_SYSTEM_PROMPT, SCORING_USER_PROMPT_TEMPLATE.format(
                    count=len(batch), listings_json=_build_scoring_input(batch), safety_info=safety_text), 4000)
                for si in _parse_scores(r, len(batch)):
                    scored_map[si["url"]] = si
            except Exception as e:
                logger.warning("AI 评分失败: %s", e)
        for key, ex in db.get("listings", {}).items():
            u = ex.get("url", "")
            if u in scored_map:
                ex.update({k: scored_map[u].get(k) for k in ("total_score", "scores", "verdict_zh", "floor_issue", "safety_concern")})
        merged, _, _, _ = _merge_into_db(db, filtered)

    # === Step 5: 保存 + HTML ===
    _save_listings_db(_db_path(out_path.parent), db)
    if output:
        _write_html(merged, output, cfg)
        print(f"✓ 已保存: {output}")
        d = datetime.now().strftime("%Y-%m-%d")
        hd = Path(output).parent / d
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "digest.html").write_text(Path(output).read_text(encoding="utf-8"), encoding="utf-8")
        (hd / "meta.json").write_text(json.dumps({"date_str": d, "article_count": len(merged), "source_count": 2}, ensure_ascii=False), encoding="utf-8")
        try:
            from .render_html import render_combined_index
            from .channels import load_channels
            (Path(output).parents[1] / "index.html").write_text(render_combined_index(Path(output).parents[1], load_channels()), encoding="utf-8")
        except Exception:
            pass
    return merged


# ======================================================================
# [模块] RENDERER
# ======================================================================


def _write_html(scored: list[ScoredListing], path: str, cfg: dict) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    high = [s for s in scored if s.total_score >= 7]
    mid = [s for s in scored if 4 <= s.total_score <= 6]
    low = [s for s in scored if s.total_score <= 3]

    cards = ""
    for title, grp, cls in [(f"🏆 推荐 ({len(high)})", high, "high"),
                            (f"📋 可考虑 ({len(mid)})", mid, "mid"),
                            (f"⚠️ 不推荐 ({len(low)})", low, "low")]:
        if not grp:
            continue
        cards += f"<h2 class='group-title {cls}'>{title}</h2><div class='cards'>"
        for item in grp:
            l = item.listing
            price_s = f"{l.price:,} kr".replace(",", " ") if l.price else ""
            tags = " ".join(filter(None, [
                f"<span class='tag'>{l.rooms} rum</span>" if l.rooms else "",
                f"<span class='tag'>{l.living_area:.0f} m²</span>" if l.living_area else "",
                f"<span class='tag type-tag'>🏠 {l.listing_type}</span>" if l.listing_type else "",
            ]))
            sc = item.total_score
            sc_c = "#22c55e" if sc >= 7 else "#eab308" if sc >= 4 else "#ef4444"
            status_tag = ""
            if item.status == "new":
                status_tag = "<span class='status-tag status-new'>🆕 NY</span>"
            elif item.status == "updated":
                status_tag = "<span class='status-tag status-updated'>📝 Ändrad</span>"
            floor_warn = f"<p class='warn floor-warn'>⚠️ {item.floor_issue}</p>" if item.floor_issue else ""
            safety_warn = f"<p class='warn safety-warn'>🔒 {item.safety_concern}</p>" if item.safety_concern else ""
            img = f"<img src='{l.image_url}' alt=''>" if l.image_url else "<div class='no-img'>📷</div>"

            cards += f"""
            <div class='card'>
                <div class='card-img'>{img}</div>
                <div class='card-body'>
                    <div class='score-badge' style='background:{sc_c}'>{sc}</div>
                    {status_tag}
                    <h2><a href='{l.url}' target='_blank'>{l.title or l.address}</a></h2>
                    <p class='price'>{price_s}</p>
                    <div class='meta'>{tags}</div>
                    {floor_warn}{safety_warn}
                    {f"<p class='verdict'>{item.verdict_zh}</p>" if item.verdict_zh else ""}
                </div>
            </div>"""
        cards += "</div>"

    details = ""
    for s in scored:
        if s.scores:
            sc = s.scores
            details += f"<tr><td><a href='{s.listing.url}'>{s.listing.title or s.listing.address}</a></td><td><strong>{s.total_score}</strong></td><td>{sc.get('value','-')}</td><td>{sc.get('safety','-')}</td><td>{sc.get('floor','-')}</td><td>{sc.get('layout','-')}</td><td>{sc.get('potential','-')}</td><td style='font-size:12px;color:#666'>{s.verdict_zh}</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>瑞典房源日报 · AI 评分 · {now[:10]}</title>
<style>
  * {{margin:0;padding:0;box-sizing:border-box}}
  body {{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#f5f5f5;color:#222;padding:20px}}
  .layout {{display:flex;gap:24px;max-width:1400px;margin:0 auto}}
  nav#toc {{width:240px;flex-shrink:0;background:#fff;border-radius:12px;padding:16px;position:sticky;top:20px;max-height:calc(100vh-40px);overflow-y:auto;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  nav#toc h2 {{font-size:.85rem;color:#888;margin-bottom:12px;text-transform:uppercase;letter-spacing:.04em}}
  nav#toc a {{display:block;font-size:.82rem;color:#555;text-decoration:none;padding:4px 0}}
  nav#toc a:hover {{color:#2563eb}}
  .main-content {{flex:1;min-width:0}}
  .header h1 {{font-size:1.5rem;margin-bottom:4px}}
  .header .criteria {{font-size:.85rem;color:#555;margin-top:8px;padding:8px 12px;background:#fff;border-radius:8px;display:inline-block}}
  .summary-bar {{display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap}}
  .summary-item {{background:#fff;border-radius:10px;padding:12px 20px;flex:1;min-width:100px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  .summary-item .num {{font-size:1.8rem;font-weight:700}}
  .summary-item .label {{font-size:.8rem;color:#888}}
  .group-title {{font-size:1.2rem;margin:24px 0 12px;padding-bottom:6px;border-bottom:2px solid #ddd}}
  .group-title.high {{border-color:#22c55e}}
  .group-title.mid {{border-color:#eab308}}
  .group-title.low {{border-color:#ef4444}}
  .cards {{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;margin-bottom:24px}}
  .card {{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);position:relative}}
  .card:hover {{box-shadow:0 4px 16px rgba(0,0,0,.12)}}
  .card-img {{width:100%;height:180px;overflow:hidden;background:#e0e0e0}}
  .card-img img {{width:100%;height:100%;object-fit:cover}}
  .no-img {{display:flex;align-items:center;justify-content:center;height:100%;font-size:3rem;color:#aaa}}
  .card-body {{padding:14px;position:relative}}
  .score-badge {{position:absolute;top:-40px;right:12px;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:1rem;color:#fff;box-shadow:0 2px 6px rgba(0,0,0,.2)}}
  .status-tag {{position:absolute;top:8px;left:8px;padding:3px 8px;border-radius:4px;font-size:.7rem;font-weight:700;z-index:2}}
  .status-new {{background:#22c55e;color:#fff}}
  .status-updated {{background:#f59e0b;color:#fff}}
  .card-body h2 {{font-size:.95rem;line-height:1.4;margin-bottom:2px;padding-right:40px}}
  .card-body h2 a {{color:#222;text-decoration:none}}
  .price {{font-size:1.15rem;font-weight:700;color:#db4c3f;margin-bottom:8px}}
  .meta {{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}}
  .tag {{display:inline-block;font-size:.75rem;padding:2px 8px;border-radius:4px;background:#f0f0f0;color:#555}}
  .type-tag {{background:#e8f0fe;color:#1967d2}}
  .warn {{font-size:.78rem;padding:4px 8px;border-radius:4px;margin-top:4px}}
  .floor-warn {{background:#fff3cd;color:#856404}}
  .safety-warn {{background:#f8d7da;color:#721c24}}
  .verdict {{font-size:.82rem;color:#444;margin-top:6px;font-style:italic}}
  .details-section {{margin-top:32px;background:#fff;border-radius:12px;padding:20px;overflow-x:auto}}
  table {{width:100%;border-collapse:collapse;font-size:.82rem}}
  th,td {{padding:8px 10px;text-align:left;border-bottom:1px solid #eee}}
  th {{background:#f8fafc;font-weight:600;color:#555;position:sticky;top:0}}
  .footer {{margin-top:30px;font-size:.8rem;color:#999;text-align:center}}
  @media (max-width:900px) {{.layout {{flex-direction:column}} nav#toc {{width:auto;max-height:none;position:static}}}}
</style></head><body>
<div class="layout">
<nav id="toc"><h2>📋 目录</h2><div style="margin-bottom:12px;font-size:.78rem;color:#888">{len(scored)} 套 · 评分排序</div>
{''.join(f'<a href="#card-{i}">{s.listing.title or s.listing.address[:30]}<span class="toc-score" style="float:right;font-weight:700;font-size:.75rem;color:{"#22c55e" if s.total_score>=7 else "#eab308" if s.total_score>=4 else "#ef4444"}">{s.total_score}</span></a>' for i,s in enumerate(scored[:50]))}
<div style="margin-top:16px;padding-top:12px;border-top:1px solid #eee"><a href="#details-table">📊 详细评分表</a></div></nav>
<div class="main-content">
<div class="header"><h1>🏠 瑞典房源日报</h1><div class="criteria">📍 Stockholm · ≤ {cfg['max_price']:,} kr · ≥ {cfg['min_rooms']} rum · {'/'.join(cfg['item_types'])}</div></div>
<div class="summary-bar">
<div class="summary-item"><div class="num" style="color:#22c55e">{len(high)}</div><div class="label">推荐</div></div>
<div class="summary-item"><div class="num" style="color:#eab308">{len(mid)}</div><div class="label">可考虑</div></div>
<div class="summary-item"><div class="num" style="color:#ef4444">{len(low)}</div><div class="label">不推荐</div></div>
<div class="summary-item"><div class="num">{len(scored)}</div><div class="label">活跃房源</div></div>
</div>
{cards}
<div class="details-section" id="details-table"><h2>📊 详细评分表</h2>
<table><thead><tr><th>房源</th><th>总分</th><th>性价比</th><th>安全性</th><th>楼层</th><th>户型</th><th>潜力</th><th>评语</th></tr></thead><tbody>{details}</tbody></table></div>
<p class="footer">daily-digest-bot · Booli+Hemnet · DeepSeek 评分 · {now}</p>
</div></div></body></html>"""
    Path(path).write_text(html, encoding="utf-8")


def _write_simple_html(listings: list, path: str) -> None:
    from .fetch_booli import _render_html as booli_html
    Path(path).write_text(booli_html(listings), encoding="utf-8")


def _print_summary(scored: list[ScoredListing]) -> None:
    print(f"\n{'='*60}\n  瑞典房源（{len(scored)} 套）\n{'='*60}")
    for i, s in enumerate(scored, 1):
        l = s.listing
        tag = {"new": "🆕", "updated": "📝", "active": "  "}.get(s.status, "")
        print(f"\n  {i:2d}. {tag} ⭐ {s.total_score}/10  {l.title or l.address}")
        print(f"      {l.price:,} kr | {l.rooms} rum | {l.listing_type}".replace(",", " "))
        if s.verdict_zh: print(f"      💬 {s.verdict_zh}")


# ======================================================================
# CLI
# ======================================================================


def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="housing_digest", description="瑞典房源搜索 + AI 评分")
    p.add_argument("--output", "-o", default="output/housing/latest.html")
    p.add_argument("--visible", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--city")
    p.add_argument("--max-price", type=int)
    p.add_argument("--min-rooms", type=float)
    return p


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = _build_cli().parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s: %(message)s")
    overrides = {}
    if args.city: overrides["q"] = args.city
    if args.max_price: overrides["max_price"] = args.max_price
    if args.min_rooms: overrides["min_rooms"] = args.min_rooms
    asyncio.run(run_pipeline(search_config=overrides or None, headless=not args.visible,
                             dry_run=args.dry_run, output=args.output))


if __name__ == "__main__":
    main()
