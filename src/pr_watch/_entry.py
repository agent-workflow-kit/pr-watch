"""The entry point: stop signals are quiet from the very first import.

`pr_watch.cli` takes a while to import. A SIGINT in that window would print a KeyboardInterrupt
traceback, and a SIGTERM or SIGHUP would end the process by the signal instead of exit 128+N. This
module imports nothing heavy: it installs minimal handlers (exit 128+N at once, no traceback) and
only then imports the CLI, whose `main` installs its own handlers for the run.
"""

from __future__ import annotations

import os
import signal
import threading
from types import FrameType
from typing import NoReturn

_STOPS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def _exit_now(number: int, frame: FrameType | None) -> NoReturn:
    os._exit(128 + number)  # nothing is open yet, so nothing needs unwinding or flushing


def install_early_handlers() -> None:
    if threading.current_thread() is threading.main_thread():
        for number in _STOPS:
            signal.signal(number, _exit_now)


def main() -> int:
    install_early_handlers()
    from .cli import main as cli_main

    return cli_main()
