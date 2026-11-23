"""Fixtures of the `live` suite. Every test skips (never fails) unless `PR_WATCH_LIVE=1` and the
variables of docs/live.md are set; a malformed value fails with the variable's name. Even then the
suite runs only when the `-m` expression explicitly selects `live`."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests import live_env
from tests.live.support import (
    HostError,
    HostOps,
    Workspace,
    install_sigterm_handler,
    make_ops,
    record,
)

RUN_ID = time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"
HINT = "set PR_WATCH_LIVE=1 and the variables of docs/live.md (make live)"


@dataclass
class LiveSession:
    """What outlives one test: targets already announced and verified, logins to scrub, and the
    cleanup problems that fail the session at its end."""

    announced: set[str] = field(default_factory=set)
    verified: dict[str, str | None] = field(default_factory=dict)  # kind -> failure or None
    logins: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


@pytest.fixture(scope="session")
def live_cfg(request: pytest.FixtureRequest, live_raw: dict[str, str] | None) -> live_env.LiveEnv:
    if live_raw is None:
        pytest.skip(f"live suite not enabled: {HINT}")
    if not live_env.selects_live(str(request.config.getoption("markexpr") or "")):
        pytest.skip("live suite not selected: run it with -m live (make live); another -m skips it")
    try:
        cfg = live_env.parse(live_raw)
    except live_env.LiveConfigError as exc:
        if live_env.is_missing(exc):
            pytest.skip(f"live suite not configured: {exc}")
        pytest.fail(f"live configuration invalid: {exc}", pytrace=False)
    if not cfg.hosts:
        pytest.skip("live suite not configured: " + "; ".join(cfg.skipped.values()))
    return cfg


@pytest.fixture(scope="session")
def live_session(live_cfg: live_env.LiveEnv) -> Iterator[LiveSession]:
    """SIGTERM ends the session like an interrupt (teardown and cleanup run); cleanup problems
    collected over the session fail it at the end."""
    session = LiveSession()
    try:
        restore = install_sigterm_handler()
    except ValueError:  # not the main thread: no handler
        restore = None
    try:
        yield session
    finally:
        if restore is not None:
            restore()
    if session.problems:
        pytest.fail(
            "live cleanup left problems (docs/live.md, Cleanup): " + "; ".join(session.problems),
            pytrace=False,
        )


@pytest.fixture
def ws(
    live_cfg: live_env.LiveEnv, live_session: LiveSession, tmp_path: Path, sandbox_bin: Path
) -> Iterator[Workspace]:
    w = Workspace(tmp_path / "live", sandbox_bin, live_cfg, RUN_ID)
    yield w
    leaked = list(w.leaked_runs)
    w.close()
    assert not leaked, f"a secret was present in the unredacted output of: {', '.join(leaked)}"


@pytest.fixture
def rec(
    live_cfg: live_env.LiveEnv, ws: Workspace, request: pytest.FixtureRequest
) -> Callable[..., None]:
    """`rec(**fields)` prints one scrubbed LIVE-RESULT line for this test (docs/live.md)."""

    def _rec(**fields: object) -> None:
        record(request.node.name, live_cfg.results_file, ws.scrub, **fields)

    return _rec


_CONFIG: pytest.Config | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _CONFIG
    _CONFIG = config


def announce(line: str) -> None:
    """Write through the terminal reporter so the line shows without -s; print without one."""
    reporter: Any = _CONFIG.pluginmanager.getplugin("terminalreporter") if _CONFIG else None
    if reporter is not None:
        reporter.write_line(line)
    else:
        print(line, flush=True)


def _ops(kind: str, ws: Workspace, session: LiveSession) -> Iterator[HostOps]:
    cfg = ws.env.hosts.get(kind)
    if cfg is None:
        pytest.skip(ws.env.skipped.get(kind, f"{kind} not configured"))
    ops = make_ops(ws, cfg)
    if kind not in session.announced:
        session.announced.add(kind)
        announce(f"LIVE-TARGET {cfg.repo_id} receives branches, PRs and comments")
    if kind not in session.verified:
        try:
            ops.verify_target()
            session.verified[kind] = None
            session.logins[kind] = ops.user()[0]
        except HostError as exc:
            if kind not in session.verified:
                session.verified[kind] = str(exc)
    failure = session.verified[kind]
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    if kind in session.logins:
        ws.add_scrub(session.logins[kind], "<login>")
    try:
        yield ops
    finally:
        problems = [ws.scrub(p) for p in ops.cleanup()]
        if problems:
            print("LIVE-CLEANUP-PROBLEMS " + "; ".join(problems), flush=True)
            record("LIVE-CLEANUP-PROBLEMS", ws.env.results_file, ws.scrub, problems=problems)
            session.problems.extend(problems)


@pytest.fixture
def github_ops(ws: Workspace, live_session: LiveSession) -> Iterator[HostOps]:
    yield from _ops("github", ws, live_session)


@pytest.fixture
def gitlab_ops(ws: Workspace, live_session: LiveSession) -> Iterator[HostOps]:
    yield from _ops("gitlab", ws, live_session)


@pytest.fixture(params=live_env.KINDS)
def ops(
    request: pytest.FixtureRequest, ws: Workspace, live_session: LiveSession
) -> Iterator[HostOps]:
    """The test runs once per configured host (the other one skips)."""
    yield from _ops(request.param, ws, live_session)
