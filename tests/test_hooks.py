"""The hook runner (#28): payloads, environment, the bounded pool, caps, timeouts, process groups.

Unit level: real `/bin/sh` hooks through the real gateway where the process is the point, a
scripted gateway backend where timing and concurrency are (no sleeps the test relies on: every
wait is on an Event or a handshake file). Hooks write into tmp dirs; pids and process groups are
recorded by the hook itself and killed exactly, never by name. The poll wiring is in
tests/test_hooks_poll.py.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch import gateway, hooks
from pr_watch.config import Config
from pr_watch.gateway import LaunchError, Request, Result
from pr_watch.hooks import HookRunner, HookSpec
from pr_watch.state_dir import StateDirError
from pr_watch.store import StoredEvent
from tests.harness import assert_valid
from tests.test_signals import alive, wait_gone

WAIT = 30.0


def ev(n: int, type_: str = "pushed", pr_id: str = "P1") -> StoredEvent:
    payload: dict[str, Any] = {
        "repo": "example.net/o/r",
        "occurred_at": None,
        "head_sha": "a" * 40,
        "watches": ["w-aaaaaaaaaa"],
    }
    if type_ == "merged":
        payload |= {"merged_head_sha": None, "merge_commit_sha": None}
    return StoredEvent(
        seq=n, id=f"ev-{'a' * 11}{'abcdefg234567'[n % 13]}", type=type_, pr_id=pr_id,
        pr="example.net/o/r#1", payload=payload, detected_at="2026-10-03T00:00:00.000000Z",
        labels=["k=v"],
    )  # fmt: skip


@pytest.fixture
def cwd(tmp_path: Path) -> Path:
    path = tmp_path / "hooks-cwd"
    path.mkdir(mode=0o700)
    return path


class Records:
    """A Log stand-in that keeps what was written (the real Log validates the same fields)."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def write(self, event: str, level: str = "info", **fields: Any) -> bool:
        with self._lock:
            self.items.append((event, level, fields))
        return True

    def of(self, event: str) -> list[dict[str, Any]]:
        return [f for e, _, f in self.items if e == event]


def make(cwd: Path, specs: dict[str, list[HookSpec]], log: Any = None, **kw: Any) -> HookRunner:
    opts: dict[str, Any] = {
        "max_per_poll": 50,
        "max_per_pr": 10,
        "concurrency": 4,
        "hooks_cwd": lambda: cwd,
        "environ": {"PATH": os.environ["PATH"], "HOME": str(cwd)},
    }
    opts.update(kw)
    return HookRunner(specs, log=log if log is not None else Records(), **opts)  # type: ignore[arg-type]


def spec(cmd: str, name: str = "on-pushed", **kw: Any) -> HookSpec:
    return HookSpec(name, cmd, **kw)


# --- environment ---------------------------------------------------------------------------------


def test_the_environment_is_built_from_scratch_with_only_the_documented_names() -> None:
    environ = {
        "PATH": "/usr/bin:relative/bin::/bin", "HOME": "/h", "LANG": "C", "TERM": "xterm",
        "XDG_STATE_HOME": "/s", "GH_TOKEN": "t", "SSH_AUTH_SOCK": "/x", "FOO": "foo",
        "PR_WATCH_ID": "spoof", "LD_PRELOAD": "/evil.so",
    }  # fmt: skip
    env = hooks.hook_environment(
        "on-pushed", "P1", "ev-aaaaaaaaaaaa", "pr", environ=environ, env_allow=["FOO", "MISSING"]
    )
    assert env == {
        "PATH": "/usr/bin:/bin", "HOME": "/h", "LANG": "C", "TERM": "xterm",
        "XDG_STATE_HOME": "/s", "FOO": "foo", "PR_WATCH_HOOK": "on-pushed",
        "PR_WATCH_ID": "P1", "PR_WATCH_EVENT_ID": "ev-aaaaaaaaaaaa",
        "PR_WATCH_OBJECT_TYPE": "pr",
    }  # fmt: skip


def test_a_path_with_no_absolute_entry_falls_back_and_an_allowed_path_is_still_filtered() -> None:
    env = hooks.hook_environment("post-poll", "T", None, "poll", environ={"PATH": ".:bin"})
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin" and env["PR_WATCH_EVENT_ID"] == ""
    env = hooks.hook_environment(
        "post-poll", "T", None, "poll", environ={"PATH": "/a:rel"}, env_allow=["PATH"]
    )
    assert env["PATH"] == "/a"


