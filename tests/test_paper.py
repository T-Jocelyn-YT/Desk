from decimal import Decimal

from desk.fees import FeeSchedule
from desk.paper import AskLevel, best_ask, paper_fill


def _schedule() -> FeeSchedule:
    return FeeSchedule(Decimal("0.03"), Decimal("1"), True, "fixture")


def test_best_ask_is_the_lowest_price_and_does_not_include_deeper_size():
    book = {
        "asks": [
            {"price": "0.50", "size": "100"},
            {"price": "0.40", "size": "2"},
            {"price": "0.40", "size": "3"},
            {"price": "0.39", "size": "0"},
        ]
    }
    ask = best_ask(book)
    assert ask is not None
    assert ask.price == Decimal("0.40")
    assert ask.size == Decimal("5")


def test_fill_uses_the_best_ask_and_caps_at_displayed_size():
    ask = AskLevel(Decimal("0.40"), Decimal("6"))
    fill = paper_fill(ask, Decimal("10"), _schedule(), Decimal("5"))
    assert fill is not None
    assert fill.price == Decimal("0.40")
    assert fill.shares == Decimal("6")
    assert fill.notional == Decimal("2.40")
    assert fill.fee == Decimal("6") * Decimal("0.03") * Decimal("0.40") * Decimal("0.60")
    assert fill.available == Decimal("6")


def test_fill_does_not_drop_below_the_minimum_when_the_book_is_short():
    ask = AskLevel(Decimal("0.40"), Decimal("4"))
    assert paper_fill(ask, Decimal("10"), _schedule(), Decimal("5")) is None


def test_object_asks_from_the_clob_client_shape_are_read():
    class Level:
        def __init__(self, price: str, size: str) -> None:
            self.price = price
            self.size = size

    class Book:
        asks = [Level("0.62", "1"), Level("0.55", "9")]
        min_order_size = "5"

    ask = best_ask(Book())
    assert ask is not None
    assert ask.price == Decimal("0.55")
    assert ask.size == Decimal("9")
    fill = paper_fill(ask, Decimal("5"), _schedule(), Decimal("5"))
    assert fill is not None
    assert fill.price == Decimal("0.55")
    assert fill.shares == Decimal("5")
    assert fill.fee == _schedule().fee(Decimal("5"), Decimal("0.55"))
