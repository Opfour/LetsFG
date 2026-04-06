"""EgyptAir connector — BLOCKED (Imperva WAF + hCaptcha).

EgyptAir (IATA: MS) — CAI hub, Star Alliance.

Blocked reason:
  - egyptair.com flight search form (ASP.NET WebForms) works — can fill
    origin, destination, date and submit successfully.
  - Form submission redirects to onlinebooking.egyptair.com which is
    protected by Imperva WAF (IP 45.60.154.91) with hCaptcha
    (sitekey e94865c2-4231-4c25-9c6e-2b797b2b56cf).
  - Results page shows "Pardon our interruption" and blocks all
    automation, including headed Chrome with stealth patches.
  - No accessible API endpoints found.
  - Probed extensively: 2026-03-16, re-investigated 2026-04-05.
"""

from __future__ import annotations

import logging

from ..models.flights import FlightSearchRequest, FlightSearchResponse

logger = logging.getLogger(__name__)


class EgyptAirConnectorClient:
    """EgyptAir — BLOCKED (Imperva WAF + hCaptcha on results page)."""

    def __init__(self, timeout: float = 20.0):
        pass

    async def close(self):
        pass

    async def search_flights(self, req: FlightSearchRequest) -> FlightSearchResponse:
        logger.debug("EgyptAir connector blocked — no accessible booking page")
        return FlightSearchResponse(
            search_id="fs_blocked", origin=req.origin, destination=req.destination,
            currency="EGP", offers=[], total_results=0,
        )