def test_an_allowed_name_cannot_replace_a_reserved_one() -> None:
    env = hooks.hook_environment(
        "on-pushed", "P1", None, "pr", environ={"PR_WATCH_HOOK": "x"}, env_allow=["PR_WATCH_HOOK"]
    )
    assert env["PR_WATCH_HOOK"] == "on-pushed"


# --- payload and process -------------------------------------------------------------------------


def test_the_payload_is_the_event_plus_the_hook_fields_and_matches_its_schema() -> None:
    payload = hooks.event_payload(ev(1, "merged"))
    assert_valid("hooks/on-merged", payload)
    assert payload["event_id"] == payload["id"] and payload["object_id"] == "P1"
    assert {k: v for k, v in payload.items() if k in ev(1, "merged").to_json()} == (
        ev(1, "merged").to_json()
    )  # the event shape is untouched


def test_a_hook_gets_the_payload_on_stdin_only_and_runs_in_hooks_cwd(
    cwd: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cmd = (
        f'cat > {out}.in; pwd -P > {out}.cwd; printf "%s|" "$#" "$0" > {out}.args; env > {out}.env'
    )
    event = ev(1)
    runner = make(cwd, {"on-pushed": [spec(cmd)]})
    runner.submit_events([event])
    counts = runner.finish()
    assert (counts.run, counts.failed, counts.skipped) == (1, 0, 0)
    assert json.loads(Path(f"{out}.in").read_text()) == hooks.event_payload(event)
    assert Path(f"{out}.cwd").read_text().strip() == str(cwd.resolve())
    assert Path(f"{out}.args").read_text() == "0|/bin/sh|"  # no payload in argv
    env = dict(line.split("=", 1) for line in Path(f"{out}.env").read_text().splitlines()
               if "=" in line)  # fmt: skip
    assert env["PR_WATCH_HOOK"] == "on-pushed" and env["PR_WATCH_ID"] == "P1"
    assert env["PR_WATCH_EVENT_ID"] == event.id and env["PR_WATCH_OBJECT_TYPE"] == "pr"
    assert not any("example.net" in v or "a" * 40 in v for v in env.values())  # no payload text
    assert set(env) <= {
        "PATH", "HOME", "PWD", "OLDPWD", "_", "SHLVL", "PR_WATCH_HOOK", "PR_WATCH_ID",
        "PR_WATCH_EVENT_ID", "PR_WATCH_OBJECT_TYPE",
    }  # fmt: skip
    (record,) = runner._log.of("hook_run")  # type: ignore[attr-defined]
    assert record["exit_code"] == 0 and record["hook"] == "on-pushed"
    assert record["event_id"] == event.id and record["timed_out"] is False


def test_hook_output_is_discarded_and_never_blocks(cwd: Path) -> None:
    flood = (
        "i=0; while [ $i -lt 300 ]; do printf '%0200d' 0; printf '%0200d' 0 >&2; i=$((i+1)); done"
    )
    runner = make(cwd, {"on-pushed": [spec(flood)]})
    runner.submit_events([ev(1)])
    assert runner.finish().failed == 0


def test_a_hook_that_closes_stdin_early_does_not_break_the_poll(cwd: Path, tmp_path: Path) -> None:
    big = ev(1)
    big.payload["blob"] = "x" * 3_000_000  # more than a pipe holds
    done = tmp_path / "done"
    runner = make(cwd, {"on-pushed": [spec(f"exec 0<&-; : > {done}")]})
    runner.submit_events([big])
    counts = runner.finish()
    assert done.exists() and (counts.run, counts.failed) == (1, 0)


def test_a_nonzero_exit_is_a_failure_that_is_logged_and_never_raised(cwd: Path) -> None:
    runner = make(cwd, {"on-pushed": [spec("exit 7"), spec("kill -TERM $$")]})
    runner.submit_events([ev(1)])
    counts = runner.finish()
    assert (counts.run, counts.failed) == (2, 2)
    codes = sorted(r["exit_code"] for r in runner._log.of("hook_run"))  # type: ignore[attr-defined]
    assert codes == [7, 128 + signal.SIGTERM]


def test_hooks_of_other_types_do_not_fire_and_post_poll_is_not_an_event_hook(cwd: Path) -> None:
    runner = make(cwd, {"on-comment": [spec("exit 0", "on-comment")], "post-poll": [
        spec("exit 0", "post-poll")]})  # fmt: skip
    runner.submit_events([ev(1, "pushed")])
    assert runner.finish().run == 0


# --- timeouts, process groups, abandon -----------------------------------------------------------


def pid_files(tmp_path: Path, tag: str) -> tuple[Path, Path]:
    return tmp_path / f"{tag}.leader", tmp_path / f"{tag}.child"


def read_pid(path: Path) -> int:
    """The pid a hook wrote (atomically, by mv) to `path`; waits for the handshake file."""
    t0 = time.monotonic()
    while not (path.exists() and path.read_text().endswith("\n")):  # `echo` writes it whole
        assert time.monotonic() - t0 < WAIT, f"{path} never written"
        time.sleep(0.01)
    return int(path.read_text().strip())


def test_a_timeout_kills_the_whole_group_and_marks_timed_out(
    cwd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "TERM_GRACE", 0.3)
    leader, child = pid_files(tmp_path, "t")
    # the leader and a background child (same group) both ignore SIGTERM
    cmd = (
        f"trap '' TERM; (trap '' TERM; echo $$ > {child}; "
        f"exec sleep 300) & echo $$ > {leader}; wait"
    )
    runner = make(cwd, {"on-pushed": [spec(cmd, timeout=1)]})
    pids: list[int] = []
    try:
        runner.submit_events([ev(1)])
        counts = runner.finish()
        pids = [read_pid(leader), read_pid(child)]
        assert (counts.run, counts.failed) == (1, 1)
        (rec,) = runner._log.of("hook_run")  # type: ignore[attr-defined]
        assert rec["timed_out"] is True and rec["exit_code"] == 128 + signal.SIGKILL
        for pid in pids:
            assert wait_gone(pid), f"{pid} outlived its hook"
    finally:
        for pid in pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)  # a pid this test saw the hook write


