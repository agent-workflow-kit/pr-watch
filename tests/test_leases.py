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
        "INSERT OR REPLACE INTO leases VALUES (?, ?, ?, ?, ?)",
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


def test_sweep_deletes_dead_and_expired_leases_only(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    insert_row(store, ProcessIdentity(99, "5", "boot-a"), NOW + TTL, "dead")
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW - timedelta(seconds=1), "expired")
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + TTL, "live")
    insert_row(store, ProcessIdentity(1, "100", "boot-a"), NOW + TTL, "mine")
    assert leases.sweep() == 2
    kept = {r[0] for r in store.conn.execute("SELECT pr_id FROM leases")}
    assert kept == {"live", "mine"}


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
    procs = [spawn(RACER, str(state.path), "0.5") for _ in range(3)]
    try:
        results = []
        for p in procs:
            assert p.stdout is not None
            results.append(p.stdout.readline().strip())
    finally:
        for p in procs:
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


def test_a_row_with_a_non_numeric_pid_is_takeable(store: Store, tmp_path: Path) -> None:
    leases = make(store, tmp_path, Clock())
    insert_row(store, ProcessIdentity(2, "200", "boot-a"), NOW + TTL)
    store.conn.execute("UPDATE leases SET holder_pid = 'abc'")
    assert leases.acquire(PR_ID) is not None
