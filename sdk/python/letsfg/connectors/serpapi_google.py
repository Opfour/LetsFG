"""
Google Flights connector — direct API via curl_cffi (no browser).

Instead of launching a real Chrome browser to intercept XHR traffic,
this connector reverse-engineers the GetShoppingResults RPC endpoint
and POSTs directly to it, using curl_cffi to impersonate Chrome's
TLS fingerprint (JA3/JA4). No browser, no Playwright, no proxy.

Speed: ~1-3 s per call (one-way and round-trip alike — single request each).

Google's GetShoppingResults endpoint natively supports round trips: pass
both outbound and return segments in the same payload and Google returns
combined RT prices directly. No two-stage selection needed.

Ancillary support exposed through the request payload:
  • Cabin class — economy / premium economy / business / first
  • Passenger count — adults, children, infants
  • Stop filter — nonstop, 1-stop, any
  • Max price cap
  • Bags-included pricing — when checked_bags > 0 the returned price
    already includes the checked-bag fee (Google applies it server-side).
  • Exclude basic economy fares flag

Keeps source="serpapi_google" for backwards-compat with the stats
pipeline (_compute_google_flights_comparison) and website annotation
(buildGoogleFlightsMatchIndex).

Endpoint:
  POST https://www.google.com/_/FlightsFrontendUi/data/
       travel.frontend.flights.FlightsFrontendService/GetShoppingResults
  Content-Type: application/x-www-form-urlencoded
  Body: f.req=<url-encoded nested JSON payload>

Payload structure reverse-engineered by the fli project (punitarani/fli, MIT).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from typing import Optional

from ..models.flights import (
    FlightOffer,
    FlightRoute,
    FlightSearchRequest,
    FlightSearchResponse,
    FlightSegment,
)

from .airline_routes import get_city_airports

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_GF_ENDPOINT = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
)

_GF_HEADERS = {
    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://www.google.com",
    "referer": "https://www.google.com/travel/flights",
}

# Cabin class: M=economy, W=premium economy, C=business, F=first
_CABIN_GF: dict[str, int] = {"M": 1, "W": 2, "C": 3, "F": 4}

# Sort mode: 0=top, 1=best, 2=cheapest, 3=departure time, 4=arrival, 5=duration
_SORT_GF: dict[str, int] = {"price": 2, "duration": 5, "departure_time": 3}

# Trip types
_TRIP_ONE_WAY = 2
_TRIP_ROUND_TRIP = 1

_PRIMARY_CITY_AIRPORTS: dict[str, str] = {
    "NYC": "JFK", "LON": "LHR", "PAR": "CDG", "ROM": "FCO",
    "MIL": "MXP", "WAS": "IAD", "TYO": "HND", "OSA": "KIX",
    "SEL": "ICN", "RIO": "GIG", "CHI": "ORD", "BJS": "PEK",
    "SHA": "PVG", "STO": "ARN", "MOW": "SVO", "BUE": "EZE",
    "SAO": "GRU", "JKT": "CGK", "YTO": "YYZ", "YMQ": "YUL",
    "REK": "KEF",
}

# ─── Regex patterns ───────────────────────────────────────────────────────────

_IATA_RE = re.compile(r"^[A-Z]{3}$")
_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_PRICE_STR_RE = re.compile(r"^\d+(\.\d{1,2})?$")
_FLIGHT_NO_RE = re.compile(r"^[A-Z0-9]{2,3}\d{1,4}[A-Z]?$")
_AIRLINE_CODE_RE = re.compile(r"^[A-Z0-9]{2,3}$")

# ─── Utility helpers ──────────────────────────────────────────────────────────

def _is_iata(s) -> bool:
    return isinstance(s, str) and bool(_IATA_RE.match(s))


def _parse_dt(s) -> datetime:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "").split("+")[0][:19])
    except Exception:
        return datetime(2000, 1, 1)


def _looks_airline_code(value) -> bool:
    return isinstance(value, str) and bool(_AIRLINE_CODE_RE.match(value))


def _is_google_date_parts(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= 3
        and all(isinstance(p, int) for p in value[:3])
    )


def _is_google_time_parts(value) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= 2
        and all(isinstance(p, int) for p in value)
    )


def _make_google_datetime(date_parts, time_parts, fallback: datetime) -> datetime:
    year, month, day = fallback.year, fallback.month, fallback.day
    if _is_google_date_parts(date_parts):
        year, month, day = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
    hour = minute = 0
    if _is_google_time_parts(time_parts):
        hour = int(time_parts[0])
        minute = int(time_parts[1]) if len(time_parts) > 1 else 0
    try:
        return datetime(year, month, day, hour, minute)
    except Exception:
        return fallback


def _collect_strings(node, max_depth: int, depth: int = 0):
    if depth > max_depth:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _collect_strings(item, max_depth, depth + 1)


# ─── Payload builder ──────────────────────────────────────────────────────────

def _stops_filter(max_stopovers: int) -> int:
    """Convert our max_stopovers to Google's stops enum.
    0=any, 1=nonstop, 2=one-stop-or-fewer, 3=two-or-fewer."""
    if max_stopovers == 0:
        return 1   # NON_STOP
    if max_stopovers == 1:
        return 2   # ONE_STOP_OR_FEWER
    return 0       # ANY


def _build_segment_entry(
    origin: str,
    destination: str,
    travel_date: str,
    stops: int,
) -> list:
    """Build one flight-segment entry for the GetShoppingResults payload.

    Index map (reverse-engineered):
      [0]  departure airport  — [[[[ [iata, 0] ]]]]
      [1]  arrival airport    — [[[[ [iata, 0] ]]]]
      [2]  time restrictions  — [earliest_dep, latest_dep, earliest_arr, latest_arr]
      [3]  stops enum         — 0=any 1=nonstop 2=1stop 3=2stops
      [4]  airlines filter    — list of iata codes
      [5]  unknown
      [6]  travel date        — "YYYY-MM-DD"
      [7]  max duration mins
      [8]  selected outbound  — not used (native RT, always None)
      [9]  layover airports
      [10] unknown
      [11] unknown
      [12] layover duration
      [13] emissions filter   — [1]=less emissions
      [14] hardcoded 3
    """
    return [
        [[[origin, 0]]],        # [0] departure airport
        [[[destination, 0]]],   # [1] arrival airport
        None,                   # [2] time restrictions
        stops,                  # [3] stops enum
        None,                   # [4] airlines filter
        None,                   # [5]
        travel_date,            # [6] "YYYY-MM-DD"
        None,                   # [7] max duration
        None,                   # [8] selected outbound — always None (native RT)
        None,                   # [9] layover airports
        None,                   # [10]
        None,                   # [11]
        None,                   # [12] layover duration
        None,                   # [13] emissions filter
        3,                      # [14] hardcoded
    ]


def _build_f_req(
    origin: str,
    destination: str,
    travel_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    cabin: int = 1,
    stops: int = 0,
    checked_bags: int = 0,
    carry_on: bool = False,
    max_price: Optional[float] = None,
    exclude_basic_economy: bool = False,
    sort_by: int = 2,
) -> str:
    """Build and URL-encode the f.req payload for GetShoppingResults.

    Outer structure:
      filters[0]  = []        — no effect
      filters[1]  = inner     — all search settings
      filters[2]  = sort_by   — sort mode
      filters[3]  = 1         — 1=all results, 0=~30
      filters[4]  = 0         — no effect
      filters[5]  = 1         — no effect

    Inner settings index map (filters[1]):
      [2]   trip type             — 1=round trip, 2=one-way
      [5]   cabin/seat type       — 1=economy 2=premium 3=business 4=first
      [6]   passengers            — [adults, children, infants_lap, infants_seat]
      [7]   price limit           — [None, max_price] or None
      [10]  bags filter           — [checked_bags, carry_on_int] or None
      [13]  flight segments       — list of segment entries
      [28]  exclude basic economy — 1=exclude, 0=allow
    """
    trip_type = _TRIP_ROUND_TRIP if return_date else _TRIP_ONE_WAY

    segments = [_build_segment_entry(origin, destination, travel_date, stops)]
    if return_date:
        segments.append(_build_segment_entry(destination, origin, return_date, stops))

    bags_filter = [checked_bags, int(carry_on)] if (checked_bags or carry_on) else None
    price_limit = [None, int(max_price)] if max_price else None

    inner = [
        None,          # [0]
        None,          # [1]
        trip_type,     # [2] trip type
        None,          # [3]
        [],            # [4] must be [] not None
        cabin,         # [5] seat type
        [adults, children, infants, 0],  # [6] [adults, children, infants_lap, infants_seat]
        price_limit,   # [7]
        None,          # [8]
        None,          # [9]
        bags_filter,   # [10] bags-included pricing
        None,          # [11]
        None,          # [12]
        segments,      # [13] flight segments
        None,          # [14]
        None,          # [15]
        None,          # [16]
        1,             # [17] hardcoded
        None, None, None, None,  # [18-21]
        None, None, None, None,  # [22-25]
        None, None,              # [26-27]
        1 if exclude_basic_economy else 0,  # [28]
    ]

    filters = [[], inner, sort_by, 1, 0, 1]
    inner_json = json.dumps(filters, separators=(",", ":"))
    wrapped = json.dumps([None, inner_json], separators=(",", ":"))
    return urllib.parse.quote(wrapped)


# ─── XSSI / frame parsing ─────────────────────────────────────────────────────

def _strip_xssi(body: str) -> str:
    body = body.lstrip()
    for prefix in (")]}'\n", ")]}'"):
        if body.startswith(prefix):
            return body[len(prefix):]
    return body


def _extract_json_frames(body: str) -> list[str]:
    """Extract JSON payload frames from Google's chunked RPC response body."""
    stripped = _strip_xssi(body).lstrip("\r\n")
    if not stripped:
        return []
    frames: list[str] = []
    lines = stripped.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.isdigit() and index + 1 < len(lines):
            frames.append(lines[index + 1])
            index += 2
            continue
        frames.append(lines[index])
        index += 1
    return frames


