"""Watcher v1 fair odds for ETH 15-minute Up or Down.

Settlement comparison: Up when the Chainlink ETH/USD 60-second TWAP at the
end of the window is greater than or equal to the TWAP at the start.
Otherwise Down. Ties resolve Up.

    P(Up) = Φ( [ln(S_t / S_0) − ½ σ² τ] / (σ √τ) )

τ is seconds remaining / 900. Drift is zero; the formula already includes
the −½σ²τ term. Down is 1 − P(Up). These are raw probabilities before the
taker fee.

The 6-point rule compares fee-adjusted fair odds with the book price:

    edge = fair − rate × (price × (1 − price)) ^ exponent − price

Buy only when edge is at least the desk minimum (0.06).
"""

from __future__ import annotations

import math
from decimal import Decimal

from desk.fees import FeeSchedule
from desk.limits import EDGE_POINTS, WINDOW_SECONDS
from desk.numbers import as_decimal


def _phi(z: Decimal) -> Decimal:
    value = float(z)
    if value > 20:
        return Decimal("1")
    if value < -20:
        return Decimal("0")
    probability = 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
    return Decimal(str(probability))


def probability_up(
    spot: Decimal,
    open_price: Decimal,
    sigma: Decimal,
    seconds_remaining: int,
) -> Decimal:
    """Raw P(Up) from the Watcher v1 diffusion. No fee is applied here."""

    spot = as_decimal(spot)
    open_price = as_decimal(open_price)
    sigma = as_decimal(sigma)
    if spot <= 0 or open_price <= 0:
        raise ValueError("spot and open must be positive")
    if seconds_remaining <= 0 or sigma == 0:
        return Decimal("1") if spot >= open_price else Decimal("0")
    tau = Decimal(seconds_remaining) / Decimal(WINDOW_SECONDS)
    log_ratio = (spot / open_price).ln()
    numerator = log_ratio - (Decimal("0.5") * sigma * sigma * tau)
    denominator = sigma * tau.sqrt()
    return _phi(numerator / denominator)


def probability_down(
    spot: Decimal,
    open_price: Decimal,
    sigma: Decimal,
    seconds_remaining: int,
) -> Decimal:
    return Decimal("1") - probability_up(spot, open_price, sigma, seconds_remaining)


def fee_adjusted_fair(fair: Decimal, book_price: Decimal, schedule: FeeSchedule) -> Decimal:
    """Fair probability after subtracting the taker fee per share."""

    return as_decimal(fair) - schedule.per_share(book_price)


def edge_points(fair: Decimal, book_price: Decimal, schedule: FeeSchedule) -> Decimal:
    """How many probability points fee-adjusted fair odds beat the book by."""

    return fee_adjusted_fair(fair, book_price, schedule) - as_decimal(book_price)


def clears_six_point_edge(fair: Decimal, book_price: Decimal, schedule: FeeSchedule) -> bool:
    return edge_points(fair, book_price, schedule) >= EDGE_POINTS
