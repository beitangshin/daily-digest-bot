"""Playwright-based headless browser fetcher for JavaScript-heavy listing sites.

Currently implements a Hemnet scraper for the "瑞典房源日报" channel.

Usage (standalone):
    python -m daily_digest.fetch_playwright \\
        --url "https://www.hemnet.se/bostader?location_ids%5B%5D=..."
    python -m daily_digest.fetch_playwright \\
        --location "stockholm" --max-price 5000000 --min-rooms 3

Usage (as part of the pipeline, see fetch.py -- type="playwright"):
    from daily_digest.fetch_playwright import HemnetScraper

    async with HemnetScraper() as scraper:
        listings = await scraper.search(search_url="...")
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
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class HemnetListing:
    """A single listing from Hemnet."""

    title: str
    address: str
    price: int  # SEK
    url: str
    rooms: float | None = None
    living_area: float | None = None  # m² (boarea)
    land_area: float | None = None  # m² (tomtarea), only for houses
    monthly_fee: int | None = None  # SEK/month (avgift), only for bostadsrätt
    price_per_sqm: int | None = None  # SEK/m²
    brokerage: str | None = None
    published_at: datetime | None = None
    listing_type: str = ""  # "villa", "bostadsrätt", "fritidshus", etc.
    image_url: str | None = None
    city: str = ""
    source: str = "hemnet"


# ---------------------------------------------------------------------------
# Hemnet scraper
# ---------------------------------------------------------------------------

SEARCH_BASE = "https://www.hemnet.se/bostader"

# Mapping for common location slugs → location_ids used by Hemnet.
# These can change; verify at https://www.hemnet.se/bostader and inspect the
# network request when you select an area in the search filter.
LOCATION_IDS: dict[str, list[str]] = {
    "stockholm": ["17919"],  # Stockholm kommun
    "gothenburg": ["17920"],  # Göteborgs kommun
    "malmo": ["17921"],  # Malmö kommun
    "uppsala": ["17922"],
    "linkoping": ["17923"],
    "vasteras": ["17924"],
    "orebro": ["17925"],
    "helsingborg": ["17926"],
    "jonkoping": ["17927"],
    "norrkoping": ["17928"],
    "hela_sverige": [],  # no filter = all of Sweden
}

_ITEM_TYPES = {
    "villa": "villa",
    "bostadsratt": "bostadsrätt",
    "bostadsrätt": "bostadsrätt",
    "fritidshus": "fritidshus",
    "tomt": "tomt",
    "gård": "gård",
    "radhus": "radhus",
    "parhus": "parhus",
}


def _build_search_url(
    *,
    location: str | None = None,
    location_ids: list[str] | None = None,
    item_types: list[str] | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rooms: float | None = None,
    max_rooms: float | None = None,
    min_area: int | None = None,
    max_area: int | None = None,
    min_land_area: int | None = None,
    sort: str = "publication_time_desc",
) -> str:
    """Build a Hemnet search URL from structured parameters.

    Parameters match the filters on hemnet.se/bostader.
    """
    from urllib.parse import urlencode

    params: dict[str, str | list[str]] = {}

    # Resolve location → location_ids
    ids = location_ids or []
    if location:
        key = location.lower().replace(" ", "_")
        resolved = LOCATION_IDS.get(key)
        if resolved:
            ids.extend(resolved)
        else:
            logger.warning("unknown location %r, leaving location unset", location)
    if ids:
        params["location_ids"] = ids  # urlencode handles list → repeated key

    if item_types:
        normalized = []
        for t in item_types:
            t_lower = t.lower()
            norm = _ITEM_TYPES.get(t_lower, t_lower)
            normalized.append(norm)
        params["item_types"] = normalized

    if min_price is not None:
        params["price_min"] = str(min_price)
    if max_price is not None:
        params["price_max"] = str(max_price)
    if min_rooms is not None:
        params["rooms_min"] = str(min_rooms)
    if max_rooms is not None:
        params["rooms_max"] = str(max_rooms)
    if min_area is not None:
        params["living_area_min"] = str(min_area)
    if max_area is not None:
        params["living_area_max"] = str(max_area)
    if min_land_area is not None:
        params["plot_area_min"] = str(min_land_area)

    if sort:
        params["sort_by"] = sort

    return SEARCH_BASE + "?" + urlencode(params, doseq=True)


def _parse_price(text: str | None) -> int | None:
    """Parse a Swedish price string like '3 500 000 kr' → 3500000."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_float(text: str | None) -> float | None:
    """Parse '3.5' or '3 5' → 3.5."""
    if not text:
        return None
    cleaned = text.replace(" ", "").replace(",", ".").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(text: str | None) -> int | None:
    """Parse '1 234' → 1234."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    try:
        return int(cleaned)
    except ValueError:
        return None


class HemnetScraper:
    """Async context manager that wraps a Playwright browser page for Hemnet.

    Typical flow:
        async with HemnetScraper(headless=True) as scraper:
            listings = await scraper.search(
                search_url="https://www.hemnet.se/bostader?..."
            )
            for listing in listings:
                print(listing)
    """

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 30_000,
        max_pages: int = 5,
        slow_mo: int | None = None,
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_pages = max_pages
        self.slow_mo = slow_mo
        self._browser = None
        self._page = None

    async def __aenter__(self) -> HemnetScraper:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--disable-blink-features=IdleDetection",
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
            # 更真实的浏览器指纹
            screen={"width": 1920, "height": 1080},
            no_viewport=False,
            # 允许权限
            permissions=["geolocation"],
            # 地理位置模拟 Stockholm
            geolocation={"latitude": 59.3293, "longitude": 18.0686},
            # 存储和 cookie
            storage_state=None,
        )
        self._page = await context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

        # 强反爬：全方位模拟真实浏览器
        await self._page.add_init_script("""
            // 1. 隐藏 webdriver
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // 2. 语言
            Object.defineProperty(navigator, 'languages', { get: () => ['sv-SE', 'en-US', 'en'] });

            // 3. 插件
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin' })),
            });

            // 4. Chrome 对象
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {},
            };

            // 5. WebGL 指纹
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {
                if (p === 37445) return 'Intel Inc.';  // UNMASKED_VENDOR_WEBGL
                if (p === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
                return getParameter(p);
            };

            // 6. 硬件信息
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

            // 7. 权限
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) => (
                p.name === 'notifications' ? Promise.resolve({ state: 'denied' }) : originalQuery(p)
            );

            // 8. 覆盖 connection
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                    saveData: false,
                }),
            });
        """)

        # 额外：设置 cookies 让 Hemnet 以为来过
        await context.add_cookies([
            {"name": "cookie_consent", "value": "1", "domain": ".hemnet.se", "path": "/"},
        ])
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Cookie / consent handling
    # ------------------------------------------------------------------

    _CLOUDFLARE_KEYWORDS = ["just a moment", "checking your browser", "cf-challenge", "cloudflare"]

    async def _is_cloudflare_blocked(self) -> bool:
        """Check if the page is blocked by Cloudflare."""
        title = (await self._page.title()).lower()
        body = await self._page.evaluate("document.body?.innerText?.substring(0, 500) || ''")
        body_lower = body.lower()
        for kw in self._CLOUDFLARE_KEYWORDS:
            if kw in title or kw in body_lower:
                return True
        return False

    async def _retry_on_cloudflare(self, url: str, max_retries: int = 3) -> bool:
        """Reload the page up to max_retries times if Cloudflare blocks."""
        for attempt in range(1, max_retries + 1):
            if await self._is_cloudflare_blocked():
                logger.warning("  Cloudflare 拦截（尝试 %d/%d），重新加载...", attempt, max_retries)
                await asyncio.sleep(2 * attempt)  # 递增等待
                await self._page.goto(url, wait_until="domcontentloaded")
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                await self._dismiss_cookies()
                await asyncio.sleep(1)
            else:
                return True  # 通过了
        return False  # 重试完仍然被挡

    async def _dismiss_cookies(self) -> None:
        """Try to dismiss Hemnet's cookie / GDPR consent banner."""
        try:
            btn = self._page.locator(
                'button:has-text("Godkänn alla"), '
                'button:has-text("Acceptera alla"), '
                'button:has-text("Accept all"), '
                "#didomi-notice-agree-button"
            )
            if await btn.count() > 0:
                await btn.first.click(timeout=5000)
                await asyncio.sleep(0.5)
                logger.info("dismissed cookie banner")
        except Exception:
            logger.debug("no cookie banner found or already dismissed")

    # ------------------------------------------------------------------
    # Listing extraction
    # ------------------------------------------------------------------

    async def _extract_listings_from_page(self) -> list[HemnetListing]:
        """Parse all listing cards from the current page using Playwright
        Python locator API (more reliable than inline JS)."""
        # Find card elements: locate links pointing to /bostad/ URLs, then get
        # the parent card wrapper that contains all the metadata.
        cards = self._page.locator("a[href*='/bostad/']").locator("..")
        count = await cards.count()
        if count == 0:
            logger.warning("no listing cards found on page")
            return []

        BASE_URL = "https://www.hemnet.se"
        results: list[HemnetListing] = []
        for i in range(count):
            try:
                card = cards.nth(i)
                text = await card.inner_text()
                link_el = card.locator("a[href*='/bostad/']")
                href = await link_el.first.get_attribute("href")
                if not href:
                    continue

                url = href if href.startswith("http") else BASE_URL + href

                # Determine listing type from URL (note: /bostad/villa-... not /villa/)
                listing_type = ""
                url_path = url.split("?")[0]  # strip query params
                if "/villa-" in url_path:
                    listing_type = "villa"
                elif "/bostadsratt-" in url_path or "/bostadsrätt-" in url_path:
                    listing_type = "bostadsrätt"
                elif "/lagenhet-" in url_path:
                    listing_type = "bostadsrätt"
                elif "/fritidshus-" in url_path:
                    listing_type = "fritidshus"
                elif "/tomt-" in url_path:
                    listing_type = "tomt"
                elif "/radhus-" in url_path:
                    listing_type = "radhus"
                elif "/gård-" in url_path or "/gard-" in url_path:
                    listing_type = "gård"
                elif "/parhus-" in url_path:
                    listing_type = "parhus"

                # Get image
                img = card.locator("img").first
                image_url = await img.get_attribute("src") if await img.count() > 0 else None

                # Parse text content
                title = ""
                address = ""
                price = 0
                rooms: float | None = None
                living_area: float | None = None
                monthly_fee: int | None = None

                lines = [line.strip() for line in text.split("\n") if line.strip()]
                for line in lines:
                    # Price: "3 595 000 kr" (cap at 100M SEK to filter junk)
                    if "kr" in line and not line.startswith("Betald") and "Mäklar" not in line and "mån" not in line and "kr/m²" not in line:
                        parsed = _parse_price(line)
                        if parsed and 10000 < parsed < 100_000_000:
                            price = parsed
                    # Rooms: "2 rum", "3,5 rum"
                    rooms_match = re.search(r"(\d+[.,]?\d*)\s*(?:rum|rok)", line, re.IGNORECASE)
                    if rooms_match and rooms is None:
                        rooms = _parse_float(rooms_match.group(1))
                    # Area: "123 m²"
                    area_match = re.search(r"(\d+)\s*m²", line)
                    if area_match and living_area is None:
                        living_area = _parse_float(area_match.group(1))
                    # Monthly fee: "3 500 kr/mån"
                    fee_match = re.search(r"(\d[\d\s]*)\s*kr/mån", line)
                    if fee_match and monthly_fee is None:
                        monthly_fee = _parse_int(fee_match.group(1))
                    # Title heuristics: first long-ish non-metadata line that looks
                    # like a property description (not a person name, date, etc.)
                    skip_kw = ["kr", "rum", "m²", "Mäklar", "Betald", "mån",
                               "visning", "Idag", "Imorgon"]
                    if not title and len(line) > 5 and not any(kw in line for kw in skip_kw):
                        # Skip weekday date-lines and single-word broker names
                        if not re.match(r"^(Mån|Tis|Ons|Tor|Fre|Lör|Sön)\s+\d", line):
                            title = line
                    # Address: first line with a comma and at least one space after
                    if title and not address and len(line) > 8 and "," in line:
                        # Remove leading comma if present
                        addr = line.lstrip(", ")
                        if addr:
                            address = addr

                # Sanity: living_area < 10 m² is almost certainly noise (e.g. "80 m²"
                # matched as "8 m²" from "188 m²" or "3 m²" from a date like "2023")
                if living_area is not None and living_area < 10:
                    living_area = None

                # Extract city from address (usually "Street, City" or "Area, Municipality")
                _city = ""
                if address and "," in address:
                    _city = address.rsplit(",", 1)[-1].strip()
                elif title and "," in title:
                    _city = title.rsplit(",", 1)[-1].strip()

                listing = HemnetListing(
                    title=title or address or "Hemnet listing",
                    address=address if address and address.strip(", ") else (title or ""),
                    price=price,
                    url=url,
                    rooms=rooms,
                    living_area=living_area,
                    monthly_fee=monthly_fee,
                    price_per_sqm=int(price / living_area) if price and living_area and living_area > 0 else None,
                    listing_type=listing_type,
                    image_url=image_url,
                    city=_city,
                )
                results.append(listing)
            except Exception:
                logger.debug("failed to parse card %d on page, skipping", i)
                continue

        return results

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def _has_next_page(self) -> bool:
        """Check if a 'next page' link/button exists."""
        next_btn = self._page.locator(
            'a[rel="next"], '
            'button:has-text("Nästa"), '
            'a:has-text("Nästa sida"), '
            '[data-testid="pagination-next"]'
        )
        return await next_btn.count() > 0

    async def _go_next_page(self) -> bool:
        """Click the 'next page' link and wait for new results to load."""
        next_btn = self._page.locator(
            'a[rel="next"], '
            'button:has-text("Nästa"), '
            'a:has-text("Nästa sida"), '
            '[data-testid="pagination-next"]'
        )
        if await next_btn.count() == 0:
            return False
        try:
            await next_btn.first.click(timeout=5_000)
            await self._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            await asyncio.sleep(2)
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        search_url: str | None = None,
        *,
        location: str | None = None,
        location_ids: list[str] | None = None,
        item_types: list[str] | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        min_rooms: float | None = None,
        max_rooms: float | None = None,
        min_area: int | None = None,
        max_area: int | None = None,
        max_pages: int | None = None,
        sort: str = "publication_time_desc",
    ) -> list[HemnetListing]:
        """Search Hemnet for listings and return structured results.

        Args:
            search_url: Full Hemnet search URL (takes precedence over other params).
            location: Shortcut name, e.g. "stockholm", "gothenburg".
            location_ids: Raw Hemnet location ID(s).
            item_types: List like ["villa", "bostadsrätt"].
            min_price / max_price: Price range in SEK.
            min_rooms / max_rooms: Room count range.
            min_area / max_area: Living area range in m².
            max_pages: How many result pages to scrape (default: self.max_pages).
            sort: Sort order. Common: "publication_time_desc" (newest first, default).

        Returns:
            List of HemnetListing dataclass instances, newest first.
        """
        if not search_url:
            search_url = _build_search_url(
                location=location,
                location_ids=location_ids,
                item_types=item_types,
                min_price=min_price,
                max_price=max_price,
                min_rooms=min_rooms,
                max_rooms=max_rooms,
                min_area=min_area,
                max_area=max_area,
                sort=sort,
            )

        pages_to_scrape = max_pages or self.max_pages
        logger.info("navigating to: %s", search_url)
        await self._page.goto(search_url, wait_until="domcontentloaded")
        await self._dismiss_cookies()

        # Wait for the result list to render
        try:
            await self._page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # 检测 Cloudflare，被挡则自动重试
        if not await self._retry_on_cloudflare(search_url):
            logger.warning("Cloudflare 拦截超过最大重试次数，返回空结果")
            return []

        all_listings: list[HemnetListing] = []
        for page_num in range(1, pages_to_scrape + 1):
            logger.info("scraping page %d/%d", page_num, pages_to_scrape)

            # Scroll down to trigger lazy loading
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)

            page_listings = await self._extract_listings_from_page()
            logger.info("  found %d listing(s) on page %d", len(page_listings), page_num)
            all_listings.extend(page_listings)

            # Try next page
            if page_num < pages_to_scrape and await self._has_next_page():
                if not await self._go_next_page():
                    logger.info("no more pages available")
                    break
                await asyncio.sleep(2)
            else:
                break

        # Deduplicate by URL (keep first / newest occurrence)
        seen_urls: set[str] = set()
        unique: list[HemnetListing] = []
        for listing in all_listings:
            if listing.url in seen_urls:
                continue
            seen_urls.add(listing.url)
            unique.append(listing)

        logger.info("total unique listings scraped: %d", len(unique))
        return unique

    async def get_listing_detail(self, url: str) -> dict[str, Any]:
        """Fetch detailed info from a single listing page.

        Returns a dict with keys like: description, energy_class, build_year,
        etc. This is supplementary; the main pipeline uses search() results.
        """
        logger.info("fetching detail: %s", url)
        await self._page.goto(url, wait_until="domcontentloaded")
        await self._dismiss_cookies()
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            await asyncio.sleep(3)

        detail: dict[str, Any] = {"url": url}
        for key, selector in [
            ("description", '[data-testid="listing-description"]'),
            ("energy_class", '[data-testid="energy-class"]'),
            ("build_year", '[data-testid="build-year"]'),
            ("biarea", '[data-testid="bi-area"]'),
            ("monthly_fee", '[data-testid="monthly-fee"]'),
            ("operating_cost", '[data-testid="operating-cost"]'),
        ]:
            try:
                el = self._page.locator(selector).first
                if await el.count() > 0:
                    detail[key] = await el.inner_text()
            except Exception:
                pass
        return detail


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------


