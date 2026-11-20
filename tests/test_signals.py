"""Process-level signal tests (#26): real `pr-watch` processes, real signals, real kills.

What the commands promise (docs/interface.md, Signals and Locks and commit): SIGTERM, SIGINT and
SIGHUP end every command with 128+N, quietly, with nothing partial in state.db and no lease left
behind; a SIGKILL anywhere leaves a database that opens, passes its integrity check and holds
either all of a command's writes or none; no forgectl child outlives the command.

The stopping points come from tests/proc_launcher.py (a handshake file, never a sleep): the test
knows the process is inside the commit, or between a snapshot and its commit, before it signals.
Every process this file starts has its pid and process group recorded at launch; teardown kills
exactly that group, never by name. Complements tests/test_poll.py (the poll's own stages) and
tests/test_snapshots.py (concurrent pools).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pr_watch import watches
from pr_watch.state_dir import open_state_dir
from pr_watch.store import NewEvent, Store, open_store
from tests.forgectl_support import FakeForgectl, new_pr, sha
from tests.gateway_support import Reaper, _start_time
from tests.harness import assert_valid

HOST = "github.com"
OWNER = "human:a"
OTHER = "agent:b"
LAUNCHER = Path(__file__).parent / "proc_launcher.py"
WAIT = 60.0
STOPS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


# --- process helpers -----------------------------------------------------------------------------


def _state_of(pid: int) -> tuple[str, int] | None:
    """`(state letter, process group)` of a live process, None when it is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = text.rsplit(")", 1)[1].split()
    return fields[0], int(fields[2])


def alive(pid: int) -> bool:
    found = _state_of(pid)
    return found is not None and found[0] not in ("Z", "X")


