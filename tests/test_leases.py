"""Leases: per-PR locks decided by holder identity (pid, start time, boot id) and expiry."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pr_watch.leases import (
    Leases,
    Liveness,
    ProcessIdentity,
    ProcFs,
)
from pr_watch.state_dir import StateDir, open_state_dir
from pr_watch.store import Store, format_ts, open_store

PR_ID = "PR_abc123"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
TTL = timedelta(minutes=6)


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDir]:
    with open_state_dir(tmp_path / "pr-watch") as s:
        yield s


@pytest.fixture
def store(state: StateDir) -> Iterator[Store]:
    with open_store(state) as s:
        yield s


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def stat_line(pid: int, comm: str, start: int, state: str = "S") -> str:
    # fields 3..21 are 19 values (state, ppid, ..., itrealvalue), then starttime is field 22
    middle = " ".join(["0"] * 18)
    return f"{pid} ({comm}) {state} {middle} {start} 0 0\n"


def fake_proc(
    root: Path, boot: str | None = "boot-a", procs: dict[int, tuple[str, int, str]] | None = None
) -> ProcFs:
    root.mkdir(parents=True, exist_ok=True)
    if boot is not None:
        (root / "sys/kernel/random").mkdir(parents=True)
        (root / "sys/kernel/random/boot_id").write_text(boot + "\n")
    for pid, (comm, start, st) in (procs or {}).items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "stat").write_text(stat_line(pid, comm, start, st))
    return ProcFs(root)


def insert_row(store: Store, ident: ProcessIdentity, expires: datetime, pr_id: str = PR_ID) -> None:
    store.conn.execute(
        "INSERT OR REPLACE INTO leases (pr_id, holder_pid, holder_start, boot_id, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pr_id, ident.pid, ident.start, ident.boot_id, format_ts(expires)),
    )


def row(store: Store, pr_id: str = PR_ID) -> tuple[int, str, str, str] | None:
    r = store.conn.execute(
        "SELECT holder_pid, holder_start, boot_id, expires_at FROM leases WHERE pr_id = ?", (pr_id,)
    ).fetchone()
    return None if r is None else (r[0], r[1], r[2], r[3])


# --- the process probe -----------------------------------------------------------------------


def test_probe_reads_identity_and_liveness_from_proc(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", procs={10: ("bash", 555, "S")})
    assert proc.boot_id() == "boot-a"
    assert proc.liveness(10, "555") is Liveness.ALIVE
    assert proc.liveness(10, "556") is Liveness.DEAD  # reused pid: same pid, other start time
    assert proc.liveness(11, "555") is Liveness.DEAD  # no such process


def test_probe_survives_hostile_command_names(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", procs={10: ("a) S 1 (b", 777, "S")})
    assert proc.liveness(10, "777") is Liveness.ALIVE


def test_a_zombie_holder_is_dead(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", procs={10: ("x", 5, "Z")})
    assert proc.liveness(10, "5") is Liveness.DEAD


def test_unparseable_stat_is_unknown_not_dead(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc", procs={10: ("x", 5, "S")})
    (tmp_path / "proc/10/stat").write_text("garbage")
    assert proc.liveness(10, "5") is Liveness.UNKNOWN


def test_a_platform_without_proc_is_unknown(tmp_path: Path) -> None:
    proc = ProcFs(tmp_path / "missing")
    assert proc.boot_id() is None
    assert proc.liveness(10, "5") is Liveness.UNKNOWN
    me = proc.identity()
    assert me.pid == os.getpid() and me.start == "unknown" and me.boot_id == "unknown"


def test_invalid_pids_are_dead(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path / "proc")
    assert proc.liveness(0, "1") is Liveness.DEAD
    assert proc.liveness(-5, "1") is Liveness.DEAD


def test_real_proc_identity_matches_this_process() -> None:
    proc = ProcFs()
    me = proc.identity()
    assert me.pid == os.getpid()
    assert proc.liveness(me.pid, me.start) is Liveness.ALIVE
    assert me.boot_id == (Path("/proc/sys/kernel/random/boot_id").read_text().strip())


# --- acquire, renew, release -----------------------------------------------------------------


def make(store: Store, tmp_path: Path, clock: Clock, **procs: object) -> Leases:
    proc = fake_proc(tmp_path / "proc", procs={1: ("me", 100, "S"), 2: ("other", 200, "S")})
    return Leases(store, TTL, clock=clock, proc=proc, identity=ProcessIdentity(1, "100", "boot-a"))


def test_acquire_a_free_pr_records_this_process_and_expiry(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    lease = leases.acquire(PR_ID)
    assert lease is not None and lease.pr_id == PR_ID
    assert lease.expires_at == NOW + TTL
    assert row(store) == (1, "100", "boot-a", format_ts(NOW + TTL))


def test_a_live_holder_is_never_taken_over_before_expiry(store: Store, tmp_path: Path) -> None:
    clock = Clock()
    leases = make(store, tmp_path, clock)
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + timedelta(seconds=1))
    assert leases.acquire(PR_ID) is None
    assert row(store) is not None and row(store)[0] == 2  # type: ignore[index]


def test_a_live_holder_is_taken_over_once_expired(store: Store, tmp_path: Path) -> None:
    clock = Clock()
    leases = make(store, tmp_path, clock)
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW)  # expires exactly now
    lease = leases.acquire(PR_ID)
    assert lease is not None
    assert row(store) is not None and row(store)[0] == 1  # type: ignore[index]


def test_takeover_of_a_dead_holder(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    insert_row(store, ProcessIdentity(99, "5", "boot-a"), NOW + TTL)
    assert leases.acquire(PR_ID) is not None


def test_takeover_when_the_pid_was_reused(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    insert_row(store, ProcessIdentity(2, "201", "boot-a"), NOW + TTL)  # pid 2 lives, start differs
    assert leases.acquire(PR_ID) is not None


def test_takeover_of_a_holder_from_a_previous_boot(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    # same pid and start time as a live process, but another boot
    insert_row(store, ProcessIdentity(2, "200", "boot-old"), NOW + TTL)
    assert leases.acquire(PR_ID) is not None


def test_unknown_liveness_is_alive_until_expiry(store: Store, tmp_path: Path) -> None:
    clock = Clock()
    leases = Leases(
        store,
        TTL,
        clock=clock,
        proc=ProcFs(tmp_path / "none"),
        identity=ProcessIdentity(1, "unknown", "unknown"),
    )
    insert_row(store, ProcessIdentity(7, "unknown", "unknown"), NOW + TTL)
    assert leases.acquire(PR_ID) is None
    clock.now = NOW + TTL
    assert leases.acquire(PR_ID) is not None


def test_a_clock_stepped_back_does_not_pin_a_lease(store: Store, tmp_path: Path) -> None:
    clock = Clock()
    leases = make(store, tmp_path, clock)
    # the holder wrote its expiry when the clock read a day later; its pid and start still verify
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + timedelta(days=1))
    assert leases.acquire(PR_ID) is not None


def test_a_smaller_ttl_contender_does_not_take_a_live_holders_lease(
    store: Store, tmp_path: Path
) -> None:
    proc = fake_proc(tmp_path / "proc", procs={1: ("me", 100, "S"), 2: ("other", 200, "S")})
    short = Leases(
        store,
        timedelta(minutes=1),
        clock=Clock(),
        proc=proc,
        identity=ProcessIdentity(1, "100", "boot-a"),
    )
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + TTL)  # written with a 6m ttl
    assert short.acquire(PR_ID) is None


@pytest.mark.parametrize("empty", ["start", "boot"])
def test_an_empty_identity_field_with_a_live_pid_is_takeable(
    store: Store, tmp_path: Path, empty: str
) -> None:
    leases = make(store, tmp_path, Clock())
    ident = ProcessIdentity(
        2, "" if empty == "start" else "200", "" if empty == "boot" else "boot-a"
    )
    insert_row(store, ident, NOW + TTL)
    assert leases.acquire(PR_ID) is not None


def test_a_garbage_expiry_is_expired(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + TTL)
    store.conn.execute("UPDATE leases SET expires_at = 'soon'")
    assert leases.acquire(PR_ID) is not None


def test_acquiring_a_lease_this_process_holds_is_refused(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    assert leases.acquire(PR_ID) is not None
    assert leases.acquire(PR_ID) is None


def test_renew_extends_the_expiry_while_held(store: Store, tmp_path: Path) -> None:
    clock = Clock()
    leases = make(store, tmp_path, clock)
    lease = leases.acquire(PR_ID)
    assert lease is not None
    clock.now = NOW + timedelta(minutes=5)
    renewed = leases.renew(lease)
    assert renewed is not None and renewed.expires_at == clock.now + TTL
    assert row(store) is not None and row(store)[3] == format_ts(clock.now + TTL)  # type: ignore[index]


def test_renew_reports_a_lost_lease_and_writes_nothing(store: Store, tmp_path: Path) -> None:
    clock = Clock()
    leases = make(store, tmp_path, clock)
    lease = leases.acquire(PR_ID)
    assert lease is not None
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + TTL)  # taken over
    assert leases.renew(lease) is None
    assert row(store) is not None and row(store)[0] == 2  # type: ignore[index]


def test_release_deletes_only_our_own_lease(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    lease = leases.acquire(PR_ID)
    assert lease is not None
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + TTL)
    assert leases.release(lease) is False
    assert row(store) is not None
    insert_row(store, ProcessIdentity(1, "100", "boot-a"), NOW + TTL)
    assert leases.release(lease) is True
    assert row(store) is None
    assert leases.release(lease) is False  # idempotent


def test_leases_are_per_pr(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    assert leases.acquire("PR_a") is not None
    assert leases.acquire("PR_b") is not None


def test_a_bad_ttl_is_refused(store: Store) -> None:
    with pytest.raises(ValueError):
        Leases(store, timedelta(0))


# --- processes -------------------------------------------------------------------------------

RACER = """
    import sys, time
    from datetime import timedelta
    from pathlib import Path
    from pr_watch.leases import Leases
    from pr_watch.state_dir import open_state_dir
    from pr_watch.store import open_store
    st = open_state_dir(Path(sys.argv[1]))
    s = open_store(st, busy_timeout_ms=20000)
    lease = Leases(s, timedelta(minutes=5)).acquire("PR_race")
    print("won" if lease else "lost", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def spawn(code: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_processes_racing_for_a_lease_exactly_one_wins(state: StateDir) -> None:
    with open_store(state):
        pass
    procs = [spawn(RACER, str(state.path), "120") for _ in range(3)]  # all stay alive
    try:
        results = []
        for p in procs:
            assert p.stdout is not None
            results.append(p.stdout.readline().strip())
    finally:
        for p in procs:  # the exact children we started
            if p.poll() is None:
                p.kill()
                p.wait(timeout=30)
    assert sorted(results) == ["lost", "lost", "won"]


def test_a_killed_holder_is_taken_over_by_pid_and_start_time(state: StateDir) -> None:
    with open_store(state) as s:
        holder = spawn(RACER, str(state.path), "60")
        try:
            assert holder.stdout is not None and holder.stdout.readline().strip() == "won"
            assert Leases(s, TTL).acquire("PR_race") is None  # alive: not taken over
            holder.kill()  # the exact child we started
            holder.wait(timeout=30)
            lease = Leases(s, TTL).acquire("PR_race")
            assert lease is not None and lease.holder.pid == os.getpid()
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=30)


TAKEOVER_CHILD = """
    import sys, time
    from datetime import timedelta
    from pathlib import Path
    from pr_watch.commit import commit_snapshot
    from pr_watch.leases import Leases
    from pr_watch.state_dir import open_state_dir
    from pr_watch.store import NewEvent, open_store
    s = open_store(open_state_dir(Path(sys.argv[1])), busy_timeout_ms=20000)
    assert Leases(s, timedelta(minutes=5)).acquire("PR_abc123") is not None
    rev = s.conn.execute("SELECT rev FROM prs WHERE pr_id = 'PR_abc123'").fetchone()[0]
    print("held", flush=True)
    sys.stdin.readline()  # the parent has taken over and committed
    r = commit_snapshot(
        s, pr_id="PR_abc123", expected_rev=rev, snapshot_pr_id="PR_abc123",
        fields={"digest": "child"},
        prepare=lambda conn: [NewEvent("closed", "PR_abc123", "github.com/o/r#1",
            {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []})],
    )
    print("committed" if r.committed else r.reason.value, flush=True)
"""


def test_takeover_of_a_live_holder_yields_no_duplicate_events(
    state: StateDir, tmp_path: Path
) -> None:
    from pr_watch.commit import commit_snapshot
    from pr_watch.store import NewEvent

    pr = "PR_abc123"
    with open_store(state) as s:
        s.conn.execute(
            "INSERT INTO watches VALUES ('w-aaaaaaaaaa', 'pr', 'github.com/o/r#1', "
            "'2026-01-01T00:00:00.000000Z')"
        )
        s.conn.execute("INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, 'p', 'r', 'h')", (pr,))
        s.conn.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, 'w-aaaaaaaaaa')", (pr,))
    env = {**os.environ, "HOME": str(tmp_path / "h"), "XDG_CONFIG_HOME": str(tmp_path / "c")}
    child = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(TAKEOVER_CHILD), str(state.path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        assert child.stdout.readline().strip() == "held"  # alive, rev 0 recorded
        with open_store(state, busy_timeout_ms=20000) as s:
            clock = Clock(datetime.now(UTC))
            leases = Leases(s, timedelta(minutes=5), clock=clock)
            assert leases.acquire(pr) is None  # live and unexpired
            clock.now += timedelta(minutes=6)  # expiry: taken over although the child lives
            assert leases.acquire(pr) is not None
            event = NewEvent(
                "closed",
                pr,
                "github.com/o/r#1",
                {
                    "repo": "github.com/o/r",
                    "occurred_at": None,
                    "head_sha": "a" * 40,
                    "watches": [],
                },
            )
            first = commit_snapshot(
                s,
                pr_id=pr,
                expected_rev=0,
                snapshot_pr_id=pr,
                fields={"digest": "parent"},
                prepare=lambda conn: [event],
            )
            assert first.committed
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "revision"
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
    with open_store(state) as s:
        assert s.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert s.conn.execute("SELECT digest FROM prs").fetchone()[0] == "parent"


def test_a_row_with_a_non_numeric_pid_is_takeable(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + TTL)
    store.conn.execute("UPDATE leases SET holder_pid = 'abc'")
    assert leases.acquire(PR_ID) is not None