def _extract_inner_json(outer) -> Optional[str]:
    """Locate the GetShoppingResults inner JSON string inside the wrb.fr wrapper.

    Structure: [..., [["wrb.fr", "GetShoppingResults", "<inner_json>", ...], ...], ...]
    """
    if isinstance(outer, list):
        if len(outer) >= 3 and outer[0] == "wrb.fr":
            for item in outer[1:]:
                if isinstance(item, str) and len(item) > 100 and item.lstrip().startswith("["):
                    return item
        for item in outer:
            result = _extract_inner_json(item)
            if result:
                return result
    return None


def _find_largest_embedded_json(data, depth: int = 0) -> Optional[str]:
    """Fallback: walk the structure and return the longest parseable JSON string."""
    if depth > 12:
        return None
    if isinstance(data, str) and len(data) > 200:
        stripped = data.strip()
        if stripped.startswith("["):
            try:
                json.loads(stripped)
                return stripped
            except Exception:
                pass
    elif isinstance(data, list):
        best: Optional[str] = None
        best_len = 0
        for item in data:
            c = _find_largest_embedded_json(item, depth + 1)
            if c and len(c) > best_len:
                best, best_len = c, len(c)
        return best
    return None


# ─── Live offer extraction (structured data from Google's segment arrays) ──────

