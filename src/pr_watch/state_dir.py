"""The state directory: where it is, how it is created, and the checks that refuse unsafe ones.

docs/interface.md (Files): the directory is `$XDG_STATE_HOME/pr-watch` (only when absolute) or
`~/.local/state/pr-watch` (HOME must be absolute, else the passwd home is used), created mode 0700,
and refused (exit 12, `state_unsafe`) when its final component is a symlink, is not owned by the
user, or is group- or world-writable. At open, `hooks-cwd` (the hooks' empty working directory) is
created mode 0700 if missing and checked the same way, and `cron.lock` is checked when it exists.

The directory is opened once with `O_NOFOLLOW | O_DIRECTORY` and every check runs on that
descriptor (`fstat`), so a swap after the check cannot change what was checked. Files inside it
are opened relative to the descriptor (`dir_fd`), never by a path that could be re-pointed. Parent
directories of the state directory may be symlinks (a relocated `~/.local/state` is common); only
the final component is refused.

Other modules use `open_state_dir` for the descriptor, `StateDir.path_for` for a path SQLite needs,
and `verify_file` for any file they open.

`store.open_store` checks `state.db` and its sidecars with `verify_path_at` (docs/interface.md,
Files); it never opens them itself.
"""

from __future__ import annotations

import errno
import os
import pwd
import stat
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Final

from .text import one_line

_mkdir = os.mkdir  # seam: tests patch this name, not the shared `os` module

TOOL_DIR: Final = "pr-watch"
DIR_MODE: Final = 0o700
HOOKS_CWD: Final = "hooks-cwd"
CRON_LOCK: Final = "cron.lock"
_UNWRITABLE: Final = stat.S_IWGRP | stat.S_IWOTH
_geteuid = os.geteuid  # seam: tests patch this name, not the shared `os` module


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
        base = _home(env) / ".local" / "state"
    return base / TOOL_DIR


def home_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The home directory as the state directory rule reads it (`HOME` only when absolute, else
    the password-database home). Raises `StateDirError` when neither is usable."""
    return _home(os.environ if environ is None else environ)


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME", "")
    if home.startswith("/"):
        return Path(home)
    try:  # a missing or relative HOME is never used: that would put state in the cwd
        return Path(pwd.getpwuid(_geteuid()).pw_dir)
    except KeyError:
        raise StateDirError("cannot determine the home directory (HOME is not absolute)") from None


def _label(path: Path | str) -> str:
    return one_line(str(path), 200)


def verify_file(info: os.stat_result, what: str) -> None:
    """Refuse a file (from `fstat` of an open descriptor) that is not a regular file, is owned by
    another user, or is group- or world-writable."""
    if not stat.S_ISREG(info.st_mode):
        raise StateDirError(f"{one_line(what, 200)} is not a regular file")
    _check_owner_and_mode(info, what)


def verify_path_at(dir_fd: int, name: str) -> os.stat_result | None:
    """`verify_file` for `name` under `dir_fd` without opening it (`lstat`, so a symlink is
    refused as such). Returns the stat, or None when the file does not exist.

    Use this for a file SQLite or a lock holder keeps open: closing any descriptor of ours on an
    inode releases the POSIX locks the process holds on it, so such files must never be opened
    just to be checked. A swap after the check is caught by comparing device and inode later."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateDirError(
            f"{one_line(name, 200)} cannot be examined ({one_line(exc.strerror or '', 100)})"
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise StateDirError(f"{one_line(name, 200)} is a symlink")
    verify_file(info, name)
    return info


def _check_owner_and_mode(info: os.stat_result, what: str) -> None:
    if info.st_uid != _geteuid():
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
        _chmod_private(directory)  # exact mode even under a permissive umask


def _chmod_private(directory: Path) -> None:
    """`fchmod` 0700 on a descriptor opened without following a symlink (never `chmod(path)`,
    which would follow a symlink swapped in after the mkdir)."""
    fd = os.open(directory, _OPEN_FLAGS)
    try:
        os.fchmod(fd, DIR_MODE)
    finally:
        os.close(fd)


