"""Per-PR leases (docs/interface.md, Locks and commit).

A lease row says which process is snapshotting a PR: holder pid, process start time, boot id and an
expiry. Leases only decide who may start work; they never signal anything. A lease is taken over
when its holder is dead (pid, start time and boot id do not verify) or it has expired, whether or
not the holder is alive: that is safe because every commit is a compare-and-set (commit.py).

Liveness comes from `/proc` (Linux): the start time is field 22 of `/proc/<pid>/stat` (clock ticks
since boot) and the boot id is `/proc/sys/kernel/random/boot_id`. A pid that exists with another
start time is a reused pid (dead holder); another boot id is a rebooted machine (dead holder).
Where `/proc` is missing (macOS) there is no way to read a start time or boot id, and no
subprocess may run outside the gateway: the identity is then `unknown`, and an unknown holder
counts as alive until its lease expires (conservative; the macOS checklist, #32, covers a
substitute).

Expiry uses the wall clock, since the row is shared between processes (a monotonic clock is
per-boot and per-process). A clock stepped backwards would leave a lease with an expiry far in the
future; an expiry more than `MAX_LEASE_TTL + FUTURE_SLACK` ahead of now is therefore treated as
expired, and so is an unparseable one. Takeover is always safe (compare-and-set), so these err
toward progress.

Like store.py this module never opens a descriptor on `state.db`, `-wal` or `-shm` (closing one
drops SQLite's locks); it reads only `/proc` files and goes through `Store.transaction()`.
"""

from __future__ import annotations

import enum
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from .store import Store, format_ts

UNKNOWN: Final = "unknown"
FUTURE_SLACK: Final = timedelta(seconds=60)
# The longest lease any process may write; a longer `ttl` is capped to it. The future-expiry check
# uses this fixed value, never the contender's own ttl: a contender with a shorter ttl must not
# take over a live holder's unexpired lease (docs/interface.md allows no cap on `forgectl.timeout`).
MAX_LEASE_TTL: Final = timedelta(hours=1)
_STAT_START_INDEX: Final = 19  # starttime is field 22; the fields after "(comm)" begin at field 3
_DEAD_STATES: Final = frozenset({"Z", "X", "x"})


@dataclass(frozen=True)
class ProcessIdentity:
    """Who holds a lease. `start` and `boot_id` are `unknown` where `/proc` is unavailable."""

    pid: int
    start: str
    boot_id: str


