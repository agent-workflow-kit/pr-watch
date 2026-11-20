"""One poll's snapshot stage in its own process, for tests/test_snapshots.py (two concurrent
pools on one state directory). Usage: pool_proc.py STATE_DIR GO_FILE; prints one JSON line."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pr_watch import enumeration as en
from pr_watch import selection as sel
from pr_watch import snapshots as sn
from pr_watch.config import Config
from pr_watch.forgectl_client import ForgectlClient, resolve_executable
from pr_watch.leases import Leases
from pr_watch.state_dir import open_state_dir
from pr_watch.store import open_store


def main(state_dir: str, go: str) -> None:
    exe = resolve_executable(os.environ["PATH"])
    assert exe is not None
    client = ForgectlClient(
        exe, timeout_ms=20000, max_output=4 * 1024 * 1024, env=dict(os.environ),
        deadline_slack=10.0,
    )  # fmt: skip
    with open_state_dir(Path(state_dir)) as state, open_store(state) as store:
        plan = sel.HostPlan("github.com", concurrency=2)
        chosen = sel.choose_snapshots(
            store.conn, {plan.host: plan}, now=datetime.now(UTC), pending_max_age_ms=10**9
        )
        deadline = time.monotonic() + 20
        while not Path(go).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        res = en.EnumerationResult(en.HostList((), frozenset(), True))
        out = sn.run_snapshots(
            store, client, Config(), chosen, res,
            leases=Leases(store, timedelta(minutes=6)),
        )  # fmt: skip
        run = out.host("github.com")
        print(json.dumps({
            "pid": os.getpid(), "needed": out.needed, "snapshots": run.snapshots,
            "locked": run.skipped_locked, "conflict": run.skipped_conflict,
            "failures": [f.code for f in res.failures],
        }))  # fmt: skip


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
