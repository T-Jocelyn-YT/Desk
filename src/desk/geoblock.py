"""Startup geoblock check against https://polymarket.com/api/geoblock."""

from __future__ import annotations

from dataclasses import dataclass


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


class GeoblockError(Exception):
    """The process must not run."""


@dataclass(frozen=True)
class GeoblockStatus:
    blocked: bool
    country: str | None
    region: str | None


def parse_geoblock(payload: object) -> GeoblockStatus:
    """Accept a geoblock JSON object.

    The IP address is discarded here and is not part of the status, so
    later logs cannot print it.
    """

    if not isinstance(payload, dict) or "blocked" not in payload:
        raise GeoblockError("Refusing to run: Polymarket geoblock response was not usable.")
    blocked = payload["blocked"]
    if not isinstance(blocked, bool):
        raise GeoblockError("Refusing to run: Polymarket geoblock response was not usable.")
    country = payload.get("country")
    region = payload.get("region")
    status = GeoblockStatus(
        blocked=blocked,
        country=country if isinstance(country, str) else None,
        region=region if isinstance(region, str) else None,
    )
    if status.blocked:
        raise GeoblockError("Refusing to run: Polymarket geoblock reports this host as blocked.")
    return status
