"""
H.I.S. (エイチ・アイ・エス) connector — Japanese OTA, Playwright browser.

HIS Travel is one of Japan's largest travel agencies offering international
flights from Japanese airports (NRT, HND, KIX, FUK, etc.) via 180+ airlines.

Website: https://air.his-j.com/

Strategy:
  HIS uses a server-side streaming architecture (IBS middleware) that:
  1. Accepts a search via externalsearch.htm GET request
  2. Redirects to airproductsList.htm, initiating a streaming session
  3. Browser polls streamProducts.htm?rd={reqId} until status = COMPLETED
  4. All flight cards are rendered as server-side HTML in the DOM

  We use Playwright to:
  - Navigate to externalsearch.htm with search parameters
  - Wait for streamingSearchStatus = COMPLETED (or non-streaming mode)
  - Parse the server-rendered flight card HTML

  All prices are in JPY (¥). Only fires for routes originating from Japan.

Search URL format:
  https://air.his-j.com/fb/shop/externalsearch.htm
    ?SrcType=1
    &OrgArpt={origin_iata}    (origin airport, e.g. NRT)
    &DestCty={dest_city}      (destination city code, e.g. LON)
    &DepDt={YYYYMMDD}
    &RetDt={YYYYMMDD}         (omit for one-way)
    &Adt={adults}
    &Chd={children}
    &Inf={infants}
    &IsConn=Y
    &Cabin=Y                  (Y=economy, C=business, F=first)
    &ChkStAvlty=Y
    &OpenTicket=N

DOM selectors (confirmed):
  Flight card:    [class*="listViewDtls_"]
  Outbound leg:   .sliceOne
  Return leg:     .sliceTwo
  Airline img:    .airlineImg[src]        → extract IATA code from filename
  Airline name:   .airlineImg[data-original-title]
  Cabin class:    .flightType
  From code:      .travelFrom .travelPlaceCode
  From time:      .travelFrom .travelTime
  To code:        .travelTo .travelPlaceCode
  To time:        .travelTo .travelTime
  Stops:          .flightConnectione       → "乗継 1 回" / "直行便"
  Duration:       .travelDuration span     → "21時間 20分"
  Via airports:   .travelStops i           → [origin, stop, ..., destination]
  Price:          .amount                  → "154,960 円"
  Baggage:        [data-original-title]    → contains "受託手荷物" text
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

from ..models.flights import (
    FlightOffer,
    FlightRoute,
    FlightSearchRequest,
    FlightSearchResponse,
    FlightSegment,
)
from .browser import acquire_browser_slot, release_browser_slot, auto_block_if_proxied, inject_stealth_js

logger = logging.getLogger(__name__)

_SEARCH_BASE = "https://air.his-j.com/fb/shop/externalsearch.htm"

# Persistent cookie cache — pre-seeds Akamai and AWS ALB cookies so fresh Chrome
# sessions aren't immediately flagged as bots. Saved after each successful search.
_HIS_COOKIE_CACHE = os.path.join(
    os.environ.get("TEMP", os.path.expanduser("~")), ".his_cookies.json"
)
# Also accept cache from workspace root (e.g. seeded by the MCP browser)
_HIS_COOKIE_CACHE_FALLBACK = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".his_cookies.json"
)

# Map destination IATA airport codes to HIS city codes (for multi-airport cities)
# HIS uses 3-letter city codes for the DestCty parameter.
# Single-airport cities use the same code.
_AIRPORT_TO_CITY: dict[str, str] = {
    # UK
    "LHR": "LON", "LGW": "LON", "LCY": "LON", "STN": "LON", "LTN": "LON",
    # France
    "CDG": "PAR", "ORY": "PAR",
    # Italy
    "FCO": "ROM", "CIA": "ROM",
    "MXP": "MIL", "LIN": "MIL", "BGY": "MIL",
    # Spain
    "MAD": "MAD", "BCN": "BCN",
    # Germany
    "FRA": "FRA", "MUC": "MUC",
    # USA
    "JFK": "NYC", "EWR": "NYC", "LGA": "NYC",
    "LAX": "LAX", "SFO": "SFO", "ORD": "CHI",
    "MIA": "MIA", "BOS": "BOS", "SEA": "SEA",
    "ATL": "ATL", "DFW": "DFW", "DEN": "DEN",
    "LAS": "LAS", "HNL": "HNL",
    # Canada
    "YYZ": "YTO", "YUL": "YMQ", "YVR": "YVR",
    # Korea
    "ICN": "SEL", "GMP": "SEL",
    # China
    "PEK": "BJS", "PKX": "BJS",
    "PVG": "SHA", "SHA": "SHA",
    "CAN": "GZH", "SZX": "SZX",
    # Taiwan
    "TPE": "TPE", "TSA": "TPE",
    # Hong Kong
    "HKG": "HKG",
    # Thailand
    "BKK": "BKK", "DMK": "BKK",
    # Singapore
    "SIN": "SIN",
    # Malaysia
    "KUL": "KUL",
    # Indonesia
    "CGK": "JKT",
    # Vietnam
    "SGN": "SGN", "HAN": "HAN",
    # Philippines
    "MNL": "MNL",
    # India
    "DEL": "DEL", "BOM": "BOM",
    # Australia
    "SYD": "SYD", "MEL": "MEL", "BNE": "BNE", "PER": "PER",
    # New Zealand
    "AKL": "AKL",
    # UAE
    "DXB": "DXB", "AUH": "AUH",
    # Qatar
    "DOH": "DOH",
    # Finland
    "HEL": "HEL",
    # Netherlands
    "AMS": "AMS",
    # Switzerland
    "ZRH": "ZRH",
    # Portugal
    "LIS": "LIS",
    # Greece
    "ATH": "ATH",
    # Turkey
    "IST": "IST",
    # Russia
    "SVO": "MOW", "DME": "MOW",
    # Brazil
    "GRU": "SAO", "CGH": "SAO",
    # Mexico
    "MEX": "MEX",
    # Czech Republic
    "PRG": "PRG",
    # Poland
    "WAW": "WAW",
    # Hungary
    "BUD": "BUD",
    # Austria
    "VIE": "VIE",
    # Belgium
    "BRU": "BRU",
    # Denmark
    "CPH": "CPH",
    # Sweden
    "ARN": "STO",
    # Norway
    "OSL": "OSL",
    # South Africa
    "JNB": "JNB", "CPT": "CPT",
    # Egypt
    "CAI": "CAI",
    # Kenya
    "NBO": "NBO",
    # Sri Lanka
    "CMB": "CMB",
    # Maldives
    "MLE": "MLE",
    # Nepal
    "KTM": "KTM",
    # Cambodia
    "PNH": "PNH", "REP": "REP",
    # Myanmar
    "RGN": "RGN",
    # Guam
    "GUM": "GUM",
    # Saipan
    "SPN": "SPN",
    # Fiji
    "NAN": "NAN",
    # Hawai'i — already listed above as HNL → HNL
}

# Japanese cabin code mapping
_CABIN_MAP = {
    "M": "Y",  # Economy
    "W": "W",  # Premium economy (HIS may not support this, falls back to Y)
    "C": "C",  # Business
    "F": "F",  # First
}

# Japanese cabin text to LetsFG cabin code
_CABIN_TEXT_MAP = {
    "エコノミークラス": "M",
    "プレミアムエコノミークラス": "W",
    "プレミアムエコノミー": "W",
    "ビジネスクラス": "C",
    "ファーストクラス": "F",
}

# Japanese stop text
_STOP_PATTERN = re.compile(r"乗継\s*(\d+)\s*回")
_DUR_PATTERN = re.compile(r"(\d+)時間\s*(\d+)分")
_TIME_PATTERN = re.compile(r"^(\d{1,2}:\d{2})")
_AIRLINE_CODE_PATTERN = re.compile(r"/([A-Z0-9]{2})\.gif$", re.IGNORECASE)
_PRICE_PATTERN = re.compile(r"[\d,]+")
_BAGGAGE_KG_PATTERN = re.compile(r"(\d+)\s*kg", re.IGNORECASE)


def _parse_stops(text: str) -> int:
    """Parse connection text → number of stops."""
    if not text:
        return 0
    if "直行" in text:
        return 0
    m = _STOP_PATTERN.search(text)
    if m:
        return int(m.group(1))
    return 0


def _parse_duration_seconds(text: str) -> int:
    """Parse '21時間 20分' → seconds."""
    if not text:
        return 0
    m = _DUR_PATTERN.search(text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    return 0


def _parse_time(text: str) -> str:
    """Extract HH:MM from text like '07:0006/16(火)'."""
    if not text:
        return ""
    m = _TIME_PATTERN.match(text.strip())
    return m.group(1) if m else text.strip()[:5]


def _parse_price(text: str) -> float:
    """Parse '154,960 円' → 154960.0."""
    m = _PRICE_PATTERN.search(text.replace(",", ""))
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return 0.0


def _extract_airline_code(src: str) -> str:
    """Extract IATA code from img src '/fb/pageicons/airlines/japan/EY.gif'."""
    if not src:
        return ""
    m = _AIRLINE_CODE_PATTERN.search(src)
    return m.group(1).upper() if m else ""


def _dest_city_code(iata: str) -> str:
    """Convert destination IATA airport code to HIS city code."""
    return _AIRPORT_TO_CITY.get(iata.upper(), iata.upper())


def _build_search_url(req: FlightSearchRequest) -> str:
    """Build HIS externalsearch.htm URL from FlightSearchRequest."""
    cabin = _CABIN_MAP.get(req.cabin_class or "M", "Y")
    dep_dt = req.date_from.strftime("%Y%m%d")
    dest_city = _dest_city_code(req.destination)

    params = [
        "SrcType=1",
        f"OrgArpt={req.origin}",
        f"DestCty={dest_city}",
        f"DepDt={dep_dt}",
    ]

    # RetDt is REQUIRED by the server even for one-way searches — without it
    # the server returns 302 → error.jsp regardless of session state.
    if req.return_from:
        ret_dt = req.return_from.strftime("%Y%m%d")
    else:
        # Dummy return date: departure + 7 days (server needs a roundtrip
        # search to function; we parse only the outbound leg from results).
        from datetime import timedelta
        ret_dt = (req.date_from + timedelta(days=7)).strftime("%Y%m%d")
    params.append(f"RetDt={ret_dt}")

    params += [
        f"Adt={req.adults or 1}",
        f"Chd={req.children or 0}",
        f"Inf={req.infants or 0}",
        "IsConn=Y",
        f"Cabin={cabin}",
        "ChkStAvlty=Y",
        "OpenTicket=N",
    ]

    return _SEARCH_BASE + "?" + "&".join(params)


def _parse_slice(
    slice_el,
    search_date: date,
    fallback_airline: str = "ZZ",
) -> Optional[tuple[FlightSegment, int]]:
    """Parse a .sliceOne or .sliceTwo element.

    Returns (FlightSegment, stopovers_count) or None on parse failure.
    """
    if slice_el is None:
        return None

    try:
        from_code_el = slice_el.select_one(".travelFrom .travelPlaceCode")
        from_time_el = slice_el.select_one(".travelFrom .travelTime")
        to_code_el   = slice_el.select_one(".travelTo .travelPlaceCode")
        to_time_el   = slice_el.select_one(".travelTo .travelTime")
        conn_el      = slice_el.select_one(".flightConnectione")
        dur_el       = slice_el.select_one(".travelDuration span") or slice_el.select_one(".travelDuration")
        airline_img  = slice_el.select_one(".airlineImg")

        origin = (from_code_el.get_text(strip=True) if from_code_el else "").strip()[:4]
        dest   = (to_code_el.get_text(strip=True)   if to_code_el   else "").strip()[:4]
        if not origin or not dest:
            return None

        dep_time_str = _parse_time(from_time_el.get_text() if from_time_el else "")
        arr_time_str = _parse_time(to_time_el.get_text()   if to_time_el   else "")
        stops      = _parse_stops(conn_el.get_text() if conn_el else "")
        duration_s = _parse_duration_seconds(dur_el.get_text() if dur_el else "")

        # Airline: prefer img in this slice, fall back to card-level
        airline_code = _extract_airline_code(airline_img.get("src", "") if airline_img else "") or fallback_airline

        # Departure datetime
        if dep_time_str:
            dep_dt = datetime.combine(search_date, datetime.strptime(dep_time_str, "%H:%M").time())
        else:
            dep_dt = datetime.combine(search_date, datetime.min.time())

        # Arrival: use duration offset when available (handles multi-day flights)
        if duration_s > 0:
            arr_dt = dep_dt + timedelta(seconds=duration_s)
        elif arr_time_str:
            arr_time = datetime.strptime(arr_time_str, "%H:%M").time()
            arr_dt = datetime.combine(search_date, arr_time)
            # Roll over midnight if arrival is earlier than departure
            if arr_dt < dep_dt:
                arr_dt += timedelta(days=1)
        else:
            arr_dt = dep_dt

        seg = FlightSegment(
            airline=airline_code,
            origin=origin,
            destination=dest,
            departure=dep_dt,
            arrival=arr_dt,
            duration_seconds=duration_s,
        )
        return seg, stops
    except Exception as e:
        logger.debug("HIS _parse_slice error: %s", e)
        return None


def _parse_cards(html: str, req: FlightSearchRequest) -> list[FlightOffer]:
    """Parse HIS results page HTML into FlightOffer list."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    offers: list[FlightOffer] = []

    # Find all flight cards: class contains "listViewDtls_" and a numeric product ID
    cards = [
        tag for tag in soup.find_all(True, class_=True)
        if any(cls.startswith("listViewDtls_") and cls.replace("listViewDtls_", "").isdigit()
               for cls in tag.get("class", []))
    ]

    is_rt = bool(req.return_from)

    for card in cards:
        try:
            # Product ID
            product_id = None
            for cls in card.get("class", []):
                if cls.startswith("listViewDtls_") and cls.replace("listViewDtls_", "").isdigit():
                    product_id = cls.replace("listViewDtls_", "")
                    break
            if not product_id:
                continue

            # Airline info — also used as fallback for _parse_slice
            airline_img = card.select_one(".airlineImg")
            airline_code = ""
            airline_name = ""
            if airline_img:
                airline_code = _extract_airline_code(airline_img.get("src", ""))
                # data-original-title holds Japanese airline name
                airline_name = airline_img.get("data-original-title", "")

            fallback_al = airline_code or "ZZ"

            # Baggage — from data-original-title on .generalText wrapper
            # Example values:
            #   "受託手荷物付き（23kg 1個）"        → bag included (23 kg)
            #   "受託手荷物は含まれておりません"      → no free checked bag
            general_el = card.select_one(".generalText[data-original-title]")
            baggage_title = general_el.get("data-original-title", "") if general_el else ""
            has_bag = "受託手荷物付き" in baggage_title or "受託手荷物込み" in baggage_title
            no_bag = "受託手荷物は含まれておりません" in baggage_title or "手荷物なし" in baggage_title

            conditions: dict[str, str] = {}
            bags_price: dict = {}

            if has_bag:
                # Try to parse baggage weight (e.g. "23kg")
                kg_m = _BAGGAGE_KG_PATTERN.search(baggage_title)
                kg_str = f" ({kg_m.group(1)} kg)" if kg_m else ""
                conditions["checked_bag"] = f"1 checked bag included{kg_str}"
                bags_price["checked_bag"] = 0.0  # included in ticket price
            elif no_bag:
                conditions["checked_bag"] = "no free checked bag; add at checkout"
                # No numeric bags_price entry — price varies by airline

            # Price from .amount
            amount_el = card.select_one(".amount")
            price = _parse_price(amount_el.get_text() if amount_el else "0")
            if price <= 0:
                continue

            # Outbound leg
            slice_one = card.select_one(".sliceOne")
            ob_result = _parse_slice(slice_one, req.date_from, fallback_al) if slice_one else None
            if not ob_result:
                continue
            ob_seg, ob_stops = ob_result

            outbound_route = FlightRoute(
                segments=[ob_seg],
                total_duration_seconds=ob_seg.duration_seconds,
                stopovers=ob_stops,
            )

            # Return leg
            inbound_route = None
            if is_rt:
                slice_two = card.select_one(".sliceTwo")
                ib_result = _parse_slice(slice_two, req.return_from, fallback_al) if slice_two else None
                if ib_result:
                    ib_seg, ib_stops = ib_result
                    inbound_route = FlightRoute(
                        segments=[ib_seg],
                        total_duration_seconds=ib_seg.duration_seconds,
                        stopovers=ib_stops,
                    )

            # Build offer ID
            sh = hashlib.md5(
                f"his{product_id}{req.origin}{req.destination}".encode()
            ).hexdigest()[:12]
            offer_id = f"his_{sh}"

            offer = FlightOffer(
                id=offer_id,
                price=price,
                currency="JPY",
                airlines=[airline_name or airline_code],
                owner_airline=airline_name or airline_code,
                outbound=outbound_route,
                inbound=inbound_route,
                conditions=conditions,
                bags_price=bags_price,
                source="his_ota",
            )
            offers.append(offer)

        except Exception as e:
            logger.debug("HIS card parse error: %s", e)
            continue

    return offers


