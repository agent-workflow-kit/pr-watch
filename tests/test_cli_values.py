"""The CLI frame validates every value with refs before a handler runs."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pr_watch import cli as cli_module
from pr_watch.cli import HANDLERS, Context, main
from pr_watch.refs import Label, PrId, PrRef
from tests.harness import assert_valid

OWNER = ["--owner", "t"]


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every handler records its args and prints nothing."""
    calls: list[Any] = []

    def handler(ctx: Context, args: Any) -> int:
        calls.append(args)
        return 0

    for name in list(HANDLERS):
        monkeypatch.setitem(HANDLERS, name, handler)
    return calls


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def refused(
    capsys: pytest.CaptureFixture[str],
    seen: list[Any],
    code: str,
    kind: str,
    *argv: str,
    value: str | None = None,
) -> None:
    status, out, err = run(capsys, "--json", *argv)
    assert status == 2, (argv, err)
    doc = json.loads(out)
    assert_valid("cli/error", doc)
    assert (doc["error"]["code"], doc["error"]["details"]["kind"]) == (code, kind)
    assert seen == []  # no handler ran
    if value is not None:  # the offending value never reaches any output
        assert value not in out + err


# --- validated values reach the handler -------------------------------------------------------


def test_handlers_receive_validated_values(
    capsys: pytest.CaptureFixture[str], seen: list[Any]
) -> None:
    assert run(capsys, "watch", "add", "GitHub.com/O/R#7", "--owner", "me")[0] == 0
    args = seen[-1]
    assert args.pr == PrRef("github.com", "O/R", 7)
    assert args.owner == "me"

    run(capsys, "show", "gh:PR_abc")
    assert seen[-1].pr == PrId("gh:PR_abc")

    run(capsys, "watch", "add", "--mine", "--open", "--host", "H.Test", "--repo", "H.test/a/b",
        "--label", "k=v", *OWNER)  # fmt: skip
    args = seen[-1]
    assert (args.host, args.repo, args.label) == (["h.test"], ["h.test/a/b"], [Label("k", "v")])

    run(
        capsys,
        "events",
        "--since",
        "v1.abcdefg2.5",
        "--limit",
        "7",
        "--type",
        "ci",
        "--pr",
        "o/r#1",
    )
    args = seen[-1]
    assert (args.since, args.limit, args.type) == ("v1.abcdefg2.5", 7, ["ci"])
    assert args.pr == [PrRef("o", "r", 1)]

    run(capsys, "label", "add", "o/r#1", "k=v", "a=b", *OWNER)
    assert seen[-1].labels == [Label("k", "v"), Label("a", "b")]
    run(capsys, "label", "add", "--watch", "w-abcdefg234", "k=v", *OWNER)
    assert (seen[-1].watch, seen[-1].pr) == ("w-abcdefg234", None)

    run(capsys, "poll", "--pr", "https://github.com/o/r/pull/3", "--repo", "H.test/x/y",
        "--hook", "post-poll=echo hi")  # fmt: skip
    args = seen[-1]
    assert args.pr == PrRef("github.com", "o/r", 3)
    assert (args.repo, args.hook) == ("h.test/x/y", [("post-poll", "echo hi")])

    run(capsys, "export", "--since", "1d")
    assert seen[-1].since == "1d"
    run(capsys, "cron", "install", "--every", "15m")
    assert seen[-1].every == "15m"


# --- refusals --------------------------------------------------------------------------------

BAD_PR = ["a b", "github.com/o/r#0", "https://u@github.com/o/r/pull/1", "--config=/x", "-x"]

# Each form puts `{}` where the PR goes.
PR_PLACES = [
    ["watch", "add", "--owner", "t", "--", "{}"],
    ["watch", "remove", "--owner", "t", "--", "{}"],
    ["show", "--", "{}"],
    ["poll", "--pr={}"],
    ["list", "--pr={}"],
    ["events", "--pr={}"],
    ["export", "--pr={}"],
    ["label", "add", "--owner", "t", "--", "{}", "k=v"],
    ["label", "remove", "--owner", "t", "--", "{}", "k=v"],
]


@pytest.mark.parametrize("value", BAD_PR)
@pytest.mark.parametrize("place", PR_PLACES, ids=lambda p: " ".join(p[:2]))
def test_bad_pr_is_invalid_pr_everywhere(
    capsys: pytest.CaptureFixture[str], seen: list[Any], place: list[str], value: str
) -> None:
    argv = [word.replace("{}", value) for word in place]
    refused(capsys, seen, "invalid_pr", "bad_pr", *argv, value=value)


