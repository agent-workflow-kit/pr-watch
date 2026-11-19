"""The log.jsonl writer: records, sanitizing, safe opens, appends, rotation, failures."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch import log as log_module
from pr_watch.config import ConfigWarning
from pr_watch.log import EVENT_FIELDS, MAX_LINE_BYTES, Log
from pr_watch.state_dir import StateDir, open_state_dir
from tests.harness import assert_valid, load_schema

HOST = "github.com"
PR = "github.com/o/r#1"
PR_ID = "0123456789abcdef"
HEX = "ev-abcdefghijkl"

SAMPLES: dict[str, dict[str, Any]] = {
    "poll_start": {
        "full": False,
        "forgectl_path": "/usr/bin/forgectl",
        "forgectl_version": "1.2.3",
    },
    "poll_end": {
        "duration_ms": 5,
        "snapshots": 1,
        "events": 2,
        "errors": 0,
        "already_running": False,
        "interrupted": False,
        "truncated": False,
    },
    "host_failed": {"host": HOST, "code": "auth_required"},
    "part_incomplete": {"pr_id": PR_ID, "pr": PR, "part": "ci", "snapshots": 3},
    "commit_skipped": {"pr_id": PR_ID, "reason": "lost_source"},
    "host_summary": {
        "host": HOST,
        "snapshots": 1,
        "requests_estimate": 9,
        "errors": 0,
        "paused_until": None,
    },
    "snapshot_failed": {"pr": PR, "pr_id": PR_ID, "code": "network", "failures": 1},
    "pr_status": {"pr": PR, "pr_id": PR_ID, "from": "active", "to": "terminal"},
    "hook_run": {
        "event_id": None,
        "hook": "post-poll",
        "exit_code": 0,
        "duration_ms": 12,
        "timed_out": False,
    },
    "hook_cap": {"skipped": 4, "first_event_id": HEX, "last_event_id": HEX},
    "config_warning": {"key": "hooks.env_allow"},
}


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDir]:
    with open_state_dir(tmp_path / "pr-watch") as s:
        yield s


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def log_path(state: StateDir) -> Path:
    return state.path / "log.jsonl"


# --- records ---------------------------------------------------------------------------------


def test_samples_cover_every_event_of_the_schema() -> None:
    schema = load_schema("log")
    assert set(SAMPLES) == {o["properties"]["event"]["const"] for o in schema["oneOf"]}


def test_event_fields_table_matches_the_schema() -> None:
    schema = load_schema("log")
    framing = {"ts", "level", "event"}
    for branch in schema["oneOf"]:
        props = branch["properties"]
        name = props["event"]["const"]
        required = {k for k in branch["required"] if k not in framing}
        optional = {k for k in props if k not in framing and k not in required}
        assert EVENT_FIELDS[name] == (required, optional), name
    assert set(EVENT_FIELDS) == set(SAMPLES)


@pytest.mark.parametrize("event", sorted(SAMPLES))
def test_every_record_validates_against_the_schema(state: StateDir, event: str) -> None:
    for level in ("debug", "info", "warn", "error"):
        assert Log(state).write(event, level, **SAMPLES[event]) is True
    recs = records(log_path(state))
    assert [r["level"] for r in recs] == ["debug", "info", "warn", "error"]
    for rec in recs:
        assert_valid("log", rec)
        assert rec["event"] == event


def test_optional_message_validates(state: StateDir) -> None:
    Log(state).write("host_failed", "error", message="the token may have reached another host",
                     **SAMPLES["host_failed"])  # fmt: skip
    (rec,) = records(log_path(state))
    assert_valid("log", rec)
    assert rec["message"].startswith("the token")


def test_timestamp_is_utc_microseconds(state: StateDir) -> None:
    Log(state, clock=lambda: 1_700_000_000.123456).write("config_warning", "warn", key="k")
    (rec,) = records(log_path(state))
    assert rec["ts"] == "2023-11-14T22:13:20.123456Z"
    assert_valid("log", rec)


def test_a_record_is_one_ascii_line_ending_in_newline(state: StateDir) -> None:
    Log(state).write("config_warning", "warn", key="ключ   é", message="line1\nline2\r\x00")
    raw = log_path(state).read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    raw.decode("ascii")


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        ("nope", {}),
        ("config_warning", {}),  # missing required key
        ("config_warning", {"key": "k", "extra": 1}),  # unknown field
        ("config_warning", {"key": "k", "ts": "x"}),  # framing field smuggled in
    ],
)
def test_a_malformed_call_is_a_programming_error(
    state: StateDir, event: str, fields: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        Log(state).write(event, "warn", **fields)
    assert not log_path(state).exists()


def test_unknown_level_is_a_programming_error(state: StateDir) -> None:
    with pytest.raises(ValueError):
        Log(state).write("config_warning", "fatal", key="k")


# --- hostile text ----------------------------------------------------------------------------


def test_control_and_bidi_characters_become_markers(state: StateDir) -> None:
    Log(state).write(
        "config_warning", "warn", key="a\x1b[31mb", message='x‮y\x07\n{"event":"forged"}'
    )
    (rec,) = records(log_path(state))
    assert rec["key"] == "a<U+001B>[31mb"
    assert "<U+202E>" in rec["message"] and "<U+0007>" in rec["message"]
    assert "<U+000A>" in rec["message"]
    assert rec["event"] == "config_warning"  # a forged record stayed inside one string


def test_overlong_fields_are_capped_to_the_schema(state: StateDir) -> None:
    big = "é" * 10_000
    Log(state).write(
        "poll_start", "info", message=big, scope=big, forgectl_path=big, forgectl_version=big,
        full=True,
    )  # fmt: skip
    (rec,) = records(log_path(state))
    assert_valid("log", rec)
    assert len(rec["forgectl_path"]) == 4096 and len(rec["forgectl_version"]) == 32


def test_marker_expansion_stays_inside_the_cap(state: StateDir) -> None:
    Log(state).write("config_warning", "warn", key="\x00" * 1000, message="\x00" * 1000)
    (rec,) = records(log_path(state))
    assert_valid("log", rec)


def test_worst_case_record_fits_the_single_write_bound(state: StateDir) -> None:
    worst = "\U0001f600" * 5000
    assert Log(state).write(
        "poll_start", "info", message=worst, scope=worst, forgectl_path=worst,
        forgectl_version=worst, full=True,
    )  # fmt: skip
    assert len(log_path(state).read_bytes()) <= MAX_LINE_BYTES


def test_non_string_junk_in_a_string_field_is_not_logged_as_code(state: StateDir) -> None:
    with pytest.raises(ValueError):
        Log(state).write("config_warning", "warn", key=object())


def test_config_warnings_become_records(state: StateDir) -> None:
    warnings = (
        ConfigWarning("config_warning", "PR_WATCH_X is unknown; ignored", "PR_WATCH_X"),
        ConfigWarning("config_warning", "secret-like name", "hooks.env_allow"),
    )
    Log(state).config_warnings(warnings)
    recs = records(log_path(state))
    assert [r["key"] for r in recs] == ["PR_WATCH_X", "hooks.env_allow"]
    for rec in recs:
        assert_valid("log", rec)
        assert rec["level"] == "warn" and rec["event"] == "config_warning"


# --- safe opens ------------------------------------------------------------------------------


def test_file_is_created_private(state: StateDir) -> None:
    old = os.umask(0)
    try:
        Log(state).write("config_warning", "warn", key="k")
    finally:
        os.umask(old)
    assert stat.S_IMODE(log_path(state).stat().st_mode) == 0o600


def test_existing_file_is_appended_to_not_truncated(state: StateDir) -> None:
    lg = Log(state)
    lg.write("config_warning", "warn", key="a")
    Log(state).write("config_warning", "warn", key="b")
    assert [r["key"] for r in records(log_path(state))] == ["a", "b"]


def test_a_symlinked_log_is_never_followed(state: StateDir, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    log_path(state).symlink_to(victim)
    problems: list[str] = []
    assert Log(state, on_error=problems.append).write("config_warning", "warn", key="k") is False
    assert victim.read_text() == "precious\n"
    assert len(problems) == 1


def test_a_dangling_symlinked_log_is_not_created_through(state: StateDir, tmp_path: Path) -> None:
    log_path(state).symlink_to(tmp_path / "created-by-attacker")
    assert Log(state, on_error=lambda _: None).write("config_warning", "warn", key="k") is False
    assert not (tmp_path / "created-by-attacker").exists()


def test_a_fifo_does_not_block_the_writer(state: StateDir) -> None:
    os.mkfifo(log_path(state))
    problems: list[str] = []
    assert Log(state, on_error=problems.append).write("config_warning", "warn", key="k") is False
    assert len(problems) == 1


def test_a_directory_in_place_of_the_log_is_refused(state: StateDir) -> None:
    log_path(state).mkdir()
    problems: list[str] = []
    assert Log(state, on_error=problems.append).write("config_warning", "warn", key="k") is False
    assert len(problems) == 1


def test_a_group_writable_log_is_refused(state: StateDir) -> None:
    log_path(state).write_text("")
    log_path(state).chmod(0o660)
    problems: list[str] = []
    assert Log(state, on_error=problems.append).write("config_warning", "warn", key="k") is False
    assert log_path(state).read_text() == ""
    assert "writable" in problems[0]


def test_another_users_log_is_refused(state: StateDir, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path(state).write_text("")
    other = os.geteuid() + 1
    monkeypatch.setattr(log_module.os, "geteuid", lambda: other)
    problems: list[str] = []
    assert Log(state, on_error=problems.append).write("config_warning", "warn", key="k") is False
    assert log_path(state).read_text() == ""
    assert "another user" in problems[0]


def test_a_world_readable_log_is_accepted(state: StateDir) -> None:
    log_path(state).write_text("")
    log_path(state).chmod(0o644)
    assert Log(state).write("config_warning", "warn", key="k") is True


# --- failures never crash the command --------------------------------------------------------


def _patch_os(monkeypatch: pytest.MonkeyPatch, name: str, fn: Callable[..., Any]) -> None:
    monkeypatch.setattr(log_module.os, name, fn)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EDQUOT, errno.EIO])
def test_write_failures_are_reported_once_and_do_not_raise(
    state: StateDir, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def failing(fd: int, data: bytes) -> int:
        raise OSError(code, os.strerror(code))

    _patch_os(monkeypatch, "write", failing)
    problems: list[str] = []
    lg = Log(state, on_error=problems.append)
    assert lg.write("config_warning", "warn", key="a") is False
    assert lg.write("config_warning", "warn", key="b") is False
    assert len(problems) == 1 and os.strerror(code) in problems[0]


@pytest.mark.parametrize("code", [errno.EROFS, errno.ENOSPC, errno.EACCES, errno.EMFILE])
def test_open_failures_are_reported_once_and_do_not_raise(
    state: StateDir, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    real_open = os.open

    def failing(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "log.jsonl":
            raise OSError(code, os.strerror(code))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    _patch_os(monkeypatch, "open", failing)
    problems: list[str] = []
    assert Log(state, on_error=problems.append).write("config_warning", "warn", key="k") is False
    assert len(problems) == 1


def test_a_short_write_terminates_the_fragment_so_the_next_record_parses(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    calls = {"n": 0}

    def short(fd: int, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, data[: len(data) // 2])
        return real_write(fd, data)

    _patch_os(monkeypatch, "write", short)
    lg = Log(state, on_error=lambda _: None)
    assert lg.write("config_warning", "warn", key="lost") is False
    assert lg.write("config_warning", "warn", key="kept") is True
    lines = log_path(state).read_text().splitlines()
    assert json.loads(lines[-1])["key"] == "kept"
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[0])  # the fragment is its own (invalid) line, not glued to the next


def test_a_null_log_writes_nothing_and_never_raises() -> None:
    from pr_watch.log import NULL_LOG

    assert NULL_LOG.write("config_warning", "warn", key="k") is False
    NULL_LOG.config_warnings((ConfigWarning("config_warning", "m", "k"),))


# --- rotation --------------------------------------------------------------------------------


def _fill(lg: Log, count: int, tag: str) -> None:
    for i in range(count):
        assert lg.write("config_warning", "warn", key=f"{tag}{i}", message="m" * 100)


def test_rotation_keeps_three_files_in_order(state: StateDir) -> None:
    lg = Log(state, max_bytes=2000)
    _fill(lg, 80, "k")
    names = sorted(p.name for p in state.path.iterdir())
    assert names == ["log.jsonl", "log.jsonl.1", "log.jsonl.2", "log.jsonl.3"]
    seen = []
    for name in ("log.jsonl.3", "log.jsonl.2", "log.jsonl.1", "log.jsonl"):
        seen += [int(r["key"][1:]) for r in records(state.path / name)]
    assert seen == sorted(seen) and seen[-1] == 79
    for name in names:
        assert (state.path / name).stat().st_size <= 2000 + 400
        assert stat.S_IMODE((state.path / name).stat().st_mode) == 0o600


def test_no_fourth_file_is_ever_kept(state: StateDir) -> None:
    _fill(Log(state, max_bytes=1000), 200, "k")
    assert not (state.path / "log.jsonl.4").exists()


def test_default_cap_is_ten_mebibytes_and_three_files() -> None:
    assert log_module.MAX_BYTES == 10 * 1024 * 1024
    assert log_module.KEEP == 3


def test_below_the_cap_nothing_rotates(state: StateDir) -> None:
    _fill(Log(state), 20, "k")
    assert [p.name for p in state.path.iterdir()] == ["log.jsonl"]


def test_rotation_does_not_follow_planted_symlinks(state: StateDir, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    for n in (1, 2, 3):
        (state.path / f"log.jsonl.{n}").symlink_to(victim)
    _fill(Log(state, max_bytes=500), 10, "k")
    assert victim.read_text() == "precious\n"
    assert not (state.path / "log.jsonl.1").is_symlink()


def test_rotation_failure_does_not_lose_the_record(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    lg = Log(state, max_bytes=500, on_error=lambda _: None)
    _fill(lg, 3, "a")

    def failing(*a: Any, **k: Any) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    _patch_os(monkeypatch, "rename", failing)
    assert lg.write("config_warning", "warn", key="after", message="m" * 100)
    assert records(log_path(state))[-1]["key"] == "after"


def test_a_log_over_the_cap_at_start_rotates_on_first_write(state: StateDir) -> None:
    log_path(state).write_text("x" * 5000 + "\n")
    log_path(state).chmod(0o600)
    Log(state, max_bytes=1000).write("config_warning", "warn", key="k")
    assert (state.path / "log.jsonl.1").read_text().startswith("xxx")
    assert [r["key"] for r in records(log_path(state))] == ["k"]


# --- concurrent writers ----------------------------------------------------------------------

WORKER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from pr_watch.log import Log
    from pr_watch.state_dir import open_state_dir

    state = open_state_dir(Path(sys.argv[1]))
    tag, count, cap = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    lg = Log(state, max_bytes=cap)
    for i in range(count):
        lg.write("config_warning", "warn", key=f"{tag}-{i}", message="z" * 200)
    """
)


