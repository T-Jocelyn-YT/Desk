"""Hard-limit checks.

Thresholds other than the daily stop's dollar amount are constants in
desk.limits. The daily stop is 20% of the UTC day's morning-verified
cash and stays at that figure until the next 00:00 UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from desk.fair import edge_points
from desk.fees import FeeSchedule
from desk.limits import (
    BANKROLL_USD,
    EDGE_POINTS,
    ENTRY_SECONDS,
    PER_BET_USD,
    PRICE_CEILING,
    WINDOW_CAP_USD,
    WINDOW_SECONDS,
    daily_stop_from_cash,
    quantize_money,
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


def _parse_day(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != value:
        return None
    return value


def _finite_money(value: object, *, allow_negative: bool = False) -> Decimal | None:
    try:
        amount = quantize_money(as_decimal(value))
    except (ValueError, InvalidOperation, ArithmeticError):
        return None
    if not amount.is_finite():
        return None
    if amount < 0 and not allow_negative:
        return None
    return amount


@dataclass
class MorningLock:
    """One UTC day's locked stop.

    `authority` is `operator` for a hand-written line and `journal` for a
    line the desk recorded. The cash on the line is the stop base only.
    It does not replace book cash. Fills, settlements, and verified
    heartbeats still move cash and realized P&L. They do not recompute a
    stop that is already locked for the UTC day.
    """

    day: str
    cash: Decimal | None
    stop: Decimal
    authority: str
    dirty: bool = False


def morning_lock_from_record(record: object) -> MorningLock | None:
    """Parse one morning-cash line. Invalid lines are ignored.

    When `cash` is present the stop is 20% of that cash. A supplied
    `daily_stop_usd` is used only when cash is absent. The first parsed
    line for a date is the lock; the caller drops later ones.
    """

    if not isinstance(record, dict):
        return None
    if record.get("type") not in (None, "", "morning_cash"):
        return None
    day = _parse_day(record.get("date"))
    if day is None:
        return None
    source = record.get("source")
    if source in (None, "", "operator"):
        authority = "operator"
    elif source == "journal":
        authority = "journal"
    else:
        return None
    cash_raw = record.get("cash")
    stop_raw = record.get("daily_stop_usd")
    has_cash = cash_raw not in (None, "")
    has_stop = stop_raw not in (None, "")
    if not has_cash and not has_stop:
        return None
    if has_cash:
        cash = _finite_money(cash_raw)
        if cash is None:
            return None
        stop = daily_stop_from_cash(cash)
    else:
        cash = None
        stop = _finite_money(stop_raw)
        if stop is None:
            return None
    return MorningLock(day=day, cash=cash, stop=stop, authority=authority, dirty=False)


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
    morning_cash: Decimal | None = None
    daily_stop: Decimal = field(default_factory=lambda: daily_stop_from_cash(BANKROLL_USD))
    locks: dict[str, MorningLock] = field(default_factory=dict)
    _morning_provisional: bool = field(default=False, repr=False, compare=False)
    _verified_observation: bool = field(default=False, repr=False, compare=False)
    _replay_today: str | None = field(default=None, repr=False, compare=False)

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

    def install_lock(self, lock: MorningLock) -> None:
        """Keep the first lock for a UTC day. Later records do not replace it."""

        if lock.day in self.locks:
            return
        self.locks[lock.day] = lock

    def ensure_day(self, now: datetime, *, replay_boundary: bool = False) -> dict[str, str | int] | None:
        """Roll the UTC trading day. Return the closed day's summary.

        Entering a day locks the stop from morning cash. A later call on
        the same day does not recompute it when cash has moved.
        """

        key = trading_day(now)
        summary = None
        entering = self.day == "" or key != self.day
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
        if entering:
            created = self._lock_day(key)
            self._set_provisional(created, replay_boundary=replay_boundary)
        self._halt_if_stopped()
        return summary

    def replay(
        self,
        records: list[dict],
        now: datetime,
        heartbeats: list[dict] | None = None,
    ) -> None:
        """Restore the book from fills.jsonl using the daily-stop day roll.

        Every fill and settlement is applied in order, starting from the
        bankroll. `ensure_day` rolls the UTC day (00:00 UTC, 08:00
        Asia/Shanghai) and resets realized P&L, the halt, and the day's
        counters. It does not reset cash or positions, so a restart after
        the roll starts from the previous day's ending cash and does not
        put that day's realized P&L into the new stop.

        Heartbeats are verified cash and realized P&L. When the fill log
        does not meet a heartbeat, that stretch is missing and is not
        booked as zero cash or zero P&L: the heartbeat replaces the
        inferred figures. The stop still locks once, from the morning
        figure, and does not follow later cash.
        """

        moment = _as_utc(now)
        preserved = {day: lock for day, lock in self.locks.items() if not lock.dirty}
        self.cash = BANKROLL_USD
        self.realized = Decimal("0")
        self.halted = False
        self.halt_reason = None
        self.positions = []
        self.fills_today = 0
        self.notional_today = Decimal("0")
        self.fees_today = Decimal("0")
        self.day = ""
        self.morning_cash = None
        self.daily_stop = daily_stop_from_cash(BANKROLL_USD)
        self.locks = preserved
        self._morning_provisional = False
        self._verified_observation = False
        self._replay_today = trading_day(moment)
        try:
            grouped: dict[datetime, dict[str, list[dict]]] = {}
            order: list[datetime] = []

            def add(timestamp: datetime | None, kind: str, record: dict) -> None:
                if timestamp is None or timestamp > moment:
                    return
                bucket = grouped.get(timestamp)
                if bucket is None:
                    bucket = {"book": [], "heartbeat": []}
                    grouped[timestamp] = bucket
                    order.append(timestamp)
                bucket[kind].append(record)

            for record in records:
                if not isinstance(record, dict):
                    continue
                if str(record.get("mode") or "paper") != "paper":
                    continue
                add(_parse_time(record.get("ts")), "book", record)
            for record in heartbeats or []:
                if not isinstance(record, dict):
                    continue
                if str(record.get("mode") or "paper") != "paper":
                    continue
                add(_parse_time(record.get("ts")), "heartbeat", record)
            for timestamp in sorted(order):
                bucket = grouped[timestamp]
                self.ensure_day(timestamp, replay_boundary=True)
                cash_before = quantize_money(self.cash)
                realized_before = quantize_money(self.realized)
                for record in bucket["book"]:
                    kind = record.get("type")
                    try:
                        if kind == "fill":
                            self._replay_fill(record)
                        elif kind == "settlement":
                            self._replay_settlement(record)
                    except (KeyError, ValueError):
                        continue
                cash_after = quantize_money(self.cash)
                realized_after = quantize_money(self.realized)
                for record in bucket["heartbeat"]:
                    self._consider_heartbeat(
                        record,
                        cash_before,
                        realized_before,
                        cash_after,
                        realized_after,
                    )
            self.ensure_day(moment, replay_boundary=True)
        finally:
            self._replay_today = None
            self._morning_provisional = False

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

    def _lock_day(self, day: str) -> bool:
        """Install the day's stop. Return whether this call created it.

        A line from morning-cash.jsonl supplies the stop base only. Book
        cash stays on the fill and settlement replay.
        """

        lock = self.locks.get(day)
        created = lock is None
        if lock is None:
            cash = quantize_money(self.cash)
            lock = MorningLock(
                day=day,
                cash=cash,
                stop=daily_stop_from_cash(cash),
                authority="journal",
                dirty=self._replay_today is None or day == self._replay_today,
            )
            self.locks[day] = lock
        self.morning_cash = lock.cash
        self.daily_stop = lock.stop
        return created

    def _set_provisional(self, created: bool, *, replay_boundary: bool) -> None:
        if not replay_boundary or not created:
            self._morning_provisional = False
            return
        self._morning_provisional = not self._verified_observation

    def _heartbeat_observation(self, record: dict) -> tuple[Decimal | None, Decimal | None]:
        if record.get("type") not in (None, "", "heartbeat"):
            return None, None
        cash = _finite_money(record.get("cash"))
        if cash is None:
            return None, None
        raw = record.get("realized_pnl")
        if raw in (None, ""):
            return cash, None
        realized = _finite_money(raw, allow_negative=True)
        if realized is None:
            return None, None
        return cash, realized

    def _consider_heartbeat(
        self,
        record: dict,
        cash_before: Decimal,
        realized_before: Decimal,
        cash_after: Decimal,
        realized_after: Decimal,
    ) -> None:
        """Adopt a verified cash reading when the fill log cannot explain it.

        A heartbeat written in the same second as a fill or settlement
        matches either the cash before those rows or the cash after them.
        A morning-cash line locks the stop only, so a later heartbeat may
        still correct book cash and realized P&L.
        """

        observed_cash, observed_realized = self._heartbeat_observation(record)
        if observed_cash is None:
            return
        cash_ok = observed_cash == cash_before or observed_cash == cash_after
        realized_ok = (
            observed_realized is None
            or observed_realized == realized_before
            or observed_realized == realized_after
        )
        if cash_ok and realized_ok:
            if self._morning_provisional:
                self._morning_provisional = False
            self._verified_observation = True
            return
        if not cash_ok:
            self.cash = observed_cash
        if observed_realized is not None and not realized_ok:
            self.realized = observed_realized
        if not cash_ok:
            self._revise_morning_from_verified_cash()
        else:
            self._morning_provisional = False
        self._verified_observation = True
        self._halt_if_stopped()

    def _revise_morning_from_verified_cash(self) -> None:
        """Use the first verified cash when the opening figure was never confirmed."""

        if not self._morning_provisional:
            return
        lock = self.locks.get(self.day)
        if lock is None or lock.authority == "operator":
            self._morning_provisional = False
            return
        cash = quantize_money(self.cash)
        lock.cash = cash
        lock.stop = daily_stop_from_cash(cash)
        lock.dirty = self._replay_today is None or self.day == self._replay_today
        self.morning_cash = cash
        self.daily_stop = lock.stop
        self._morning_provisional = False

    def _halt_if_stopped(self) -> None:
        if self.realized <= -self.daily_stop:
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
        return self.daily_stop + self.realized - self.open_worst()

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
