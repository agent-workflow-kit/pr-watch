"""Test support for the gateway: a scriptable fake backend, a process reaper and a fake-tool dir.

`FakeGateway` replaces the real backend (unit tests of forgectl_client, hooks, cron). It mimics
the real contract: the byte cap, the deadline (in virtual time, no real sleeping), signal exits
and the stderr cap. An unscripted call fails the test instead of guessing.

`Reaper` backs every test that starts real processes: it records each leader (and any pid a
test hands it) at launch, holding a pidfd where the platform has one, and kills exactly those on
teardown, never by name or pattern.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pr_watch import gateway
from pr_watch.gateway import STDERR_CAP, LaunchError, Request, Result

Matcher = Callable[[Request], bool]


@dataclass
class _Script:
    match: Matcher
    stdout: bytes
    stderr: bytes
    returncode: int
    delay: float
    launch_error: str | None
    remaining: int | None  # None: unlimited


class FakeGateway:
    """Scripted stdout, stderr, exit codes, delays and oversized output; records every call."""

    def __init__(self) -> None:
        self.calls: list[Request] = []
        self._scripts: list[_Script] = []

    def on(
        self,
        *prefix: str,
        match: Matcher | None = None,
        stdout: str | bytes = b"",
        stderr: str | bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
        launch_error: str | None = None,
        times: int | None = None,
    ) -> FakeGateway:
        """Script calls whose argv starts with `prefix` (and satisfy `match`, if given)."""

        def matches(request: Request) -> bool:
            return request.argv[: len(prefix)] == prefix and (match is None or match(request))

        self._scripts.append(
            _Script(
                matches,
                _bytes(stdout),
                _bytes(stderr),
                returncode,
                delay,
                launch_error,
                times,
            )
        )
        return self

    def __call__(self, request: Request) -> Result:
        self.calls.append(request)
        for script in self._scripts:
            if script.remaining == 0 or not script.match(request):
                continue
            if script.remaining is not None:
                script.remaining -= 1
            return self._answer(script, request)
        raise AssertionError(f"unscripted gateway call: {list(request.argv)!r}")

    @staticmethod
    def _answer(script: _Script, request: Request) -> Result:
        if script.launch_error is not None:
            raise LaunchError(script.launch_error)
        stderr = script.stderr[:STDERR_CAP]
        common = {
            "stderr": _text(stderr),
            "stderr_truncated": len(script.stderr) > STDERR_CAP,
            "pid": None,
        }
        if script.delay > request.timeout:
            return Result(
                returncode=-signal.SIGTERM,
                timed_out=True,
                elapsed=request.timeout,
                **common,  # type: ignore[arg-type]
            )
        stdout = b"" if request.discard_output else script.stdout
        if len(stdout) > request.max_output:
            return Result(
                returncode=-signal.SIGTERM,
                stdout=_text(stdout[: request.max_output]),
                too_large=True,
                elapsed=script.delay,
                **common,  # type: ignore[arg-type]
            )
        return Result(
            returncode=script.returncode,
            stdout=_text(stdout),
            elapsed=script.delay,
            **common,  # type: ignore[arg-type]
        )

    def argvs(self) -> list[list[str]]:
        return [list(r.argv) for r in self.calls]


def _bytes(data: str | bytes) -> bytes:
    return data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


@pytest.fixture
def fake_gateway() -> Iterator[FakeGateway]:
    """Route `gateway.run` to a FakeGateway for the test."""
    fake = FakeGateway()
    with gateway.use_backend(fake):
        yield fake


class Reaper:
    """Kills exactly the processes it was told about (pidfd when available)."""

    def __init__(self) -> None:
        self._targets: list[tuple[int, int | None]] = []

    def track(self, pid: int) -> None:
        """Remember `pid` for teardown; call as early as possible after launch."""
        fd: int | None = None
        if hasattr(os, "pidfd_open"):
            try:
                fd = os.pidfd_open(pid)
            except OSError:  # already gone
                return
        self._targets.append((pid, fd))

    def kill_all(self) -> None:
        for pid, fd in self._targets:
            try:
                if fd is not None:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                else:
                    # No pidfd: only a direct child that is still unreaped is certainly ours.
                    if os.waitpid(pid, os.WNOHANG) == (0, 0):
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        self._targets.clear()


@pytest.fixture
def reaper(monkeypatch: pytest.MonkeyPatch) -> Iterator[Reaper]:
    """Tracks every process the gateway starts; kills the survivors by handle at teardown."""
    reaper = Reaper()
    real_popen = gateway._popen

    def recording(*args: object, **kwargs: object) -> Any:
        proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        reaper.track(proc.pid)
        return proc

    monkeypatch.setattr(gateway, "_popen", recording)
    try:
        yield reaper
    finally:
        reaper.kill_all()


@pytest.fixture
def fake_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], Path]:
    """Install an executable `name` running `sh` body in a per-test dir put first on PATH.

    For process tests of a throwaway tool; the standing fakes (forgectl, gh) live in
    tests/fakebin, which the isolation guard in conftest already puts on PATH.
    """
    directory = tmp_path / "fake-tools"
    directory.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(directory), os.environ["PATH"]]))

    def install(name: str, body: str) -> Path:
        path = directory / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    return install
