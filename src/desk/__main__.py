"""Run the paper desk. Live trading cannot be selected."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from desk.app import DeskApp, FreezeStop, StartupError
from desk.geoblock import GeoblockError
from desk.journal import Journal
from desk.live import LivePorts
from desk.mode import LiveTradingDisabled, public_settings
from desk.risk import initial_state
from desk.twap import TwapBook, start_rtds


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        settings = public_settings(args, dict(os.environ))
    except LiveTradingDisabled as exc:
        print(str(exc), file=sys.stderr)
        return 4

    now = datetime.now(timezone.utc)
    if settings.freeze_file.exists():
        journal = Journal(settings.log_dir)
        state = initial_state()
        state.ensure_day(now)
        journal.daily(state.summary("freeze"))
        print("FREEZE file present; stopping", flush=True)
        return 3

    journal = Journal(settings.log_dir)
    app = DeskApp(
        ports=LivePorts(),
        journal=journal,
        twap=TwapBook(),
        freeze_file=settings.freeze_file,
    )
    try:
        app.startup(datetime.now(timezone.utc))
    except GeoblockError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        return 5

    _thread, stop = start_rtds(app.twap)
    try:
        while True:
            app.tick(datetime.now(timezone.utc))
            if settings.once:
                break
            time.sleep(1)
    except FreezeStop:
        print("FREEZE file present; stopping", flush=True)
        return 3
    except KeyboardInterrupt:
        app.close(datetime.now(timezone.utc), "shutdown")
        return 0
    finally:
        stop.set()
        if settings.once:
            app.close(datetime.now(timezone.utc), "shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
