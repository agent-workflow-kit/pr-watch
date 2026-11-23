"""SQLite state: safe open, additive migrations, `store_id`, transactions and event append.

docs/interface.md (State, Files, Upgrades) is normative. `state.db` lives in the verified state
directory (state_dir.py), is mode 0600 and runs in WAL mode.

Opening. This process never opens a descriptor on `state.db`, `-wal` or `-shm`: closing any
descriptor on an inode drops every POSIX lock the process holds on it (SQLite's included), which
loses commits. The files are checked with `lstat` relative to the state directory descriptor
(`verify_path_at`): a symlink, a non-regular file, a foreign owner or a group- or world-writable
mode is `StateDirError` (exit 12 `state_unsafe`). A missing `state.db` is created `O_EXCL`, mode
0600, and closed at once (a brand-new inode has no locks to lose). SQLite opens by path, so after
connecting the path is compared (device and inode) with the checked `lstat`; a swap in between is
refused. `-wal` and `-shm` inherit the database's mode.

Versions. `meta.schema_version` and `PRAGMA user_version` hold the same integer. Before anything
is written, a separate read-only connection (it cannot checkpoint or delete a WAL) validates the
file: an empty or 0-byte file is initialized; any other file must hold the pr-watch `meta` (with
`store_id` and `schema_version`) and `events` tables, else it is `StoreCorruptError`, and a
foreign database is left byte-for-byte unchanged. A version above `SCHEMA_VERSION` (the larger of
`meta.schema_version` and `user_version`) is refused (`StoreTooNewError`, exit 12
`store_too_new`); a lower mismatch is corrupt. Migrations (`MIGRATIONS`, version = position + 1)
are additive and run in one `BEGIN IMMEDIATE` transaction together with the version bump, taken
only when the database is behind; a current database is opened without the write lock, so
read-only commands do not wait for a commit. The version is re-read under the lock, so two
processes opening a new file at once migrate it once.

Transactions. `Store.transaction()` is `BEGIN IMMEDIATE` ... `COMMIT`; writers are serialized, so
event sequence numbers are assigned in commit order. The compare-and-set commit (commit.py) and the
leases (leases.py) are built on it; `append_event` must run inside it, so an event is stored in the
same transaction as the digest it belongs to (docs/interface.md, Locks and commit). A trigger
drops a `prs` row in the transaction that deletes its last `sources` row.

A corrupt, non-SQLite or foreign file and a database busy beyond the timeout report as `internal`
(exit 1); the interface names no code for them.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from .state_dir import StateDir, StateDirError, verify_path_at
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
PR_MAX_CHARS: Final = 512  # schemas/defs.json gives `pr` no maxLength; this is the store's cap
REPO_RE: Final = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|\Z))[A-Za-z0-9._-]+)+\Z"
)
SHA_RE: Final = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
WATCH_ID_RE: Final = re.compile(r"^w-[a-z2-7]{10}\Z")
MAX_SAFE_INT: Final = 2**53
JOURNAL_SIZE_LIMIT: Final = 64 * 1024 * 1024
PR_ID_RE: Final = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9:/_=.+-]{0,127}\Z")
LABEL_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}=[A-Za-z0-9._:@/#+-]{1,128}\Z")
TS_RE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z")

EVENT_TYPES: Final = frozenset(
    {
        "renamed",
        "opened",
        "reopened",
        "pushed",
        "retargeted",
        "review",
        "comment",
        "review_comment",
        "review_requested",
        "approved",
        "changes_requested",
        "approval_withdrawn",
        "ci",
        "ready",
        "converted_to_draft",
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
    # A PR row is dropped in the transaction that deletes its last source (no orphan can exist).
    """CREATE TRIGGER sources_drop_orphan_pr AFTER DELETE ON sources
    BEGIN
        DELETE FROM prs WHERE pr_id = OLD.pr_id
            AND NOT EXISTS (SELECT 1 FROM sources WHERE pr_id = OLD.pr_id);
    END""",
    # Lease semantics are in leases.py; the holder identity is (pid, start time, boot id).
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


def _prepare_files(state: StateDir, *, create: bool = True) -> os.stat_result | None:
    """Check (creating `state.db` 0600 when missing) and return the database's `lstat`; None when
    it is missing and `create` is False. Nothing here keeps a descriptor on a database file open,
    nor opens an existing one."""
    info = verify_path_at(state.fd, DB_NAME)
    if info is None and not create:
        return None
    for _ in range(3):  # a concurrent creator can win the O_EXCL race; check its file next
        if info is not None:
            break
        _create_db_file(state)
        info = verify_path_at(state.fd, DB_NAME)
    if info is None:
        raise StateDirError(f"{DB_NAME} cannot be opened (it keeps changing)")
    for suffix in ("-wal", "-shm"):
        verify_path_at(state.fd, DB_NAME + suffix)
    return info


def _create_db_file(state: StateDir) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(DB_NAME, flags, 0o600, dir_fd=state.fd)
    except FileExistsError:
        return
    except OSError as exc:
        raise _refuse(exc) from None
    try:  # a new inode: no lock of this process can be on it, so closing it is safe
        os.fchmod(fd, 0o600)  # exact mode even under a permissive umask
    finally:
        os.close(fd)


def _refuse(exc: OSError) -> StateDirError:
    if exc.errno == errno.ELOOP:
        return StateDirError(f"{DB_NAME} is a symlink")
    return StateDirError(f"{DB_NAME} cannot be opened ({one_line(exc.strerror or '', 100)})")


def classify_error(exc: sqlite3.Error) -> StoreError:
    """Map a sqlite error to a store error without echoing its text beyond a short tag."""
    if isinstance(exc, sqlite3.OperationalError):
        name = getattr(exc, "sqlite_errorname", "") or ""
        if name in ("SQLITE_BUSY", "SQLITE_LOCKED") or "locked" in str(exc):
            return StoreBusyError(f"{DB_NAME} is busy (locked longer than the busy timeout)")
        return StoreError(f"{DB_NAME} cannot be used ({one_line(name or 'sqlite error', 60)})")
    if isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, sqlite3.IntegrityError):
        return StoreCorruptError(f"{DB_NAME} is not a usable database (corrupt or not SQLite)")
    return StoreError(f"{DB_NAME} failed ({one_line(type(exc).__name__, 60)})")


def _is_event_id_collision(exc: sqlite3.IntegrityError) -> bool:
    return getattr(
        exc, "sqlite_errorname", ""
    ) == "SQLITE_CONSTRAINT_UNIQUE" and "events.id" in str(exc)


def _read_version(conn: sqlite3.Connection, latest: int) -> int:
    """`_read_state` in one read transaction (unless the caller has one open), so a concurrent
    migration commit cannot show half of its effect to the separate reads."""
    if conn.in_transaction:
        return _read_state(conn, latest)
    conn.execute("BEGIN")
    try:
        return _read_state(conn, latest)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


def _read_state(conn: sqlite3.Connection, latest: int) -> int:
    """The schema version of an opened database (0 for an empty file), read-only.

    Any non-empty database must be ours: `meta` with `store_id` and `schema_version`, and
    `events` (else `StoreCorruptError`, before the caller writes anything). The larger of
    `meta.schema_version` and `user_version` above `latest` is `StoreTooNewError`; a lower
    mismatch is corrupt."""
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables and user_version == 0:
        return 0
    if not {"meta", "events"} <= tables:
        raise StoreCorruptError(f"{DB_NAME} is not a pr-watch database")
    meta = dict(
        conn.execute("SELECT key, value FROM meta WHERE key IN ('store_id', 'schema_version')")
    )
    if "store_id" not in meta or "schema_version" not in meta:
        raise StoreCorruptError(f"{DB_NAME} is not a pr-watch database")
    recorded = _int_version(meta["schema_version"])
    found = max(recorded, user_version)
    if found > latest:
        raise StoreTooNewError(found, latest)
    if recorded != user_version:
        raise StoreCorruptError(f"{DB_NAME} has inconsistent schema versions")
    return recorded


def _int_version(raw: object) -> int:
    try:
        value = int(str(raw))
    except ValueError:
        raise StoreCorruptError(f"{DB_NAME} has an unreadable schema version") from None
    if value < 0:
        raise StoreCorruptError(f"{DB_NAME} has an unreadable schema version")
    return value


def _enable_wal(conn: sqlite3.Connection, busy_ms: int) -> None:
    """Switch to WAL only when the journal mode is not already WAL (the switch needs the write
    lock). The switch can report 'locked' under contention even with a busy timeout, so it is
    retried until the busy timeout has passed."""
    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return
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
        current = _read_version(conn, len(migrations))
        for step in range(current, len(migrations)):
            migrations[step](conn)
            _write_version(conn, step + 1)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return len(migrations)


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
    store = _open(state, busy_timeout_ms, migrations, create=True)
    assert store is not None
    return store


def open_existing_store(
    state: StateDir,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> Store | None:
    """The read-only open of `list`, `show`, `events` and `export`. A missing `state.db`, and a
    file with no tables and `user_version` 0 (empty or 0-byte), return None: an empty store, and
    pr-watch creates no file. An existing file gets every check of `open_store`, but is
    opened `mode=ro` with `PRAGMA query_only=ON`: no WAL switch, no migration, no checkpoint on
    close, no write pragma. The database file and an existing WAL are never changed; SQLite itself
    may create an empty `-wal` and a `-shm` for a read-only reader of a WAL database
    (docs/interface.md, Files). A database behind the supported schema is not migrated here: it is
    refused with a `StoreError` (`internal`) telling to run a mutating command first."""
    if not 0 <= busy_timeout_ms <= 600_000:
        raise ValueError("busy_timeout_ms out of range")
    checked = _prepare_files(state, create=False)
    if checked is None:
        return None
    path = state.path_for(DB_NAME)
    latest = len(migrations)
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000, isolation_level=None)
    except sqlite3.Error as exc:
        raise classify_error(exc) from None
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA query_only = ON")
        _confirm_same_file(state, checked)
        found = _read_version(conn, latest)  # BEGIN ... ROLLBACK; validates the file
        if found == 0:
            conn.close()
            return None
        if found < latest:
            raise StoreError(
                f"{DB_NAME} is at schema {found}, older than {latest}; "
                "run a command that writes (such as poll) to upgrade it"
            )
        return _finish(conn, path, latest)
    except sqlite3.Error as exc:
        conn.close()
        raise classify_error(exc) from None
    except BaseException:
        conn.close()
        raise


def _open(
    state: StateDir, busy_timeout_ms: int, migrations: Sequence[Migration], *, create: bool
) -> Store | None:
    if not 0 <= busy_timeout_ms <= 600_000:
        raise ValueError("busy_timeout_ms out of range")
    checked = _prepare_files(state, create=create)
    if checked is None:
        return None
    path = state.path_for(DB_NAME)
    _peek(path, busy_timeout_ms, len(migrations))
    conn = _connect(path, busy_timeout_ms)
    try:
        _confirm_same_file(state, checked)
        return _initialize(conn, migrations, busy_timeout_ms, path)
    except BaseException:
        conn.close()
        raise


def _peek(path: Path, busy_ms: int, latest: int) -> None:
    """Validate the file through a separate read-only connection before the read-write one
    exists: a refusal then closes only a connection that cannot checkpoint or delete a WAL, so a
    newer or foreign database is left byte-for-byte as it was."""
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=busy_ms / 1000, isolation_level=None)
    except sqlite3.Error as exc:
        raise classify_error(exc) from None
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
        _read_version(conn, latest)
    except sqlite3.Error as exc:
        raise classify_error(exc) from None
    finally:
        conn.close()


def _connect(path: Path, busy_ms: int) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(path), timeout=busy_ms / 1000, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    except sqlite3.Error as exc:
        raise classify_error(exc) from None
    return conn


def _confirm_same_file(state: StateDir, checked: os.stat_result) -> None:
    """The path SQLite opened must still be the file that passed the `lstat` check."""
    try:
        now = os.stat(DB_NAME, dir_fd=state.fd, follow_symlinks=False)
    except OSError:
        raise StateDirError(f"{DB_NAME} changed while it was being opened") from None
    if (now.st_dev, now.st_ino) != (checked.st_dev, checked.st_ino):
        raise StateDirError(f"{DB_NAME} changed while it was being opened")


def _initialize(
    conn: sqlite3.Connection, migrations: Sequence[Migration], busy_ms: int, path: Path
) -> Store:
    latest = len(migrations)
    try:
        found = _read_version(conn, latest)  # validates again: nothing is written before this
        _enable_wal(conn, busy_ms)
        conn.execute("PRAGMA synchronous = FULL")  # an event is durable once its commit returns
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
        if found < latest:  # only a database that is behind takes the write lock
            _apply_migrations(conn, migrations)
        return _finish(conn, path, latest)
    except sqlite3.Error as exc:
        raise classify_error(exc) from None


def _finish(conn: sqlite3.Connection, path: Path, latest: int) -> Store:
    version = _read_version(conn, latest)
    row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
    store_id = row[0] if row else None
    if not isinstance(store_id, str) or not STORE_ID_RE.match(store_id):
        raise StoreCorruptError(f"{DB_NAME} has no valid store id")
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


def _check_text(text: str, what: str) -> None:
    """A string that cannot encode as UTF-8 (a lone surrogate) is refused rather than stored
    escaped, where a consumer decoding it would fail."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise EventError(f"event {what} is not valid text") from None


