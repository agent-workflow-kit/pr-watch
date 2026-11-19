"""Package layout, entry point, live-test deselection and the isolation guards."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import FAKEBIN, GUARD_EXIT

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


def test_pr_watch_variables_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    # The autouse fixture ran before this body; a value set by the host must not leak in.
    assert not [n for n in os.environ if n.startswith("PR_WATCH_")]


@pytest.mark.parametrize("tool", ["forgectl", "crontab"])
def test_real_forgectl_and_crontab_are_unreachable(tool: str, tmp_path: Path) -> None:
    found = shutil.which(tool)
    assert found is not None
    assert not (FAKEBIN / tool).exists(), "a fake is allowed to replace the stub"
    proc = subprocess.run([tool, "-l"], capture_output=True, text=True, timeout=15, check=False)
    assert proc.returncode == GUARD_EXIT
    assert "tests must not run the real" in proc.stderr


def test_unlisted_system_tools_are_not_on_path() -> None:
    assert shutil.which("git") is None
