"""Fixtures of the `live` suite. Every test skips (never fails) unless `PR_WATCH_LIVE=1` and the
variables of docs/live.md are set; a malformed value fails with the variable's name."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests import live_env
from tests.live.support import HostOps, Workspace, make_ops, record

RUN_ID = time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"
HINT = "set PR_WATCH_LIVE=1 and the variables of docs/live.md (make live)"


@pytest.fixture(scope="session")
def live_cfg(live_raw: dict[str, str] | None) -> live_env.LiveEnv:
    if live_raw is None:
        pytest.skip(f"live suite not enabled: {HINT}")
    try:
        cfg = live_env.parse(live_raw)
    except live_env.LiveConfigError as exc:
        if live_env.is_missing(exc):
            pytest.skip(f"live suite not configured: {exc}")
        pytest.fail(f"live configuration invalid: {exc}", pytrace=False)
    if not cfg.hosts:
        pytest.skip("live suite not configured: " + "; ".join(cfg.skipped.values()))
    return cfg


@pytest.fixture
def ws(live_cfg: live_env.LiveEnv, tmp_path: Path, sandbox_bin: Path) -> Iterator[Workspace]:
    w = Workspace(tmp_path / "live", sandbox_bin, live_cfg, RUN_ID)
    yield w
    w.close()


@pytest.fixture
def rec(live_cfg: live_env.LiveEnv, request: pytest.FixtureRequest) -> Callable[..., None]:
    """`rec(**fields)` prints one LIVE-RESULT line for this test (docs/live.md, Recording)."""

    def _rec(**fields: object) -> None:
        record(request.node.name, live_cfg.results_file, **fields)

    return _rec


def _ops(kind: str, ws: Workspace) -> Iterator[HostOps]:
    cfg = ws.env.hosts.get(kind)
    if cfg is None:
        pytest.skip(ws.env.skipped.get(kind, f"{kind} not configured"))
    ops = make_ops(ws, cfg)
    yield ops
    problems = ops.cleanup()
    if problems:
        print("LIVE-CLEANUP-PROBLEMS " + "; ".join(problems), flush=True)


@pytest.fixture
def github_ops(ws: Workspace) -> Iterator[HostOps]:
    yield from _ops("github", ws)


@pytest.fixture
def gitlab_ops(ws: Workspace) -> Iterator[HostOps]:
    yield from _ops("gitlab", ws)


@pytest.fixture(params=live_env.KINDS)
def ops(request: pytest.FixtureRequest, ws: Workspace) -> Iterator[HostOps]:
    """The test runs once per configured host (the other one skips)."""
    yield from _ops(request.param, ws)
