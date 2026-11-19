"""CLI frame: commands, usage errors, the error envelope, exit codes and output channels."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pr_watch import __version__
from pr_watch import cli as cli_module
from pr_watch.cli import (
    EXIT_CODES,
    HANDLERS,
    CliError,
    Context,
    build_envelope,
    main,
    one_line,
)
from pr_watch.config import ConfigError
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "cli"

# Every command path with the smallest argv that parses.
VALID: dict[str, list[str]] = {
    "watch add": ["watch", "add", "o/r#1"],
    "watch remove": ["watch", "remove", "o/r#1"],
    "poll": ["poll"],
    "list": ["list"],
    "show": ["show", "o/r#1"],
    "events": ["events"],
    "label add": ["label", "add", "o/r#1", "k=v"],
    "label remove": ["label", "remove", "o/r#1", "k=v"],
    "prune": ["prune"],
    "cron install": ["cron", "install"],
    "cron uninstall": ["cron", "uninstall"],
    "export": ["export"],
}

# Bad usage for each command (an unknown flag is always one more).
BAD: dict[str, list[str]] = {
    "watch add": ["watch", "add"],
    "watch remove": ["watch", "remove"],
    "poll": ["poll", "--bogus"],
    "list": ["list", "--status", "weird"],
    "show": ["show"],
    "events": ["events", "--bogus"],
    "label add": ["label", "add", "o/r#1"],
    "label remove": ["label", "remove"],
    "prune": ["prune", "--bogus"],
    "cron install": ["cron", "install", "--bogus"],
    "cron uninstall": ["cron", "uninstall", "extra"],
    "export": ["export", "--bogus"],
}


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Context, Any]]:
    """Replace every handler with a recorder that prints `{"ok": true}`."""
    calls: list[tuple[Context, Any]] = []

    def make(name: str) -> Any:
        def handler(ctx: Context, args: Any) -> int:
            calls.append((ctx, args))
            ctx.emit({"ok": True, "command": name}, human=f"ok {name}")
            return 0

        return handler

    for name in list(HANDLERS):
        monkeypatch.setitem(HANDLERS, name, make(name))
    return calls


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def envelope_of(out: str) -> dict[str, Any]:
    doc = json.loads(out)
    assert_valid("cli/error", doc)
    return doc["error"]


# --- version, help, no command ---------------------------------------------------------------


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"pr-watch {__version__}"


def test_help_lists_every_command_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for word in ("watch", "poll", "list", "show", "events", "label", "prune", "cron", "export"):
        assert word in out


@pytest.mark.parametrize("name", sorted(VALID))
def test_help_works_for_every_command(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [w for w in VALID[name] if w in name.split()] + ["--help"]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert captured.err == ""


def test_no_arguments_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys)
    assert code == 2
    assert out == ""
    assert "usage:" in err


def test_no_arguments_with_json_prints_the_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "--json")
    assert code == 2
    assert envelope_of(out)["code"] == "usage"
    assert "usage:" in err


@pytest.mark.parametrize("group", ["watch", "label", "cron"])
def test_group_without_subcommand_is_a_usage_error(
    group: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", group)
    assert code == 2
    assert envelope_of(out)["code"] == "usage"


# --- every command exists, parses, and has a handler ------------------------------------------


def test_every_parsed_command_has_a_handler() -> None:
    assert set(HANDLERS) == set(VALID)


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_argv_reaches_the_handler(
    name: str, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", *VALID[name])
    assert code == 0
    assert json.loads(out) == {"ok": True, "command": name}
    assert len(stubs) == 1


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_usage_exits_2_with_envelope(
    name: str, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "--json", *BAD[name])
    assert code == 2
    assert envelope_of(out)["code"] == "usage"
    assert err.strip() and "Traceback" not in err
    assert stubs == []


@pytest.mark.parametrize("name", sorted(VALID))
def test_unknown_flag_is_a_usage_error_everywhere(
    name: str, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, *VALID[name], "--no-such-flag", "--json")
    assert code == 2
    assert envelope_of(out)["code"] == "usage"


@pytest.mark.parametrize("flag", ["--yes", "--actor", "--dry-run"])
def test_flags_pr_watch_does_not_have_are_rejected(
    flag: str, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    # --dry-run exists on prune only.
    for name, argv in VALID.items():
        if name == "prune" and flag == "--dry-run":
            continue
        assert run(capsys, *argv, flag)[0] == 2, name
    assert run(capsys, "--json", "prune", "--dry-run")[0] == 0


def test_flag_abbreviations_are_not_accepted(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "poll", "--ful")[0] == 2
    assert run(capsys, "poll", "--js")[0] == 2


# --- global flags -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(VALID))
def test_global_flags_are_accepted_before_and_after_the_command(
    name: str, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    flags = ["--json", "--verbose"]
    before = run(capsys, *flags, *VALID[name])
    after = run(capsys, *VALID[name], *flags)
    assert before[0] == after[0] == 0
    assert stubs[0][0].json and stubs[0][0].verbose
    assert stubs[1][0].json and stubs[1][0].verbose


def test_flags_default_to_off(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "poll")
    ctx = stubs[0][0]
    assert (ctx.json, ctx.verbose, ctx.quiet) == (False, False, False)


def test_verbose_and_quiet_conflict(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", "poll", "--verbose", "--quiet")
    assert code == 2
    err = envelope_of(out)
    assert err["code"] == "usage"
    assert err["details"]["kind"] == "conflicting_flags"


def test_quiet_silences_human_results_but_not_json(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "poll", "--quiet") == (0, "", "")
    code, out, _ = run(capsys, "poll", "--quiet", "--json")
    assert code == 0 and json.loads(out)["ok"] is True


def test_json_output_is_one_document_and_human_is_text(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, err = run(capsys, "poll", "--json")
    assert out.endswith("\n") and out.count("\n") == 1 and err == ""
    code, out, err = run(capsys, "poll")
    assert (code, out, err) == (0, "ok poll\n", "")


# --- command argument shapes -------------------------------------------------------------------


def test_watch_add_per_pr_and_query_forms_parse(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "watch", "add", "o/r#1", "--label", "a=b", "--label", "c=d", "--owner", "me",
        "--no-baseline")  # fmt: skip
    a = stubs[-1][1]
    assert (a.pr, a.label, a.owner, a.no_baseline) == ("o/r#1", ["a=b", "c=d"], "me", True)
    run(capsys, "watch", "add", "--mine", "--open", "--host", "h1", "--host", "h2", "--repo", "x/y")
    a = stubs[-1][1]
    assert (a.pr, a.mine, a.open, a.host, a.repo) == (None, True, True, ["h1", "h2"], ["x/y"])


@pytest.mark.parametrize(
    ("argv", "kind"),
    [
        (["watch", "add"], "missing_target"),
        (["watch", "add", "--mine"], "bad_combination"),
        (["watch", "add", "--open"], "bad_combination"),
        (["watch", "add", "o/r#1", "--mine", "--open"], "bad_combination"),
        (["watch", "add", "--mine", "--open", "--no-baseline"], "bad_combination"),
        (["watch", "add", "o/r#1", "--host", "h"], "bad_combination"),
        (["watch", "add", "o/r#1", "--repo", "x/y"], "bad_combination"),
        (["watch", "remove"], "missing_target"),
        (["watch", "remove", "--mine"], "bad_combination"),
        (["watch", "remove", "o/r#1", "--mine", "--open"], "bad_combination"),
        (["watch", "remove", "o/r#1", "--repo", "x/y"], "bad_combination"),
    ],
)
def test_watch_shape_errors(
    argv: list[str],
    kind: str,
    stubs: list[tuple[Context, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(capsys, "--json", *argv)
    assert code == 2
    err = envelope_of(out)
    assert (err["code"], err["details"]["kind"]) == ("usage", kind)


def test_watch_remove_takes_no_labels(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "watch", "remove", "o/r#1", "--label", "a=b")[0] == 2


def test_label_splits_reference_from_labels(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "label", "add", "o/r#1", "a=b", "c=d", "--owner", "me")
    a = stubs[-1][1]
    assert (a.pr, a.watch, a.labels, a.owner) == ("o/r#1", None, ["a=b", "c=d"], "me")
    run(capsys, "label", "remove", "--watch", "W1", "a=b")
    a = stubs[-1][1]
    assert (a.pr, a.watch, a.labels) == (None, "W1", ["a=b"])


@pytest.mark.parametrize(
    "argv",
    [
        ["label", "add", "--watch", "W1"],  # no labels
        ["label", "add", "o/r#1"],  # no labels
        ["label", "remove", "--watch"],
        ["label", "remove"],
    ],
)
def test_label_shape_errors(
    argv: list[str], stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", *argv)
    assert code == 2
    assert envelope_of(out)["code"] == "usage"
    assert stubs == []


def test_poll_options_parse(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "poll", "--pr", "o/r#1", "--repo", "h/o/r", "--full", "--no-hooks",
        "--hook", "on-merged=echo hi", "--hook", "post-poll=true")  # fmt: skip
    a = stubs[-1][1]
    assert a.pr == "o/r#1" and a.repo == "h/o/r" and a.full and a.no_hooks
    assert a.hook == ["on-merged=echo hi", "post-poll=true"]


def test_events_options_parse_as_raw_strings(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "events", "--label", "a=b", "--pr", "x", "--pr", "y", "--type", "t1",
        "--since", "garbage", "--limit", "99999")  # fmt: skip
    a = stubs[-1][1]
    # Values are validated by their owners (#7, #27), not by the frame.
    assert (a.label, a.pr, a.type, a.since, a.limit) == (
        ["a=b"],
        ["x", "y"],
        ["t1"],
        "garbage",
        "99999",
    )


def test_list_status_choices(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    for status in ("active", "terminal", "unreachable"):
        assert run(capsys, "list", "--status", status)[0] == 0


def test_export_cron_and_prune_options_parse(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "export", "--since", "1d", "--type", "a", "--pr", "p", "--label", "k=v")
    assert stubs[-1][1].since == "1d"
    run(capsys, "cron", "install", "--every", "5m")
    assert stubs[-1][1].every == "5m"
    run(capsys, "prune", "--dry-run")
    assert stubs[-1][1].dry_run is True


def test_double_dash_ends_options(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "show", "--", "--json")
    ctx, args = stubs[-1]
    assert args.pr == "--json" and not ctx.json


# --- unimplemented commands --------------------------------------------------------------------

OWNERS = {
    "watch add": 18, "watch remove": 17, "label add": 17, "label remove": 17, "list": 19,
    "prune": 20, "poll": 25, "events": 27, "show": 27, "export": 27, "cron install": 29,
    "cron uninstall": 29,
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(VALID))
def test_unimplemented_command_is_an_internal_error_naming_its_issue(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "--json", *VALID[name])
    assert code == 1
    error = envelope_of(out)
    assert error["code"] == "internal"
    assert f"#{OWNERS[name]}" in error["message"]
    assert "Traceback" not in err


# --- error envelope and exit codes -------------------------------------------------------------


def test_exit_code_table_matches_the_interface() -> None:
    assert EXIT_CODES == {
        "usage": 2, "config_invalid": 2, "invalid_pr": 2, "not_found": 10, "host_failed": 12,
        "tool_missing": 12, "tool_too_old": 12, "contract": 12, "store_too_new": 12,
        "state_unsafe": 12, "refused": 20, "internal": 1,
    }  # fmt: skip


def test_exit_code_table_covers_every_error_code_in_the_schema() -> None:
    from tests.harness import load_schema

    assert set(EXIT_CODES) == set(load_schema("defs")["$defs"]["error_code"]["enum"])


LOWER = {"code": "rate_limited", "reason": None, "retry_after": 30, "message": "slow down"}


def failing(error: CliError) -> Any:
    def handler(ctx: Context, args: Any) -> int:
        raise error

    return handler


CASES: dict[str, CliError] = {
    "usage": CliError("usage", "bad flag", details={"kind": "bad_limit"}),
    "invalid_pr": CliError("invalid_pr", "not a PR reference"),
    "not_found": CliError("not_found", "PR is not stored"),
    "host_failed": CliError("host_failed", "host unreachable", lower_error=LOWER),
    "tool_missing": CliError("tool_missing", "forgectl not found", details={"install": "pip x"}),
    "tool_too_old": CliError(
        "tool_too_old", "forgectl too old", details={"required": "1.2.0", "found": "1.0.0"}
    ),
    "contract": CliError("contract", "result violates the contract"),
    "store_too_new": CliError("store_too_new", "database is from a newer version"),
    "state_unsafe": CliError("state_unsafe", "state directory is unsafe"),
    "refused": CliError(
        "refused",
        "refused by forgectl",
        reason="unknown_host",
        lower_error={"code": None, "reason": "unknown_host", "retry_after": None, "message": None},
    ),
    "internal": CliError("internal", "boom"),
}


@pytest.mark.parametrize("code", sorted(CASES))
def test_envelope_golden_and_exit_code(
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(HANDLERS, "poll", failing(CASES[code]))
    exit_code, out, err = run(capsys, "poll", "--json")
    assert exit_code == EXIT_CODES[code]
    doc = json.loads(out)
    assert_valid("cli/error", doc)
    assert doc == json.loads((GOLDEN / f"error-{code}.json").read_text())
    assert err == f"pr-watch: error: {CASES[code].message}\n"


def test_golden_directory_has_no_stale_files() -> None:
    assert {p.name for p in GOLDEN.glob("*.json")} == {f"error-{c}.json" for c in CASES}


def test_human_mode_prints_only_stderr_on_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(HANDLERS, "poll", failing(CASES["not_found"]))
    assert run(capsys, "poll") == (10, "", "pr-watch: error: PR is not stored\n")


def test_quiet_does_not_silence_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(HANDLERS, "poll", failing(CASES["not_found"]))
    assert run(capsys, "poll", "--quiet")[2] != ""


def test_a_handler_may_return_its_own_exit_code_with_a_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(ctx: Context, args: Any) -> int:
        ctx.emit({"failed": []}, human="report")
        return 12

    monkeypatch.setitem(HANDLERS, "poll", handler)
    code, out, _ = run(capsys, "poll", "--json")
    assert (code, json.loads(out)) == (12, {"failed": []})


def test_envelope_messages_are_one_line_and_at_most_256_chars() -> None:
    env = build_envelope(CliError("usage", "a\nb\x1b[31m‮" + "x" * 1000))
    message = env["error"]["message"]
    assert len(message) <= 256 and message.endswith("...")
    assert "\n" not in message and "\x1b" not in message and "‮" not in message
    assert "<U+000A>" in message and "<U+001B>" in message and "<U+202E>" in message
    assert_valid("cli/error", env)


def test_one_line_never_cuts_a_marker() -> None:
    for pad in range(0, 12):
        text = "a" * (240 + pad) + "\x1b" * 5
        out = one_line(text)
        assert len(out) <= 256
        assert out.count("<") == out.count(">")


def test_one_line_handles_lone_surrogates_from_argv() -> None:
    out = one_line("bad\udcffarg")
    assert out == "bad<U+DCFF>arg"
    out.encode("utf-8")


def test_refused_requires_a_reason_and_others_have_null_reason() -> None:
    assert build_envelope(CliError("refused", "m", reason="r"))["error"]["reason"] == "r"
    assert build_envelope(CliError("usage", "m"))["error"]["reason"] is None


def test_required_details_are_enforced_for_tool_errors() -> None:
    with pytest.raises(ValueError):
        CliError("tool_missing", "m")
    with pytest.raises(ValueError):
        CliError("tool_too_old", "m", details={"required": "1"})


def test_human_output_is_scrubbed_of_control_characters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = Context(json=False, quiet=False, verbose=False, loaded=None)  # type: ignore[arg-type]
    ctx.emit({}, human="ok\tline\x1b[31m\x85\nnext")
    assert capsys.readouterr().out == "ok\tline<U+001B>[31m<U+0085>\nnext\n"


def test_refused_without_a_reason_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        CliError("refused", "m")
    with pytest.raises(ValueError):
        CliError("usage", "m", reason="r")
    with pytest.raises(ValueError):
        CliError("nope", "m")  # type: ignore[arg-type]


def test_unexpected_exception_does_not_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "ghp_SECRETTOKEN host text <script>"

    def handler(ctx: Context, args: Any) -> int:
        raise RuntimeError(secret)

    monkeypatch.setitem(HANDLERS, "poll", handler)
    code, out, err = run(capsys, "poll", "--json", "--verbose")
    assert code == 1
    assert secret not in out and secret not in err
    assert "Traceback" not in out and "Traceback" not in err
    assert "File " not in err
    error = envelope_of(out)
    assert error["code"] == "internal"
    assert "\n" not in error["message"]


def test_unexpected_exception_in_human_mode_prints_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(ctx: Context, args: Any) -> int:
        raise KeyError("secret")

    monkeypatch.setitem(HANDLERS, "poll", handler)
    code, out, err = run(capsys, "poll")
    assert (code, out) == (1, "")
    assert err.count("\n") == 1 and "secret" not in err


def test_keyboard_interrupt_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # TODO(#25) owns the signal contract (exit 128+N); the frame must not eat it.
    def handler(ctx: Context, args: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(HANDLERS, "poll", handler)
    with pytest.raises(KeyboardInterrupt):
        main(["poll"])


# --- config wiring -----------------------------------------------------------------------------


def write_config(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def test_config_flag_loads_the_file_and_reaches_the_handler(
    tmp_path: Path, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_path / "c.yaml", "poll:\n  concurrency: 3\n")
    for argv in (["--config", str(cfg), "poll"], ["poll", "--config", str(cfg)]):
        assert run(capsys, *argv)[0] == 0
        assert stubs[-1][0].loaded.config.poll.concurrency == 3


def test_default_config_is_used_without_the_flag(
    stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "poll")
    assert stubs[-1][0].loaded.files == ()


def test_missing_explicit_config_is_config_invalid(
    tmp_path: Path, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", "--config", str(tmp_path / "nope.yaml"), "poll")
    assert code == 2
    assert envelope_of(out)["code"] == "config_invalid"
    assert stubs == []


def test_bad_config_value_names_the_key_and_never_echoes_the_value(
    tmp_path: Path, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_path / "c.yaml", "poll:\n  concurrency: hunter2-secret-value\n")
    code, out, err = run(capsys, "--json", "--config", str(cfg), "poll")
    assert code == 2
    error = envelope_of(out)
    assert error["code"] == "config_invalid"
    assert "poll.concurrency" in error["message"]
    assert error["details"]["key"] == "poll.concurrency"
    assert error["details"]["source"] == str(cfg)
    assert "hunter2-secret-value" not in out + err
    assert stubs == []


def test_unsafe_config_mode_is_config_invalid_in_human_mode(
    tmp_path: Path, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_path / "c.yaml", "", mode=0o666)
    code, out, err = run(capsys, "--config", str(cfg), "poll")
    assert (code, out) == (2, "")
    assert err.startswith("pr-watch: error: ") and str(cfg) in err


def test_config_environment_value_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
    stubs: list[tuple[Context, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PR_WATCH_POLL__CONCURRENCY", "tokentokentoken")
    code, out, err = run(capsys, "--json", "poll")
    assert code == 2
    assert "tokentokentoken" not in out + err
    assert "PR_WATCH_POLL__CONCURRENCY" in envelope_of(out)["details"]["source"]


def test_config_error_more_problems_are_in_the_message() -> None:
    exc = ConfigError("f.yaml", "a.b", "is invalid", more=("c.d: is invalid",))
    env = build_envelope(exc)["error"]
    assert env["code"] == "config_invalid"
    assert "a.b" in env["message"] and "c.d" in env["message"]
    assert_valid("cli/error", build_envelope(exc))


def test_config_error_without_key_or_source_has_no_details() -> None:
    env = build_envelope(ConfigError(None, None, "bad"))["error"]
    assert "details" not in env


def test_config_error_text_is_sanitized() -> None:
    env = build_envelope(ConfigError("f\x1b.yaml", "k", "m\nx"))["error"]
    assert "\x1b" not in env["message"] and "\n" not in env["message"]
    assert "\x1b" not in env["details"]["source"]


@pytest.mark.parametrize("path", ["", "a\x00b"])
def test_odd_config_paths_do_not_crash(
    path: str, stubs: list[tuple[Context, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", "--config", path, "poll")
    assert code == 2
    assert envelope_of(out)["code"] == "config_invalid"


def test_help_and_version_do_not_load_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = str(tmp_path / "missing.yaml")
    with pytest.raises(SystemExit) as exc:
        main(["--config", bad, "--version"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc:
        main(["--config", bad, "poll", "--help"])
    assert exc.value.code == 0


def test_usage_errors_win_over_config_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", "--config", str(tmp_path / "missing.yaml"), "show")
    assert code == 2 and envelope_of(out)["code"] == "usage"


def test_module_exports_the_dispatch_table() -> None:
    assert cli_module.HANDLERS is HANDLERS
