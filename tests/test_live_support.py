"""The live suite's own machinery, without a host: configuration parsing, the isolation guard's
narrow opt-in, the call-counting wrappers, token handling and the default deselection."""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests import live_env
from tests.conftest import forbidden_launch, live_allowance
from tests.live import support
from tests.live.support import (
    GitHubOps,
    GitLabOps,
    HostError,
    Workspace,
    nested_bodies,
    wait_until,
)

ROOT = Path(__file__).resolve().parent.parent


def script(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def raw_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    base = {
        "PR_WATCH_LIVE": "1",
        "PR_WATCH_LIVE_FORGECTL": str(script(tmp_path / "real" / "forgectl")),
        "PR_WATCH_LIVE_GIT": str(script(tmp_path / "real" / "git")),
        "PR_WATCH_LIVE_GH": str(script(tmp_path / "real" / "gh")),
        "PR_WATCH_LIVE_GLAB": str(script(tmp_path / "real" / "glab")),
        "PR_WATCH_LIVE_GITHUB_REPO": "github.example/own/repo",
        "PR_WATCH_LIVE_GITHUB_TOKEN": "tok-one-0001",
        "PR_WATCH_LIVE_GITHUB_THROWAWAY": "github.example/own/repo",
    }
    base.update(extra)
    return base


# --- configuration ------------------------------------------------------------------------------


def test_capture_keeps_nothing_without_the_opt_in() -> None:
    assert live_env.capture({"PR_WATCH_LIVE_FORGECTL": "/x", "PATH": "/usr/bin"}) is None
    assert live_env.capture({"PR_WATCH_LIVE": "yes", "PR_WATCH_LIVE_FORGECTL": "/x"}) is None


def test_capture_resolves_tools_and_keeps_prefixed_variables(tmp_path: Path) -> None:
    script(tmp_path / "bin" / "git")
    raw = live_env.capture(
        {"PR_WATCH_LIVE": "1", "PR_WATCH_LIVE_X": "y", "OTHER": "z", "PATH": str(tmp_path / "bin")}
    )
    assert raw is not None and raw["PR_WATCH_LIVE_X"] == "y" and "OTHER" not in raw
    assert raw["PR_WATCH_LIVE_GIT"] == str(tmp_path / "bin" / "git")


def test_parse_valid_and_partial_hosts(tmp_path: Path) -> None:
    cfg = live_env.parse(raw_env(tmp_path, PR_WATCH_LIVE_GITHUB_TOKEN2="tok-two-0002"))
    assert set(cfg.hosts) == {"github"} and "gitlab" in cfg.skipped
    github = cfg.hosts["github"]
    assert (github.host, github.path, github.repo_id) == (
        "github.example", "own/repo", "github.example/own/repo",
    )  # fmt: skip
    assert github.token2 == "tok-two-0002" and github.base_branch == "main" and not github.ci
    assert "tok-one-0001" not in repr(github)


def test_missing_required_values_skip_and_malformed_ones_fail(tmp_path: Path) -> None:
    raw = raw_env(tmp_path)
    del raw["PR_WATCH_LIVE_FORGECTL"]
    with pytest.raises(live_env.LiveConfigError) as missing:
        live_env.parse(raw)
    assert live_env.is_missing(missing.value)
    for var, value in (
        ("PR_WATCH_LIVE_GITHUB_REPO", "github.example/only-one-segment"),
        ("PR_WATCH_LIVE_GITHUB_REPO", "github.example/a/b/c"),
        ("PR_WATCH_LIVE_GITHUB_TOKEN", "has space"),
        ("PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX", "ends/"),
        ("PR_WATCH_LIVE_GITHUB_API_PREFIX", "/x"),
        ("PR_WATCH_LIVE_RESULTS_FILE", "relative.jsonl"),
        ("PR_WATCH_LIVE_FORGECTL", "forgectl"),
    ):
        with pytest.raises(live_env.LiveConfigError) as bad:
            live_env.parse(raw_env(tmp_path, **{var: value}))
        assert not live_env.is_missing(bad.value), var
        assert value not in str(bad.value) or var.endswith("REPO")


def test_gitlab_nested_groups_and_ci_flag(tmp_path: Path) -> None:
    cfg = live_env.parse(
        raw_env(
            tmp_path,
            PR_WATCH_LIVE_GITLAB_REPO="gl.example:8443/g/sub/proj",
            PR_WATCH_LIVE_GITLAB_THROWAWAY="gl.example:8443/g/sub/proj",
            PR_WATCH_LIVE_GITLAB_TOKEN="gl-token-0003",
            PR_WATCH_LIVE_GITLAB_CI="1",
            PR_WATCH_LIVE_GITLAB_API_PREFIX="/gitlab",
        )
    )
    gl = cfg.hosts["gitlab"]
    assert (gl.host, gl.path, gl.ci, gl.api_prefix) == (
        "gl.example:8443",
        "g/sub/proj",
        True,
        "/gitlab",
    )


# --- the isolation guard ------------------------------------------------------------------------


def test_guard_allowance_is_the_real_forgectl_only(tmp_path: Path) -> None:
    raw = raw_env(tmp_path)
    real = Path(os.path.realpath(raw["PR_WATCH_LIVE_FORGECTL"]))
    assert live_env.guard_allowance(raw) == (real,)
    assert live_env.guard_allowance(None) == ()
    assert live_env.guard_allowance({"PR_WATCH_LIVE_FORGECTL": "relative/forgectl"}) == ()
    crontab = script(tmp_path / "x" / "crontab")
    assert live_env.guard_allowance({"PR_WATCH_LIVE_FORGECTL": str(crontab)}) == ()


class FakeNode:
    def __init__(self, live: bool) -> None:
        self.live = live

    def get_closest_marker(self, name: str) -> object | None:
        return object() if self.live and name == "live" else None


def test_only_a_live_test_gets_the_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.conftest as conftest

    raw = raw_env(tmp_path)
    monkeypatch.setitem(conftest._SESSION, "live_raw", raw)
    assert live_allowance(FakeNode(live=False)) == ()
    assert len(live_allowance(FakeNode(live=True))) == 1
    monkeypatch.setitem(conftest._SESSION, "live_raw", None)
    assert live_allowance(FakeNode(live=True)) == ()


def test_launch_guard_without_and_with_the_allowance(tmp_path: Path) -> None:
    real = script(tmp_path / "real" / "forgectl")
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "forgectl").symlink_to(real)
    allowed_dirs = (tmp_path / "sandbox",)
    env = {"PATH": str(link_dir)}
    for program in (str(real), str(link_dir / "forgectl"), "forgectl"):
        refused = forbidden_launch([program, "--version"], env, False, allowed_dirs)
        assert refused == str(real), program
    allowance = (Path(os.path.realpath(real)),)
    for program in (str(real), str(link_dir / "forgectl"), "forgectl"):
        assert forbidden_launch([program], env, False, allowed_dirs, None, allowance) is None
    other = script(tmp_path / "other" / "forgectl")
    assert forbidden_launch([str(other)], env, False, allowed_dirs, None, allowance) == str(other)


