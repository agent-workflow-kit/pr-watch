"""`pr-watch browse` on the command line: refusals, the missing extra, exit codes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pr_watch import browse as browse_module
from pr_watch import browse_app
from pr_watch.cli import main
from tests.test_list import add_pr, digest
from tests.test_list import store as store  # noqa: F401  (fixture)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_without_textual_exits_2_with_an_install_hint_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    monkeypatch.setitem(sys.modules, "textual", None)  # `import textual` raises ImportError
    code, out, err = run(capsys, "browse")
    assert code == 2 and out == ""
    assert "pr-watch[browse]" in err and "Traceback" not in err
    assert not state_dir.exists()


def test_the_missing_extra_error_carries_kind_and_install_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "textual", None)
    with pytest.raises(browse_module.UsageError) as caught:
        browse_module._import_app()
    assert caught.value.details is not None
    assert caught.value.details["kind"] == "tui_missing"
    assert "pr-watch[browse]" in caught.value.details["install"]


def test_json_is_refused(capsys: pytest.CaptureFixture[str], state_dir: Path) -> None:
    code, out, _ = run(capsys, "browse", "--json")
    assert code == 2
    assert json.loads(out)["error"]["details"]["kind"] == "bad_combination"
    assert not state_dir.exists()


class FakeStream:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def set_terminals(monkeypatch: pytest.MonkeyPatch, *, stdin: bool, stderr: bool) -> None:
    """Textual reads sys.__stdin__ and draws to sys.__stderr__ (never stdout)."""
    monkeypatch.setattr(sys, "__stdin__", FakeStream(stdin))
    monkeypatch.setattr(sys, "__stderr__", FakeStream(stderr))


@pytest.mark.parametrize(("stdin", "stderr"), [(False, False), (True, False), (False, True)])
def test_without_a_terminal_on_stdin_and_stderr_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
    stdin: bool,
    stderr: bool,
) -> None:
    set_terminals(monkeypatch, stdin=stdin, stderr=stderr)
    code, out, err = run(capsys, "browse")
    assert code == 2 and out == ""
    assert "terminal" in err and "Traceback" not in err
    assert not state_dir.exists()


def test_a_redirected_stdout_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    set_terminals(monkeypatch, stdin=True, stderr=True)  # capsys makes sys.stdout a non-tty
    ran: list[bool] = []
    monkeypatch.setattr(browse_app.BrowseApp, "run", lambda self: ran.append(True) or 0)
    code, _, err = run(capsys, "browse")
    assert code == 0 and ran == [True] and err == ""


def unsafe_state_dir(state_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    state_dir.symlink_to(target)


def test_an_unsafe_state_dir_does_not_pre_empt_the_terminal_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
    tmp_path: Path,
) -> None:
    unsafe_state_dir(state_dir, tmp_path)
    set_terminals(monkeypatch, stdin=False, stderr=False)
    code, _, err = run(capsys, "browse")
    assert code == 2 and "terminal" in err
    monkeypatch.setitem(sys.modules, "textual", None)
    code, _, err = run(capsys, "browse")
    assert code == 2 and "pr-watch[browse]" in err


def test_an_unsafe_state_dir_opens_the_tui_which_shows_the_failed_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
    tmp_path: Path,
) -> None:
    unsafe_state_dir(state_dir, tmp_path)
    set_terminals(monkeypatch, stdin=True, stderr=True)
    ran: list[bool] = []
    monkeypatch.setattr(browse_app.BrowseApp, "run", lambda self: ran.append(True) or 0)
    code, _, _ = run(capsys, "browse")
    assert code == 0 and ran == [True]


def test_focus_surfaces_the_stores_own_error_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
    tmp_path: Path,
) -> None:
    unsafe_state_dir(state_dir, tmp_path)
    set_terminals(monkeypatch, stdin=True, stderr=True)
    code, _, err = run(capsys, "browse", "--focus", "github.com/o/r#1")
    assert code == 12 and "state directory" in err


def test_a_bad_focus_reference_is_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "browse", "--focus", "not a pr")
    assert code == 2 and "Traceback" not in err


def test_focus_on_an_unstored_pr_is_exit_10(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    set_terminals(monkeypatch, stdin=True, stderr=True)
    code, _, err = run(capsys, "browse", "--focus", "github.com/o/r#1")
    assert code == 10 and "no stored PR matches" in err
    assert not state_dir.exists()


def test_the_app_exit_code_and_message_are_passed_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], store: object
) -> None:
    add_pr(store, "P1", "github.com/o/r#1", dig=digest(), sources=("w-aaaaaaaaaa",))  # type: ignore[arg-type]
    seen: list[str | None] = []

    def fake_run(self: browse_app.BrowseApp) -> int:
        seen.append(self._focus)
        self.exit_message = "internal error (X\x1b)"
        return 1

    set_terminals(monkeypatch, stdin=True, stderr=True)
    monkeypatch.setattr(browse_app.BrowseApp, "run", fake_run)
    code, _, err = run(capsys, "browse", "--focus", "github.com/o/r#1")
    assert code == 1 and seen == ["P1"]
    assert "internal error (X<U+001B>)" in err and "\x1b" not in err


def test_browse_is_in_the_parser_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["browse", "--help"])
    assert "--focus" in capsys.readouterr().out