def test_the_real_term_grace_is_five_seconds_and_the_abandon_wait_five() -> None:
    assert (hooks.TERM_GRACE, hooks.ABANDON_AFTER) == (5.0, 5.0)


def test_hooks_run_with_the_documented_gateway_arguments(cwd: Path) -> None:
    seen: list[Request] = []

    def backend(request: Request) -> Result:
        seen.append(request)
        return Result(returncode=0)

    runner = make(cwd, {"on-pushed": [spec("true", timeout=9)]})
    with gateway.use_backend(backend):
        runner.submit_events([ev(1)])
        runner.finish()
    (req,) = seen
    assert req.argv == ("/bin/sh", "-c", "true") and req.timeout == 9
    assert (req.term_grace, req.abandon_after, req.discard_output) == (5.0, 5.0, True)
    assert req.cwd == str(cwd) and req.stdin is not None
    assert json.loads(req.stdin)["hook"] == "on-pushed"


def test_an_abandoned_hook_is_logged_null_timed_out_and_frees_its_slot(cwd: Path) -> None:
    calls: list[str] = []

    def backend(request: Request) -> Result:
        calls.append(request.argv[2])
        if request.argv[2] == "stuck":
            return Result(returncode=None, timed_out=True, abandoned=True)
        return Result(returncode=0)

    runner = make(cwd, {"on-pushed": [spec("stuck"), spec("next")]}, concurrency=1)
    with gateway.use_backend(backend):
        runner.submit_events([ev(1)])
        counts = runner.finish()
    assert calls == ["stuck", "next"] and (counts.run, counts.failed) == (2, 1)
    stuck = runner._log.of("hook_run")[0]  # type: ignore[attr-defined]
    assert stuck["exit_code"] is None and stuck["timed_out"] is True


def test_a_spawn_failure_is_a_failed_run_with_a_null_exit_code(cwd: Path) -> None:
    def backend(request: Request) -> Result:
        raise LaunchError("cannot start")

    runner = make(cwd, {"on-pushed": [spec("x")]})
    with gateway.use_backend(backend):
        runner.submit_events([ev(1)])
        counts = runner.finish()
    assert (counts.run, counts.failed) == (1, 1)
    (rec,) = runner._log.of("hook_run")  # type: ignore[attr-defined]
    assert rec["exit_code"] is None and rec["timed_out"] is False


