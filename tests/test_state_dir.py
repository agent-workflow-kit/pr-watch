"""State directory resolution, creation and the safety checks (docs/interface.md: Files)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

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
    monkeypatch.setattr(sd.os, "geteuid", lambda: other)
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
