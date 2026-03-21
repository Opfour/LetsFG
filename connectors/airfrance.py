"""
Air France connector — EveryMundo airTRFX fare pages via curl_cffi.

Air France (IATA: AF) is the flag carrier of France.
Hub at Paris Charles de Gaulle (CDG) with 200+ destinations worldwide.
Part of the Air France-KLM Group, SkyTeam alliance.

Strategy (curl_cffi required — Cloudflare / Akamai protections):
  1. Resolve IATA codes to city slugs via AF's airport lookup API
  2. Fetch route page: wwws.airfrance.nl/en-nl/flights-from-{origin}-to-{dest}
  3. Extract __NEXT_DATA__ JSON from <script> tag
  4. Parse DpaHeadline → lowestFare for route pricing data
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

from curl_cffi import requests as creq

from models.flights import (
    FlightOffer,
    FlightRoute,
    FlightSearchRequest,
    FlightSearchResponse,
    FlightSegment,
)

logger = logging.getLogger(__name__)

_MARKETS = [
    ("https://wwws.airfrance.nl", "en-nl"),
    ("https://wwws.airfrance.us", "en-us"),
]
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Static slug mapping for major airports. Falls back to airport API.
_IATA_TO_SLUG: dict[str, str] = {
    # Netherlands
    "AMS": "amsterdam",
    # France
    "CDG": "paris", "ORY": "paris",
    "NCE": "nice", "LYS": "lyon", "MRS": "marseille", "TLS": "toulouse",
    "BOD": "bordeaux", "NTE": "nantes", "MPL": "montpellier",
    # UK / Ireland
    "LHR": "london", "LGW": "london", "STN": "london", "LTN": "london",
    "MAN": "manchester", "EDI": "edinburgh", "GLA": "glasgow",
    "BHX": "birmingham", "BRS": "bristol", "NCL": "newcastle",
    "DUB": "dublin", "SNN": "shannon", "ORK": "cork",
    # Germany
    "FRA": "frankfurt/main", "MUC": "munich", "BER": "berlin",
    "HAM": "hamburg", "DUS": "dusseldorf", "CGN": "cologne",
    "STR": "stuttgart", "HAJ": "hanover", "NUE": "nuremberg",
    # Spain
    "BCN": "barcelona", "MAD": "madrid", "AGP": "malaga",
    "ALC": "alicante", "PMI": "palma-de-mallorca", "VLC": "valencia",
    "BIO": "bilbao", "SVQ": "seville", "TFS": "tenerife",
    # Italy
    "FCO": "rome", "MXP": "milan", "VCE": "venice",
    "NAP": "naples", "BLQ": "bologna", "FLR": "florence",
    "CTA": "catania", "PSA": "pisa",
    # Nordics
    "CPH": "copenhagen", "ARN": "stockholm", "GOT": "gothenburg",
    "OSL": "oslo", "BGO": "bergen", "TRD": "trondheim",
    "HEL": "helsinki", "TMP": "tampere",
    # Central/Eastern Europe
    "VIE": "vienna", "ZRH": "zurich", "GVA": "geneva",
    "BRU": "brussels", "LUX": "luxembourg",
    "WAW": "warsaw", "KRK": "krakow",
    "PRG": "prague", "BUD": "budapest",
    "OTP": "bucharest", "SOF": "sofia", "ZAG": "zagreb",
    "BEG": "belgrade", "LJU": "ljubljana",
    # Greece / Turkey / Cyprus
    "ATH": "athens", "SKG": "thessaloniki",
    "IST": "istanbul", "SAW": "istanbul", "AYT": "antalya",
    "LCA": "larnaca", "PFO": "paphos",
    # Portugal
    "LIS": "lisbon", "OPO": "porto", "FAO": "faro",
    # Americas
    "JFK": "new-york", "EWR": "new-york", "LGA": "new-york",
    "LAX": "los-angeles", "SFO": "san-francisco",
    "ORD": "chicago", "IAH": "houston", "DFW": "dallas",
    "ATL": "atlanta", "MIA": "miami", "BOS": "boston",
    "IAD": "washington-dc", "DCA": "washington-dc",
    "SEA": "seattle", "MSP": "minneapolis", "DEN": "denver",
    "DTW": "detroit", "PHL": "philadelphia",
    "YYZ": "toronto", "YUL": "montreal", "YVR": "vancouver",
    "MEX": "mexico-city", "CUN": "cancun",
    "GRU": "sao-paulo", "GIG": "rio-de-janeiro",
    "EZE": "buenos-aires", "BOG": "bogota", "SCL": "santiago",
    "LIM": "lima", "PTY": "panama-city",
    # Middle East / Africa
    "DXB": "dubai", "AUH": "abu-dhabi", "DOH": "doha",
    "BAH": "bahrain", "KWI": "kuwait", "RUH": "riyadh",
    "JED": "jeddah", "TLV": "tel-aviv", "AMM": "amman",
    "CAI": "cairo", "CMN": "casablanca", "TUN": "tunis",
    "NBO": "nairobi", "DSS": "dakar", "ACC": "accra",
    "LOS": "lagos", "JNB": "johannesburg", "CPT": "cape-town",
    "ADD": "addis-ababa", "DAR": "dar-es-salaam",
    # Asia-Pacific
    "NRT": "tokyo", "HND": "tokyo", "KIX": "osaka",
    "ICN": "seoul", "PEK": "beijing", "PVG": "shanghai",
    "HKG": "hong-kong", "TPE": "taipei",
    "SIN": "singapore", "KUL": "kuala-lumpur",
    "BKK": "bangkok", "HAN": "hanoi", "SGN": "ho-chi-minh-city",
    "CGK": "jakarta", "DPS": "bali",
    "DEL": "delhi", "BOM": "mumbai", "BLR": "bengaluru",
    "SYD": "sydney", "MEL": "melbourne",
    "AKL": "auckland",
}

_AIRPORT_API = "https://openair-california.airtrfx.com/hangar-service/v2/af/airports/search"
_EM_API_KEY = "HeQpRjsFI5xlAaSx2onkjc1HTK0ukqA1IrVvd5fvaMhNtzLTxInTpeYB1MK93pah"

_slug_cache: dict[str, str] = {}
_slug_cache_loaded = False


def _load_slug_cache_sync() -> None:
    global _slug_cache, _slug_cache_loaded
    if _slug_cache_loaded:
        return
    try:
        sess = creq.Session(impersonate="chrome124")
        r = sess.post(
            _AIRPORT_API,
            json={"language": "en", "siteEdition": "en-nl"},
            headers={
                "em-api-key": _EM_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if r.status_code == 200:
            for ap in r.json():
                iata = ap.get("iataCode", "")
                city = ap.get("city", {}).get("name", "")
                if iata and city:
                    _slug_cache[iata] = city.lower().replace(" ", "-")
            logger.info("AF: cached %d airport slugs", len(_slug_cache))
    except Exception as e:
        logger.warning("AF: airport cache load failed: %s", e)
    _slug_cache_loaded = True


def _resolve_slug(iata: str) -> str | None:
    slug = _IATA_TO_SLUG.get(iata)
    if slug:
        return slug
    if not _slug_cache_loaded:
        _load_slug_cache_sync()
    return _slug_cache.get(iata)


class AirfranceConnectorClient:
    """Air France — EveryMundo airTRFX route pages via curl_cffi."""

    def __init__(self, timeout: float = 25.0):
        self.timeout = timeout

    async def close(self):
        pass

    async def search_flights(self, req: FlightSearchRequest) -> FlightSearchResponse:
        t0 = time.monotonic()

        origin_slug = _resolve_slug(req.origin)
        dest_slug = _resolve_slug(req.destination)
        if not origin_slug or not dest_slug:
            logger.warning("AF: unmapped IATA %s or %s", req.origin, req.destination)
            return self._empty(req)

        html = None
        for base, edition in _MARKETS:
            url = f"{base}/{edition}/flights-from-{origin_slug}-to-{dest_slug}"
            logger.info("AF: trying %s", url)
            try:
                html = await asyncio.get_event_loop().run_in_executor(
                    None, self._fetch_sync, url
                )
            except Exception as e:
                logger.debug("AF: %s failed: %s", base, e)
            if html:
                break

        if not html:
            return self._empty(req)

        offers = self._extract_offers(html, req)
        offers.sort(key=lambda o: o.price if o.price > 0 else float("inf"))

        elapsed = time.monotonic() - t0
        logger.info(
            "AF %s→%s: %d offers in %.1fs",
            req.origin, req.destination, len(offers), elapsed,
        )

        h = hashlib.md5(
            f"af{req.origin}{req.destination}{req.date_from}".encode()
        ).hexdigest()[:12]
        return FlightSearchResponse(
            search_id=f"fs_{h}",
            origin=req.origin,
            destination=req.destination,
            currency=offers[0].currency if offers else "EUR",
            offers=offers,
            total_results=len(offers),
        )

    def _fetch_sync(self, url: str) -> str | None:
        sess = creq.Session(impersonate="chrome124")
        try:
            r = sess.get(url, headers=_HEADERS, timeout=int(self.timeout))
            if r.status_code != 200:
                logger.warning("AF: %s returned %d", url, r.status_code)
                return None
            return r.text
        except Exception as e:
            logger.warning("AF curl_cffi error: %s", e)
            return None

    def _extract_offers(
        self, html: str, req: FlightSearchRequest
    ) -> list[FlightOffer]:
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        if not m:
            logger.info("AF: no __NEXT_DATA__ found")
            return []

        try:
            nd = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            logger.warning("AF: __NEXT_DATA__ JSON parse failed")
            return []

        props = nd.get("props", {}).get("pageProps", {})
        apollo = props.get("apolloState", {}).get("data", {})

        offers: list[FlightOffer] = []
        target_date = req.date_from.strftime("%Y-%m-%d")

        for key, val in apollo.items():
            if not isinstance(val, dict):
                continue
            if val.get("__typename") != "DpaHeadline":
                continue

            meta = val.get("metaData", {})
            if not isinstance(meta, dict):
                continue

            headline = meta.get("headline", {})
            if not isinstance(headline, dict):
                continue

            lowest_fare = headline.get("lowestFare", {})
            if not isinstance(lowest_fare, dict):
                continue

            offer = self._build_offer_from_fare(lowest_fare, req, target_date)
            if offer:
                offers.append(offer)

        return offers

    def _build_offer_from_fare(
        self,
        fare: dict,
        req: FlightSearchRequest,
        target_date: str,
    ) -> FlightOffer | None:
        price = fare.get("totalPrice")
        if not price:
            return None
        try:
            price_f = round(float(price), 2)
        except (ValueError, TypeError):
            return None
        if price_f <= 0:
            return None

        dep_date_str = fare.get("departureDate", "")[:10]
        if not dep_date_str:
            return None

        currency = fare.get("currencyCode") or "EUR"
        origin_code = fare.get("originAirportCode") or req.origin
        dest_code = fare.get("destinationAirportCode") or req.destination
        cabin = (fare.get("formattedTravelClass") or "Economy").lower()

        try:
            dep_dt = datetime.strptime(dep_date_str, "%Y-%m-%d")
        except ValueError:
            dep_dt = datetime(2000, 1, 1)

        seg = FlightSegment(
            airline="AF",
            airline_name="Air France",
            flight_no="",
            origin=origin_code,
            destination=dest_code,
            origin_city="",
            destination_city="",
            departure=dep_dt,
            arrival=dep_dt,
            duration_seconds=0,
            cabin_class=cabin,
        )
        route = FlightRoute(segments=[seg], total_duration_seconds=0, stopovers=0)

        fid = hashlib.md5(
            f"af_{origin_code}{dest_code}{dep_date_str}{price_f}{cabin}".encode()
        ).hexdigest()[:12]

        return FlightOffer(
            id=f"af_{fid}",
            price=price_f,
            currency=currency,
            price_formatted=(
                fare.get("formattedTotalPrice") or f"{price_f:.2f} {currency}"
            ),
            outbound=route,
            inbound=None,
            airlines=["Air France"],
            owner_airline="AF",
            booking_url=(
                f"https://wwws.airfrance.nl/search/offers"
                f"?origin={req.origin}&destination={req.destination}"
                f"&outboundDate={target_date}"
                f"&adultCount={req.adults or 1}&tripType=ONE_WAY"
            ),
            is_locked=False,
            source="airfrance_direct",
            source_tier="free",
        )

    @staticmethod
    def _empty(req: FlightSearchRequest) -> FlightSearchResponse:
        h = hashlib.md5(
            f"af{req.origin}{req.destination}{req.date_from}".encode()
        ).hexdigest()[:12]
        return FlightSearchResponse(
            search_id=f"fs_{h}",
            origin=req.origin,
            destination=req.destination,
            currency="EUR",
            offers=[],
            total_results=0,
        )
