from datetime import datetime, timedelta, timezone
from decimal import Decimal

from desk.limits import (
    BANKROLL_USD,
    DAILY_STOP_FRACTION,
    EDGE_POINTS,
    ENTRY_SECONDS,
    PER_BET_USD,
    PRICE_CEILING,
    WINDOW_CAP_USD,
    WINDOW_SECONDS,
    daily_stop_from_cash,
    displayed_limits,
    quantize_money,
)
from desk.risk import (
    Approval,
    Position,
    Rejection,
    decide,
    initial_state,
    morning_lock_from_record,
)
from tests.support import schedule

WINDOW = 1_700_002_800
INSIDE = datetime.fromtimestamp(WINDOW + 900 - 120, tz=timezone.utc)


def _decide(state, **overrides):
    params = {
        "now": INSIDE,
        "window_start": WINDOW,
        "side": "Up",
        "fair": Decimal("0.99"),
        "ask_price": Decimal("0.50"),
        "ask_size": Decimal("100"),
        "schedule": schedule("0"),
        "min_shares": Decimal("1"),
    }
    params.update(overrides)
    return decide(state, **params)


def test_displayed_limits_match_the_constants_and_do_not_set_them():
    state = initial_state()
    shown = displayed_limits()
    assert state.cash == BANKROLL_USD == Decimal("50")
    assert shown["bankroll_usd"] == "50"
    assert shown["per_bet_usd"] == "1.00"
    assert PER_BET_USD == Decimal("1.00")
    assert shown["window_cap_usd"] == "5.00"
    assert WINDOW_CAP_USD == Decimal("5")
    assert shown["daily_stop_fraction"] == "0.20"
    assert DAILY_STOP_FRACTION == Decimal("0.20")
    assert "daily_stop_usd" not in shown
    assert shown["price_ceiling"] == "0.72"
    assert PRICE_CEILING == Decimal("0.72")
    assert shown["edge_points"] == "0.06"
    assert EDGE_POINTS == Decimal("0.06")


def test_price_ceiling_allows_72_cents_and_rejects_above_it():
    allowed = _decide(initial_state(), ask_price=Decimal("0.72"))
    assert isinstance(allowed, Approval)
    blocked = _decide(initial_state(), ask_price=Decimal("0.73"))
    assert isinstance(blocked, Rejection)
    assert blocked.reason == "price_ceiling"


def test_last_four_minutes_only():
    early = datetime.fromtimestamp(WINDOW + 900 - 241, tz=timezone.utc)
    opening = datetime.fromtimestamp(WINDOW + 900 - 240, tz=timezone.utc)
    closed = datetime.fromtimestamp(WINDOW + 900, tz=timezone.utc)
    assert _decide(initial_state(), now=early).reason == "entry_window"
    assert isinstance(_decide(initial_state(), now=opening), Approval)
    assert _decide(initial_state(), now=closed).reason == "entry_window"


def test_six_point_edge_is_measured_after_the_fee():
    # At 50¢ with a supplied coefficient of 0.07, the fee per share is 0.0175.
    # Fair odds equal to the ask plus 6 points do not clear the rule once the
    # fee is deducted. Fair odds of ask + fee + 6 points do.
    state = initial_state()
    fee_schedule = schedule("0.07")
    short = _decide(
        state,
        fair=Decimal("0.56"),
        schedule=fee_schedule,
        min_shares=Decimal("1"),
    )
    assert isinstance(short, Rejection)
    assert short.reason == "edge"
    exact = _decide(
        initial_state(),
        fair=Decimal("0.5775"),
        schedule=fee_schedule,
        min_shares=Decimal("1"),
    )
    assert isinstance(exact, Approval)
    assert exact.edge == Decimal("0.06")
    just_under = _decide(
        initial_state(),
        fair=Decimal("0.5774"),
        schedule=fee_schedule,
        min_shares=Decimal("1"),
    )
    assert isinstance(just_under, Rejection)
    assert just_under.reason == "edge"


def test_zero_fee_schedule_accepts_an_exact_six_point_edge():
    decision = _decide(initial_state(), fair=Decimal("0.56"), schedule=schedule("0"))
    assert isinstance(decision, Approval)
    assert decision.edge == Decimal("0.06")


