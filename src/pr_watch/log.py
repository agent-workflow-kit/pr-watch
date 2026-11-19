"""`log.jsonl`: the public, append-only record of what pr-watch did (schemas/log.json).

One record is one JSON line (ASCII only, `\\n`-terminated) written with a single `write(2)` to a
descriptor opened `O_APPEND`, so concurrent processes never interleave inside a record. The file
is opened per record, relative to the state directory descriptor and with `O_NOFOLLOW`, and the
opened descriptor is checked (a regular file, owned by the user, not group- or world-writable): a
planted symlink, FIFO or foreign file is never written to.

Rotation at `MAX_BYTES` keeps `KEEP` files (`log.jsonl.1` newest to `log.jsonl.3`). It runs under
an exclusive `flock` on the state directory descriptor and re-checks the size under the lock, so
two processes that both see a full log rotate it once. If the lock or a rename fails, the record is
still appended and the file just grows until the next rotation succeeds. The lock is taken
without blocking and retried for about a second; past that, rotation is skipped. Under concurrent
rotation a record may be lost (best effort), but the log is never corrupted and at most `KEEP + 1`
files exist.

Logging never fails the command: any `OSError` (disk full, read-only filesystem, a refused file)
or a record that would not match schemas/log.json (unknown event, missing or unknown field, a
value of the wrong type or range) makes `write` return False and calls `on_error` once with one
line. Nothing invalid is ever written.
"""

from __future__ import annotations

import errno
import fcntl
import itertools
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from .state_dir import StateDir, StateDirError, verify_file
from .text import one_line

if TYPE_CHECKING:
    from .config import ConfigWarning

LOG_NAME: Final = "log.jsonl"
MAX_BYTES: Final = 10 * 1024 * 1024
KEEP: Final = 3
# Free-text fields are cut to their schema maxLength (256 unless listed), markers included, so a
# record is bounded: 4096 + 32 + 3 * 256 characters, each at most 6 bytes once ASCII-escaped.
# Identifier fields are never cut: a value that does not match its schema pattern is refused.
MAX_LINE_BYTES: Final = 64 * 1024
_TEXT_LIMITS: Final = {"message": 256, "scope": 256, "key": 256, "forgectl_path": 4096,
                       "forgectl_version": 32}  # fmt: skip
