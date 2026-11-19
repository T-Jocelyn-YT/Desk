from desk.markets import MarketRejected, is_eth_15m_up_down, parse_market, select_eth_15m
from tests.support import eth_event


def test_only_eth_15m_up_down_markets_are_selected():
    valid = eth_event(1_700_002_800)
    btc = eth_event(1_700_002_800, slug="btc-updown-15m-1700002800", asset="btc", series="btc-up-or-down-15m")
    five_minute = eth_event(1_700_002_800, slug="eth-updown-5m-1700002800", duration="5m")
    hourly = {
        "slug": "ethereum-up-or-down-hourly",
        "seriesSlug": "eth-up-or-down-hourly",
        "title": "Ethereum Up or Down hourly",
        "markets": [{"description": "hourly", "outcomes": ["Up", "Down"]}],
    }
    politics = {"slug": "will-it-rain", "title": "Will it rain?", "markets": []}
    wrong_config = eth_event(1_700_002_800, asset="btc")
    selected = select_eth_15m([btc, five_minute, hourly, politics, wrong_config, valid])
    assert [market.slug for market in selected] == [valid["slug"]]
    assert is_eth_15m_up_down(valid)
    assert not is_eth_15m_up_down(btc)
    assert not is_eth_15m_up_down(five_minute)


def test_series_slug_must_be_the_eth_15m_series():
    event = eth_event(1_700_002_800, series="eth-up-or-down-1h")
    assert not is_eth_15m_up_down(event)


def test_resolution_source_and_rules_come_from_the_payload():
    event = eth_event(1_700_002_800, rules="Custom fetched rules text for this window.")
    market = parse_market(event)
    assert market.rules_text == "Custom fetched rules text for this window."
    assert market.resolution_source.startswith("https://data.chain.link/streams/eth-usd-twap-60s-streams")
    assert market.tokens == {"Up": "token-up", "Down": "token-down"}


def test_stringified_outcomes_and_reversed_order():
    event = eth_event(
        1_700_002_800,
        outcomes='["Down", "Up"]',
        token_ids='["token-down", "token-up"]',
    )
    market = parse_market(event)
    assert market.tokens["Up"] == "token-up"
    assert market.tokens["Down"] == "token-down"


def test_missing_rules_or_wrong_source_are_rejected():
    try:
        parse_market(eth_event(1_700_002_800, rules=""))
    except MarketRejected as exc:
        assert exc.reason == "rules"
    else:
        raise AssertionError("empty rules were accepted")
    try:
        parse_market(eth_event(1_700_002_800, resolution="https://example.invalid/spot"))
    except MarketRejected as exc:
        assert exc.reason == "resolution_source"
    else:
        raise AssertionError("foreign resolution source was accepted")


def test_twap_config_must_match_eth_15m_60s():
    assert not is_eth_15m_up_down(eth_event(1_700_002_800, lookback=30))
    assert not is_eth_15m_up_down(eth_event(1_700_002_800, twap_enabled=False))
    assert not is_eth_15m_up_down(eth_event(1_700_002_800, duration="1h"))
