"""`cron install` and `cron uninstall`: the tagged crontab block (docs/interface.md, `cron`).

The block is four lines (header with `v1 every=D`, a `PATH=` line, one entry, the end line). All
text outside it is kept byte for byte (CRLF, bytes that are not UTF-8); the one change to it is a
final newline when the written table would not end in one (cron refuses such a table): when the
block is appended after a final line that lacks one, or a foreign last line that lacked one
follows the block. An install then an uninstall therefore cannot restore a missing final newline;
that byte is the only difference. The read-modify-write
runs under an exclusive `flock` on `<state dir>/cron.lock` and re-reads the crontab before writing,
and the written table is read back and compared. `crontab` runs only through `gateway.run`, with
the table on stdin (`crontab -`) and `crontab -r` when nothing is left.

Judgment calls where the interface is silent (the conservative reading in each case):

- A crontab whose block is not exactly one start line followed by one end line (a missing end, a
  second start inside, an end with no start, a second block) is refused untouched, exit 2 `usage`
  (`details.kind: malformed_crontab`), by both commands; the message names the shape.
- A block line longer than 999 bytes (cron reads a line into a 1000-byte buffer with its
  terminator) is refused: `config_invalid` for a `cron.env_file` that makes the entry too long,
  `usage` (`unsafe_path`) otherwise. A `:` in a directory of `PATH=` leaves that directory out
  with a warning (it would split the line).
- The text of `crontab`'s stderr is never put in an error (it can echo crontab lines).
- `PATH=` holds `<home>/.local/bin`, the directory of `forgectl` and `/usr/local/bin:/usr/bin:/bin`
  (a crontab `PATH` replaces cron's own, and hooks need the system tools). A directory that is
  unsafe (not owned by the user or root, group- or world-writable, or with characters that cannot
  go in the line) is left out with a warning, never written. A missing `~/.local/bin` is kept.
- The paths of `pr-watch` and `cron.env_file`, once quoted, must still be plain: no whitespace or
  shell, cron or quote metacharacters (`PLAIN`). A bad `env_file` is `config_invalid` (exit 2); a
  bad `pr-watch` path is `usage` (`unsafe_path`).
- An unreadable, failing or timed out `crontab` is `internal` (exit 1); so is a lock held for more
  than `LOCK_WAIT` seconds.
- An empty result is installed with `crontab -r`, not as an empty table.
- `--dry-run` runs the same read and the same `compute` as a real run and skips only the write:
  it reads the crontab once through `crontab -l`, takes no `cron.lock` (it writes nothing, and one
  `crontab -l` is a consistent snapshot) and creates no state directory. `dry_run` and `block`
  are in the result of real runs too (`dry_run` false), so one document serves both.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import gateway
from .cli import PROG, CliError, Context, _stderr_line
from .refs import RefError, parse_every
from .state_dir import CRON_LOCK, StateDir, StateDirError, home_dir, verify_file, verify_path_at
from .text import one_line

MAX_CRONTAB_BYTES: Final = 1024 * 1024
LOCK_WAIT: Final = 30.0  # seconds a command waits for `cron.lock` before giving up
_LOCK_TICK: Final = 0.05
_CRONTAB_TIMEOUT: Final = 30.0
_ATTEMPTS: Final = 3

START: Final = b"# pr-watch managed"
END: Final = b"# pr-watch managed end"
# vixie/cronie read a line into a 1000-byte buffer with its terminator; the schemas allow 4096
MAX_LINE_BYTES: Final = 999
SYSTEM_DIRS: Final = ("/usr/local/bin", "/usr/bin", "/bin")
# Characters a path may not hold on top of whitespace and control characters: shell, cron and
# quote metacharacters. Paths are also single-quoted, but the interface refuses them outright.
_REFUSED_CHARS: Final = frozenset("'\"`%;\\$&|<>(){}[]*?!#^")
_REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Cs"})
_POLL_LINE: Final = re.compile(
    rb"(?<![\w.-])pr-watch['\"]?\s+(?:-\S+\s+(?:[^-\s]\S*\s+)?)*poll(?=\s|$)"
)
_geteuid = os.geteuid  # seam: tests patch this name, not the shared `os` module


class MalformedCrontab(Exception):
    """The crontab holds a managed block that cannot be read unambiguously."""


# --- pure helpers -----------------------------------------------------------------------------


def schedule(every: str) -> str:
    """The five cron fields of `Nm` (N dividing 60) or `Nh` (N dividing 24)."""
    count, unit = int(every[:-1]), every[-1]
    if unit == "m":
        if count == 1:
            return "* * * * *"
        return "0 * * * *" if count == 60 else f"*/{count} * * * *"
    if count == 1:
        return "0 * * * *"
    return "0 0 * * *" if count == 24 else f"0 */{count} * * *"


def _plain(text: str) -> bool:
    return bool(text) and not any(
        ch in _REFUSED_CHARS or unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in text
    )


def locate_binary(argv0: str, path_env: str) -> str:
    """The absolute path of the `pr-watch` executable: `argv[0]` when it is the installed script,
    else the first `pr-watch` on `path_env` (a `python -m pr_watch` run). Symlinks are kept, so an
    upgrade that re-points them keeps working."""
    found: str | None = None
    if os.path.basename(argv0) == "pr-watch":
        found = os.path.abspath(argv0) if "/" in argv0 else shutil.which(argv0, path=path_env)
    if found is None:
        found = shutil.which("pr-watch", path=path_env)
    if found is None:
        raise CliError("internal", "cannot find the pr-watch executable; put it on PATH first")
    found = os.path.abspath(found)
    if not _plain(found):
        raise CliError(
            "usage",
            "the path of pr-watch has whitespace or characters that cannot go in a crontab",
            details={"kind": "unsafe_path"},
        )
    if not os.path.isfile(found) or not os.access(found, os.X_OK):
        raise CliError("internal", "the pr-watch executable is not an executable file")
    return found


def _dir_problem(path: str) -> str | None:
    if not _plain(path):
        return "its path has characters that cannot go in a crontab"
    if ":" in path:
        return "its path has a ':' that would split the PATH line"
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None  # a missing directory is harmless on PATH
    except OSError:
        return "it cannot be examined"
    if info.st_uid not in (_geteuid(), 0):
        return "it is owned by another user"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "it is group- or world-writable"
    return None


def path_dirs(home: Path, path_env: str) -> tuple[list[str], list[str]]:
    """The directories of the `PATH=` line and a warning for each one left out."""
    dirs: list[str] = []
    warnings: list[str] = []

    def add(path: str, label: str) -> None:
        problem = _dir_problem(path)
        if problem is None:
            if path not in dirs:
                dirs.append(path)
        else:
            warnings.append(f"{label} directory {one_line(path, 200)} left out of PATH: {problem}")

    add(f"{home}/.local/bin", "~/.local/bin")
    forgectl = shutil.which("forgectl", path=path_env)
    if forgectl is None:
        warnings.append(
            "forgectl was not found on PATH; cron will not find it unless its directory is on "
            "the crontab PATH"
        )
    elif not os.path.isabs(forgectl):
        warnings.append(
            "forgectl was found only through a relative PATH entry; its directory is left out "
            "of the crontab PATH"
        )
    else:
        add(os.path.dirname(forgectl), "forgectl")
    for system in SYSTEM_DIRS:
        if system not in dirs:
            dirs.append(system)
    return dirs, warnings


def _env_file_error(reason: str) -> CliError:
    return CliError("config_invalid", f"cron.env_file {reason}")


def check_env_file(value: str) -> str:
    """`cron.env_file` as it may be written: an absolute, plain path to a regular file (not a
    symlink) owned by the user with no group or world bits, in a directory owned by the user and
    not group- or world-writable. The message never carries the value."""
    if not value.startswith("/"):
        raise _env_file_error("must be an absolute path")
    if not _plain(value):
        raise _env_file_error("has whitespace or characters that cannot go in a crontab")
    try:
        info = os.lstat(value)
        parent = os.stat(os.path.dirname(value))
    except OSError:
        raise _env_file_error("does not exist or cannot be examined") from None
    if stat.S_ISLNK(info.st_mode):
        raise _env_file_error("is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise _env_file_error("is not a regular file")
    if info.st_uid != _geteuid():
        raise _env_file_error("is owned by another user")
    if info.st_mode & 0o077:
        raise _env_file_error("must have no group or world permission bits")
    if parent.st_uid != _geteuid() or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _env_file_error("is in a directory that is not the user's or is group/world-writable")
    return value


def build_block(every: str, dirs: list[str], binary: str, env_file: str | None) -> list[str]:
    source = f". '{env_file}' && " if env_file else ""
    return [
        f"# pr-watch managed v1 every={every}",
        "PATH=" + ":".join(dirs),
        f"{schedule(every)} {source}'{binary}' poll --quiet",
        END.decode(),
    ]


def _check_line_lengths(block: list[str], env_file: str | None) -> None:
    """Refuse a block line cron could not read (vixie cron's limit is 999 bytes)."""
    if len(block[1].encode()) > MAX_LINE_BYTES:
        raise CliError(
            "usage",
            "the PATH line of the cron block would be longer than 999 bytes",
            details={"kind": "unsafe_path"},
        )
    if len(block[2].encode()) > MAX_LINE_BYTES:
        if env_file:
            raise _env_file_error("is too long for a crontab line")
        raise CliError(
            "usage",
            "the cron entry would be longer than 999 bytes (the path of pr-watch is too long)",
            details={"kind": "unsafe_path"},
        )


# --- the crontab text -------------------------------------------------------------------------


def _norm(line: bytes) -> bytes:
    return line.rstrip(b" \t\r")


def _is_end(line: bytes) -> bool:
    return _norm(line) == END


def _is_start(line: bytes) -> bool:
    norm = _norm(line)
    return norm != END and (norm == START or norm.startswith(START + b" "))


def _split(raw: bytes) -> list[bytes]:
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _locate(lines: list[bytes]) -> tuple[int, int] | None:
    """(start, end) of the one block, None when there is none; a missing, repeated or unpaired
    start or end line raises `MalformedCrontab`."""
    starts = [i for i, line in enumerate(lines) if _is_start(line)]
    ends = [i for i, line in enumerate(lines) if _is_end(line)]
    if not starts and not ends:
        return None
    if len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]:
        return starts[0], ends[0]
    if not ends:
        problem = "has a '# pr-watch managed' line but no '# pr-watch managed end' line"
    elif not starts:
        problem = "has a '# pr-watch managed end' line with no '# pr-watch managed' start line"
    elif len(starts) > 1 or len(ends) > 1:
        problem = "has more than one '# pr-watch managed' block (a second start or end line)"
    else:
        problem = "has a '# pr-watch managed end' line before its start line"
    raise MalformedCrontab(
        f"the crontab {problem}; the crontab is unchanged, repair or remove the block by hand"
    )