def test_crontab_is_never_allowed_even_if_named_in_the_allowance(tmp_path: Path) -> None:
    crontab = script(tmp_path / "real" / "crontab")
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "crontab").symlink_to(crontab)
    (link_dir / "forgectl").symlink_to(crontab)  # a forgectl-named link to a crontab
    allowance = (Path(os.path.realpath(crontab)),)
    env = {"PATH": str(link_dir)}
    for program in (str(crontab), "crontab", str(link_dir / "forgectl")):
        got = forbidden_launch([program], env, False, (tmp_path / "s",), None, allowance)
        assert got == str(crontab), program


@pytest.mark.expect_guard_hit
def test_plain_tests_cannot_launch_a_real_forgectl(tmp_path: Path) -> None:
    real = script(tmp_path / "real" / "forgectl")
    with pytest.raises(AssertionError, match="must not run the real forgectl"):
        subprocess.Popen([str(real)])  # noqa: S603  (the autouse guard refuses it)


# --- workspace, wrappers and tokens -------------------------------------------------------------


def make_ws(tmp_path: Path, sandbox: Path, gh_body: str = "echo '{}'", **extra: str) -> Workspace:
    cfg = live_env.parse(raw_env(tmp_path, **extra))
    script(tmp_path / "real" / "gh", gh_body)
    return Workspace(tmp_path / "ws", sandbox, cfg, "run1")


