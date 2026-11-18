"""Paper is the only mode this build will run."""

from __future__ import annotations

from pathlib import Path


class LiveTradingDisabled(Exception):
    """A live-trading request was refused."""


class Settings:
    def __init__(self, mode: str, log_dir: Path, freeze_file: Path, once: bool) -> None:
        self.mode = mode
        self.log_dir = log_dir
        self.freeze_file = freeze_file
        self.once = once

    def as_public_dict(self) -> dict[str, str | bool]:
        return {
            "mode": self.mode,
            "log_dir": str(self.log_dir),
            "freeze_file": str(self.freeze_file),
            "once": self.once,
        }


def resolve_mode(argv: list[str], env: dict[str, str]) -> str:
    """Return `paper`.

    `--live`, or MODE / DESK_MODE set to live, does not enable trading.
    """

    if "--live" in argv:
        raise LiveTradingDisabled(
            "Live trading is disabled in this build. Paper mode is the only mode."
        )
    for flag in argv:
        lowered = flag.lower()
        if lowered.startswith("--private-key") or lowered.startswith("--seed") or lowered.startswith("--api-"):
            raise LiveTradingDisabled("This build does not accept wallet or API credentials.")
    raw = env.get("DESK_MODE", env.get("MODE", "paper"))
    requested = (raw or "paper").strip().lower()
    if requested == "live":
        raise LiveTradingDisabled(
            "Live trading is disabled in this build. Paper mode is the only mode."
        )
    if requested != "paper":
        raise LiveTradingDisabled(
            f"Unsupported mode {requested!r}. This build runs paper mode only."
        )
    return "paper"


def public_settings(argv: list[str], env: dict[str, str]) -> Settings:
    """Settings that can be printed. Credential fields in `env` are ignored."""

    mode = resolve_mode(argv, env)
    log_dir = Path(env.get("DESK_LOG_DIR", "logs"))
    freeze_file = Path(env.get("DESK_FREEZE_FILE", "FREEZE"))
    return Settings(
        mode=mode,
        log_dir=log_dir,
        freeze_file=freeze_file,
        once="--once" in argv,
    )