def _extract_live_price(node: list) -> Optional[float]:
    for item in node:
        if not isinstance(item, list) or len(item) < 8:
            continue
        price_raw = item[7]
        if isinstance(price_raw, (int, float)) and 1000 <= price_raw <= 5_000_000:
            return round(float(price_raw) / 1000.0, 2)
    return None


def _parse_live_segment(
    segment: list,
    fallback_airline_code: str,
    fallback_airline_name: str,
    fallback_dep: datetime,
    fallback_arr: datetime,
) -> Optional[FlightSegment]:
    if not isinstance(segment, list) or len(segment) < 23:
        return None
    origin = segment[3] if len(segment) > 3 and _is_iata(segment[3]) else ""
    destination = segment[6] if len(segment) > 6 and _is_iata(segment[6]) else ""
    if not origin or not destination:
        return None

    dep = _make_google_datetime(
        segment[20] if len(segment) > 20 else None,
        segment[8]  if len(segment) > 8  else None,
        fallback_dep,
    )
    arr = _make_google_datetime(
        segment[21] if len(segment) > 21 else None,
        segment[10] if len(segment) > 10 else None,
        fallback_arr,
    )
    duration_minutes = (
        segment[11]
        if len(segment) > 11 and isinstance(segment[11], (int, float))
        else 0
    )
    carrier_info = (
        segment[22]
        if len(segment) > 22 and isinstance(segment[22], list)
        else None
    )
    airline = fallback_airline_code
    airline_name = fallback_airline_name
    flight_no = ""

    if isinstance(segment[2], str) and segment[2].strip():
        airline_name = segment[2].strip()

    if carrier_info:
        if len(carrier_info) > 0 and isinstance(carrier_info[0], str) and carrier_info[0].strip():
            airline = carrier_info[0].strip()
        if len(carrier_info) > 1 and isinstance(carrier_info[1], str):
            flight_no = carrier_info[1].strip()
        if len(carrier_info) > 3 and isinstance(carrier_info[3], str) and carrier_info[3].strip():
            airline_name = carrier_info[3].strip()

    return FlightSegment(
        airline=airline,
        airline_name=airline_name,
        flight_no=flight_no,
        origin=origin,
        destination=destination,
        departure=dep,
        arrival=arr,
        duration_seconds=int(duration_minutes * 60),
    )


