import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from desk.app import DeskApp
from desk.journal import Journal
from desk.limits import BANKROLL_USD, WINDOW_SECONDS, daily_stop_from_cash
from desk.markets import slug_for, window_start_of
from desk.risk import trading_day
from desk.twap import TwapTick
from tests.support import Book, FakePorts, clob_info, eth_event, settled_series

WINDOW = 1_700_002_800
INSIDE = datetime.fromtimestamp(WINDOW + WINDOW_SECONDS - 120, tz=timezone.utc)
SHANGHAI = timezone(timedelta(hours=8))


def _install(ports: FakePorts, start: int, *, condition: str, rate: str = "0.03") -> dict:
    event = eth_event(
        start,
        rate=rate,
        condition=condition,
        price_to_beat="1000",
        min_size="5",
    )
    ports.events[event["slug"]] = event
    ports.clob[condition] = clob_info(rate=rate, mos="5")
    ports.books["token-up"] = Book([{"price": "0.20", "size": "20"}], min_order_size="5")
    ports.books["token-down"] = Book([{"price": "0.90", "size": "20"}], min_order_size="5")
    return event


def _ports(*starts: int, rate: str = "0.03") -> FakePorts:
    ports = FakePorts()
    for index, start in enumerate(starts):
        _install(ports, start, condition=f"condition-{index}", rate=rate)
    ports.settled = settled_series(starts[0])
    return ports


def _start(tmp_path, ports: FakePorts, when: datetime) -> DeskApp:
    app = DeskApp(ports, Journal(tmp_path), freeze_file=tmp_path / "FREEZE")
    app.twap.observe(TwapTick(price=Decimal("1100"), timestamp_ms=int(when.timestamp()) * 1000))
    app.startup(when)
    return app


def _lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _types(path):
    return [row["type"] for row in _lines(path)]


def _view(risk):
    return {
        "cash": risk.cash,
        "realized": risk.realized,
        "halted": risk.halted,
        "halt_reason": risk.halt_reason,
        "fills_today": risk.fills_today,
        "notional_today": risk.notional_today,
        "fees_today": risk.fees_today,
        "daily_room": risk.daily_room(),
        "positions": [
            (item.window_start, item.side, item.shares, item.price, item.notional, item.fee, item.settled)
            for item in risk.positions
        ],
    }


def test_fill_then_settle_then_restart_restores_the_book(tmp_path):
    ports = _ports(WINDOW, WINDOW + WINDOW_SECONDS)
    app = _start(tmp_path, ports, INSIDE)
    app.tick(INSIDE)
    close = Decimal("999")
    app.twap.observe(TwapTick(price=close, timestamp_ms=(WINDOW + WINDOW_SECONDS) * 1000 + 500))
    app.tick(datetime.fromtimestamp(WINDOW + WINDOW_SECONDS, tz=timezone.utc))
    before = _view(app.risk)
    assert before["cash"] < BANKROLL_USD
    assert before["realized"] < 0
    assert before["positions"][0][6] is True
    assert app.risk.window_notional(WINDOW) == before["positions"][0][4]
    written = _types(tmp_path / "fills.jsonl")
    assert written == ["fill", "settlement"]

    later = datetime.fromtimestamp(WINDOW + WINDOW_SECONDS + 30, tz=timezone.utc)
    assert trading_day(later) == trading_day(INSIDE)
    restored = _start(tmp_path, ports, later)
    assert _view(restored.risk) == before
    assert restored.risk.window_notional(WINDOW) == app.risk.window_notional(WINDOW)
    restored.tick(later)
    assert _types(tmp_path / "fills.jsonl") == written
    assert not (tmp_path / "FREEZE").exists()


