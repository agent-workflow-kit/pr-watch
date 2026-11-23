"""The `renamed` event (docs/interface.md, Events): the stored `pr` and `repo` of a row change for
the same `pr_id`.

A row's name is refreshed in three places: the snapshot commit (`plan_snapshot` and the over-cap
`show` fallback put `pr` and `repo` in the columns they write), the enumeration of a listing
(`_upsert_listed`) and `watch add` for a stored row. Each of them asks `rename_event` inside its
own transaction, before it writes the new name, so the comparison is against the name the
database holds at that moment: whichever path sees a rename first emits the one event, and the
others find the name already stored. A rename that two processes race on is therefore still one
event (the loser's transaction sees the winner's name).

Conservative readings (the interface is silent):

- No event for a row that has no readable digest (never baselined: the PR was not seen yet), for a
  row whose digest says `merged` (final: nothing is emitted for it) or when no valid head sha is
  available for the common `head_sha` field. The name is refreshed anyway; the last case is
  reported as a drop (`invalid_head_sha`) where the caller can log it.
- A change of letter case only is no rename (hosts treat repository names case-insensitively, and
  an event for a case flap would be noise); the name is refreshed silently.
- `renamed` fires under `rebaseline` too: it reports an identity fact, not a state change.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from .diff import DiffEvent, Drop
from .digest import digest_from_json
from .store import SHA_RE

DROP_REASON: Final = "invalid_head_sha"


def is_rename(old_pr: object, old_repo: object, new_pr: object, new_repo: object) -> bool:
    """Whether the name changed beyond letter case."""
    if not all(isinstance(v, str) for v in (old_pr, old_repo, new_pr, new_repo)):
        return False
    return (str(old_pr).casefold(), str(old_repo).casefold()) != (
        str(new_pr).casefold(),
        str(new_repo).casefold(),
    )


def _valid(sha: object) -> str | None:
    return sha if isinstance(sha, str) and SHA_RE.match(sha) else None


def rename_event(
    conn: sqlite3.Connection,
    pr_id: str,
    new_pr: object,
    new_repo: object,
    *,
    head_sha: object = None,
) -> tuple[DiffEvent | None, Drop | None]:
    """The `renamed` event for `pr_id` becoming `new_pr` and `new_repo`, read against the stored
    row (call it inside the writing transaction, before the columns change), or `(None, None)`
    when nothing fires. `head_sha` is the head the caller's new digest has, if any; else the
    stored digest's head is used."""
    row = conn.execute("SELECT pr, repo, digest FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()
    if row is None or not is_rename(row[0], row[1], new_pr, new_repo):
        return None, None
    stored: dict[str, Any] | None = digest_from_json(row[2])
    if stored is None or stored.get("state") not in ("open", "closed"):
        return None, None  # never seen, or merged (final)
    head = _valid(head_sha) or _valid(stored.get("head_sha"))
    if head is None:
        return None, Drop("renamed", DROP_REASON)
    payload = {
        "repo": new_repo,
        "head_sha": head,
        "occurred_at": None,
        "from_pr": row[0],
        "to_pr": new_pr,
        "from_repo": row[1],
        "to_repo": new_repo,
    }
    return DiffEvent("renamed", pr_id, str(new_pr), payload), None
