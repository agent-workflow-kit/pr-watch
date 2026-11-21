"""The `pr_row` object of schemas/defs.json from a stored PR row (`watch add` prints it; `list`
and `show` share it).

Judgment calls where the interface is silent: `incomplete_snapshots` is the largest consecutive
count over the incomplete parts (0 when none); `last_full_at` is the PR's host's; a field that
the digest cannot supply is null.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from . import settle
from .digest import PARTS, digest_from_json


def _counters(text: str) -> dict[str, int]:
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if k in PARTS and isinstance(v, int) and not isinstance(v, bool)
    }


def pr_row(
    conn: sqlite3.Connection, pr_id: str, *, now: datetime, pending_max_age_ms: int
) -> dict[str, Any] | None:
    """The `pr_row` of a stored PR, or None when it is not stored."""
    r = conn.execute(
        "SELECT pr, pr_id, host, status, terminal_at, terminal_kind, digest, last_polled_at, "
        "failure_count, incomplete, unsettled_since, baselined_at FROM prs WHERE pr_id = ?",
        (pr_id,),
    ).fetchone()
    if r is None:
        return None
    digest = digest_from_json(r[6])
    d: dict[str, Any] = digest if digest is not None else {}
    raw_ci = d.get("ci")
    ci: dict[str, Any] = raw_ci if isinstance(raw_ci, dict) else {}
    raw_hold = ci.get("hold")
    stale = isinstance(raw_hold, int) and not isinstance(raw_hold, bool) and raw_hold > 0
    ci_state = None if stale or digest is None else ci.get("state")
    stuck = settle.is_unsettled(digest) and settle.is_stuck(r[10], now, pending_max_age_ms)
    counts = _counters(str(r[9]))
    full = conn.execute("SELECT last_full_at FROM hosts WHERE host = ?", (r[2],)).fetchone()
    sources = sorted(
        str(s[0]) for s in conn.execute("SELECT watch_id FROM sources WHERE pr_id = ?", (pr_id,))
    )
    baselined = d.get("baselined")
    return {
        "pr": str(r[0]),
        "pr_id": str(r[1]),
        "status": str(r[3]),
        "state": d.get("state"),
        "draft": d.get("draft"),
        "ci_state": ci_state,
        "ci_head_sha": ci.get("head_sha") if digest is not None else None,
        "ci_stale": stale,
        "ci_stuck": stuck,
        "head_sha": d.get("head_sha"),
        "last_polled_at": r[7],
        "last_full_at": None if full is None else full[0],
        "incomplete": sorted(counts),
        "incomplete_snapshots": max(counts.values(), default=0),
        "failures": int(r[8]),
        "sources": sources,
        "terminal_at": r[4],
        "terminal_kind": r[5],
        "baselined_at": r[11],
        "baselined": (
            {p: bool(baselined.get(p)) for p in PARTS} if isinstance(baselined, dict) else None
        ),
    }