def wait_gone(pid: int, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while alive(pid) and time.monotonic() < end:
        time.sleep(0.02)
    return not alive(pid)


def group_members(pgid: int) -> list[int]:
    """Live (not zombie) processes of group `pgid`, from /proc."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            state = _state_of(int(entry.name))
            if state is not None and state[1] == pgid and state[0] not in ("Z", "X"):
                found.append(int(entry.name))
    return found


class Run:
    """One pr-watch process in its own process group (so its own group is exactly its pgid)."""

    def __init__(
        self,
        directory: Path,
        index: int,
        argv: tuple[str, ...],
        env: dict[str, str],
        reaper: Reaper,
    ) -> None:
        self.ready = directory / f"ready-{index}"
        self.go = directory / f"go-{index}"
        self.rollback = directory / f"rollback-{index}"
        self.noted = directory / f"signalled-{index}"
        merged = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(sys.path),
            "PRW_READY": str(self.ready),
            "PRW_GO": str(self.go),
            **env,
        }
        # Opt-in hooks: pass PRW_ROLLBACK_FILE="1" or PRW_NOTE_SIGNAL="1" to get a handshake file.
        if merged.get("PRW_ROLLBACK_FILE") == "1":
            merged["PRW_ROLLBACK_FILE"] = str(self.rollback)
        if merged.get("PRW_NOTE_SIGNAL") == "1":
            merged["PRW_NOTE_SIGNAL"] = str(self.noted)
        self.proc = subprocess.Popen(
            [sys.executable, str(LAUNCHER), "--json", *argv],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=merged, text=True,
            start_new_session=True,
        )  # fmt: skip
        self.pid = self.proc.pid
        self.pgid = self.proc.pid  # a session leader leads its own group
        reaper.track(self.pid)

    def _wait_file(self, path: Path, what: str) -> None:
        end = time.monotonic() + WAIT
        while not path.exists():
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                raise AssertionError(
                    f"the process ended before {what}: {self.proc.returncode} {out!r} {err!r}"
                )
            if time.monotonic() > end:
                raise AssertionError(f"timed out waiting for {what}")
            time.sleep(0.02)

    def wait_ready(self) -> None:
        self._wait_file(self.ready, "its stopping point")

    def wait_rollback(self) -> None:
        self._wait_file(self.rollback, "its rollback")

    def wait_noted(self) -> None:
        self._wait_file(self.noted, "the signal handler")

    def release(self) -> None:
        self.go.write_text("go", encoding="utf-8")

    def signal(self, number: int) -> None:
        self.proc.send_signal(number)  # the exact process this test started

    def finish(self, timeout: float = 120.0) -> tuple[int, str, str]:
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
            raise AssertionError(
                f"the process did not end in {timeout}s: {out!r} {err!r}"
            ) from None
        return self.proc.returncode, out, err

    def kill_group(self) -> None:
        """Whatever of this run's own group is left. While the leader is unreaped its pid is the
        group id and `killpg` is exact; after that the id may be recycled, so only the members seen
        now are signalled, each re-checked by pid and start time."""
        members = [(pid, _start_time(pid)) for pid in group_members(self.pgid)]
        if self.proc.returncode is None:
            try:
                os.killpg(self.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            for pid, started in members:
                if started is not None and _start_time(pid) == started:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.proc.communicate()


Launch = Callable[..., Run]


@pytest.fixture
def launch(tmp_path: Path, reaper: Reaper, fake_forgectl: FakeForgectl) -> Iterator[Launch]:
    runs: list[Run] = []
    directory = tmp_path / "handshakes"
    directory.mkdir()

    def start(*argv: str, **env: str) -> Run:
        run = Run(directory, len(runs), argv, env, reaper)
        runs.append(run)
        return run

    try:
        yield start
    finally:
        for run in runs:
            run.kill_group()
        # The fake forgectl runs in its own session, so no group above reaches it: a failed test
        # must not leave a 60 s hang behind.
        try:
            for call in fake_forgectl.calls():
                for pid in (call.pid, *call.descendants):
                    reaper.track(pid)
        except OSError:  # the fake was never called
            pass


def check_clean_exit(run: Run, number: int, timeout: float = 120.0) -> tuple[str, str]:
    code, out, err = run.finish(timeout)
    assert code == 128 + number, (code, out, err)
    assert "Traceback" not in err and "Exception ignored" not in err
    return out, err


# --- state helpers -------------------------------------------------------------------------------


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


def tables(store: Store) -> list[str]:
    return [
        str(r[0])
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]


def dump(store: Store, skip: tuple[str, ...] = ()) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table (the whole database), for before/after equality."""
    return {
        name: [tuple(r) for r in store.conn.execute(f"SELECT * FROM {name} ORDER BY 1, 2")]  # noqa: S608
        for name in tables(store)
        if name not in skip
    }


def assert_sound(store: Store) -> None:
    assert store.conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []


# Enumeration and the host stages of an interrupted poll legitimately update `hosts` and the
# listing's columns of `prs`; `leases` is the poll's own lock. Everything else is compared whole.
ENUMERATION_OWNED = ("leases", "hosts", "prs")


def poll_state(store: Store) -> tuple[dict[str, list[tuple[Any, ...]]], list[tuple[Any, ...]]]:
    """The state a snapshot commit or a poll's end could change: every other table whole, plus the
    per-PR columns the snapshot commit owns."""
    return dump(store, skip=ENUMERATION_OWNED), pr_state(store)


def pr_state(store: Store) -> list[tuple[Any, ...]]:
    """What a snapshot commit owns, per PR (enumeration owns the rest of the row)."""
    return [
        tuple(r)
        for r in store.conn.execute(
            "SELECT pr_id, rev, digest, status, failure_count, snap_updated_at, last_polled_at "
            "FROM prs ORDER BY pr_id"
        )
    ]


def wait_for_new_call(fake: FakeForgectl, command: str, seen: int) -> Any:
    """The first call of `command` after the `seen` earlier ones (a readiness handshake)."""
    end = time.monotonic() + WAIT
    while len(fake.calls(command)) <= seen:
        assert time.monotonic() < end, f"forgectl {command} was never called"
        time.sleep(0.02)
    return fake.calls(command)[seen]


def count(store: Store, table: str) -> int:
    return int(store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608


def meta(store: Store, key: str) -> str | None:
    row = store.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def events(store: Store) -> list[str]:
    return [str(r[0]) for r in store.conn.execute("SELECT type FROM events ORDER BY seq")]


def log_records(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "log.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for record in records:
        assert_valid("log", record)
    return records


def run_to_end(launch: Launch, *argv: str, **env: str) -> tuple[int, dict[str, Any]]:
    code, out, err = launch(*argv, **env).finish()
    assert out.strip(), (code, err)
    return code, json.loads(out)


def pr(n: int, **ch: Any) -> dict[str, Any]:
    ch.setdefault("updated_at", "2026-10-02T10:30:00Z")
    return new_pr(HOST, "example-org/widgets", n, **ch)


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def baselined(launch: Launch, fake: FakeForgectl, n: int = 1) -> list[dict[str, Any]]:
    """`n` PRs under a `--mine --open` query watch (owner OWNER), baselined by one real poll."""
    snaps = [pr(i) for i in range(1, n + 1)]
    for snap in snaps:
        fake.add(snap)
    code, doc = run_to_end(launch, "watch", "add", "--mine", "--open", "--owner", OWNER)
    assert code == 0, doc
    code, doc = run_to_end(launch, "poll")
    assert code == 0 and doc["prs"]["snapshotted"] == n, doc
    return snaps


# --- kill and signal during the commit of every writing command ----------------------------------


def seed_expired(store: Store) -> None:
    """Rows and events a prune has work for: an expired terminal PR and 3 old events."""
    old = "2026-04-01T00:00:00.000000Z"
    with store.transaction() as c:
        c.execute(
            "INSERT INTO prs (pr_id, pr, repo, host, status, terminal_at, terminal_kind, digest) "
            "VALUES ('P1', 'h/o/r#1', 'h/o/r', 'h', 'terminal', ?, 'merged', '{}')",
            (old,),
        )
        wid, _ = watches.ensure_watch(c, "pr", "P1", datetime(2026, 4, 1, tzinfo=UTC))
        watches.add_source(c, "P1", wid)
        watches.add_owner(c, wid, OWNER)
        for _ in range(3):
            ev = store.append_event(
                NewEvent(
                    "pushed", "P1", "h/o/r#1",
                    {"repo": "h/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []},
                    [],
                )
            )  # fmt: skip
            c.execute("UPDATE events SET detected_at = ? WHERE seq = ?", (old, ev.seq))


class Case:
    """One writing command: its setup, argv, the probe that selects its commit, its proof."""

    def __init__(
        self,
        name: str,
        argv: tuple[str, ...],
        sql: str | None,
        setup: Callable[..., None],
        effect: Callable[[Store], bool],
        *,
        skip: int = 0,
    ) -> None:
        self.name, self.argv, self.sql, self.setup, self.effect, self.skip = (
            name, argv, sql, setup, effect, skip,
        )  # fmt: skip

    def __repr__(self) -> str:
        return self.name


def _setup_none(launch: Launch, fake: FakeForgectl, store: Store) -> None:
    fake.add(pr(1))


def _setup_watched(launch: Launch, fake: FakeForgectl, store: Store) -> None:
    fake.add(pr(1))
    for owner in (OWNER, OTHER):
        code, doc = run_to_end(
            launch, "watch", "add", HOST + "/example-org/widgets#1", "--owner", owner
        )
        assert code == 0, doc


def _setup_labeled(launch: Launch, fake: FakeForgectl, store: Store) -> None:
    _setup_watched(launch, fake, store)
    code, doc = run_to_end(
        launch, "label", "add", f"{HOST}/example-org/widgets#1", "--owner", OWNER, "k=v"
    )
    assert code == 0, doc


def _setup_prune(launch: Launch, fake: FakeForgectl, store: Store) -> None:
    seed_expired(store)


CASES = [
    Case(
        "watch-add", ("watch", "add", f"{HOST}/example-org/widgets#1", "--owner", OWNER),
        "SELECT 1 FROM watches", _setup_none, lambda s: count(s, "watches") == 1,
    ),
    Case(
        "watch-remove", ("watch", "remove", f"{HOST}/example-org/widgets#1", "--owner", OTHER),
        None, _setup_watched, lambda s: count(s, "owners") == 1,
    ),
    Case(
        "label-add", ("label", "add", f"{HOST}/example-org/widgets#1", "--owner", OWNER, "k=v"),
        None, _setup_watched, lambda s: count(s, "labels") == 1,
    ),
    Case(
        "prune", ("prune",), None, _setup_prune,
        lambda s: count(s, "events") == 0 and count(s, "prs") == 0,
    ),
    Case(
        "watch-add-query", ("watch", "add", "--mine", "--open", "--owner", OWNER),
        "SELECT 1 FROM watches", _setup_none, lambda s: count(s, "watches") == 1,
    ),
    Case(
        "label-remove",
        ("label", "remove", f"{HOST}/example-org/widgets#1", "--owner", OWNER, "k=v"),
        None, _setup_labeled, lambda s: count(s, "labels") == 0,
    ),
]  # fmt: skip


def paused(case: Case) -> dict[str, str]:
    env = {"PRW_PAUSE_COMMIT": "1", "PRW_PAUSE_SKIP": str(case.skip)}
    if case.sql:
        env["PRW_PAUSE_SQL"] = case.sql
    return env


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_sigkill_inside_the_commit_leaves_the_database_as_it_was_and_the_command_reruns(
    case: Case, launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    case.setup(launch, fake, store)
    before = dump(store)
    run = launch(*case.argv, **paused(case))
    run.wait_ready()
    run.signal(signal.SIGKILL)
    code, out, _ = run.finish()
    assert code == -signal.SIGKILL and out == ""
    assert dump(store) == before  # nothing of the open transaction was published
    assert_sound(store)
    assert not case.effect(store)
    code, doc = run_to_end(launch, *case.argv)  # a fresh process recovers and does the work
    assert code == 0, doc
    assert case.effect(store)
    assert_sound(store)


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_sigterm_inside_the_commit_rolls_back_and_exits_143_quietly(
    case: Case, launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    case.setup(launch, fake, store)
    before = dump(store)
    run = launch(*case.argv, **paused(case))
    run.wait_ready()
    run.signal(signal.SIGTERM)
    out, err = check_clean_exit(run, signal.SIGTERM)
    assert out == "" and err == ""
    assert dump(store) == before and not case.effect(store)
    assert_sound(store)
    assert group_members(run.pgid) == []


@pytest.mark.parametrize("number", STOPS)
def test_every_stop_signal_inside_a_watch_add_commit_exits_128_plus_it(
    number: signal.Signals, launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    case = CASES[0]
    case.setup(launch, fake, store)
    before = dump(store)
    run = launch(*case.argv, **paused(case))
    run.wait_ready()
    run.signal(number)
    check_clean_exit(run, number)
    assert dump(store) == before


# --- poll: the snapshot commit, a second signal, a killed or signalled forgectl -------------------

POLL_COMMIT = {"PRW_PAUSE_COMMIT": "1", "PRW_PAUSE_SQL": "SELECT 1 FROM events"}
# Between the snapshot call's answer and the commit, in the worker, before the pool's stop check:
POLL_BEFORE_DRAIN = {"PRW_PAUSE_AT": "pr_watch.snapshots:plan_snapshot"}
# Inside the pool's commit step, after the stop check, just before the compare-and-set:
POLL_PRE_COMMIT = {"PRW_PAUSE_AT": "pr_watch.snapshots:commit_snapshot"}


def pushed(fake: FakeForgectl, n: int = 2, number: int = 1) -> None:
    """A new head on PR `number`; the listing's `updated_at` moves with it, so a poll sees it."""
    fake.update(pr(number)["pr"], head={"sha": sha(n)}, updated_at=f"2026-10-0{n + 1}T00:00:00Z")


def test_sigkill_inside_a_poll_commit_leaves_no_partial_pr_and_the_next_poll_emits_one_event(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    baselined(launch, fake)
    pushed(fake)
    before = poll_state(store)
    run = launch("poll", **POLL_COMMIT)
    run.wait_ready()
    run.signal(signal.SIGKILL)
    code, _, _ = run.finish()
    assert code == -signal.SIGKILL
    assert poll_state(store) == before  # every other table, explicitly
    assert_sound(store)
    assert count(store, "leases") == 1  # the dead holder's lease is left; the next poll takes it
    code, doc = run_to_end(launch, "poll")
    assert code == 0 and doc["prs"]["snapshotted"] == 1, doc
    assert events(store) == ["pushed"] and count(store, "leases") == 0
    code, doc = run_to_end(launch, "poll")  # and nothing is emitted twice
    assert code == 0 and events(store) == ["pushed"]
    assert_valid("cli/poll", doc)


@pytest.mark.parametrize("number", STOPS)
def test_a_stop_signal_inside_a_poll_commit_lets_that_commit_finish_whole_then_exits(
    number: signal.Signals, launch: Launch, fake: FakeForgectl, store: Store, state_dir: Path
) -> None:
    baselined(launch, fake)
    pushed(fake)
    rev = pr_state(store)[0][1]
    finished = meta(store, "last_poll_finished_at")
    run = launch("poll", PRW_NOTE_SIGNAL="1", **POLL_COMMIT)
    run.wait_ready()
    run.signal(number)  # the poll's handler only sets a flag: the open transaction is not torn
    run.wait_noted()  # the handler has run before the commit is let go
    run.release()
    out, _ = check_clean_exit(run, number)
    assert out == ""
    assert events(store) == ["pushed"]  # all of it: the event, the digest and the revision
    assert pr_state(store)[0][1] == rev + 1  # one revision, with its event and digest
    assert count(store, "leases") == 0
    assert meta(store, "last_poll_finished_at") == finished  # an interrupted poll stamps nothing
    ends = [r for r in log_records(state_dir) if r["event"] == "poll_end"]
    assert ends[-1]["interrupted"] is True
    code, doc = run_to_end(launch, "poll")  # no duplicate on the next poll
    assert code == 0 and doc["prs"]["snapshotted"] == 0 and events(store) == ["pushed"]


def test_a_second_signal_during_poll_shutdown_still_ends_it_once_with_the_first_code(
    launch: Launch, fake: FakeForgectl, store: Store, state_dir: Path
) -> None:
    baselined(launch, fake)
    pushed(fake)
    fake.hang("snapshot", seconds=60, ignore_term=True)  # ignores the stop until pr-watch's KILL
    before = poll_state(store)
    seen = len(fake.calls("snapshot"))
    run = launch("poll", PRW_NOTE_SIGNAL="1")
    child = wait_for_new_call(fake, "snapshot", seen).pid
    fake.wait_ready()  # its SIGTERM-ignore is installed: the signal below really is ignored
    run.signal(signal.SIGTERM)
    run.wait_noted()  # the handler has run: the shutdown is under way
    run.signal(signal.SIGINT)
    code, out, err = run.finish(timeout=30)  # not the fake's 60 s hang
    assert code == 128 + signal.SIGTERM and out == "" and "Traceback" not in err
    assert wait_gone(child)
    assert poll_state(store) == before and count(store, "leases") == 0
    ends = [r for r in log_records(state_dir) if r["event"] == "poll_end"]
    assert len(ends) == 2 and ends[-1]["interrupted"] is True  # the baseline poll and this one


@pytest.mark.parametrize("number", STOPS)
def test_a_stop_signal_after_the_snapshot_and_before_its_commit_commits_nothing(
    number: signal.Signals, launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    baselined(launch, fake)
    pushed(fake)
    before = poll_state(store)
    run = launch("poll", PRW_NOTE_SIGNAL="1", **POLL_BEFORE_DRAIN)
    run.wait_ready()
    run.signal(number)
    run.wait_noted()
    run.release()
    check_clean_exit(run, number)
    assert poll_state(store) == before and events(store) == []
    assert count(store, "leases") == 0
    code, doc = run_to_end(launch, "poll")
    assert code == 0 and doc["prs"]["snapshotted"] == 1 and events(store) == ["pushed"]


def test_a_signal_during_whoami_exits_with_it_and_leaves_no_child(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    fake.add(pr(1))
    code, doc = run_to_end(launch, "watch", "add", "--mine", "--open", "--owner", OWNER)
    assert code == 0, doc
    fake.slow("whoami", 60.0)
    seen = len(fake.calls("whoami"))
    run = launch("poll")
    child = wait_for_new_call(fake, "whoami", seen).pid
    run.signal(signal.SIGTERM)
    check_clean_exit(run, signal.SIGTERM)
    assert wait_gone(child) and count(store, "leases") == 0


def test_a_forgectl_exiting_143_unsignalled_is_an_interrupted_pr_with_no_counter(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    baselined(launch, fake, 2)
    pushed(fake)
    pushed(fake, 3, number=2)
    fake.raw("snapshot", exit=143, times=1)  # the first snapshot call only
    before = pr_state(store)
    code, doc = run_to_end(launch, "poll")
    assert code == 0, doc  # pr-watch was not signalled: the poll goes on and ends normally
    assert_valid("cli/poll", doc)
    assert doc["prs"]["snapshotted"] == 1
    assert store.conn.execute("SELECT sum(failure_count) FROM prs").fetchone()[0] == 0
    assert events(store) == ["pushed"]
    after = pr_state(store)
    kept = [
        b[:4] == a[:4] for b, a in zip(before, after, strict=True)
    ]  # pr_id, rev, digest, status
    assert sorted(kept) == [
        False,
        True,
    ]  # exactly one PR advanced; the interrupted one is as it was
    code, doc = run_to_end(launch, "poll")  # the interrupted PR is retried next cycle
    assert code == 0 and doc["prs"]["snapshotted"] == 1 and events(store) == ["pushed", "pushed"]


def test_no_forgectl_child_or_descendant_outlives_a_signalled_poll(
    launch: Launch, fake: FakeForgectl
) -> None:
    baselined(launch, fake)
    pushed(fake)
    fake.hang("snapshot", seconds=60, ignore_term=True, descendant=True)
    seen = len(fake.calls("snapshot"))
    run = launch("poll")
    wait_for_new_call(fake, "snapshot", seen)
    fake.wait_ready()  # the child ignores SIGTERM and its descendant is logged
    call = fake.calls("snapshot")[seen]
    assert call.descendants, "the fake never started its descendant"
    run.signal(signal.SIGTERM)
    check_clean_exit(run, signal.SIGTERM, timeout=30)  # not the fake's 60 s hang
    for pid in [call.pid, *call.descendants]:
        assert wait_gone(pid), f"pid {pid} outlived the poll"


# --- a signal inside a lease holder, takeover and a watch removed mid-snapshot --------------------


def test_a_live_holder_whose_lease_expired_is_taken_over_and_its_late_commit_is_discarded(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    baselined(launch, fake)
    pushed(fake)
    first = launch("poll", **POLL_PRE_COMMIT)
    first.wait_ready()  # holds the lease, snapshot in hand, alive
    with store.transaction() as c:
        assert (
            c.execute("UPDATE leases SET expires_at = '2000-01-01T00:00:00.000000Z'").rowcount == 1
        )
    code, doc = run_to_end(launch, "poll")  # takes the expired lease from its live holder
    assert code == 0 and doc["prs"]["snapshotted"] == 1, doc
    assert events(store) == ["pushed"]
    first.release()
    code, out, err = first.finish()
    assert code == 0, err
    late = json.loads(out)
    assert_valid("cli/poll", late)
    assert late["prs"]["skipped_conflict"] == 1 and late["prs"]["snapshotted"] == 0
    assert events(store) == ["pushed"] and count(store, "leases") == 0  # no duplicate


def test_a_watch_removed_while_its_snapshot_is_in_flight_is_not_resurrected(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    fake.add(pr(1))
    ref = f"{HOST}/example-org/widgets#1"
    code, doc = run_to_end(launch, "watch", "add", ref, "--owner", OWNER)
    assert code == 0, doc
    pushed(fake)
    run = launch("poll", **POLL_PRE_COMMIT)
    run.wait_ready()
    code, doc = run_to_end(launch, "watch", "remove", ref, "--owner", OWNER)
    assert code == 0 and doc["watch_ended"] is True, doc
    assert count(store, "prs") == 0
    run.release()
    code, out, err = run.finish()
    assert code == 0, err
    assert json.loads(out)["prs"]["skipped_conflict"] == 1
    assert count(store, "prs") == 0 and events(store) == [] and count(store, "leases") == 0
    assert_sound(store)


# --- prune: killed between its transactions ------------------------------------------------------


def test_prune_killed_in_an_event_batch_keeps_the_expiry_stamps_nothing_and_reruns(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    seed_expired(store)
    # The expiry transaction (qualifying #1) commits; the kill lands in the event batch (#2).
    run = launch("prune", PRW_PAUSE_COMMIT="1", PRW_PAUSE_SKIP="1")
    run.wait_ready()
    run.signal(signal.SIGKILL)
    run.finish()
    assert count(store, "prs") == 0  # the first transaction was whole
    assert count(store, "events") == 3 and meta(store, "last_prune_at") is None
    assert_sound(store)
    code, doc = run_to_end(launch, "prune")
    assert code == 0 and doc["events_deleted"] == 3
    assert meta(store, "last_prune_at") is not None and count(store, "events") == 0


# --- a second signal while a non-poll command unwinds --------------------------------------------


def test_a_second_signal_while_a_command_rolls_back_still_exits_143_quietly(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    case = CASES[2]
    case.setup(launch, fake, store)
    before = dump(store)
    run = launch(*case.argv, PRW_ROLLBACK_FILE="1", **paused(case))
    run.wait_ready()
    run.signal(signal.SIGTERM)
    run.wait_rollback()  # the first signal is being unwound, the rollback already done
    run.signal(signal.SIGINT)
    time.sleep(0.2)  # the second signal is delivered while the stall is still on
    run.release()
    out, err = check_clean_exit(run, signal.SIGTERM)
    assert out == "" and err == ""
    assert dump(store) == before and not case.effect(store)
    assert_sound(store)


# --- read-only commands and a closed stdout -------------------------------------------------------


@pytest.mark.parametrize("number", STOPS)
def test_a_signal_to_a_read_only_command_exits_128_plus_it_and_changes_nothing(
    number: signal.Signals, launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    baselined(launch, fake)
    before = dump(store)
    run = launch("list", PRW_PAUSE_AT="pr_watch.listing:list_command")
    run.wait_ready()
    run.signal(number)
    out, err = check_clean_exit(run, number)
    assert out == "" and err == ""
    assert dump(store) == before


def seed_many_prs(store: Store, count_: int) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    with store.transaction() as c:
        for i in range(count_):
            pr_id = f"P{i}"
            c.execute(
                "INSERT INTO prs (pr_id, pr, repo, host, status, digest) "
                "VALUES (?, ?, 'h/o/r', 'h', 'active', '{}')",
                (pr_id, f"h/o/r#{i + 1}"),
            )
            wid, _ = watches.ensure_watch(c, "pr", pr_id, now)
            watches.add_source(c, pr_id, wid)
            watches.add_owner(c, wid, OWNER)


def test_list_json_into_a_pipe_that_closes_early_ends_quietly_without_a_traceback(
    launch: Launch, store: Store
) -> None:
    seed_many_prs(store, 600)  # far more than a pipe holds, so the write must hit the closed end
    before = dump(store)
    run = launch("list")
    assert run.proc.stdout is not None
    assert run.proc.stdout.read(1) == "{"  # `| head -c1`
    run.proc.stdout.close()
    err = run.proc.stderr.read() if run.proc.stderr else ""
    assert run.proc.wait(timeout=60) == 1
    assert "Traceback" not in err and "Exception ignored" not in err
    assert err.count("cannot write to stdout") == 1
    assert dump(store) == before


# --- the first signal wins, everywhere ------------------------------------------------------------


def test_a_second_signal_during_a_non_poll_commands_forgectl_shutdown_does_not_change_the_exit(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    fake.add(pr(1))
    fake.hang("snapshot", seconds=60, ignore_term=True, descendant=True)
    seen = len(fake.calls("snapshot"))
    run = launch("watch", "add", f"{HOST}/example-org/widgets#1", "--owner", OWNER)
    wait_for_new_call(fake, "snapshot", seen)
    fake.wait_ready()  # the child ignores SIGTERM: its shutdown takes the 2 s term grace
    call = fake.calls("snapshot")[seen]
    assert call.descendants
    run.signal(signal.SIGTERM)
    time.sleep(0.3)  # the unwind is in the child's grace (2 s); the handler ran long ago
    run.signal(signal.SIGINT)
    check_clean_exit(run, signal.SIGTERM, timeout=30)  # 143, not 130, and not a leaked child
    for pid in [call.pid, *call.descendants]:
        assert wait_gone(pid), f"pid {pid} outlived the command"
    assert count(store, "watches") == 0


def test_a_signal_just_before_finish_poll_writes_nothing_and_exits_with_it(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    baselined(launch, fake)
    finished = meta(store, "last_poll_finished_at")
    assert finished is not None
    before = dump(store)
    run = launch("poll", PRW_NOTE_SIGNAL="1", PRW_PAUSE_AT="pr_watch.pacing:finish_poll")
    run.wait_ready()  # the stop flag was read (clear) by the caller; the write has not begun
    run.signal(signal.SIGTERM)
    run.wait_noted()
    run.release()
    check_clean_exit(run, signal.SIGTERM)
    assert meta(store, "last_poll_finished_at") == finished
    assert dump(store, skip=ENUMERATION_OWNED) == {
        k: v for k, v in before.items() if k not in ENUMERATION_OWNED
    }


# --- more commands, one signal each ---------------------------------------------------------------


def test_a_signal_to_prune_dry_run_exits_with_it_and_changes_nothing(
    launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    seed_expired(store)
    before = dump(store)
    run = launch("prune", "--dry-run", PRW_PAUSE_AT="pr_watch.prune:plan")
    run.wait_ready()
    run.signal(signal.SIGTERM)
    out, err = check_clean_exit(run, signal.SIGTERM)
    assert out == "" and err == "" and dump(store) == before


@pytest.mark.parametrize("scope", ["pr", "repo"])
def test_a_signal_to_a_restricted_poll_commits_nothing_and_exits_with_it(
    scope: str, launch: Launch, fake: FakeForgectl, store: Store
) -> None:
    baselined(launch, fake)
    pushed(fake)
    before = poll_state(store)
    target = (
        ("--pr", f"{HOST}/example-org/widgets#1")
        if scope == "pr"
        else ("--repo", f"{HOST}/example-org/widgets")
    )
    run = launch("poll", *target, PRW_NOTE_SIGNAL="1", **POLL_BEFORE_DRAIN)
    run.wait_ready()
    run.signal(signal.SIGTERM)
    run.wait_noted()
    run.release()
    check_clean_exit(run, signal.SIGTERM)
    assert poll_state(store) == before and count(store, "leases") == 0


# --- the startup window, closed stdout and closed stderr ------------------------------------------


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}


@pytest.mark.parametrize("number", STOPS)
def test_a_stop_signal_before_the_cli_is_loaded_exits_128_plus_it_without_a_traceback(
    number: signal.Signals,
) -> None:
    code = (
        "from pr_watch._entry import install_early_handlers\n"
        "install_early_handlers()\n"
        "import os, signal, time\n"
        f"os.kill(os.getpid(), {int(number)})\n"
        "time.sleep(30)\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=_env(), timeout=60
    )
    assert done.returncode == 128 + number and done.stderr == ""


def test_the_module_entry_point_runs_the_cli() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pr_watch", "--version"],
        capture_output=True, text=True, env=_env(), timeout=60,
    )  # fmt: skip
    assert done.returncode == 0 and done.stdout.strip()


def test_a_closed_stdout_is_exit_1_with_one_stderr_line_even_after_the_command_committed(
    launch: Launch, fake: FakeForgectl, store: Store, reaper: Reaper
) -> None:
    fake.add(pr(1))
    proc = subprocess.run(  # noqa: S603
        [
            "sh", "-c", 'exec "$@" >&-', "sh", sys.executable, str(LAUNCHER), "--json",
            "watch", "add", f"{HOST}/example-org/widgets#1", "--owner", OWNER,
        ],
        stderr=subprocess.PIPE, text=True, env=_env(), timeout=60,
    )  # fmt: skip
    assert proc.returncode == 1, proc.stderr
    assert proc.stderr.count("cannot write to stdout") == 1 and "Traceback" not in proc.stderr
    assert count(store, "watches") == 1  # the write itself had committed


def test_a_closed_stderr_does_not_stop_a_usage_error_from_reporting_on_stdout() -> None:
    proc = subprocess.run(  # noqa: S603
        [
            "sh", "-c", 'exec "$@" 2>&-', "sh", sys.executable, str(LAUNCHER), "--json",
            "watch", "add",
        ],
        stdout=subprocess.PIPE, text=True, env=_env(), timeout=60,
    )  # fmt: skip
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error"]
