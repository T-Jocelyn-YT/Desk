import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from desk.app import DeskApp, FreezeStop
from desk.journal import Journal
from desk.twap import TwapTick
from tests.support import ROOT, FakePorts, eth_event, clob_info, Book, settled_series

WINDOW = 1_700_002_800
INSIDE = datetime.fromtimestamp(WINDOW + 900 - 120, tz=timezone.utc)


def _ready() -> FakePorts:
    ports = FakePorts()
    event = eth_event(WINDOW, price_to_beat="1000")
    ports.events[event["slug"]] = event
    ports.clob[event["markets"][0]["conditionId"]] = clob_info()
    ports.books["token-up"] = Book([{"price": "0.40", "size": "20"}])
    ports.books["token-down"] = Book([{"price": "0.90", "size": "20"}])
    ports.settled = settled_series(WINDOW)
    return ports


def test_freeze_file_stops_the_tick_and_writes_a_summary(tmp_path):
    freeze = tmp_path / "FREEZE"
    ports = _ready()
    app = DeskApp(ports, Journal(tmp_path / "logs"), freeze_file=freeze)
    app.startup(INSIDE)
    freeze.write_text("stop\n", encoding="utf-8")
    with pytest.raises(FreezeStop):
        app.tick(INSIDE)
    summary = (tmp_path / "logs" / "daily-summary.jsonl").read_text(encoding="utf-8")
    assert '"reason":"freeze"' in summary
    assert not (tmp_path / "logs" / "fills.jsonl").exists()


def test_freeze_process_exits_before_any_network_client(tmp_path):
    freeze = tmp_path / "FREEZE"
    freeze.write_text("stop\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT / "src"),
        "MODE": "paper",
        "DESK_FREEZE_FILE": str(freeze),
        "DESK_LOG_DIR": str(tmp_path / "logs"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "desk"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 3
    assert "FREEZE" in combined
    summary = json.loads((tmp_path / "logs" / "daily-summary.jsonl").read_text(encoding="utf-8"))
    assert summary["reason"] == "freeze"
    assert summary["mode"] == "paper"
    for key, value in os.environ.items():
        lowered = key.lower()
        if value and len(value) > 12 and any(word in lowered for word in ("secret", "token", "password", "private")):
            assert value not in combined


def test_freeze_after_a_fill_does_not_place_another(tmp_path):
    ports = _ready()
    freeze = tmp_path / "FREEZE"
    app = DeskApp(ports, Journal(tmp_path / "logs"), freeze_file=freeze)
    app.twap.observe(TwapTick(price=Decimal("1100"), timestamp_ms=int(INSIDE.timestamp()) * 1000))
    app.startup(INSIDE)
    app.tick(INSIDE)
    fills = (tmp_path / "logs" / "fills.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(fills) == 1
    freeze.write_text("stop\n", encoding="utf-8")
    with pytest.raises(FreezeStop):
        app.tick(INSIDE + timedelta(seconds=1))
    fills_after = (tmp_path / "logs" / "fills.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(fills_after) == 1
