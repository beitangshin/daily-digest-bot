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
import random
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
# Hemnet 的 GraphQL locationSearch 查出来的真实 ID（用 Playwright 走一遍搜索框自动补全
# 拿到的，不是猜的）。之前这里 "stockholm": ["17919"] 其实根本不对应 Stockholm——那是个
# 无效/别的地区的 ID，导致 location_ids 查出来是空的，才被前人改成 q="Stockholm" 兜底；
# 但 q= 在 Hemnet 上只是普通全文搜索，完全不做地域限定，会把全瑞典的房源都搜出来。
# "stockholm" 这里用 Stockholms län（省级，id=17744），覆盖 Solna/Täby/Danderyd/Huddinge
# 这些近郊——比只用 Stockholms kommun（市级，id=18031）更符合我们 allowed_cities 的范围，
# 剩下的靠 housing_pipeline.py 里的 allowed_cities/blocked_cities 再精筛一遍。
LOCATION_IDS: dict[str, list[str]] = {
    "stockholm": ["17744"],  # Stockholms län
    "hela_sverige": [],  # no filter = all of Sweden
}

# Hemnet 的筛选表单里 item_types 复选框的真实 value（用 Playwright 点开筛选面板读 DOM
# 拿到的，不是猜的）：villa / radhus / bostadsratt / fritidshus / tomt / gard / other——
# 注意 bostadsratt、gard 都不带重音符号，传 "bostadsrätt"/"gård" 会被 Hemnet 静默忽略。
# 复选框标签里公寓（lägenhet）用的就是 bostadsratt 这个 value（label 显示"Lägenheter"），
# Hemnet 没有独立的 lägenhet 分类。parhus（双拼/联排）没有对应的 checkbox value，实测
# 结果里也没见到单独归类，这里先兜底映射到 other，不保证精确（需要另外验证）。
_ITEM_TYPES = {
    "villa": "villa",
    "bostadsratt": "bostadsratt",
    "bostadsrätt": "bostadsratt",
    "lagenhet": "bostadsratt",
    "lägenhet": "bostadsratt",
    "fritidshus": "fritidshus",
    "tomt": "tomt",
    "gård": "gard",
    "gard": "gard",
    "radhus": "radhus",
    "parhus": "other",  # 未在 Hemnet 筛选面板里发现独立 value，暂兜底，需要单独验证
}