def test_an_unusable_hooks_cwd_fails_every_hook_and_the_poll_goes_on(tmp_path: Path) -> None:
    def refuse() -> Path:
        raise StateDirError("hooks-cwd is a symlink")

    ran: list[str] = []

    def backend(request: Request) -> Result:
        ran.append("x")
        return Result(returncode=0)

    runner = make(tmp_path, {"on-pushed": [spec("a"), spec("b")]}, hooks_cwd=refuse)
    with gateway.use_backend(backend):
        runner.submit_events([ev(1)])
        counts = runner.finish()
    assert ran == [] and (counts.run, counts.failed) == (2, 2)
    assert all(r["exit_code"] is None for r in runner._log.of("hook_run"))  # type: ignore[attr-defined]


# --- caps ----------------------------------------------------------------------------------------


def run_names(log: Records) -> list[str]:
    return [r["hook"] for r in log.of("hook_run")]


def ok_backend(request: Request) -> Result:
    return Result(returncode=0)


def test_the_per_poll_cap_skips_the_rest_with_one_record_naming_first_and_last(cwd: Path) -> None:
    log = Records()
    runner = make(cwd, {"on-pushed": [spec("a")]}, log, max_per_poll=3)
    events = [ev(i) for i in range(1, 8)]
    with gateway.use_backend(ok_backend):
        runner.submit_events(events)
        counts = runner.finish()
    assert (counts.run, counts.skipped) == (3, 4)
    (cap,) = log.of("hook_cap")
    assert cap == {"skipped": 4, "first_event_id": events[3].id, "last_event_id": events[6].id,
                   "message": "hooks skipped by the cap"}  # fmt: skip


def test_the_per_pr_cap_leaves_the_budget_to_other_prs(cwd: Path) -> None:
    log = Records()
    runner = make(cwd, {"on-pushed": [spec("a")]}, log, max_per_pr=2)
    with gateway.use_backend(ok_backend):
        runner.submit_events([ev(i, pr_id="FLOOD") for i in range(1, 6)])
        runner.submit_events([ev(9, pr_id="OTHER")])
        counts = runner.finish()
    assert (counts.run, counts.skipped) == (3, 3)


def test_merged_closed_and_post_poll_are_exempt_from_both_caps_and_run_first(cwd: Path) -> None:
    order: list[str] = []
    gate = threading.Event()

    def backend(request: Request) -> Result:
        gate.wait(WAIT)
        order.append(request.argv[2])
        return Result(returncode=0)

    specs = {n: [spec(n, n)] for n in ("on-pushed", "on-merged", "on-closed", "post-poll")}
    runner = make(cwd, specs, max_per_poll=1, max_per_pr=1, concurrency=1)
    with gateway.use_backend(backend):
        runner.submit_events([ev(1, "pushed"), ev(2, "pushed")])  # 1 accepted, 1 skipped
        runner.submit_events([ev(3, "merged", "P1"), ev(4, "closed", "P1")])
        runner.submit_post_poll({"object_id": "2026-10-03T00:00:00.000000Z"})
        gate.set()
        counts = runner.finish()
    assert counts.skipped == 1 and counts.run == 4
    assert sorted(order).count("on-pushed") == 1


def test_exempt_hooks_are_queued_ahead_of_capped_ones(cwd: Path) -> None:
    started = threading.Event()
    gate = threading.Event()
    order: list[str] = []

    def backend(request: Request) -> Result:
        order.append(request.argv[2])
        started.set()
        gate.wait(WAIT)
        return Result(returncode=0)

    specs = {n: [spec(n, n)] for n in ("on-pushed", "on-merged")}
    runner = make(cwd, specs, concurrency=1)
    with gateway.use_backend(backend):
        runner.submit_events([ev(1, "pushed")])
        assert started.wait(WAIT)  # the worker holds the first one
        runner.submit_events([ev(2, "pushed"), ev(3, "merged")])
        gate.set()
        runner.finish()
    assert order == ["on-pushed", "on-merged", "on-pushed"]


def test_a_max_per_poll_of_zero_runs_no_hook_at_all(cwd: Path) -> None:
    log = Records()
    specs = {n: [spec(n, n)] for n in ("on-pushed", "on-merged", "post-poll")}
    runner = make(cwd, specs, log, max_per_poll=0)
    with gateway.use_backend(ok_backend):
        runner.submit_events([ev(1, "pushed"), ev(2, "merged")])
        runner.submit_post_poll({"object_id": "T"})
        counts = runner.finish()
    assert (counts.run, counts.skipped) == (0, 3) and log.of("hook_run") == []


# --- the pool ------------------------------------------------------------------------------------