def test_counting_wrapper_logs_calls_and_runs_the_real_tool(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    ws = make_ws(tmp_path, sandbox_bin, 'echo "ran $*"')
    mark = ws.mark()
    res = ws.run([str(ws.bin / "gh"), "api", "repos/x/y", "--hostname", "h"], env=ws.base_env())
    ws.run([str(ws.bin / "gh"), "auth", "status"], env=ws.base_env())
    assert res.out.strip() == "ran api repos/x/y --hostname h"
    assert ws.api_calls(mark, "gh") == ["gh api repos/x/y --hostname h"]
    assert ws.api_calls(mark, "glab") == []
    assert stat.S_IMODE((ws.bin / "gh").stat().st_mode) == 0o700


def test_tokens_only_in_the_environment_of_tool_runs(tmp_path: Path, sandbox_bin: Path) -> None:
    ws = make_ws(tmp_path, sandbox_bin)
    assert "LIVE_GITHUB_TOKEN" not in ws.base_env()
    assert ws.tool_env()["LIVE_GITHUB_TOKEN"] == "tok-one-0001"
    assert "LIVE_GITHUB_TOKEN" not in ws.tool_env(tokens=False)
    for value in ws.tool_env().values():
        assert str(Path.home()) not in value or Path.home() == Path("/")
    cfg = (ws.config_home / "forgectl" / "config.yaml").read_text(encoding="utf-8")
    assert "tok-one-0001" not in cfg and "token_env: LIVE_GITHUB_TOKEN" in cfg
    assert str(ws.bin / "gh") in cfg
    assert ws.redact("a tok-one-0001 b") == "a <redacted> b"
    assert str(ws.bin) in ws.tool_env()["PATH"].split(os.pathsep)[0]


def test_github_ops_pass_the_token_by_environment_and_the_body_by_stdin(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    ws = make_ws(tmp_path, sandbox_bin)
    # the fake logs to fixed paths: the workspace env carries no extra variables
    argv_log, token_log, stdin_log = (tmp_path / n for n in ("argv", "token", "stdin"))
    script(
        tmp_path / "real" / "gh",
        f'printf "%s" "$*" > {argv_log}; printf "%s" "$GH_ENTERPRISE_TOKEN" > {token_log};'
        f' cat > {stdin_log}; echo \'{{"number": 7, "node_id": "N7"}}\'',
    )
    ops = GitHubOps(ws, ws.env.hosts["github"])
    assert ops.create_pr("br", "title", body="b", draft=False, base=None) == 7
    assert token_log.read_text() == "tok-one-0001"
    assert "tok-one-0001" not in argv_log.read_text()
    assert '"head": "br"' in stdin_log.read_text() and '"base": "main"' in stdin_log.read_text()
    assert "--hostname github.example" in argv_log.read_text()


def test_a_failing_host_call_raises_a_redacted_error(tmp_path: Path, sandbox_bin: Path) -> None:
    ws = make_ws(tmp_path, sandbox_bin, 'echo "denied tok-one-0001" >&2; exit 1')
    ops = GitHubOps(ws, ws.env.hosts["github"])
    with pytest.raises(HostError) as err:
        ops.comment(1, "x")
    assert "tok-one-0001" not in str(err.value) and "denied" in str(err.value)


def test_a_child_that_overruns_is_ended_by_its_own_handle(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    ws = make_ws(tmp_path, sandbox_bin)
    sleeper = script(tmp_path / "sleeper", "exec sleep 30")
    with pytest.raises(AssertionError, match="overran"):
        ws.run([str(sleeper)], env=ws.base_env(), timeout=0.5)
    assert ws.pids


def test_wait_until_is_bounded() -> None:
    with pytest.raises(AssertionError, match="timed out"):
        wait_until(lambda: False, timeout=0.2, interval=0.05, what="never")
    value, _ = wait_until(lambda: "v", timeout=1, what="at once")
    assert value == "v"


def test_nested_bodies_ignores_the_pr_body_only() -> None:
    doc: dict[str, Any] = {
        "body": "pr",
        "reviews": [{"body": "r"}],
        "threads": [{"comments": [{"body": "c"}]}],
    }
    assert nested_bodies(doc) == ["reviews.0.body", "threads.0.comments.0.body"]
    assert nested_bodies({"body": "pr", "reviews": [{"id": 1}]}) == []


# --- default deselection and opt-in ------------------------------------------------------------


def test_every_live_module_is_marked_live() -> None:
    modules = sorted((ROOT / "tests" / "live").glob("test_*.py"))
    assert modules
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        marked = any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets)
            and "live" in ast.unparse(n.value)
            for n in tree.body
        )
        assert marked, f"{path.name} has no `pytestmark = pytest.mark.live`"


def test_make_live_selects_only_live_and_verify_does_not_include_it() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    live = makefile.split("\nlive:\n", 1)[1].split("\n\n", 1)[0]
    assert "-m live" in live and "tests/live" in live
    verify = next(x for x in makefile.splitlines() if x.startswith("verify:"))
    assert "live" not in verify


def test_live_tests_skip_without_the_opt_in(tmp_path: Path) -> None:
    """`pytest -m live` with no PR_WATCH_LIVE contacts nothing and skips every test."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}
    res = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-m", "live", "-q", "-rs", "tests/live",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-400:]
    assert "passed" not in res.stdout and "failed" not in res.stdout, res.stdout[-800:]
    assert "skipped" in res.stdout and "live suite not enabled" in res.stdout


# --- review round 1: validation, selection and confirmation -------------------------------------


def bad_value(tmp_path: Path, var: str, value: str) -> live_env.LiveConfigError:
    with pytest.raises(live_env.LiveConfigError) as err:
        live_env.parse(raw_env(tmp_path, **{var: value}))
    assert not live_env.is_missing(err.value), var
    return err.value


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("PR_WATCH_LIVE_GITHUB_REPO", "github.example/own/repo\n"),  # no trailing-newline bypass
        ("PR_WATCH_LIVE_GITHUB_REPO", "github.example/own/repo.git"),
        ("PR_WATCH_LIVE_GITHUB_REPO", "github.example/own/.."),
        ("PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX", "ok\n"),
        ("PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX", "a..b"),
        ("PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX", "a//b"),
        ("PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX", "topic.lock"),
        ("PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX", "topic."),
        ("PR_WATCH_LIVE_GITHUB_BASE_BRANCH", "a/.hidden"),
        ("PR_WATCH_LIVE_GITHUB_RENAME_TO", "repo"),
        ("PR_WATCH_LIVE_GITHUB_RENAME_TO", "REPO"),
        ("PR_WATCH_LIVE_GITHUB_RENAME_TO", "..."),
        ("PR_WATCH_LIVE_GITHUB_RENAME_TO", "other.git"),
        ("PR_WATCH_LIVE_GITHUB_TOKEN", "short"),
        ("PR_WATCH_LIVE_GITHUB_TOKEN2", "tok-one-0001"),
        ("PR_WATCH_LIVE_GITHUB_TOKEN2", "tiny"),
        ("PR_WATCH_LIVE_FORGECTL_EXTRA_CONFIG", "/nonexistent/extra.yaml"),
        ("PR_WATCH_LIVE_RESULTS_FILE", "/nonexistent-dir/results.jsonl"),
        ("PR_WATCH_LIVE_HUGE_PR", "github.example/own/repo"),
        ("PR_WATCH_LIVE_HUGE_PR", "-github.example/own/repo#1"),
        ("PR_WATCH_LIVE_HUGE_PR", "other.example/own/repo#1"),
        ("PR_WATCH_LIVE_HUGE_PR", "github.example/own/repo#0"),
    ],
)
def test_malformed_values_fail_naming_the_variable(tmp_path: Path, var: str, value: str) -> None:
    err = bad_value(tmp_path, var, value)
    assert var.removeprefix("PR_WATCH_LIVE_") in str(err) or "REPO" in str(err)
    assert "tok-one-0001" not in str(err)


@pytest.mark.parametrize("value", ["yes", "2", "true"])
def test_ci_flag_is_one_or_zero(tmp_path: Path, value: str) -> None:
    gitlab = {
        "PR_WATCH_LIVE_GITLAB_REPO": "gl.example/g/p",
        "PR_WATCH_LIVE_GITLAB_THROWAWAY": "gl.example/g/p",
        "PR_WATCH_LIVE_GITLAB_TOKEN": "gl-token-0003",
    }
    with pytest.raises(live_env.LiveConfigError, match="GITLAB_CI"):
        live_env.parse(raw_env(tmp_path, PR_WATCH_LIVE_GITLAB_CI=value, **gitlab))
    ok = live_env.parse(raw_env(tmp_path, PR_WATCH_LIVE_GITLAB_CI="0", **gitlab))
    assert ok.hosts["gitlab"].ci is False


@pytest.mark.parametrize("prefix", ["/gitlab/", "//x", "/a/../b", "gitlab"])
def test_api_prefix_must_be_a_clean_path(tmp_path: Path, prefix: str) -> None:
    extra = {
        "PR_WATCH_LIVE_GITLAB_REPO": "gl.example/g/p",
        "PR_WATCH_LIVE_GITLAB_THROWAWAY": "gl.example/g/p",
        "PR_WATCH_LIVE_GITLAB_TOKEN": "gl-token-0003",
    }
    with pytest.raises(live_env.LiveConfigError, match="API_PREFIX"):
        live_env.parse(raw_env(tmp_path, PR_WATCH_LIVE_GITLAB_API_PREFIX=prefix, **extra))


def test_valid_extras_are_accepted(tmp_path: Path) -> None:
    extra = tmp_path / "extra.yaml"
    extra.write_text("retries: 1\n")
    cfg = live_env.parse(
        raw_env(
            tmp_path,
            PR_WATCH_LIVE_FORGECTL_EXTRA_CONFIG=str(extra),
            PR_WATCH_LIVE_RESULTS_FILE=str(tmp_path / "r.jsonl"),
            PR_WATCH_LIVE_HUGE_PR="github.example/own/big#12",
            PR_WATCH_LIVE_GITHUB_RENAME_TO="renamed",
            PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX="team/live",
        )
    )
    assert cfg.huge_pr == "github.example/own/big#12"
    assert cfg.hosts["github"].rename_to == "renamed"


def test_throwaway_confirmation_skips_when_missing_and_fails_when_different(
    tmp_path: Path,
) -> None:
    raw = raw_env(tmp_path)
    del raw["PR_WATCH_LIVE_GITHUB_THROWAWAY"]
    cfg = live_env.parse(raw)
    assert "github" not in cfg.hosts
    assert "PR_WATCH_LIVE_GITHUB_THROWAWAY" in cfg.skipped["github"]
    err = bad_value(tmp_path, "PR_WATCH_LIVE_GITHUB_THROWAWAY", "github.example/own/other")
    assert "PR_WATCH_LIVE_GITHUB_THROWAWAY" in str(err) and "own/other" not in str(err)
    assert "own/repo" not in str(err)


@pytest.mark.parametrize(
    ("expr", "selected"),
    [
        ("live", True),
        ("not live", False),
        ("not slow", False),
        ("", False),
        ("live and not slow", True),
        ("(live)", True),
        ("not (live)", False),
        ("olive or alive", False),
        ("not live or live", True),
    ],
)
def test_selects_live_needs_an_explicit_live(expr: str, selected: bool) -> None:
    assert live_env.selects_live(expr) is selected


def test_a_foreign_mark_expression_runs_no_live_test(tmp_path: Path) -> None:
    """With the opt-in but `-m "not slow"`, every live test skips at once, before any config."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path),
        "PR_WATCH_LIVE": "1",
    }
    res = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-m", "not slow", "-q", "-rs", "tests/live",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert res.returncode == 0, res.stdout[-800:] + res.stderr[-400:]
    assert "live suite not selected" in res.stdout, res.stdout[-800:]
    assert "passed" not in res.stdout and "failed" not in res.stdout


