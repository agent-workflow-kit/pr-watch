"""The `unreachable` and `recovered` events (docs/interface.md, Events and Failures): a PR's
reachability, not its state.

They are the two exceptions to "a failure never produces an event":

- `unreachable` is stored in the transaction that counts the failure crossing
  `failures.unreachable_after` and sets `status` to `unreachable` (`snapshots._count`), once per
  crossing: later failures of an unreachable row cross nothing, and a flap below the threshold
  never reaches it. Its payload is the failure `code` of that failure and the `failures` count.
- `recovered` is stored in the snapshot commit of a row whose `status` is `unreachable` (the first
  success since), before the snapshot's other events: `failures` and `code` are the row's count
  and last failure code from before the success. Time spent unreachable is not recorded (no
  column holds when the row crossed), so it is not in the payload.

Neither says anything about the PR's state: `not_found` never means closed or merged, and a
recovery that shows the PR merged or closed also carries that event. Both need the common
`head_sha`, the row's stored digest head and nothing else (so a `recovered` always pairs with an
`unreachable` that had the same head); a row with no valid stored head (never baselined) gets no
event, and the caller logs the drop (`invalid_head_sha`).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from .diff import DiffEvent, Drop
from .digest import digest_from_json
from .store import SHA_RE

DROP_REASON: Final = "invalid_head_sha"


def _valid(sha: object) -> str | None:
    return sha if isinstance(sha, str) and SHA_RE.match(sha) else None


def health_event(
    conn: sqlite3.Connection,
    pr_id: str,
    kind: str,
    code: str | None,
    failures: int,
) -> tuple[DiffEvent | None, Drop | None]:
    """The `unreachable` or `recovered` event of `pr_id` (read inside the writing transaction),
    or the drop when no valid head is known. `(None, None)` when the row is gone."""
    row = conn.execute("SELECT pr, repo, digest FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()
    if row is None:
        return None, None
    stored: dict[str, Any] = digest_from_json(row[2]) or {}
    head = _valid(stored.get("head_sha"))
    if head is None:
        return None, Drop(kind, DROP_REASON)
    payload = {
        "repo": row[1],
        "head_sha": head,
        "occurred_at": None,
        "code": code,
        "failures": failures,
    }
    return DiffEvent(kind, pr_id, str(row[0]), payload), None