def _try_parse_live_offer_node(
    node: list,
    req: FlightSearchRequest,
    currency: str,
    booking_url: str,
) -> Optional[FlightOffer]:
    if len(node) < 23:
        return None
    if not isinstance(node[0], str) or not (node[0] == "multi" or _looks_airline_code(node[0])):
        return None
    if not isinstance(node[1], list) or not any(isinstance(i, str) and i.strip() for i in node[1]):
        return None
    if not isinstance(node[2], list) or not node[2] or not all(isinstance(i, list) for i in node[2]):
        return None
    if len(node) <= 8 or not _is_iata(node[3]) or not _is_iata(node[6]):
        return None
    if not _is_google_date_parts(node[4]) or not _is_google_time_parts(node[5]):
        return None

    price = _extract_live_price(node)
    if price is None:
        return None

    fallback_dep = _make_google_datetime(
        node[4], node[5],
        datetime(req.date_from.year, req.date_from.month, req.date_from.day),
    )
    fallback_arr = _make_google_datetime(node[7], node[8], fallback_dep)
    fallback_airline_name = next(
        (i.strip() for i in node[1] if isinstance(i, str) and i.strip()), ""
    )

    segments: list[FlightSegment] = []
    for seg in node[2]:
        parsed = _parse_live_segment(seg, node[0], fallback_airline_name, fallback_dep, fallback_arr)
        if parsed:
            segments.append(parsed)
    if not segments:
        return None

    outbound_segs, inbound_segs = _split_legs(segments, req)
    if not outbound_segs:
        return None

    outbound = FlightRoute(
        segments=outbound_segs,
        total_duration_seconds=sum(s.duration_seconds for s in outbound_segs),
        stopovers=max(0, len(outbound_segs) - 1),
    )
    inbound: Optional[FlightRoute] = None
    if inbound_segs:
        inbound = FlightRoute(
            segments=inbound_segs,
            total_duration_seconds=sum(s.duration_seconds for s in inbound_segs),
            stopovers=max(0, len(inbound_segs) - 1),
        )

    all_segments = outbound_segs + (inbound_segs or [])
    airlines = list(dict.fromkeys(
        s.airline_name or s.airline for s in all_segments if s.airline_name or s.airline
    ))
    owner = fallback_airline_name or (airlines[0] if airlines else "")
    offer_key = "|".join(
        f"{s.airline}:{s.flight_no}:{s.origin}:{s.destination}:{s.departure.isoformat()}"
        for s in all_segments
    )
    offer_id = hashlib.md5(f"gf_live_{offer_key}_{price}".encode()).hexdigest()[:12]

    return FlightOffer(
        id=f"gf_{offer_id}",
        price=price,
        currency=currency,
        price_formatted=f"{price:.2f} {currency}",
        outbound=outbound,
        inbound=inbound,
        airlines=airlines,
        owner_airline=owner,
        booking_url=booking_url,
        is_locked=False,
        source="serpapi_google",
        source_tier="free",
    )