# --- review round 1: helpers --------------------------------------------------------------------


def test_http_status_from_gh_and_glab_text() -> None:
    assert support.http_status("gh: Not Found (HTTP 404)") == 404
    assert support.http_status('{"message":"403 Forbidden"}') == 403
    assert support.http_status("Reference does not exist (HTTP 422)") == 422
    assert support.http_status("connection reset") is None
    assert support.fail_fast(HostError("x", 401)) and not support.fail_fast(HostError("x", 405))


def test_token_variable_follows_forgectl() -> None:
    assert support.github_token_variable("github.com") == "GH_TOKEN"
    assert support.github_token_variable("acme.ghe.com") == "GH_TOKEN"
    assert support.github_token_variable("github.example") == "GH_ENTERPRISE_TOKEN"
    assert support.github_token_variable("evilghe.com") == "GH_ENTERPRISE_TOKEN"


def test_forgectl_version_floor() -> None:
    assert support.forgectl_new_enough("forgectl 0.55.3\n")
    assert support.forgectl_new_enough("forgectl 1.0.0\n")
    assert not support.forgectl_new_enough("forgectl 0.54.9\n")
    assert not support.forgectl_new_enough("forgectl dev\n")


def test_timestamps_accept_an_offset() -> None:
    for text in (
        "2026-01-02T03:04:05Z",
        "2026-01-02T03:04:05.123456+02:00",
        "2026-01-02t03:04:05z",
    ):
        assert support.TS.fullmatch(text), text
    assert support.parse_ts("2026-01-02T05:04:05+02:00") == support.parse_ts("2026-01-02T03:04:05Z")
    assert not support.TS.fullmatch("2026-01-02 03:04:05Z")