def _check_size(raw: bytes) -> None:
    if len(raw) > MAX_CRONTAB_BYTES:
        raise CliError("internal", "the existing crontab is larger than 1 MiB; not touching it")
    if b"\0" in raw:
        raise CliError("internal", "the existing crontab contains a NUL byte; not touching it")


def previous_every(header: bytes) -> str | None:
    for token in _norm(header).split()[3:]:
        if token.startswith(b"every="):
            try:
                return parse_every(token[6:].decode("ascii"))
            except (RefError, UnicodeDecodeError):
                return None
    return None


@dataclass(frozen=True)
class Spliced:
    data: bytes
    replaced: bool
    previous_every: str | None
    outside_poll: bool


def splice(table: bytes | None, block: list[str]) -> Spliced:
    """`table` with the block appended, or replacing the existing one in place."""
    raw = table or b""
    _check_size(raw)
    lines = _split(raw)
    where = _locate(lines)
    new = [line.encode() for line in block]
    if where is None:
        body = raw if not raw or raw.endswith(b"\n") else raw + b"\n"
        return Spliced(body + b"\n".join(new) + b"\n", False, None, _has_poll(lines))
    start, end = where
    outside = lines[:start] + lines[end + 1 :]
    prior = previous_every(lines[start])
    if lines[start : end + 1] == new:
        return Spliced(raw, True, prior, _has_poll(outside))
    return Spliced(b"\n".join(lines[:start] + new + lines[end + 1 :]) + b"\n", True, prior,
                   _has_poll(outside))  # fmt: skip


