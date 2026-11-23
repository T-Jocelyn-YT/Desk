"""Append-only operator logs. Records are allowlisted so secrets cannot be written."""

from __future__ import annotations

import json
import threading
from pathlib import Path


FILL_KEYS = (
    "type",
    "ts",
    "mode",
    "market",
    "side",
    "price",
    "shares",
    "notional",
    "fee",
    "best_ask_size",
    "fair",
    "edge",
    "fee_rate",
    "fee_exponent",
    "min_shares",
)

HEARTBEAT_KEYS = (
    "type",
    "ts",
    "mode",
    "market",
    "cash",
    "realized_pnl",
    "halted",
    "halt_reason",
    "has_spot",
    "has_open",
    "has_sigma",
    "fee_rate",
    "fee_exponent",
    "min_shares",
    "sigma_windows",
)

SUMMARY_KEYS = (
    "type",
    "date",
    "mode",
    "fills",
    "notional",
    "fees",
    "realized_pnl",
    "reason",
    "halt_reason",
)

SETTLEMENT_KEYS = (
    "type",
    "ts",
    "mode",
    "market",
    "side",
    "outcome",
    "open_price",
    "final_price",
    "realized_pnl",
    "shares",
    "price",
    "notional",
    "fee",
)

STARTUP_KEYS = (
    "type",
    "ts",
    "mode",
    "market",
    "series",
    "fee_rate",
    "fee_exponent",
    "fee_source",
    "formula",
    "min_shares",
    "rules_sha256",
    "resolution_source",
    "sigma_windows",
)


class Journal:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def fill(self, record: dict) -> None:
        self._write("fills.jsonl", FILL_KEYS, record)

    def settlement(self, record: dict) -> None:
        """Append one resolved fill to the same log as the buy."""

        self._write("fills.jsonl", SETTLEMENT_KEYS, record)

    def read_fills(self) -> list[dict]:
        """Fill and settlement lines, in append order.

        A torn or non-JSON line is skipped. The writer only appends
        complete lines, so a crash mid-write cannot drop the earlier book.
        """

        path = self.directory / "fills.jsonl"
        with self._lock:
            if not path.exists():
                return []
            text = path.read_text(encoding="utf-8")
        rows: list[dict] = []
        for line in text.splitlines():
            if line.strip() == "":
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                rows.append(record)
        return rows

    def heartbeat(self, record: dict) -> None:
        self._write("heartbeat.jsonl", HEARTBEAT_KEYS, record)

    def daily(self, record: dict) -> None:
        self._write("daily-summary.jsonl", SUMMARY_KEYS, record)

    def startup(self, record: dict) -> None:
        self._write("startup.jsonl", STARTUP_KEYS, record)

    def _write(self, name: str, keys: tuple[str, ...], record: dict) -> None:
        clean = {key: record[key] for key in keys}
        line = json.dumps(clean, separators=(",", ":"), sort_keys=True)
        path = self.directory / name
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
