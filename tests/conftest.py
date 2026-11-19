"""Test isolation: nothing a test runs may reach the user's real home, state, crontab or forgectl.

The guards fail closed. At pytest_configure (so collection and session fixtures are covered) and
again per test (autouse), HOME and every XDG directory and TMPDIR point into a tmp dir, secrets
and tool-config variables are scrubbed (see `scrub_env`), and PATH holds only the tests' fake
binaries (tests/fakebin) plus a sandbox bin directory. The sandbox bin holds:

- symlinks to a small allowlist of harmless system tools (never git, ssh or crontab),
- `python3`/`python` (wrapper scripts exec'ing the interpreter running the tests, so the venv
  holds) and `pr-watch` if it sits beside that interpreter,
- stubs for `forgectl` and `crontab` that exit 99 and record the hit in `hits.log`.

A test that needs forgectl or crontab must put a fake in tests/fakebin. A stub hit fails the test
at teardown even if the code under test swallowed the failure, unless the test carries the
`expect_guard_hit` marker. Because PATH can be bypassed (absolute paths, an explicit `env=`),
`subprocess.Popen` is also wrapped: launching a `crontab` or `forgectl` binary from outside the
sandbox bin or tests/fakebin raises.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.gateway_support import fake_gateway, fake_tool, reaper  # noqa: E402,F401  (fixtures)

pytest_plugins = ["pytester"]  # tests/test_layout.py runs an inner pytest session

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
GUARDED_TOOLS = ("forgectl", "crontab")
# Harmless tools hooks and process tests need by name; everything else is unreachable.
ALLOWED_TOOLS = (
    "sh", "env", "cat", "sleep", "true", "false", "echo", "printf", "tee", "head",
    "mkdir", "touch", "rm", "ls", "grep", "date", "wc", "sort", "kill", "mktemp", "timeout",
    "setsid",
)  # fmt: skip
GUARD_EXIT = 99
HITS_LOG = "hits.log"
XDG_SUBDIRS = (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_STATE_HOME", ".local/state"),
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_CACHE_HOME", ".cache"),
)
SESSION_PREFIX = "pr-watch-session-"
_SESSION: dict[str, Path] = {}


def scrub_names(environ: Mapping[str, str]) -> list[str]:
    """The variable names of `environ` that must not reach a test."""
    exact = {"PYTEST_ADDOPTS", "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN"}
    prefixes = ("PR_WATCH_", "FORGECTL_", "GIT_", "TMUX")
    return [
        name
        for name in environ
        if name in exact
        or name.startswith(prefixes)
        or "TOKEN" in name.upper()
        or "SECRET" in name.upper()
    ]


def scrub_env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Drop every scrubbed variable from os.environ (undone by monkeypatch); returns the names."""
    names = scrub_names(os.environ)
    for name in names:
        monkeypatch.delenv(name)
    return names