def remove_block(table: bytes | None) -> tuple[bytes, list[bytes] | None]:
    """`table` without the block, and the block's lines (None when there was none)."""
    raw = table or b""
    _check_size(raw)
    lines = _split(raw)
    where = _locate(lines)
    if where is None:
        return raw, None
    rest = lines[: where[0]] + lines[where[1] + 1 :]
    return (b"\n".join(rest) + b"\n" if rest else b""), lines[where[0] : where[1] + 1]


def _has_poll(lines: list[bytes]) -> bool:
    return any(
        not line.lstrip().startswith(b"#") and _POLL_LINE.search(line) is not None for line in lines
    )


# --- the crontab program ----------------------------------------------------------------------


def _fail(message: str) -> CliError:
    return CliError("internal", message)


def _crontab(args: list[str], stdin: bytes | None = None) -> gateway.Result:
    try:
        result = gateway.run(
            ["crontab", *args],
            env=gateway.minimal_env(LC_ALL="C"),
            timeout=_CRONTAB_TIMEOUT,
            max_output=MAX_CRONTAB_BYTES + 1,
            stdin=stdin,
        )
    except gateway.LaunchError as exc:
        raise _fail(f"the crontab command cannot be run ({one_line(str(exc), 150)})") from None
    if result.timed_out:
        raise _fail("the crontab command timed out")
    if result.abandoned or result.returncode is None:
        raise _fail("the crontab command did not exit")
    return result