def test_per_bet_stays_at_one_dollar_when_the_minimum_fits():
    small_minimum = _decide(initial_state(), ask_price=Decimal("0.10"), min_shares=Decimal("5"))
    assert isinstance(small_minimum, Approval)
    assert small_minimum.shares == Decimal("10")
    assert small_minimum.notional == Decimal("1.0")

    exact = _decide(initial_state(), ask_price=Decimal("0.20"), min_shares=Decimal("5"))
    assert isinstance(exact, Approval)
    assert exact.shares == Decimal("5")
    assert exact.notional == Decimal("1.00")


def test_minimum_that_costs_more_than_the_per_bet_cap_is_skipped():
    # 5 shares at 22¢ is $1.10, above the $1 cap.
    state = initial_state()
    skipped = _decide(state, ask_price=Decimal("0.22"), min_shares=Decimal("5"))
    assert isinstance(skipped, Rejection)
    assert skipped.reason == "per_bet"
    assert state.positions == []
    assert state.halted is False
    assert state.cash == BANKROLL_USD

    above = _decide(initial_state(), ask_price=Decimal("0.50"), min_shares=Decimal("5"))
    assert isinstance(above, Rejection)
    assert above.reason == "per_bet"

    seventy_two = _decide(initial_state(), ask_price=Decimal("0.72"), min_shares=Decimal("5"))
    assert isinstance(seventy_two, Rejection)
    assert seventy_two.reason == "per_bet"


def test_minimum_order_size_is_the_value_passed_in_not_a_constant_five():
    blocked = _decide(
        initial_state(),
        ask_price=Decimal("0.10"),
        ask_size=Decimal("6"),
        min_shares=Decimal("8"),
    )
    assert isinstance(blocked, Rejection)
    assert blocked.reason == "book"

    taken = _decide(
        initial_state(),
        ask_price=Decimal("0.10"),
        ask_size=Decimal("8"),
        min_shares=Decimal("8"),
    )
    assert isinstance(taken, Approval)
    assert taken.shares == Decimal("8")
    assert taken.notional == Decimal("0.80")


def test_book_smaller_than_the_minimum_is_not_a_fill():
    decision = _decide(
        initial_state(),
        ask_size=Decimal("4"),
        min_shares=Decimal("5"),
        ask_price=Decimal("0.40"),
    )
    assert isinstance(decision, Rejection)
    assert decision.reason == "book"


def test_book_caps_the_fill_when_it_still_covers_the_minimum():
    decision = _decide(
        initial_state(),
        ask_price=Decimal("0.10"),
        ask_size=Decimal("7"),
        min_shares=Decimal("5"),
    )
    assert isinstance(decision, Approval)
    assert decision.shares == Decimal("7")
    assert decision.notional == Decimal("0.70")


def test_window_cap_rejects_when_prior_stake_in_the_window_leaves_too_little_room():
    state = initial_state()
    state.ensure_day(INSIDE)
    state.positions.append(
        Position(
            window_start=WINDOW,
            side="Up",
            shares=Decimal("10"),
            price=Decimal("0.45"),
            notional=Decimal("4.50"),
            fee=Decimal("0"),
        )
    )
    state.cash -= Decimal("4.50")
    state.settle(WINDOW, Decimal("100"), Decimal("101"))
    decision = _decide(state, ask_price=Decimal("0.50"), min_shares=Decimal("1"))
    assert isinstance(decision, Rejection)
    assert decision.reason == "window_cap"


def test_daily_stop_blocks_another_bet_once_open_risk_reaches_ten_dollars():
    # A fresh book locks 20% of the $50 bankroll, which is $10.
    # The window cap is $5, so that stop takes two windows of $1 buys.
    state = initial_state()
    for index in range(2):
        window = WINDOW + index * WINDOW_SECONDS
        now = datetime.fromtimestamp(window + WINDOW_SECONDS - 120, tz=timezone.utc)
        for _ in range(5):
            decision = _decide(
                state,
                now=now,
                window_start=window,
                ask_price=Decimal("0.20"),
                min_shares=Decimal("5"),
            )
            assert isinstance(decision, Approval)
            assert decision.notional == Decimal("1.00")
            state.record(window, decision)
    assert state.open_worst() == Decimal("10.00")
    assert state.daily_room() == Decimal("0.00")
    next_window = WINDOW + 2 * WINDOW_SECONDS
    next_now = datetime.fromtimestamp(next_window + WINDOW_SECONDS - 120, tz=timezone.utc)
    blocked = _decide(
        state,
        now=next_now,
        window_start=next_window,
        ask_price=Decimal("0.20"),
        min_shares=Decimal("5"),
    )
    assert isinstance(blocked, Rejection)
    assert blocked.reason == "daily_stop"

    seventy_two = _decide(
        state,
        now=next_now,
        window_start=next_window,
        ask_price=Decimal("0.72"),
        min_shares=Decimal("1"),
    )
    assert isinstance(seventy_two, Rejection)
    assert seventy_two.reason == "daily_stop"


