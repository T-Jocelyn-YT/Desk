from decimal import Decimal

import pytest

from desk.fees import (
    FeeSchedule,
    FeeScheduleError,
    fee_schedule_from_clob,
    fee_schedule_from_gamma,
    reconcile_fees,
)
from tests.support import clob_info, eth_event


def test_curve_matches_the_published_100_share_table_when_rate_is_supplied():
    # Polymarket's fee table: 100 shares at $0.50 with coefficient 0.07 is $1.75.
    # The coefficient is an argument to this test, not a default in the app.
    schedule = FeeSchedule(Decimal("0.07"), Decimal("1"), True, "fixture")
    assert schedule.fee(Decimal("100"), Decimal("0.50")) == Decimal("1.75")


def test_exponent_is_read_from_the_schedule():
    schedule = FeeSchedule(Decimal("0.07"), Decimal("2"), True, "fixture")
    assert schedule.fee(Decimal("100"), Decimal("0.50")) == Decimal("0.4375")


def test_dust_fees_round_to_zero_and_half_up_reaches_the_minimum_unit():
    dust = FeeSchedule(Decimal("0.0000001"), Decimal("1"), True, "fixture")
    assert dust.fee(Decimal("1"), Decimal("0.50")) == Decimal("0")
    minimum = FeeSchedule(Decimal("0.00002"), Decimal("1"), True, "fixture")
    assert minimum.fee(Decimal("1"), Decimal("0.50")) == Decimal("0.00001")


def test_clob_fd_is_used_and_legacy_tbf_is_not():
    info = clob_info(rate="0.03", exponent="2", taker_base_fee=1000)
    schedule = fee_schedule_from_clob(info)
    assert schedule.rate == Decimal("0.03")
    assert schedule.exponent == Decimal("2")
    assert schedule.rate != Decimal("1000")
    assert schedule.rate != Decimal("0.10")
    assert "0.03" in schedule.formula()
    assert "1000" not in schedule.formula()


def test_missing_fd_is_an_error_rather_than_a_remembered_rate():
    with pytest.raises(FeeScheduleError):
        fee_schedule_from_clob({"tbf": 1000, "mos": 5})


def test_gamma_schedule_is_read_from_the_event():
    event = eth_event(1_700_002_800, rate="0.04", exponent="1")
    schedule = fee_schedule_from_gamma(event["markets"][0])
    assert schedule.rate == Decimal("0.04")
    assert schedule.exponent == Decimal("1")


def test_disabled_fees_are_zero_only_when_the_payload_says_so():
    event = eth_event(1_700_002_800, fees_enabled=False, rate="0")
    schedule = fee_schedule_from_gamma(event["markets"][0])
    assert schedule.rate == Decimal("0")
    assert schedule.fee(Decimal("10"), Decimal("0.50")) == Decimal("0")


def test_disabled_flag_with_a_nonzero_rate_is_rejected():
    event = eth_event(1_700_002_800, fees_enabled=False, rate="0.03")
    with pytest.raises(FeeScheduleError):
        fee_schedule_from_gamma(event["markets"][0])


def test_gamma_and_clob_must_agree():
    gamma = fee_schedule_from_gamma(eth_event(1_700_002_800, rate="0.04")["markets"][0])
    clob = fee_schedule_from_clob(clob_info(rate="0.03"))
    with pytest.raises(FeeScheduleError):
        reconcile_fees(gamma, clob)
    agreed = reconcile_fees(
        fee_schedule_from_gamma(eth_event(1_700_002_800, rate="0.03", exponent="2")["markets"][0]),
        fee_schedule_from_clob(clob_info(rate="0.03", exponent="2")),
    )
    assert agreed.rate == Decimal("0.03")
    assert agreed.exponent == Decimal("2")
    assert agreed.source == "clob.fd"


def test_maker_rebate_is_not_subtracted_from_the_taker_fee():
    event = eth_event(1_700_002_800, rate="0.04")
    assert event["markets"][0]["feeSchedule"]["rebateRate"] == "0.20"
    schedule = fee_schedule_from_gamma(event["markets"][0])
    assert schedule.fee(Decimal("100"), Decimal("0.50")) == Decimal("1")
