import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from desk.app import DeskApp, StartupError
from desk.journal import Journal
from desk.twap import TwapTick
from tests.support import RULES, Book, FakePorts, clob_info, eth_event, settled_series

WINDOW = 1_700_002_800
INSIDE = datetime.fromtimestamp(WINDOW + 900 - 120, tz=timezone.utc)


def _ports(rate: str = "0.03", exponent: str = "1", mos: str = "5") -> FakePorts:
    ports = FakePorts()
    event = eth_event(
        WINDOW,
        rate=rate,
        exponent=exponent,
        min_size=mos,
        price_to_beat="1000",
        condition="condition-current",
    )
    ports.events[event["slug"]] = event
    ports.clob["condition-current"] = clob_info(rate=rate, exponent=exponent, mos=mos)
    ports.books["token-up"] = Book([{"price": "0.40", "size": "20"}], min_order_size=mos)
    ports.books["token-down"] = Book([{"price": "0.90", "size": "20"}], min_order_size=mos)
    ports.settled = settled_series(WINDOW)
    return ports


def _app(tmp_path, ports: FakePorts) -> DeskApp:
    app = DeskApp(ports, Journal(tmp_path), freeze_file=tmp_path / "FREEZE")
    app.twap.observe(TwapTick(price=Decimal("1100"), timestamp_ms=int(INSIDE.timestamp()) * 1000))
    return app


def test_paper_fill_uses_best_ask_size_and_the_mocked_fee(tmp_path):
    app = _app(tmp_path, _ports())
    assert app.mode == "paper"
    app.startup(INSIDE)
    app.tick(INSIDE)
    startup = json.loads((tmp_path / "startup.jsonl").read_text(encoding="utf-8"))
    assert startup["mode"] == "paper"
    assert startup["fee_rate"] == "0.03"
    assert startup["fee_exponent"] == "1"
    assert startup["fee_source"] == "clob.fd"
    assert startup["formula"] == "shares * 0.03 * (price * (1 - price)) ** 1"
    assert startup["min_shares"] == "5"
    assert startup["rules_sha256"] == hashlib.sha256(RULES.encode("utf-8")).hexdigest()
    assert "1000" not in startup["formula"]
    fill = json.loads((tmp_path / "fills.jsonl").read_text(encoding="utf-8"))
    assert fill["mode"] == "paper"
    assert fill["side"] == "Up"
    assert fill["price"] == "0.40"
    assert fill["shares"] == "5"
    assert Decimal(fill["notional"]) == Decimal("2.00")
    assert Decimal(fill["fee"]) == Decimal("0.036")
    assert fill["fee_rate"] == "0.03"
    assert fill["market"] == f"eth-updown-15m-{WINDOW}"
    assert "203.0.113.42" not in (tmp_path / "fills.jsonl").read_text(encoding="utf-8")


def test_fee_disagreement_refuses_startup(tmp_path):
    ports = _ports(rate="0.03")
    ports.clob["condition-current"] = clob_info(rate="0.04")
    app = _app(tmp_path, ports)
    with pytest.raises(StartupError):
        app.startup(INSIDE)
    assert not (tmp_path / "fills.jsonl").exists()


def test_minimum_is_taken_from_the_payload_when_it_is_not_five(tmp_path):
    app = _app(tmp_path, _ports(mos="8", rate="0.03"))
    app.startup(INSIDE)
    startup = json.loads((tmp_path / "startup.jsonl").read_text(encoding="utf-8"))
    assert startup["min_shares"] == "8"


