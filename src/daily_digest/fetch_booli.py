"""Playwright 无头浏览器 Booli 房源搜索。

用法：
    python -m daily_digest.fetch_booli --city Stockholm --max-price 5000000
    python -m daily_digest.fetch_booli --url "https://www.booli.se/sok/till-salu?q=Uppsala&typ=villa"
    python -m daily_digest.fetch_booli --city Göteborg --item-types villa --html -o booli.html --open

集成到频道：
    daily-digest sources add --channel housing --name "Booli - Stockholm" \
      --url "https://www.booli.se/sok/till-salu?q=Stockholm&sort=pubdate_desc" \
      --type booli
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BooliListing:
    title: str
    address: str
    price: int
    url: str
    rooms: float | None = None
    living_area: float | None = None  # m²
    land_area: float | None = None
    monthly_fee: int | None = None  # kr/month (avgift)
    price_per_sqm: int | None = None
    listing_type: str = ""
    image_url: str | None = None
    published_at: datetime | None = None
    city: str = ""
    brokerage: str | None = None
    source: str = "booli"


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

SEARCH_BASE = "https://www.booli.se/sok/till-salu"

BOOLI_TYPES = {
    "villa": "villa",
    "bostadsratt": "bostadsrätt",
    "bostadsrätt": "bostadsrätt",
    "lagenhet": "lägenhet",
    "lägenhet": "lägenhet",
    "fritidshus": "fritidshus",
    "radhus": "radhus",
    "parhus": "parhus",
    "tomt": "tomt",
    "gård": "gård",
}


def _build_search_url(
    *,
    q: str | None = None,
    city: str | None = None,
    item_types: list[str] | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rooms: float | None = None,
    max_rooms: float | None = None,
    sort: str = "pubdate_desc",
) -> str:
    from urllib.parse import urlencode

    params: dict[str, str] = {}
    query = q or city or ""
    if query:
        params["q"] = query
    if item_types:
        params["typ"] = ",".join(BOOLI_TYPES.get(t.lower(), t.lower()) for t in item_types)
    if min_price is not None:
        params["pris_min"] = str(min_price)
    if max_price is not None:
        params["pris_max"] = str(max_price)
    if min_rooms is not None:
        params["rum_min"] = str(min_rooms)
    if max_rooms is not None:
        params["rum_max"] = str(max_rooms)
    if sort:
        params["sort"] = sort

    return SEARCH_BASE + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_price(text: str) -> int | None:
    if not text or "ej angivet" in text.lower():
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        val = int(cleaned)
        return val if 10000 < val < 100_000_000 else None
    except ValueError:
        return None


def _parse_float(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace(" ", "").replace(",", ".").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return int(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class BooliScraper:
    """Async context manager that wraps Playwright for Booli.se scraping."""

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 30_000,
        max_pages: int = 3,
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_pages = max_pages
        self._browser = None
        self._page = None

    async def __aenter__(self) -> BooliScraper:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="sv-SE",
            timezone_id="Europe/Stockholm",
            screen={"width": 1920, "height": 1080},
            no_viewport=False,
            permissions=["geolocation"],
            geolocation={"latitude": 59.3293, "longitude": 18.0686},
        )
        self._page = await context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

        # Stealth: 全方位指纹伪装
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['sv-SE', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin' })),
            });
            window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            const gp = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {
                if (p === 37445) return 'Intel Inc.';
                if (p === 37446) return 'Intel Iris OpenGL Engine';
                return gp(p);
            };
        """)

        # 预设 Booli cookie 避免弹窗
        await context.add_cookies([
            {"name": "cookie_consent", "value": "1", "domain": ".booli.se", "path": "/"},
        ])
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    _CLOUDFLARE_KEYWORDS = ["just a moment", "checking your browser", "cf-challenge", "cloudflare"]

    async def _is_cloudflare_blocked(self) -> bool:
        title = (await self._page.title()).lower()
        body = await self._page.evaluate("document.body?.innerText?.substring(0, 500) || ''")
        for kw in self._CLOUDFLARE_KEYWORDS:
            if kw in title or kw in body.lower():
                return True
        return False

    async def _retry_on_cloudflare(self, url: str, max_retries: int = 3) -> bool:
        for attempt in range(1, max_retries + 1):
            if await self._is_cloudflare_blocked():
                logger.warning("  Cloudflare 拦截（尝试 %d/%d），重新加载...", attempt, max_retries)
                await asyncio.sleep(2 * attempt)
                await self._page.goto(url, wait_until="domcontentloaded")
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                await self._dismiss_cookies()
                await asyncio.sleep(1)
            else:
                return True
        return False

    async def _dismiss_cookies(self) -> None:
        try:
            # Booli uses a truncated cookie dialog with "Godkänn" (not "alla")
            btn = self._page.locator(
                'button:has-text("Godkänn"), '
                'button:has-text("Acceptera alla"), '
                'button:has-text("Accept all")'
            )
            if await btn.count() > 0:
                await btn.first.click(timeout=5000)
                await asyncio.sleep(1)
                logger.info("dismissed cookie banner")
        except Exception:
            pass

    async def _extract_listings(self) -> list[BooliListing]:
        """Extract listing cards from the current Booli search page."""
        # Booli listing cards — find via links to /annons/
        cards = self._page.locator("a[href*='/annons/']").locator("..")
        count = await cards.count()
        if count == 0:
            logger.warning("no listing cards found on Booli page")
            return []

        BASE_URL = "https://www.booli.se"
        results: list[BooliListing] = []
        for i in range(count):
            try:
                card = cards.nth(i)
                text = await card.inner_text()
                link_el = card.locator("a[href*='/annons/']")
                href = await link_el.first.get_attribute("href")
                if not href:
                    continue

                url = href if href.startswith("http") else BASE_URL + href

                # Get image
                img = card.locator("img").first
                has_img = await img.count() > 0
                image_url = None
                if has_img:
                    image_url = await img.get_attribute("src") or await img.get_attribute("data-src") or None

                lines = [line.strip() for line in text.split("\n") if line.strip()]

                # Parse fields from text
                address = ""
                price = 0
                rooms: float | None = None
                living_area: float | None = None
                monthly_fee: int | None = None
                listing_type = ""
                city_area = ""

                for line in lines:
                    # Price
                    if "kr" in line and "mån" not in line and "ej angivet" not in line.lower():
                        parsed = _parse_price(line)
                        if parsed:
                            price = parsed
                    # Rooms
                    m = re.search(r"(\d+[.,]?\d*)\s*(?:rum|rok)", line, re.IGNORECASE)
                    if m and rooms is None:
                        rooms = _parse_float(m.group(1))
                    # Area
                    m = re.search(r"(\d+)\s*m²", line)
                    if m and living_area is None:
                        va = _parse_float(m.group(1))
                        if va and va >= 8:  # sanity
                            living_area = va
                    # Monthly fee
                    m = re.search(r"(\d[\d\s]*)\s*kr/mån", line)
                    if m and monthly_fee is None:
                        monthly_fee = _parse_int(m.group(1))

                # Address and location parsing:
                # Booli card format:
                #   [Viewing date or "Snart till salu"]
                #   Spara
                #   [Street address]              ← address
                #   [Street address again]
                #   [Type] · [Area] · [City]      ← location line (contains ·)
                #   [Price]
                #   [m²] [rum] [vån] [fee]
                #   [Features like Balkong, Hiss...]
                address = ""
                city_area = ""
                content_lines = [l for l in lines if not re.match(r"^(Spara|Snart|Idag|Imorgon|Mån|Tis|Ons|Tor|Fre|Lör|Sön)", l)]
                for j, line in enumerate(content_lines):
                    if "·" in line and any(t in line.lower() for t in ["villa", "lägenhet", "bostadsrätt", "radhus", "fritidshus", "parhus", "gård", "tomt", "hus"]):
                        city_area = line
                        if j > 0:
                            address = content_lines[j - 1]  # line before location is usually the street address
                        break
                    elif not address and line.strip():
                        # First content line that's not metadata could be address
                        if not any(kw in line.lower() for kw in ["kr", "m²", "rum", "vån", "·"]):
                            address = line

                # Listing type from the type area or URL
                if not listing_type:
                    if "/villa-" in url or "typ=villa" in url:
                        listing_type = "villa"
                    elif "/bostadsratt-" in url or "typ=bostadsrätt" in url:
                        listing_type = "bostadsrätt"
                    elif "/lagenhet-" in url:
                        listing_type = "bostadsrätt"
                    elif "/radhus-" in url:
                        listing_type = "radhus"
                    elif "/fritidshus-" in url:
                        listing_type = "fritidshus"
                    else:
                        # Try from "Lägenhet · ..." pattern in text
                        for line in lines:
                            for btype in ["villa", "bostadsrätt", "bostadsratt", "lägenhet", "lagenhet",
                                          "radhus", "fritidshus", "tomt", "parhus", "gård"]:
                                if btype in line.lower():
                                    listing_type = BOOLI_TYPES.get(btype.replace("ä", "a").replace("ö", "o"), btype)
                                    break
                            if listing_type:
                                break

                # Price per sqm
                price_per_sqm = None
                if price and living_area and living_area > 0:
                    price_per_sqm = int(price / living_area)

                # Parse city from address line if possible
                city = ""
                if city_area and "·" in city_area:
                    parts = city_area.split("·")
                    if len(parts) >= 2:
                        city = parts[-1].strip()
                if not city and city_area:
                    city = city_area

                listing = BooliListing(
                    title=address or url.split("/")[-1],
                    address=address.strip() if address else "",
                    price=price,
                    url=url,
                    rooms=rooms,
                    living_area=living_area,
                    monthly_fee=monthly_fee,
                    price_per_sqm=price_per_sqm,
                    listing_type=listing_type,
                    image_url=image_url,
                    city=city,
                )
                results.append(listing)
            except Exception as exc:
                logger.debug("failed to parse Booli card %d: %s", i, exc)
                continue

        return results

    async def _has_next_page(self) -> bool:
        # Booli uses ?page=N for pagination — check if next page link exists
        next_link = self._page.locator('a:has-text("Nästa sida"), a:has-text("Nästa")')
        return await next_link.count() > 0

    async def _go_next_page(self) -> bool:
        # Booli pagination uses direct page URLs: /sok/till-salu?page=N
        # Extract the href and navigate instead of clicking
        next_link = self._page.locator('a:has-text("Nästa sida"), a:has-text("Nästa")')
        if await next_link.count() == 0:
            return False
        try:
            href = await next_link.first.get_attribute("href")
            if href:
                full_url = "https://www.booli.se" + (href if href.startswith("/") else "/" + href)
                logger.info("  navigating to next page: %s", full_url)
                await self._page.goto(full_url, wait_until="domcontentloaded")
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                # Dismiss cookie again if needed
                await self._dismiss_cookies()
                await asyncio.sleep(1)
                return True
        except Exception as exc:
            logger.debug("pagination failed: %s", exc)
        return False

    async def search(
        self,
        search_url: str | None = None,
        *,
        q: str | None = None,
        city: str | None = None,
        item_types: list[str] | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        min_rooms: float | None = None,
        max_rooms: float | None = None,
        max_pages: int | None = None,
        sort: str = "pubdate_desc",
    ) -> list[BooliListing]:
        """Search Booli for listings.

        Args:
            search_url: Full Booli search URL (takes precedence).
            q: Free-text query.
            city: City name.
            item_types: Property type(s).
            min_price / max_price: Price range.
            min_rooms / max_rooms: Room count range.
            max_pages: Pages to scrape (default: self.max_pages).
            sort: Sort order ("pubdate_desc" = newest first).
        """
        if not search_url:
            search_url = _build_search_url(
                q=q, city=city, item_types=item_types,
                min_price=min_price, max_price=max_price,
                min_rooms=min_rooms, max_rooms=max_rooms,
                sort=sort,
            )

        pages = max_pages or self.max_pages
        logger.info("navigating to: %s", search_url)
        await self._page.goto(search_url, wait_until="domcontentloaded")
        await self._dismiss_cookies()
        try:
            await self._page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # 检测 Cloudflare 并重试
        if not await self._retry_on_cloudflare(search_url):
            logger.warning("Cloudflare 拦截超过最大重试次数，返回空结果")
            return []

        all_listings: list[BooliListing] = []
        for page_num in range(1, pages + 1):
            logger.info("scraping page %d/%d", page_num, pages)
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)

            listings = await self._extract_listings()
            logger.info("  found %d listing(s) on page %d", len(listings), page_num)
            all_listings.extend(listings)

            if page_num < pages and await self._has_next_page():
                if not await self._go_next_page():
                    break
                await asyncio.sleep(2)
            else:
                break

        # Deduplicate by URL
        seen: set[str] = set()
        unique: list[BooliListing] = []
        for l in all_listings:
            if l.url in seen:
                continue
            seen.add(l.url)
            unique.append(l)

        logger.info("total unique listings: %d", len(unique))
        return unique


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------


