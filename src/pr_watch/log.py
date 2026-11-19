"""`log.jsonl`: the public, append-only record of what pr-watch did (schemas/log.json).

One record is one JSON line (ASCII only, `\\n`-terminated) written with a single `write(2)` to a
descriptor opened `O_APPEND`, so concurrent processes never interleave inside a record. The file
is opened per record, relative to the state directory descriptor and with `O_NOFOLLOW`, and the
opened descriptor is checked (a regular file, owned by the user, not group- or world-writable): a
planted symlink, FIFO or foreign file is never written to.

Rotation at `MAX_BYTES` keeps `KEEP` files (`log.jsonl.1` newest to `log.jsonl.3`). It runs under
an exclusive `flock` on the state directory descriptor and re-checks the size under the lock, so
two processes that both see a full log rotate it once. If the lock or a rename fails, the record is
still appended and the file just grows until the next rotation succeeds.

Logging never fails the command: any `OSError` (disk full, read-only filesystem, a refused file)
makes `write` return False and calls `on_error` once with one line. A malformed call (unknown
event, missing or unknown field) is a programming error and raises `ValueError`.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from .state_dir import StateDir, StateDirError, verify_file
from .text import MAX_MESSAGE_CHARS, one_line

if TYPE_CHECKING:
    from .config import ConfigWarning

LOG_NAME: Final = "log.jsonl"
MAX_BYTES: Final = 10 * 1024 * 1024
KEEP: Final = 3
# Every string field is cut to its schema maxLength (256 unless listed), markers included, so a
# record is bounded: 4096 + 32 + 5 * 256 characters, each at most 6 bytes once ASCII-escaped.
MAX_LINE_BYTES: Final = 64 * 1024
_LIMITS: Final = {"forgectl_path": 4096, "forgectl_version": 32, "pr": 1024, "pr_id": 128}
LEVELS: Final = ("debug", "info", "warn", "error")
_FRAMING: Final = frozenset({"ts", "level", "event"})

# event -> (required fields, optional fields); tests/test_log.py pins this to schemas/log.json.
_MSG = {"message"}
EVENT_FIELDS: Final[dict[str, tuple[set[str], set[str]]]] = {
    "poll_start": ({"full", "forgectl_path", "forgectl_version"}, {"message", "scope"}),
    "poll_end": (
        {"duration_ms", "snapshots", "events", "errors", "already_running", "interrupted",
         "truncated"},
        _MSG,
    ),
    "host_failed": ({"host", "code"}, _MSG),
    "part_incomplete": ({"pr_id", "pr", "part", "snapshots"}, _MSG),
    "commit_skipped": ({"pr_id", "reason"}, _MSG),
    "host_summary": (
        {"host", "snapshots", "requests_estimate", "errors", "paused_until"}, _MSG),
    "snapshot_failed": ({"pr", "pr_id", "code", "failures"}, _MSG),
    "pr_status": ({"pr", "pr_id", "from", "to"}, _MSG),
    "hook_run": ({"event_id", "hook", "exit_code", "duration_ms", "timed_out"}, _MSG),
    "hook_cap": ({"skipped", "first_event_id", "last_event_id"}, _MSG),
    "config_warning": ({"key"}, _MSG),
}  # fmt: skip


def _timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _clean(key: str, value: Any) -> Any:
    if isinstance(value, str):
        return one_line(value, _LIMITS.get(key, MAX_MESSAGE_CHARS))
    if value is None or isinstance(value, (bool, int)):
        return value
    raise ValueError(f"log field {key!r} has an unsupported type {type(value).__name__}")


def _build(event: str, level: str, fields: Mapping[str, Any], now: float) -> bytes:
    if event not in EVENT_FIELDS:
        raise ValueError(f"unknown log event {event!r}")
    if level not in LEVELS:
        raise ValueError(f"unknown log level {level!r}")
    required, optional = EVENT_FIELDS[event]
    given = set(fields)
    if given & _FRAMING:
        raise ValueError("ts, level and event are set by the writer")
    if not required <= given:
        raise ValueError(f"{event} needs {sorted(required - given)}")
    if given - required - optional:
        raise ValueError(f"{event} has no fields {sorted(given - required - optional)}")
    record: dict[str, Any] = {"ts": _timestamp(now), "level": level, "event": event}
    record.update({key: _clean(key, value) for key, value in fields.items()})
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(line) > MAX_LINE_BYTES:  # unreachable with the caps above; kept as a hard bound
        raise ValueError("log record too large")
    return line


class Log:
    """Appends records to `<state dir>/log.jsonl`."""

    def __init__(
        self,
        state: StateDir | None,
        *,
        max_bytes: int = MAX_BYTES,
        keep: int = KEEP,
        clock: Callable[[], float] = time.time,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._state = state
        self._max_bytes = max_bytes
        self._keep = keep
        self._clock = clock
        self._on_error = on_error
        self._reported = False

    # -- public ---------------------------------------------------------------------------

    def write(self, event: str, level: str = "info", **fields: Any) -> bool:
        """Append one record. False when it could not be written (never raises for I/O)."""
        line = _build(event, level, fields, self._clock())
        if self._state is None:
            return False
        try:
            self._append(self._state, line)
        except (OSError, StateDirError) as exc:
            self._report(exc)
            return False
        return True

    def config_warnings(self, warnings: Iterable[ConfigWarning]) -> None:
        for warning in warnings:
            self.write("config_warning", "warn", key=warning.key, message=warning.message)

    # -- internals ------------------------------------------------------------------------

    def _report(self, exc: OSError | StateDirError) -> None:
        if self._reported or self._on_error is None:
            return
        self._reported = True
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        self._on_error(f"{LOG_NAME} not written: {one_line(reason, 120)}")

    def _open(self, state: StateDir) -> int:
        # O_NONBLOCK: opening a FIFO with no reader fails (ENXIO) instead of hanging.
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(LOG_NAME, flags | os.O_NONBLOCK, 0o600, dir_fd=state.fd)
        try:
            verify_file(os.fstat(fd), LOG_NAME)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _append(self, state: StateDir, line: bytes) -> None:
        fd = self._open(state)
        try:
            if os.fstat(fd).st_size >= self._max_bytes:
                os.close(fd)
                fd = -1
                self._rotate(state)
                fd = self._open(state)
            self._write_all(fd, line)
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def _write_all(fd: int, line: bytes) -> None:
        written = os.write(fd, line)
        if written != len(line):
            # Never continue a partial record (another writer may have appended since): end the
            # fragment with a newline so the next record starts on its own line.
            try:
                os.write(fd, b"\n")
            except OSError:
                pass
            raise OSError(errno.EIO, "short write")

    def _rotate(self, state: StateDir) -> None:
        try:
            fcntl.flock(state.fd, fcntl.LOCK_EX)
        except OSError:
            return  # no lock, no rotation: the log just grows
        try:
            try:
                size = os.stat(LOG_NAME, dir_fd=state.fd, follow_symlinks=False).st_size
            except FileNotFoundError:
                return
            if size < self._max_bytes:
                return  # another process rotated while we waited for the lock
            self._shift(state)
        except OSError:
            return
        finally:
            try:
                fcntl.flock(state.fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def _shift(self, state: StateDir) -> None:
        def name(n: int) -> str:
            return f"{LOG_NAME}.{n}"

        try:
            os.unlink(name(self._keep), dir_fd=state.fd)
        except FileNotFoundError:
            pass
        for n in range(self._keep - 1, 0, -1):
            try:
                os.rename(name(n), name(n + 1), src_dir_fd=state.fd, dst_dir_fd=state.fd)
            except FileNotFoundError:
                pass
        os.rename(LOG_NAME, name(1), src_dir_fd=state.fd, dst_dir_fd=state.fd)


class _NullLog(Log):
    """A log that writes nothing, for a `Context` built without a state directory."""

    def __init__(self) -> None:
        super().__init__(None)


NULL_LOG: Final[Log] = _NullLog()