def test_graphql_errors_include_nested_payload_errors() -> None:
    ok = {"data": {"mergeRequestRequestChanges": {"errors": []}}}
    assert support.graphql_errors(ok) == []
    nested = {"data": {"mergeRequestRequestChanges": {"errors": ["not allowed"]}}}
    assert support.graphql_errors(nested) and "not allowed" in support.graphql_errors(nested)[0]
    assert support.graphql_errors({"errors": [{"message": "x"}], "data": None})
    assert support.graphql_errors("text")


def test_wait_until_retries_errors_and_reraises_the_last() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise HostError(f"try {calls['n']}")
        return "ok"

    assert wait_until(flaky, timeout=2, interval=0.01, what="flaky")[0] == "ok"

    def always() -> None:
        raise AssertionError("still wrong")

    with pytest.raises(AssertionError, match="still wrong") as err:
        wait_until(always, timeout=0.1, interval=0.02, what="never right")
    assert any("never right" in n for n in err.value.__notes__)


def test_wait_until_gives_up_at_once_on_a_final_error() -> None:
    calls = {"n": 0}

    def denied() -> None:
        calls["n"] += 1
        raise HostError("denied", 403)

    with pytest.raises(HostError):
        wait_until(denied, timeout=30, interval=0.01, what="x", give_up=support.fail_fast)
    assert calls["n"] == 1