def _check_json_value(value: Any, depth: int) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise EventError("event payload is nested too deeply")
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise EventError("event payload has an overlong string")
        _check_text(value, "payload")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventError("event payload has a non-string key")
            if len(key) > 64:
                raise EventError("event payload has an overlong key")
            _check_text(key, "payload")
            _check_json_value(item, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            _check_json_value(item, depth + 1)
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise EventError("event payload has an integer outside +/-2**53")
    elif not isinstance(value, float):
        raise EventError("event payload holds a value that is not JSON")


def _check_common_fields(payload: Mapping[str, Any]) -> None:
    """The fields schemas/event.json requires of every type that are not columns."""
    repo = payload.get("repo")
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise EventError("event payload has no valid repo")
    occurred = payload.get("occurred_at", ...)
    if occurred is not None and (not isinstance(occurred, str) or not TS_RE.match(occurred)):
        raise EventError("event payload has no valid occurred_at")
    head = payload.get("head_sha")
    if not isinstance(head, str) or not SHA_RE.match(head):
        raise EventError("event payload has no valid head_sha")
    watches = payload.get("watches")
    if (
        not isinstance(watches, list | tuple)
        or not all(isinstance(w, str) and WATCH_ID_RE.match(w) for w in watches)
        or len(set(watches)) != len(watches)
    ):
        raise EventError("event payload has no valid watches")


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


def _validate_event(event: NewEvent, labels: list[str]) -> str:
    """Check the event and return its encoded payload. `labels` is the one materialized list
    that is validated and stored (a generator would be consumed by the first pass)."""
    if event.type not in EVENT_TYPES:
        raise EventError("unknown event type")
    if not isinstance(event.pr_id, str) or not PR_ID_RE.match(event.pr_id):
        raise EventError("event pr_id is malformed")
    if not isinstance(event.pr, str) or len(event.pr) > PR_MAX_CHARS or not PR_RE.match(event.pr):
        raise EventError("event pr is malformed")
    if len(labels) > MAX_LABELS or len(set(labels)) != len(labels):
        raise EventError("event labels are too many or repeated")
    if not all(isinstance(x, str) and LABEL_RE.match(x) for x in labels):
        raise EventError("event label is malformed")
    encoded = encode_payload(event.payload)
    _check_common_fields(event.payload)
    return encoded


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
            raise classify_error(exc) from None
        try:
            yield self.conn
        except BaseException:
            self._rollback()
            raise
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback()
            raise classify_error(exc) from None

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
        labels_list = list(event.labels)
        payload = _validate_event(event, labels_list)
        stamp = format_ts(detected_at if detected_at is not None else datetime.now(UTC))
        labels = json.dumps(labels_list, ensure_ascii=True, separators=(",", ":"))
        for _ in range(5):
            event_id = new_event_id()
            try:
                cur = self.conn.execute(
                    "INSERT INTO events (id, type, pr_id, pr, payload, detected_at, labels) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, event.type, event.pr_id, event.pr, payload, stamp, labels),
                )
            except sqlite3.IntegrityError as exc:
                if _is_event_id_collision(exc):
                    continue  # about 2**-60: draw another id
                raise classify_error(exc) from None
            except sqlite3.Error as exc:
                raise classify_error(exc) from None
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
                labels=labels_list,
            )
        raise StoreError("could not draw an unused event id")
