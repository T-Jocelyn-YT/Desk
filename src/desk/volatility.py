"""15-minute log-return volatility from settled Gamma windows.

The pricing input is the sample standard deviation of the last 16 settled
windows (4 hours). The last 95 windows (24 hours) are a check and are not
used as the pricing sigma.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from desk.limits import VOL_CHECK, VOL_LOOKBACK
from desk.markets import Market, is_eth_15m_up_down, parse_market
from desk.numbers import as_decimal


@dataclass(frozen=True)
class Volatility:
    sigma: Decimal | None
    windows_used: int
    sigma_check: Decimal | None
    check_windows: int


def sample_std(values: list[Decimal]) -> Decimal:
    count = len(values)
    if count < 2:
        raise ValueError("sample standard deviation needs at least two returns")
    mean = sum(values, Decimal("0")) / Decimal(count)
    variance = sum((value - mean) ** 2 for value in values) / Decimal(count - 1)
    return variance.sqrt()


def log_return(open_price: Decimal, final_price: Decimal) -> Decimal:
    open_price = as_decimal(open_price)
    final_price = as_decimal(final_price)
    if open_price <= 0 or final_price <= 0:
        raise ValueError("window prices must be positive")
    return (final_price / open_price).ln()


def volatility_from_returns(returns_oldest_first: list[Decimal]) -> Volatility:
    """`returns_oldest_first` is chronological. The tail is the most recent."""

    usable = [as_decimal(value) for value in returns_oldest_first]
    sigma = sample_std(usable[-VOL_LOOKBACK:]) if len(usable) >= VOL_LOOKBACK else None
    check = sample_std(usable[-VOL_CHECK:]) if len(usable) >= VOL_CHECK else None
    return Volatility(
        sigma=sigma,
        windows_used=min(len(usable), VOL_LOOKBACK) if sigma is not None else len(usable),
        sigma_check=check,
        check_windows=min(len(usable), VOL_CHECK) if check is not None else len(usable),
    )


def settled_markets(events: list[object]) -> list[Market]:
    """ETH 15-minute windows that already have an official open and close."""

    markets: list[Market] = []
    for event in events:
        if not is_eth_15m_up_down(event):
            continue
        market = parse_market(event)
        if market.price_to_beat is None or market.final_price is None:
            continue
        markets.append(market)
    markets.sort(key=lambda market: market.window_start)
    return markets


def volatility_from_events(events: list[object]) -> Volatility:
    markets = settled_markets(events)
    returns = [log_return(market.price_to_beat, market.final_price) for market in markets]
    return volatility_from_returns(returns)