def open_state_dir(path: Path | None = None) -> StateDir:
    """Create (mode 0700) if missing, open without following a symlink, and verify.

    Raises `StateDirError` for an unsafe directory or one that cannot be created.
    """
    state = _open_state_dir(path, create=True)
    assert state is not None
    return state


def open_existing_state_dir(path: Path | None = None) -> StateDir | None:
    """Like `open_state_dir`, but nothing is created (read-only commands): a missing directory
    returns None, and a missing `hooks-cwd` is left missing. An existing one is fully verified."""
    return _open_state_dir(path, create=False)


def _open_state_dir(path: Path | None, *, create: bool) -> StateDir | None:
    target = default_state_dir() if path is None else Path(path)
    try:
        fd = os.open(target, _OPEN_FLAGS)
    except FileNotFoundError:
        if not create:
            return None
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
        _ensure_hooks_cwd(fd, create=create)
        _check_cron_lock(fd)
    except BaseException:
        os.close(fd)
        raise
    return StateDir(target, fd)


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # let the open below report why it cannot be used
    return True


def _ensure_hooks_cwd(state_fd: int, *, create: bool = True) -> None:
    """Create `hooks-cwd` (0700) under the state directory and verify it: a directory, not a
    symlink, owned by the user, not group- or world-writable. A missing one that cannot be created
    because the filesystem is read-only or access is denied (EROFS, EACCES) is left missing, and
    with `create=False` a missing one is left missing (an existing one is still verified)."""
    created = False
    try:
        if create:
            _mkdir(HOOKS_CWD, DIR_MODE, dir_fd=state_fd)
            created = True
        elif not _exists_at(state_fd, HOOKS_CWD):
            return
    except FileExistsError:
        pass
    except OSError as exc:
        if exc.errno in (errno.EROFS, errno.EACCES):
            return  # a read-only state dir is otherwise safe; the hook runner creates it or fails
        raise StateDirError(f"{HOOKS_CWD} cannot be created ({exc.strerror})") from None
    try:
        fd = os.open(HOOKS_CWD, _OPEN_FLAGS, dir_fd=state_fd)
    except OSError as exc:
        try:
            if stat.S_ISLNK(os.stat(HOOKS_CWD, dir_fd=state_fd, follow_symlinks=False).st_mode):
                raise StateDirError(f"{HOOKS_CWD} is a symlink") from None
        except OSError:
            pass
        if exc.errno in (errno.ENOTDIR, errno.ELOOP):
            raise StateDirError(f"{HOOKS_CWD} is not a directory") from None
        raise StateDirError(f"{HOOKS_CWD} cannot be opened ({exc.strerror})") from None
    try:
        if created:
            os.fchmod(fd, DIR_MODE)  # exact mode even under a permissive umask
        _check_owner_and_mode(os.fstat(fd), HOOKS_CWD)
    finally:
        os.close(fd)


def ensure_hooks_cwd(state: StateDir) -> Path:
    """The verified `<state dir>/hooks-cwd` for the hook runner: created (0700) when missing and
    checked like at start. Raises `StateDirError` when it cannot be created or is unsafe."""
    _ensure_hooks_cwd(state.fd, create=True)
    if not _exists_at(state.fd, HOOKS_CWD):  # a read-only or denied creation left it missing
        raise StateDirError(f"{HOOKS_CWD} is missing and cannot be created")
    return state.path_for(HOOKS_CWD)


def _check_cron_lock(state_fd: int) -> None:
    """When `cron.lock` exists it must be a regular file owned by the user, not group- or
    world-writable, and not a symlink (the cron commands lock it, #26). Checked with `lstat`
    semantics and no open: closing any descriptor on an inode drops the process's POSIX locks on
    it, so a check must never open a file another part of the process may lock."""
    verify_path_at(state_fd, CRON_LOCK)