def _run_workers(directory: Path, workers: int, count: int, cap: int) -> None:
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(directory), f"w{n}", str(count), str(cap)],
            stderr=subprocess.PIPE,
        )
        for n in range(workers)
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode()


def test_concurrent_appenders_never_interleave_or_lose_a_record(tmp_path: Path) -> None:
    directory = tmp_path / "pr-watch"
    open_state_dir(directory).close()
    _run_workers(directory, workers=4, count=60, cap=10 * 1024 * 1024)
    recs = records(directory / "log.jsonl")
    assert len(recs) == 240
    for rec in recs:
        assert_valid("log", rec)
    for n in range(4):
        keys = [r["key"] for r in recs if r["key"].startswith(f"w{n}-")]
        assert keys == [f"w{n}-{i}" for i in range(60)]  # each writer's own order survives


def test_concurrent_rotation_keeps_every_file_valid_and_bounded(tmp_path: Path) -> None:
    directory = tmp_path / "pr-watch"
    open_state_dir(directory).close()
    _run_workers(directory, workers=3, count=60, cap=6000)
    names = sorted(p.name for p in directory.iterdir())
    assert set(names) <= {"log.jsonl", "log.jsonl.1", "log.jsonl.2", "log.jsonl.3"}
    total = 0
    for name in names:
        recs = records(directory / name)  # raises on a torn or interleaved line
        total += len(recs)
        assert (directory / name).stat().st_size < 6000 + 3 * 400
    assert total >= 40