def test_daily_stop_resets_on_the_next_utc_day_and_cash_stays_reduced():
    state = initial_state()
    first = _decide(state, ask_price=Decimal("0.20"), min_shares=Decimal("5"), schedule=schedule("0"))
    state.record(WINDOW, first)
    state.settle(WINDOW, Decimal("2"), Decimal("1"))
    assert state.realized == Decimal("-1.00")
    assert state.cash == Decimal("49.00")
    later = INSIDE + timedelta(days=1)
    later_window = WINDOW + (24 * 60 * 60)
    decision = _decide(
        state,
        now=later,
        window_start=later_window,
        ask_price=Decimal("0.20"),
        min_shares=Decimal("5"),
        schedule=schedule("0"),
    )
    assert isinstance(decision, Approval)
    assert state.daily_stop == daily_stop_from_cash(Decimal("49.00"))
    assert state.morning_cash == quantize_money(Decimal("49.00"))


def test_bankroll_rejects_when_cash_cannot_pay_for_the_order():
    # Morning cash is the $50 bankroll, so the locked stop is $10 and still
    # has room. The order is rejected because cash cannot pay for it.
    state = initial_state()
    state.ensure_day(INSIDE)
    assert state.daily_stop == daily_stop_from_cash(BANKROLL_USD)
    state.cash = Decimal("0.50")
    decision = _decide(
        state,
        ask_price=Decimal("0.20"),
        min_shares=Decimal("5"),
        schedule=schedule("0"),
    )
    assert isinstance(decision, Rejection)
    assert decision.reason == "bankroll"
    assert state.positions == []
    assert state.cash == Decimal("0.50")


def test_settlement_reports_outcome_open_final_and_pnl_after_fees():
    state = initial_state()
    bought = _decide(
        state,
        ask_price=Decimal("0.20"),
        min_shares=Decimal("5"),
        schedule=schedule("0.03"),
    )
    assert isinstance(bought, Approval)
    state.record(WINDOW, bought)
    cash_after_buy = state.cash
    won = state.settle(WINDOW, Decimal("1000"), Decimal("1001"))
    assert len(won) == 1
    assert won[0].outcome == "Up"
    assert won[0].open_price == Decimal("1000")
    assert won[0].final_price == Decimal("1001")
    assert won[0].realized_pnl == won[0].shares - won[0].notional - won[0].fee
    assert state.realized == won[0].realized_pnl
    assert state.cash == cash_after_buy + won[0].shares
    assert state.settle(WINDOW, Decimal("1000"), Decimal("1001")) == []

    loser = initial_state()
    bought = _decide(
        loser,
        ask_price=Decimal("0.20"),
        min_shares=Decimal("5"),
        schedule=schedule("0.03"),
    )
    assert isinstance(bought, Approval)
    loser.record(WINDOW, bought)
    cash_after_buy = loser.cash
    lost = loser.settle(WINDOW, Decimal("1000"), Decimal("999"))
    assert lost[0].outcome == "Down"
    assert lost[0].open_price == Decimal("1000")
    assert lost[0].final_price == Decimal("999")
    assert lost[0].realized_pnl == -(lost[0].notional + lost[0].fee)
    assert loser.cash == cash_after_buy

    tied = initial_state()
    bought = _decide(tied, schedule=schedule("0"), min_shares=Decimal("1"))
    assert isinstance(bought, Approval)
    tied.record(WINDOW, bought)
    tie = tied.settle(WINDOW, Decimal("100"), Decimal("100"))
    assert tie[0].outcome == "Up"
    assert tie[0].realized_pnl == tie[0].shares - tie[0].notional - tie[0].fee


