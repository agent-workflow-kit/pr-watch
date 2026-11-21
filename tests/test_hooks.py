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
from pr_watch import log as log_module
from pr_watch.config import Config
from pr_watch.gateway import LaunchError, Request, Result
from pr_watch.hooks import HookRunner, HookSpec
from pr_watch.state_dir import StateDirError
from pr_watch.store import StoredEvent
from tests.harness import assert_valid
from tests.test_signals import alive, wait_gone

WAIT = 30.0
GOLDEN = Path(__file__).parent / "golden" / "hooks"


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
        try:
            log_module._build(event, level, fields, 0.0)  # what the real Log would refuse
        except ValueError:
            return False
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
    assert not set(env.values()) & {event.pr, event.payload["repo"]}  # no payload host text
    assert not any("a" * 40 in v for v in env.values())  # no payload sha text
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
    (cap,) = log.of("hook_cap")  # accepted by the log's own validation (Records checks it)
    assert cap["skipped"] == 3
    assert (cap["first_event_id"], cap["last_event_id"]) == (ev(1).id, ev(2).id)


def test_a_skipped_post_poll_alone_still_writes_a_hook_cap_record_without_event_ids(
    cwd: Path,
) -> None:
    log = Records()
    runner = make(cwd, {"post-poll": [spec("x", "post-poll")]}, log, max_per_poll=0)
    with gateway.use_backend(ok_backend):
        runner.submit_post_poll({"object_id": "T"})
        counts = runner.finish()
    assert (counts.run, counts.skipped) == (0, 1)
    assert log.of("hook_cap") == [{"skipped": 1, "message": "hooks skipped by the cap"}]
    assert_valid("log", {"ts": "2026-10-03T00:00:00.000000Z", "level": "warn",
                         "event": "hook_cap", **log.of("hook_cap")[0]})  # fmt: skip