def _failure(result: gateway.Result) -> str:
    """The exit code only: crontab's stderr can echo crontab lines (which may hold secrets)."""
    return f"exit {result.returncode}; run `crontab -l` by hand to see why"


def _read() -> bytes | None:
    """The crontab as stored, or None when the user has none."""
    result = _crontab(["-l"])
    if result.too_large:
        raise _fail("the existing crontab is larger than 1 MiB; not touching it")
    if result.returncode == 0:
        return result.stdout.encode("utf-8", "surrogateescape")
    if result.returncode == 1 and "no crontab for" in result.stderr:
        return None
    raise _fail(f"cannot read the crontab ({_failure(result)})")


def _write(data: bytes) -> None:
    """Install `data`, or remove the crontab when `data` is empty."""
    removing = data == b""
    result = _crontab(["-r"] if removing else ["-"], None if removing else data)
    if result.returncode != 0 and not (removing and "no crontab for" in result.stderr):
        raise _fail(f"the crontab command failed; your crontab is unchanged ({_failure(result)})")


def _same(a: bytes | None, b: bytes | None) -> bool:
    return (a or b"") == (b or b"")


def _commit[T](
    state: StateDir, compute: Callable[[bytes | None], tuple[bytes, T]]
) -> tuple[bool, T]:
    """Under the cron lock: read, compute the new table, re-read to catch a concurrent editor,
    write, read back and compare. Returns whether it wrote and what `compute` returned."""
    lock_fd = _acquire_lock(state)
    try:
        for _ in range(_ATTEMPTS):
            before = _read()
            after, info = compute(before)
            if _same(before, after):
                return False, info
            if not _same(_read(), before):
                continue  # edited meanwhile: compute again from what is there now
            _write(after)
            if not _same(_read(), after):
                raise _fail("the crontab changed while it was being written; check it by hand")
            return True, info
        raise _fail("the crontab keeps changing under us; run the command again")
    finally:
        os.close(lock_fd)  # the only descriptor this process has on the lock file


def _preview[T](compute: Callable[[bytes | None], tuple[bytes, T]]) -> tuple[bool, T]:
    """`--dry-run`: one read and the same `compute`; returns whether a real run would write."""
    before = _read()
    after, info = compute(before)
    return not _same(before, after), info


