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
from tests.live.support import GitHubOps, HostError, Workspace, nested_bodies, wait_until

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
        "PR_WATCH_LIVE_GITHUB_TOKEN": "tok-one",
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
    cfg = live_env.parse(raw_env(tmp_path, PR_WATCH_LIVE_GITHUB_TOKEN2="tok-two"))
    assert set(cfg.hosts) == {"github"} and "gitlab" in cfg.skipped
    github = cfg.hosts["github"]
    assert (github.host, github.path, github.repo_id) == (
        "github.example", "own/repo", "github.example/own/repo",
    )  # fmt: skip
    assert github.token2 == "tok-two" and github.base_branch == "main" and not github.ci
    assert "tok-one" not in repr(github)


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
            PR_WATCH_LIVE_GITLAB_TOKEN="t",
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


def make_ws(tmp_path: Path, sandbox: Path, gh_body: str = "echo '{}'") -> Workspace:
    cfg = live_env.parse(raw_env(tmp_path))
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
    assert ws.tool_env()["LIVE_GITHUB_TOKEN"] == "tok-one"
    assert "LIVE_GITHUB_TOKEN" not in ws.tool_env(tokens=False)
    for value in ws.tool_env().values():
        assert str(Path.home()) not in value or Path.home() == Path("/")
    cfg = (ws.config_home / "forgectl" / "config.yaml").read_text(encoding="utf-8")
    assert "tok-one" not in cfg and "token_env: LIVE_GITHUB_TOKEN" in cfg
    assert str(ws.bin / "gh") in cfg
    assert ws.redact("a tok-one b") == "a <redacted> b"
    assert str(ws.bin) in ws.tool_env()["PATH"].split(os.pathsep)[0]


def test_github_ops_pass_the_token_by_environment_and_the_body_by_stdin(
    tmp_path: Path, sandbox_bin: Path
) -> None:
    ws = make_ws(tmp_path, sandbox_bin)
    # the fake logs to fixed paths: the workspace env carries no extra variables
    argv_log, token_log, stdin_log = (tmp_path / n for n in ("argv", "token", "stdin"))
    script(
        tmp_path / "real" / "gh",
        f'printf "%s" "$*" > {argv_log}; printf "%s" "$GH_TOKEN" > {token_log};'
        f' cat > {stdin_log}; echo \'{{"number": 7, "node_id": "N7"}}\'',
    )
    ops = GitHubOps(ws, ws.env.hosts["github"])
    assert ops.create_pr("br", "title", body="b", draft=False, base=None) == 7
    assert token_log.read_text() == "tok-one"
    assert "tok-one" not in argv_log.read_text()
    assert '"head": "br"' in stdin_log.read_text() and '"base": "main"' in stdin_log.read_text()
    assert "--hostname github.example" in argv_log.read_text()


def test_a_failing_host_call_raises_a_redacted_error(tmp_path: Path, sandbox_bin: Path) -> None:
    ws = make_ws(tmp_path, sandbox_bin, 'echo "denied tok-one" >&2; exit 1')
    ops = GitHubOps(ws, ws.env.hosts["github"])
    with pytest.raises(HostError) as err:
        ops.comment(1, "x")
    assert "tok-one" not in str(err.value) and "denied" in str(err.value)


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