def _render_listings_html(listings: list[HemnetListing]) -> str:
    """Render listings as a standalone HTML page (browseable)."""
    cards_html = ""
    for listing in listings:
        price_str = f"{listing.price:,} kr".replace(",", " ") if listing.price else ""
        rooms_str = f"{listing.rooms} rum" if listing.rooms else ""
        area_str = f"{listing.living_area:.0f} m²" if listing.living_area else ""
        fee_str = f"{listing.monthly_fee:,} kr/mån".replace(",", " ") if listing.monthly_fee else ""
        pps_str = f"{listing.price_per_sqm:,} kr/m²".replace(",", " ") if listing.price_per_sqm else ""

        meta_parts = []
        if rooms_str:
            meta_parts.append(f"<span class='tag'>{rooms_str}</span>")
        if area_str:
            meta_parts.append(f"<span class='tag'>{area_str}</span>")
        if listing.listing_type:
            t = {"villa": "🏠 Villa", "bostadsrätt": "🏢 BRF", "radhus": "🏘️ Radhus", "fritidshus": "🌲 Fritidshus", "tomt": "🌿 Tomt", "gård": "🏡 Gård"}.get(listing.listing_type, listing.listing_type)
            meta_parts.append(f"<span class='tag type-tag'>{t}</span>")
        if fee_str:
            meta_parts.append(f"<span class='tag fee-tag'>{fee_str}</span>")
        meta_html = " ".join(meta_parts)

        img_html = f"<img src='{listing.image_url}' alt='' loading='lazy'>" if listing.image_url else "<div class='no-img'>📷</div>"

        cards_html += f"""
        <div class='card'>
            <div class='card-img'>{img_html}</div>
            <div class='card-body'>
                <h2><a href='{listing.url}' target='_blank'>{listing.title}</a></h2>
                <p class='address'>{listing.address}</p>
                <p class='price'>{price_str}</p>
                <div class='meta'>{meta_html}</div>
                {f"<p class='pps'>{pps_str}</p>" if pps_str else ""}
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hemnet 房源搜索结果 ({len(listings)} 套)</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #222; padding: 20px; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 20px; color: #444; }}
  h1 span {{ font-weight: 400; color: #888; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 16px; }}
  .card {{ background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); transition: box-shadow .15s; }}
  .card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,.12); }}
  .card-img {{ width: 100%; height: 200px; overflow: hidden; background: #e0e0e0; }}
  .card-img img {{ width: 100%; height: 100%; object-fit: cover; }}
  .no-img {{ display: flex; align-items: center; justify-content: center; height: 100%; font-size: 3rem; color: #aaa; }}
  .card-body {{ padding: 14px; }}
  .card-body h2 {{ font-size: 1rem; line-height: 1.4; margin-bottom: 4px; }}
  .card-body h2 a {{ color: #222; text-decoration: none; }}
  .card-body h2 a:hover {{ color: #db4c3f; }}
  .address {{ font-size: .85rem; color: #666; margin-bottom: 8px; }}
  .price {{ font-size: 1.15rem; font-weight: 700; color: #db4c3f; margin-bottom: 8px; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .tag {{ display: inline-block; font-size: .75rem; padding: 2px 8px; border-radius: 4px; background: #f0f0f0; color: #555; }}
  .type-tag {{ background: #e8f0fe; color: #1967d2; }}
  .fee-tag {{ background: #fef7e0; color: #b8860b; }}
  .pps {{ font-size: .78rem; color: #888; margin-top: 6px; }}
  .footer {{ margin-top: 20px; font-size: .8rem; color: #999; text-align: center; }}
</style>
</head>
<body>
<h1>Hemnet <span>— {len(listings)} 套房源</span></h1>
<div class='cards'>{cards_html}</div>
<p class='footer'>由 daily-digest-bot · Playwright 抓取 · {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Async entry-point helpers
# ---------------------------------------------------------------------------

async def _search_and_print(
    *,
    search_url: str | None = None,
    location: str | None = None,
    location_ids: list[str] | None = None,
    item_types: list[str] | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rooms: float | None = None,
    max_rooms: float | None = None,
    min_area: int | None = None,
    max_area: int | None = None,
    max_pages: int = 5,
    headless: bool = True,
    output: str | None = None,
    format: str = "text",  # "text", "json", or "html"
    open_browser: bool = False,
) -> None:
    """Run a search and print/save results."""
    async with HemnetScraper(headless=headless, max_pages=max_pages) as scraper:
        listings = await scraper.search(
            search_url=search_url,
            location=location,
            location_ids=location_ids,
            item_types=item_types,
            min_price=min_price,
            max_price=max_price,
            min_rooms=min_rooms,
            max_rooms=max_rooms,
            min_area=min_area,
            max_area=max_area,
        )

    if format == "json":
        data = [asdict(l) for l in listings]
        # Convert datetimes to ISO strings for JSON
        for item in data:
            if item.get("published_at"):
                item["published_at"] = item["published_at"].isoformat()
        output_str = json.dumps(data, ensure_ascii=False, indent=2)
    elif format == "html":
        output_str = _render_listings_html(listings)
    else:
        lines = [f"找到 {len(listings)} 套房源：\n"]
        for i, l in enumerate(listings, 1):
            price_str = f"{l.price:,} kr".replace(",", " ") if l.price else "? kr"
            rooms_str = f"{l.rooms} rum" if l.rooms else ""
            area_str = f"{l.living_area:.0f} m²" if l.living_area else ""
            fee_str = f"avgift {l.monthly_fee:,} kr".replace(",", " ") if l.monthly_fee else ""
            meta = " | ".join(filter(None, [rooms_str, area_str, fee_str]))
            lines.append(f"  {i}. {l.title}")
            lines.append(f"     {l.address}")
            lines.append(f"     {price_str}" + (f"  ({meta})" if meta else ""))
            lines.append(f"     {l.url}")
            lines.append(f"     [{l.listing_type}]" if l.listing_type else "")
            lines.append("")
        output_str = "\n".join(lines)

    from pathlib import Path

    if output:
        out_path = Path(output)
        out_path.write_text(output_str, encoding="utf-8")
        print(f"结果已保存到 {out_path.resolve()}")
        if format == "html" and open_browser:
            import webbrowser
            webbrowser.open(f"file://{out_path.resolve()}")
    else:
        print(output_str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m daily_digest.fetch_playwright",
        description="无头浏览器房源搜索（当前支持 Hemnet）",
    )

    # Search URL (takes precedence)
    parser.add_argument("--url", help="完整的 Hemnet 搜索结果 URL（覆盖其他搜索参数）")

    # Structured search params
    parser.add_argument("--location", help="地区名，如 stockholm, gothenburg, malmo 等")
    parser.add_argument("--location-ids", nargs="*", help="直接传 Hemnet location_id（覆盖 --location 的映射）")
    parser.add_argument("--item-types", nargs="*", default=[], help="房型：villa, bostadsrätt, fritidshus ...")
    parser.add_argument("--min-price", type=int, help="最低总价 SEK")
    parser.add_argument("--max-price", type=int, help="最高总价 SEK")
    parser.add_argument("--min-rooms", type=float, help="最少房间数")
    parser.add_argument("--max-rooms", type=float, help="最多房间数")
    parser.add_argument("--min-area", type=int, help="最小居住面积 m²")
    parser.add_argument("--max-area", type=int, help="最大居住面积 m²")

    # Behaviour
    parser.add_argument("--pages", type=int, default=5, help="抓取页数（默认 5）")
    parser.add_argument("--visible", action="store_true", help="有头模式（调试用，默认无头）")
    parser.add_argument("--output", "-o", help="保存到文件（.html 后缀自动用 HTML 格式，.json 用 JSON）")
    parser.add_argument("--html", action="store_true", help="输出 HTML 页面（默认文字）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--open", action="store_true", help="保存 HTML 后自动在浏览器打开")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")

    return parser


def _force_utf8_console() -> None:
    """Windows consoles default to the system codepage (e.g. GBK), which
    mangles the Chinese output this CLI prints."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _force_utf8_console()
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

    asyncio.run(
        _search_and_print(
            search_url=args.url,
            location=args.location,
            location_ids=args.location_ids,
            item_types=args.item_types or None,
            min_price=args.min_price,
            max_price=args.max_price,
            min_rooms=args.min_rooms,
            max_rooms=args.max_rooms,
            min_area=args.min_area,
            max_area=args.max_area,
            max_pages=args.pages,
            headless=not args.visible,
            output=args.output,
            format=fmt,
            open_browser=args.open,
        )
    )


if __name__ == "__main__":
    main()
