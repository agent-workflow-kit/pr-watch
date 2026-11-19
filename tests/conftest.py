"""Test isolation: nothing a test runs may reach the user's real home, state, crontab or forgectl.

The guards are autouse and fail closed: HOME and every XDG directory point into a per-test tmp
dir, PR_WATCH_* variables are dropped, and PATH holds only the tests' fake binaries
(tests/fakebin), a small allowlist of harmless system tools, and stubs for `forgectl` and
`crontab` that exit 99. A test that needs them must put a fake in tests/fakebin.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]  # tests/test_layout.py runs an inner pytest session

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
GUARDED_TOOLS = ("forgectl", "crontab")
# Harmless tools hooks and process tests need by name; everything else is unreachable.
ALLOWED_TOOLS = ("sh", "env", "cat", "sleep", "true", "false", "echo", "printf", "tee", "head")
GUARD_EXIT = 99


@pytest.fixture(scope="session")
def sandbox_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory with allowlisted symlinks and fail-closed stubs for the guarded tools."""
    directory = tmp_path_factory.mktemp("sandbox-bin")
    for name in ALLOWED_TOOLS:
        found = shutil.which(name)
        if found:
            (directory / name).symlink_to(found)
    for name in GUARDED_TOOLS:
        stub = directory / name
        stub.write_text(
            f"#!/bin/sh\necho 'tests must not run the real {name}; add a fake to "
            f"tests/fakebin' >&2\nexit {GUARD_EXIT}\n"
        )
        stub.chmod(0o755)
    return directory


@pytest.fixture(autouse=True)
def isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sandbox_bin: Path
) -> Iterator[Path]:
    """HOME, XDG_* and PATH confined to tmp; yields the fake HOME."""
    home = tmp_path / "home"
    home.mkdir()
    for var, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        (home / sub).mkdir(parents=True)
        monkeypatch.setenv(var, str(home / sub))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_DIRS", raising=False)
    monkeypatch.delenv("XDG_DATA_DIRS", raising=False)
    for name in [n for n in os.environ if n.startswith("PR_WATCH_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", os.pathsep.join([str(FAKEBIN), str(sandbox_bin)]))
    yield home