_BOOLS: Final = frozenset({"full", "already_running", "interrupted", "truncated", "timed_out"})
# integer fields -> minimum (None: any integer); `exit_code` may also be null.
_INTS: Final[dict[str, int | None]] = {
    "duration_ms": 0, "snapshots": 0, "events": 0, "errors": 0, "requests_estimate": 0,
    "failures": 0, "skipped": 1, "exit_code": None,
}  # fmt: skip
_NULLABLE: Final = frozenset({"paused_until", "event_id", "exit_code"})
# pattern fields: the schema pattern (tests/test_log.py pins them), matched whole. A length bound
# (4096 for `pr`, which the schema leaves open) keeps a record inside MAX_LINE_BYTES.
_TS = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
_HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
PATTERNS: Final[dict[str, str]] = {
    "host": _HOST,
    "code": r"[a-z_]{1,64}",
    "pr_id": r"(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9:/_=.+-]{0,127}",
    "pr": _HOST + r"(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}",
    "reason": r"[a-z_]{1,64}",
    "hook": r"(?:on-[a-z_]+|post-poll)",
    "event_id": r"ev-[a-z2-7]{12}",
    "first_event_id": r"ev-[a-z2-7]{12}",
    "last_event_id": r"ev-[a-z2-7]{12}",
    "paused_until": _TS,
}
_COMPILED: Final = {key: re.compile(pattern) for key, pattern in PATTERNS.items()}
_MAX_PATTERN_CHARS: Final = 4096
ENUMS: Final[dict[str, tuple[str, ...]]] = {
    "part": ("reviews", "threads", "ci"),
    "from": ("active", "terminal", "unreachable"),
    "to": ("active", "terminal", "unreachable"),
}
LEVELS: Final = ("debug", "info", "warn", "error")
_FRAMING: Final = frozenset({"ts", "level", "event"})
_ROTATE_LOCK: Final = threading.Lock()  # flock is per descriptor: it does not order threads
LOCK_WAIT: Final = 1.0
_write = os.write  # seams: tests patch these names, not the shared `os` module
_open = os.open
_rename = os.rename
_aside_counter = itertools.count(1)  # unique suffixes for entries moved out of a slot

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
    if value is None:
        if key in _NULLABLE:
            return None
        raise ValueError(f"log field {key!r} cannot be null")
    if key in _BOOLS:
        if isinstance(value, bool):
            return value
        raise ValueError(f"log field {key!r} must be a boolean")
    if key in _INTS:
        minimum = _INTS[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"log field {key!r} must be an integer")
        if minimum is not None and value < minimum:
            raise ValueError(f"log field {key!r} must be at least {minimum}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"log field {key!r} has an unsupported type {type(value).__name__}")
    if key in _TEXT_LIMITS:
        return one_line(value, _TEXT_LIMITS[key])
    if key in ENUMS:
        if value not in ENUMS[key]:
            raise ValueError(f"log field {key!r} is not one of {list(ENUMS[key])}")
        return value
    if len(value) > _MAX_PATTERN_CHARS or not _COMPILED[key].fullmatch(value):
        raise ValueError(f"log field {key!r} does not match its schema pattern")
    return value


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
        lock_wait: float = LOCK_WAIT,
    ) -> None:
        self._state = state
        self._max_bytes = max_bytes
        self._keep = keep
        self._clock = clock
        self._on_error = on_error
        self._reported: set[str] = set()  # failure categories already reported
        self._lock_wait = lock_wait

    # -- public ---------------------------------------------------------------------------

    def write(self, event: str, level: str = "info", **fields: Any) -> bool:
        """Append one record. False when it could not be written (never raises for bad content or
        I/O): the reason goes to `on_error` once."""
        try:
            line = _build(event, level, fields, self._clock())
        except ValueError as exc:
            self._report_text("invalid", f"invalid record: {exc}")
            return False
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
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        self._report_text("write", reason)

    def _report_text(self, category: str, reason: str) -> None:
        if category in self._reported or self._on_error is None:
            return
        self._reported.add(category)
        self._on_error(f"{LOG_NAME} not written: {one_line(reason, 120)}")

    def _open(self, state: StateDir) -> int:
        # O_NONBLOCK: opening a FIFO with no reader fails (ENXIO) instead of hanging.
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        created = False
        while True:
            try:
                fd = _open(LOG_NAME, flags, dir_fd=state.fd)
                break
            except FileNotFoundError:
                pass
            try:  # O_EXCL: only a file this call created is chmod-ed below
                fd = _open(LOG_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=state.fd)
                created = True
                break
            except FileExistsError:
                continue  # created concurrently: open it normally
        try:
            if created:
                os.fchmod(fd, 0o600)  # exact mode even under a restrictive umask
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
        written = _write(fd, line)
        if written != len(line):
            # Never continue a partial record (another writer may have appended since): end the
            # fragment with a newline so the next record starts on its own line.
            try:
                _write(fd, b"\n")
            except OSError:
                pass
            raise OSError(errno.EIO, "short write")

    def _lock(self, state: StateDir) -> bool:
        """Take the exclusive lock without blocking, retrying for `lock_wait` seconds."""
        deadline = time.monotonic() + self._lock_wait
        while True:
            try:
                fcntl.flock(state.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.02)
            except OSError:
                return False

    def _rotate(self, state: StateDir) -> None:
        with _ROTATE_LOCK:
            if not self._lock(state):
                return  # no lock, no rotation: the log just grows
            try:
                try:
                    size = os.stat(LOG_NAME, dir_fd=state.fd, follow_symlinks=False).st_size
                except FileNotFoundError:
                    return
                if size < self._max_bytes:
                    return  # another process rotated while we waited for the lock
                self._shift(state)
            except OSError as exc:
                self._report_text("rotation", f"rotation failed: {exc.strerror or exc}")
            finally:
                try:
                    fcntl.flock(state.fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    def _shift(self, state: StateDir) -> None:
        def name(n: int) -> str:
            return f"{LOG_NAME}.{n}"

        def aside(n: int) -> None:
            # Never delete what we did not write: move it out of the rotation slots.
            stale = f"{name(n)}.stale-{os.getpid()}-{next(_aside_counter)}"
            _rename(name(n), stale, src_dir_fd=state.fd, dst_dir_fd=state.fd)

        for n in range(1, self._keep + 1):  # a directory planted in a slot blocks the renames
            try:
                if stat.S_ISDIR(os.stat(name(n), dir_fd=state.fd, follow_symlinks=False).st_mode):
                    try:
                        os.rmdir(name(n), dir_fd=state.fd)
                    except OSError:  # not empty (or not removable): rename it aside
                        aside(n)
            except FileNotFoundError:
                pass
        try:
            os.unlink(name(self._keep), dir_fd=state.fd)
        except FileNotFoundError:
            pass
        except OSError:
            aside(self._keep)
        for n in range(self._keep - 1, 0, -1):
            try:
                _rename(name(n), name(n + 1), src_dir_fd=state.fd, dst_dir_fd=state.fd)
            except FileNotFoundError:
                pass
        _rename(LOG_NAME, name(1), src_dir_fd=state.fd, dst_dir_fd=state.fd)


class _NullLog(Log):
    """A log that writes nothing, for a `Context` built without a state directory."""

    def __init__(self) -> None:
        super().__init__(None)


NULL_LOG: Final[Log] = _NullLog()
