"""The state directory: where it is, how it is created, and the checks that refuse unsafe ones.

docs/interface.md (Files): the directory is `$XDG_STATE_HOME/pr-watch` (only when absolute) or
`~/.local/state/pr-watch`, created mode 0700, and refused (exit 12, `state_unsafe`) when its final
component is a symlink, is not owned by the user, or is group- or world-writable.

The directory is opened once with `O_NOFOLLOW | O_DIRECTORY` and every check runs on that
descriptor (`fstat`), so a swap after the check cannot change what was checked. Files inside it
are opened relative to the descriptor (`dir_fd`), never by a path that could be re-pointed. Parent
directories of the state directory may be symlinks (a relocated `~/.local/state` is common); only
the final component is refused.

Other modules use `open_state_dir` for the descriptor, `StateDir.path_for` for a path SQLite needs
(#11 checks `state.db` with `verify_file`), and `verify_file` for any file they open.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Final

from .text import one_line

TOOL_DIR: Final = "pr-watch"
DIR_MODE: Final = 0o700
_UNWRITABLE: Final = stat.S_IWGRP | stat.S_IWOTH


class StateDirError(Exception):
    """The state directory is unsafe or cannot be created (`state_unsafe`, exit 12). The message
    is one sanitized line and never carries a file's content."""


def default_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """`$XDG_STATE_HOME/pr-watch` (only when absolute), else `~/.local/state/pr-watch`."""
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg.startswith("/"):
        base = Path(xdg)
    else:
        home = env.get("HOME") or os.path.expanduser("~")
        base = Path(home) / ".local" / "state"
    return base / TOOL_DIR


def _label(path: Path | str) -> str:
    return one_line(str(path), 200)


def verify_file(info: os.stat_result, what: str) -> None:
    """Refuse a file (from `fstat` of an open descriptor) that is not a regular file, is owned by
    another user, or is group- or world-writable."""
    if not stat.S_ISREG(info.st_mode):
        raise StateDirError(f"{one_line(what, 200)} is not a regular file")
    _check_owner_and_mode(info, what)


def _check_owner_and_mode(info: os.stat_result, what: str) -> None:
    if info.st_uid != os.geteuid():
        raise StateDirError(f"{one_line(what, 200)} is owned by another user")
    if info.st_mode & _UNWRITABLE:
        raise StateDirError(f"{one_line(what, 200)} is group- or world-writable")


class StateDir:
    """An open, verified state directory. `fd` is a directory descriptor (close-on-exec)."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self.fd: int = fd
        self._open = True

    def path_for(self, name: str) -> Path:
        """`<state dir>/<name>` for one plain file name (never a path with separators)."""
        if not name or name in (".", "..") or "/" in name or "\0" in name:
            raise ValueError(f"not a plain file name: {name!r}")
        return self.path / name

    def close(self) -> None:
        if self._open:
            self._open = False
            os.close(self.fd)

    def __enter__(self) -> StateDir:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


_OPEN_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _refuse_open(path: Path, exc: OSError) -> StateDirError:
    """The error for a failed open of the final component, telling a symlink from other causes."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return StateDirError(f"state directory {_label(path)} is a symlink")
    except OSError:
        pass
    if exc.errno in (errno.ENOTDIR, errno.ELOOP):
        return StateDirError(f"state directory {_label(path)} is not a directory")
    return StateDirError(f"state directory {_label(path)} cannot be opened ({exc.strerror})")


def _create(path: Path) -> None:
    """Create `path` and any missing parents, each mode 0700 (independent of the umask)."""
    missing: list[Path] = []
    probe = path
    while not os.path.lexists(probe):
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, DIR_MODE)
        except FileExistsError:
            continue  # created by a concurrent process; opened and verified next
        os.chmod(directory, DIR_MODE)  # exact mode even under a permissive umask


def open_state_dir(path: Path | None = None) -> StateDir:
    """Create (mode 0700) if missing, open without following a symlink, and verify.

    Raises `StateDirError` for an unsafe directory or one that cannot be created.
    """
    target = default_state_dir() if path is None else Path(path)
    try:
        fd = os.open(target, _OPEN_FLAGS)
    except FileNotFoundError:
        try:
            _create(target)
        except OSError as exc:
            raise StateDirError(
                f"state directory {_label(target)} cannot be created ({exc.strerror})"
            ) from None
        try:
            fd = os.open(target, _OPEN_FLAGS)
        except OSError as exc:
            raise _refuse_open(target, exc) from None
    except OSError as exc:
        raise _refuse_open(target, exc) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise StateDirError(f"state directory {_label(target)} is not a directory")
        _check_owner_and_mode(info, f"state directory {_label(target)}")
    except BaseException:
        os.close(fd)
        raise
    return StateDir(target, fd)
