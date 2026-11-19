from decimal import Decimal

from desk.fair import (
    edge_points,
    fee_adjusted_fair,
    probability_down,
    probability_up,
)
from desk.volatility import sample_std, volatility_from_returns
from tests.support import schedule


def test_published_watcher_snapshot_lower_bound():
    """Watcher v1 published example, 18 Nov 2026 18:51 Asia/Shanghai.

    The open used there was the prior window's price-to-beat, treated as a
    cap because this window's open had not been published. Inputs are copied
    from that note. They are not a live quote from this terminal.
    """

    spot = Decimal("1805.805391495481")
    open_price = Decimal("1803.929378776958")
    sigma = Decimal("0.00235357")
    probability = probability_up(spot, open_price, sigma, 529)
    assert abs(probability - Decimal("0.7174")) < Decimal("0.0002")
    assert probability_down(spot, open_price, sigma, 529) == Decimal("1") - probability


def test_down_is_one_minus_up_and_the_terminal_cases_are_exact():
    assert probability_up(Decimal("100"), Decimal("100"), Decimal("0.01"), 0) == Decimal("1")
    assert probability_up(Decimal("99"), Decimal("100"), Decimal("0.01"), 0) == Decimal("0")
    assert probability_down(Decimal("110"), Decimal("100"), Decimal("0"), 30) == Decimal("0")


def test_six_point_interface_deducts_the_supplied_fee():
    fee_schedule = schedule("0.07")
    fair = Decimal("0.5775")
    price = Decimal("0.50")
    assert fee_adjusted_fair(fair, price, fee_schedule) - price == Decimal("0.06")
    assert edge_points(fair, price, fee_schedule) == Decimal("0.06")
    assert edge_points(Decimal("0.56"), price, fee_schedule) < Decimal("0.06")


def test_pricing_sigma_uses_the_last_16_windows_and_reports_a_95_window_check():
    returns = [Decimal(index) / Decimal("1000") for index in range(1, 101)]
    volatility = volatility_from_returns(returns)
    assert volatility.windows_used == 16
    assert volatility.sigma == sample_std(returns[-16:])
    assert volatility.check_windows == 95
    assert volatility.sigma_check == sample_std(returns[-95:])
    assert volatility.sigma != sample_std(returns)


def test_fewer_than_16_windows_does_not_invent_a_sigma():
    volatility = volatility_from_returns([Decimal("0.01"), Decimal("-0.01")])
    assert volatility.sigma is None
    assert volatility.windows_used == 2
    assert volatility.sigma_check is None