def _acquire_lock(state: StateDir) -> int:
    """Open `cron.lock` once (never followed if a symlink, checked first with `lstat` so no second
    descriptor is opened on it) and take an exclusive `flock`, waiting at most `LOCK_WAIT`."""
    try:
        verify_path_at(state.fd, CRON_LOCK)
        fd = os.open(
            CRON_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=state.fd
        )
    except StateDirError as exc:
        raise CliError("state_unsafe", str(exc)) from None
    except OSError as exc:
        raise _fail(f"cannot open {CRON_LOCK} ({one_line(exc.strerror or '', 100)})") from None
    try:
        verify_file(os.fstat(fd), CRON_LOCK)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _fail(f"{CRON_LOCK} is held by another process; try again") from None
                time.sleep(_LOCK_TICK)
    except StateDirError as exc:
        os.close(fd)
        raise CliError("state_unsafe", str(exc)) from None
    except BaseException:
        os.close(fd)
        raise


# --- commands ---------------------------------------------------------------------------------


def _state(ctx: Context) -> StateDir:
    if ctx.state is None:
        raise _fail("the state directory is not open")
    return ctx.state


def install_command(ctx: Context, args: argparse.Namespace) -> int:
    config = ctx.loaded.config
    every: str = args.every or config.poll.interval_hint
    env_file = check_env_file(config.cron.env_file) if config.cron.env_file else None
    path_env = os.environ.get("PATH", os.defpath)
    binary = locate_binary(sys.argv[0] if sys.argv else "pr-watch", path_env)
    try:
        home = home_dir()
    except StateDirError as exc:
        raise CliError("state_unsafe", str(exc)) from None
    dirs, warnings = path_dirs(home, path_env)
    block = build_block(every, dirs, binary, env_file)
    _check_line_lengths(block, env_file)

    def compute(table: bytes | None) -> tuple[bytes, Spliced]:
        try:
            spliced = splice(table, block)
        except MalformedCrontab as exc:
            raise CliError("usage", str(exc), details={"kind": "malformed_crontab"}) from None
        return spliced.data, spliced

    if args.dry_run:
        changed, spliced = _preview(compute)
    else:
        changed, spliced = _commit(_state(ctx), compute)
    if spliced.outside_poll:
        warnings.append(
            "the crontab has a pr-watch poll entry outside the managed block; remove it so "
            "PRs are not polled twice"
        )
    warnings = [one_line(w, 1024) for w in warnings]
    for warning in warnings:
        _stderr_line(f"{PROG}: warning: {warning}\n")  # never silenced by --quiet
    verb = "updated" if spliced.replaced else "installed"
    if args.dry_run:
        head = (
            f"dry run: would {'update' if spliced.replaced else 'install'} the pr-watch cron block"
            if changed
            else "dry run: the pr-watch cron block is already current"
        )
    elif changed:
        head = f"{verb} the pr-watch cron block"
    else:
        head = "the pr-watch cron block is already current"
    ctx.emit(
        {
            "installed": True,
            "changed": changed,
            "every": every,
            "replaced": spliced.replaced,
            "previous_every": spliced.previous_every,
            "lines": block,
            "warnings": warnings,
            "dry_run": args.dry_run,
            "block": block if changed else None,
        },
        human="\n".join([f"{head} (every {every})", *block]),
    )
    return 0


def _text(lines: list[bytes]) -> list[str]:
    return [line.decode("utf-8", "replace") for line in lines]


def uninstall_command(ctx: Context, args: argparse.Namespace) -> int:
    def compute(table: bytes | None) -> tuple[bytes, list[bytes] | None]:
        try:
            return remove_block(table)
        except MalformedCrontab as exc:
            raise CliError("usage", str(exc), details={"kind": "malformed_crontab"}) from None

    if args.dry_run:
        changed, removed = _preview(compute)
    else:
        changed, removed = _commit(_state(ctx), compute)
    block = _text(removed) if changed and removed else None
    if args.dry_run:
        head = (
            "dry run: would remove the pr-watch cron block"
            if changed
            else "dry run: no pr-watch cron block to remove"
        )
    else:
        head = "removed the pr-watch cron block" if changed else "no pr-watch cron block to remove"
    ctx.emit(
        {"installed": False, "changed": changed, "dry_run": args.dry_run, "block": block},
        human="\n".join([head, *block]) if args.dry_run and block else head,
    )
    return 0
