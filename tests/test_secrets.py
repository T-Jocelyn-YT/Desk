import re
from pathlib import Path

from tests.support import ROOT

FORBIDDEN_FIELDS = re.compile(
    r"(?i)^(?:export\s+)?(private[_-]?key|seed|mnemonic|api[_-]?secret|"
    r"api[_-]?key|passphrase|wallet[_-]?key|secret)\s*="
)
HEX_KEY = re.compile(r"\b0x[a-fA-F0-9]{64}\b")
BANNED_SOURCE = (
    "post_order",
    "create_order",
    "create_market_order",
    "private_key",
    "api_secret",
    "mnemonic",
    "seed_phrase",
    "polybolt",
    "ws-live-v2",
)


def _tracked_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "__pycache__", "logs"} for part in path.parts):
            continue
        files.append(path)
    return files


def test_repository_has_no_key_material_or_secret_fields():
    offenders = []
    for path in _tracked_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if HEX_KEY.search(text):
            offenders.append(f"{path}: hex key")
        for line in text.splitlines():
            if FORBIDDEN_FIELDS.search(line.strip()):
                offenders.append(f"{path}: {line.strip()}")
    assert offenders == []


def test_env_example_is_paper_mode_without_credential_fields():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MODE=paper" in text
    lowered = text.lower()
    for word in ("private", "seed", "mnemonic", "secret", "api_key", "passphrase"):
        assert word not in lowered


def test_desk_source_does_not_remember_a_fee_coefficient_or_send_orders():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src" / "desk").rglob("*.py")
    )
    assert "0.07" not in source
    assert "0.01" not in source
    lowered = source.lower()
    for banned in BANNED_SOURCE:
        assert banned not in lowered


def test_journal_drops_fields_outside_the_allowlist(tmp_path):
    from desk.journal import Journal

    journal = Journal(tmp_path)
    journal.fill(
        {
            "type": "fill",
            "ts": "2026-01-01T00:00:00+00:00",
            "mode": "paper",
            "market": "eth-updown-15m-1",
            "side": "Up",
            "price": "0.40",
            "shares": "5",
            "notional": "2",
            "fee": "0.01",
            "best_ask_size": "20",
            "fair": "0.90",
            "edge": "0.40",
            "fee_rate": "0.03",
            "fee_exponent": "1",
            "min_shares": "5",
            "private_key": "not-a-real-key",
            "api_secret": "not-a-real-secret",
        }
    )
    text = (tmp_path / "fills.jsonl").read_text(encoding="utf-8")
    assert "not-a-real-key" not in text
    assert "not-a-real-secret" not in text
    assert "private_key" not in text
