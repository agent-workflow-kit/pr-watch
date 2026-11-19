"""Package layout, entry point, live-test deselection and the isolation guards."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.conftest import GUARD_EXIT, scrub_env
from tests.harness import load_schema, schema_names

ROOT = Path(__file__).resolve().parent.parent

# docs/plan.md module map (stage in the comment). Later modules are stubs until their issue.
MODULES = [
    "gateway", "config", "cli",  # stage 0
    "refs", "forgectl_client",  # stage 1
    "store", "watches",  # stage 2
    "digest", "diff",  # stage 3
    "poller",  # stage 4
    "hooks",  # stage 5
    "cron",  # stage 6
]  # fmt: skip


@pytest.mark.parametrize("module", MODULES)
def test_every_module_of_the_map_imports(module: str) -> None:
    assert importlib.import_module(f"pr_watch.{module}").__name__ == f"pr_watch.{module}"


def test_console_script_is_declared() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pr-watch = "pr_watch.cli:main"' in text


def test_module_entry_point_runs_and_reports_version() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pr_watch", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.startswith("pr-watch ")


def test_installed_console_script_runs() -> None:
    script = Path(sys.executable).parent / "pr-watch"
    if not script.exists():
        pytest.skip("pr-watch console script not installed in this environment")
    proc = subprocess.run(
        [str(script), "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0
    assert proc.stdout.startswith("pr-watch ")


def test_console_script_entry_point_is_registered() -> None:
    eps = importlib.metadata.entry_points(group="console_scripts")
    assert {ep.name: ep.value for ep in eps}.get("pr-watch") == "pr_watch.cli:main"


def test_version_output_matches_pyproject() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "pr_watch", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.stdout.strip() == f"pr-watch {declared}"


@pytest.mark.parametrize("name", schema_names())
def test_schema_is_a_valid_draft_2020_12_schema(name: str) -> None:
    Draft202012Validator.check_schema(load_schema(name))


def test_live_tests_are_deselected_by_the_repo_configuration(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_inner="""
        import pytest

        @pytest.mark.live
        def test_needs_real_host():
            raise AssertionError("live test must not run")

        def test_plain():
            pass
        """
    )
    result = pytester.runpytest_subprocess(
        "-c",
        str(ROOT / "pyproject.toml"),
        "--rootdir",
        str(pytester.path),
        "-p",
        "no:cacheprovider",
    )
    result.assert_outcomes(passed=1, deselected=1)


def test_home_and_xdg_point_into_tmp(isolated_env: Path, tmp_path: Path) -> None:
    assert Path(os.environ["HOME"]) == isolated_env
    assert isolated_env.is_relative_to(tmp_path)
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert Path(os.environ[var]).is_relative_to(tmp_path), var


SCRUBBED = [
    "PR_WATCH_X", "FORGECTL_URL", "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "PYTEST_ADDOPTS",
    "SSH_AUTH_SOCK", "TMUX", "TMUX_PANE", "GIT_DIR", "GIT_AUTHOR_NAME", "MY_API_TOKEN",
    "SOME_SECRET_KEY",
]  # fmt: skip


def test_scrub_env_removes_credentials_and_tool_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SCRUBBED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("KEEP_ME", "y")
    removed = scrub_env(monkeypatch)
    assert set(SCRUBBED) <= set(removed)
    assert not [n for n in SCRUBBED if n in os.environ]
    assert os.environ["KEEP_ME"] == "y"


def test_tmpdir_points_into_tmp(tmp_path: Path) -> None:
    assert Path(os.environ["TMPDIR"]).is_relative_to(tmp_path)


@pytest.mark.expect_guard_hit
@pytest.mark.parametrize("tool", ["forgectl", "crontab"])
def test_guard_stubs_exit_99_by_absolute_path(tool: str, sandbox_bin: Path) -> None:
    proc = subprocess.run(
        [str(sandbox_bin / tool), "-l"], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == GUARD_EXIT
    assert "tests must not run the real" in proc.stderr


@pytest.mark.expect_guard_hit
def test_crontab_by_name_is_the_exit_99_stub() -> None:
    proc = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == GUARD_EXIT
    assert "tests must not run the real" in proc.stderr


def test_forgectl_by_name_is_the_fake_and_leaves_no_stub_hit(sandbox_bin: Path) -> None:
    # tests/fakebin/forgectl shadows the stub: with no scenario it refuses (98), and the stub's
    # hit log stays empty, so the test needs no `expect_guard_hit`
    proc = subprocess.run(
        ["forgectl", "-l"], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 98
    assert "fake-forgectl: no scenario" in proc.stderr
    assert (sandbox_bin / "hits.log").read_text() == ""


def _fake_real_crontab(tmp_path: Path) -> Path:
    real = Path("/usr/bin/crontab")
    if real.exists():
        return real
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    fake = outside / "crontab"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    return fake


@pytest.mark.expect_guard_hit
def test_absolute_path_to_a_real_crontab_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="must not run the real crontab"):
        subprocess.run([str(_fake_real_crontab(tmp_path)), "-l"], check=False)
    with pytest.raises(AssertionError, match="must not run the real crontab"):
        subprocess.run(f"{_fake_real_crontab(tmp_path)} -l", shell=True, check=False)


@pytest.mark.expect_guard_hit
def test_explicit_env_path_cannot_reach_a_real_crontab(tmp_path: Path) -> None:
    real = _fake_real_crontab(tmp_path)
    env = {"PATH": f"{real.parent}:/usr/bin:/bin", "HOME": str(tmp_path)}
    with pytest.raises(AssertionError, match="must not run the real crontab"):
        subprocess.run(["crontab", "-l"], env=env, check=False)


def test_allowlisted_python_and_tools_resolve_and_run(sandbox_bin: Path) -> None:
    py = subprocess.run(
        ["python3", "-c", "import json, sys; print(sys.version_info[0])"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (py.returncode, py.stdout.strip()) == (0, "3"), py.stderr
    venv = subprocess.run(
        ["python3", "-c", "import pr_watch, jsonschema, sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (venv.returncode, venv.stdout.strip()) == (0, sys.prefix), venv.stderr
    for tool in ("python", "kill", "timeout", "mkdir", "setsid", "mktemp"):
        assert shutil.which(tool) == str(sandbox_bin / tool), tool
    proc = subprocess.run(
        ["timeout", "5", "sh", "-c", "echo ok"], capture_output=True, text=True, check=False
    )
    assert proc.stdout.strip() == "ok"


def _inner(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, body: str) -> Any:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    pytester.makepyfile(test_inner=body)
    return pytester.runpytest_subprocess("-p", "tests.conftest", "-p", "no:cacheprovider")


def test_a_swallowed_stub_hit_fails_the_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _inner(
        pytester,
        monkeypatch,
        """
        import subprocess

        def test_swallows():
            try:
                subprocess.run(["crontab", "-l"], check=True)
            except subprocess.CalledProcessError:
                pass
        """,
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*guard stub or launch guard was hit*"])


def test_expect_guard_hit_marker_allows_the_hit_and_resets_the_log(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _inner(
        pytester,
        monkeypatch,
        """
        import subprocess
        import pytest

        @pytest.mark.expect_guard_hit
        def test_marked():
            subprocess.run(["crontab", "-l"], check=False)

        def test_clean_after():
            pass
        """,
    )
    result.assert_outcomes(passed=2)


def test_home_is_sandboxed_before_collection(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/nonexistent-real-home")
    monkeypatch.setenv("GH_TOKEN", "secret")
    result = _inner(
        pytester,
        monkeypatch,
        """
        import os
        from pathlib import Path

        HOME_AT_IMPORT = os.environ["HOME"]
        TOKEN_AT_IMPORT = os.environ.get("GH_TOKEN")

        def test_module_level_values():
            assert HOME_AT_IMPORT != "/nonexistent-real-home"
            assert "pr-watch-session-" in HOME_AT_IMPORT
            assert TOKEN_AT_IMPORT is None
        """,
    )
    result.assert_outcomes(passed=1)


def test_unlisted_system_tools_are_not_on_path() -> None:
    assert shutil.which("git") is None