def test_tie_settles_up():
    state = initial_state()
    up = _decide(state, schedule=schedule("0"), min_shares=Decimal("1"))
    state.record(WINDOW, up)
    down = _decide(
        state,
        side="Down",
        fair=Decimal("0.99"),
        ask_price=Decimal("0.50"),
        schedule=schedule("0"),
        min_shares=Decimal("1"),
    )
    assert isinstance(down, Approval)
    state.record(WINDOW, down)
    state.settle(WINDOW, Decimal("100"), Decimal("100"))
    up_position, down_position = state.positions
    assert up_position.settled and down_position.settled
    # Tie resolves Up. Each side was a $1 notional. Up pays 2 (two shares).
    assert state.cash == Decimal("50")


def _heartbeat(ts: str, cash: str, realized: str = "0") -> dict:
    return {
        "type": "heartbeat",
        "ts": ts,
        "mode": "paper",
        "cash": cash,
        "realized_pnl": realized,
    }


def _fill(ts: str, notional: str = "1.00") -> dict:
    return {
        "type": "fill",
        "ts": ts,
        "mode": "paper",
        "market": "eth-updown-15m-1700000000",
        "side": "Up",
        "price": "0.20",
        "shares": "5",
        "notional": notional,
        "fee": "0",
    }


def test_twenty_percent_of_owner_morning_cash_rounds_to_the_usdc_quantum():
    # 309.82909 * 0.20 = 61.965818, which rounds half up to 61.96582.
    morning = Decimal("309.82909")
    assert daily_stop_from_cash(morning) == Decimal("61.96582")
    assert quantize_money(morning) == morning


def test_daily_stop_locks_once_and_rolls_at_08_00_shanghai():
    shanghai = timezone(timedelta(hours=8))
    before = datetime(2026, 11, 26, 7, 59, tzinfo=shanghai)
    at_open = datetime(2026, 11, 26, 8, 0, tzinfo=shanghai)
    assert before.astimezone(timezone.utc) == datetime(2026, 11, 25, 23, 59, tzinfo=timezone.utc)
    assert at_open.astimezone(timezone.utc) == datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)

    state = initial_state()
    state.cash = Decimal("309.82909")
    state.ensure_day(before)
    assert state.day == "2026-11-25"
    assert state.morning_cash == Decimal("309.82909")
    assert state.daily_stop == Decimal("61.96582")
    state.positions.append(
        Position(
            window_start=WINDOW,
            side="Up",
            shares=Decimal("1"),
            price=Decimal("0.20"),
            notional=Decimal("0.20"),
            fee=Decimal("0"),
            settled=True,
        )
    )
    state.cash = Decimal("200")
    state.realized = Decimal("-9")
    state.halted = True
    state.halt_reason = "daily_stop"
    state.fills_today = 4
    state.ensure_day(before + timedelta(seconds=30))
    assert state.daily_stop == Decimal("61.96582")
    assert state.morning_cash == Decimal("309.82909")
    assert state.halted is True

    summary = state.ensure_day(at_open)
    assert summary is not None
    assert summary["date"] == "2026-11-25"
    assert state.day == "2026-11-26"
    assert state.realized == Decimal("0")
    assert state.halted is False
    assert state.halt_reason is None
    assert state.fills_today == 0
    assert state.cash == Decimal("200")
    assert len(state.positions) == 1
    assert state.morning_cash == quantize_money(Decimal("200"))
    assert state.daily_stop == daily_stop_from_cash(Decimal("200"))
    state.cash = Decimal("10")
    state.ensure_day(datetime(2026, 11, 26, 9, 0, tzinfo=shanghai))
    assert state.daily_stop == daily_stop_from_cash(Decimal("200"))
    assert state.cash == Decimal("10")
    assert BANKROLL_USD == Decimal("50")
    assert PER_BET_USD == Decimal("1.00")
    assert WINDOW_CAP_USD == Decimal("5")
    assert PRICE_CEILING == Decimal("0.72")
    assert EDGE_POINTS == Decimal("0.06")
    assert ENTRY_SECONDS == 4 * 60


