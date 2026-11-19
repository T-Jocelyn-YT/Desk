"""Identify Polymarket 15-minute Ethereum Up or Down markets.

The live series slug is `eth-up-or-down-15m`. Window slugs look like
`eth-updown-15m-{unix_start}`. Older windows in the same series use
`eth-up-or-down-15m-{unix_start}`. Anything else is ignored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from desk.fees import FeeSchedule, fee_schedule_from_gamma
from desk.limits import WINDOW_SECONDS
from desk.numbers import as_decimal


SERIES_SLUG = "eth-up-or-down-15m"
RESOLUTION_SOURCE = "https://data.chain.link/streams/eth-usd-twap-60s-streams"
_SLUG_RE = re.compile(r"^(?:eth-updown-15m|eth-up-or-down-15m)-(\d+)$")


class MarketRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Market:
    slug: str
    series_slug: str
    condition_id: str
    window_start: int
    window_end: int
    title: str
    rules_text: str
    resolution_source: str
    tokens: dict[str, str]
    gamma_fee: FeeSchedule
    gamma_min_shares: Decimal
    price_to_beat: Decimal | None
    final_price: Decimal | None
    config: dict


def window_start_of(now: datetime) -> int:
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    ts = int(moment.timestamp())
    return ts - (ts % WINDOW_SECONDS)


def slug_for(start: int) -> str:
    return f"eth-updown-15m-{start}"


def _loads_maybe(value: object) -> object:
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _metadata(event: dict) -> dict:
    raw = _loads_maybe(event.get("eventMetadata") or event.get("event_metadata") or {})
    return raw if isinstance(raw, dict) else {}


def _market_object(event: dict) -> dict:
    markets = event.get("markets") or []
    if isinstance(markets, list) and markets and isinstance(markets[0], dict):
        return markets[0]
    return {}


def _config(market: dict) -> dict:
    raw = _loads_maybe(market.get("cryptoMarketConfig") or {})
    return raw if isinstance(raw, dict) else {}


def _optional_price(metadata: dict, key: str) -> Decimal | None:
    if key not in metadata or metadata[key] in (None, ""):
        return None
    value = as_decimal(metadata[key])
    if value <= 0:
        return None
    return value


def _tokens(market: dict) -> dict[str, str]:
    outcomes = _loads_maybe(market.get("outcomes"))
    token_ids = _loads_maybe(market.get("clobTokenIds"))
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        raise MarketRejected("outcomes")
    if len(outcomes) != len(token_ids) or len(outcomes) < 2:
        raise MarketRejected("outcomes")
    mapping: dict[str, str] = {}
    for outcome, token_id in zip(outcomes, token_ids):
        name = str(outcome).strip().lower()
        mapping[name] = str(token_id)
    if set(mapping) != {"up", "down"}:
        raise MarketRejected("outcomes")
    return {"Up": mapping["up"], "Down": mapping["down"]}


def is_eth_15m_up_down(event: object) -> bool:
    try:
        parse_market(event)
    except MarketRejected:
        return False
    return True


def parse_market(event: object) -> Market:
    """Parse one Gamma event. Raise MarketRejected when it is not our market."""

    if not isinstance(event, dict):
        raise MarketRejected("not_eth_15m")
    slug = str(event.get("slug") or "")
    match = _SLUG_RE.fullmatch(slug)
    if match is None:
        raise MarketRejected("not_eth_15m")
    series = str(event.get("seriesSlug") or "")
    if series and series != SERIES_SLUG:
        raise MarketRejected("not_eth_15m")
    market = _market_object(event)
    config = _config(market)
    if config.get("asset") != "eth" or str(config.get("duration")) != "15m":
        raise MarketRejected("config")
    if config.get("twapEnabled") is not True:
        raise MarketRejected("config")
    try:
        lookback = int(config.get("twapLookbackSeconds"))
    except (TypeError, ValueError):
        raise MarketRejected("config") from None
    if lookback != 60:
        raise MarketRejected("config")
    source = str(market.get("resolutionSource") or event.get("resolutionSource") or "")
    if not source.startswith(RESOLUTION_SOURCE):
        raise MarketRejected("resolution_source")
    rules = str(market.get("description") or event.get("description") or "").strip()
    if not rules:
        raise MarketRejected("rules")
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
    if condition_id == "":
        raise MarketRejected("condition")
    try:
        gamma_fee = fee_schedule_from_gamma(market)
    except Exception as exc:
        raise MarketRejected("fee") from exc
    if "orderMinSize" not in market:
        raise MarketRejected("min_size")
    min_shares = as_decimal(market["orderMinSize"])
    if min_shares <= 0:
        raise MarketRejected("min_size")
    metadata = _metadata(event)
    start = int(match.group(1))
    return Market(
        slug=slug,
        series_slug=series or SERIES_SLUG,
        condition_id=condition_id,
        window_start=start,
        window_end=start + WINDOW_SECONDS,
        title=str(event.get("title") or market.get("question") or slug),
        rules_text=rules,
        resolution_source=source,
        tokens=_tokens(market),
        gamma_fee=gamma_fee,
        gamma_min_shares=min_shares,
        price_to_beat=_optional_price(metadata, "priceToBeat"),
        final_price=_optional_price(metadata, "finalPrice"),
        config={
            "asset": config.get("asset"),
            "duration": config.get("duration"),
            "twapEnabled": config.get("twapEnabled"),
            "twapLookbackSeconds": lookback,
        },
    )


def select_eth_15m(events: list[object]) -> list[Market]:
    selected: list[Market] = []
    for event in events:
        if is_eth_15m_up_down(event):
            selected.append(parse_market(event))
    return selected
