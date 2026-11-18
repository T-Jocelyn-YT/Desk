"""Hard-limit checks. Every threshold is a constant in desk.limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from desk.fair import edge_points
from desk.fees import FeeSchedule
from desk.limits import (
    BANKROLL_USD,
    DAILY_STOP_USD,
    EDGE_POINTS,
    ENTRY_SECONDS,
    PER_BET_USD,
    PRICE_CEILING,
    WINDOW_CAP_USD,
    WINDOW_SECONDS,
)
from desk.numbers import as_decimal


@dataclass
class Position:
    window_start: int
    side: str
    shares: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    settled: bool = False


@dataclass(frozen=True)
class Rejection:
    reason: str

    @property
    def approved(self) -> bool:
        return False


@dataclass(frozen=True)
class Approval:
    side: str
    shares: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    edge: Decimal
    fair: Decimal
    best_ask_size: Decimal

    @property
    def approved(self) -> bool:
        return True


Decision = Rejection | Approval


@dataclass
class RiskState:
    cash: Decimal = BANKROLL_USD
    day: str = ""
    realized: Decimal = Decimal("0")
    halted: bool = False
    halt_reason: str | None = None
    positions: list[Position] = field(default_factory=list)
    fills_today: int = 0
    notional_today: Decimal = Decimal("0")
    fees_today: Decimal = Decimal("0")

    def summary(self, reason: str) -> dict[str, str | int]:
        return {
            "type": "daily_summary",
            "date": self.day,
            "mode": "paper",
            "fills": self.fills_today,
            "notional": format(self.notional_today, "f"),
            "fees": format(self.fees_today, "f"),
            "realized_pnl": format(self.realized, "f"),
            "reason": reason,
            "halt_reason": self.halt_reason or "",
        }

    def ensure_day(self, now: datetime) -> dict[str, str | int] | None:
        """Roll the UTC trading day. Return the closed day's summary."""

        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        key = moment.astimezone(timezone.utc).date().isoformat()
        summary = None
        if self.day == "":
            self.day = key
        elif key != self.day:
            summary = self.summary("date_change")
            self.day = key
            self.realized = Decimal("0")
            self.halted = False
            self.halt_reason = None
            self.fills_today = 0
            self.notional_today = Decimal("0")
            self.fees_today = Decimal("0")
        if self.realized <= -DAILY_STOP_USD:
            self.halted = True
            self.halt_reason = "daily_stop"
        return summary

    def window_notional(self, window_start: int) -> Decimal:
        return sum(
            (position.notional for position in self.positions if position.window_start == window_start),
            Decimal("0"),
        )

    def open_worst(self) -> Decimal:
        return sum(
            (
                position.notional + position.fee
                for position in self.positions
                if not position.settled
            ),
            Decimal("0"),
        )

    def daily_room(self) -> Decimal:
        return DAILY_STOP_USD + self.realized - self.open_worst()

    def record(self, window_start: int, approval: Approval) -> Position:
        cost = approval.notional + approval.fee
        if cost > self.cash or cost > BANKROLL_USD:
            raise RuntimeError("bankroll check failed after approval")
        self.cash -= cost
        position = Position(
            window_start=window_start,
            side=approval.side,
            shares=approval.shares,
            price=approval.price,
            notional=approval.notional,
            fee=approval.fee,
            settled=False,
        )
        self.positions.append(position)
        self.fills_today += 1
        self.notional_today += approval.notional
        self.fees_today += approval.fee
        return position

    def settle(self, window_start: int, open_price: Decimal, final_price: Decimal) -> list[Position]:
        """Settle unsettled buys in this window. Up wins when final >= open."""

        up_wins = as_decimal(final_price) >= as_decimal(open_price)
        settled: list[Position] = []
        for position in self.positions:
            if position.window_start != window_start or position.settled:
                continue
            won = (position.side == "Up" and up_wins) or (position.side == "Down" and not up_wins)
            payout = position.shares if won else Decimal("0")
            self.cash += payout
            self.realized += payout - position.notional - position.fee
            position.settled = True
            settled.append(position)
        if self.realized <= -DAILY_STOP_USD:
            self.halted = True
            self.halt_reason = "daily_stop"
        return settled


def initial_state() -> RiskState:
    """Starting bankroll. There is no argument that can raise it."""

    return RiskState(cash=BANKROLL_USD)


def decide(
    state: RiskState,
    *,
    now: datetime,
    window_start: int,
    side: str,
    fair: Decimal,
    ask_price: Decimal,
    ask_size: Decimal,
    schedule: FeeSchedule,
    min_shares: Decimal,
) -> Decision:
    """Apply the desk rules to one side of one window.

    `limit_overrides` is intentionally not a parameter. Callers cannot
    pass a wider ceiling, stop, or bankroll into this function.
    """

    state.ensure_day(now)
    if state.halted:
        return Rejection("daily_stop")

    remaining = window_start + WINDOW_SECONDS - int(now.timestamp())
    if not (0 < remaining <= ENTRY_SECONDS):
        return Rejection("entry_window")

    price = as_decimal(ask_price)
    if price <= 0:
        return Rejection("book")
    if price > PRICE_CEILING:
        return Rejection("price_ceiling")

    fair_probability = as_decimal(fair)
    edge = edge_points(fair_probability, price, schedule)
    if edge < EDGE_POINTS:
        return Rejection("edge")

    minimum = as_decimal(min_shares)
    if minimum <= 0:
        return Rejection("book")
    size = as_decimal(ask_size)
    if size < minimum:
        return Rejection("book")
    desired = max(PER_BET_USD / price, minimum)
    shares = min(desired, size)
    notional = shares * price
    fee = schedule.fee(shares, price)
    if state.window_notional(window_start) + notional > WINDOW_CAP_USD:
        return Rejection("window_cap")
    worst = notional + fee
    if worst > state.daily_room():
        return Rejection("daily_stop")
    if worst > state.cash or worst > BANKROLL_USD:
        return Rejection("bankroll")
    return Approval(
        side=side,
        shares=shares,
        price=price,
        notional=notional,
        fee=fee,
        edge=edge,
        fair=fair_probability,
        best_ask_size=size,
    )
