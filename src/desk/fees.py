"""Taker fee curve read from a Polymarket payload.

The protocol formula is:

    fee = shares × rate × (price × (1 − price)) ^ exponent

`rate` and `exponent` come from the payload (`fd.r` / `fd.e` on the CLOB
market, or `feeSchedule.rate` / `feeSchedule.exponent` on Gamma). This
module has no remembered coefficient to substitute when those fields are
absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from desk.numbers import as_decimal


class FeeScheduleError(Exception):
    """The payload did not contain a usable taker-fee formula."""


_FEE_QUANTUM = Decimal("0.00001")


def quantize_fee(amount: Decimal) -> Decimal:
    """Round a USDC fee to 5 decimal places.

    Polymarket's published precision rule: fees are rounded to 5 decimal
    places, and a rounded amount smaller than 0.00001 USDC is zero.
    """

    rounded = amount.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_UP)
    if Decimal("0") < rounded < _FEE_QUANTUM:
        return Decimal("0")
    return rounded


@dataclass(frozen=True)
class FeeSchedule:
    rate: Decimal
    exponent: Decimal
    taker_only: bool
    source: str

    def per_share(self, price: Decimal) -> Decimal:
        price = as_decimal(price)
        base = price * (Decimal("1") - price)
        if base == 0:
            return Decimal("0")
        return self.rate * (base ** self.exponent)

    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        return quantize_fee(as_decimal(shares) * self.per_share(price))

    def formula(self) -> str:
        rate = format(self.rate, "f")
        exponent = format(self.exponent, "f")
        return f"shares * {rate} * (price * (1 - price)) ** {exponent}"


def fee_schedule_from_clob(info: dict) -> FeeSchedule:
    """Build a schedule from `GET /clob-markets/{condition_id}`.

    The legacy `tbf` / `base_fee` field is not the curve coefficient and
    is ignored on purpose.
    """

    fd = info.get("fd") if isinstance(info, dict) else None
    if not isinstance(fd, dict) or "r" not in fd or "e" not in fd:
        raise FeeScheduleError("CLOB market info did not include fd.r and fd.e")
    return FeeSchedule(
        rate=as_decimal(fd["r"]),
        exponent=as_decimal(fd["e"]),
        taker_only=bool(fd.get("to", True)),
        source="clob.fd",
    )


def fee_schedule_from_gamma(market: dict) -> FeeSchedule:
    """Build a schedule from a Gamma market object."""

    if not isinstance(market, dict):
        raise FeeScheduleError("gamma market payload was not an object")
    schedule = market.get("feeSchedule")
    if market.get("feesEnabled") is False:
        rate = Decimal("0")
        exponent = Decimal("1")
        if isinstance(schedule, dict) and "rate" in schedule:
            rate = as_decimal(schedule["rate"])
        if isinstance(schedule, dict) and "exponent" in schedule:
            exponent = as_decimal(schedule["exponent"])
        if rate != 0:
            raise FeeScheduleError("feesEnabled is false but feeSchedule.rate is non-zero")
        return FeeSchedule(
            rate=rate,
            exponent=exponent,
            taker_only=True,
            source="gamma.feesEnabled",
        )
    if not isinstance(schedule, dict) or "rate" not in schedule or "exponent" not in schedule:
        raise FeeScheduleError("gamma feeSchedule did not include rate and exponent")
    return FeeSchedule(
        rate=as_decimal(schedule["rate"]),
        exponent=as_decimal(schedule["exponent"]),
        taker_only=bool(schedule.get("takerOnly", True)),
        source="gamma.feeSchedule",
    )


def reconcile_fees(gamma: FeeSchedule, clob: FeeSchedule) -> FeeSchedule:
    """Use the CLOB curve after checking it matches Gamma.

    Paper fills simulate a CLOB match, so the CLOB `fd` object is the
    schedule that is charged. A disagreement is a failed read, not a
    reason to keep a previously seen coefficient.
    """

    if gamma.rate != clob.rate or gamma.exponent != clob.exponent:
        raise FeeScheduleError(
            "fee parameters disagree between gamma feeSchedule and clob fd: "
            f"gamma rate {format(gamma.rate, 'f')} exponent {format(gamma.exponent, 'f')}, "
            f"clob rate {format(clob.rate, 'f')} exponent {format(clob.exponent, 'f')}"
        )
    return FeeSchedule(
        rate=clob.rate,
        exponent=clob.exponent,
        taker_only=clob.taker_only,
        source="clob.fd",
    )