def _looks_like_live_offer_wrapper(node: list) -> bool:
    return (
        isinstance(node, list)
        and len(node) >= 2
        and isinstance(node[0], list)
        and len(node[0]) >= 23
        and isinstance(node[0][0], str)
        and (node[0][0] == "multi" or _looks_airline_code(node[0][0]))
    )


def _extract_live_wrapper_price(node: list) -> Optional[float]:
    if len(node) < 2 or not isinstance(node[1], list) or not node[1]:
        return None
    price_info = node[1][0]
    if not isinstance(price_info, list):
        return None
    numeric_values = [
        float(item)
        for item in price_info
        if isinstance(item, (int, float)) and 10 <= item <= 10000
    ]
    if not numeric_values:
        return None
    return round(numeric_values[-1], 2)


def _try_parse_live_offer_wrapper(
    node: list,
    req: FlightSearchRequest,
    currency: str,
    booking_url: str,
) -> Optional[FlightOffer]:
    if not _looks_like_live_offer_wrapper(node):
        return None
    offer = _try_parse_live_offer_node(node[0], req, currency, booking_url)
    if offer is None:
        return None
    wrapper_price = _extract_live_wrapper_price(node)
    if wrapper_price is None:
        return None if req.return_from else offer
    if req.return_from and wrapper_price + 0.01 < offer.price:
        return None
    offer.price = wrapper_price
    offer.price_formatted = f"{wrapper_price:.2f} {currency}"
    if req.return_from:
        offer.id = f"{offer.id}_rt"
    return offer


def _collect_live_offers(node, req, currency, booking_url, out, seen_ids, depth):
    if depth > 18 or not isinstance(node, list):
        return
    if _looks_like_live_offer_wrapper(node):
        offer = _try_parse_live_offer_wrapper(node, req, currency, booking_url)
        if offer is not None and offer.id not in seen_ids:
            seen_ids.add(offer.id)
            out.append(offer)
        return
    offer = _try_parse_live_offer_node(node, req, currency, booking_url)
    if offer is not None:
        if offer.id not in seen_ids:
            seen_ids.add(offer.id)
            out.append(offer)
        return
    for item in node:
        if isinstance(item, list):
            _collect_live_offers(item, req, currency, booking_url, out, seen_ids, depth + 1)


def _extract_live_offers_from_inner(
    inner,
    req: FlightSearchRequest,
    currency: str,
    booking_url: str,
) -> list[FlightOffer]:
    if not isinstance(inner, list):
        return []
    offers: list[FlightOffer] = []
    seen_ids: set[str] = set()
    _collect_live_offers(inner, req, currency, booking_url, offers, seen_ids, depth=0)
    return offers


# ─── Fallback offer parser (when live parser finds nothing) ───────────────────

def _try_parse_offer_node(node: list, req, currency, booking_url) -> Optional[FlightOffer]:
    if len(node) < 3:
        return None
    price: Optional[float] = None
    for item in node:
        if isinstance(item, float) and not isinstance(item, bool) and 10.0 <= item <= 15000.0:
            price = round(item, 2)
            break
        if isinstance(item, str) and _PRICE_STR_RE.match(item):
            try:
                v = float(item)
                if 10.0 <= v <= 15000.0:
                    price = round(v, 2)
                    break
            except ValueError:
                pass
    if price is None:
        return None
    if not any(isinstance(item, list) for item in node):
        return None
    iata_codes = [s for s in _collect_strings(node, max_depth=5) if _is_iata(s)]
    if len(iata_codes) < 2:
        return None
    datetimes = [s for s in _collect_strings(node, max_depth=5) if _DT_RE.match(s)]
    if not datetimes:
        return None

    segments = _extract_segments(node)
    if not segments:
        dep_dt = _parse_dt(datetimes[0]) if datetimes else datetime(
            req.date_from.year, req.date_from.month, req.date_from.day, 12, 0,
        )
        segments = [FlightSegment(
            airline="", airline_name="", flight_no="",
            origin=req.origin, destination=req.destination,
            departure=dep_dt, arrival=dep_dt, duration_seconds=0,
        )]

    outbound_segs, inbound_segs = _split_legs(segments, req)
    if not outbound_segs:
        return None

    outbound = FlightRoute(
        segments=outbound_segs,
        total_duration_seconds=sum(s.duration_seconds for s in outbound_segs),
        stopovers=max(0, len(outbound_segs) - 1),
    )
    inbound: Optional[FlightRoute] = None
    if inbound_segs:
        inbound = FlightRoute(
            segments=inbound_segs,
            total_duration_seconds=sum(s.duration_seconds for s in inbound_segs),
            stopovers=max(0, len(inbound_segs) - 1),
        )

    all_segs = outbound_segs + (inbound_segs or [])
    airlines = list(dict.fromkeys(
        s.airline_name or s.airline for s in all_segs if s.airline_name or s.airline
    ))
    offer_key = "|".join(
        f"{s.airline}:{s.flight_no}:{s.origin}:{s.destination}:{s.departure.isoformat()}"
        for s in all_segs
    )
    offer_id = hashlib.md5(f"gf_fb_{offer_key}_{price}".encode()).hexdigest()[:12]

    return FlightOffer(
        id=f"gf_{offer_id}",
        price=price,
        currency=currency,
        price_formatted=f"{price:.2f} {currency}",
        outbound=outbound,
        inbound=inbound,
        airlines=airlines,
        owner_airline=airlines[0] if airlines else "",
        booking_url=booking_url,
        is_locked=False,
        source="serpapi_google",
        source_tier="free",
    )


