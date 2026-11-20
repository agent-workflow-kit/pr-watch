"""The CLI frame opens the state directory and writes config warnings to log.jsonl."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from pr_watch import log as log_module
from pr_watch.cli import HANDLERS, Context
from tests.harness import assert_valid
from tests.test_cli import envelope_of, run, stubs  # noqa: F401  (fixture)

WRITING = [["prune"], ["poll"], ["cron", "uninstall"]]
READ_ONLY = [["list"], ["events"], ["export"]]


def log_records(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_command_creates_the_private_state_dir_and_hands_it_to_the_handler(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = run(capsys, "prune")
    assert code == 0
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    ctx = stubs[0][0]
    assert ctx.state is not None and ctx.state.path == state_dir


def test_help_version_and_usage_errors_touch_no_state(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["--version"], ["--help"], ["prune", "--bogus"], []):
        with contextlib.suppress(SystemExit):
            run(capsys, *argv)
    assert not state_dir.exists()


def test_config_errors_touch_no_state(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PR_WATCH_POLL__CONCURRENCY", "nope")
    code, _, _ = run(capsys, "prune")
    assert code == 2
    assert not state_dir.exists()


@pytest.mark.parametrize("argv", WRITING)
def test_config_warnings_are_logged_before_the_handler_runs(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("PR_WATCH_BOGUS_KEY", "1")
    seen: list[list[dict[str, Any]]] = []
    name = " ".join(argv)
    original = HANDLERS[name]

    def spy(ctx: Context, args: Any) -> int:
        seen.append(log_records(state_dir))
        return original(ctx, args)

    HANDLERS[name] = spy
    code, _, _ = run(capsys, *argv)
    assert code == 0
    assert len(seen) == 1 and len(seen[0]) == 1
    (rec,) = log_records(state_dir)
    assert_valid("log", rec)
    assert rec["event"] == "config_warning" and rec["key"] == "PR_WATCH_BOGUS_KEY"
    assert stat.S_IMODE((state_dir / "log.jsonl").stat().st_mode) == 0o600


@pytest.mark.parametrize("argv", READ_ONLY)
def test_read_only_commands_write_no_log(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("PR_WATCH_BOGUS_KEY", "1")
    code, _, _ = run(capsys, *argv)
    assert code == 0
    assert not (state_dir / "log.jsonl").exists()


def test_no_warnings_means_no_log_file(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(capsys, "prune")[0] == 0
    assert not (state_dir / "log.jsonl").exists()


def test_the_context_log_writes_to_the_state_dir(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wrote: list[bool] = []

    def handler(ctx: Context, args: Any) -> int:
        wrote.append(ctx.log.write("commit_skipped", "info", pr_id="x1", reason="lost_source"))
        return 0

    HANDLERS["poll"] = handler
    assert run(capsys, "poll")[0] == 0
    assert wrote == [True]
    assert log_records(state_dir)[-1]["event"] == "commit_skipped"


def test_symlinked_state_dir_exits_12_with_the_envelope(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    state_dir.symlink_to(target)
    code, out, err = run(capsys, "--json", "prune")
    assert code == 12 and stubs == []
    error = envelope_of(out)
    assert error["code"] == "state_unsafe" and "symlink" in error["message"]
    assert err.startswith("pr-watch: error: state directory ")
    assert list(target.iterdir()) == []


def test_writable_state_dir_exits_12_for_every_command(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir.mkdir()
    state_dir.chmod(0o777)
    for argv in (*WRITING, *READ_ONLY):
        code, _, err = run(capsys, *argv)
        assert code == 12 and "writable" in err
    assert stubs == []


def test_uncreatable_state_dir_is_state_unsafe_exit_12(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    isolated_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (isolated_env / ".local" / "state").rmdir()
    (isolated_env / ".local" / "state").write_text("a file, not a directory")
    code, out, _ = run(capsys, "--json", "prune")
    assert code == 12 and envelope_of(out)["code"] == "state_unsafe"


def test_a_log_failure_never_changes_the_result_and_warns_once_on_stderr(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PR_WATCH_BOGUS_ONE", "1")
    monkeypatch.setenv("PR_WATCH_BOGUS_TWO", "1")

    def failing(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(log_module, "_write", failing)
    code, out, err = run(capsys, "--json", "prune")
    assert code == 0 and json.loads(out)["ok"] is True
    assert err.count("pr-watch: log.jsonl not written: ") == 1 and "No space left" in err


def test_quiet_silences_the_log_failure_line(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PR_WATCH_BOGUS_ONE", "1")
    attempts: list[int] = []

    def failing(fd: int, data: bytes) -> int:
        attempts.append(fd)
        raise OSError(5, "x")

    monkeypatch.setattr(log_module, "_write", failing)
    code, _, err = run(capsys, "--quiet", "prune")
    assert attempts  # the write was attempted and failed; only the stderr line is silenced
    assert code == 0 and err == ""


def test_a_symlinked_log_is_skipped_with_a_stderr_line_and_the_command_succeeds(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    state_dir.mkdir(mode=0o700)
    (state_dir / "log.jsonl").symlink_to(victim)
    monkeypatch.setenv("PR_WATCH_BOGUS_ONE", "1")
    code, _, err = run(capsys, "prune")
    assert code == 0 and "log.jsonl not written" in err
    assert victim.read_text() == "precious\n"


def test_an_unsafe_hooks_cwd_exits_12_with_a_schema_valid_envelope(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir.mkdir(mode=0o700, parents=True)
    (state_dir / "hooks-cwd").symlink_to(tmp_path)
    code, out, err = run(capsys, "--json", "prune")
    assert code == 12 and stubs == []
    error = envelope_of(out)  # validates cli/error
    assert error["code"] == "state_unsafe" and "hooks-cwd is a symlink" in error["message"]
    assert err == f"pr-watch: error: {error['message']}\n"


def test_an_unsafe_cron_lock_exits_12_with_a_schema_valid_envelope(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir.mkdir(mode=0o700, parents=True)
    (state_dir / "cron.lock").write_text("")
    (state_dir / "cron.lock").chmod(0o666)
    code, out, _ = run(capsys, "--json", "poll")
    assert code == 12 and stubs == []
    error = envelope_of(out)
    assert (
        error["code"] == "state_unsafe"
        and "cron.lock is group- or world-writable" in (error["message"])
    )


def test_a_command_creates_hooks_cwd(
    stubs: list[tuple[Context, Any]],  # noqa: F811
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(capsys, "prune")[0] == 0  # a read-only command creates nothing (Files)
    assert stat.S_IMODE((state_dir / "hooks-cwd").stat().st_mode) == 0o700