def test_locked_stop_allows_a_loss_past_ten_dollars_and_blocks_at_the_locked_amount():
    state = initial_state()
    state.cash = Decimal("309.82909")
    state.ensure_day(INSIDE)
    assert state.daily_stop == Decimal("61.96582")
    state.cash = Decimal("40")
    state.realized = Decimal("-10")
    allowed = _decide(
        state,
        ask_price=Decimal("0.20"),
        min_shares=Decimal("5"),
        schedule=schedule("0"),
    )
    assert isinstance(allowed, Approval)
    assert state.daily_stop == Decimal("61.96582")
    assert state.halted is False

    blocked_state = initial_state()
    blocked_state.cash = Decimal("309.82909")
    blocked_state.ensure_day(INSIDE)
    blocked_state.realized = Decimal("-61.90")
    blocked = _decide(
        blocked_state,
        ask_price=Decimal("0.20"),
        min_shares=Decimal("5"),
        schedule=schedule("0"),
    )
    assert isinstance(blocked, Rejection)
    assert blocked.reason == "daily_stop"

    halted = initial_state()
    halted.cash = Decimal("309.82909")
    halted.ensure_day(INSIDE)
    halted.realized = Decimal("-61.96582")
    halted.ensure_day(INSIDE)
    assert halted.halted is True
    assert halted.halt_reason == "daily_stop"
    assert halted.daily_stop == Decimal("61.96582")


def test_incomplete_log_uses_the_last_verified_cash_not_zero():
    # Two heartbeats before 00:00 UTC. The fill log never explains the cash.
    # The gap between them is not zero P&L, and the missing prefix is not $0
    # or the $50 bankroll.
    now = datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc)
    state = initial_state()
    state.replay(
        [],
        now,
        heartbeats=[
            _heartbeat("2026-11-25T22:00:00+00:00", "100.00000", "0"),
            _heartbeat("2026-11-25T23:30:00+00:00", "80.00000", "-20.00000"),
        ],
    )
    assert state.day == "2026-11-26"
    assert state.cash == Decimal("80")
    assert state.morning_cash == Decimal("80")
    assert state.daily_stop == daily_stop_from_cash(Decimal("80"))
    assert state.daily_stop == Decimal("16")
    assert state.realized == Decimal("0")
    assert state.halted is False
    assert state.cash != BANKROLL_USD
    assert state.cash != Decimal("0")
    assert state.morning_cash != Decimal("100")


def test_same_day_gap_keeps_the_locked_stop_and_does_not_zero_pnl():
    # The 02:00 fill is in the log. The 06:00 heartbeat does not match that
    # fill, so the stretch is missing: cash and P&L come from the heartbeat,
    # and the stop stays at 20% of the 00:30 verification.
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    state = initial_state()
    state.replay(
        [_fill("2026-11-26T02:00:00+00:00")],
        now,
        heartbeats=[
            _heartbeat("2026-11-26T00:30:00+00:00", "100.00000", "0"),
            _heartbeat("2026-11-26T06:00:00+00:00", "70.00000", "-30.00000"),
        ],
    )
    assert state.morning_cash == Decimal("100")
    assert state.daily_stop == Decimal("20")
    assert state.daily_stop != daily_stop_from_cash(Decimal("70"))
    assert state.cash == Decimal("70")
    assert state.realized == Decimal("-30")
    assert state.halted is True
    assert state.halt_reason == "daily_stop"


def test_fill_after_the_morning_lock_does_not_recompute_the_stop():
    now = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
    state = initial_state()
    state.replay(
        [_fill("2026-11-26T02:00:00+00:00", "1.00")],
        now,
        heartbeats=[_heartbeat("2026-11-26T00:30:00+00:00", "309.82909", "0")],
    )
    assert state.morning_cash == Decimal("309.82909")
    assert state.daily_stop == Decimal("61.96582")
    assert state.cash == Decimal("308.82909")
    assert state.halted is False


def test_morning_lock_record_prefers_cash_and_ignores_a_second_install():
    day = "2026-11-26"
    both = morning_lock_from_record(
        {"date": day, "cash": "309.82909", "daily_stop_usd": "10", "private_key": "nope"}
    )
    assert both is not None
    assert both.cash == Decimal("309.82909")
    assert both.stop == Decimal("61.96582")
    assert both.authority == "operator"
    stop_only = morning_lock_from_record({"date": day, "daily_stop_usd": "61.96582"})
    assert stop_only is not None
    assert stop_only.cash is None
    assert stop_only.stop == Decimal("61.96582")
    assert morning_lock_from_record({"date": "2026-13-01", "cash": "1"}) is None
    assert morning_lock_from_record({"date": day, "cash": "-1"}) is None
    assert morning_lock_from_record("{not-json") is None

    state = initial_state()
    assert both is not None and stop_only is not None
    state.install_lock(both)
    state.install_lock(stop_only)
    assert state.locks[day].stop == Decimal("61.96582")
    assert state.locks[day].cash == Decimal("309.82909")
