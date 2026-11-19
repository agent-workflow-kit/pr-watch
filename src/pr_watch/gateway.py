"""The single boundary for every subprocess call made by pr-watch (forgectl, hooks, crontab).

Nothing else in the package may import `subprocess` (tests/test_gateway_boundary.py enforces it),
so tests substitute a fake backend here (`use_backend`) or put fake binaries in tests/fakebin.

Contract of `run`:

- argv is a list, never a shell string. Untrusted or agent text never belongs in argv or env; it
  travels through `stdin` or a file. The gateway cannot tell trusted from untrusted text, so
  callers (refs, forgectl_client, hooks) own that rule.
- `env` is required and is the child's whole environment; nothing is inherited. `argv[0]` is
  resolved once against the caller's PATH (not the child's) and made absolute against the caller's
  cwd, so a relative or empty PATH entry, or a relative `argv[0]` with a separator, never resolves
  against the child's `cwd`. `stdin` must be None, str, bytes or bytearray (anything else is a
  TypeError before launch); a str is encoded as UTF-8 with surrogateescape.
- stdio is explicit: stdin is the given bytes or /dev/null, stdout and stderr are pipes read by
  the gateway (or /dev/null with `discard_output`). A child never shares the caller's terminal.
- stdout is read under a byte cap (`max_output`); one byte more kills the child (`too_large`).
  stderr keeps its first STDERR_CAP bytes and drops the rest. Output is decoded as UTF-8 with
  surrogateescape, never through the caller's locale.
- The child runs in its own session and process group. At the deadline or over the cap the
  group gets SIGTERM, then SIGKILL after `term_grace` seconds or as soon as the leader has exited,
  whichever comes first, so descendants that ignore SIGTERM do not outlive the call. The leader's
  exit is detected without reaping it (waitid with WNOWAIT), so the group id stays valid while it
  is signalled; a group is signalled only while its leader is unreaped, so a recycled process-group
  id is never hit. Nothing is signalled by pattern, only by the handles of children this module
  started. (Where `os.waitid` is missing, e.g. macOS, the leader can only be observed by reaping
  it: the SIGKILL step then reaches no one once the leader died on SIGTERM.)
- On a normal exit (the leader exits before the deadline and the cap) `run` drains what is already
  readable, closes the pipes and returns: it does not wait for EOF, does not signal the group and
  does not set `timed_out`, even when a descendant still holds the pipes. Such a descendant is not
  bounded; callers must not leave background work. `timed_out` means the leader was still running
  at the deadline.
- `signal_live` is safe from a signal handler on a thread that is inside `run`: it never blocks on
  a lock that thread may hold. A child whose lock cannot be taken gets the signal queued, and its
  owning `run` loop delivers it at its next tick (never to a reaped group).

Out of scope here, by design: classifying results into error kinds (the poller), the fake
forgectl and gh binaries (tests/fakebin) and hook specifics; this module only runs one argv.
"""

from __future__ import annotations

import contextlib
import math
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import IO

TERM_GRACE = 2.0  # seconds between SIGTERM and SIGKILL (forgectl calls)
ABANDON_AFTER = 5.0  # seconds to wait for the leader after SIGKILL before giving up
STDERR_CAP = 64 * 1024
_TICK = 0.02  # longest poll interval
_TICK_MIN = 0.001  # first poll interval; doubles while idle up to _TICK
_CHUNK = 65536


class LaunchError(OSError):
    """The child could not be started (missing, not executable)."""