def test_dash_values_after_double_dash_are_refused_not_trusted(
    capsys: pytest.CaptureFixture[str], seen: list[Any]
) -> None:
    refused(capsys, seen, "usage", "bad_label", "label", "add", "o/r#1", "--", "-k=v", *OWNER)


@pytest.mark.parametrize(
    ("argv", "kind"),
    [
        (["watch", "add", "--mine", "--open", "--host=-x", *OWNER], "bad_host"),
        (["watch", "add", "--mine", "--open", "--host", "a b", *OWNER], "bad_host"),
        (["watch", "add", "--mine", "--open", "--repo", "h.test/../x", *OWNER], "bad_repo"),
        (["watch", "remove", "--mine", "--open", "--repo=--config=/x", *OWNER], "bad_repo"),
        (["poll", "--repo", "h.test/o/r.git"], "bad_repo"),
        (["watch", "add", "o/r#1", "--owner", "Bad Owner"], "bad_owner"),
        (["list", "--owner=-x"], "bad_owner"),
        (["list", "--label", "K=v"], "bad_label"),
        (["events", "--label", "k=v w"], "bad_label"),
        (["events", "--since", "nope"], "bad_cursor"),
        (["events", "--limit", "0"], "bad_limit"),
        (["events", "--limit", "1001"], "bad_limit"),
        (["events", "--type", "bogus"], "bad_type"),
        (["export", "--since", "5"], "bad_duration"),
        (["export", "--type", "post-poll"], "bad_type"),
        (["cron", "install", "--every", "7m"], "bad_every"),
        (["poll", "--hook", "on-bogus=x"], "bad_hook"),
        (["poll", "--hook", "post-poll"], "bad_hook"),
        (["label", "add", "--watch", "w-x", "k=v", *OWNER], "bad_watch_id"),
        (["label", "add", "o/r#1", "nokey", *OWNER], "bad_label"),
    ],
)
def test_bad_values_are_usage_errors_with_a_kind(
    capsys: pytest.CaptureFixture[str], seen: list[Any], argv: list[str], kind: str
) -> None:
    refused(capsys, seen, "usage", kind, *argv)


def test_repo_host_must_be_in_the_host_list(
    capsys: pytest.CaptureFixture[str], seen: list[Any]
) -> None:
    refused(
        capsys, seen, "usage", "bad_combination",
        "watch", "add", "--mine", "--open", "--host", "a.test", "--repo", "b.test/x/y", *OWNER,
    )  # fmt: skip
    run(
        capsys,
        "watch",
        "add",
        "--mine",
        "--open",
        "--host",
        "A.test",
        "--repo",
        "a.test/x/y",
        *OWNER,
    )
    assert len(seen) == 1


def test_validation_runs_before_the_config_is_read(
    capsys: pytest.CaptureFixture[str], seen: list[Any], tmp_path: Any
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("poll: [unclosed\n")
    code, out, _ = run(capsys, "--json", "--config", str(broken), "show", "a b")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "invalid_pr"


def test_invalid_pr_envelope_matches_the_schema(
    capsys: pytest.CaptureFixture[str], seen: list[Any]
) -> None:
    code, out, err = run(capsys, "--json", "show", "https://github.com/o/r/issues/1")
    assert code == 2
    doc = json.loads(out)
    assert_valid("cli/error", doc)
    assert doc["error"]["code"] == "invalid_pr"
    assert "host/path#N" in doc["error"]["message"]
    assert "issues" not in out + err


# --- owner defaulting -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["watch", "add", "o/r#1"],
        ["watch", "remove", "o/r#1"],
        ["label", "add", "o/r#1", "k=v"],
        ["label", "remove", "o/r#1", "k=v"],
    ],
)
def test_owner_is_required_off_a_terminal(
    capsys: pytest.CaptureFixture[str],
    seen: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: False)
    code, out, err = run(capsys, "--json", *argv)
    assert code == 2
    error = json.loads(out)["error"]
    assert (error["code"], error["details"]["kind"]) == ("usage", "owner_required")
    assert error["message"] == "--owner is required when not run from a terminal"
    assert seen == []


def test_owner_defaults_to_human_me_name_on_a_terminal(
    capsys: pytest.CaptureFixture[str],
    seen: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("me:\n  name: Jake Q\n")
    cfg.chmod(0o600)
    assert run(capsys, "--config", str(cfg), "watch", "add", "o/r#1")[0] == 0
    assert seen[-1].owner == "human:jake-q"
    assert run(capsys, "--config", str(cfg), "label", "add", "o/r#1", "k=v", "--owner", "x")[0] == 0
    assert seen[-1].owner == "x"


def test_commands_without_owner_defaulting_keep_none(
    capsys: pytest.CaptureFixture[str], seen: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: False)
    assert run(capsys, "list")[0] == 0
    assert seen[-1].owner is None