def test_at_most_concurrency_hooks_run_together_and_all_of_them_run(cwd: Path) -> None:
    lock = threading.Lock()
    active = peak = done = 0
    full = threading.Event()

    def backend(request: Request) -> Result:
        nonlocal active, peak, done
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                full.set()
        assert full.wait(WAIT)  # two are inside together; a third must wait for one to leave
        with lock:
            active -= 1
            done += 1
        return Result(returncode=0)

    runner = make(cwd, {"on-pushed": [spec("a")]}, concurrency=2)
    with gateway.use_backend(backend):
        runner.submit_events([ev(i) for i in range(1, 7)])
        counts = runner.finish()
    assert peak == 2 and done == 6 and counts.run == 6


def test_async_false_hooks_never_overlap_but_do_not_hold_back_async_ones(cwd: Path) -> None:
    lock = threading.Lock()
    sync_active = 0
    overlap = False
    sync_started = threading.Event()
    async_done = threading.Event()
    order: list[str] = []

    def backend(request: Request) -> Result:
        nonlocal sync_active, overlap
        name = request.argv[2]
        if name.startswith("sync"):
            with lock:
                sync_active += 1
                overlap = overlap or sync_active > 1
                order.append(name)
            if name == "sync1":
                sync_started.set()
                assert async_done.wait(WAIT)  # an async hook runs while sync1 is still running
            with lock:
                sync_active -= 1
        else:
            assert sync_started.wait(WAIT)
            async_done.set()
        return Result(returncode=0)

    specs = {"on-pushed": [spec("sync1", is_async=False), spec("sync2", is_async=False),
                           spec("async1")]}  # fmt: skip
    runner = make(cwd, specs, concurrency=3)
    with gateway.use_backend(backend):
        runner.submit_events([ev(1)])
        runner.finish()
    assert not overlap and order == ["sync1", "sync2"]  # queue order


def test_the_drain_bound_skips_capped_hooks_but_not_exempt_ones(cwd: Path) -> None:
    now = [0.0]
    log = Records()
    specs = {n: [spec(n, n)] for n in ("on-pushed", "on-merged")}
    runner = make(cwd, specs, log, started=0.0, drain_after=100.0, clock=lambda: now[0])
    with gateway.use_backend(ok_backend):
        runner.submit_events([ev(1, "pushed")])
        runner.finish()
        now[0] = 100.0
        runner.submit_events([ev(2, "pushed"), ev(3, "merged")])
        counts = runner.finish()
    assert (counts.run, counts.skipped) == (2, 1)
    assert run_names(log) == ["on-pushed", "on-merged"]
    (cap,) = log.of("hook_cap")
    assert cap["skipped"] == 1 and cap["first_event_id"] == ev(2).id


def test_the_drain_bound_is_the_max_duration_plus_two_minutes() -> None:
    config = Config.model_validate({"poll": {"max_duration": "10m"}})
    runner = HookRunner.from_config(
        config,
        None,
        log=Records(),  # type: ignore[arg-type]
        started=0.0,
        should_stop=lambda: False,  # type: ignore[arg-type]
    )
    assert runner._drain_after == 600 + 120


def test_after_a_stop_nothing_more_starts_and_the_queue_is_dropped(cwd: Path) -> None:
    stop = [False]
    ran: list[str] = []
    started = threading.Event()
    gate = threading.Event()

    def backend(request: Request) -> Result:
        ran.append(request.argv[2])
        started.set()
        gate.wait(WAIT)
        return Result(returncode=-signal.SIGTERM)

    runner = make(cwd, {"on-pushed": [spec("a")]}, concurrency=1, should_stop=lambda: stop[0])
    with gateway.use_backend(backend):
        runner.submit_events([ev(1), ev(2), ev(3)])
        assert started.wait(WAIT)
        stop[0] = True
        gate.set()
        runner.finish()
    assert ran == ["a"]  # the one in flight only; its stop is not a logged run
    assert runner._log.of("hook_run") == []  # type: ignore[attr-defined]
    runner.submit_events([ev(4)])  # and nothing new is accepted
    assert runner.finish().run == 1


def test_no_specs_means_nothing_is_queued(cwd: Path) -> None:
    runner = make(cwd, {})
    runner.submit_events([ev(1)])
    assert not runner.active and runner.finish().run == 0


@pytest.fixture(autouse=True)
def _no_leaked_threads() -> Iterator[None]:
    yield
    for thread in threading.enumerate():
        if thread.name == "pr-watch-hook":
            thread.join(WAIT)
            assert not thread.is_alive()