@dataclass(frozen=True)
class Request:
    """A validated call; the unit a fake backend receives."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    timeout: float
    max_output: int
    stdin: bytes | None = None
    cwd: str | None = None
    term_grace: float = TERM_GRACE
    abandon_after: float = ABANDON_AFTER
    discard_output: bool = False


@dataclass(frozen=True)
class Result:
    """Outcome of a call. `returncode` is negative for a signal, None when abandoned.

    `stdout` and `stderr` are decoded as UTF-8 with surrogateescape: bytes that are not valid UTF-8
    (including a multi-byte sequence cut by the output cap) become lone surrogates, and `.encode()`
    on such text raises. Re-encode with `errors="surrogateescape"` to get the original bytes back,
    or `errors="replace"` for display.
    """

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    too_large: bool = False  # stdout exceeded max_output (stdout holds the first max_output bytes)
    stderr_truncated: bool = False
    abandoned: bool = False  # the leader was not reaped after SIGKILL plus abandon_after
    elapsed: float = 0.0
    pid: int | None = None


Backend = Callable[[Request], Result]


def minimal_env(**extra: str) -> dict[str, str]:
    """PATH and HOME from the caller plus explicit extras: a starting point for `env`."""
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    env.update(extra)
    return env


def _check_text(what: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be str, got {type(value).__name__}")
    if "\0" in value:
        raise ValueError(f"{what} contains a NUL byte")
    return value


def _positive(what: str, value: float, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{what} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{what} must be finite, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        kind = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{what} must be {kind}, got {value!r}")
    return float(value)


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    max_output: int,
    stdin: str | bytes | bytearray | None = None,
    cwd: str | os.PathLike[str] | None = None,
    term_grace: float = TERM_GRACE,
    abandon_after: float = ABANDON_AFTER,
    discard_output: bool = False,
) -> Result:
    """Validate the call and run it on the active backend (real unless a test installed one)."""
    if isinstance(argv, (str, bytes)) or not argv:
        raise TypeError("argv must be a non-empty list of str")
    argv_t = tuple(_check_text("argv item", a) for a in argv)
    if not argv_t[0]:
        raise ValueError("argv[0] is empty")
    env_d: dict[str, str] = {}
    for key, value in env.items():
        _check_text("env name", key)
        if not key or "=" in key:
            raise ValueError(f"invalid env name {key!r}")
        env_d[key] = _check_text(f"env value of {key}", value)
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 1:
        raise ValueError("max_output must be a positive int (bytes)")
    data: bytes | None
    if stdin is None:
        data = None
    elif isinstance(stdin, str):
        data = stdin.encode("utf-8", "surrogateescape")
    elif isinstance(stdin, (bytes, bytearray)):
        data = bytes(stdin)
    else:
        raise TypeError(f"stdin must be None, str or bytes, got {type(stdin).__name__}")
    request = Request(
        argv=argv_t,
        env=env_d,
        timeout=_positive("timeout", timeout),
        max_output=max_output,
        stdin=data,
        cwd=None if cwd is None else os.fspath(cwd),
        term_grace=_positive("term_grace", term_grace, allow_zero=True),
        abandon_after=_positive("abandon_after", abandon_after, allow_zero=True),
        discard_output=discard_output,
    )
    return (_state.backend or _run_real)(request)


# --- backend plug-in point -------------------------------------------------------------------


@dataclass
class _State:
    backend: Backend | None = None  # None: the real backend


_state = _State()


@contextlib.contextmanager
def use_backend(backend: Backend) -> Iterator[None]:
    """Route `run` to `backend` for the duration (tests: a fake; never used in production)."""
    previous = _state.backend
    _state.backend = backend
    try:
        yield
    finally:
        _state.backend = previous


# --- the real backend ------------------------------------------------------------------------

_popen = subprocess.Popen  # tests wrap this to record every launch
_killpg = os.killpg  # tests wrap this to record every group signal


class _Child:
    """A started child; the only object allowed to signal or reap it.

    `_lock` guards "is the leader reaped" together with the signal that depends on it. It is
    non-reentrant, so a signal handler on the thread that holds it must not wait for it: it uses
    `signal_group(blocking=False)`, which queues the signal; whoever holds the lock delivers the
    queue right after releasing it (`_drain`).
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.pgid = proc.pid  # start_new_session made the leader its group's id
        self._lock = threading.Lock()
        self._pending: deque[int] = deque()

    def poll(self) -> int | None:
        """Reap the leader if it exited; returns its code or None while it runs."""
        with self._lock:
            code = self.proc.poll()
        self._drain()
        return code

    def exited(self) -> bool:
        """True once the leader has exited. Does not reap it where `os.waitid` exists."""
        with self._lock:
            if self.proc.returncode is not None:
                done = True
            elif hasattr(os, "waitid"):
                try:
                    done = (
                        os.waitid(os.P_PID, self.pgid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                        is not None
                    )
                except ChildProcessError:  # reaped behind our back: nothing left to signal
                    done = True
            else:  # no WNOWAIT: the leader can only be observed by reaping it
                done = self.proc.poll() is not None
        self._drain()
        return done

    def signal_group(self, sig: int, *, blocking: bool = True) -> bool:
        """Signal the group only while the leader is unreaped; False when nothing was sent.

        With `blocking=False` (signal handlers) a busy lock queues the signal instead of waiting,
        and the result is True (queued).
        """
        if not self._lock.acquire(blocking):
            self._pending.append(sig)
            return True
        try:
            sent = self._send(sig)
        finally:
            self._lock.release()
        self._drain()
        return sent

    def _send(self, sig: int) -> bool:  # caller holds the lock
        if self.proc.returncode is not None:  # reaped: the group id may be recycled
            return False
        try:
            _killpg(self.pgid, sig)
        except ProcessLookupError:
            return False
        return True

    def _drain(self) -> None:
        """Deliver signals queued by a handler that found the lock busy."""
        while True:
            try:
                sig = self._pending.popleft()
            except IndexError:
                return
            with self._lock:
                self._send(sig)

    def _wait_until(self, test: Callable[[], bool], seconds: float) -> bool:
        end = time.monotonic() + seconds
        tick = _TICK_MIN
        while True:
            if test():
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(tick, remaining))
            tick = min(tick * 2, _TICK)

    def wait(self, seconds: float) -> bool:
        """Wait up to `seconds` for the leader to be reaped."""
        return self._wait_until(lambda: self.poll() is not None, seconds)

    def stop(self, term_grace: float, abandon_after: float) -> bool:
        """Terminate the group; True if the leader was reaped.

        SIGTERM; wait up to term_grace for the leader to exit (not reaping it); then SIGKILL the
        group, always, while the leader is still unreaped, so descendants that ignored SIGTERM die
        too; then reap, waiting at most abandon_after.
        """
        self.signal_group(signal.SIGTERM)
        self._wait_until(self.exited, term_grace)
        self.signal_group(signal.SIGKILL)
        return self.wait(abandon_after)


