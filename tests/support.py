"""Fixture builders. Prices here are synthetic, not live observations."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from desk.fees import FeeSchedule
from desk.markets import RESOLUTION_SOURCE, SERIES_SLUG


ROOT = Path(__file__).resolve().parents[1]
RULES = (
    "Fetched rules: this market resolves Up when the Chainlink ETH/USD "
    "60-second TWAP at the end of the window is greater than or equal to "
    "the TWAP at the start. Otherwise it resolves Down."
)


def schedule(rate: str, exponent: str = "1") -> FeeSchedule:
    return FeeSchedule(
        rate=Decimal(rate),
        exponent=Decimal(exponent),
        taker_only=True,
        source="test",
    )


def eth_event(
    start: int,
    *,
    rate: str = "0.03",
    exponent: str = "1",
    min_size: str = "5",
    asset: str = "eth",
    duration: str = "15m",
    lookback: int = 60,
    twap_enabled: bool = True,
    price_to_beat: str | None = None,
    final_price: str | None = None,
    series: str = SERIES_SLUG,
    resolution: str = RESOLUTION_SOURCE,
    rules: str = RULES,
    fees_enabled: bool = True,
    condition: str = "condition-eth-15m",
    slug: str | None = None,
    outcomes: object | None = None,
    token_ids: object | None = None,
) -> dict:
    metadata: dict[str, str] = {}
    if price_to_beat is not None:
        metadata["priceToBeat"] = price_to_beat
    if final_price is not None:
        metadata["finalPrice"] = final_price
    return {
        "slug": slug or f"eth-updown-15m-{start}",
        "seriesSlug": series,
        "title": "Ethereum Up or Down fixture",
        "description": rules,
        "eventMetadata": metadata,
        "resolutionSource": resolution,
        "markets": [
            {
                "conditionId": condition,
                "question": "Ethereum Up or Down fixture",
                "description": rules,
                "resolutionSource": resolution,
                "outcomes": outcomes if outcomes is not None else ["Up", "Down"],
                "clobTokenIds": token_ids if token_ids is not None else ["token-up", "token-down"],
                "feesEnabled": fees_enabled,
                "feeSchedule": {
                    "rate": rate,
                    "exponent": exponent,
                    "takerOnly": True,
                    "rebateRate": "0.20",
                },
                "orderMinSize": min_size,
                "cryptoMarketConfig": {
                    "id": "eth-15m-twap-60",
                    "asset": asset,
                    "duration": duration,
                    "twapEnabled": twap_enabled,
                    "twapLookbackSeconds": lookback,
                },
            }
        ],
    }


def clob_info(
    *,
    rate: str = "0.03",
    exponent: str = "1",
    mos: str = "5",
    taker_base_fee: int = 1000,
) -> dict:
    return {
        "mos": mos,
        "mts": "0.001",
        "tbf": taker_base_fee,
        "mbf": taker_base_fee,
        "fd": {"r": rate, "e": exponent, "to": True},
        "t": [{"t": "token-up", "o": "Up"}, {"t": "token-down", "o": "Down"}],
    }


class Book:
    def __init__(self, asks: list[dict], min_order_size: str = "5") -> None:
        self.asks = asks
        self.bids = [{"price": "0.01", "size": "100"}]
        self.min_order_size = min_order_size


class FakePorts:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.clob: dict[str, dict] = {}
        self.books: dict[str, Book] = {}
        self.settled: list[dict] = []
        self.geoblock_payload: dict = {"blocked": False, "ip": "203.0.113.42", "country": "XX", "region": "YY"}

    def geoblock(self) -> dict:
        return self.geoblock_payload

    def event(self, slug: str) -> dict | None:
        return self.events.get(slug)

    def clob_market(self, condition_id: str) -> dict:
        return self.clob[condition_id]

    def order_book(self, token_id: str):
        return self.books[token_id]

    def settled_events(self) -> list[dict]:
        return list(self.settled)


def settled_series(origin: int, count: int = 16) -> list[dict]:
    """`count` older windows with constructed opens and closes."""

    events = []
    for index in range(count, 0, -1):
        start = origin - (900 * index)
        open_price = Decimal("1000") + Decimal(index)
        final_price = open_price + Decimal("1")
        events.append(
            eth_event(
                start,
                price_to_beat=format(open_price, "f"),
                final_price=format(final_price, "f"),
                condition=f"settled-{index}",
            )
        )
    return events