def test_restart_before_settlement_keeps_the_position_and_settles_later(tmp_path):
    ports = _ports(WINDOW)
    app = _start(tmp_path, ports, INSIDE)
    app.tick(INSIDE)
    assert _types(tmp_path / "fills.jsonl") == ["fill"]
    open_book = _view(app.risk)
    assert open_book["realized"] == 0
    assert open_book["positions"][0][6] is False
    assert app.risk.window_notional(WINDOW) == open_book["positions"][0][4]
    assert app.risk.open_worst() == open_book["positions"][0][4] + open_book["positions"][0][5]

    restarted = _start(tmp_path, ports, INSIDE + timedelta(seconds=30))
    assert _view(restarted.risk) == open_book
    assert restarted.risk.window_notional(WINDOW) == app.risk.window_notional(WINDOW)
    assert _types(tmp_path / "fills.jsonl") == ["fill"]

    close = Decimal("999")
    restarted.twap.observe(TwapTick(price=close, timestamp_ms=(WINDOW + WINDOW_SECONDS) * 1000 + 500))
    restarted.tick(datetime.fromtimestamp(WINDOW + WINDOW_SECONDS, tz=timezone.utc))
    rows = _lines(tmp_path / "fills.jsonl")
    assert [row["type"] for row in rows] == ["fill", "settlement"]
    settlement = rows[1]
    assert settlement["market"] == slug_for(WINDOW)
    assert settlement["outcome"] == "Down"
    assert Decimal(settlement["open_price"]) == Decimal("1000")
    assert Decimal(settlement["final_price"]) == close
    assert restarted.risk.positions[0].settled is True
    assert restarted.risk.realized == Decimal(settlement["realized_pnl"])
    assert restarted.risk.realized < 0
    assert restarted.risk.cash == open_book["cash"]
    restarted.tick(datetime.fromtimestamp(WINDOW + WINDOW_SECONDS + 1, tz=timezone.utc))
    assert _types(tmp_path / "fills.jsonl") == ["fill", "settlement"]


def test_missed_previous_window_settles_when_official_prices_arrive(tmp_path):
    later_start = WINDOW + WINDOW_SECONDS
    ports = _ports(WINDOW, later_start)
    app = _start(tmp_path, ports, INSIDE)
    app.tick(INSIDE)
    app.tick(INSIDE + timedelta(seconds=1))
    assert _types(tmp_path / "fills.jsonl") == ["fill", "fill"]
    assert len(app.risk.positions) == 2
    assert all(not position.settled for position in app.risk.positions)

    rolled = datetime.fromtimestamp(later_start, tz=timezone.utc)
    app.tick(rolled)
    assert _types(tmp_path / "fills.jsonl") == ["fill", "fill"]
    assert app.session is not None
    assert app.session.market.window_start == later_start

    official = Decimal("999")
    ports.events[slug_for(WINDOW)]["eventMetadata"]["finalPrice"] = format(official, "f")
    app.twap.observe(TwapTick(price=Decimal("1001"), timestamp_ms=later_start * 1000 + 500))
    app.tick(rolled + timedelta(seconds=60))
    rows = _lines(tmp_path / "fills.jsonl")
    assert [row["type"] for row in rows] == ["fill", "fill", "settlement", "settlement"]
    for settlement in rows[2:]:
        assert settlement["market"] == slug_for(WINDOW)
        assert settlement["side"] == "Up"
        assert settlement["outcome"] == "Down"
        assert Decimal(settlement["open_price"]) == Decimal("1000")
        assert Decimal(settlement["final_price"]) == official
        assert Decimal(settlement["realized_pnl"]) < 0
    assert all(position.settled for position in app.risk.positions)
    assert app.risk.realized == sum((Decimal(row["realized_pnl"]) for row in rows[2:]), Decimal("0"))
    app.tick(rolled + timedelta(seconds=61))
    assert _types(tmp_path / "fills.jsonl") == ["fill", "fill", "settlement", "settlement"]


