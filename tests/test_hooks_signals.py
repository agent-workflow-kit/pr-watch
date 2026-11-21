"""Hooks under a real `pr-watch poll` process (#28): signals, SIGKILL, ordering against the commit,
process-group limits. Reuses the handshakes of tests/test_signals.py (file based readiness, exact
pid teardown through the Reaper), never a sleep the test relies on and never a kill by name.

Every pid a hook writes is tracked by the Reaper the moment the test reads it (a pidfd or the pid
plus its start time), so teardown kills exactly those processes and nothing else.
"""

from __future__ import annotations

import signal
import time
from pathlib import Path
from typing import Any

import pytest

from tests.forgectl_support import FakeForgectl
from tests.gateway_support import Reaper
from tests.test_signals import (
    POLL_COMMIT,
    STOPS,
    Launch,
    alive,
    baselined,
    check_clean_exit,
    events,
    fake,  # noqa: F401  (fixture)
    group_members,
    launch,  # noqa: F401  (fixture)
    log_records,
    meta,
    pushed,
    run_to_end,
    store,  # noqa: F401  (fixture)
    wait_gone,
)

WAIT = 60.0


def pid_of(path: Path, reaper: Reaper) -> int:
    """The pid a hook wrote with `echo` (complete once it ends in a newline); now tracked."""
    t0 = time.monotonic()
    while not (path.exists() and path.read_text().endswith("\n")):
        assert time.monotonic() - t0 < WAIT, f"{path} never written"
        time.sleep(0.01)
    pid = int(path.read_text().strip())
    reaper.track(pid)
    return pid


