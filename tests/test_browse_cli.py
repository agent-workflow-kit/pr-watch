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


def test_without_a_terminal_exits_2(capsys: pytest.CaptureFixture[str], state_dir: Path) -> None:
    code, out, err = run(capsys, "browse")  # pytest's stdin is not a terminal
    assert code == 2 and out == ""
    assert "terminal" in err and "Traceback" not in err
    assert not state_dir.exists()


def test_a_bad_focus_reference_is_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "browse", "--focus", "not a pr")
    assert code == 2 and "Traceback" not in err


def test_focus_on_an_unstored_pr_is_exit_10(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    monkeypatch.setattr(browse_module, "_is_tty", lambda _stream: True)
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

    monkeypatch.setattr(browse_module, "_is_tty", lambda _stream: True)
    monkeypatch.setattr(browse_app.BrowseApp, "run", fake_run)
    code, _, err = run(capsys, "browse", "--focus", "github.com/o/r#1")
    assert code == 1 and seen == ["P1"]
    assert "internal error (X<U+001B>)" in err and "\x1b" not in err


def test_browse_is_in_the_parser_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["browse", "--help"])
    assert "--focus" in capsys.readouterr().out