def test_next_window_reads_a_new_coefficient(tmp_path):
    ports = _ports(rate="0.03")
    later_start = WINDOW + 900
    later = eth_event(
        later_start,
        rate="0.04",
        exponent="2",
        condition="condition-later",
        price_to_beat="1000",
    )
    ports.events[later["slug"]] = later
    ports.clob["condition-later"] = clob_info(rate="0.04", exponent="2")
    app = _app(tmp_path, ports)
    app.startup(INSIDE)
    app.tick(INSIDE + timedelta(seconds=900))
    lines = (tmp_path / "startup.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["fee_rate"] == "0.03"
    assert json.loads(lines[1])["fee_rate"] == "0.04"
    assert json.loads(lines[1])["fee_exponent"] == "2"


def test_short_book_does_not_walk_to_a_worse_ask(tmp_path):
    ports = _ports()
    ports.books["token-up"] = Book(
        [{"price": "0.40", "size": "4"}, {"price": "0.50", "size": "100"}]
    )
    app = _app(tmp_path, ports)
    app.startup(INSIDE)
    app.tick(INSIDE)
    assert not (tmp_path / "fills.jsonl").exists()


def test_no_sigma_means_no_buy(tmp_path):
    ports = _ports()
    ports.settled = []
    app = _app(tmp_path, ports)
    app.startup(INSIDE)
    app.tick(INSIDE)
    startup = json.loads((tmp_path / "startup.jsonl").read_text(encoding="utf-8"))
    assert startup["sigma_windows"] == 0
    assert not (tmp_path / "fills.jsonl").exists()


def test_heartbeat_is_written_once_a_minute(tmp_path):
    ports = _ports()
    app = DeskApp(ports, Journal(tmp_path), freeze_file=tmp_path / "absent")
    app.startup(INSIDE)
    app.tick(INSIDE)
    app.tick(INSIDE + timedelta(seconds=59))
    app.tick(INSIDE + timedelta(seconds=60))
    lines = (tmp_path / "heartbeat.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    beat = json.loads(lines[0])
    assert beat["mode"] == "paper"
    assert beat["fee_rate"] == "0.03"
    assert "private_key" not in beat


def test_missing_price_to_beat_uses_the_boundary_tick_without_blocking(tmp_path):
    ports = _ports()
    event = eth_event(WINDOW, condition="condition-current")
    assert "priceToBeat" not in event["eventMetadata"]
    ports.events[event["slug"]] = event
    app = DeskApp(ports, Journal(tmp_path), freeze_file=tmp_path / "absent")
    start_ms = WINDOW * 1000
    app.twap.observe(TwapTick(price=Decimal("1000"), timestamp_ms=start_ms))
    app.twap.observe(TwapTick(price=Decimal("1100"), timestamp_ms=int(INSIDE.timestamp()) * 1000))
    app.startup(INSIDE)
    app.tick(INSIDE)
    fill = json.loads((tmp_path / "fills.jsonl").read_text(encoding="utf-8"))
    assert fill["side"] == "Up"
    assert fill["price"] == "0.40"


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_twap_close_writes_a_settlement_line_with_pnl_after_fees(tmp_path):
    app = _app(tmp_path, _ports())
    app.startup(INSIDE)
    app.tick(INSIDE)
    assert [row["type"] for row in _lines(tmp_path / "fills.jsonl")] == ["fill"]
    close = Decimal("1001")
    app.twap.observe(TwapTick(price=close, timestamp_ms=(WINDOW + 900) * 1000 + 500))
    app.tick(datetime.fromtimestamp(WINDOW + 900, tz=timezone.utc))
    rows = _lines(tmp_path / "fills.jsonl")
    assert [row["type"] for row in rows] == ["fill", "settlement"]
    settlement = rows[1]
    assert settlement["mode"] == "paper"
    assert settlement["market"] == f"eth-updown-15m-{WINDOW}"
    assert settlement["side"] == "Up"
    assert settlement["outcome"] == "Up"
    assert Decimal(settlement["open_price"]) == Decimal("1000")
    assert Decimal(settlement["final_price"]) == close
    assert Decimal(settlement["shares"]) == Decimal("5")
    assert Decimal(settlement["price"]) == Decimal("0.40")
    assert Decimal(settlement["notional"]) == Decimal("2.00")
    assert Decimal(settlement["fee"]) == Decimal("0.036")
    assert Decimal(settlement["realized_pnl"]) == Decimal("5") - Decimal("2.00") - Decimal("0.036")
    assert settlement["realized_pnl"] == format(app.risk.realized, "f")
    app.tick(datetime.fromtimestamp(WINDOW + 900 + 1, tz=timezone.utc))
    assert [row["type"] for row in _lines(tmp_path / "fills.jsonl")] == ["fill", "settlement"]


def test_losing_twap_close_logs_a_negative_pnl(tmp_path):
    app = _app(tmp_path, _ports())
    app.startup(INSIDE)
    app.tick(INSIDE)
    close = Decimal("999")
    app.twap.observe(TwapTick(price=close, timestamp_ms=(WINDOW + 900) * 1000 + 500))
    app.tick(datetime.fromtimestamp(WINDOW + 900, tz=timezone.utc))
    settlement = _lines(tmp_path / "fills.jsonl")[1]
    assert settlement["outcome"] == "Down"
    assert Decimal(settlement["open_price"]) == Decimal("1000")
    assert Decimal(settlement["final_price"]) == close
    assert Decimal(settlement["realized_pnl"]) == -(Decimal(settlement["notional"]) + Decimal(settlement["fee"]))


def test_official_final_price_is_the_one_written_when_gamma_has_published_it(tmp_path):
    ports = _ports()
    app = _app(tmp_path, ports)
    app.startup(INSIDE)
    app.tick(INSIDE)
    event = ports.events[f"eth-updown-15m-{WINDOW}"]
    event["eventMetadata"] = {"priceToBeat": "1000", "finalPrice": "1005"}
    app.twap.observe(TwapTick(price=Decimal("999"), timestamp_ms=(WINDOW + 900) * 1000 + 500))
    app.tick(datetime.fromtimestamp(WINDOW + 900, tz=timezone.utc))
    settlement = _lines(tmp_path / "fills.jsonl")[1]
    assert settlement["outcome"] == "Up"
    assert Decimal(settlement["open_price"]) == Decimal("1000")
    assert Decimal(settlement["final_price"]) == Decimal("1005")
    assert Decimal(settlement["realized_pnl"]) == Decimal(settlement["shares"]) - (
        Decimal(settlement["notional"]) + Decimal(settlement["fee"])
    )


def test_btc_payload_on_the_eth_slug_is_rejected(tmp_path):
    ports = _ports()
    ports.events[f"eth-updown-15m-{WINDOW}"] = eth_event(WINDOW, asset="btc", condition="condition-current")
    app = _app(tmp_path, ports)
    with pytest.raises(StartupError):
        app.startup(INSIDE)