def config(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def hook_config(tmp_path: Path, cmd: str, **extra: Any) -> str:
    lines = ["hooks:"] + [f"  {k}: {v}" for k, v in extra.items()]
    body = "\n".join([*lines, "  on-pushed:", f'    - cmd: "{cmd}"', ""])
    return config(tmp_path, "hooks", body)


@pytest.mark.parametrize("number", STOPS)
def test_a_signal_while_a_hook_runs_stops_it_and_its_group_and_prints_no_report(
    number: signal.Signals,
    launch: Launch,  # noqa: F811
    fake: FakeForgectl,  # noqa: F811
    store: Any,  # noqa: F811
    reaper: Reaper,
    tmp_path: Path,
) -> None:
    baselined(launch, fake)
    pushed(fake)
    leader, child = tmp_path / "leader", tmp_path / "child"
    cfg = hook_config(
        tmp_path, f"(exec sh -c 'echo $$ > {child}; exec sleep 300') & echo $$ > {leader}; wait"
    )
    run = launch("--config", cfg, "poll")
    pids = [pid_of(leader, reaper), pid_of(child, reaper)]
    run.signal(number)
    out, _ = check_clean_exit(run, number, timeout=60)
    assert out == ""  # no report
    for pid in pids:
        assert wait_gone(pid), f"hook process {pid} outlived the poll"
    assert events(store) == ["pushed"]  # committed before the hook ran
    assert [r for r in log_records(store_dir(store)) if r["event"] == "hook_run"] == []
    ends = [r for r in log_records(store_dir(store)) if r["event"] == "poll_end"]
    assert ends[-1]["interrupted"] is True


def store_dir(db: Any) -> Path:
    return Path(db.path).parent


def test_a_hook_that_ignores_sigterm_and_its_ignoring_child_are_killed_by_the_shutdown(
    launch: Launch,  # noqa: F811
    fake: FakeForgectl,  # noqa: F811
    reaper: Reaper,
    tmp_path: Path,
) -> None:
    baselined(launch, fake)
    pushed(fake)
    leader, child = tmp_path / "leader", tmp_path / "child"
    cfg = hook_config(
        tmp_path,
        f"trap '' TERM; (exec sh -c 'echo $$ > {child}; exec sleep 300') & "
        f"echo $$ > {leader}; wait",
    )
    run = launch("--config", cfg, "poll")
    pids = [pid_of(leader, reaper), pid_of(child, reaper)]
    assert all(alive(p) for p in pids)
    pgid = pids[0]  # the hook's shell leads its own group
    assert sorted(group_members(pgid)) == sorted(pids)
    run.signal(signal.SIGTERM)
    check_clean_exit(run, signal.SIGTERM, timeout=60)  # SHUTDOWN_KILL_AFTER (5 s), not 300 s
    for pid in pids:
        assert wait_gone(pid), f"{pid} outlived the poll"


def test_a_sigkilled_poll_loses_its_pending_hooks_and_a_later_poll_does_not_rerun_them(
    launch: Launch,  # noqa: F811
    fake: FakeForgectl,  # noqa: F811
    store: Any,  # noqa: F811
    reaper: Reaper,
    tmp_path: Path,
) -> None:
    baselined(launch, fake, n=3)
    for number in (1, 2, 3):
        pushed(fake, number=number)
    marks, release, started = tmp_path / "marks", tmp_path / "release", tmp_path / "started"
    cfg = hook_config(
        tmp_path,
        f"echo $$ >> {started}; echo x >> {marks}; while [ ! -e {release} ]; do sleep 0.05; done",
        concurrency=1,
    )
    run = launch("--config", cfg, "poll")
    holder = pid_of(started, reaper)  # the one hook the single worker started
    t0 = time.monotonic()
    while len(events(store)) < 3:  # snapshots go on while the hook holds the only worker
        assert time.monotonic() - t0 < WAIT, "the other commits never happened"
        time.sleep(0.02)
    run.signal(signal.SIGKILL)
    code, _, _ = run.finish()
    assert code == -signal.SIGKILL
    assert events(store) == ["pushed"] * 3  # committed before any hook
    release.write_text("go", encoding="utf-8")  # the orphaned running hook may end now
    assert wait_gone(holder)
    code, doc = run_to_end(launch, "--config", cfg, "poll")
    assert code == 0 and doc["hooks"] == {"run": 0, "skipped": 0, "failed": 0}
    assert marks.read_text().count("x") == 1  # the two pending hooks never ran


def test_a_hook_never_starts_before_its_events_transaction_commits(
    launch: Launch,  # noqa: F811
    fake: FakeForgectl,  # noqa: F811
    reaper: Reaper,
    tmp_path: Path,
) -> None:
    baselined(launch, fake)
    pushed(fake)
    ran = tmp_path / "ran"
    cfg = hook_config(tmp_path, f"echo $$ > {ran}")
    run = launch("--config", cfg, "poll", **POLL_COMMIT)
    run.wait_ready()  # inside the commit that stores the event
    assert not ran.exists()
    run.release()
    out, err = run.finish()[1:]
    assert pid_of(ran, reaper) > 0 and '"run": 1' in out, (out, err)


def test_a_hook_that_starts_a_daemon_in_a_new_session_does_not_hold_the_poll_and_outlives_it(
    launch: Launch,  # noqa: F811
    fake: FakeForgectl,  # noqa: F811
    reaper: Reaper,
    tmp_path: Path,
) -> None:
    """The documented limit (Residual risks): only the hook's own process group is bounded."""
    baselined(launch, fake)
    pushed(fake)
    daemon = tmp_path / "daemon"
    cfg = hook_config(tmp_path, f"setsid sh -c 'echo $$ > {daemon}; exec sleep 300' & exit 0")
    code, doc = run_to_end(launch, "--config", cfg, "poll")
    assert code == 0 and doc["hooks"] == {"run": 1, "skipped": 0, "failed": 0}
    pid = pid_of(daemon, reaper)  # the Reaper kills it at teardown
    assert alive(pid)  # not reached by pr-watch: another session, another group


def test_a_poll_signalled_during_a_slow_hook_leaves_last_poll_finished_at_unset(
    launch: Launch,  # noqa: F811
    fake: FakeForgectl,  # noqa: F811
    store: Any,  # noqa: F811
    reaper: Reaper,
    tmp_path: Path,
) -> None:
    baselined(launch, fake)
    stamp = meta(store, "last_poll_finished_at")
    pushed(fake)
    started = tmp_path / "started"
    cfg = hook_config(tmp_path, f"echo $$ > {started}; exec sleep 300")
    run = launch("--config", cfg, "poll")
    pid_of(started, reaper)
    run.signal(signal.SIGTERM)
    check_clean_exit(run, signal.SIGTERM, timeout=60)
    assert meta(store, "last_poll_finished_at") == stamp  # the earlier poll's value, not moved


def test_a_normal_poll_with_hooks_still_sets_last_poll_finished_at(
    launch: Launch,  # noqa: F811
    fake: FakeForgectl,  # noqa: F811
    store: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    baselined(launch, fake)
    stamp = meta(store, "last_poll_finished_at")
    pushed(fake)
    cfg = hook_config(tmp_path, "exit 0")
    code, doc = run_to_end(launch, "--config", cfg, "poll")
    assert code == 0 and doc["hooks"]["run"] == 1
    assert meta(store, "last_poll_finished_at") != stamp