def build_sandbox_bin(directory: Path) -> Path:
    """Fill `directory` with allowlisted symlinks, python/pr-watch and fail-closed stubs."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ALLOWED_TOOLS:
        found = shutil.which(name)
        if found and not (directory / name).exists():
            (directory / name).symlink_to(found)
    for name in ("python3", "python"):
        # A symlink would make python resolve pyvenv.cfg next to the link and lose the venv, so
        # a wrapper script execs the interpreter by its real path instead.
        wrapper = directory / name
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        wrapper.chmod(0o755)
    script = Path(sys.executable).parent / "pr-watch"
    if script.exists():
        (directory / "pr-watch").symlink_to(script)
    for name in GUARDED_TOOLS:
        stub = directory / name
        stub.write_text(
            f"#!/bin/sh\n"
            f'echo "$0" >> "${{0%/*}}/{HITS_LOG}"\n'
            f"echo 'tests must not run the real {name}; add a fake to tests/fakebin' >&2\n"
            f"exit {GUARD_EXIT}\n"
        )
        stub.chmod(0o755)
    (directory / HITS_LOG).touch()
    return directory


def _apply_home(setenv: Any, root: Path) -> Path:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    for var, sub in XDG_SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
        setenv(var, str(home / sub))
    setenv("HOME", str(home))
    tmp = root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    setenv("TMPDIR", str(tmp))
    return home


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "expect_guard_hit: the test deliberately runs a guard stub (forgectl/crontab)"
    )
    root = Path(tempfile.mkdtemp(prefix=SESSION_PREFIX)).resolve()
    _SESSION["root"] = root
    _SESSION["bin"] = build_sandbox_bin(root / "sandbox-bin")
    for name in scrub_names(os.environ):
        del os.environ[name]
    _apply_home(os.environ.__setitem__, root)
    for var in ("XDG_RUNTIME_DIR", "XDG_CONFIG_DIRS", "XDG_DATA_DIRS"):
        os.environ.pop(var, None)
    os.environ["PATH"] = os.pathsep.join([str(FAKEBIN), str(_SESSION["bin"])])


def pytest_unconfigure(config: pytest.Config) -> None:
    root = _SESSION.pop("root", None)
    _SESSION.pop("bin", None)
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def sandbox_bin() -> Path:
    """The session's sandbox bin directory (created at pytest_configure)."""
    return _SESSION["bin"]


def _first_program(args: Any, shell: bool) -> str | None:
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        if not shell:
            return text
        try:
            tokens = shlex.split(text)
        except ValueError:
            return None
        return tokens[0] if tokens else None
    try:
        first = next(iter(args), None)
    except TypeError:
        return None
    return None if first is None else os.fsdecode(first)


def forbidden_launch(
    args: Any, env: Mapping[str, str] | None, shell: bool, allowed_dirs: tuple[Path, ...]
) -> str | None:
    """The offending real path if `args` would start a real crontab/forgectl, else None."""
    program = _first_program(args, shell)
    if not program:
        return None
    search = (env if env is not None else os.environ).get("PATH", os.defpath)
    found = shutil.which(program, path=search)
    candidates = [Path(found)] if found else []
    if os.sep in program:
        candidates.append(Path(program))  # also catches a path that does not pass which()
    for candidate in candidates:
        real = Path(os.path.realpath(candidate))
        if real.name not in GUARDED_TOOLS and candidate.name not in GUARDED_TOOLS:
            continue
        if not real.exists() and not found:
            continue
        absolute = Path(os.path.abspath(candidate))
        if any(absolute.is_relative_to(d) for d in allowed_dirs):
            continue
        return str(real)
    return None


@pytest.fixture(autouse=True)
def isolated_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sandbox_bin: Path,
    request: pytest.FixtureRequest,
) -> Iterator[Path]:
    """HOME, XDG_*, TMPDIR and PATH confined to tmp; yields the fake HOME."""
    home = _apply_home(monkeypatch.setenv, tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_DIRS", raising=False)
    monkeypatch.delenv("XDG_DATA_DIRS", raising=False)
    scrub_env(monkeypatch)
    monkeypatch.setenv("PATH", os.pathsep.join([str(FAKEBIN), str(sandbox_bin)]))

    allowed = (sandbox_bin.resolve(), FAKEBIN)
    hits = sandbox_bin / HITS_LOG
    original_init = subprocess.Popen.__init__

    def guarded_init(self: Any, args: Any, *a: Any, **kw: Any) -> None:
        bad = forbidden_launch(args, kw.get("env"), bool(kw.get("shell")), allowed)
        if bad:
            with hits.open("a", encoding="utf-8") as fh:
                fh.write(f"popen:{bad}\n")
            raise AssertionError(f"tests must not run the real {Path(bad).name}: {bad}")
        original_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    hits.write_text("", encoding="utf-8")
    yield home
    recorded = hits.read_text(encoding="utf-8").strip()
    hits.write_text("", encoding="utf-8")
    if recorded and request.node.get_closest_marker("expect_guard_hit") is None:
        pytest.fail(f"a guard stub or launch guard was hit (swallowed?):\n{recorded}")