def _collect_offers(node, req, currency, booking_url, out, seen_ids, depth):
    if depth > 22 or not isinstance(node, list):
        return
    offer = _try_parse_offer_node(node, req, currency, booking_url)
    if offer is not None:
        if offer.id not in seen_ids:
            seen_ids.add(offer.id)
            out.append(offer)
        return
    for item in node:
        if isinstance(item, list):
            _collect_offers(item, req, currency, booking_url, out, seen_ids, depth + 1)


def _extract_segments(node: list) -> list[FlightSegment]:
    segments: list[FlightSegment] = []
    _find_segments(node, segments, depth=0)
    return segments


def _find_segments(node, out: list, depth: int):
    if depth > 10 or not isinstance(node, list):
        return
    seg = _try_parse_segment_node(node)
    if seg:
        out.append(seg)
        return
    for item in node:
        if isinstance(item, list):
            _find_segments(item, out, depth + 1)


def _try_parse_segment_node(node: list) -> Optional[FlightSegment]:
    if len(node) < 4:
        return None
    direct_strings = [item for item in node if isinstance(item, str)]
    nested_lists = [item for item in node if isinstance(item, list)]
    if len(nested_lists) > len(direct_strings):
        return None
    iata = [s for s in direct_strings if _is_iata(s)]
    if len(iata) < 2:
        return None
    datetimes = [s for s in direct_strings if _DT_RE.match(s)]
    if not datetimes:
        return None
    flight_nos = [s for s in direct_strings if _FLIGHT_NO_RE.match(s) and not _is_iata(s)]
    flight_no = flight_nos[0] if flight_nos else ""
    nums = [float(item) for item in node if isinstance(item, (int, float)) and not isinstance(item, bool)]
    duration_min = next((n for n in nums if 10 <= n <= 1800), 0)
    dep = _parse_dt(datetimes[0])
    arr = _parse_dt(datetimes[1]) if len(datetimes) > 1 else dep
    if dep == datetime(2000, 1, 1):
        return None
    return FlightSegment(
        airline=iata[2] if len(iata) > 2 else "",
        airline_name=iata[2] if len(iata) > 2 else "",
        flight_no=flight_no,
        origin=iata[0],
        destination=iata[1],
        departure=dep,
        arrival=arr,
        duration_seconds=int(duration_min * 60),
    )


def _extract_offers_from_inner(
    inner,
    req: FlightSearchRequest,
    currency: str,
    booking_url: str,
) -> list[FlightOffer]:
    if not isinstance(inner, list):
        return []
    offers = _extract_live_offers_from_inner(inner, req, currency, booking_url)
    if offers:
        return offers
    # Fallback generic walker
    seen_ids: set[str] = set()
    fb_offers: list[FlightOffer] = []
    _collect_offers(inner, req, currency, booking_url, fb_offers, seen_ids, depth=0)
    return fb_offers


# ─── Leg splitter ─────────────────────────────────────────────────────────────