def _build_search_url(
    *,
    q: str | None = None,
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

    # 地域限定必须走 location_ids —— q= 在 Hemnet 上只是全文搜索，不做地域限定
    # （传 q=Stockholm 实际会搜出全瑞典的房源）。q/location 只作为查 LOCATION_IDS 的 key，
    # 查不到才退回 q= 硬搜（等于不限地域，聊胜于无）。
    ids = list(location_ids or [])
    key_source = location or q
    if key_source:
        key = key_source.lower().replace(" ", "_")
        resolved = LOCATION_IDS.get(key)
        if resolved:
            ids.extend(resolved)
        elif not ids:
            logger.warning("unknown location %r, falling back to free-text q= (not geo-scoped)", key_source)
            params["q"] = key_source
    # Hemnet 的搜索表单提交的是 location_ids[]=.../item_types[]=... 这种带方括号的
    # key（不是普通的重复 key=val1&key=val2）——用 Playwright 走一遍真实筛选器抓包
    # 确认的，没有方括号后缀这两个过滤器会被 Hemnet 静默忽略，等于没筛。
    if ids:
        params["location_ids[]"] = ids

    if item_types:
        normalized = []
        for t in item_types:
            t_lower = t.lower()
            norm = _ITEM_TYPES.get(t_lower, t_lower)
            if norm not in normalized:
                normalized.append(norm)
        params["item_types[]"] = normalized

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


# A genuine price line is "<digits with space separators> kr" and nothing else
# -- deliberately excludes "kr/mån" (fee) and "kr/m²" (price-per-area), which
# would otherwise also parse as a plausible-looking number and get mistaken
# for the price by a looser digits-only check.
_PRICE_LINE_RE = re.compile(r"^\d[\d\s]*\s*kr$")

# Banner/metadata lines that can appear above a card's real headline -- none of
# these are the listing's title, but only the weekday-abbreviated date form used
# to be excluded (see the title-picking loop below).
_TITLE_BOILERPLATE_RE = re.compile(
    r"^(Mån|Tis|Ons|Tor|Fre|Lör|Sön|Idag|Imorgon|Visas|Budgivning|Kontakta|Snart|Spara)\b",
    re.IGNORECASE,
)


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
        self._context = None
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
        await self._new_page()
        return self

    async def _new_page(self) -> None:
        """(Re)create the browser context + page.

        Hemnet 会在同一个 context/session 里的第二次导航（不管是 goto、真实
        click 还是 dispatch_event 触发的）上返回一个卡死不动的 "Vänta..."
        验证页——用 Playwright 实测过，goto/click/dispatch_event 三种方式都会
        被拦，唯独换一个全新的 context（哪怕还在同一个 browser 进程里）就完全
        正常。所以翻页时不复用 page，而是每页都重新开一个 context。
        """
        if self._context:
            await self._context.close()
        self._context = await self._browser.new_context(
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
        self._page = await self._context.new_page()
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
        await self._context.add_cookies([
            {"name": "cookie_consent", "value": "1", "domain": ".hemnet.se", "path": "/"},
        ])

    async def __aexit__(self, *args: Any) -> None:
        if self._context:
            await self._context.close()
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
        """Try to dismiss Hemnet's cookie / GDPR consent banner.

        Hemnet 目前用 Usercentrics（<aside id="usercentrics-cmp-ui">），不是这里按钮文案
        认识的 Didomi/自建横幅，点击会直接超时。所以先试按钮，再用 JS 强制把已知的几种
        弹窗容器都摘掉——保证无论后端换成哪家 CMP，弹窗都不会一直挡住页面阻止翻页/点击。
        """
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
            logger.debug("cookie banner button not found/clickable, falling back to force-remove")
        try:
            await self._page.evaluate(
                """() => {
                    document.querySelectorAll(
                        '#usercentrics-cmp-ui, [id*="usercentrics"], #didomi-host, ' +
                        '.didomi-popup-backdrop, #didomi-popup, #onetrust-consent-sdk, ' +
                        '[id*="CybotCookiebotDialog"]'
                    ).forEach(e => e.remove());
                }"""
            )
        except Exception:
            pass

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
                # ── 卡片结构 ──
                # [Rubrik]                    ← title
                # [Mäklarnamn]
                # [Gatuadress]
                # [Stadsdel / Ort]            ← city (line before price)
                # [Pris X XXX XXX kr]         ← find this → city = line before it
                # [X rum]
                # [Mäklartipset]
                _city = ""
                for idx, line in enumerate(lines):
                    # Price line -- only the first match counts. Without this
                    # guard, a stray number elsewhere in the card (visit
                    # counts, price-trend %, a "similar listings" widget
                    # bleeding into the card's DOM parent, etc.) could
                    # silently overwrite an already-correct price later in
                    # the loop, producing a wrong-but-plausible price that
                    # passes the min/max price filter undetected.
                    if price == 0 and _PRICE_LINE_RE.match(line):
                        parsed_price = _parse_price(line)
                        if parsed_price and 10000 < parsed_price < 100_000_000:
                            price = parsed_price
                        # City = line before price
                        if idx >= 1:
                            before = lines[idx - 1]
                            if not any(kw in before.lower() for kw in ["betald", "mäklar", "rum", "m²", "vån", "kr"]):
                                _city = before
                            # Address = 2 lines before price. Some cards insert an
                            # extra "avgift" (monthly fee, e.g. "6 612 kr/mån") line
                            # between the real address and the price, shifting this
                            # offset onto the fee line -- exclude "kr" here too (the
                            # city check above already does) so we don't mistake a
                            # fee for the street address.
                            if idx >= 2:
                                addr_before = lines[idx - 2]
                                if not any(kw in addr_before.lower() for kw in ["betald", "mäklar", "rum", "m²", "vån", "kr"]):
                                    address = addr_before
                        continue
                    # Rooms
                    rm = re.search(r"(\d+[.,]?\d*)\s*(?:rum|rok)", line, re.IGNORECASE)
                    if rm and rooms is None:
                        rooms = _parse_float(rm.group(1))
                    # Area -- villa/house cards often combine boarea + biarea
                    # into one line, e.g. "134 + 60 m²" (living area + non-
                    # heated secondary area like a basement). A plain
                    # r"(\d+)\s*m²" grabs whichever number sits directly
                    # before "m²", which for that combined format is the
                    # smaller trailing biarea number, not the true living
                    # area -- silently corrupting living_area to a fraction
                    # of the real value. Allow an optional "+ N" before the
                    # unit so the leading (boarea) number is captured instead.
                    am = re.search(r"(\d+)(?:\s*\+\s*\d+)?\s*m²", line)
                    if am and living_area is None:
                        va = _parse_float(am.group(1))
                        if va and va >= 8:
                            living_area = va
                    # Monthly fee
                    fm = re.search(r"(\d[\d\s]*)\s*kr/mån", line)
                    if fm and monthly_fee is None:
                        monthly_fee = _parse_int(fm.group(1))
                    # Title: first long-ish non-boilerplate line. Cards can carry
                    # extra banner lines above the real headline -- a viewing-time
                    # stamp ("Idag kl 14:30", "Imorgon kl 17:15-17:45", "Sön 16 aug
                    # kl 12:10"), a live-showing note ("Visas nu på söndag"), a
                    # bidding-status flag ("Budgivning pågår"), or a broker CTA
                    # ("Kontakta mig vid intresse!"). The old check only excluded
                    # the weekday-abbreviated date form, so any of these other
                    # variants got picked up as the title instead of the actual
                    # headline/address -- confirmed by scraping live cards where
                    # e.g. "Idag kl 11:30" or "Budgivning pågår" ended up as title.
                    if (not title and len(line) > 5
                            and not any(kw in line.lower() for kw in ["betald", "mäklar", "kr", "rum", "m²", "visning"])
                            and not _TITLE_BOILERPLATE_RE.match(line)):
                        title = line

                # Sanity: living_area < 10 m² is almost certainly noise
                if living_area is not None and living_area < 10:
                    living_area = None

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
    #
    # Hemnet 的翻页链接就是当前 URL 加一个 &page=N，本可以直接构造 URL 跳转，
    # 但同一个 context 里的第二次导航——不管是 goto()、真实 click 还是
    # dispatch_event('click')，三种都实测过——会被 Hemnet 判定成可疑行为，
    # 返回一个卡死不动的 "Vänta..." 验证页，永远等不到结果（这也是之前只抓到
    # 每次搜索恰好第 1 页数量的根本原因：翻页从没真正成功过）。唯独换一个全新
    # 的 context（即使还在同一个 browser 进程里）就完全正常，所以每翻一页都
    # 通过 `_new_page()` 重开一个 context 再直接 goto 目标页 URL。

    @staticmethod
    def _with_page_param(url: str, page_num: int) -> str:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
        if page_num > 1:
            query.append(("page", str(page_num)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

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

        all_listings: list[HemnetListing] = []
        for page_num in range(1, pages_to_scrape + 1):
            page_url = self._with_page_param(search_url, page_num)
            if page_num > 1:
                # 每页都是全新 context——见上面 Pagination 小节的说明。
                await self._new_page()
            logger.info("navigating to: %s", page_url)
            await self._page.goto(page_url, wait_until="domcontentloaded")
            await self._dismiss_cookies()

            try:
                await self._page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            await asyncio.sleep(2)

            if not await self._retry_on_cloudflare(page_url):
                logger.warning("Cloudflare 拦截超过最大重试次数，停止翻页")
                break

            logger.info("scraping page %d/%d", page_num, pages_to_scrape)

            # Scroll down to trigger lazy loading
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(random.uniform(1.0, 2.2))

            page_listings = await self._extract_listings_from_page()
            logger.info("  found %d listing(s) on page %d", len(page_listings), page_num)
            if not page_listings:
                logger.info("no more listings, stopping pagination")
                break
            all_listings.extend(page_listings)

            # 随机化翻页间隔，避免固定节奏被识别为爬虫；每 10 页额外歇一下
            await asyncio.sleep(random.uniform(3.0, 6.5))
            if page_num % 10 == 0:
                await asyncio.sleep(random.uniform(5.0, 10.0))

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
