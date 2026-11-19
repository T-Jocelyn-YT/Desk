from datetime import datetime, timezone

import pytest

from desk.app import DeskApp
from desk.geoblock import GeoblockError, parse_geoblock
from desk.journal import Journal
from tests.support import ROOT, FakePorts


def test_blocked_response_refuses_without_echoing_the_ip(tmp_path):
    payload = {"blocked": True, "ip": "203.0.113.42", "country": "US", "region": "NY"}
    with pytest.raises(GeoblockError) as caught:
        parse_geoblock(payload)
    message = str(caught.value)
    assert "blocked" in message
    assert "203.0.113.42" not in message
    assert "ip" not in message.lower()


def test_unblocked_response_discards_the_ip():
    status = parse_geoblock(
        {"blocked": False, "ip": "203.0.113.42", "country": "XX", "region": "YY"}
    )
    assert status.blocked is False
    assert status.country == "XX"
    assert not hasattr(status, "ip")


def test_missing_or_non_boolean_blocked_field_refuses():
    with pytest.raises(GeoblockError):
        parse_geoblock({"country": "XX"})
    with pytest.raises(GeoblockError):
        parse_geoblock({"blocked": "false"})
    with pytest.raises(GeoblockError):
        parse_geoblock(["blocked"])


def test_startup_refuses_when_the_port_reports_blocked(tmp_path):
    ports = FakePorts()
    ports.geoblock_payload = {
        "blocked": True,
        "ip": "203.0.113.42",
        "country": "US",
        "region": "VA",
    }
    app = DeskApp(ports, Journal(tmp_path), freeze_file=tmp_path / "FREEZE")
    now = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    with pytest.raises(GeoblockError) as caught:
        app.startup(now)
    assert "203.0.113.42" not in str(caught.value)
    assert list(tmp_path.glob("*.jsonl")) == []


def test_geoblock_url_is_the_official_endpoint():
    source = (ROOT / "src" / "desk" / "geoblock.py").read_text(encoding="utf-8")
    assert "https://polymarket.com/api/geoblock" in source
