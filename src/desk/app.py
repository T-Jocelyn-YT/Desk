"""Paper desk loop. Live order entry is not implemented."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from desk.fair import probability_down, probability_up
from desk.fees import FeeSchedule, FeeScheduleError, fee_schedule_from_clob, reconcile_fees
from desk.geoblock import GeoblockError, parse_geoblock
from desk.journal import Journal
from desk.limits import ENTRY_SECONDS
from desk.markets import Market, MarketRejected, parse_market, slug_for, window_start_of
from desk.numbers import as_decimal
from desk.paper import best_ask, book_min_shares, paper_fill
from desk.risk import Approval, RiskState, initial_state, decide
from desk.twap import TwapBook, resolve_open
from desk.volatility import Volatility, volatility_from_events


class StartupError(Exception):
    pass


class FreezeStop(Exception):
    pass


@dataclass(frozen=True)
class Session:
    market: Market
    fee: FeeSchedule
    min_shares: Decimal
    volatility: Volatility
    rules_sha256: str


def reconcile_min(*values: Decimal) -> Decimal:
    if not values or any(value <= 0 for value in values):
        raise StartupError("minimum order size was not positive")
    first = values[0]
    if any(value != first for value in values[1:]):
        raise StartupError("minimum order size disagreed across Polymarket sources")
    return first


def min_shares_from_clob(info: dict) -> Decimal:
    if not isinstance(info, dict) or "mos" not in info:
        raise StartupError("CLOB market info did not include mos")
    value = as_decimal(info["mos"])
    if value <= 0:
        raise StartupError("CLOB mos was not positive")
    return value


def load_session(ports: object, now: datetime) -> Session:
    start = window_start_of(now)
    event = ports.event(slug_for(start))  # type: ignore[attr-defined]
    if event is None:
        raise StartupError("current 15-minute ETH up/down market was not returned")
    try:
        market = parse_market(event)
    except MarketRejected as exc:
        raise StartupError(f"current market rejected: {exc.reason}") from exc
    try:
        clob_payload = ports.clob_market(market.condition_id)  # type: ignore[attr-defined]
        clob_fee = fee_schedule_from_clob(clob_payload)
        fee = reconcile_fees(market.gamma_fee, clob_fee)
        clob_min = min_shares_from_clob(clob_payload)
    except FeeScheduleError as exc:
        raise StartupError(str(exc)) from exc
    up_book = ports.order_book(market.tokens["Up"])  # type: ignore[attr-defined]
    down_book = ports.order_book(market.tokens["Down"])  # type: ignore[attr-defined]
    try:
        minimum = reconcile_min(
            market.gamma_min_shares,
            clob_min,
            book_min_shares(up_book),
            book_min_shares(down_book),
        )
    except ValueError as exc:
        raise StartupError(str(exc)) from exc
    volatility = volatility_from_events(ports.settled_events())  # type: ignore[attr-defined]
    digest = hashlib.sha256(market.rules_text.encode("utf-8")).hexdigest()
    return Session(
        market=market,
        fee=fee,
        min_shares=minimum,
        volatility=volatility,
        rules_sha256=digest,
    )


class DeskApp:
    def __init__(
        self,
        ports: object,
        journal: Journal,
        twap: TwapBook | None = None,
        risk: RiskState | None = None,
        freeze_file: object | None = None,
    ) -> None:
        self.ports = ports
        self.journal = journal
        self.twap = twap if twap is not None else TwapBook()
        self.risk = risk if risk is not None else initial_state()
        self.freeze_file = freeze_file
        self.session: Session | None = None
        self.mode = "paper"
        self._last_beat: datetime | None = None
        self._closed = False

    def startup(self, now: datetime) -> None:
        parse_geoblock(self.ports.geoblock())  # type: ignore[attr-defined]
        self.risk.ensure_day(now)
        self.session = load_session(self.ports, now)
        self._log_startup(now)

    def tick(self, now: datetime) -> None:
        if self._freeze_present():
            self.close(now, "freeze")
            raise FreezeStop("FREEZE file present; stopping")
        summary = self.risk.ensure_day(now)
        if summary is not None:
            self.journal.daily(summary)
        # Resolve the window already in hand before the clock rolls to the next one.
        if self.session is not None:
            self._refresh_official_prices()
        if self.session is not None:
            self._settle(now, self.session)
        try:
            self._ensure_window(now)
        except (StartupError, FeeScheduleError, MarketRejected, ValueError):
            self.session = None
        self._heartbeat(now)
        session = self.session
        if session is None:
            return
        self._settle(now, session)
        self._consider_buys(now, session)

    def close(self, now: datetime, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.risk.ensure_day(now)
        self.journal.daily(self.risk.summary(reason))

    def _freeze_present(self) -> bool:
        if self.freeze_file is None:
            return False
        from pathlib import Path

        return Path(self.freeze_file).exists()

    def _ensure_window(self, now: datetime) -> None:
        start = window_start_of(now)
        if self.session is not None and self.session.market.window_start == start:
            self._refresh_official_prices()
            return
        self.session = load_session(self.ports, now)
        self._log_startup(now)

    def _refresh_official_prices(self) -> None:
        session = self.session
        if session is None:
            return
        event = self.ports.event(session.market.slug)  # type: ignore[attr-defined]
        if not isinstance(event, dict):
            return
        try:
            updated = parse_market(event)
        except MarketRejected:
            self.session = None
            return
        if (
            updated.gamma_fee.rate != session.fee.rate
            or updated.gamma_fee.exponent != session.fee.exponent
        ):
            self.session = None
            return
        self.session = replace(
            session,
            market=replace(
                session.market,
                price_to_beat=updated.price_to_beat,
                final_price=updated.final_price,
            ),
        )

    def _settle(self, now: datetime, session: Session) -> None:
        market = session.market
        now_ts = int(now.timestamp())
        if market.final_price is not None:
            final_price = market.final_price
        elif now_ts >= market.window_end:
            final_price = self.twap.close_price(market.window_end)
        else:
            return
        open_price = self._open_price(market)
        if open_price is None or final_price is None:
            return
        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        for item in self.risk.settle(market.window_start, open_price, final_price):
            self.journal.settlement(
                {
                    "type": "settlement",
                    "ts": moment.astimezone(timezone.utc).isoformat(),
                    "mode": self.mode,
                    "market": market.slug,
                    "side": item.side,
                    "outcome": item.outcome,
                    "open_price": format(item.open_price, "f"),
                    "final_price": format(item.final_price, "f"),
                    "realized_pnl": format(item.realized_pnl, "f"),
                    "shares": format(item.shares, "f"),
                    "price": format(item.price, "f"),
                    "notional": format(item.notional, "f"),
                    "fee": format(item.fee, "f"),
                }
            )
            print(
                f"settle {item.outcome} {item.side} pnl {format(item.realized_pnl, 'f')} {market.slug}",
                flush=True,
            )

    def _consider_buys(self, now: datetime, session: Session) -> None:
        market = session.market
        if market.final_price is not None:
            return
        remaining = market.window_end - int(now.timestamp())
        if not (0 < remaining <= ENTRY_SECONDS):
            return
        spot = self.twap.spot()
        open_price = self._open_price(market)
        sigma = session.volatility.sigma
        if spot is None or open_price is None or sigma is None:
            return
        for side in ("Up", "Down"):
            book = self.ports.order_book(market.tokens[side])  # type: ignore[attr-defined]
            ask = best_ask(book)
            if ask is None:
                continue
            fair = (
                probability_up(spot, open_price, sigma, remaining)
                if side == "Up"
                else probability_down(spot, open_price, sigma, remaining)
            )
            decision = decide(
                self.risk,
                now=now,
                window_start=market.window_start,
                side=side,
                fair=fair,
                ask_price=ask.price,
                ask_size=ask.size,
                schedule=session.fee,
                min_shares=session.min_shares,
            )
            if not isinstance(decision, Approval):
                continue
            fill = paper_fill(ask, decision.shares, session.fee, session.min_shares)
            if (
                fill is None
                or fill.shares != decision.shares
                or fill.price != decision.price
                or fill.fee != decision.fee
            ):
                continue
            self.risk.record(market.window_start, decision)
            moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
            self.journal.fill(
                {
                    "type": "fill",
                    "ts": moment.astimezone(timezone.utc).isoformat(),
                    "mode": self.mode,
                    "market": market.slug,
                    "side": decision.side,
                    "price": format(decision.price, "f"),
                    "shares": format(decision.shares, "f"),
                    "notional": format(decision.notional, "f"),
                    "fee": format(decision.fee, "f"),
                    "best_ask_size": format(decision.best_ask_size, "f"),
                    "fair": format(decision.fair, "f"),
                    "edge": format(decision.edge, "f"),
                    "fee_rate": format(session.fee.rate, "f"),
                    "fee_exponent": format(session.fee.exponent, "f"),
                    "min_shares": format(session.min_shares, "f"),
                }
            )
            print(
                f"fill {decision.side} {format(decision.shares, 'f')} "
                f"@ {format(decision.price, 'f')} fee {format(decision.fee, 'f')} {market.slug}",
                flush=True,
            )

    def _open_price(self, market: Market) -> Decimal | None:
        prior = None
        window_length = market.window_end - market.window_start
        previous = self.ports.event(slug_for(market.window_start - window_length))  # type: ignore[attr-defined]
        if isinstance(previous, dict):
            try:
                prior_market = parse_market(previous)
            except MarketRejected:
                prior_market = None
            if prior_market is not None:
                prior = prior_market.final_price
        return resolve_open(market.price_to_beat, prior, self.twap.open_price(market.window_start))

    def _heartbeat(self, now: datetime) -> None:
        if self._last_beat is not None and (now - self._last_beat).total_seconds() < 60:
            return
        self._last_beat = now
        session = self.session
        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        market = "" if session is None else session.market.slug
        fee_rate = "" if session is None else format(session.fee.rate, "f")
        fee_exponent = "" if session is None else format(session.fee.exponent, "f")
        min_shares = "" if session is None else format(session.min_shares, "f")
        sigma_windows = 0 if session is None else session.volatility.windows_used
        has_open = False
        if session is not None:
            has_open = self._open_price(session.market) is not None
        self.journal.heartbeat(
            {
                "type": "heartbeat",
                "ts": moment.astimezone(timezone.utc).isoformat(),
                "mode": self.mode,
                "market": market,
                "cash": format(self.risk.cash, "f"),
                "realized_pnl": format(self.risk.realized, "f"),
                "halted": self.risk.halted,
                "halt_reason": self.risk.halt_reason or "",
                "has_spot": self.twap.spot() is not None,
                "has_open": has_open,
                "has_sigma": session is not None and session.volatility.sigma is not None,
                "fee_rate": fee_rate,
                "fee_exponent": fee_exponent,
                "min_shares": min_shares,
                "sigma_windows": sigma_windows,
            }
        )
        print(f"heartbeat paper {market} cash {format(self.risk.cash, 'f')}", flush=True)

    def _log_startup(self, now: datetime) -> None:
        session = self.session
        if session is None:
            return
        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        self.journal.startup(
            {
                "type": "startup",
                "ts": moment.astimezone(timezone.utc).isoformat(),
                "mode": self.mode,
                "market": session.market.slug,
                "series": session.market.series_slug,
                "fee_rate": format(session.fee.rate, "f"),
                "fee_exponent": format(session.fee.exponent, "f"),
                "fee_source": session.fee.source,
                "formula": session.fee.formula(),
                "min_shares": format(session.min_shares, "f"),
                "rules_sha256": session.rules_sha256,
                "resolution_source": session.market.resolution_source,
                "sigma_windows": session.volatility.windows_used,
            }
        )
        print(
            f"paper {session.market.slug} fee {session.fee.formula()} "
            f"min_shares {format(session.min_shares, 'f')}",
            flush=True,
        )
