"""The single boundary for every subprocess call made by pr-watch (forgectl, hooks, crontab).

Nothing else in the package may import `subprocess` (tests/test_gateway_boundary.py enforces it),
so tests substitute a fake backend here (`use_backend`) or put fake binaries in tests/fakebin.

Contract of `run`:

- argv is a list, never a shell string. Untrusted or agent text never belongs in argv or env; it
  travels through `stdin` or a file. The gateway cannot tell trusted from untrusted text, so
  callers (refs, forgectl_client, hooks) own that rule.
- `env` is required and is the child's whole environment; nothing is inherited. `argv[0]` is
  resolved once against the caller's PATH, not the child's.
- stdio is explicit: stdin is the given bytes or /dev/null, stdout and stderr are pipes read by
  the gateway (or /dev/null with `discard_output`). A child never shares the caller's terminal.
- stdout is read under a byte cap (`max_output`); one byte more kills the child (`too_large`).
  stderr keeps its first STDERR_CAP bytes and drops the rest. Output is decoded as UTF-8 with
  surrogateescape, never through the caller's locale.
- The child runs in its own session and process group. At the deadline or over the cap the
  group gets SIGTERM, then SIGKILL after `term_grace` seconds. A group is signalled only while
  its leader is unreaped, so a recycled process-group id is never hit; nothing is signalled by
  pattern, only by the handles of children this module started.
- Only the leader is bounded: once it is reaped, descendants that keep the pipes open are not
  signalled, and reading stops at EOF or the deadline.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

TERM_GRACE = 2.0  # seconds between SIGTERM and SIGKILL (forgectl calls)
ABANDON_AFTER = 5.0  # seconds to wait for the leader after SIGKILL before giving up
STDERR_CAP = 64 * 1024
_TICK = 0.02
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
    """Outcome of a call. `returncode` is negative for a signal, None when abandoned."""

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
    if value < 0 or (value == 0 and not allow_zero) or value != value:
        raise ValueError(f"{what} must be positive, got {value!r}")
    return float(value)


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    max_output: int,
    stdin: str | bytes | None = None,
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
    if isinstance(stdin, str):
        data: bytes | None = stdin.encode("utf-8", "surrogateescape")
    else:
        data = stdin
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
    """A started child; the only object allowed to signal or reap it."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.pgid = proc.pid  # start_new_session made the leader its group's id
        self._lock = threading.Lock()

    def poll(self) -> int | None:
        with self._lock:
            return self.proc.poll()

    def signal_group(self, sig: int) -> bool:
        """Signal the group only while the leader is unreaped; False when nothing was sent."""
        with self._lock:
            if self.proc.poll() is not None:  # reaps a leader that already exited
                return False
            try:
                _killpg(self.pgid, sig)
            except ProcessLookupError:
                return False
            return True

    def wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while True:
            if self.poll() is not None:
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_TICK, remaining))

    def stop(self, term_grace: float, abandon_after: float) -> bool:
        """SIGTERM, SIGKILL after term_grace, then wait at most abandon_after; True if reaped."""
        self.signal_group(signal.SIGTERM)
        if self.wait(term_grace):
            return True
        self.signal_group(signal.SIGKILL)
        return self.wait(abandon_after)


_LIVE: dict[int, _Child] = {}
_LIVE_LOCK = threading.Lock()


def live_pids() -> list[int]:
    """Leaders of the children currently running through the real backend."""
    with _LIVE_LOCK:
        return sorted(_LIVE)


def signal_live(sig: int = signal.SIGTERM) -> int:
    """Send `sig` to the group of every live child (leader unreaped); returns how many.

    For a SIGTERM/SIGINT/SIGHUP shutdown of the poll process. Each `run` then sees its child
    exit and returns normally; the caller recognises an interrupted call by its returncode.
    """
    with _LIVE_LOCK:
        children = list(_LIVE.values())
    return sum(1 for child in children if child.signal_group(sig))


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", "surrogateescape")


def _launch(request: Request) -> _Child:
    program = request.argv[0]
    if os.sep not in program:
        found = shutil.which(program)  # the caller's PATH, resolved once
        if found is None:
            raise LaunchError(f"{program}: not found in PATH")
        program = found
    argv = [program, *request.argv[1:]]
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
    return _Child(proc)


def _run_real(request: Request) -> Result:
    started = time.monotonic()
    deadline = started + request.timeout
    child = _launch(request)
    proc = child.proc
    with _LIVE_LOCK:
        _LIVE[proc.pid] = child
    out = bytearray()
    err = bytearray()
    timed_out = too_large = stderr_truncated = False
    sel = selectors.DefaultSelector()
    stdin_f, stdout_f, stderr_f = proc.stdin, proc.stdout, proc.stderr
    try:
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
        while True:
            if not sel.get_map() and child.poll() is not None:
                break
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            events = sel.select(min(_TICK * 2, deadline - now)) if sel.get_map() else []
            if not sel.get_map():
                time.sleep(min(_TICK, max(0.0, deadline - time.monotonic())))
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
                assert not isinstance(stream, int)
                try:
                    chunk = os.read(stream.fileno(), _CHUNK)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(stream)
                    continue
                if stream is stdout_f:
                    out += chunk
                    if len(out) > request.max_output:
                        del out[request.max_output :]
                        too_large = True
                else:
                    room = STDERR_CAP - len(err)
                    err += chunk[: max(room, 0)]
                    stderr_truncated = stderr_truncated or len(chunk) > room
            if too_large:
                break
        reaped = child.poll() is not None
        if not reaped:
            reaped = child.stop(request.term_grace, request.abandon_after)
        returncode = proc.returncode if reaped else None
        return Result(
            returncode=returncode,
            stdout=_decode(out),
            stderr=_decode(err),
            timed_out=timed_out,
            too_large=too_large,
            stderr_truncated=stderr_truncated,
            abandoned=not reaped,
            elapsed=time.monotonic() - started,
            pid=proc.pid,
        )
    except BaseException:
        child.stop(request.term_grace, request.abandon_after)
        raise
    finally:
        sel.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        with _LIVE_LOCK:
            _LIVE.pop(proc.pid, None)