def test_sigterm_becomes_a_normal_interrupt() -> None:
    restore = support.install_sigterm_handler()
    try:
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), 15)
            import time

            time.sleep(1)
    finally:
        restore()


def test_unique_paths_never_repeat(tmp_path: Path, sandbox_bin: Path) -> None:
    ws = make_ws(tmp_path, sandbox_bin)
    ops = GitHubOps(ws, ws.env.hosts["github"])
    paths = {ops.unique_path("thread.txt") for _ in range(50)}
    assert len(paths) == 50 and all(p.startswith("live/") for p in paths)


def test_live_tests_use_no_fixed_content_paths() -> None:
    for path in (ROOT / "tests" / "live").glob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        assert "live/thread.txt" not in text and "live/conflict.txt" not in text, path.name


# --- review round 1: secrets, children, recording -----------------------------------------------

TOKEN = "tok/one+0001=x"


def secret_ws(tmp_path: Path, sandbox: Path) -> Workspace:
    return make_ws(
        tmp_path,
        sandbox,
        PR_WATCH_LIVE_GITHUB_TOKEN=TOKEN,
        PR_WATCH_LIVE_GITHUB_TOKEN2="tok-two-0002",
    )


def test_every_form_of_a_secret_is_redacted_and_flagged(tmp_path: Path, sandbox_bin: Path) -> None:
    import base64
    import urllib.parse

    ws = secret_ws(tmp_path, sandbox_bin)
    forms = [
        TOKEN,
        "tok-two-0002",
        urllib.parse.quote(TOKEN, safe=""),
        base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(),
        base64.b64encode(TOKEN.encode()).decode().rstrip("="),
    ]
    for form in forms:
        sh = script(tmp_path / "leak", f"printf '%s' '{form}'; printf '%s' '{form}' >&2")
        res = ws.run([str(sh)], env=ws.base_env())
        assert res.leaked and "<redacted>" in res.out and form not in res.out + res.err, form
        assert_msg = None
        with pytest.raises(AssertionError) as err:
            support.assert_no_leak(res)
        assert form not in str(err.value) and assert_msg is None
    clean = script(tmp_path / "clean", "echo fine")
    assert not ws.run([str(clean)], env=ws.base_env()).leaked
    assert ws.leaked_runs  # the workspace remembers (its fixture fails the test)


