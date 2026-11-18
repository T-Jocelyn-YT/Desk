import os
import subprocess
import sys

from desk.clob import build_readonly_client
from desk.mode import LiveTradingDisabled, public_settings, resolve_mode
from tests.support import ROOT


def test_default_mode_is_paper():
    assert resolve_mode([], {}) == "paper"
    settings = public_settings([], {})
    assert settings.mode == "paper"
    assert settings.once is False


def test_explicit_paper_mode():
    assert resolve_mode([], {"MODE": "paper"}) == "paper"
    assert resolve_mode([], {"DESK_MODE": "PAPER"}) == "paper"


def test_live_flag_does_not_enable_trading():
    try:
        resolve_mode(["--live"], {"MODE": "paper"})
    except LiveTradingDisabled as exc:
        assert "disabled" in str(exc).lower()
    else:
        raise AssertionError("live flag was accepted")


def test_live_environment_does_not_enable_trading():
    try:
        resolve_mode([], {"MODE": "live"})
    except LiveTradingDisabled:
        return
    raise AssertionError("MODE=live was accepted")


def test_credential_flags_are_refused():
    try:
        resolve_mode(["--private-key", "not-a-real-key"], {})
    except LiveTradingDisabled as exc:
        assert "not-a-real-key" not in str(exc)
    else:
        raise AssertionError("credential flag was accepted")


def test_settings_ignore_credential_environment_values():
    settings = public_settings([], {"MODE": "paper", "PRIVATE_KEY": "not-a-real-key", "API_SECRET": "also-not"})
    rendered = str(settings.as_public_dict())
    assert settings.mode == "paper"
    assert "not-a-real-key" not in rendered
    assert "also-not" not in rendered


def test_readonly_clob_client_is_constructed_without_credentials():
    seen: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs

    build_readonly_client("https://clob.polymarket.com", client_cls=FakeClient)
    assert seen["args"] == ("https://clob.polymarket.com",)
    assert seen["kwargs"] == {}


def test_live_process_exits_without_trading():
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "MODE": "live",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "desk"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 4
    assert "disabled" in proc.stderr.lower()
    assert "not-a-real-key" not in proc.stderr
