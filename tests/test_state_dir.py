"""State directory resolution, creation and the safety checks (docs/interface.md: Files)."""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path
from typing import Any

import pytest

from pr_watch import state_dir as sd
from pr_watch.state_dir import StateDirError, default_state_dir, open_state_dir


def test_default_uses_xdg_state_home_when_absolute(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "xs"), "HOME": str(tmp_path / "h")}
    assert default_state_dir(env) == tmp_path / "xs" / "pr-watch"


@pytest.mark.parametrize("xdg", ["", "relative/dir", "~/x"])
def test_default_ignores_a_missing_or_relative_xdg_state_home(tmp_path: Path, xdg: str) -> None:
    env = {"XDG_STATE_HOME": xdg, "HOME": str(tmp_path / "h")}
    assert default_state_dir(env) == tmp_path / "h" / ".local" / "state" / "pr-watch"


@pytest.mark.parametrize("home", ["", "relative/home", "~", "."])
def test_default_never_uses_a_relative_home(home: str) -> None:
    passwd_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    env = {"HOME": home}
    assert default_state_dir(env) == passwd_home / ".local" / "state" / "pr-watch"
    assert default_state_dir({}) == passwd_home / ".local" / "state" / "pr-watch"


def test_default_reads_the_sandboxed_environment(isolated_env: Path) -> None:
    assert default_state_dir() == isolated_env / ".local" / "state" / "pr-watch"


def test_state_dir_fixture_is_the_default_location(state_dir: Path) -> None:
    assert state_dir == default_state_dir()
    assert not state_dir.exists()


def test_creates_missing_directories_private(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "pr-watch"
    with open_state_dir(target) as state:
        assert state.path == target
    for part in (tmp_path / "a", tmp_path / "a" / "b", target):
        assert stat.S_IMODE(part.stat().st_mode) == 0o700


def test_creation_is_private_whatever_the_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        with open_state_dir(tmp_path / "pr-watch"):
            pass
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "pr-watch").stat().st_mode) == 0o700


def test_existing_private_directory_is_accepted_and_untouched(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "keep").write_text("x")
    with open_state_dir(target) as state:
        assert state.path == target
    assert (target / "keep").read_text() == "x"


def test_read_only_for_others_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir()
    target.chmod(0o755)
    with open_state_dir(target):
        pass


@pytest.mark.parametrize("mode", [0o770, 0o720, 0o707, 0o702, 0o777])
def test_group_or_world_writable_is_refused(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir()
    target.chmod(mode)
    with pytest.raises(StateDirError, match="writable"):
        open_state_dir(target)


def test_symlink_to_a_directory_is_refused_and_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "pr-watch"
    link.symlink_to(real)
    with pytest.raises(StateDirError, match="symlink"):
        open_state_dir(link)
    assert list(real.iterdir()) == []


def test_dangling_symlink_is_refused_not_created_through(tmp_path: Path) -> None:
    link = tmp_path / "pr-watch"
    link.symlink_to(tmp_path / "nowhere")
    with pytest.raises(StateDirError, match="symlink"):
        open_state_dir(link)
    assert not (tmp_path / "nowhere").exists()


def test_a_file_in_the_way_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.write_text("x")
    with pytest.raises(StateDirError, match="not a directory"):
        open_state_dir(target)


def test_a_file_as_parent_is_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    with pytest.raises(StateDirError):
        open_state_dir(tmp_path / "f" / "pr-watch")


def test_other_owner_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    other = os.geteuid() + 1
    monkeypatch.setattr(sd, "_geteuid", lambda: other)
    with pytest.raises(StateDirError, match="owned by another user"):
        open_state_dir(target)


def test_unwritable_parent_is_a_state_error_not_a_crash(tmp_path: Path) -> None:
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        if os.access(parent, os.W_OK):
            pytest.skip("running with privileges that ignore directory modes")
        with pytest.raises(StateDirError, match="cannot be created"):
            open_state_dir(parent / "pr-watch")
    finally:
        parent.chmod(0o700)


def test_hostile_path_text_never_reaches_the_message_raw(tmp_path: Path) -> None:
    bad = tmp_path / "pr\x1b[31m-watch‮"
    bad.write_text("x")
    with pytest.raises(StateDirError) as info:
        open_state_dir(bad)
    assert "\x1b" not in str(info.value)
    assert "‮" not in str(info.value)


def test_path_for_joins_a_plain_name_only(tmp_path: Path) -> None:
    with open_state_dir(tmp_path / "pr-watch") as state:
        assert state.path_for("state.db") == tmp_path / "pr-watch" / "state.db"
        for bad in ("", ".", "..", "a/b", "/abs", "../x", "a\0b"):
            with pytest.raises(ValueError):
                state.path_for(bad)


def test_close_is_idempotent_and_the_fd_is_closed(tmp_path: Path) -> None:
    state = open_state_dir(tmp_path / "pr-watch")
    fd = state.fd
    state.close()
    state.close()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_the_descriptor_is_not_inherited(tmp_path: Path) -> None:
    with open_state_dir(tmp_path / "pr-watch") as state:
        assert not os.get_inheritable(state.fd)


def test_second_open_of_an_existing_dir_does_not_race_creation(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    with open_state_dir(target), open_state_dir(target):
        pass


def test_verify_file_accepts_private_and_refuses_unsafe(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    f.chmod(0o600)
    sd.verify_file(os.stat(f), "f")
    f.chmod(0o660)
    with pytest.raises(StateDirError, match="writable"):
        sd.verify_file(os.stat(f), "f")
    f.chmod(0o600)
    (tmp_path / "d").mkdir()
    with pytest.raises(StateDirError, match="regular file"):
        sd.verify_file(os.stat(tmp_path / "d"), "d")


# --- hooks-cwd and cron.lock -----------------------------------------------------------------


def test_hooks_cwd_is_created_private_whatever_the_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        with open_state_dir(tmp_path / "pr-watch"):
            pass
    finally:
        os.umask(old)
    hooks = tmp_path / "pr-watch" / "hooks-cwd"
    assert hooks.is_dir() and stat.S_IMODE(hooks.stat().st_mode) == 0o700
    assert list(hooks.iterdir()) == []


def test_an_existing_private_hooks_cwd_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "hooks-cwd").mkdir(mode=0o700)
    with open_state_dir(target):
        pass
    with open_state_dir(target):
        pass


def test_a_symlinked_hooks_cwd_is_refused_and_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (target / "hooks-cwd").symlink_to(real)
    with pytest.raises(StateDirError, match="hooks-cwd is a symlink"):
        open_state_dir(target)
    assert stat.S_IMODE(real.stat().st_mode) == 0o700 and list(real.iterdir()) == []


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_a_writable_hooks_cwd_is_refused(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "hooks-cwd").mkdir()
    (target / "hooks-cwd").chmod(mode)
    with pytest.raises(StateDirError, match="hooks-cwd is group- or world-writable"):
        open_state_dir(target)


def test_a_foreign_hooks_cwd_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "hooks-cwd").mkdir(mode=0o700)
    calls = {"n": 0}
    real = os.geteuid()

    def euid() -> int:
        calls["n"] += 1  # the state dir check passes, the hooks-cwd check sees another owner
        return real if calls["n"] == 1 else real + 1

    monkeypatch.setattr(sd, "_geteuid", euid)
    with pytest.raises(StateDirError, match="hooks-cwd is owned by another user"):
        open_state_dir(target)


def test_a_file_in_place_of_hooks_cwd_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "hooks-cwd").write_text("x")
    with pytest.raises(StateDirError, match="hooks-cwd"):
        open_state_dir(target)