def test_an_overrunning_group_is_killed_and_the_wait_is_bounded(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    import time

    ws = make_ws(tmp_path, sandbox_bin)
    holder = script(
        tmp_path / "holder", "sleep 60 &\nexec sleep 60"
    )  # a grandchild holds the pipes
    start = time.monotonic()
    with pytest.raises(AssertionError, match="overran"):
        ws.run([str(holder)], env=ws.base_env(), timeout=0.5)
    assert time.monotonic() - start < 15


def test_a_signal_is_sent_and_its_latency_measured(tmp_path: Path, sandbox_bin: Path) -> None:
    ws = make_ws(tmp_path, sandbox_bin)
    sleeper = script(tmp_path / "sl", "exec sleep 30")
    res = ws.run([str(sleeper)], env=ws.base_env(), timeout=10, signal_after=0.2)
    assert res.rc in (143, -15) and res.latency is not None and res.latency < 5


def test_record_scrubs_names_and_writes_a_private_file(tmp_path: Path, sandbox_bin: Path) -> None:
    ws = secret_ws(tmp_path, sandbox_bin)
    GitHubOps(ws, ws.env.hosts["github"])
    ws.add_scrub("someone-login", "<login>")
    out = tmp_path / "results.jsonl"
    support.record(
        "t",
        out,
        ws.scrub,
        a="github.example/own/repo#4 by Someone-Login",
        b={"k": [f"{TOKEN} at github.example"]},
        n=3,
    )
    text = out.read_text(encoding="utf-8")
    for needle in ("github.example", "own/repo", "ome" + "one-login", TOKEN):
        assert needle.lower() not in text.lower(), needle
    assert "<repo>#4" in text and "<login>" in text
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    support.record("t2", out, ws.scrub, x=1)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2


def test_record_never_follows_a_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    link = tmp_path / "results.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        support.record("t", link, x=1)
    assert target.read_text() == "keep\n"


# --- review round 1: host calls -----------------------------------------------------------------


def test_github_token_variable_by_host_in_the_child(tmp_path: Path, sandbox_bin: Path) -> None:
    for repo, var, other in (
        ("github.com/own/repo", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"),
        ("github.example/own/repo", "GH_ENTERPRISE_TOKEN", "GH_TOKEN"),
    ):
        ws = make_ws(
            tmp_path / var,
            sandbox_bin,
            f'printf "%s|%s" "${{{var}}}" "${{{other}:-unset}}"',
            PR_WATCH_LIVE_GITHUB_REPO=repo,
            PR_WATCH_LIVE_GITHUB_THROWAWAY=repo,
        )
        ops = GitHubOps(ws, ws.env.hosts["github"])
        res = ws.run(
            [str(ws.env.gh), "x"],
            env={**ws.base_env(), var: "v"},
        )
        assert res.out == "v|unset" or res.out.startswith("v|")
        # and through the real call path: the answer is not JSON, which now raises
        with pytest.raises(HostError, match="not JSON"):
            ops.api("GET", "user")


def test_glab_environment_avoids_the_deprecated_prompt_variable(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    ws = make_ws(
        tmp_path,
        sandbox_bin,
        PR_WATCH_LIVE_GITLAB_REPO="gl.example/g/p",
        PR_WATCH_LIVE_GITLAB_THROWAWAY="gl.example/g/p",
        PR_WATCH_LIVE_GITLAB_TOKEN="gl-token-0003",
    )
    log = tmp_path / "glab-env"
    script(tmp_path / "real" / "glab", f'env > {log}; echo "{{}}"')
    GitLabOps(ws, ws.env.hosts["gitlab"]).api("GET", "user")
    names = {line.split("=", 1)[0] for line in log.read_text().splitlines()}
    assert {"GLAB_NO_PROMPT", "GLAB_SHOW_WHATS_NEW"} <= names and "NO_PROMPT" not in names


def failing_gh(tmp_path: Path, sandbox: Path, body: str) -> GitHubOps:
    ws = make_ws(tmp_path, sandbox, body)
    ops = GitHubOps(ws, ws.env.hosts["github"])
    ops._prs = [1, 2, 3]
    ops._branches = ["b1", "b2"]
    ops._merged = {2}
    ops._closed = {3}
    ops._deleted = {"b2"}
    return ops


def test_cleanup_treats_gone_as_done_and_reports_real_problems(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    ops = failing_gh(tmp_path / "a", sandbox_bin, 'echo "gh: Not Found (HTTP 404)" >&2; exit 1')
    assert ops.cleanup() == []  # #1 and branch b1 are gone; #2, #3 and b2 are not touched
    ops = failing_gh(tmp_path / "b", sandbox_bin, 'echo "gh: boom (HTTP 500)" >&2; exit 1')
    problems = ops.cleanup()
    assert len(problems) == 2 and "close #1" in problems[0] and "delete branch b1" in problems[1]


def test_merge_failure_is_reported_and_final_errors_stop_at_once(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    log = tmp_path / "calls"
    ws = make_ws(
        tmp_path,
        sandbox_bin,
        f'echo x >> {log}; case "$*" in *"-X GET"*) echo \'{{"head":{{"ref":"b"}}}}\';; '
        '*) echo "gh: Forbidden (HTTP 403)" >&2; exit 1;; esac',
    )
    ops = GitHubOps(ws, ws.env.hosts["github"])
    with pytest.raises(HostError, match="403"):
        ops.merge(5)
    assert len(log.read_text().splitlines()) == 2  # the PR read and one merge attempt only
    assert 5 not in ops._merged


def test_a_failed_rename_is_marked_and_rolled_back_in_cleanup(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    counter = tmp_path / "n"
    calls = tmp_path / "calls"
    ws = make_ws(
        tmp_path,
        sandbox_bin,
        f'echo "$*" >> {calls}; n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); '
        f'echo $n > {counter}; if [ $n -le 2 ]; then echo "gh: timeout" >&2; exit 1; fi; '
        'echo "{}"',
    )
    ops = GitHubOps(ws, ws.env.hosts["github"])
    with pytest.raises(HostError):
        ops.rename_repo("new-name")
    assert ops.rename_pending == "own/new-name" and ops.path == "own/repo"
    markers = ops._pending_files()
    assert markers[0].is_file() and stat.S_IMODE(markers[0].stat().st_mode) == 0o600
    assert ops.cleanup() == []  # the first try fails, the aimed-at name succeeds
    assert ops.rename_pending is None and ops.path == ops.original_path
    assert not markers[0].exists()
    lines = calls.read_text().splitlines()
    assert "repos/own/repo" in lines[1] and "repos/own/new-name" in lines[2]


def test_the_target_must_be_the_canonical_name(tmp_path: Path, sandbox_bin: Path) -> None:
    ws = make_ws(tmp_path, sandbox_bin, 'echo \'{"full_name": "own/Repo"}\'')
    with pytest.raises(HostError, match="canonical"):
        GitHubOps(ws, ws.env.hosts["github"]).verify_target()
    script(tmp_path / "real" / "gh", 'echo \'{"full_name": "own/repo"}\'')
    GitHubOps(ws, ws.env.hosts["github"]).verify_target()
