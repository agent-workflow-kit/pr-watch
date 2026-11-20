"""Unsettled tracking and the snapshot-commit columns owned by the poll (#22)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pr_watch import settle

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SHA1, SHA2 = "a" * 40, "b" * 40
HOUR = 3600 * 1000


def digest(ci: str | None = "success", head: str = SHA1, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"state": "open", "head_sha": head, "blocked_by": []}
    d["ci"] = {"state": ci, "hold": 0}
    d.update(extra)
    return d


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (digest("pending"), True),
        (digest("success"), False),
        (digest("failure"), False),
        (digest(None), False),
        (digest("success", blocked_by=["mergeability_unknown"]), True),
        (digest("success", blocked_by=["draft"]), False),
        ({**digest("success"), "ci": {"state": "success", "hold": 2}}, True),
        ({**digest("success"), "ci": {"state": "success", "hold": 0}}, False),
        ({**digest("success"), "ci": {"state": "success", "hold": True}}, False),
        ({**digest("success"), "ci": {"state": "success", "hold": -1}}, False),
        ({"state": "open"}, False),
        ({"state": "open", "ci": "pending"}, False),
        ({"state": "open", "blocked_by": "mergeability_unknown"}, False),
        (None, False),
    ],
)
def test_is_unsettled(d: dict[str, Any] | None, expected: bool) -> None:
    assert settle.is_unsettled(d) is expected


def test_stuck_needs_age_at_least_the_limit() -> None:
    old = (NOW - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    young = (NOW - timedelta(hours=6) + timedelta(microseconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert settle.is_stuck(old, NOW, 6 * HOUR) is True
    assert settle.is_stuck(young, NOW, 6 * HOUR) is False


@pytest.mark.parametrize("since", [None, "", "garbage", "2026-10-02T11:00:00"])
def test_stuck_is_false_without_a_readable_start(since: str | None) -> None:
    assert settle.is_stuck(since, NOW, 0) is False


def cols(
    plan: dict[str, Any],
    *,
    old: dict[str, Any] | None = None,
    since: str | None = None,
    listed: str | None = None,
    updated: str = "2026-10-02T11:00:00Z",
) -> dict[str, Any]:
    return settle.snapshot_columns(
        stored=old, new=plan, snapshot={"updated_at": updated}, unsettled_since=since,
        listed_updated_at=listed, now=NOW,
    )  # fmt: skip


STAMP = "2026-10-02T12:00:00.000000Z"


def test_becoming_unsettled_sets_unsettled_since_now() -> None:
    assert cols(digest("pending"), old=digest("success"))["unsettled_since"] == STAMP


def test_unsettled_with_no_start_sets_it_now_even_without_a_transition() -> None:
    # a baseline taken by `watch add` on an unsettled PR leaves unsettled_since null (#18)
    assert cols(digest("pending"), old=digest("pending"), since=None)["unsettled_since"] == STAMP


def test_still_unsettled_keeps_the_start() -> None:
    out = cols(digest("pending"), old=digest("pending"), since="2026-10-02T08:00:00.000000Z")
    assert "unsettled_since" not in out


def test_a_push_resets_the_start() -> None:
    out = cols(
        digest("pending", head=SHA2), old=digest("pending"), since="2026-10-02T08:00:00.000000Z"
    )
    assert out["unsettled_since"] == STAMP


def test_settled_clears_the_start() -> None:
    out = cols(digest("success"), old=digest("pending"), since="2026-10-02T08:00:00.000000Z")
    assert out["unsettled_since"] is None


def test_first_sight_unsettled_sets_the_start() -> None:
    assert cols(digest("pending"), old=None)["unsettled_since"] == STAMP


def test_terminal_clears_the_start() -> None:
    merged = digest("pending", state="merged")
    assert (
        cols(merged, old=digest("pending"), since="2026-10-02T08:00:00.000000Z")["unsettled_since"]
        is None
    )


def test_snap_updated_at_is_the_snapshot_time_when_larger() -> None:
    out = cols(digest(), listed="2026-10-02T10:00:00.000000Z", updated="2026-10-02T11:00:00Z")
    assert out["snap_updated_at"] == "2026-10-02T11:00:00.000000Z"


def test_snap_updated_at_is_the_listed_time_when_the_snapshot_is_older() -> None:
    # the snapshot is not assumed to be at least as new as the listing (V4)
    out = cols(digest(), listed="2026-10-02T11:30:00.000000Z", updated="2026-10-02T11:00:00Z")
    assert out["snap_updated_at"] == "2026-10-02T11:30:00.000000Z"


def test_snap_updated_at_without_a_listing_and_with_offsets() -> None:
    out = cols(digest(), listed=None, updated="2026-10-02T13:00:00+02:00")
    assert out["snap_updated_at"] == "2026-10-02T11:00:00.000000Z"


def test_unreadable_listed_time_loses() -> None:
    out = cols(digest(), listed="junk", updated="2026-10-02T11:00:00Z")
    assert out["snap_updated_at"] == "2026-10-02T11:00:00.000000Z"


def test_unreadable_snapshot_time_writes_no_snap_updated_at() -> None:
    assert "snap_updated_at" not in cols(digest(), listed=None, updated="junk")
