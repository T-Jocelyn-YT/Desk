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
from desk.markets import window_start_from_slug
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
class FillSettlement:
    """One paper fill resolved against the window's open and close."""

    window_start: int
    side: str
    shares: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    outcome: str
    open_price: Decimal
    final_price: Decimal
    realized_pnl: Decimal


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


def trading_day(now: datetime) -> str:
    """UTC date the daily stop uses.

    The stop rolls at 00:00 UTC, which is 08:00 Asia/Shanghai. Replay
    uses this same key. It does not use a Shanghai-local midnight.
    """

    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def _as_utc(now: datetime) -> datetime:
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if text == "":
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


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

        key = trading_day(now)
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
        self._halt_if_stopped()
        return summary

    def replay(self, records: list[dict], now: datetime) -> None:
        """Restore the book from fills.jsonl using the daily-stop day roll.

        Every fill and settlement is applied in order, starting from the
        bankroll. `ensure_day` rolls the UTC day (00:00 UTC, 08:00
        Asia/Shanghai) and resets realized P&L, the halt, and the day's
        counters. It does not reset cash or positions, so a restart after
        the roll starts from the previous day's ending cash and does not
        put that day's realized P&L into the new stop.
        """

        self.cash = BANKROLL_USD
        self.realized = Decimal("0")
        self.halted = False
        self.halt_reason = None
        self.positions = []
        self.fills_today = 0
        self.notional_today = Decimal("0")
        self.fees_today = Decimal("0")
        self.day = ""
        moment = _as_utc(now)
        for record in records:
            if not isinstance(record, dict):
                continue
            if str(record.get("mode") or "paper") != "paper":
                continue
            timestamp = _parse_time(record.get("ts"))
            if timestamp is None or timestamp > moment:
                continue
            self.ensure_day(timestamp)
            kind = record.get("type")
            try:
                if kind == "fill":
                    self._replay_fill(record)
                elif kind == "settlement":
                    self._replay_settlement(record)
            except (KeyError, ValueError):
                continue
        self.ensure_day(now)

    def _replay_fill(self, record: dict) -> None:
        window_start = window_start_from_slug(record.get("market"))
        if window_start is None:
            return
        side = record.get("side")
        if side not in {"Up", "Down"}:
            return
        approval = Approval(
            side=str(side),
            shares=as_decimal(record["shares"]),
            price=as_decimal(record["price"]),
            notional=as_decimal(record["notional"]),
            fee=as_decimal(record["fee"]),
            edge=as_decimal(record.get("edge", "0")),
            fair=as_decimal(record.get("fair", "0")),
            best_ask_size=as_decimal(record.get("best_ask_size", record["shares"])),
        )
        self.record(window_start, approval)

    def _replay_settlement(self, record: dict) -> None:
        window_start = window_start_from_slug(record.get("market"))
        if window_start is None:
            return
        side = record.get("side")
        shares = as_decimal(record["shares"])
        price = as_decimal(record["price"])
        notional = as_decimal(record["notional"])
        fee = as_decimal(record["fee"])
        open_price = as_decimal(record["open_price"])
        final_price = as_decimal(record["final_price"])
        for position in self.positions:
            if position.settled or position.window_start != window_start or position.side != side:
                continue
            if (
                position.shares != shares
                or position.price != price
                or position.notional != notional
                or position.fee != fee
            ):
                continue
            self._close_position(position, open_price, final_price)
            self._halt_if_stopped()
            return

    def _halt_if_stopped(self) -> None:
        if self.realized <= -DAILY_STOP_USD:
            self.halted = True
            self.halt_reason = "daily_stop"

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
        return self._open_position(window_start, approval)

    def _open_position(self, window_start: int, approval: Approval) -> Position:
        self.cash -= approval.notional + approval.fee
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

    def settle(self, window_start: int, open_price: Decimal, final_price: Decimal) -> list[FillSettlement]:
        """Settle unsettled buys in this window. Up wins when final >= open."""

        open_price = as_decimal(open_price)
        final_price = as_decimal(final_price)
        settled: list[FillSettlement] = []
        for position in self.positions:
            if position.window_start != window_start or position.settled:
                continue
            settled.append(self._close_position(position, open_price, final_price))
        self._halt_if_stopped()
        return settled

    def _close_position(
        self,
        position: Position,
        open_price: Decimal,
        final_price: Decimal,
    ) -> FillSettlement:
        open_price = as_decimal(open_price)
        final_price = as_decimal(final_price)
        outcome = "Up" if final_price >= open_price else "Down"
        up_wins = outcome == "Up"
        won = (position.side == "Up" and up_wins) or (position.side == "Down" and not up_wins)
        payout = position.shares if won else Decimal("0")
        realized_pnl = payout - position.notional - position.fee
        self.cash += payout
        self.realized += realized_pnl
        position.settled = True
        return FillSettlement(
            window_start=position.window_start,
            side=position.side,
            shares=position.shares,
            price=position.price,
            notional=position.notional,
            fee=position.fee,
            outcome=outcome,
            open_price=open_price,
            final_price=final_price,
            realized_pnl=realized_pnl,
        )


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
    # The per-bet cap is not raised to the exchange minimum. If the
    # minimum itself costs more than the cap, skip the order.
    if minimum * price > PER_BET_USD:
        return Rejection("per_bet")
    shares = min(PER_BET_USD / price, size)
    notional = shares * price
    if shares < minimum or notional > PER_BET_USD:
        return Rejection("per_bet")
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