_LIVE: dict[int, _Child] = {}
_LIVE_LOCK = threading.RLock()  # re-entrant: a signal handler may interrupt register/unregister


def _register(child: _Child) -> None:
    with _LIVE_LOCK:
        _LIVE[child.proc.pid] = child


def _unregister(child: _Child) -> None:
    with _LIVE_LOCK:
        _LIVE.pop(child.proc.pid, None)


def live_pids() -> list[int]:
    """Leaders of the children currently running through the real backend."""
    with _LIVE_LOCK:
        return sorted(_LIVE)


def signal_live(sig: int = signal.SIGTERM) -> int:
    """Send `sig` to the group of every live child (leader unreaped); returns how many.

    For a SIGTERM/SIGINT/SIGHUP shutdown of the poll process; safe to call from a signal handler,
    including on a thread that is inside `run` (see the module docstring). A child that was busy
    is counted as signalled: its signal is queued and delivered by its `run` loop at the next tick,
    unless the leader was reaped meanwhile. Each `run` then sees its child exit and returns
    normally; the caller recognises an interrupted call by its returncode.
    """
    with _LIVE_LOCK:
        children = list(_LIVE.values())
    return sum(1 for child in children if child.signal_group(sig, blocking=False))


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", "surrogateescape")


def _absolute(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(os.getcwd(), path)


def _launch(request: Request, started: list[_Child] | None = None) -> _Child:
    """Start the child. `started` receives it the moment it exists, for cleanup on interruption."""
    program = request.argv[0]
    if os.sep not in program:
        found = shutil.which(program)  # the caller's PATH, resolved once
        if found is None:
            raise LaunchError(f"{program}: not found in PATH")
        program = found
    argv = [_absolute(program), *request.argv[1:]]  # never the child's cwd
    devnull = subprocess.DEVNULL
    try:
        proc = _popen(
            argv,
            executable=None,
            stdin=subprocess.PIPE if request.stdin is not None else devnull,
            stdout=devnull if request.discard_output else subprocess.PIPE,
            stderr=devnull if request.discard_output else subprocess.PIPE,
            env=dict(request.env),
            cwd=request.cwd,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise LaunchError(f"{request.argv[0]}: cannot start: {exc}") from exc
    child = _Child(proc)
    if started is not None:
        started.append(child)
    _register(child)
    return child


class _Capture:
    """Bytes read from the child so far, with the caps applied."""

    def __init__(self, request: Request, stdout_f: IO[bytes] | None) -> None:
        self.request = request
        self.stdout_f = stdout_f
        self.out = bytearray()
        self.err = bytearray()
        self.too_large = False
        self.stderr_truncated = False

    def read(self, stream: IO[bytes]) -> str:
        """Read once from a pipe: "eof", "data" or "empty" (a read would block)."""
        try:
            chunk = os.read(stream.fileno(), _CHUNK)
        except BlockingIOError:
            return "empty"
        if not chunk:
            return "eof"
        if stream is self.stdout_f:
            self.out += chunk
            if len(self.out) > self.request.max_output:
                del self.out[self.request.max_output :]
                self.too_large = True
        else:
            room = STDERR_CAP - len(self.err)
            self.err += chunk[: max(room, 0)]
            self.stderr_truncated = self.stderr_truncated or len(chunk) > room
        return "data"


def _run_real(request: Request) -> Result:
    started_at = time.monotonic()
    deadline = started_at + request.timeout
    started: list[_Child] = []
    sel = selectors.DefaultSelector()
    proc: subprocess.Popen[bytes] | None = None
    timed_out = False
    try:
        child = _launch(request, started)
        proc = child.proc
        stdin_f, stdout_f, stderr_f = proc.stdin, proc.stdout, proc.stderr
        cap = _Capture(request, stdout_f)
        for stream in (stdout_f, stderr_f):
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                sel.register(stream, selectors.EVENT_READ)
        pending = request.stdin
        if stdin_f is not None:
            os.set_blocking(stdin_f.fileno(), False)
            if pending:
                sel.register(stdin_f, selectors.EVENT_WRITE)
            else:
                stdin_f.close()
        offset = 0
        tick = _TICK_MIN
        while True:
            if child.exited():
                # Normal exit: take what is already in the pipes, do not wait for EOF.
                for stream in (stdout_f, stderr_f):
                    while stream is not None and stream in sel.get_map() and not cap.too_large:
                        status = cap.read(stream)
                        if status == "eof":
                            sel.unregister(stream)
                        if status != "data":
                            break
                break
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            events = sel.select(min(tick, deadline - now)) if sel.get_map() else []
            if not events:
                if not sel.get_map():
                    time.sleep(min(tick, max(0.0, deadline - time.monotonic())))
                tick = min(tick * 2, _TICK)
            else:
                tick = _TICK_MIN
            for key, _ in events:
                stream = key.fileobj
                if stdin_f is not None and stream is stdin_f:
                    assert pending is not None
                    try:
                        offset += os.write(stdin_f.fileno(), pending[offset : offset + _CHUNK])
                    except BlockingIOError:
                        continue
                    except OSError:  # the child closed its stdin early
                        offset = len(pending)
                    if offset >= len(pending):
                        sel.unregister(stdin_f)
                        stdin_f.close()
                    continue
                if cap.read(stream) == "eof":  # type: ignore[arg-type]
                    sel.unregister(stream)  # type: ignore[arg-type]
            if cap.too_large:
                break
        reaped = False
        if timed_out or cap.too_large:
            reaped = child.stop(request.term_grace, request.abandon_after)
        else:
            reaped = child.wait(request.abandon_after)  # the leader has exited: reaps at once
        return Result(
            returncode=proc.returncode if reaped else None,
            stdout=_decode(cap.out),
            stderr=_decode(cap.err),
            timed_out=timed_out,
            too_large=cap.too_large,
            stderr_truncated=cap.stderr_truncated,
            abandoned=not reaped,
            elapsed=time.monotonic() - started_at,
            pid=proc.pid,
        )
    except BaseException:
        if started:
            started[0].stop(request.term_grace, request.abandon_after)
        raise
    finally:
        sel.close()
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        if started:
            _unregister(started[0])
