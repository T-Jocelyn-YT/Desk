"""Desk hard limits.

These constants are the trading rules. Configuration may display them.
It cannot replace them. Fee coefficients are not desk limits; those are
read from Polymarket at startup.
"""

from decimal import Decimal, ROUND_HALF_UP

from desk.numbers import as_decimal

BANKROLL_USD = Decimal("50")
PER_BET_FRACTION = Decimal("0.02")
PER_WINDOW_FRACTION = Decimal("0.10")

PER_BET_USD = BANKROLL_USD * PER_BET_FRACTION
WINDOW_CAP_USD = BANKROLL_USD * PER_WINDOW_FRACTION
# Share of that UTC day's morning-verified cash. Locked at 00:00 UTC
# (08:00 Asia/Shanghai) and not recomputed as cash moves later that day.
DAILY_STOP_FRACTION = Decimal("0.20")
# Same 5-decimal USDC quantum as the taker fee. Half up.
MONEY_QUANTUM = Decimal("0.00001")

PRICE_CEILING = Decimal("0.72")
EDGE_POINTS = Decimal("0.06")

WINDOW_SECONDS = 15 * 60
ENTRY_SECONDS = 4 * 60

# First TWAP tick that may stand in for the window open or close, in milliseconds.
BOUNDARY_GRACE_MS = 5_000

VOL_LOOKBACK = 16
VOL_CHECK = 95


def quantize_money(amount: Decimal) -> Decimal:
    """Round a USDC amount to 5 decimal places, half up.

    The taker fee uses this same quantum. A rounded fee below 0.00001
    becomes zero; that fee-specific floor is not applied here.
    """

    return as_decimal(amount).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def daily_stop_from_cash(morning_cash: Decimal) -> Decimal:
    """Locked daily stop: 20% of morning cash, on the USDC quantum."""

    cash = quantize_money(morning_cash)
    return quantize_money(cash * DAILY_STOP_FRACTION)


def displayed_limits() -> dict[str, str]:
    """Values an operator may display. Enforcement reads the constants above."""

    return {
        "bankroll_usd": format(BANKROLL_USD, "f"),
        "per_bet_usd": format(PER_BET_USD, "f"),
        "window_cap_usd": format(WINDOW_CAP_USD, "f"),
        "daily_stop_fraction": format(DAILY_STOP_FRACTION, "f"),
        "price_ceiling": format(PRICE_CEILING, "f"),
        "edge_points": format(EDGE_POINTS, "f"),
        "entry_seconds": str(ENTRY_SECONDS),
        "window_seconds": str(WINDOW_SECONDS),
    }
