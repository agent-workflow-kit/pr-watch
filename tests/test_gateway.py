"""The real gateway backend: stdio, caps, deadlines, group signalling and decoding."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pr_watch import gateway
from pr_watch.gateway import STDERR_CAP, LaunchError, Request
from tests.gateway_support import Reaper

PY_ONLY = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")


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


def test_argv0_resolves_against_the_callers_path_not_the_childs(reaper: Reaper) -> None:
    result = gateway.run(["echo", "hi"], env={}, timeout=5, max_output=100)
    assert result.stdout == "hi\n"


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
    result = sh("trap '' TERM; while :; do sleep 0.05; done", timeout=0.3)
    assert result.timed_out and result.returncode == -signal.SIGKILL
    assert 2.0 <= result.elapsed - 0.3 < 4.0  # SIGTERM at the deadline, SIGKILL 2 s later


def test_the_whole_group_dies_not_just_the_leader(reaper: Reaper) -> None:
    result = sh("sleep 60 & echo $!; wait", timeout=0.5)
    grandchild = int(result.stdout)
    reaper.track(grandchild)
    assert result.timed_out
    assert is_dead(grandchild)


def test_a_sibling_process_is_untouched_by_a_kill(reaper: Reaper) -> None:
    sibling = subprocess.Popen(["sleep", "60"], start_new_session=True)  # noqa: S603,S607
    reaper.track(sibling.pid)
    sh("sleep 30", timeout=0.2)
    assert sibling.poll() is None


def test_a_reaped_leader_is_never_signalled(
    reaper: Reaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[int, int]] = []
    real = os.killpg
    monkeypatch.setattr(
        gateway, "_killpg", lambda pgid, sig: (sent.append((pgid, sig)), real(pgid, sig))
    )
    # The leader exits at once but a background child keeps the pipes open until the deadline.
    result = sh("sleep 60 & echo $!; exit 0", timeout=0.4)
    reaper.track(int(result.stdout))
    assert result.timed_out and result.returncode == 0
    assert sent == []


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
    result = sh("sleep 60", timeout=0.2, term_grace=0.1, abandon_after=0.2)
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


def test_gateway_has_no_pattern_based_signalling() -> None:
    source = Path(gateway.__file__).read_text(encoding="utf-8")
    for forbidden in ("pkill", "killall", "pgrep", "os.kill(", "kill -"):
        assert forbidden not in source
    assert source.count("_killpg(") == 1  # one choke point: _Child.signal_group


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


def test_python_is_the_interpreter_running_the_tests(reaper: Reaper) -> None:
    result = gateway.run(
        ["python3", "-c", "import sys; print(sys.version_info[:2])"],
        env=gateway.minimal_env(),
        timeout=30,
        max_output=1000,
    )
    assert result.stdout.strip() == str(sys.version_info[:2])