def test_a_payload_that_cannot_be_encoded_is_skipped_with_its_own_message(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(payload: Any) -> bytes:
        raise ValueError("no")

    log = Records()
    runner = make(cwd, {"on-pushed": [spec("x")]}, log)
    monkeypatch.setattr(hooks, "encode_payload", boom)
    runner.submit_events([ev(1)])
    counts = runner.finish()
    assert (counts.run, counts.skipped) == (0, 1)
    (cap,) = log.of("hook_cap")
    assert cap["message"] == "hooks skipped: payload could not be encoded"
    assert cap["first_event_id"] == cap["last_event_id"] == ev(1).id


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


@pytest.mark.parametrize(
    ("result", "logged"),
    [
        (Result(returncode=3), True),  # it failed on its own while the stop flag was set
        (Result(returncode=-signal.SIGTERM, timed_out=True), True),  # its own timeout
        (Result(returncode=-signal.SIGTERM), False),  # our shutdown's SIGTERM
        (Result(returncode=-signal.SIGKILL), False),  # our shutdown's SIGKILL
        (Result(returncode=None, abandoned=True), False),
    ],
)
def test_a_hook_that_failed_on_its_own_is_logged_even_when_the_stop_flag_is_set(
    cwd: Path, result: Result, logged: bool
) -> None:
    stop = [False]
    log = Records()
    runner = make(cwd, {"on-pushed": [spec("a")]}, log, should_stop=lambda: stop[0])

    def backend(request: Request) -> Result:
        stop[0] = True  # the poll is signalled while the hook runs
        return result

    with gateway.use_backend(backend):
        runner.submit_events([ev(1)])
        counts = runner.finish()
    assert counts.failed == 1
    assert (len(log.of("hook_run")) == 1) is logged


def test_a_launch_refused_by_the_shutdown_writes_no_hook_run(cwd: Path) -> None:
    stop = [False]
    log = Records()
    runner = make(cwd, {"on-pushed": [spec("a")]}, log, should_stop=lambda: stop[0])

    def backend(request: Request) -> Result:
        stop[0] = True
        raise LaunchError("/bin/sh: not started: shutting down")

    with gateway.use_backend(backend):
        runner.submit_events([ev(1)])
        counts = runner.finish()
    assert counts.failed == 1 and log.of("hook_run") == []


def failed(kind: str, ref: str, code: str) -> dict[str, str]:
    return {"kind": kind, "ref": ref, "code": code}


def report(failures: list[dict[str, str]], hosts: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "started_at": "2026-10-03T00:00:00.000000Z", "duration_ms": 1200, "full_pass": False,
        "prs": {"snapshotted": 2, "unchanged": 1, "skipped_locked": 0, "skipped_conflict": 0,
                "unreachable": 0, "incomplete": 0},
        "events": {"pushed": 1}, "hooks": {"run": 1, "skipped": 0, "failed": 0},
        "hosts": hosts or {}, "failed": failures,
    }  # fmt: skip


def test_a_pr_failing_too_large_alone_does_not_fire_post_poll_but_other_failures_do() -> None:
    too_large = report([failed("pr", "example.net/o/r#1", "too_large")])
    assert not hooks.should_post_poll(too_large, set(), [])
    assert hooks.should_post_poll(too_large, set(), [("example.net/o/r#1", "P1", "too_large")])
    other = report([failed("pr", "example.net/o/r#1", "not_found")])
    assert hooks.should_post_poll(other, set(), [])
    mixed = report([failed("pr", "example.net/o/r#1", "too_large"), failed("scope", "s", "x")])
    assert hooks.should_post_poll(mixed, set(), [])


def test_failed_host_entries_without_a_hosts_entry_are_in_post_poll_errors() -> None:
    rep = report(
        [
            failed("host", "a.example.net", "auth_required"),
            failed("host", "a.example.net", "auth_required"),
            failed("host", "a.example.net", "rate_limited"),
            failed("host", "whoami", "garbled"),
            failed("host", "Bad Host!", "x"),
            failed("pr", "example.net/o/r#1", "not_found"),
            failed("host", "b.example.net", "network_error"),
        ],
        hosts={
            "b.example.net": {"errors": [{"code": "contract", "pr": None, "retry_after": None}]}
        },
    )
    payload = hooks.post_poll_payload(rep, [])
    assert payload["errors"] == [
        {"host": "b.example.net", "codes": ["contract", "network_error"]},
        {"host": "a.example.net", "codes": ["auth_required", "rate_limited"]},
    ]
    assert_valid("hooks/post-poll", payload)


def check_golden(name: str, actual: Any) -> None:
    path = GOLDEN / f"{name}.json"
    if not path.exists():  # first run: write it, fail, and review the file before committing
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.fail(f"golden {path.name} was missing and has been written; review it")
    assert actual == json.loads(path.read_text())


def test_the_on_pushed_payload_golden() -> None:
    event = ev(1, "pushed")
    event.payload.update(from_sha="b" * 40, to_sha="a" * 40, forced=False)
    payload = hooks.event_payload(event)
    assert_valid("hooks/on-pushed", payload)
    check_golden("on-pushed", payload)


@pytest.mark.parametrize("kind", ["opened", "reopened"])
def test_the_on_opened_and_on_reopened_payload_goldens(kind: str) -> None:
    event = ev(1, kind)
    if kind == "opened":
        event.payload.update(occurred_at="2026-10-02T09:00:00.000000Z")
    payload = hooks.event_payload(event)
    assert_valid(f"hooks/on-{kind}", payload)
    assert payload["hook"] == f"on-{kind}" and payload["type"] == kind
    check_golden(f"on-{kind}", payload)


def test_the_post_poll_payload_golden() -> None:
    rep = report(
        [
            failed("host", "a.example.net", "auth_required"),
            failed("pr", "example.net/o/r#1", "gone"),
        ],
        hosts={
            "b.example.net": {"errors": [{"code": "rate_limited", "pr": None, "retry_after": 60}]}
        },
    )
    payload = hooks.post_poll_payload(rep, [("example.net/o/r#2", "P2", "not_found")])
    assert_valid("hooks/post-poll", payload)
    check_golden("post-poll", payload)


def test_home_and_xdg_are_passed_only_when_absolute_and_home_falls_back_to_the_account() -> None:
    import pwd

    account = pwd.getpwuid(os.geteuid()).pw_dir
    rel = hooks.hook_environment(
        "post-poll", "T", None, "poll",
        environ={"HOME": "rel/home", "XDG_STATE_HOME": "rel", "XDG_CONFIG_HOME": "/abs"},
    )  # fmt: skip
    assert rel["HOME"] == account and "XDG_STATE_HOME" not in rel
    assert rel["XDG_CONFIG_HOME"] == "/abs"
    unset = hooks.hook_environment("post-poll", "T", None, "poll", environ={})
    assert unset["HOME"] == account
    kept = hooks.hook_environment("post-poll", "T", None, "poll", environ={"HOME": "/h"})
    assert kept["HOME"] == "/h"


@pytest.fixture(autouse=True)
def _no_leaked_threads() -> Iterator[None]:
    yield
    for thread in threading.enumerate():
        if thread.name == "pr-watch-hook":
            thread.join(WAIT)
            assert not thread.is_alive()