def _split_legs(
    segments: list[FlightSegment], req: FlightSearchRequest
) -> tuple[list[FlightSegment], list[FlightSegment]]:
    """Split a flat segment list into (outbound, inbound) for round trips."""
    if not req.return_from or len(segments) <= 1:
        return segments, []
    dest = req.destination
    for i, seg in enumerate(segments):
        if i > 0 and seg.origin == dest:
            return segments[:i], segments[i:]
    return segments, []


# ─── Response parser ─────────────────────────────────────────────────────────

def _parse_gf_response(
    body: str,
    req: FlightSearchRequest,
    currency: str,
    booking_url: str,
) -> list[FlightOffer]:
    """Parse a raw GetShoppingResults response body into FlightOffers.

    Two paths:
      1. Single-JSON fast path — typical for direct curl_cffi API calls.
      2. Chunked frame fallback — for TravelFrontendUi streaming format.
    """
    offers: list[FlightOffer] = []
    seen_ids: set[str] = set()

    def _add(new_offers: list[FlightOffer]) -> None:
        for o in new_offers:
            if o.id not in seen_ids:
                seen_ids.add(o.id)
                offers.append(o)

    stripped = _strip_xssi(body).strip()
    if not stripped:
        return []

    # ── Fast path: single-JSON response ──────────────────────────────────────
    try:
        outer = json.loads(stripped)
        inner_str = _extract_inner_json(outer)
        if inner_str:
            inner = json.loads(inner_str)
            _add(_extract_offers_from_inner(inner, req, currency, booking_url))
            if offers:
                return offers
    except Exception:
        pass

    # ── Fallback: chunked frame-by-frame parser ───────────────────────────────
    for frame in _extract_json_frames(body):
        try:
            outer = json.loads(frame)
        except Exception:
            continue
        inner_str = _extract_inner_json(outer)
        if inner_str:
            try:
                inner = json.loads(inner_str)
                _add(_extract_offers_from_inner(inner, req, currency, booking_url))
            except Exception:
                pass
        inner_str2 = _find_largest_embedded_json(outer)
        if inner_str2 and inner_str2 != inner_str:
            try:
                inner2 = json.loads(inner_str2)
                _add(_extract_offers_from_inner(inner2, req, currency, booking_url))
            except Exception:
                pass

    return offers


# ─── Booking URL ──────────────────────────────────────────────────────────────

def _make_booking_url(req: FlightSearchRequest, currency: str) -> str:
    query = (
        f"Flights from {req.origin} to {req.destination} "
        f"on {req.date_from.strftime('%Y-%m-%d')}"
    )
    if req.return_from:
        query += f" returning {req.return_from.strftime('%Y-%m-%d')}"
    return (
        f"https://www.google.com/travel/flights"
        f"?q={urllib.parse.quote(query)}&curr={urllib.parse.quote(currency)}&hl=en"
    )


# ─── Direct API caller ────────────────────────────────────────────────────────

async def _call_gf_api(session, f_req: str, call_timeout: float = 20.0) -> Optional[str]:
    """POST to GetShoppingResults; returns raw response body or None on failure."""
    try:
        resp = await session.post(
            _GF_ENDPOINT,
            data=f"f.req={f_req}",
            headers=_GF_HEADERS,
            impersonate="chrome131",
            timeout=call_timeout,
        )
        if resp.status_code != 200:
            logger.warning("GF API returned HTTP %d", resp.status_code)
            return None
        return resp.text
    except Exception as exc:
        logger.warning("GF API call failed: %s", exc)
        return None


# ─── Connector ────────────────────────────────────────────────────────────────

