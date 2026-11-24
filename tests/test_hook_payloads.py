"""Payload goldens for the event hooks that tests/test_hooks.py does not pin (issue #91).

Together with the goldens there, every `on-<event>` payload has a schema-validated golden.
"""

from __future__ import annotations

from typing import Any

import pytest

from pr_watch import hooks
from tests.harness import assert_valid
from tests.test_hooks import check_golden, ev

AUTHOR = {"id": "77", "login": "reviewer-1", "is_bot": False}
COMMENT_ID = "gh:ic:IC_example_1"
AT = "2026-10-02T09:00:00.000000Z"

EXTRA: dict[str, dict[str, Any]] = {
    "comment": {"comment_id": COMMENT_ID, "thread_id": None, "author": AUTHOR},
    "review_comment": {
        "comment_id": "gh:rc:RC_example_1",
        "thread_id": "gh:rt:RT_example_1",
        "author": AUTHOR,
        "path": "src/app.py",
        "line": 12,
        "side": "RIGHT",
        "commit_sha": "a" * 40,
    },  # fmt: skip
    "review": {"review_id": "gh:r:R_example_1", "author": AUTHOR, "review_state": "approved"},
    "approved": {
        "reviewer": AUTHOR,
        "commit_sha": "a" * 40,
        "at": AT,
        "can_push": True,
        "approvals_at_head": 1,
    },  # fmt: skip
    "changes_requested": {
        "reviewer": AUTHOR,
        "commit_sha": "a" * 40,
        "at": AT,
        "can_push": None,
    },  # fmt: skip
    "ci": {
        "from": "pending",
        "to": "failing",
        "for_head": "a" * 40,
        "checks": [{"name": "build", "from": "pending", "to": "failing"}],
        "counts": {"passing": 0, "failing": 1, "pending": 0},
    },  # fmt: skip
    "ready": {"from_blocked_by": [{"code": "draft"}], "blocked_by": []},
    "merged": {"merged_head_sha": "a" * 40, "merge_commit_sha": "b" * 40},
    "closed": {},
}


@pytest.mark.parametrize("kind", sorted(EXTRA))
def test_the_event_hook_payload_goldens(kind: str) -> None:
    event = ev(1, kind)
    event.payload.update(EXTRA[kind])
    payload = hooks.event_payload(event)
    assert_valid(f"hooks/on-{kind}", payload)
    assert payload["hook"] == f"on-{kind}" and payload["type"] == kind
    check_golden(f"on-{kind}", payload)
