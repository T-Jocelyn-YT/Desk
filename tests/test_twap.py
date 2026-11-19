import json
from decimal import Decimal

from desk.twap import (
    RTDS_URL,
    SYMBOL,
    TOPIC,
    TwapBook,
    TwapTick,
    decode_full_accuracy,
    parse_frame,
    resolve_open,
    subscribe_frame,
)


def test_e18_decoder_uses_the_constructed_protocol_vector():
    # Constructed vector used by public RTDS decoder tests. Not a live print.
    assert decode_full_accuracy("65000123456789012345678") == Decimal("65000.123456789012345678")


def test_full_accuracy_beats_the_display_float_and_other_topics_are_ignored():
    frame = json.dumps(
        {
            "topic": TOPIC,
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1_700_002_800_000,
                "value": 1,
                "full_accuracy_value": "1800000000000000000000",
                "window_s": 60,
            },
        }
    )
    ticks = parse_frame(frame)
    assert len(ticks) == 1
    assert ticks[0].price == Decimal("1800")
    assert ticks[0].price != Decimal("1")

    display_only = json.dumps(
        {
            "topic": TOPIC,
            "type": "update",
            "payload": {"symbol": "eth/usd", "timestamp": 1, "value": 123, "window_s": 60},
        }
    )
    assert parse_frame(display_only) == []
    assert parse_frame("") == []
    assert parse_frame("PONG") == []
    other = json.dumps(
        {
            "topic": TOPIC,
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1,
                "full_accuracy_value": "1",
                "window_s": 60,
            },
        }
    )
    assert parse_frame(other) == []
    wrong_window = json.dumps(
        {
            "topic": TOPIC,
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1,
                "full_accuracy_value": "1800000000000000000000",
                "window_s": 30,
            },
        }
    )
    assert parse_frame(wrong_window) == []
    decimal_string = json.dumps(
        {
            "topic": TOPIC,
            "type": "update",
            "payload": {
                "symbol": "eth/usd",
                "timestamp": 1,
                "full_accuracy_value": "1800.0",
                "window_s": 60,
            },
        }
    )
    assert parse_frame(decimal_string) == []


def test_snapshot_rows_are_parsed_from_the_subscribe_frame():
    start_ms = 1_700_002_800_000
    frame = json.dumps(
        {
            "topic": TOPIC,
            "type": "subscribe",
            "payload": {
                "symbol": SYMBOL,
                "window_s": 60,
                "data": [
                    {
                        "timestamp": start_ms,
                        "full_accuracy_value": "1800000000000000000000",
                        "value": 1,
                    },
                    {
                        "timestamp": start_ms + 1000,
                        "full_accuracy_value": "1801000000000000000000",
                        "value": 1,
                    },
                ],
            },
        }
    )
    ticks = parse_frame(frame)
    book = TwapBook()
    for tick in ticks:
        book.observe(tick)
    assert book.open_price(1_700_002_800) == Decimal("1800")
    assert book.spot() == Decimal("1801")


def test_mid_window_tick_does_not_become_the_open():
    start = 1_700_002_800
    book = TwapBook()
    book.observe(TwapTick(Decimal("1000"), start * 1000 + 60_000))
    assert book.spot() == Decimal("1000")
    assert book.open_price(start) is None
    book.observe(TwapTick(Decimal("1001"), start * 1000 + 4_000))
    book.observe(TwapTick(Decimal("1002"), start * 1000 + 1_000))
    assert book.open_price(start) == Decimal("1002")
    assert book.close_price(start + 900) is None
    book.observe(TwapTick(Decimal("1003"), (start + 900) * 1000 + 500))
    assert book.close_price(start + 900) == Decimal("1003")
    book.observe(TwapTick(Decimal("1004"), (start + 900) * 1000 + 10_000))
    assert book.close_price(start + 900) == Decimal("1003")


def test_open_resolution_does_not_wait_for_price_to_beat():
    assert resolve_open(Decimal("10"), Decimal("9"), Decimal("8")) == Decimal("10")
    assert resolve_open(None, Decimal("9"), Decimal("8")) == Decimal("9")
    assert resolve_open(None, None, Decimal("8")) == Decimal("8")
    assert resolve_open(None, None, None) is None


def test_subscribe_frame_is_public_and_selects_eth_usd_twap():
    frame = json.loads(subscribe_frame())
    rendered = subscribe_frame()
    assert RTDS_URL == "wss://ws-live-data.polymarket.com"
    assert frame["action"] == "subscribe"
    subscription = frame["subscriptions"][0]
    assert subscription["topic"] == "crypto_prices_twap_sixty"
    assert subscription["type"] == "update"
    assert json.loads(subscription["filters"]) == {"symbol": "eth/usd"}
    lowered = rendered.lower()
    for forbidden in ("auth", "api_key", "secret", "passphrase", "clob_auth"):
        assert forbidden not in lowered
