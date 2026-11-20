"""Run `pr_watch.cli.main` in a real process with a deterministic stopping point, for the process
tests of tests/test_signals.py. Usage: proc_launcher.py ARGS... (the arguments of `pr-watch`).

The test process cannot hit "inside the commit" or "between the snapshot and its commit" by
timing, so this launcher plants a pause where the test asks. It changes nothing else: the real
commands run as they do in production. Every pause is a handshake, never a sleep the test relies
on: the launcher writes `PRW_READY` (atomically, holding its pid) and then waits until `PRW_GO`
exists (at most 120 s, so a lost test cannot leave it hanging for long). A signal that arrives
meanwhile is handled by pr-watch's own handlers, exactly as in production.

Environment (all optional):

- `PRW_PAUSE_COMMIT=1`: pause in the first write transaction that qualifies, after its body ran
  and before COMMIT. A transaction qualifies when `PRW_PAUSE_SQL` (a query, run inside the
  transaction) returns a row, or, with no query, when the transaction changed something. The
  first `PRW_PAUSE_SKIP` (default 0) qualifying transactions run on.
- `PRW_PAUSE_AT=module:function`: pause before that function runs (patched before any pr_watch
  module that imports it by name is loaded).
- `PRW_ROLLBACK_FILE`: stall after `Store._rollback` (the first thing a signalled command does with
  an open transaction) has rolled it back: write this file, then wait until `PRW_GO` exists: the
  window for a second signal during shutdown.
- `PRW_NOTE_SIGNAL`: append a line to this file whenever the gateway is told to signal its
  children, i.e. once the poll's signal handler has run (the handshake for "the signal landed").
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PAUSE_LIMIT = 120.0


def _write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    Path(tmp).write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _wait_for_go() -> None:
    go = os.environ["PRW_GO"]
    end = time.monotonic() + PAUSE_LIMIT
    while not os.path.exists(go) and time.monotonic() < end:
        time.sleep(0.01)


def _pause(ready: str) -> None:
    _write_atomic(ready, str(os.getpid()))
    _wait_for_go()


def _patch_commit_pause() -> None:
    from pr_watch.store import Store

    original = Store.transaction
    skip = [int(os.environ.get("PRW_PAUSE_SKIP", "0"))]
    probe = os.environ.get("PRW_PAUSE_SQL")
    paused = [False]

    @contextmanager
    def transaction(self: Any) -> Any:
        with original(self) as conn:
            before = conn.total_changes
            yield conn
            if paused[0]:
                return
            hit = (
                conn.execute(probe).fetchone() is not None if probe else conn.total_changes > before
            )
            if hit and skip[0] > 0:
                skip[0] -= 1
            elif hit:
                paused[0] = True
                _pause(os.environ["PRW_READY"])

    Store.transaction = transaction  # type: ignore[method-assign]


def _patch_pause_at(target: str) -> None:
    module_name, _, name = target.partition(":")
    module = importlib.import_module(module_name)
    original = getattr(module, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _pause(os.environ["PRW_READY"])
        return original(*args, **kwargs)

    setattr(module, name, wrapper)


def _patch_rollback_stall(marker: str) -> None:
    from pr_watch.store import Store

    original = Store._rollback

    def stalled(self: Any) -> None:
        was_open = self.conn.in_transaction
        original(self)  # the rollback itself runs first, as in production ...
        if was_open:  # ... and the window for a second signal opens after it
            _write_atomic(marker, str(os.getpid()))
            _wait_for_go()

    Store._rollback = stalled  # type: ignore[method-assign]


def _patch_signal_note(path: str) -> None:
    from pr_watch import gateway

    original = gateway.signal_live

    def noted(*args: Any, **kwargs: Any) -> int:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("signalled\n")
        return original(*args, **kwargs)

    gateway.signal_live = noted  # type: ignore[assignment]


def main() -> int:
    env = os.environ
    if env.get("PRW_PAUSE_AT"):
        _patch_pause_at(env["PRW_PAUSE_AT"])
    if env.get("PRW_PAUSE_COMMIT"):
        _patch_commit_pause()
    if env.get("PRW_ROLLBACK_FILE"):
        _patch_rollback_stall(env["PRW_ROLLBACK_FILE"])
    if env.get("PRW_NOTE_SIGNAL"):
        _patch_signal_note(env["PRW_NOTE_SIGNAL"])
    from pr_watch.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
