"""Network adapters for paper mode. No credential is read or sent."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from desk.clob import CLOB_HOST, PyClobBooks, build_readonly_client
from desk.geoblock import GEOBLOCK_URL, GeoblockError

GAMMA_HOST = "https://gamma-api.polymarket.com"
USER_AGENT = "desk-paper/0.1"


class LiveRequestError(Exception):
    pass


def http_get_json(url: str, accept_json: bool = True) -> object:
    headers = {"User-Agent": USER_AGENT}
    if accept_json:
        headers["Accept"] = "application/json"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise LiveRequestError(type(exc).__name__) from exc
    return json.loads(body, parse_float=Decimal)


class LivePorts:
    """Gamma, CLOB market info, geoblock, and a read-only order book."""

    def __init__(self, books: PyClobBooks | None = None) -> None:
        self.books = books if books is not None else PyClobBooks(build_readonly_client())

    def geoblock(self) -> object:
        try:
            return http_get_json(GEOBLOCK_URL)
        except LiveRequestError as exc:
            raise GeoblockError(
                "Refusing to run: Polymarket geoblock check did not succeed."
            ) from exc

    def event(self, slug: str) -> dict | None:
        query = urllib.parse.urlencode({"slug": slug})
        payload = http_get_json(f"{GAMMA_HOST}/events?{query}")
        if isinstance(payload, list):
            if not payload:
                return None
            event = payload[0]
            return event if isinstance(event, dict) else None
        return payload if isinstance(payload, dict) else None

    def clob_market(self, condition_id: str) -> dict:
        payload = http_get_json(f"{CLOB_HOST}/clob-markets/{condition_id}")
        if not isinstance(payload, dict):
            raise LiveRequestError("clob market payload was not an object")
        return payload

    def order_book(self, token_id: str):
        return self.books.order_book(token_id)

    def settled_events(self) -> list:
        rows: list = []
        for offset in (0, 50):
            query = urllib.parse.urlencode(
                {
                    "series_slug": "eth-up-or-down-15m",
                    "closed": "true",
                    "limit": "50",
                    "offset": str(offset),
                    "order": "endDate",
                    "ascending": "false",
                }
            )
            # The history query is sent without an Accept header. A large
            # Gamma response has answered 406 when that header is set.
            payload = http_get_json(f"{GAMMA_HOST}/events?{query}", accept_json=False)
            if not isinstance(payload, list):
                raise LiveRequestError("settled-window payload was not a list")
            rows.extend(payload)
        return rows
