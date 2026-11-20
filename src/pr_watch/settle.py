"""Unsettled tracking and the poll-owned columns of a snapshot commit (docs/interface.md: State,
Definitions; Poll cycle, step 4).

A PR is **unsettled** when `ci.state` is `pending`, `ci.hold` is above 0, or `blocked_by` holds
`mergeability_unknown`; the answer comes from the stored digest only. `unsettled_since` is set by
the commit when the PR is unsettled and it is null, unreadable or later than the commit time (a
clock stepped back), so a baseline taken unsettled by `watch add` is picked up by the next
snapshot; it is reset to the commit time when `head_sha` changes and cleared when the PR is
settled or no longer open. `ci_stuck` is unsettled for `ci.pending_max_age`.

`snapshot_columns` returns the two columns `baseline.plan_snapshot` leaves to the poller:
`snap_updated_at` (the larger of the snapshot's `updated_at` and the `listed_updated_at` read when
the snapshot started, so a snapshot older than its listing never leaves the PR looking changed)
and `unsettled_since` (only when it must change).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import format_ts


def _parse(text: object) -> datetime | None:
    """An aware timestamp as UTC, else None."""
    if not isinstance(text, str) or not text or len(text) > 64:
        return None
    try:
        moment = datetime.fromisoformat(text.upper())
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    try:
        return moment.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


_UNKNOWN = "mergeability_unknown"


def _blocker_code(item: object) -> object:
    """The code of a `blocked_by` item: the stored digest holds `{"code": ...}` mappings; a bare
    string is read as its own code."""
    return item.get("code") if isinstance(item, Mapping) else item


def is_unsettled(digest: Mapping[str, Any] | None) -> bool:
    if digest is None:
        return False
    raw_ci = digest.get("ci")
    ci = raw_ci if isinstance(raw_ci, Mapping) else {}
    hold = ci.get("hold")
    held = isinstance(hold, int) and not isinstance(hold, bool) and hold > 0
    blocked = digest.get("blocked_by")
    unknown = isinstance(blocked, list) and any(_blocker_code(b) == _UNKNOWN for b in blocked)
    return bool(ci.get("state") == "pending" or held or unknown)


def is_stuck(unsettled_since: str | None, now: datetime, pending_max_age_ms: int) -> bool:
    """For an unsettled PR: its start is at least `pending_max_age` old. An unreadable start is
    not stuck (the next commit rewrites it)."""
    since = _parse(unsettled_since)
    return since is not None and since <= now - timedelta(milliseconds=pending_max_age_ms)


def snapshot_columns(
    *,
    stored: Mapping[str, Any] | None,
    new: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    unsettled_since: str | None,
    listed_updated_at: str | None,
    now: datetime,
) -> dict[str, Any]:
    """The `snap_updated_at` and `unsettled_since` columns of one snapshot commit. `stored` is the
    digest before the snapshot, `new` the digest being stored (`DiffResult.digest`)."""
    out: dict[str, Any] = {}
    taken = _parse(snapshot.get("updated_at"))
    if taken is not None:
        listed = _parse(listed_updated_at)
        out["snap_updated_at"] = format_ts(taken if listed is None else max(taken, listed))
    if new.get("state") != "open" or not is_unsettled(new):
        out["unsettled_since"] = None
    else:
        old_head = stored.get("head_sha") if stored is not None else None
        since = _parse(unsettled_since)  # unreadable or in the future (clock stepped back): redo
        if since is None or since > now or old_head != new.get("head_sha"):
            out["unsettled_since"] = format_ts(now)
    return out