class HISConnectorClient:
    """H.I.S. Travel OTA — Playwright browser + server-rendered HTML scraping."""

    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout

    async def close(self):
        pass  # Browser closed per-search

    async def _fetch_ancillaries(self) -> dict:
        """Return OTA-level ancillary notes for fields not set per-card inline."""
        return {
            "carry_on": "carry-on: personal item free on most airlines; cabin bag varies by airline and fare",
            "seat": "seat selection: varies by airline; skip at checkout for free random seat",
        }

    def _apply_ancillaries(self, offers: list[FlightOffer], ancillary: dict) -> None:
        """Apply fallback ancillary notes to offers that don't already have them."""
        carry_on_note = ancillary.get("carry_on")
        seat_note = ancillary.get("seat")
        for offer in offers:
            if carry_on_note:
                offer.conditions.setdefault("carry_on", carry_on_note)
            if seat_note:
                offer.conditions.setdefault("seat", seat_note)

    async def search_flights(self, req: FlightSearchRequest) -> FlightSearchResponse:
        t0 = time.monotonic()
        offers = await self._search(req)
        offers.sort(key=lambda o: o.price if o.price > 0 else float("inf"))
        anc = await self._fetch_ancillaries()
        self._apply_ancillaries(offers, anc)
        elapsed = time.monotonic() - t0
        logger.info("HIS %s→%s: %d offers in %.1fs", req.origin, req.destination, len(offers), elapsed)

        sh = hashlib.md5(
            f"his{req.origin}{req.destination}{req.date_from}{req.return_from or ''}".encode()
        ).hexdigest()[:12]

        return FlightSearchResponse(
            search_id=f"fs_{sh}",
            origin=req.origin,
            destination=req.destination,
            currency="JPY",
            offers=offers[:40],
            total_results=len(offers),
        )

    async def _search(self, req: FlightSearchRequest) -> list[FlightOffer]:
        from playwright.async_api import async_playwright
        from .browser import _launched_pw_instances

        search_url = _build_search_url(req)
        logger.info("HIS search URL: %s", search_url)

        page_html = ""
        page_url = ""

        await acquire_browser_slot()
        pw = None
        browser = None
        try:
            pw = await async_playwright().start()
            _launched_pw_instances.append(pw)

            browser = await pw.chromium.launch(
                headless=False,
                channel="chrome",
                args=[
                    # Hides navigator.webdriver (primary bot detection signal)
                    "--disable-blink-features=AutomationControlled",
                    # Keep window off-screen so it doesn't appear on desktop
                    "--window-position=-2400,-2400",
                    "--window-size=1366,768",
                    # NOTE: Do NOT use --blink-settings=imagesEnabled=false or
                    # --disable-remote-fonts here — Akamai Bot Manager uses image
                    # loading and canvas font fingerprinting to score sessions.
                    # Blocking them produces a definitive "bot" verdict.
                ],
            )

            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
            )

            page = await context.new_page()
            await auto_block_if_proxied(page)
            await inject_stealth_js(page)

            try:
                # Direct navigation with explicit Referer header.
                #
                # The server checks that Referer is from www.his-j.com AND
                # that the URL includes RetDt (return date). Playwright's
                # page.goto() accepts a referer= argument that sets the Referer
                # header directly, so we don't need to pre-visit www.his-j.com.
                await page.goto(
                    search_url,
                    referer="https://www.his-j.com/",
                    wait_until="domcontentloaded",
                    timeout=int(self.timeout * 1000),
                )
                logger.info("HIS landed at: %s", page.url)
                if "error.jsp" in page.url:
                    logger.warning("HIS error.jsp for %s→%s — zero results", req.origin, req.destination)
                    return []

                # Wait for streaming to complete.
                # isStreamingRequired (hidden input) controls streaming mode.
                # streamingSearchStatus becomes 'COMPLETED' when done.
                # We also accept: no streaming element (non-streaming mode) +
                # at least one card present, or noResultsDiv visible.
                await page.wait_for_function(
                    """() => {
                        const reqEl = document.getElementById('isStreamingRequired');
                        if (!reqEl) {
                            // No streaming control: accept if we have cards or no-results
                            return document.querySelector('[class*="listViewDtls_"]') !== null
                                || document.getElementById('noResultsDiv') !== null
                                || document.getElementById('shoppingErrorDiv') !== null;
                        }
                        const isStreaming = reqEl.value === 'true';
                        if (!isStreaming) {
                            return document.querySelector('[class*="listViewDtls_"]') !== null
                                || document.getElementById('noResultsDiv') !== null
                                || document.getElementById('shoppingErrorDiv') !== null;
                        }
                        const statusEl = document.getElementById('streamingSearchStatus');
                        return statusEl && statusEl.value === 'COMPLETED';
                    }""",
                    timeout=int(self.timeout * 1000),
                )
                logger.info("HIS streaming complete, parsing HTML")

            except Exception as e:
                logger.warning("HIS wait timeout for %s→%s: %s — using partial HTML", req.origin, req.destination, e)

            page_url = page.url
            page_html = await page.content()

            # Save cookies for future sessions (refreshes Akamai _abck etc.)
            if "airproductsList.htm" in page_url:
                try:
                    _cookies = await context.cookies(["https://air.his-j.com"])
                    with open(_HIS_COOKIE_CACHE, "w") as _f:
                        json.dump(_cookies, _f)
                    logger.debug("HIS: saved %d cookies to cache", len(_cookies))
                except Exception:
                    pass

            # Log diagnostic state before closing
            try:
                diag = await page.evaluate("""() => ({
                    url: location.href,
                    isStreaming: (document.getElementById('isStreamingRequired') || {}).value,
                    status: (document.getElementById('streamingSearchStatus') || {}).value,
                    cards: document.querySelectorAll('[class*="listViewDtls_"]').length,
                    noResults: !!document.getElementById('noResultsDiv'),
                })""")
                logger.info("HIS diag: %s", diag)
            except Exception:
                pass

        except Exception as e:
            logger.warning("HIS browser error for %s→%s: %s", req.origin, req.destination, e)
            return []
        finally:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if pw:
                    await pw.stop()
                    if pw in _launched_pw_instances:
                        _launched_pw_instances.remove(pw)
            except Exception:
                pass
            release_browser_slot()

        if not page_html:
            logger.warning("HIS empty HTML for %s→%s (url=%s)", req.origin, req.destination, page_url)
            return []

        # Check for error/no-results page
        if "noResultsDiv" in page_html or "shoppingErrorDiv" in page_html:
            logger.info("HIS no results for %s→%s", req.origin, req.destination)
            return []

        offers = _parse_cards(page_html, req)
        logger.info("HIS parsed %d offers from %s", len(offers), page_url)
        return offers