def test_missed_previous_window_settles_from_the_boundary_twap(tmp_path):
    later_start = WINDOW + WINDOW_SECONDS
    ports = _ports(WINDOW, later_start)
    app = _start(tmp_path, ports, INSIDE)
    app.tick(INSIDE)
    app.tick(datetime.fromtimestamp(later_start, tz=timezone.utc))
    assert _types(tmp_path / "fills.jsonl") == ["fill"]

    close = Decimal("999")
    app.twap.observe(TwapTick(price=close, timestamp_ms=later_start * 1000 + 500))
    app.tick(datetime.fromtimestamp(later_start + 60, tz=timezone.utc))
    settlement = _lines(tmp_path / "fills.jsonl")[1]
    assert settlement["type"] == "settlement"
    assert settlement["market"] == slug_for(WINDOW)
    assert settlement["outcome"] == "Down"
    assert Decimal(settlement["open_price"]) == Decimal("1000")
    assert Decimal(settlement["final_price"]) == close
    assert app.risk.positions[0].settled is True


def test_restart_across_the_utc_day_does_not_carry_the_previous_stop(tmp_path):
    # 00:00 UTC is 08:00 Asia/Shanghai. A fill at 07:13 Shanghai is still the
    # previous UTC day. A restart at 08:13 Shanghai is the next one.
    # A fresh book locks 20% of the $50 bankroll, which is $10. Each window
    # holds at most $5, so the loss that trips that stop is ten $1 fills
    # in the 23:00 and 23:15 UTC windows.
    boundary = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
    assert boundary.astimezone(SHANGHAI) == datetime(2026, 11, 23, 8, 0, tzinfo=SHANGHAI)
    assert trading_day(boundary - timedelta(seconds=1)) == "2026-11-22"
    assert trading_day(boundary) == "2026-11-23"

    first = datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc)
    w1 = window_start_of(first)
    w2 = w1 + WINDOW_SECONDS
    w_late = w2 + WINDOW_SECONDS
    w3 = window_start_of(boundary)
    ports = _ports(w1, w2, w_late, w3, rate="0")
    inside = datetime.fromtimestamp(w1 + WINDOW_SECONDS - 120, tz=timezone.utc)
    assert trading_day(inside) == "2026-11-22"
    assert inside.astimezone(SHANGHAI) == datetime(2026, 11, 23, 7, 13, tzinfo=SHANGHAI)

    app = _start(tmp_path, ports, inside)
    for offset in range(5):
        app.tick(inside + timedelta(seconds=offset))
    assert app.risk.fills_today == 5
    app.twap.observe(TwapTick(price=Decimal("999"), timestamp_ms=(w1 + WINDOW_SECONDS) * 1000 + 500))
    app.tick(datetime.fromtimestamp(w1 + WINDOW_SECONDS, tz=timezone.utc))
    assert app.risk.realized == Decimal("-5")
    assert app.risk.cash == Decimal("45")
    assert app.risk.halted is False
    assert all(position.settled for position in app.risk.positions)

    # The close print replaced the spot. Put the spot back above the open
    # before the next window's buys, without treating it as that window's open.
    app.twap.observe(TwapTick(price=Decimal("1100"), timestamp_ms=(w2 + 60) * 1000))
    inside_w2 = datetime.fromtimestamp(w2 + WINDOW_SECONDS - 120, tz=timezone.utc)
    assert trading_day(inside_w2) == "2026-11-22"
    assert inside_w2.astimezone(SHANGHAI) == datetime(2026, 11, 23, 7, 28, tzinfo=SHANGHAI)
    for offset in range(5):
        app.tick(inside_w2 + timedelta(seconds=offset))
    assert app.risk.fills_today == 10
    app.twap.observe(TwapTick(price=Decimal("999"), timestamp_ms=(w2 + WINDOW_SECONDS) * 1000 + 500))
    app.tick(datetime.fromtimestamp(w2 + WINDOW_SECONDS, tz=timezone.utc))
    assert app.risk.realized == Decimal("-10")
    assert app.risk.cash == Decimal("40")
    assert app.risk.halted is True
    assert app.risk.halt_reason == "daily_stop"
    assert all(position.settled for position in app.risk.positions)

    same_day = datetime.fromtimestamp(w_late + WINDOW_SECONDS - 120, tz=timezone.utc)
    assert trading_day(same_day) == "2026-11-22"
    assert same_day.astimezone(SHANGHAI) == datetime(2026, 11, 23, 7, 43, tzinfo=SHANGHAI)
    restored = _start(tmp_path, ports, same_day)
    assert restored.risk.realized == Decimal("-10")
    assert restored.risk.cash == Decimal("40")
    assert restored.risk.halted is True
    assert restored.risk.halt_reason == "daily_stop"
    assert restored.risk.morning_cash == Decimal("50")
    assert restored.risk.daily_stop == daily_stop_from_cash(Decimal("50"))
    assert restored.risk.daily_room() == restored.risk.daily_stop + restored.risk.realized
    assert restored.risk.window_notional(w1) == Decimal("5")
    assert restored.risk.window_notional(w2) == Decimal("5")
    assert len(restored.risk.positions) == 10
    before = _types(tmp_path / "fills.jsonl")
    restored.tick(same_day)
    assert _types(tmp_path / "fills.jsonl") == before

    fresh = _start(tmp_path / "fresh", ports, same_day)
    fresh.tick(same_day)
    assert _types(tmp_path / "fresh" / "fills.jsonl") == ["fill"]

    next_day = datetime.fromtimestamp(w3 + WINDOW_SECONDS - 120, tz=timezone.utc)
    assert trading_day(next_day) == "2026-11-23"
    assert next_day.astimezone(SHANGHAI) == datetime(2026, 11, 23, 8, 13, tzinfo=SHANGHAI)
    ending_cash = app.risk.cash
    rolled = _start(tmp_path, ports, next_day)
    assert ending_cash == Decimal("40")
    assert rolled.risk.cash == ending_cash
    assert rolled.risk.cash != BANKROLL_USD
    assert rolled.risk.realized == Decimal("0")
    assert rolled.risk.halted is False
    assert rolled.risk.halt_reason is None
    assert rolled.risk.fills_today == 0
    assert rolled.risk.notional_today == Decimal("0")
    assert rolled.risk.fees_today == Decimal("0")
    assert rolled.risk.open_worst() == Decimal("0")
    assert rolled.risk.morning_cash == Decimal("40")
    assert rolled.risk.daily_stop == daily_stop_from_cash(Decimal("40"))
    assert rolled.risk.daily_stop == Decimal("8")
    assert rolled.risk.daily_room() == rolled.risk.daily_stop
    assert len(rolled.risk.positions) == 10
    assert all(position.settled for position in rolled.risk.positions)
    rolled.tick(next_day)
    assert rolled.risk.fills_today == 1
    assert rolled.risk.cash == ending_cash - Decimal("1")
    assert rolled.risk.halted is False
    assert rolled.risk.realized == Decimal("0")
    assert [row["type"] for row in _lines(tmp_path / "fills.jsonl") if row["type"] == "fill"][-1] == "fill"
    assert _lines(tmp_path / "fills.jsonl")[-1]["market"] == slug_for(w3)


