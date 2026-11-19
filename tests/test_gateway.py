"""The real gateway backend: stdio, caps, deadlines, group signalling and decoding."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pr_watch import gateway
from pr_watch.gateway import STDERR_CAP, LaunchError, Request
from tests.gateway_support import Reaper

PY_ONLY = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Tests that call `_launch` directly register a child; never leak it into the next test."""
    yield
    with gateway._LIVE_LOCK:
        gateway._LIVE.clear()


def sh(script: str, **kw: Any) -> gateway.Result:
    kw.setdefault("env", gateway.minimal_env())
    kw.setdefault("timeout", 20)
    kw.setdefault("max_output", 1 << 20)
    return gateway.run(["sh", "-c", script], **kw)


def is_dead(pid: int, wait: float = 3.0) -> bool:
    """Gone, or a zombie nobody reaped yet (containers without a reaping init)."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in ("Z", "X"):
            return True
        time.sleep(0.02)
    return False


# --- stdio and environment -------------------------------------------------------------------


def test_captures_stdout_stderr_and_exit_code(reaper: Reaper) -> None:
    result = sh("echo out; echo err >&2; exit 7")
    assert (result.stdout, result.stderr, result.returncode) == ("out\n", "err\n", 7)
    assert not (result.timed_out or result.too_large or result.abandoned)
    assert result.pid is not None


def test_stdin_reaches_the_child_and_large_stdin_does_not_deadlock(reaper: Reaper) -> None:
    payload = "é" * 400_000  # 800 kB, far over the pipe buffer
    result = gateway.run(
        ["cat"], env=gateway.minimal_env(), timeout=20, max_output=2 << 20, stdin=payload
    )
    assert result.stdout == payload


def test_no_stdin_means_dev_null(reaper: Reaper) -> None:
    result = gateway.run(["cat"], env=gateway.minimal_env(), timeout=5, max_output=100)
    assert (result.stdout, result.returncode, result.timed_out) == ("", 0, False)


def test_child_that_closes_stdin_early_is_not_an_error(reaper: Reaper) -> None:
    result = sh("exec 0<&-; echo done", stdin="x" * 3_000_000)
    assert (result.stdout, result.returncode) == ("done\n", 0)


def test_env_is_exactly_what_was_given(reaper: Reaper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_VALUE", "hunter2")
    result = gateway.run(["env"], env={"A": "1"}, timeout=5, max_output=1000)
    assert result.stdout == "A=1\n"  # no PATH, HOME or SECRET_VALUE inherited


def test_argv0_resolves_against_the_callers_path_not_the_childs(
    reaper: Reaper, fake_tool: Callable[[str, str], Path]
) -> None:
    fake_tool("callers-only-tool", "echo from-callers-path")
    # The child gets an empty env, so only the gateway's own lookup can find the tool.
    result = gateway.run(["callers-only-tool"], env={}, timeout=5, max_output=100)
    assert result.stdout == "from-callers-path\n"


def test_a_relative_path_entry_runs_the_callers_binary_not_one_in_the_childs_cwd(
    reaper: Reaper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = tmp_path / "caller"
    other = tmp_path / "other"
    (caller / "bin").mkdir(parents=True)
    other.mkdir()
    for path, text in ((caller / "bin" / "rtool", "caller"), (other / "rtool", "other")):
        path.write_text(f"#!/bin/sh\necho {text}\n")
        path.chmod(0o755)
    monkeypatch.chdir(caller)
    monkeypatch.setenv("PATH", "bin" + os.pathsep + os.environ["PATH"])
    result = gateway.run(["rtool"], env={}, timeout=5, max_output=100, cwd=other)
    assert result.stdout == "caller\n"


def test_an_empty_path_element_means_the_callers_cwd_not_the_childs(
    reaper: Reaper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = tmp_path / "caller"
    other = tmp_path / "other"
    caller.mkdir()
    other.mkdir()
    for path, text in ((caller / "etool", "caller"), (other / "etool", "other")):
        path.write_text(f"#!/bin/sh\necho {text}\n")
        path.chmod(0o755)
    monkeypatch.chdir(caller)
    monkeypatch.setenv("PATH", os.pathsep + "/nonexistent-dir")
    try:
        result = gateway.run(["etool"], env={}, timeout=5, max_output=100, cwd=other)
    except LaunchError:  # an interpreter whose lookup skips empty elements: also safe
        return
    assert result.stdout == "caller\n"


def test_a_relative_argv0_with_a_separator_resolves_against_the_callers_cwd(
    reaper: Reaper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = tmp_path / "caller"
    other = tmp_path / "other"
    caller.mkdir()
    other.mkdir()
    for path, text in ((caller / "stool", "caller"), (other / "stool", "other")):
        path.write_text(f"#!/bin/sh\necho {text}\n")
        path.chmod(0o755)
    monkeypatch.chdir(caller)
    result = gateway.run(["./stool"], env={}, timeout=5, max_output=100, cwd=other)
    assert result.stdout == "caller\n"


def test_argv_is_never_interpreted_by_a_shell(reaper: Reaper, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    gateway.run(
        ["echo", "; touch pwned $(touch pwned2) `touch pwned3`"],
        env=gateway.minimal_env(),
        timeout=5,
        max_output=1000,
        cwd=work,
    )
    assert list(work.iterdir()) == []


def test_cwd_is_honoured(reaper: Reaper, tmp_path: Path) -> None:
    assert sh("pwd -P", cwd=tmp_path).stdout.strip() == str(tmp_path.resolve())


@PY_ONLY
def test_child_leads_its_own_session_and_process_group(reaper: Reaper) -> None:
    result = sh("exec cat /proc/self/stat")
    fields = result.stdout.rsplit(")", 1)[1].split()
    pgrp, session = int(fields[2]), int(fields[3])
    assert pgrp == session == result.pid != os.getpgrp()


def test_discard_output_drops_output_without_blocking(reaper: Reaper) -> None:
    script = "i=0; while [ $i -lt 3000 ]; do printf '%0900d\\n' 0; echo x >&2; i=$((i+1)); done"
    result = sh(script, discard_output=True, max_output=10)
    assert (result.stdout, result.stderr, result.returncode, result.too_large) == ("", "", 0, False)


def test_nothing_stays_registered_after_a_call(reaper: Reaper) -> None:
    sh("true")
    assert gateway.live_pids() == []


# --- decoding --------------------------------------------------------------------------------


def test_invalid_utf8_round_trips_through_surrogateescape(reaper: Reaper) -> None:
    result = sh(r"printf '\377\376ok'")
    assert result.stdout == "\udcff\udcfeok"
    assert result.stdout.encode("utf-8", "surrogateescape") == b"\xff\xfeok"


def test_decoding_ignores_the_callers_locale(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("LC_ALL", "LANG", "LC_CTYPE"):
        monkeypatch.setenv(var, "C")
    assert sh(r"printf '\303\251'").stdout == "é"


def test_stdin_text_is_encoded_as_utf8_with_surrogateescape(reaper: Reaper) -> None:
    result = gateway.run(
        ["cat"],
        env=gateway.minimal_env(),
        timeout=5,
        max_output=100,
        stdin="é\udcff",
    )
    assert result.stdout == "é\udcff"


# --- byte caps -------------------------------------------------------------------------------


def test_output_exactly_at_the_cap_is_not_too_large(reaper: Reaper) -> None:
    result = sh("printf 12345", max_output=5)
    assert (result.stdout, result.too_large, result.returncode) == ("12345", False, 0)


def test_output_over_the_cap_kills_the_child_and_keeps_the_first_bytes(reaper: Reaper) -> None:
    result = sh("while :; do printf 0123456789; done", max_output=1000)
    assert result.too_large and not result.timed_out
    assert result.stdout == ("0123456789" * 100)
    assert result.returncode == -signal.SIGTERM
    assert result.elapsed < 10


def test_one_byte_over_the_cap_is_too_large(reaper: Reaper) -> None:
    result = sh("printf 123456", max_output=5)
    assert (result.stdout, result.too_large) == ("12345", True)


def test_stderr_is_capped_without_deadlock_and_stdout_stays_intact(reaper: Reaper) -> None:
    result = sh("i=0; while [ $i -lt 20000 ]; do printf 0123456789 >&2; i=$((i+1)); done; echo ok")
    assert result.stdout == "ok\n"
    assert len(result.stderr) == STDERR_CAP and result.stderr_truncated
    assert result.returncode == 0


def test_large_stdout_and_stderr_together_do_not_deadlock(reaper: Reaper) -> None:
    script = (
        "i=0; while [ $i -lt 2000 ]; do printf '%0500d' 0; printf '%0500d' 0 >&2; i=$((i+1)); done"
    )
    result = sh(script, max_output=2 << 20)
    assert len(result.stdout) == 1_000_000 and result.returncode == 0


# --- deadlines and group signalling ----------------------------------------------------------


def test_deadline_terminates_the_child(reaper: Reaper) -> None:
    result = sh("sleep 30", timeout=0.3)
    assert result.timed_out and result.returncode == -signal.SIGTERM
    assert result.elapsed < 5


def test_child_ignoring_sigterm_is_killed_after_two_seconds(reaper: Reaper) -> None:
    assert gateway.TERM_GRACE == 2.0
    # The deadline runs from the spawn; a short one can fire before the shell has installed its
    # trap, and the child then dies of SIGTERM (issue #56). The shell says "ready" once the trap
    # is in place, so the assertions below prove SIGTERM was really ignored.
    result = sh("trap '' TERM; echo ready; while :; do sleep 0.05; done", timeout=1.5)
    assert result.stdout == "ready\n"
    assert result.timed_out and result.returncode == -signal.SIGKILL
    assert 2.0 <= result.elapsed - 1.5 < 5.0  # SIGTERM at the deadline, SIGKILL 2 s later


def test_the_whole_group_dies_not_just_the_leader(reaper: Reaper) -> None:
    result = sh("sleep 60 & echo $!; wait", timeout=3.0)  # upper bound only: start-up must fit
    grandchild = int(result.stdout)
    reaper.track(grandchild)
    assert result.timed_out
    assert is_dead(grandchild)


def test_a_sibling_process_is_untouched_by_a_kill(reaper: Reaper) -> None:
    sibling = subprocess.Popen(["sleep", "60"], start_new_session=True)  # noqa: S603,S607
    reaper.track(sibling.pid)
    sh("sleep 30", timeout=0.2)
    assert sibling.poll() is None


def test_a_leader_that_exited_normally_is_not_waited_out_and_nothing_is_signalled(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[tuple[int, int]] = []
    real = os.killpg
    monkeypatch.setattr(
        gateway, "_killpg", lambda pgid, sig: (sent.append((pgid, sig)), real(pgid, sig))
    )
    pidfile = tmp_path / "pid"
    # The leader exits at once; a background child keeps the pipes open for 4 s.
    result = sh(f"sleep 4 & echo $! > {pidfile}; echo done", timeout=2)
    reaper.track(int(pidfile.read_text()))
    assert (result.stdout, result.returncode, result.timed_out) == ("done\n", 0, False)
    assert result.elapsed < 3.5  # did not wait for the 4 s pipe holder
    assert sent == []


def test_exit_racing_the_deadline_is_not_labelled_timed_out(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The deadline has already passed when the loop first looks (timeout=1 ms), and the leader has
    # exited by then: the exit wins, nothing is signalled, timed_out stays False.
    real = gateway._Child.exited

    def exited_once_dead(self: Any) -> bool:
        while not real(self):
            time.sleep(0.005)
        return True

    monkeypatch.setattr(gateway._Child, "exited", exited_once_dead)
    result = sh("echo quick", timeout=0.001)
    assert (result.stdout, result.returncode) == ("quick\n", 0)
    assert not result.timed_out


def test_timed_out_is_set_only_when_the_leader_was_running_at_the_deadline(
    reaper: Reaper,
) -> None:
    assert sh("sleep 30", timeout=0.2).timed_out


def test_output_written_before_a_normal_exit_is_fully_drained(reaper: Reaper) -> None:
    result = sh("printf 0123456789; printf abc >&2", timeout=5)
    assert (result.stdout, result.stderr) == ("0123456789", "abc")


def test_stop_kills_a_sigterm_ignoring_descendant_after_the_leader_exits(reaper: Reaper) -> None:
    # The leader dies on SIGTERM; its child ignores SIGTERM and holds the pipes.
    result = sh("sh -c 'trap \"\" TERM; echo $$; exec sleep 60' & wait", timeout=3.0)
    grandchild = int(result.stdout)
    reaper.track(grandchild)
    assert result.timed_out
    assert is_dead(grandchild)


def test_the_group_is_signalled_while_the_leader_is_a_zombie_never_after_the_reap(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    states: list[tuple[int, str | None]] = []
    real = os.killpg

    def spy(pgid: int, sig: int) -> None:
        try:
            state = Path(f"/proc/{pgid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            state = None
        states.append((sig, state))
        real(pgid, sig)

    monkeypatch.setattr(gateway, "_killpg", spy)
    result = sh("sh -c 'trap \"\" TERM; echo $$; exec sleep 60' & wait", timeout=3.0)
    reaper.track(int(result.stdout))
    assert [sig for sig, _ in states] == [signal.SIGTERM, signal.SIGKILL]
    assert all(state is not None for _, state in states)  # /proc entry still there: unreaped


def test_signal_group_refuses_once_the_leader_is_reaped(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[int] = []
    monkeypatch.setattr(gateway, "_killpg", lambda pgid, sig: sent.append(sig))
    request = Request(argv=("sleep", "60"), env=gateway.minimal_env(), timeout=1, max_output=1)
    child = gateway._launch(request)
    reaper.track(child.proc.pid)
    assert child.signal_group(signal.SIGCONT) is True
    assert sent == [signal.SIGCONT]
    os.kill(child.proc.pid, signal.SIGKILL)  # the handle's own child, in this test only
    assert child.wait(3)
    assert child.signal_group(signal.SIGKILL) is False
    assert sent == [signal.SIGCONT]


def test_an_unkillable_child_is_abandoned_not_waited_on_forever(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "_killpg", lambda pgid, sig: None)  # signals never land
    started = time.monotonic()
    result = sh("exec sleep 60", timeout=0.2, term_grace=0.1, abandon_after=0.2)
    assert result.abandoned and result.returncode is None and result.timed_out
    assert time.monotonic() - started < 3


def test_a_blocked_stdin_write_is_bounded_by_the_deadline(reaper: Reaper) -> None:
    result = sh("sleep 30", stdin="x" * 2_000_000, timeout=0.3)
    assert result.timed_out


def test_signal_live_terminates_running_children_and_the_calls_return(reaper: Reaper) -> None:
    results: list[gateway.Result] = []
    thread = threading.Thread(target=lambda: results.append(sh("sleep 60", timeout=50)))
    thread.start()
    try:
        end = time.monotonic() + 5
        while not gateway.live_pids() and time.monotonic() < end:
            time.sleep(0.01)
        assert len(gateway.live_pids()) == 1
        assert gateway.signal_live(signal.SIGTERM) == 1
    finally:
        thread.join(10)
    assert not thread.is_alive()
    assert results[0].returncode == -signal.SIGTERM and not results[0].timed_out
    assert gateway.signal_live(signal.SIGTERM) == 0  # nothing live, nothing signalled


def _unregistered_child(reaper: Reaper, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[int]]:
    sent: list[int] = []
    real = os.killpg
    monkeypatch.setattr(gateway, "_killpg", lambda pgid, sig: (sent.append(sig), real(pgid, sig)))
    request = Request(argv=("sleep", "60"), env=gateway.minimal_env(), timeout=1, max_output=1)
    child = gateway._launch(request)
    reaper.track(child.proc.pid)  # _launch already registered it as live
    return child, sent


def test_signal_live_while_the_same_thread_holds_a_childs_lock_queues_instead_of_hanging(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    child, sent = _unregistered_child(reaper, monkeypatch)
    try:
        done: list[int] = []
        # Same thread holds the child's lock (as poll() would when a handler interrupts it).
        child._lock.acquire()
        try:
            worker = threading.Thread(
                target=lambda: done.append(gateway.signal_live(signal.SIGTERM))
            )
            worker.start()
            worker.join(5)
            assert not worker.is_alive(), "signal_live blocked on a held child lock"
        finally:
            child._lock.release()
        assert done == [1] and sent == []  # queued, not lost, not sent under the lock
        assert child.wait(5)  # the owner's next tick delivered the queued SIGTERM
        assert sent == [signal.SIGTERM]
    finally:
        with gateway._LIVE_LOCK:
            gateway._LIVE.pop(child.proc.pid, None)


def test_signal_live_is_reentrant_on_the_live_registry_lock(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    child, sent = _unregistered_child(reaper, monkeypatch)
    out: list[int] = []

    def holder() -> None:
        with gateway._LIVE_LOCK:  # a handler interrupting register/unregister on this thread
            out.append(gateway.signal_live(signal.SIGTERM))

    try:
        thread = threading.Thread(target=holder)
        thread.start()
        thread.join(5)
        assert not thread.is_alive(), "signal_live blocked on the registry lock"
        assert out == [1] and sent == [signal.SIGTERM]
    finally:
        with gateway._LIVE_LOCK:
            gateway._LIVE.pop(child.proc.pid, None)
        assert child.wait(5)


def test_a_queued_signal_never_reaches_a_group_whose_leader_was_reaped_meanwhile(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    child, sent = _unregistered_child(reaper, monkeypatch)
    try:
        child._lock.acquire()
        try:
            assert gateway.signal_live(signal.SIGTERM) == 1  # queued
        finally:
            child._lock.release()
        os.kill(child.proc.pid, signal.SIGKILL)  # the handle's own child, in this test only
        assert is_dead(child.proc.pid)
        assert child.poll() is not None  # reaps, then must find the queued signal stale
        assert sent == []
    finally:
        with gateway._LIVE_LOCK:
            gateway._LIVE.pop(child.proc.pid, None)


def test_a_real_signal_handler_calling_signal_live_while_run_is_in_flight(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired: list[int] = []

    def handler(signum: int, frame: Any) -> None:
        fired.append(gateway.signal_live(signal.SIGTERM))

    old = signal.signal(signal.SIGUSR1, handler)
    me = os.getpid()

    def trigger() -> None:
        end = time.monotonic() + 5
        while not gateway.live_pids() and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.1)
        os.kill(me, signal.SIGUSR1)  # this test's own pid, captured above

    thread = threading.Thread(target=trigger)
    thread.start()
    try:
        result = sh("sleep 60", timeout=30)
    finally:
        thread.join(10)
        signal.signal(signal.SIGUSR1, old)
    assert fired == [1]
    assert result.returncode == -signal.SIGTERM and not result.timed_out


def test_gateway_has_no_pattern_based_signalling() -> None:
    source = Path(gateway.__file__).read_text(encoding="utf-8")
    for forbidden in ("pkill", "killall", "pgrep", "os.kill(", "kill -"):
        assert forbidden not in source
    # Two call sites: _Child._send and the launch-window cleanup in _launch (exact leader pid).
    assert source.count("_killpg(") == 2


# --- launch failures and validation ----------------------------------------------------------


def test_a_missing_program_is_a_launch_error(reaper: Reaper) -> None:
    with pytest.raises(LaunchError, match="no-such-tool"):
        gateway.run(["no-such-tool"], env={}, timeout=5, max_output=10)


def test_a_non_executable_path_is_a_launch_error(reaper: Reaper, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.write_text("x")
    with pytest.raises(LaunchError):
        gateway.run([str(plain)], env={}, timeout=5, max_output=10)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"argv": "echo hi"},
        {"argv": []},
        {"argv": [""]},
        {"argv": ["echo", "a\0b"]},
        {"argv": ["echo", b"x"]},
        {"env": {"A=B": "x"}},
        {"env": {"": "x"}},
        {"env": {"A": "x\0y"}},
        {"env": {"A": 1}},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": True},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"term_grace": float("inf")},
        {"term_grace": float("nan")},
        {"term_grace": -1},
        {"abandon_after": float("inf")},
        {"abandon_after": -0.5},
        {"stdin": 5},
        {"stdin": ["x"]},
        {"stdin": object()},
        {"max_output": 0},
        {"max_output": True},
        {"max_output": 1.5},
    ],
)
def test_bad_arguments_are_rejected_before_anything_starts(
    kwargs: dict[str, Any], fake_gateway: Any
) -> None:
    args: dict[str, Any] = {
        "argv": ["echo"],
        "env": {},
        "timeout": 1,
        "max_output": 10,
    } | kwargs
    argv = args.pop("argv")
    with pytest.raises((TypeError, ValueError)):
        gateway.run(argv, **args)
    assert fake_gateway.calls == []


def test_zero_is_allowed_for_grace_and_the_message_says_non_negative(fake_gateway: Any) -> None:
    fake_gateway.on("echo")
    gateway.run(["echo"], env={}, timeout=1, max_output=10, term_grace=0, abandon_after=0)
    with pytest.raises(ValueError, match="non-negative"):
        gateway.run(["echo"], env={}, timeout=1, max_output=10, term_grace=-1)
    with pytest.raises(ValueError, match="positive"):
        gateway.run(["echo"], env={}, timeout=0, max_output=10)


def test_bytearray_stdin_is_accepted_and_normalised_to_bytes(fake_gateway: Any) -> None:
    fake_gateway.on("cat")
    gateway.run(["cat"], env={}, timeout=1, max_output=10, stdin=bytearray(b"ab"))
    assert fake_gateway.calls[0].stdin == b"ab"
    assert type(fake_gateway.calls[0].stdin) is bytes


def test_bad_stdin_launches_nothing(reaper: Reaper) -> None:
    with pytest.raises(TypeError):
        gateway.run(["cat"], env=gateway.minimal_env(), timeout=1, max_output=10, stdin=5)  # type: ignore[arg-type]
    assert gateway.live_pids() == [] and reaper._targets == []


def test_a_cut_utf8_sequence_leaves_surrogate_escapes_callers_must_handle(reaper: Reaper) -> None:
    result = sh("printf '\\303\\251\\303\\251'", max_output=3)  # two e-acute, cut mid-second
    assert result.too_large and result.stdout == "é\udcc3"
    with pytest.raises(UnicodeEncodeError):
        result.stdout.encode()
    assert result.stdout.encode("utf-8", "surrogateescape") == b"\xc3\xa9\xc3"
    assert result.stdout.encode("utf-8", "replace")


def test_the_poll_interval_starts_tiny_and_caps_at_the_old_tick() -> None:
    assert gateway._TICK_MIN <= 0.002
    assert gateway._TICK == 0.02
    ticks, tick = [], gateway._TICK_MIN
    for _ in range(8):
        ticks.append(tick)
        tick = min(tick * 2, gateway._TICK)
    assert ticks[0] <= 0.002 and ticks[-1] == gateway._TICK


class _Interrupt(BaseException):
    """Stands in for KeyboardInterrupt/SystemExit raised by a handler."""


def _interrupted_wrap(proc: Any) -> Any:
    raise _Interrupt


def test_an_interrupt_between_popen_and_registration_leaves_no_live_child(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "_Child", _interrupted_wrap)
    with pytest.raises(_Interrupt):
        sh("sleep 60", timeout=30, term_grace=0.2)
    pid = reaper._targets[0][0]
    assert is_dead(pid)
    assert gateway.live_pids() == []


def test_an_exception_after_the_child_exists_stops_it_and_unregisters_it(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "_register", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        sh("sleep 60", timeout=30, term_grace=0.2)
    pid = reaper._targets[0][0]
    assert is_dead(pid)
    assert gateway.live_pids() == []


def _boom(child: Any) -> None:
    raise RuntimeError("boom")


def test_python_is_the_interpreter_running_the_tests(reaper: Reaper) -> None:
    result = gateway.run(
        ["python3", "-c", "import sys; print(sys.version_info[:2])"],
        env=gateway.minimal_env(),
        timeout=30,
        max_output=1000,
    )
    assert result.stdout.strip() == str(sys.version_info[:2])
