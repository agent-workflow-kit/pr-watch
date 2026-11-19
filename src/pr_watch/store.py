"""SQLite state: safe open, additive migrations, `store_id`, transactions and event append.

docs/interface.md (State, Files, Upgrades) is normative. `state.db` lives in the verified state
directory (state_dir.py), is mode 0600 and runs in WAL mode.

Opening. The file is opened relative to the state directory descriptor with `O_NOFOLLOW` (created
`O_EXCL`, mode 0600, when missing) and checked with `verify_file` on the descriptor: a symlink, a
non-regular file, a foreign owner or a group- or world-writable mode is `StateDirError` (exit 12
`state_unsafe`). Existing `-wal` and `-shm` files get the same check. SQLite opens by path, so
after connecting the path is compared (device and inode) with the checked descriptor; a swap in
between is refused. `-wal` and `-shm` inherit the database's mode.

Versions. `meta.schema_version` and `PRAGMA user_version` hold the same integer. A database whose
version is above `SCHEMA_VERSION` is refused (`StoreTooNewError`, exit 12 `store_too_new`) before
anything is written, including the WAL switch. Migrations (`MIGRATIONS`, version = position + 1)
are additive and run in one `BEGIN IMMEDIATE` transaction together with the version bump, so a
crash leaves the previous version intact and two processes opening a new file at once migrate it
once (the second waits on the busy timeout, then sees the new version).

Transactions. `Store.transaction()` is `BEGIN IMMEDIATE` ... `COMMIT`; writers are serialized, so
event sequence numbers are assigned in commit order. The compare-and-set commit and the leases are
built on it by #12; `append_event` must run inside it, so an event is stored in the same
transaction as the digest it belongs to (docs/interface.md, Locks and commit).

An empty or malformed file is `StoreCorruptError`; the interface names no code for it, so it
reports as `internal` (exit 1) rather than claiming a code the schema does not list.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from .state_dir import StateDir, StateDirError, verify_file
from .text import one_line

DB_NAME: Final = "state.db"
DEFAULT_BUSY_TIMEOUT_MS: Final = 5000
_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"

STORE_ID_RE: Final = re.compile(r"^[a-z2-7]{8}$")
EVENT_ID_RE: Final = re.compile(r"^ev-[a-z2-7]{12}$")
# schemas/defs.json patterns (pr, pr_id, label, ts), restated here so the store needs no jsonschema.
PR_RE: Final = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}\Z"
)
PR_ID_RE: Final = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9:/_=.+-]{0,127}\Z")
LABEL_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}=[A-Za-z0-9._:@/#+-]{1,128}\Z")
TS_RE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z")

EVENT_TYPES: Final = frozenset(
    {
        "pushed",
        "review",
        "comment",
        "review_comment",
        "approved",
        "changes_requested",
        "ci",
        "ready",
        "merged",
        "closed",
    }
)
# Columns of the events table (and so not allowed as payload keys).
RESERVED_EVENT_KEYS: Final = frozenset(
    {"id", "seq", "type", "pr", "pr_id", "detected_at", "labels"}
)
MAX_PAYLOAD_BYTES: Final = 128 * 1024
MAX_PAYLOAD_DEPTH: Final = 6
MAX_STRING_CHARS: Final = 4096
MAX_LABELS: Final = 256


# --- errors ----------------------------------------------------------------------------------


class StoreError(Exception):
    """A store failure. `code` is the error code of schemas/defs.json the CLI reports. Messages are
    one sanitized line and never carry database content."""

    code = "internal"


class StoreTooNewError(StoreError):
    """The database was written by a newer pr-watch (`store_too_new`, exit 12)."""

    code = "store_too_new"

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(
            f"{DB_NAME} has schema version {found} but this pr-watch supports up to {supported}"
        )
        self.found = found
        self.supported = supported


class StoreCorruptError(StoreError):
    """The file is not a usable pr-watch database (not SQLite, malformed, or another program's)."""


class StoreBusyError(StoreError):
    """The database stayed locked longer than the busy timeout."""


class EventError(StoreError):
    """An event that violates the stored shape (a caller bug, never a host's content)."""


# --- schema and migrations -------------------------------------------------------------------

Migration = Callable[[sqlite3.Connection], None]

_V1_STATEMENTS: Final = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY NOT NULL, value TEXT) WITHOUT ROWID",
    """CREATE TABLE hosts (
        host TEXT PRIMARY KEY NOT NULL,
        backend TEXT,
        poll_count_since_full INTEGER NOT NULL DEFAULT 0,
        full_pass_started_at TEXT,
        last_full_at TEXT,
        last_enumerated_at TEXT,
        paused_until TEXT,
        pause_step INTEGER NOT NULL DEFAULT 0,
        low_water_until TEXT
    ) WITHOUT ROWID""",
    "CREATE TABLE scopes (repo TEXT PRIMARY KEY NOT NULL, watermark TEXT) WITHOUT ROWID",
    """CREATE TABLE watches (
        id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('pr', 'query')),
        key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE owners (
        watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        PRIMARY KEY (watch_id, owner)
    ) WITHOUT ROWID""",
    """CREATE TABLE labels (
        watch_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        label TEXT NOT NULL,
        PRIMARY KEY (watch_id, owner, label),
        FOREIGN KEY (watch_id, owner) REFERENCES owners(watch_id, owner) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TABLE prs (
        pr_id TEXT PRIMARY KEY NOT NULL,
        pr TEXT NOT NULL,
        repo TEXT NOT NULL,
        host TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'terminal', 'unreachable')),
        terminal_at TEXT,
        terminal_kind TEXT CHECK (terminal_kind IS NULL OR terminal_kind IN ('merged', 'closed')),
        rev INTEGER NOT NULL DEFAULT 0,
        digest TEXT,
        snap_updated_at TEXT,
        listed_updated_at TEXT,
        last_polled_at TEXT,
        last_attempt_at TEXT,
        failure_count INTEGER NOT NULL DEFAULT 0,
        last_failure_code TEXT,
        incomplete TEXT NOT NULL DEFAULT '{}',
        unsettled_since TEXT,
        rebaseline INTEGER NOT NULL DEFAULT 0 CHECK (rebaseline IN (0, 1)),
        baselined_at TEXT
    ) WITHOUT ROWID""",
    "CREATE INDEX prs_by_pr ON prs(pr)",
    "CREATE INDEX prs_by_repo ON prs(repo)",
    """CREATE TABLE sources (
        pr_id TEXT NOT NULL REFERENCES prs(pr_id) ON DELETE CASCADE,
        watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        left_count INTEGER NOT NULL DEFAULT 0 CHECK (left_count BETWEEN 0 AND 2),
        PRIMARY KEY (pr_id, watch_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX sources_by_watch ON sources(watch_id)",
    # #12 owns the lease semantics; the holder identity is (pid, start time, boot id).
    """CREATE TABLE leases (
        pr_id TEXT PRIMARY KEY NOT NULL,
        holder_pid INTEGER NOT NULL,
        holder_start TEXT NOT NULL,
        boot_id TEXT NOT NULL,
        expires_at TEXT NOT NULL
    ) WITHOUT ROWID""",
    # AUTOINCREMENT: deleted sequence numbers are never reused (cursors depend on it).
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL,
        pr_id TEXT NOT NULL,
        pr TEXT NOT NULL,
        payload TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        labels TEXT NOT NULL
    )""",
    "CREATE INDEX events_by_pr ON events(pr_id, seq)",
    "CREATE INDEX events_by_detected_at ON events(detected_at)",
)


