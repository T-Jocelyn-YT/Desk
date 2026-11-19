"""Desk hard limits.

These constants are the trading rules. Configuration may display them.
It cannot replace them. Fee coefficients are not desk limits; those are
read from Polymarket at startup.
"""

from decimal import Decimal

BANKROLL_USD = Decimal("50")
PER_BET_FRACTION = Decimal("0.02")
PER_WINDOW_FRACTION = Decimal("0.10")
DAILY_STOP_FRACTION = Decimal("0.06")

PER_BET_USD = BANKROLL_USD * PER_BET_FRACTION
WINDOW_CAP_USD = BANKROLL_USD * PER_WINDOW_FRACTION
DAILY_STOP_USD = BANKROLL_USD * DAILY_STOP_FRACTION

PRICE_CEILING = Decimal("0.72")
EDGE_POINTS = Decimal("0.06")

WINDOW_SECONDS = 15 * 60
ENTRY_SECONDS = 4 * 60

# First TWAP tick that may stand in for the window open or close, in milliseconds.
BOUNDARY_GRACE_MS = 5_000

VOL_LOOKBACK = 16
VOL_CHECK = 95


def displayed_limits() -> dict[str, str]:
    """Values an operator may display. Enforcement reads the constants above."""

    return {
        "bankroll_usd": format(BANKROLL_USD, "f"),
        "per_bet_usd": format(PER_BET_USD, "f"),
        "window_cap_usd": format(WINDOW_CAP_USD, "f"),
        "daily_stop_usd": format(DAILY_STOP_USD, "f"),
        "price_ceiling": format(PRICE_CEILING, "f"),
        "edge_points": format(EDGE_POINTS, "f"),
        "entry_seconds": str(ENTRY_SECONDS),
        "window_seconds": str(WINDOW_SECONDS),
    }