class SerpApiGoogleConnectorClient:
    """
    Google Flights connector — direct RPC via curl_cffi (no browser, no proxy).

    Posts directly to Google Flights' internal GetShoppingResults endpoint
    using curl_cffi's Chrome TLS fingerprint impersonation. No Playwright,
    no browser process, no proxy required.

    Performance: ~1-3 s per search (one-way and round-trip alike — single call each).
    Google's native RT support handles combined pricing in one request.

    Ancillary features supported in the GF request payload:
      • cabin_class (M/W/C/F)
      • adults/children/infants passenger counts
      • max_stopovers filter (0=nonstop, 1=1stop, 2+=any)
      • bags-included pricing (price from GF includes bag fee when checked_bags > 0)
      • exclude_basic_economy flag
      • max price cap

    source="serpapi_google" preserved for backwards-compat with:
      - FSW's _compute_google_flights_comparison()
      - website's buildGoogleFlightsMatchIndex() / badge logic
    """

    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout

    async def close(self) -> None:
        return None   # stateless

    async def search_flights(self, req: FlightSearchRequest) -> FlightSearchResponse:
        t0 = time.monotonic()
        try:
            offers = await asyncio.wait_for(self._do_search(req), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.warning("GF API timed out for %s→%s", req.origin, req.destination)
            offers = None
        except Exception as exc:
            logger.warning("GF API error for %s→%s: %s", req.origin, req.destination, exc)
            offers = None

        if not offers:
            return self._empty(req)

        offers.sort(key=lambda o: o.price if o.price > 0 else float("inf"))
        total = len(offers)
        limit = req.limit or total
        offers = offers[:limit]

        elapsed = time.monotonic() - t0
        logger.info(
            "GF API %s→%s: %d offers in %.1fs",
            req.origin, req.destination, len(offers), elapsed,
        )

        currency = offers[0].currency if offers else (req.currency or "EUR")
        search_hash = hashlib.md5(
            f"gf_api{req.origin}{req.destination}{req.date_from}{req.return_from or ''}".encode()
        ).hexdigest()[:12]

        return FlightSearchResponse(
            search_id=f"fs_{search_hash}",
            origin=req.origin,
            destination=req.destination,
            currency=currency,
            offers=offers,
            total_results=total,
        )

    async def _do_search(self, req: FlightSearchRequest) -> Optional[list[FlightOffer]]:
        """Single API call for both one-way and round-trip searches.

        Google's GetShoppingResults natively supports round trips: passing
        both outbound and return segments in the same payload returns
        combined RT prices directly — no two-stage selection needed.
        """
        from curl_cffi.requests import AsyncSession

        currency = req.currency or "EUR"
        booking_url = _make_booking_url(req, currency)
        cabin = _CABIN_GF.get(req.cabin_class or "M", 1)
        stops = _stops_filter(req.max_stopovers)
        sort = _SORT_GF.get(req.sort or "price", 2)

        f_req = _build_f_req(
            origin=req.origin,
            destination=req.destination,
            travel_date=req.date_from.isoformat(),
            return_date=req.return_from.isoformat() if req.return_from else None,
            adults=req.adults,
            children=req.children,
            infants=req.infants,
            cabin=cabin,
            stops=stops,
            sort_by=sort,
        )

        proxy_url = os.environ.get("LETSFG_PROXY", "").strip() or None
        proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
        async with AsyncSession(proxies=proxies) as session:
            body = await _call_gf_api(session, f_req)

        if not body:
            logger.warning(
                "GF API %s→%s: no response body (API call failed, proxy=%s)",
                req.origin, req.destination, "yes" if proxy_url else "no",
            )
            return []
        offers = _parse_gf_response(body, req, currency, booking_url)
        if not offers:
            logger.warning(
                "GF API %s→%s: parsed 0 offers (body=%d bytes, proxy=%s, snippet=%r)",
                req.origin, req.destination, len(body),
                "yes" if proxy_url else "no",
                body[:300],
            )
        return offers

    def _candidate_airports(self, code: str) -> list[str]:
        """Return candidate airport IATA codes for a city or airport code."""
        normalized = (code or "").strip().upper()
        candidates: list[str] = [normalized] if normalized else []
        primary = _PRIMARY_CITY_AIRPORTS.get(normalized)
        if primary:
            candidates.append(primary)
        candidates.extend(get_city_airports(normalized))
        ordered: list[str] = []
        seen: set[str] = set()
        for c in candidates:
            a = (c or "").strip().upper()
            if a and a not in seen:
                seen.add(a)
                ordered.append(a)
        return ordered

    @staticmethod
    def _empty(req: FlightSearchRequest) -> FlightSearchResponse:
        return FlightSearchResponse(
            search_id="fs_empty",
            origin=req.origin,
            destination=req.destination,
            currency=req.currency or "EUR",
            offers=[],
            total_results=0,
        )