class Liveness(enum.Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


class ProcFs:
    """Reads process identity from a `/proc` tree (the root is injectable for tests)."""

    def __init__(self, root: Path | str = "/proc") -> None:
        self.root = Path(root)

    def boot_id(self) -> str | None:
        try:
            text = (self.root / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return text if text and len(text) <= 128 else None

    def _stat(self, pid: int) -> tuple[Liveness, str | None]:
        """(DEAD, None) when the process is gone or a zombie; (UNKNOWN, None) when it cannot be
        read; (ALIVE, start time) otherwise."""
        if pid <= 0:
            return Liveness.DEAD, None
        try:
            raw = (self.root / str(pid) / "stat").read_bytes()
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            return (Liveness.DEAD, None) if self.root.is_dir() else (Liveness.UNKNOWN, None)
        except OSError:
            return Liveness.UNKNOWN, None
        # the command name may hold spaces and parentheses: the fields start after the LAST ")"
        text = raw.decode("utf-8", errors="replace")
        tail = text[text.rfind(")") + 1 :].split() if ")" in text else []
        if len(tail) <= _STAT_START_INDEX or not tail[_STAT_START_INDEX].isdigit():
            return Liveness.UNKNOWN, None
        if tail[0] in _DEAD_STATES:
            return Liveness.DEAD, None
        return Liveness.ALIVE, tail[_STAT_START_INDEX]

    def start_time(self, pid: int) -> str | None:
        state, start = self._stat(pid)
        return start if state is Liveness.ALIVE else None

    def identity(self, pid: int | None = None) -> ProcessIdentity:
        pid = os.getpid() if pid is None else pid
        boot = self.boot_id()
        start = self.start_time(pid)
        if boot is None or start is None:
            return ProcessIdentity(pid, UNKNOWN, UNKNOWN)
        return ProcessIdentity(pid, start, boot)

    def liveness(self, pid: int, start: str) -> Liveness:
        """Whether the process with this pid and start time is still the same running process."""
        state, found = self._stat(pid)
        if state is not Liveness.ALIVE:
            return state
        return Liveness.ALIVE if found == start else Liveness.DEAD


@dataclass(frozen=True)
class Lease:
    pr_id: str
    holder: ProcessIdentity
    expires_at: datetime


def _parse(stamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


class Leases:
    """Lease operations for one process on one open store."""

    def __init__(
        self,
        store: Store,
        ttl: timedelta,
        *,
        clock: Callable[[], datetime] | None = None,
        proc: ProcFs | None = None,
        identity: ProcessIdentity | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("a lease ttl must be positive")
        self.store = store
        self.ttl = min(ttl, MAX_LEASE_TTL)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.proc = proc or ProcFs()
        self.identity = identity or self.proc.identity()

    # -- decisions ------------------------------------------------------------------------

    def _expired(self, stamp: str, now: datetime) -> bool:
        expires = _parse(stamp)
        if expires is None:
            return True
        return expires <= now or expires - now > MAX_LEASE_TTL + FUTURE_SLACK

    def _holder_liveness(self, pid: int, start: str, boot_id: str) -> Liveness:
        mine = self.identity
        if not start or not boot_id:
            return Liveness.DEAD  # a row no process could have written
        if UNKNOWN in (start, boot_id, mine.boot_id):
            return Liveness.UNKNOWN
        if boot_id != mine.boot_id:
            return Liveness.DEAD
        return self.proc.liveness(pid, start)

    def _takeable(self, r: sqlite3.Row | tuple[object, ...], now: datetime) -> bool:
        pid, start, boot_id, expires_at = r
        if self._expired(str(expires_at), now):
            return True
        try:
            holder_pid = int(str(pid))
        except ValueError:
            return True  # a row no process could have written
        return self._holder_liveness(holder_pid, str(start), str(boot_id)) is Liveness.DEAD

    # -- operations -----------------------------------------------------------------------

    def acquire(self, pr_id: str) -> Lease | None:
        """Take the PR's lease. `None` when a live holder (this process included) has an
        unexpired one."""
        now = self.clock()
        expires = now + self.ttl
        me = self.identity
        values = (me.pid, me.start, me.boot_id, format_ts(expires))
        with self.store.transaction() as conn:
            found = conn.execute(
                "SELECT holder_pid, holder_start, boot_id, expires_at FROM leases WHERE pr_id = ?",
                (pr_id,),
            ).fetchone()
            if found is None:
                conn.execute(
                    "INSERT INTO leases (pr_id, holder_pid, holder_start, boot_id, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pr_id, *values),
                )
            elif self._takeable(found, now):
                conn.execute(
                    "UPDATE leases SET holder_pid = ?, holder_start = ?, boot_id = ?, "
                    "expires_at = ? WHERE pr_id = ?",
                    (*values, pr_id),
                )
            else:
                return None
        return Lease(pr_id, me, expires)

    def renew(self, lease: Lease) -> Lease | None:
        """Push the expiry out to now plus the ttl. `None` when the lease is no longer ours (taken
        over or deleted): the caller's snapshot may still commit; the compare-and-set decides."""
        now = self.clock()
        expires = now + self.ttl
        with self.store.transaction() as conn:
            cur = conn.execute(
                "UPDATE leases SET expires_at = ? WHERE pr_id = ? AND holder_pid = ? "
                "AND holder_start = ? AND boot_id = ?",
                (format_ts(expires), lease.pr_id, *self._key(lease)),
            )
            if cur.rowcount != 1:
                return None
        return Lease(lease.pr_id, lease.holder, expires)

    def release(self, lease: Lease) -> bool:
        """Delete the lease if it is still ours; `False` when it was not (already released,
        expired and taken over)."""
        with self.store.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM leases WHERE pr_id = ? AND holder_pid = ? AND holder_start = ? "
                "AND boot_id = ?",
                (lease.pr_id, *self._key(lease)),
            )
            return cur.rowcount == 1

    def stale_pr_ids(self, conn: sqlite3.Connection) -> list[str]:
        """The PRs whose lease is expired or held by a dead holder, read through `conn` (a
        transaction of the caller, or a read-only snapshot for `prune --dry-run`)."""
        now = self.clock()
        rows = conn.execute(
            "SELECT pr_id, holder_pid, holder_start, boot_id, expires_at FROM leases"
        ).fetchall()
        return [str(r[0]) for r in rows if self._takeable(tuple(r[1:]), now)]

    @staticmethod
    def _key(lease: Lease) -> tuple[int, str, str]:
        return (lease.holder.pid, lease.holder.start, lease.holder.boot_id)


__all__ = ["Lease", "Leases", "Liveness", "ProcFs", "ProcessIdentity"]