def _migrate_v1(conn: sqlite3.Connection) -> None:
    for statement in _V1_STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT INTO meta (key, value) VALUES ('store_id', ?)", (new_store_id(),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('pruned_through_seq', '0')")


# Additive only: version N is MIGRATIONS[N - 1]. Append; never edit a released entry.
MIGRATIONS: Final[tuple[Migration, ...]] = (_migrate_v1,)
SCHEMA_VERSION: Final = len(MIGRATIONS)


def new_store_id() -> str:
    """8 lowercase base32 characters (the cursor's `store_id`)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


def new_event_id() -> str:
    """`ev-` plus 12 lowercase base32 characters."""
    return "ev-" + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def format_ts(moment: datetime) -> str:
    """UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ` (schemas/defs.json `ts`) of an aware datetime."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- opening ---------------------------------------------------------------------------------


def _open_checked_fd(state: StateDir) -> tuple[int, bool]:
    """Open (creating 0600 when missing) `state.db` without following a symlink and verify it."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = -1
    created = False
    for _ in range(3):  # a concurrent creator can win the O_EXCL race; open its file next
        try:
            fd = os.open(DB_NAME, flags, dir_fd=state.fd)
            break
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _refuse(exc) from None
        try:
            fd = os.open(DB_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=state.fd)
            created = True
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise _refuse(exc) from None
    if fd < 0:
        raise StateDirError(f"{DB_NAME} cannot be opened (it keeps changing)")
    try:
        if created:
            os.fchmod(fd, 0o600)  # exact mode even under a permissive umask
        verify_file(os.fstat(fd), DB_NAME)
    except BaseException:
        os.close(fd)
        raise
    return fd, created


def _refuse(exc: OSError) -> StateDirError:
    if exc.errno == errno.ELOOP:
        return StateDirError(f"{DB_NAME} is a symlink")
    return StateDirError(f"{DB_NAME} cannot be opened ({one_line(exc.strerror or '', 100)})")


def _check_sidecars(state: StateDir) -> None:
    """`-wal` and `-shm`, when present, get the same checks as the database (SQLite would open
    a symlink there without complaint)."""
    for suffix in ("-wal", "-shm"):
        name = DB_NAME + suffix
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=state.fd)  # fmt: skip
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _refuse_named(name, exc) from None
        try:
            verify_file(os.fstat(fd), name)
        finally:
            os.close(fd)


def _refuse_named(name: str, exc: OSError) -> StateDirError:
    if exc.errno == errno.ELOOP:
        return StateDirError(f"{name} is a symlink")
    return StateDirError(f"{name} cannot be opened ({one_line(exc.strerror or '', 100)})")


def _classify(exc: sqlite3.Error) -> StoreError:
    """Map a sqlite error to a store error without echoing its text beyond a short tag."""
    if isinstance(exc, sqlite3.OperationalError):
        name = getattr(exc, "sqlite_errorname", "") or ""
        if name in ("SQLITE_BUSY", "SQLITE_LOCKED") or "locked" in str(exc):
            return StoreBusyError(f"{DB_NAME} is busy (locked longer than the busy timeout)")
        return StoreError(f"{DB_NAME} cannot be used ({one_line(name or 'sqlite error', 60)})")
    if isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, sqlite3.IntegrityError):
        return StoreCorruptError(f"{DB_NAME} is not a usable database (corrupt or not SQLite)")
    return StoreError(f"{DB_NAME} failed ({one_line(type(exc).__name__, 60)})")


def _read_version(conn: sqlite3.Connection) -> int:
    """The schema version of an opened database (0 for an empty file), read-only. Raises
    `StoreCorruptError` for a file that is not SQLite or is another program's database."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == 0:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "meta" not in tables:
            raise StoreCorruptError(f"{DB_NAME} is not a pr-watch database")
        if "meta" in tables:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is not None:
                return _int_version(row[0])
    return version


def _int_version(raw: object) -> int:
    try:
        value = int(str(raw))
    except ValueError:
        raise StoreCorruptError(f"{DB_NAME} has an unreadable schema version") from None
    if value < 0:
        raise StoreCorruptError(f"{DB_NAME} has an unreadable schema version")
    return value


def _enable_wal(conn: sqlite3.Connection, busy_ms: int) -> None:
    """Switch to WAL (a no-op when already WAL). The switch can report 'locked' under contention
    even with a busy timeout, so it is retried until the busy timeout has passed."""
    deadline = time.monotonic() + busy_ms / 1000
    while True:
        try:
            mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
            continue
        if mode != "wal":
            raise StoreError(f"{DB_NAME} cannot use WAL mode (journal mode {one_line(mode, 20)})")
        return


def _apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> int:
    """Run every migration above the stored version in one immediate transaction; returns the
    version reached. Re-reads the version after taking the write lock, so a concurrent opener
    that migrated first is seen and nothing runs twice."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _read_version(conn)
        for step in range(current, len(migrations)):
            migrations[step](conn)
            _write_version(conn, step + 1)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return max(current, len(migrations))


def _write_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )
    conn.execute(f"PRAGMA user_version = {int(version)}")


def open_store(
    state: StateDir,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> Store:
    """Open (creating when missing) and migrate `state.db`.

    Raises `StateDirError` (exit 12 `state_unsafe`) for an unsafe file, `StoreTooNewError` (exit
    12 `store_too_new`), `StoreCorruptError` or `StoreBusyError` (both `internal`).
    `migrations` is a test seam; production callers use the default.
    """
    if not 0 <= busy_timeout_ms <= 600_000:
        raise ValueError("busy_timeout_ms out of range")
    fd, _ = _open_checked_fd(state)
    try:
        _check_sidecars(state)
        checked = os.fstat(fd)
        path = state.path_for(DB_NAME)
        conn = _connect(path, busy_timeout_ms)
        try:
            _confirm_same_file(state, checked)
            store = _initialize(conn, migrations, busy_timeout_ms, state, path)
        except BaseException:
            conn.close()
            raise
    finally:
        os.close(fd)
    return store


def _connect(path: Path, busy_ms: int) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(path), timeout=busy_ms / 1000, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    except sqlite3.Error as exc:
        raise _classify(exc) from None
    return conn


def _confirm_same_file(state: StateDir, checked: os.stat_result) -> None:
    """The path SQLite opened must still be the file that passed `verify_file`."""
    try:
        now = os.stat(DB_NAME, dir_fd=state.fd, follow_symlinks=False)
    except OSError:
        raise StateDirError(f"{DB_NAME} changed while it was being opened") from None
    if (now.st_dev, now.st_ino) != (checked.st_dev, checked.st_ino):
        raise StateDirError(f"{DB_NAME} changed while it was being opened")


def _initialize(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    busy_ms: int,
    state: StateDir,
    path: Path,
) -> Store:
    latest = len(migrations)
    try:
        found = _read_version(conn)  # read-only: a too-new database is refused before any write
        if found > latest:
            raise StoreTooNewError(found, latest)
        _enable_wal(conn, busy_ms)
        conn.execute("PRAGMA synchronous = FULL")  # an event is durable once its commit returns
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        reached = _apply_migrations(conn, migrations)
        if reached > latest:  # another process migrated past us between the peek and the lock
            raise StoreTooNewError(reached, latest)
        return _finish(conn, path)
    except sqlite3.Error as exc:
        raise _classify(exc) from None


def _finish(conn: sqlite3.Connection, path: Path) -> Store:
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    store_id = meta.get("store_id")
    version = _int_version(meta.get("schema_version"))
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if not isinstance(store_id, str) or not STORE_ID_RE.match(store_id):
        raise StoreCorruptError(f"{DB_NAME} has no valid store id")
    if version != user_version:
        raise StoreCorruptError(f"{DB_NAME} has inconsistent schema versions")
    return Store(conn, path, store_id, version)


# --- events ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class NewEvent:
    """An event to store. `payload` holds every schema field that is not a column: `repo`,
    `occurred_at`, `head_sha`, `watches` and the type's extra fields (schemas/event.json)."""

    type: str
    pr_id: str
    pr: str
    payload: Mapping[str, Any]
    labels: Sequence[str] = ()


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    id: str
    type: str
    pr_id: str
    pr: str
    payload: dict[str, Any]
    detected_at: str
    labels: list[str]

    def to_json(self) -> dict[str, Any]:
        """The event as schemas/event.json shapes it (columns and payload merged)."""
        return {
            **self.payload,
            "id": self.id,
            "seq": self.seq,
            "type": self.type,
            "pr": self.pr,
            "pr_id": self.pr_id,
            "detected_at": self.detected_at,
            "labels": list(self.labels),
        }


def _check_json_value(value: Any, depth: int) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise EventError("event payload is nested too deeply")
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise EventError("event payload has an overlong string")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventError("event payload has a non-string key")
            if len(key) > 64:
                raise EventError("event payload has an overlong key")
            _check_json_value(item, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            _check_json_value(item, depth + 1)
    elif value is not None and not isinstance(value, bool | int | float):
        raise EventError("event payload holds a value that is not JSON")


def encode_payload(payload: Mapping[str, Any]) -> str:
    """The payload as compact, ASCII-only JSON (control characters are always escaped)."""
    clash = RESERVED_EVENT_KEYS.intersection(payload)
    if clash:
        raise EventError(f"event payload repeats a column ({sorted(clash)[0]})")
    _check_json_value(payload, 0)
    try:
        text = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True,
                          separators=(",", ":"))  # fmt: skip
    except (TypeError, ValueError):
        raise EventError("event payload is not valid JSON") from None
    if len(text) > MAX_PAYLOAD_BYTES:  # ASCII: characters are bytes
        raise EventError("event payload is too large")
    return text


def _validate_event(event: NewEvent) -> str:
    if event.type not in EVENT_TYPES:
        raise EventError("unknown event type")
    if not isinstance(event.pr_id, str) or not PR_ID_RE.match(event.pr_id):
        raise EventError("event pr_id is malformed")
    if not isinstance(event.pr, str) or not PR_RE.match(event.pr):
        raise EventError("event pr is malformed")
    labels = list(event.labels)
    if len(labels) > MAX_LABELS or len(set(labels)) != len(labels):
        raise EventError("event labels are too many or repeated")
    if not all(isinstance(x, str) and LABEL_RE.match(x) for x in labels):
        raise EventError("event label is malformed")
    return encode_payload(event.payload)


# --- the store -------------------------------------------------------------------------------


class Store:
    """An open, migrated `state.db`. One connection, used from one thread (`sqlite3` default)."""

    def __init__(self, conn: sqlite3.Connection, path: Path, store_id: str, version: int) -> None:
        self.conn = conn
        self.path = path
        self.store_id = store_id
        self.schema_version = version

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` (the write lock is taken up front, waiting the busy timeout), commit
        on success, roll back on any exception. Not reentrant."""
        if self.conn.in_transaction:
            raise StoreError("a transaction is already open")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise _classify(exc) from None
        try:
            yield self.conn
        except BaseException:
            self._rollback()
            raise
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback()
            raise _classify(exc) from None

    def _rollback(self) -> None:
        if self.conn.in_transaction:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # the original failure is the one to report; the connection is unusable

    def head_seq(self) -> int:
        """The highest sequence number ever assigned (0 when none): deleted events still count,
        so a cursor at the head stays valid after a prune."""
        row = self.conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
        return int(row[0]) if row else 0

    def append_event(self, event: NewEvent, detected_at: datetime | None = None) -> StoredEvent:
        """Store one event with the next sequence number. Must run inside `transaction()` so the
        event commits (or vanishes) with the caller's other writes."""
        if not self.conn.in_transaction:
            raise StoreError("append_event must run inside a transaction")
        payload = _validate_event(event)
        stamp = format_ts(detected_at if detected_at is not None else datetime.now(UTC))
        labels = json.dumps(list(event.labels), ensure_ascii=True, separators=(",", ":"))
        for _ in range(5):
            event_id = new_event_id()
            try:
                cur = self.conn.execute(
                    "INSERT INTO events (id, type, pr_id, pr, payload, detected_at, labels) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, event.type, event.pr_id, event.pr, payload, stamp, labels),
                )
            except sqlite3.IntegrityError:
                continue  # an id collision (about 2**-60): draw another
            seq = cur.lastrowid
            if seq is None:
                raise StoreError("event insert returned no sequence number")
            return StoredEvent(
                seq=seq,
                id=event_id,
                type=event.type,
                pr_id=event.pr_id,
                pr=event.pr,
                payload=json.loads(payload),
                detected_at=stamp,
                labels=list(event.labels),
            )
        raise StoreError("could not draw an unused event id")
