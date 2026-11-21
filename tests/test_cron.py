"""`cron install` and `cron uninstall` against the fake crontab in tests/fakebin.

The real crontab is unreachable here (see tests/conftest.py and tests/test_fake_crontab.py): the
fake keeps its table in `$HOME/.fake-crontab`, and `table()` reads it back byte for byte.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pr_watch import cron, gateway
from pr_watch.cli import CliError, main
from pr_watch.gateway import Request, Result
from pr_watch.state_dir import open_state_dir
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "cron"
BLOCK_START = b"# pr-watch managed"
BLOCK_END = b"# pr-watch managed end"


@dataclass
class Env:
    home: Path
    forgectl_dir: Path
    pr_watch: str

    @property
    def table_file(self) -> Path:
        return self.home / ".fake-crontab"

    def table(self) -> bytes | None:
        try:
            return self.table_file.read_bytes()
        except FileNotFoundError:
            return None

    def set_table(self, data: bytes) -> None:
        self.table_file.write_bytes(data)

    def golden(self, name: str, env_file: Path | None = None) -> bytes:
        text = (GOLDEN / name).read_text(encoding="utf-8")
        for key, value in {
            "{HOME}": str(self.home),
            "{FORGECTL_DIR}": str(self.forgectl_dir),
            "{PR_WATCH}": self.pr_watch,
            "{ENV_FILE}": str(env_file or ""),
        }.items():
            text = text.replace(key, value)
        return text.encode()


@pytest.fixture(autouse=True)
def env(
    isolated_env: Path, tmp_path: Path, sandbox_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> Env:
    """A safe forgectl directory first on PATH (the repo's tests/fakebin mode is the checkout's)."""
    forgectl_dir = tmp_path / "forgectl-bin"
    forgectl_dir.mkdir()
    forgectl_dir.chmod(0o755)
    tool = forgectl_dir / "forgectl"
    tool.write_text("#!/bin/sh\nexit 98\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(forgectl_dir), os.environ["PATH"]]))
    assert shutil.which("pr-watch") == str(sandbox_bin / "pr-watch")
    return Env(isolated_env, forgectl_dir, str(sandbox_bin / "pr-watch"))


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_config(env: Env, text: str) -> None:
    path = env.home / ".config" / "pr-watch" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def make_env_file(tmp_path: Path, name: str = "pr-watch.env") -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    path = directory / name
    path.write_text("export X=1\n")
    path.chmod(0o600)
    return path


def installed(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    code, out, err = run(capsys, "--json", "cron", "install", *argv)
    assert code == 0, (out, err)
    doc = json.loads(out)
    assert_valid("cli/cron-install", doc)
    return doc


def refused(capsys: pytest.CaptureFixture[str], *argv: str, code: str = "usage") -> dict[str, Any]:
    status, out, err = run(capsys, "--json", *argv)
    assert status == 2, (out, err)
    doc = json.loads(out)["error"]
    assert doc["code"] == code
    return doc


# --- goldens ----------------------------------------------------------------------------------


def test_install_into_no_crontab_writes_the_golden_block(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = installed(capsys)
    assert env.table() == env.golden("install-fresh.crontab")
    assert doc == {
        "installed": True,
        "changed": True,
        "every": "5m",
        "replaced": False,
        "previous_every": None,
        "lines": env.golden("install-fresh.crontab").decode().splitlines(),
        "warnings": [],
        "dry_run": False,
        "block": env.golden("install-fresh.crontab").decode().splitlines(),
    }


def test_install_with_an_env_file_golden(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = make_env_file(tmp_path)
    write_config(env, f"cron:\n  env_file: {path}\n")
    installed(capsys, "--every", "1h")
    assert env.table() == env.golden("install-env-file.crontab", path)


def test_foreign_entries_are_kept_byte_for_byte_and_the_block_is_appended(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.set_table(env.golden("foreign-before.crontab"))
    installed(capsys)
    assert env.table() == env.golden("install-foreign.crontab")


def test_odd_foreign_bytes_survive(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    # CRLF line ends, a non-UTF-8 byte, no final newline: all kept; one newline joins the block
    before = b"MAILTO=x\r\n0 1 * * * echo \xff\xfe\r\n# last"
    env.set_table(before)
    installed(capsys)
    assert env.table() == before + b"\n" + env.golden("install-fresh.crontab")
    run(capsys, "cron", "uninstall")
    assert env.table() == before + b"\n"


def test_a_foreign_tail_without_a_final_newline_gains_only_that_newline(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    # cron refuses a table that does not end in a newline, so the written table always does: the
    # one byte added to foreign text. Nothing else about the tail changes.
    block = env.golden("install-fresh.crontab")
    env.set_table(block + b"0 1 * * * tail\r\n# last")
    installed(capsys, "--every", "10m")
    assert (
        env.table()
        == env.golden("install-fresh.crontab").replace(b"5m", b"10m").replace(b"*/5", b"*/10")
        + b"0 1 * * * tail\r\n# last\n"
    )
    env.set_table(block + b"# last")
    run(capsys, "cron", "uninstall")
    assert env.table() == b"# last\n"


def test_reinstall_replaces_the_block_in_place_and_keeps_neighbours(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.set_table(env.golden("reinstall-before.crontab"))
    doc = installed(capsys, "--every", "10m")
    assert env.table() == env.golden("reinstall-after.crontab")
    assert (doc["replaced"], doc["changed"], doc["previous_every"]) == (True, True, "15m")
    assert doc["every"] == "10m"


def test_reinstalling_an_identical_block_is_replaced_but_unchanged_and_writes_nothing(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(capsys)
    first = env.table()
    (env.home / ".fake-crontab-fail-writes").touch()  # a write now would fail the command
    doc = installed(capsys)
    assert (doc["replaced"], doc["changed"], doc["previous_every"]) == (True, False, "5m")
    assert env.table() == first


def test_uninstall_removes_only_the_block(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.set_table(env.golden("reinstall-before.crontab"))
    code, out, _ = run(capsys, "--json", "cron", "uninstall")
    doc = json.loads(out)
    assert_valid("cli/cron-uninstall", doc)
    assert (code, {k: doc[k] for k in ("installed", "changed", "dry_run")}) == (
        0,
        {"installed": False, "changed": True, "dry_run": False},
    )
    assert doc["block"][0].startswith("# pr-watch managed v1")
    assert env.table() == env.golden("uninstall-after.crontab")


def test_uninstall_of_a_block_only_crontab_removes_the_crontab(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(capsys)
    assert run(capsys, "cron", "uninstall")[0] == 0
    assert env.table() is None  # `crontab -r`, not an empty table


@pytest.mark.parametrize("before", [None, b"", b"0 3 * * * /bin/backup\n"])
def test_uninstall_without_a_block_is_a_no_op(
    env: Env, capsys: pytest.CaptureFixture[str], before: bytes | None
) -> None:
    if before is not None:
        env.set_table(before)
    code, out, _ = run(capsys, "--json", "cron", "uninstall")
    assert (code, json.loads(out)) == (
        0,
        {"installed": False, "changed": False, "dry_run": False, "block": None},
    )
    assert env.table() == before


def test_install_then_uninstall_round_trips_a_foreign_crontab(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    before = env.golden("foreign-before.crontab")
    env.set_table(before)
    installed(capsys)
    run(capsys, "cron", "uninstall")
    assert env.table() == before


# --- block recognition and malformed crontabs -------------------------------------------------


def test_previous_every_is_null_for_a_bare_or_unparsable_header(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    for header in (
        b"# pr-watch managed",
        b"# pr-watch managed v1",
        b"# pr-watch managed v1 every=7m",
        b"# pr-watch managed v1 every=5m extra",
    ):
        env.set_table(header + b"\n*/5 * * * * x poll\n# pr-watch managed end\n")
        doc = installed(capsys)
        assert (doc["replaced"], doc["previous_every"]) == (
            True,
            "5m" if header.endswith(b"extra") else None,
        ), header


def test_recognizer_ignores_trailing_blanks_and_cr_and_checks_the_end_line_first(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.set_table(
        b"# pr-watch managed v1 every=5m \t\r\n*/5 * * * * x poll\r\n# pr-watch managed end  \r\n"
    )
    assert installed(capsys)["replaced"] is True
    assert env.table() == env.golden("install-fresh.crontab")


@pytest.mark.parametrize("line", [b"# pr-watch managedX", b"#pr-watch managed", b"# pr-watch"])
def test_other_comments_are_foreign(
    env: Env, capsys: pytest.CaptureFixture[str], line: bytes
) -> None:
    env.set_table(line + b"\n")
    assert installed(capsys)["replaced"] is False
    assert env.table() == line + b"\n" + env.golden("install-fresh.crontab")


MALFORMED = {
    "no-end": b"# pr-watch managed v1 every=5m\nPATH=/x\n*/5 * * * * x poll\n",
    "no-end-bare": b"0 1 * * * a\n# pr-watch managed\n",
    "end-only": b"0 1 * * * a\n# pr-watch managed end\n",
    "two-blocks": (
        b"# pr-watch managed\n* * * * * a\n# pr-watch managed end\n"
        b"# pr-watch managed\n* * * * * b\n# pr-watch managed end\n"
    ),
    "start-inside": (
        b"# pr-watch managed v1 every=5m\n# pr-watch managed v1 every=1h\n# pr-watch managed end\n"
    ),
    "end-then-start": b"# pr-watch managed end\n# pr-watch managed\n* * * * * a\n",
}


SHAPE_MESSAGES = {
    "no-end": "but no '# pr-watch managed end' line",
    "no-end-bare": "but no '# pr-watch managed end' line",
    "end-only": "with no '# pr-watch managed' start line",
    "two-blocks": "more than one '# pr-watch managed' block",
    "start-inside": "more than one '# pr-watch managed' block",
    "end-then-start": "line before its start line",
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_a_malformed_block_is_never_touched_and_the_message_names_its_shape(
    env: Env, capsys: pytest.CaptureFixture[str], name: str, command: str
) -> None:
    env.set_table(MALFORMED[name])
    (env.home / ".fake-crontab-fail-writes").touch()
    error = refused(capsys, "cron", command)
    assert error["details"]["kind"] == "malformed_crontab"
    assert SHAPE_MESSAGES[name] in error["message"]
    assert "by hand" in error["message"]
    assert env.table() == MALFORMED[name]


def test_a_malformed_crontab_is_refused_without_any_write(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str]
) -> None:
    backend.table = MALFORMED["no-end"]
    refused(capsys, "cron", "install")
    assert backend.table == MALFORMED["no-end"]
    assert backend.writes() == [] and ("crontab", "-r") not in [c.argv for c in backend.calls]


def test_a_nul_byte_or_an_oversized_crontab_is_not_touched(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.set_table(b"# a\0b\n")
    code, out, _ = run(capsys, "--json", "cron", "install")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert env.table() == b"# a\0b\n"
    big = b"# " + b"x" * (cron.MAX_CRONTAB_BYTES + 1) + b"\n"
    env.set_table(big)
    assert run(capsys, "cron", "uninstall")[0] == 1
    assert env.table() == big


# --- --every ----------------------------------------------------------------------------------

SCHEDULES = {
    "1m": "* * * * *",
    "2m": "*/2 * * * *",
    "5m": "*/5 * * * *",
    "10m": "*/10 * * * *",
    "15m": "*/15 * * * *",
    "30m": "*/30 * * * *",
    "60m": "0 * * * *",
    "1h": "0 * * * *",
    "2h": "0 */2 * * *",
    "6h": "0 */6 * * *",
    "12h": "0 */12 * * *",
    "24h": "0 0 * * *",
}


@pytest.mark.parametrize("every", sorted(SCHEDULES))
def test_every_forms_map_to_cron_schedules(
    env: Env, capsys: pytest.CaptureFixture[str], every: str
) -> None:
    assert cron.schedule(every) == SCHEDULES[every]
    doc = installed(capsys, "--every", every)
    assert doc["every"] == every
    assert doc["lines"][0] == f"# pr-watch managed v1 every={every}"
    assert doc["lines"][2].startswith(SCHEDULES[every] + " ")


@pytest.mark.parametrize(
    "value",
    ["7m", "0m", "61m", "90m", "5", "5s", "1h5m", "25h", "5h", "-5m", "+5m", "m", "", "5M", "1d",
     "5m\n", " 5m", "5 m", "٥m", "05m", "1.5h", "5m;ls"],
)  # fmt: skip
def test_bad_every_is_a_usage_error_and_writes_nothing(
    env: Env, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    refused(capsys, "cron", "install", "--every", value)
    assert env.table() is None


def test_every_wins_over_the_interval_hint_which_is_the_default(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(env, "poll:\n  interval_hint: 15m\n")
    assert installed(capsys)["every"] == "15m"
    assert installed(capsys, "--every", "2h")["every"] == "2h"
    assert env.table() is not None and b"every=2h" in env.table()  # type: ignore[operator]


# --- paths and PATH ---------------------------------------------------------------------------


def test_the_command_is_the_absolute_pr_watch_path(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = installed(capsys)
    entry = doc["lines"][2]
    assert entry.endswith(f"'{env.pr_watch}' poll --quiet") and env.pr_watch.startswith("/")


def test_locate_binary_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = tmp_path / "bin" / "pr-watch"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    other = tmp_path / "elsewhere"
    other.mkdir()
    path_env = f"{other}:{tool.parent}"
    assert cron.locate_binary(str(tool), path_env) == str(tool)
    assert cron.locate_binary("pr-watch", path_env) == str(tool)  # a bare name: found on PATH
    monkeypatch.chdir(tool.parent)
    assert cron.locate_binary("./pr-watch", "") == str(tool)  # relative to the cwd
    # `python -m pr_watch`: argv[0] is the module file, so the installed script is looked up
    assert cron.locate_binary("/x/pr_watch/__main__.py", path_env) == str(tool)
    with pytest.raises(CliError) as exc:
        cron.locate_binary("/x/pr_watch/__main__.py", str(other))
    assert exc.value.code == "internal"


def test_locate_binary_refuses_a_path_that_cannot_go_in_a_crontab(tmp_path: Path) -> None:
    for name in ("with space", "q'uote", "per%cent", "semi;colon", "new\nline", "dollar$x"):
        tool = tmp_path / name / "pr-watch"
        tool.parent.mkdir()
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        with pytest.raises(CliError) as exc:
            cron.locate_binary(str(tool), "")
        assert (exc.value.code, exc.value.details) == ("usage", {"kind": "unsafe_path"}), name


def test_locate_binary_refuses_a_non_executable(tmp_path: Path) -> None:
    tool = tmp_path / "pr-watch"
    tool.write_text("x")
    tool.chmod(0o644)
    with pytest.raises(CliError):
        cron.locate_binary(str(tool), "")


def test_path_dirs_holds_local_bin_forgectl_and_the_system_dirs(env: Env, tmp_path: Path) -> None:
    dirs, warnings = cron.path_dirs(env.home, str(env.forgectl_dir))
    assert dirs == [
        f"{env.home}/.local/bin",
        str(env.forgectl_dir),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    assert warnings == []


def test_path_dirs_skips_a_forgectl_that_is_not_on_path_with_a_warning(
    env: Env, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    dirs, warnings = cron.path_dirs(env.home, str(empty))
    assert dirs == [f"{env.home}/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    assert len(warnings) == 1 and "forgectl" in warnings[0]


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o722])
def test_a_group_or_world_writable_forgectl_dir_is_left_out_with_a_warning(
    env: Env, capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    env.forgectl_dir.chmod(mode)
    doc = installed(capsys)
    assert str(env.forgectl_dir) not in doc["lines"][1]
    assert len(doc["warnings"]) == 1 and "writable" in doc["warnings"][0]
    assert str(env.forgectl_dir) in doc["warnings"][0] or "forgectl" in doc["warnings"][0]


def test_a_forgectl_dir_owned_by_another_user_is_left_out(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cron, "_geteuid", lambda: os.geteuid() + 1)
    dirs, warnings = cron.path_dirs(env.home, str(env.forgectl_dir))
    assert str(env.forgectl_dir) not in dirs and "owned" in warnings[0]


def test_a_root_owned_forgectl_dir_is_kept(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    class Info:
        st_uid = 0
        st_mode = 0o40755

    real = os.stat
    monkeypatch.setattr(
        cron.os,
        "stat",
        lambda p, *a, **k: Info() if str(p) == str(env.forgectl_dir) else real(p, *a, **k),
    )
    monkeypatch.setattr(cron, "_geteuid", lambda: os.geteuid() + 1)
    dirs, _ = cron.path_dirs(env.home, str(env.forgectl_dir))
    assert str(env.forgectl_dir) in dirs


@pytest.mark.parametrize("name", ["with space", "q'uote", "per%cent", "semi;colon", "d$x", "b`t"])
def test_a_forgectl_dir_with_unsafe_characters_is_left_out(
    env: Env, tmp_path: Path, name: str
) -> None:
    odd = tmp_path / name
    odd.mkdir(mode=0o755)
    odd.chmod(0o755)
    (odd / "forgectl").write_text("#!/bin/sh\n")
    (odd / "forgectl").chmod(0o755)
    dirs, warnings = cron.path_dirs(env.home, str(odd))
    assert str(odd) not in dirs and len(warnings) == 1


def test_an_unsafe_existing_local_bin_is_left_out(env: Env) -> None:
    local = env.home / ".local" / "bin"
    local.mkdir(parents=True)
    local.chmod(0o777)
    dirs, warnings = cron.path_dirs(env.home, str(env.forgectl_dir))
    assert str(local) not in dirs and "writable" in warnings[0]
    local.chmod(0o755)
    assert str(local) in cron.path_dirs(env.home, str(env.forgectl_dir))[0]


def test_a_home_that_cannot_be_written_to_path_is_skipped_with_a_warning(
    env: Env, tmp_path: Path
) -> None:
    spaced = tmp_path / "Jo Smith"
    dirs, warnings = cron.path_dirs(spaced, str(env.forgectl_dir))
    assert not any("Jo" in d for d in dirs) and len(warnings) == 1


def test_a_forgectl_found_through_a_relative_path_entry_is_left_out_with_a_warning(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(env.forgectl_dir.parent)
    dirs, warnings = cron.path_dirs(env.home, f"relative-nothing:{env.forgectl_dir.name}")
    assert str(env.forgectl_dir) not in dirs and not any(not d.startswith("/") for d in dirs)
    assert len(warnings) == 1 and "forgectl" in warnings[0] and "relative" in warnings[0]


def test_a_colon_in_a_path_dir_is_left_out_with_a_warning(env: Env, tmp_path: Path) -> None:
    odd = tmp_path / "a:b"
    odd.mkdir(mode=0o755)
    assert cron._dir_problem(str(odd)) is not None
    dirs, warnings = cron.path_dirs(odd, str(env.forgectl_dir))
    assert not any(":" in d for d in dirs)
    assert len(warnings) == 1 and "left out of PATH" in warnings[0]


def test_a_colon_is_allowed_in_the_env_file_path(env: Env, tmp_path: Path) -> None:
    path = make_env_file(tmp_path, "a:b.env")
    assert cron.check_env_file(str(path)) == str(path)


def test_the_path_line_is_one_variable_even_when_home_has_a_colon(env: Env, tmp_path: Path) -> None:
    odd = tmp_path / "x:y"
    dirs, _ = cron.path_dirs(odd, str(env.forgectl_dir))
    line = cron.build_block("5m", dirs, env.pr_watch, None)[1]
    assert line == "PATH=" + ":".join([str(env.forgectl_dir), "/usr/local/bin", "/usr/bin", "/bin"])


def _deep_dir(tmp_path: Path, depth: int) -> Path:
    path = tmp_path / "deep"
    for index in range(depth):
        path = path / (f"{index}" * 200)
    path.mkdir(parents=True, mode=0o700)
    for parent in path.relative_to(tmp_path).parents:
        (tmp_path / parent).chmod(0o700)
    return path


def test_a_block_line_over_1000_bytes_is_refused(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _deep_dir(tmp_path, 5)
    path = directory / "pr-watch.env"
    path.write_text("export X=1\n")
    path.chmod(0o600)
    write_config(env, f"cron:\n  env_file: {path}\n")
    env.set_table(b"# keep\n")
    doc = refused(capsys, "cron", "install", code="config_invalid")
    assert "cron.env_file" in doc["message"] and str(path) not in doc["message"]
    assert env.table() == b"# keep\n"


def test_the_longest_block_line_is_999_bytes() -> None:
    every = "15m"
    dirs = ["/usr/bin"]
    probe = cron.build_block(every, dirs, "/bin/pr-watch", "x")[2].encode()
    overhead = len(probe) - 1  # the entry without the env file path itself
    exact = "x" * (cron.MAX_LINE_BYTES - overhead)
    block = cron.build_block(every, dirs, "/bin/pr-watch", exact)
    assert len(block[2].encode()) == 999
    cron._check_line_lengths(block, exact)  # accepted
    too_long = exact + "x"
    block = cron.build_block(every, dirs, "/bin/pr-watch", too_long)
    assert len(block[2].encode()) == 1000
    with pytest.raises(CliError) as caught:
        cron._check_line_lengths(block, too_long)
    assert caught.value.code == "config_invalid"


def test_a_long_pr_watch_path_or_path_line_is_refused(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    long_home = _deep_dir(tmp_path, 5)
    monkeypatch.setenv("HOME", str(long_home))
    error = refused(capsys, "cron", "install")
    assert error["details"]["kind"] == "unsafe_path"
    assert env.table() is None


# --- env_file ---------------------------------------------------------------------------------


def test_env_file_valid_is_quoted_in_the_entry(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = make_env_file(tmp_path)
    write_config(env, f"cron:\n  env_file: {path}\n")
    doc = installed(capsys)
    assert doc["lines"][2] == f"*/5 * * * * . '{path}' && '{env.pr_watch}' poll --quiet"


def bad_env_files(tmp_path: Path) -> dict[str, str]:
    good = make_env_file(tmp_path, "ok.env")
    odd = tmp_path / "odd"
    odd.mkdir(mode=0o700)
    cases: dict[str, str] = {}
    for label, name in {
        "space": "a b.env",
        "single-quote": "a'b.env",
        "double-quote": 'a"b.env',
        "percent": "a%b.env",
        "semicolon": "a;b.env",
        "dollar": "a$b.env",
        "backtick": "a`b.env",
        "paren": "a(b.env",
        "backslash": "a\\b.env",
        "injection": "x'; touch pwned; '.env",
        "tab": "a\tb.env",
        "ampersand": "a&b.env",
        "pipe": "a|b.env",
        "hash": "a#b.env",
        "unicode-space": "a b.env",
    }.items():
        path = odd / name
        path.write_text("x")
        path.chmod(0o600)
        cases[label] = str(path)
    cases["relative"] = "relative.env"
    cases["tilde"] = "~/x.env"
    cases["missing"] = str(tmp_path / "secrets" / "missing.env")
    cases["directory"] = str(tmp_path / "secrets")
    link = tmp_path / "secrets" / "link.env"
    link.symlink_to(good)
    cases["symlink"] = str(link)
    for label, mode in (("group-read", 0o640), ("world-read", 0o604), ("group-write", 0o620)):
        path = tmp_path / "secrets" / f"{label}.env"
        path.write_text("x")
        path.chmod(mode)
        cases[label] = str(path)
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o770)
    path = loose / "x.env"
    path.write_text("x")
    path.chmod(0o600)
    cases["dir-group-writable"] = str(path)
    return cases


def test_bad_env_files_are_config_invalid_and_leave_the_crontab_alone(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for label, value in bad_env_files(tmp_path).items():
        write_config(env, f"cron:\n  env_file: {json.dumps(value)}\n")
        env.set_table(b"# keep\n")
        status, out, err = run(capsys, "--json", "cron", "install")
        assert status == 2, (label, out, err)
        doc = json.loads(out)["error"]
        assert doc["code"] == "config_invalid", label
        assert "cron.env_file" in doc["message"], label
        assert value not in out + err or label in ("missing", "relative", "tilde"), label
        assert env.table() == b"# keep\n", label


def test_env_file_with_a_newline_is_refused_by_the_config_loader(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(env, 'cron:\n  env_file: "/tmp/a\\nb"\n')
    refused(capsys, "cron", "install", code="config_invalid")
    assert env.table() is None


def test_env_file_owned_by_another_user_is_refused(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_env_file(tmp_path)
    write_config(env, f"cron:\n  env_file: {path}\n")
    monkeypatch.setattr(cron, "_geteuid", lambda: os.geteuid() + 1)
    refused(capsys, "cron", "install", code="config_invalid")


def test_uninstall_ignores_a_bad_env_file(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(capsys)
    write_config(env, 'cron:\n  env_file: "relative.env"\n')
    assert run(capsys, "cron", "uninstall")[0] == 0
    assert env.table() is None


# --- warnings ---------------------------------------------------------------------------------


def test_a_poll_line_outside_the_block_warns_on_stderr_and_in_the_result(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.set_table(b"*/5 * * * * /home/u/.local/bin/pr-watch poll --quiet\n")
    status, out, err = run(capsys, "--json", "--quiet", "cron", "install")
    doc = json.loads(out)
    assert status == 0 and len(doc["warnings"]) == 1
    assert "outside the managed block" in doc["warnings"][0]
    assert err.strip() == f"pr-watch: warning: {doc['warnings'][0]}"  # --quiet does not hide it
    assert "/home/u" not in doc["warnings"][0]  # the line itself is not echoed


@pytest.mark.parametrize(
    "line",
    [
        b"# */5 * * * * pr-watch poll",
        b"*/5 * * * * pr-watch list",
        b"*/5 * * * * jobsctl poll",
        b"*/5 * * * * not-pr-watch poll",
        b"*/5 * * * * echo pr-watch",
        b"*/5 * * * * echo pr-watch poll.log",
        b"*/5 * * * * pr-watch poll.sh",
    ],
)
def test_other_lines_do_not_warn(env: Env, capsys: pytest.CaptureFixture[str], line: bytes) -> None:
    env.set_table(line + b"\n")
    assert installed(capsys)["warnings"] == []


@pytest.mark.parametrize(
    "line",
    [
        b"*/5 * * * * pr-watch poll",
        b"*/5 * * * * '/x/pr-watch' poll --quiet",
        b"*/5 * * * * . ~/.env && /usr/bin/pr-watch  poll",
        b"@hourly pr-watch --config /x poll",
    ],
)
def test_poll_line_shapes_that_warn(
    env: Env, capsys: pytest.CaptureFixture[str], line: bytes
) -> None:
    env.set_table(line + b"\n")
    assert len(installed(capsys)["warnings"]) == 1


def test_a_poll_line_inside_the_block_does_not_warn(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(capsys)
    assert installed(capsys)["warnings"] == []


def test_forgectl_warnings_are_in_the_result_and_on_stderr(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.forgectl_dir.chmod(0o777)
    status, out, err = run(capsys, "--json", "cron", "install")
    doc = json.loads(out)
    assert status == 0 and err.strip() == f"pr-watch: warning: {doc['warnings'][0]}"


# --- human output -----------------------------------------------------------------------------


def test_human_output(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(capsys, "cron", "install")
    assert out.splitlines()[0] == "installed the pr-watch cron block (every 5m)"
    assert "*/5 * * * *" in out
    _, out, _ = run(capsys, "cron", "install")
    assert out.splitlines()[0] == "the pr-watch cron block is already current (every 5m)"
    _, out, _ = run(capsys, "cron", "uninstall")
    assert out.strip() == "removed the pr-watch cron block"
    _, out, _ = run(capsys, "cron", "uninstall")
    assert out.strip() == "no pr-watch cron block to remove"
    assert run(capsys, "--quiet", "cron", "install")[1] == ""


# --- state, lock ------------------------------------------------------------------------------


def test_cron_commands_never_open_the_store(
    env: Env, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    installed(capsys)
    run(capsys, "cron", "uninstall")
    assert not (state_dir / "state.db").exists()
    assert (state_dir / "cron.lock").stat().st_mode & 0o777 == 0o600


def test_a_symlinked_cron_lock_is_refused_as_state_unsafe(
    env: Env, capsys: pytest.CaptureFixture[str], state_dir: Path, tmp_path: Path
) -> None:
    state_dir.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.lock"
    target.write_text("")
    (state_dir / "cron.lock").symlink_to(target)
    for command in ("install", "uninstall"):
        status, out, _ = run(capsys, "--json", "cron", command)
        assert (status, json.loads(out)["error"]["code"]) == (12, "state_unsafe")
    assert env.table() is None


def test_a_cron_lock_that_is_group_writable_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    state_dir.mkdir(mode=0o700)
    (state_dir / "cron.lock").write_text("")
    (state_dir / "cron.lock").chmod(0o660)
    status, out, _ = run(capsys, "--json", "cron", "install")
    assert (status, json.loads(out)["error"]["code"]) == (12, "state_unsafe")


def test_the_lock_file_is_opened_once_per_command(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Closing a second descriptor on an inode this process locks would drop the lock (#54)
    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, flags: int, *a: Any, **k: Any) -> int:
        if os.fspath(path).endswith("cron.lock"):
            opened.append(os.fspath(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    installed(capsys)
    assert len(opened) == 1
    opened.clear()
    run(capsys, "cron", "uninstall")
    assert len(opened) == 1


def _child(*args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "pr_watch", "cron", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:  # only handles this test started
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def _wait_until_child_has_opened(proc: subprocess.Popen[bytes], name: str) -> None:
    """Wait (at most 5 s) until `proc` has `name` open: it then has reached the lock wait, so the
    checks after this call cannot pass just because the child was still starting. Without /proc
    (macOS) there is no such evidence and a fixed 1 s wait stands in for it."""
    fd_dir = Path(f"/proc/{proc.pid}/fd")
    if not fd_dir.is_dir():
        time.sleep(1.0)
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        assert proc.poll() is None, "the child exited instead of waiting for the lock"
        for entry in fd_dir.iterdir():
            try:
                if os.readlink(entry).endswith("/" + name):
                    return
            except OSError:
                continue
        time.sleep(0.02)
    raise AssertionError(f"the child never opened {name}")


def test_install_waits_for_the_cron_lock_and_then_writes(
    env: Env, children: list[subprocess.Popen[bytes]], state_dir: Path
) -> None:
    with open_state_dir(state_dir) as state:
        fd = os.open("cron.lock", os.O_RDWR | os.O_CREAT, 0o600, dir_fd=state.fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            proc = _child("install")
            children.append(proc)
            _wait_until_child_has_opened(proc, "cron.lock")
            for _ in range(6):  # then a few ticks of the child's poll loop, checked at every tick
                time.sleep(0.05)
                assert proc.poll() is None  # blocked on the lock
                assert env.table() is None  # and wrote nothing
        finally:
            os.close(fd)
    assert proc.wait(timeout=30) == 0
    assert env.table() == env.golden("install-fresh.crontab")


def test_a_held_lock_past_the_wait_limit_fails_without_touching_the_crontab(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cron, "LOCK_WAIT", 0.2)
    env.set_table(b"# keep\n")
    with open_state_dir(state_dir) as state:
        fd = os.open("cron.lock", os.O_RDWR | os.O_CREAT, 0o600, dir_fd=state.fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            status, out, _ = run(capsys, "--json", "cron", "install")
        finally:
            os.close(fd)
    assert status == 1 and json.loads(out)["error"]["code"] == "internal"
    assert "cron.lock" in json.loads(out)["error"]["message"]
    assert env.table() == b"# keep\n"


def test_concurrent_installs_leave_exactly_one_block(
    env: Env, children: list[subprocess.Popen[bytes]]
) -> None:
    env.set_table(env.golden("foreign-before.crontab"))
    for every in ("5m", "10m", "15m", "30m", "1h", "2h"):
        children.append(_child("install", "--every", every))
    assert [p.wait(timeout=60) for p in children] == [0] * 6
    table = env.table() or b""
    assert table.startswith(env.golden("foreign-before.crontab"))
    assert table.count(BLOCK_START + b" v1") == 1 and table.count(BLOCK_END) == 1
    assert table.count(b"poll --quiet") == 1


# --- the crontab command ----------------------------------------------------------------------


class Backend:
    """A scripted crontab behind the gateway: a table plus hooks, recording every call."""

    def __init__(self, table: bytes | None = None) -> None:
        self.table = table
        self.calls: list[Request] = []
        self.reads = 0
        self.on_read: Any = None
        self.read_result: Result | None = None
        self.write_result: Result | None = None
        self.drop_writes = False

    def __call__(self, request: Request) -> Result:
        self.calls.append(request)
        argv = request.argv
        if argv == ("crontab", "-l"):
            self.reads += 1
            if self.on_read:
                self.on_read(self)
            if self.read_result is not None:
                return self.read_result
            if self.table is None:
                return Result(returncode=1, stderr="no crontab for u\n")
            return Result(returncode=0, stdout=self.table.decode("utf-8", "surrogateescape"))
        if self.write_result is not None:
            return self.write_result
        if argv == ("crontab", "-"):
            assert request.stdin is not None
            if not self.drop_writes:
                self.table = request.stdin
            return Result(returncode=0)
        if argv == ("crontab", "-r"):
            self.table = None
            return Result(returncode=0)
        raise AssertionError(f"unexpected argv {argv}")

    def writes(self) -> list[bytes]:
        return [c.stdin or b"" for c in self.calls if c.argv == ("crontab", "-")]


@pytest.fixture
def backend() -> Iterator[Backend]:
    fake = Backend()
    with gateway.use_backend(fake):
        yield fake


def test_crontab_is_called_through_the_gateway_with_stdin_and_a_minimal_env(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(capsys)
    assert [c.argv for c in backend.calls] == [
        ("crontab", "-l"),
        ("crontab", "-l"),  # re-read under the lock before writing
        ("crontab", "-"),
        ("crontab", "-l"),  # read back
    ]
    write = backend.calls[2]
    assert write.stdin == env.golden("install-fresh.crontab")  # on stdin, never on argv
    assert set(write.env) == {"PATH", "HOME", "LC_ALL"} and write.env["LC_ALL"] == "C"
    assert all("poll" not in a for c in backend.calls for a in c.argv)


def test_uninstall_uses_crontab_r_when_nothing_is_left(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str]
) -> None:
    backend.table = env.golden("install-fresh.crontab")
    assert run(capsys, "cron", "uninstall")[0] == 0
    assert ("crontab", "-r") in [c.argv for c in backend.calls] and backend.table is None


def test_an_edit_made_between_the_read_and_the_write_is_not_lost(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str]
) -> None:
    backend.table = b"0 3 * * * /bin/backup\n"

    def edit(b: Backend) -> None:
        if b.reads == 2:  # the re-read under the lock sees a foreign edit
            b.table = b"0 3 * * * /bin/backup\n0 4 * * * /bin/other\n"
            b.on_read = None

    backend.on_read = edit
    installed(capsys)
    assert backend.table is not None
    assert backend.table.startswith(b"0 3 * * * /bin/backup\n0 4 * * * /bin/other\n# pr-watch")
    assert len(backend.writes()) == 1


def test_a_crontab_that_keeps_changing_is_given_up_on_without_a_write(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str]
) -> None:
    counter = iter(range(100))

    def churn(b: Backend) -> None:
        b.table = f"# {next(counter)}\n".encode()

    backend.on_read = churn
    status, out, _ = run(capsys, "--json", "cron", "install")
    assert status == 1 and json.loads(out)["error"]["code"] == "internal"
    assert backend.writes() == []


def test_a_write_that_does_not_read_back_is_an_error(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str]
) -> None:
    backend.drop_writes = True
    status, out, _ = run(capsys, "--json", "cron", "install")
    message = json.loads(out)["error"]["message"]
    assert status == 1 and "changed while it was being written" in message
    assert "by hand" in message and "run the command again" not in message


@pytest.mark.parametrize(
    "result",
    [
        Result(returncode=3, stderr="boom\x1b[31m\n"),
        Result(returncode=1, stderr="some other failure"),
        Result(returncode=None, abandoned=True),
        Result(returncode=-15, timed_out=True),
        Result(returncode=-15, too_large=True),
    ],
)
def test_a_failing_read_is_internal_and_writes_nothing(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str], result: Result
) -> None:
    backend.read_result = result
    for command in ("install", "uninstall"):
        status, out, err = run(capsys, "--json", "cron", command)
        doc = json.loads(out)["error"]
        assert (status, doc["code"]) == (1, "internal")
        assert "\x1b" not in out + err and "\n" not in doc["message"]
    assert backend.writes() == [] and ("crontab", "-r") not in [c.argv for c in backend.calls]


def test_a_missing_crontab_program_is_internal(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    only = env.forgectl_dir.parent / "only-pr-watch"
    only.mkdir()
    (only / "pr-watch").symlink_to(env.pr_watch)
    monkeypatch.setenv("PATH", str(only))  # pr-watch is found, crontab is not
    status, out, _ = run(capsys, "--json", "cron", "install")
    doc = json.loads(out)["error"]
    assert (status, doc["code"]) == (1, "internal") and "crontab" in doc["message"]


def test_a_failing_write_leaves_the_table_and_says_so(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.set_table(b"# keep\n")
    (env.home / ".fake-crontab-fail-writes").touch()
    status, out, _ = run(capsys, "--json", "cron", "install")
    doc = json.loads(out)["error"]
    assert (status, doc["code"]) == (1, "internal")
    assert "unchanged" in doc["message"]
    assert env.table() == b"# keep\n"


def test_no_crontab_for_is_tolerated_only_by_remove(backend: Backend) -> None:
    backend.write_result = Result(returncode=1, stderr="no crontab for u\n")
    cron._write(b"")  # `-r` on a table that is already gone is fine
    with pytest.raises(CliError) as info:
        cron._write(b"# x\n")
    assert info.value.code == "internal"


def test_a_crontab_the_program_rejects_is_reported_not_swallowed(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str]
) -> None:
    backend.write_result = Result(returncode=1, stderr="errors in crontab file, can't install.\n")
    status, out, _ = run(capsys, "--json", "cron", "install")
    assert status == 1 and "exit 1" in json.loads(out)["error"]["message"]


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_crontab_stderr_is_never_echoed_in_an_error(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    backend.table = None if command == "install" else env.golden("install-fresh.crontab")
    secret = "line 3: API_TOKEN=hunter2 * * * * * x"
    backend.write_result = Result(returncode=1, stderr=secret + "\n")
    status, out, err = run(capsys, "--json", "cron", command)
    assert status == 1 and "hunter2" not in out + err
    backend.write_result = None
    backend.read_result = Result(returncode=3, stderr=secret + "\n")
    status, out, err = run(capsys, "--json", "cron", command)
    assert status == 1 and "hunter2" not in out + err and "exit 3" in out


def test_the_real_crontab_is_never_reached(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert shutil.which("crontab") == str(Path(__file__).parent / "fakebin" / "crontab")
    installed(capsys)
    assert env.table_file.exists()  # the table the commands used is the fake's, under HOME


# --- invocation -------------------------------------------------------------------------------


def test_install_through_the_real_entry_point(env: Env) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pr_watch", "--json", "cron", "install"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert_valid("cli/cron-install", doc)
    assert env.table() == env.golden("install-fresh.crontab")


# --- --dry-run --------------------------------------------------------------------------------


def dry(capsys: pytest.CaptureFixture[str], command: str, *argv: str) -> dict[str, Any]:
    code, out, err = run(capsys, "--json", "cron", command, "--dry-run", *argv)
    assert code == 0, (out, err)
    doc = json.loads(out)
    assert_valid(f"cli/cron-{command}", doc)
    assert doc["dry_run"] is True
    return doc


def real(capsys: pytest.CaptureFixture[str], command: str, *argv: str) -> dict[str, Any]:
    code, out, err = run(capsys, "--json", "cron", command, *argv)
    assert code == 0, (out, err)
    doc = json.loads(out)
    assert_valid(f"cli/cron-{command}", doc)
    assert doc["dry_run"] is False
    return doc


def _same_but_dry_run(preview: dict[str, Any], actual: dict[str, Any]) -> None:
    assert {k: v for k, v in preview.items() if k != "dry_run"} == {
        k: v for k, v in actual.items() if k != "dry_run"
    }


INSTALL_BEFORES = {
    "no_crontab": None,
    "empty": b"",
    "foreign": b"0 3 * * * /bin/backup\n",
    "foreign_no_newline": b"0 3 * * * /bin/backup",
    "other_interval": "reinstall-before.crontab",
    "identical": "install-fresh.crontab",
}


@pytest.mark.parametrize("name", list(INSTALL_BEFORES))
def test_install_dry_run_reports_what_the_real_run_then_does(
    env: Env, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    before = INSTALL_BEFORES[name]
    if isinstance(before, str):
        before = env.golden(before)
    if before is not None:
        env.set_table(before)
    preview = dry(capsys, "install")
    assert env.table() == before  # the dry run wrote nothing
    actual = real(capsys, "install")
    _same_but_dry_run(preview, actual)
    assert preview["block"] == (preview["lines"] if preview["changed"] else None)


def test_install_dry_run_with_every_and_env_file(
    env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = make_env_file(tmp_path)
    write_config(env, f"cron:\n  env_file: {env_file}\n")
    preview = dry(capsys, "install", "--every", "2h")
    assert preview["every"] == "2h" and ". '" in preview["lines"][2]
    assert env.table() is None
    _same_but_dry_run(preview, real(capsys, "install", "--every", "2h"))


def test_install_dry_run_of_an_identical_block_is_unchanged_with_a_null_block(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(capsys)
    doc = dry(capsys, "install")
    assert (doc["changed"], doc["replaced"], doc["block"]) == (False, True, None)


def test_uninstall_dry_run_names_the_block_a_real_run_removes(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    before = env.golden("reinstall-before.crontab")
    env.set_table(before)
    preview = dry(capsys, "uninstall")
    assert env.table() == before
    assert preview["changed"] is True and preview["installed"] is False
    assert preview["block"][0].startswith("# pr-watch managed")
    assert preview["block"][-1] == "# pr-watch managed end"
    actual = real(capsys, "uninstall")
    _same_but_dry_run(preview, actual)
    assert not any(line in (env.table() or b"").decode() for line in preview["block"])


@pytest.mark.parametrize("before", [None, b"", b"0 3 * * * /bin/backup\n"])
def test_uninstall_dry_run_without_a_block_is_unchanged_with_a_null_block(
    env: Env, capsys: pytest.CaptureFixture[str], before: bytes | None
) -> None:
    if before is not None:
        env.set_table(before)
    doc = dry(capsys, "uninstall")
    assert (doc["changed"], doc["block"]) == (False, None)
    assert env.table() == before


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize(
    "table",
    [
        b"# pr-watch managed v1 every=5m\n0 3 * * * /bin/x\n",
        b"# pr-watch managed end\n",
        b"# pr-watch managed\n# pr-watch managed end\n# pr-watch managed\n# pr-watch managed end\n",
    ],
)
def test_dry_run_refuses_a_malformed_crontab_like_the_real_run(
    env: Env, capsys: pytest.CaptureFixture[str], command: str, table: bytes
) -> None:
    env.set_table(table)
    status, out, _ = run(capsys, "--json", "cron", command, "--dry-run")
    real_status, real_out, _ = run(capsys, "--json", "cron", command)
    assert status == real_status == 2
    assert json.loads(out) == json.loads(real_out)
    assert json.loads(out)["error"]["details"]["kind"] == "malformed_crontab"
    assert env.table() == table


def test_dry_run_still_refuses_a_bad_env_file_and_a_bad_every(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    refused_doc = refused(capsys, "cron", "install", "--dry-run", "--every", "7m")
    assert refused_doc["code"] == "usage"
    write_config(env, "cron:\n  env_file: relative.env\n")
    refused(capsys, "cron", "install", "--dry-run", code="config_invalid")
    assert env.table() is None


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_dry_run_never_writes_takes_no_lock_and_creates_nothing(
    env: Env, backend: Backend, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    backend.table = env.golden("reinstall-before.crontab")
    state = env.home / ".local" / "state" / "pr-watch"
    assert not state.exists()
    dry(capsys, command)
    assert [c.argv for c in backend.calls] == [("crontab", "-l")]
    assert backend.table == env.golden("reinstall-before.crontab")
    assert not state.exists()  # no state directory, so no cron.lock either


def test_dry_run_does_not_wait_for_a_held_cron_lock(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(capsys)
    state = open_state_dir()
    try:
        fd = os.open(cron.CRON_LOCK, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=state.fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            doc = dry(capsys, "install")
            assert time.monotonic() - started < cron.LOCK_WAIT / 2
            assert doc["changed"] is False
        finally:
            os.close(fd)
    finally:
        state.close()


def test_dry_run_still_warns_on_stderr(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.set_table(b"*/5 * * * * /usr/bin/pr-watch poll\n")
    code, out, err = run(capsys, "--json", "cron", "install", "--dry-run")
    assert code == 0 and "poll entry outside" in err
    assert json.loads(out)["warnings"]


def test_dry_run_human_output_says_it_would(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "cron", "install", "--dry-run")
    assert code == 0 and out.startswith("dry run: would install the pr-watch cron block (every 5m)")
    assert "# pr-watch managed end" in out
    env.set_table(env.golden("install-fresh.crontab"))
    out = run(capsys, "cron", "uninstall", "--dry-run")[1]
    assert out.startswith("dry run: would remove the pr-watch cron block")
    assert "# pr-watch managed end" in out
    assert env.table() == env.golden("install-fresh.crontab")


def test_real_runs_report_dry_run_false_and_the_block(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = real(capsys, "install")
    assert doc["block"] == doc["lines"]
    assert real(capsys, "install")["block"] is None
    assert real(capsys, "uninstall")["block"] == doc["lines"]
    assert real(capsys, "uninstall")["block"] is None


def _envelope(out: str) -> dict[str, Any]:
    return json.loads(out)["error"]


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize(
    "result",
    [
        Result(returncode=3, stderr="boom\n"),
        Result(returncode=None, abandoned=True),
        Result(returncode=-15, timed_out=True),
        Result(returncode=-15, too_large=True),
    ],
)
def test_dry_run_fails_on_an_unreadable_crontab_like_the_real_run(
    env: Env,
    backend: Backend,
    capsys: pytest.CaptureFixture[str],
    command: str,
    result: Result,
) -> None:
    backend.read_result = result
    real_status, real_out, _ = run(capsys, "--json", "cron", command)
    dry_status, dry_out, _ = run(capsys, "--json", "cron", command, "--dry-run")
    assert real_status == 1 and dry_status == real_status
    assert _envelope(dry_out) == _envelope(real_out)
    assert backend.writes() == []


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize(
    "table",
    [b"# a\0b\n", b"# " + b"x" * (cron.MAX_CRONTAB_BYTES + 1) + b"\n"],
    ids=["nul", "oversized"],
)
def test_dry_run_refuses_a_nul_or_oversized_crontab_like_the_real_run(
    env: Env, capsys: pytest.CaptureFixture[str], command: str, table: bytes
) -> None:
    env.set_table(table)
    real_status, real_out, _ = run(capsys, "--json", "cron", command)
    dry_status, dry_out, _ = run(capsys, "--json", "cron", command, "--dry-run")
    assert real_status == 1 and dry_status == real_status
    assert _envelope(dry_out) == _envelope(real_out)
    assert env.table() == table


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize("problem", ["symlinked_lock", "bad_mode_dir"])
def test_dry_run_refuses_an_unsafe_state_directory_like_the_real_run(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
    tmp_path: Path,
    command: str,
    problem: str,
) -> None:
    state_dir.mkdir(mode=0o700)
    if problem == "symlinked_lock":
        target = tmp_path / "elsewhere.lock"
        target.write_text("")
        (state_dir / "cron.lock").symlink_to(target)
    else:
        state_dir.chmod(0o770)
    real_status, real_out, _ = run(capsys, "--json", "cron", command)
    dry_status, dry_out, _ = run(capsys, "--json", "cron", command, "--dry-run")
    assert (real_status, _envelope(real_out)["code"]) == (12, "state_unsafe")
    assert dry_status == real_status
    assert _envelope(dry_out) == _envelope(real_out)
    assert env.table() is None
