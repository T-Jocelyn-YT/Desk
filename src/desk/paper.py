"""Paper fills at the current best ask, capped by the size on that level."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from desk.fees import FeeSchedule
from desk.numbers import as_decimal


@dataclass(frozen=True)
class AskLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class PaperFill:
    price: Decimal
    shares: Decimal
    notional: Decimal
    fee: Decimal
    available: Decimal


def _level_price_size(level: object) -> tuple[Decimal, Decimal]:
    if isinstance(level, dict):
        return as_decimal(level["price"]), as_decimal(level["size"])
    return as_decimal(getattr(level, "price")), as_decimal(getattr(level, "size"))


def best_ask(book: object) -> AskLevel | None:
    """Lowest ask price. Size is the total displayed at that price only."""

    asks = book.get("asks") if isinstance(book, dict) else getattr(book, "asks", None)
    if not asks:
        return None
    levels: list[tuple[Decimal, Decimal]] = []
    for level in asks:
        price, size = _level_price_size(level)
        if size > 0:
            levels.append((price, size))
    if not levels:
        return None
    best_price = min(price for price, _size in levels)
    available = sum((size for price, size in levels if price == best_price), Decimal("0"))
    return AskLevel(price=best_price, size=available)


def book_min_shares(book: object) -> Decimal:
    raw = book.get("min_order_size") if isinstance(book, dict) else getattr(book, "min_order_size", None)
    if raw in (None, ""):
        raise ValueError("order book did not include min_order_size")
    value = as_decimal(raw)
    if value <= 0:
        raise ValueError("order book min_order_size was not positive")
    return value


def paper_fill(
    ask: AskLevel,
    shares: Decimal,
    schedule: FeeSchedule,
    min_shares: Decimal,
) -> PaperFill | None:
    """Fill at `ask.price` for at most `ask.size` shares, then charge the fee."""

    filled = min(as_decimal(shares), ask.size)
    if filled <= 0 or filled < as_decimal(min_shares):
        return None
    fee = schedule.fee(filled, ask.price)
    return PaperFill(
        price=ask.price,
        shares=filled,
        notional=filled * ask.price,
        fee=fee,
        available=ask.size,
    )