def _render_html(listings: list[BooliListing]) -> str:
    cards_html = ""
    for listing in listings:
        price_str = f"{listing.price:,} kr".replace(",", " ") if listing.price else ""
        rooms_str = f"{listing.rooms} rum" if listing.rooms else ""
        area_str = f"{listing.living_area:.0f} m²" if listing.living_area else ""
        fee_str = f"{listing.monthly_fee:,} kr/mån".replace(",", " ") if listing.monthly_fee else ""

        meta_parts = []
        if rooms_str:
            meta_parts.append(f"<span class='tag'>{rooms_str}</span>")
        if area_str:
            meta_parts.append(f"<span class='tag'>{area_str}</span>")
        if listing.listing_type:
            t = {"villa": "🏠 Villa", "bostadsrätt": "🏢 BRF", "radhus": "🏘️ Radhus", "fritidshus": "🌲 Fritidshus", "tomt": "🌿 Tomt", "gård": "🏡 Gård", "lägenhet": "🏢 BRF"}.get(listing.listing_type, listing.listing_type)
            meta_parts.append(f"<span class='tag type-tag'>{t}</span>")
        if fee_str:
            meta_parts.append(f"<span class='tag fee-tag'>{fee_str}</span>")
        meta_html = " ".join(meta_parts)

        img_html = f"<img src='{listing.image_url}' alt='' loading='lazy'>" if listing.image_url else "<div class='no-img'>📷</div>"
        city_html = f"<span class='city'>{listing.city}</span>" if listing.city else ""

        cards_html += f"""
        <div class='card'>
            <div class='card-img'>{img_html}</div>
            <div class='card-body'>
                <h2><a href='{listing.url}' target='_blank'>{listing.address}</a></h2>
                <p class='city-line'>{city_html}</p>
                <p class='price'>{price_str}</p>
                <div class='meta'>{meta_html}</div>
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Booli 房源 ({len(listings)} 套)</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #222; padding: 20px; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 20px; color: #444; }}
  h1 span {{ font-weight: 400; color: #888; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }}
  .card {{ background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,.12); }}
  .card-img {{ width: 100%; height: 180px; overflow: hidden; background: #e0e0e0; }}
  .card-img img {{ width: 100%; height: 100%; object-fit: cover; }}
  .no-img {{ display: flex; align-items: center; justify-content: center; height: 100%; font-size: 3rem; color: #aaa; }}
  .card-body {{ padding: 14px; }}
  .card-body h2 {{ font-size: 1rem; line-height: 1.4; margin-bottom: 2px; }}
  .card-body h2 a {{ color: #222; text-decoration: none; }}
  .card-body h2 a:hover {{ color: #db4c3f; }}
  .city-line {{ font-size: .8rem; color: #888; margin-bottom: 6px; }}
  .price {{ font-size: 1.15rem; font-weight: 700; color: #db4c3f; margin-bottom: 8px; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .tag {{ display: inline-block; font-size: .75rem; padding: 2px 8px; border-radius: 4px; background: #f0f0f0; color: #555; }}
  .type-tag {{ background: #e8f0fe; color: #1967d2; }}
  .fee-tag {{ background: #fef7e0; color: #b8860b; }}
  .footer {{ margin-top: 20px; font-size: .8rem; color: #999; text-align: center; }}
</style>
</head>
<body>
<h1>Booli <span>— {len(listings)} 套房源</span></h1>
<div class='cards'>{cards_html}</div>
<p class='footer'>Booli.se · Playwright · {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m daily_digest.fetch_booli",
        description="无头浏览器 Booli 房源搜索（与 Hemnet 一样的方式）",
    )
    parser.add_argument("--url", help="完整的 Booli 搜索结果 URL（覆盖其他参数）")
    parser.add_argument("--q", help="自由搜索词")
    parser.add_argument("--city", help="城市名，如 Stockholm, Uppsala, Göteborg")
    parser.add_argument("--item-types", nargs="*", default=[], help="房型：villa, bostadsrätt, lägenhet ...")
    parser.add_argument("--min-price", type=int, help="最低总价 SEK")
    parser.add_argument("--max-price", type=int, help="最高总价 SEK")
    parser.add_argument("--min-rooms", type=float, help="最少房间数")
    parser.add_argument("--max-rooms", type=float, help="最多房间数")
    parser.add_argument("--pages", type=int, default=3, help="抓取页数（默认 3）")
    parser.add_argument("--output", "-o", help="保存到文件")
    parser.add_argument("--html", action="store_true", help="输出 HTML")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--open", action="store_true", help="保存后自动打开浏览器")
    parser.add_argument("--visible", action="store_true", help="有头模式（调试）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
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
        format="%(asctime)s %(levelname)s %(message)s",
    )

    fmt = "text"
    if args.html or (args.output and args.output.endswith(".html")):
        fmt = "html"
    elif args.json or (args.output and args.output.endswith(".json")):
        fmt = "json"

    async def _run() -> None:
        async with BooliScraper(headless=not args.visible, max_pages=args.pages) as scraper:
            listings = await scraper.search(
                search_url=args.url,
                q=args.q,
                city=args.city,
                item_types=args.item_types or None,
                min_price=args.min_price,
                max_price=args.max_price,
                min_rooms=args.min_rooms,
                max_rooms=args.max_rooms,
            )

        if fmt == "json":
            data = [asdict(l) for l in listings]
            output_str = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        elif fmt == "html":
            output_str = _render_html(listings)
        else:
            lines = [f"找到 {len(listings)} 套房源 (Booli)：\n"]
            for i, l in enumerate(listings, 1):
                price_str = f"{l.price:,} kr".replace(",", " ") if l.price else "? kr"
                meta = " | ".join(filter(None, [
                    f"{l.rooms} rum" if l.rooms else "",
                    f"{l.living_area:.0f} m²" if l.living_area else "",
                    f"{l.monthly_fee:,} kr/mån".replace(",", " ") if l.monthly_fee else "",
                ]))
                lines.append(f"  {i}. {l.address}")
                lines.append(f"     {l.city}" if l.city else "")
                lines.append(f"     {price_str}" + (f"  ({meta})" if meta else ""))
                lines.append(f"     {l.url}")
                if l.listing_type:
                    lines.append(f"     [{l.listing_type}]")
                lines.append("")
            output_str = "\n".join(lines)

        if args.output:
            Path(args.output).write_text(output_str, encoding="utf-8")
            print(f"结果已保存到 {args.output}")
            if fmt == "html" and args.open:
                import webbrowser
                webbrowser.open(f"file://{Path(args.output).resolve()}")
        else:
            print(output_str)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