def test_a_private_cron_lock_or_none_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    with open_state_dir(target):
        pass
    (target / "cron.lock").write_text("")
    (target / "cron.lock").chmod(0o600)
    with open_state_dir(target):
        pass
    assert not (tmp_path / "pr-watch" / "nothing").exists()


def test_cron_lock_is_not_created_by_the_check(tmp_path: Path) -> None:
    with open_state_dir(tmp_path / "pr-watch"):
        pass
    assert not (tmp_path / "pr-watch" / "cron.lock").exists()


def test_a_symlinked_cron_lock_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (tmp_path / "real-lock").write_text("")
    (target / "cron.lock").symlink_to(tmp_path / "real-lock")
    with pytest.raises(StateDirError, match="cron.lock is a symlink"):
        open_state_dir(target)


@pytest.mark.parametrize("mode", [0o660, 0o606])
def test_a_writable_cron_lock_is_refused(tmp_path: Path, mode: int) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "cron.lock").write_text("")
    (target / "cron.lock").chmod(mode)
    with pytest.raises(StateDirError, match="cron.lock is group- or world-writable"):
        open_state_dir(target)


def test_a_directory_as_cron_lock_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "cron.lock").mkdir()
    with pytest.raises(StateDirError, match="cron.lock is not a regular file"):
        open_state_dir(target)


def test_a_foreign_cron_lock_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "pr-watch"
    target.mkdir(mode=0o700)
    (target / "cron.lock").write_text("")
    (target / "cron.lock").chmod(0o600)
    calls = {"n": 0}
    real = os.geteuid()

    def euid() -> int:
        calls["n"] += 1  # state dir and hooks-cwd pass, cron.lock sees another owner
        return real if calls["n"] <= 2 else real + 1

    monkeypatch.setattr(sd, "_geteuid", euid)
    with pytest.raises(StateDirError, match="cron.lock is owned by another user"):
        open_state_dir(target)


def test_creation_does_not_follow_a_symlink_swapped_in_after_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pr-watch"
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    real_mkdir = os.mkdir

    def swapping(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real_mkdir(path, mode) if dir_fd is None else real_mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is None and Path(path) == target:
            os.rmdir(path)
            os.symlink(victim, path)

    monkeypatch.setattr(sd.os, "mkdir", swapping)
    with pytest.raises(StateDirError):
        open_state_dir(target)
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