def test_operator_morning_cash_locks_the_owner_stop_and_ignores_a_later_line(tmp_path):
    day = trading_day(INSIDE)
    (tmp_path / "morning-cash.jsonl").write_text(
        "\n".join(
            [
                "{not json",
                json.dumps({"date": day, "cash": "309.82909", "daily_stop_usd": "10"}),
                json.dumps({"date": day, "cash": "1.00"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    beat = INSIDE - timedelta(hours=1)
    assert trading_day(beat) == day
    (tmp_path / "heartbeat.jsonl").write_text(
        json.dumps(
            {
                "type": "heartbeat",
                "ts": beat.isoformat(),
                "mode": "paper",
                "cash": "50",
                "realized_pnl": "0",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ports = _ports(WINDOW)
    written = (tmp_path / "morning-cash.jsonl").read_text(encoding="utf-8")
    app = _start(tmp_path, ports, INSIDE)
    assert app.risk.morning_cash == Decimal("309.82909")
    assert app.risk.daily_stop == Decimal("61.96582")
    assert app.risk.cash == Decimal("309.82909")
    assert app.risk.halted is False
    app.tick(INSIDE)
    assert app.risk.daily_stop == Decimal("61.96582")
    assert app.risk.morning_cash == Decimal("309.82909")
    assert app.risk.cash < Decimal("309.82909")
    fill = _lines(tmp_path / "fills.jsonl")[0]
    assert fill["type"] == "fill"
    assert Decimal(fill["notional"]) == Decimal("1.00")
    after_fill = app.risk.cash
    assert (tmp_path / "morning-cash.jsonl").read_text(encoding="utf-8") == written

    restored = _start(tmp_path, ports, INSIDE + timedelta(seconds=30))
    assert restored.risk.daily_stop == Decimal("61.96582")
    assert restored.risk.morning_cash == Decimal("309.82909")
    assert restored.risk.cash == after_fill
    assert (tmp_path / "morning-cash.jsonl").read_text(encoding="utf-8") == written


def test_stop_only_morning_line_locks_the_amount_without_replacing_cash(tmp_path):
    day = trading_day(INSIDE)
    (tmp_path / "morning-cash.jsonl").write_text(
        json.dumps({"date": day, "daily_stop_usd": "61.96582"}) + "\n",
        encoding="utf-8",
    )
    app = _start(tmp_path, _ports(WINDOW), INSIDE)
    assert app.risk.daily_stop == Decimal("61.96582")
    assert app.risk.morning_cash is None
    assert app.risk.cash == BANKROLL_USD
    assert len(_lines(tmp_path / "morning-cash.jsonl")) == 1


def test_incomplete_heartbeat_log_locks_last_verified_cash_across_restart(tmp_path):
    now = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
    start = window_start_of(now)
    ports = _ports(start)
    fill = {
        "type": "fill",
        "ts": "2026-11-26T01:00:00+00:00",
        "mode": "paper",
        "market": slug_for(start),
        "side": "Up",
        "price": "0.20",
        "shares": "5",
        "notional": "1.00",
        "fee": "0",
    }
    beat = {
        "type": "heartbeat",
        "ts": "2026-11-26T02:00:00+00:00",
        "mode": "paper",
        "cash": "309.82909",
        "realized_pnl": "0",
        "private_key": "not-written-by-us",
    }
    (tmp_path / "fills.jsonl").write_text(json.dumps(fill) + "\n", encoding="utf-8")
    (tmp_path / "heartbeat.jsonl").write_text(json.dumps(beat) + "\n", encoding="utf-8")
    app = _start(tmp_path, ports, now)
    assert app.risk.cash == Decimal("309.82909")
    assert app.risk.morning_cash == Decimal("309.82909")
    assert app.risk.daily_stop == Decimal("61.96582")
    assert app.risk.cash != Decimal("49")
    assert app.risk.cash != Decimal("0")
    assert app.risk.cash != BANKROLL_USD
    locked = _lines(tmp_path / "morning-cash.jsonl")
    assert len(locked) == 1
    assert locked[0]["source"] == "journal"
    assert locked[0]["date"] == "2026-11-26"
    assert locked[0]["cash"] == "309.82909"
    assert locked[0]["daily_stop_usd"] == "61.96582"
    assert "private_key" not in locked[0]

    again = _start(tmp_path, ports, now)
    assert again.risk.cash == Decimal("309.82909")
    assert again.risk.daily_stop == Decimal("61.96582")
    assert again.risk.morning_cash == Decimal("309.82909")
    assert len(_lines(tmp_path / "morning-cash.jsonl")) == 1


def test_morning_journal_drops_fields_outside_the_allowlist(tmp_path):
    journal = Journal(tmp_path)
    journal.morning(
        {
            "type": "morning_cash",
            "date": "2026-11-26",
            "cash": "309.82909",
            "daily_stop_usd": "61.96582",
            "source": "journal",
            "private_key": "not-a-real-key",
        }
    )
    text = (tmp_path / "morning-cash.jsonl").read_text(encoding="utf-8")
    assert "not-a-real-key" not in text
    assert "private_key" not in text
    assert "309.82909" in text
