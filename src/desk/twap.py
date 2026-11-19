"""Public Polymarket RTDS reader for Chainlink ETH/USD 60-second TWAP.

Socket: wss://ws-live-data.polymarket.com
Topic: crypto_prices_twap_sixty
Symbol: eth/usd

The price used for fair odds is `full_accuracy_value`, an E18 integer
string. The float `value` field is display-only and is never used.
No credential is sent.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from decimal import Decimal

from desk.limits import BOUNDARY_GRACE_MS, WINDOW_SECONDS


RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_twap_sixty"
SYMBOL = "eth/usd"
_E18 = Decimal(10) ** 18
_INTEGER = re.compile(r"-?\d+")


class TwapParseError(ValueError):
    pass


@dataclass(frozen=True)
class TwapTick:
    price: Decimal
    timestamp_ms: int
    symbol: str = SYMBOL


def decode_full_accuracy(raw: object) -> Decimal:
    """Decode a legacy RTDS E18 integer string. Decimal strings are rejected."""

    if not isinstance(raw, str) or _INTEGER.fullmatch(raw) is None:
        raise TwapParseError("full_accuracy_value must be an E18 integer string")
    return Decimal(raw) / _E18


def subscribe_frame() -> str:
    body = {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": TOPIC,
                "type": "update",
                "filters": json.dumps({"symbol": SYMBOL}, separators=(",", ":")),
            }
        ],
    }
    return json.dumps(body, separators=(",", ":"))


def parse_frame(raw: object) -> list[TwapTick]:
    """Parse one RTDS text frame into ETH/USD 60-second TWAP ticks."""

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if text == "" or text in {"PING", "PONG"}:
        return []
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(message, dict) or message.get("topic") != TOPIC:
        return []
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return []
    if str(payload.get("symbol") or "").lower() != SYMBOL:
        return []
    window = payload.get("window_s", payload.get("window_seconds"))
    if window is not None and int(window) != 60:
        return []
    rows = payload["data"] if isinstance(payload.get("data"), list) else [payload]
    ticks: list[TwapTick] = []
    for row in rows:
        if not isinstance(row, dict) or "full_accuracy_value" not in row:
            continue
        try:
            price = decode_full_accuracy(row.get("full_accuracy_value"))
            timestamp_ms = int(row["timestamp"])
        except (TwapParseError, KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        ticks.append(TwapTick(price=price, timestamp_ms=timestamp_ms))
    return ticks


class TwapBook:
    """Latest spot, plus a boundary print captured from the same stream.

    A tick is the window open only when its timestamp falls in the grace
    interval that starts at the window boundary. A later mid-window tick
    updates the spot and does not become the open. This is what lets
    pricing proceed when Gamma has not published `priceToBeat`, without
    treating an arbitrary later print as the open.
    """

    def __init__(self, grace_ms: int = BOUNDARY_GRACE_MS) -> None:
        self.grace_ms = grace_ms
        self._lock = threading.Lock()
        self._latest: TwapTick | None = None
        self._opens: dict[int, TwapTick] = {}
        self._closes: dict[int, TwapTick] = {}

    def observe(self, tick: TwapTick) -> None:
        with self._lock:
            if self._latest is None or tick.timestamp_ms >= self._latest.timestamp_ms:
                self._latest = tick
            window_start = (tick.timestamp_ms // 1000 // WINDOW_SECONDS) * WINDOW_SECONDS
            for start in (window_start, window_start + WINDOW_SECONDS):
                self._capture(self._opens, start, tick)
            previous = window_start - WINDOW_SECONDS
            for end in (window_start, window_start + WINDOW_SECONDS, previous + WINDOW_SECONDS):
                self._capture(self._closes, end, tick)

    def _capture(self, store: dict[int, TwapTick], boundary: int, tick: TwapTick) -> None:
        start_ms = boundary * 1000
        if not (start_ms <= tick.timestamp_ms <= start_ms + self.grace_ms):
            return
        current = store.get(boundary)
        if current is None or tick.timestamp_ms < current.timestamp_ms:
            store[boundary] = tick

    def spot(self) -> Decimal | None:
        with self._lock:
            if self._latest is None:
                return None
            return self._latest.price

    def open_price(self, window_start: int) -> Decimal | None:
        with self._lock:
            tick = self._opens.get(window_start)
            return None if tick is None else tick.price

    def close_price(self, window_end: int) -> Decimal | None:
        with self._lock:
            tick = self._closes.get(window_end)
            return None if tick is None else tick.price


def resolve_open(
    price_to_beat: Decimal | None,
    prior_final: Decimal | None,
    stream_open: Decimal | None,
) -> Decimal | None:
    """Official open, then the prior window's official close, then the stream.

    Returns as soon as any source is present. Callers do not wait on Gamma.
    """

    if price_to_beat is not None:
        return price_to_beat
    if prior_final is not None:
        return prior_final
    return stream_open


def run_rtds(book: TwapBook, stop: threading.Event) -> None:
    """Blocking reconnect loop. Run it on a daemon thread."""

    import asyncio

    asyncio.run(_rtds_loop(book, stop))


async def _rtds_loop(book: TwapBook, stop: threading.Event) -> None:
    import asyncio

    import websockets

    delay = 2
    while not stop.is_set():
        try:
            async with websockets.connect(RTDS_URL, open_timeout=15, close_timeout=5) as socket:
                await socket.send(subscribe_frame())
                delay = 2
                pinger = asyncio.create_task(_ping(socket, stop))
                try:
                    while not stop.is_set():
                        raw = await socket.recv()
                        for tick in parse_frame(raw):
                            book.observe(tick)
                finally:
                    pinger.cancel()
        except Exception as exc:
            if stop.is_set():
                return
            print(f"RTDS reconnect after {type(exc).__name__}", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def _ping(socket: object, stop: threading.Event) -> None:
    import asyncio

    while not stop.is_set():
        await asyncio.sleep(5)
        try:
            await socket.send("PING")  # type: ignore[attr-defined]
        except Exception:
            return


def start_rtds(book: TwapBook) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run_rtds, args=(book, stop), name="rtds", daemon=True)
    thread.start()
    return thread, stop
