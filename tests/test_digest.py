"""The digest builder: snapshot to compact, deterministic digest (docs/interface.md, Digest)."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

from pr_watch.digest import (
    MAX_BLOCKED_BY,
    MAX_CHECKS,
    MAX_REVIEWERS,
    MAX_SEEN,
    PARTS,
    build_digest,
    check_key,
    comment_ids,
    digest_from_json,
    digest_to_json,
    incomplete_parts,
    review_ids,
    reviewer_key,
)
from tests.forgectl_support import assert_forgectl_valid, golden, new_pr, sha

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "digest"
MARKER = re.compile(r"<U\+[0-9A-F]{4,6}>")


def gh(**changes: Any) -> dict[str, Any]:
    return new_pr(**changes)


def gl(**changes: Any) -> dict[str, Any]:
    return new_pr("gitlab.com", "example-org/widgets", backend="gitlab", **changes)


def check(name: str, state: str = "passing", url: str | None = None, counted: bool = True) -> Any:
    return {
        "name": name,
        "state": state,
        "required": None,
        "counted": counted,
        "url": url,
        "finished_at": None,
    }


def with_checks(snap: dict[str, Any], checks: list[Any]) -> dict[str, Any]:
    snap = copy.deepcopy(snap)
    snap["ci"]["checks"] = checks
    return snap


def comment(cid: str) -> dict[str, Any]:
    return {
        "id": cid,
        "author": {"id": "1", "login": "user-1", "is_bot": False},
        "body": "SECRET BODY TEXT",
        "created_at": "2026-10-02T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
        "minimized": False,
        "reply_to": None,
        "review_id": None,
    }


def thread(tid: str, ids: list[str], path: str | None = None) -> dict[str, Any]:
    return {
        "id": tid,
        "resolvable": False,
        "resolved": False,
        "outdated": None,
        "path": path,
        "line": None,
        "side": None,
        "start_line": None,
        "commit_sha": None,
        "comments": [comment(i) for i in ids],
    }


# --- goldens and determinism ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("github-open", lambda: golden("snapshot-github-open")),
        ("gitlab-open", lambda: golden("snapshot-gitlab-open")),
    ],
)
def test_digest_matches_golden(name: str, build: Any) -> None:
    snap = build()
    assert_forgectl_valid("snapshot", snap)
    text = digest_to_json(build_digest(snap))
    assert text + "\n" == (GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("build", [gh, gl])
def test_unchanged_snapshot_gives_byte_identical_digest(build: Any) -> None:
    a = build()
    b = copy.deepcopy(a)
    b["fetched_at"] = "2030-01-01T00:00:00Z"
    b["rate_limit"] = {"host": "x.example", "remaining": 1, "reset_at": None}
    b["updated_at"] = "2030-01-01T00:00:00Z"
    b["activity"]["others_since_push"] = 99
    assert digest_to_json(build_digest(a)) == digest_to_json(build_digest(b))


def test_digest_is_independent_of_list_order() -> None:
    a = gh()
    b = copy.deepcopy(a)
    b["ci"]["checks"].reverse()
    b["threads"].reverse()
    b["reviews"].reverse()
    b["blocked_by"] = [{"code": "draft"}, {"code": "conflicts"}]
    a["blocked_by"] = [{"code": "conflicts"}, {"code": "draft"}]
    assert digest_to_json(build_digest(a)) == digest_to_json(build_digest(b))


def test_digest_json_is_compact_sorted_ascii_and_round_trips() -> None:
    digest = build_digest(gh())
    text = digest_to_json(digest)
    assert "\n" not in text and ": " not in text and ", " not in text
    assert text.isascii()
    assert json.loads(text) == digest
    assert digest_to_json(digest_from_json(text) or {}) == text
    keys = list(json.loads(text))
    assert keys == sorted(keys)


def test_digest_json_forces_ascii() -> None:
    digest = build_digest(gh(ci={"checks": [check("tést ✓", "passing")]}))
    assert digest_to_json(digest).isascii()


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '"x"', "{"])
def test_digest_from_json_rejects_non_objects(text: str) -> None:
    assert digest_from_json(text) is None
    assert digest_from_json(None) is None


# --- fields --------------------------------------------------------------------------------


def test_github_digest_fields() -> None:
    snap = gh(
        draft=True,
        mergeable=None,
        merged_head_sha=sha(3),
        merge_commit_sha=sha(4),
        blocked_by=[{"code": "reviews_required"}, {"code": "draft"}],
    )
    d = build_digest(snap)
    assert d["head_sha"] == snap["head"]["sha"]
    assert d["state"] == "open"
    assert d["draft"] is True
    assert d["mergeable"] is None
    assert d["blocked_by"] == [{"code": "draft"}, {"code": "reviews_required"}]
    assert d["pushed_at"] == snap["head"]["pushed_at"]
    assert d["merged_head_sha"] == sha(3)
    assert d["merge_commit_sha"] == sha(4)
    assert d["review_decision"] == "approved"
    assert d["approvals_at_head"] == 1
    assert d["baselined"] == {"reviews": True, "threads": True, "ci": True}
    assert d["ci"]["state"] == "passing"
    assert d["ci"]["head_sha"] == snap["ci"]["head_sha"]
    assert d["ci"]["hold"] == 0
    assert d["ci"]["hold_to"] is None
    assert d["ci"]["checks_more"] == 0


def test_digest_has_exactly_the_documented_keys() -> None:
    # The digest is an internal storage shape with no JSON schema under schemas/; the key list
    # is specified in docs/interface.md, Digest.
    d = build_digest(gh())
    assert set(d) == {
        "head_sha", "state", "draft", "mergeable", "blocked_by", "ci", "review_decision",
        "approvals_at_head", "reviewers", "seen", "pushed_at", "merged_head_sha",
        "merge_commit_sha", "baselined",
    }  # fmt: skip
    assert set(d["seen"]) == {"comments", "reviews"}
    assert set(d["ci"]) == {"state", "head_sha", "hold", "hold_to", "checks", "checks_more"}


def test_no_host_text_beyond_one_line_fields_is_kept() -> None:
    snap = gh(title="SECRET TITLE", body="SECRET BODY", labels=["SECRET LABEL"])
    snap["head"]["ref"] = "SECRET-BRANCH"
    snap["ci"]["checks"] = [check("build", url="https://example.org/SECRET-URL")]
    text = digest_to_json(build_digest(snap))
    for secret in ("SECRET", "example.org", "user-2", "src/parser.py"):
        assert secret not in text


def test_merged_snapshot() -> None:
    snap = gh(state="merged", merged_head_sha=sha(5), merge_commit_sha=sha(6))
    d = build_digest(snap)
    assert (d["state"], d["merged_head_sha"], d["merge_commit_sha"]) == ("merged", sha(5), sha(6))


def test_blocked_by_other_keeps_truncated_detail_and_sorts() -> None:
    detail = "x" * 1000
    snap = gh(blocked_by=[{"code": "other", "detail": detail}, {"code": "conflicts"}])
    d = build_digest(snap)
    assert d["blocked_by"][0] == {"code": "conflicts"}
    got = d["blocked_by"][1]
    assert got["code"] == "other" and len(got["detail"]) == 256 and got["detail"].endswith("...")


def test_blocked_by_duplicates_collapse() -> None:
    d = build_digest(gh(blocked_by=[{"code": "draft"}, {"code": "draft"}]))
    assert d["blocked_by"] == [{"code": "draft"}]


def test_unknown_blocked_by_code_maps_to_other_with_the_code_as_detail() -> None:
    d = build_digest(gh(blocked_by=[{"code": "brand_new"}]))
    assert d["blocked_by"] == [{"code": "other", "detail": "brand_new"}]


def test_malformed_blocked_by_entries_are_dropped() -> None:
    d = build_digest(
        gh(blocked_by=["draft", 3, {}, {"code": 5}, {"code": "other"}, {"code": "draft"}])
    )
    assert d["blocked_by"] == [{"code": "draft"}, {"code": "other", "detail": "unknown"}]


def test_blocked_by_detail_is_sanitized() -> None:
    d = build_digest(gh(blocked_by=[{"code": "other", "detail": "a\x1b[31m‮b"}]))
    assert d["blocked_by"] == [{"code": "other", "detail": "a<U+001B>[31m<U+202E>b"}]


# --- GitHub vs GitLab ----------------------------------------------------------------------


def test_gitlab_digest_has_no_review_objects_and_null_approvals_at_head() -> None:
    d = build_digest(gl())
    assert d["seen"]["reviews"] == []
    assert d["approvals_at_head"] is None
    assert d["reviewers"]  # latest_by_reviewer still feeds approvals
    (entry,) = d["reviewers"].values()
    assert entry == {"state": "approved", "commit_sha": None, "at": None, "can_push": None}


def test_approvals_at_head_null_is_unknown_never_zero() -> None:
    assert build_digest(gl())["approvals_at_head"] is None
    assert build_digest(gh(approvals_at_head=0))["approvals_at_head"] == 0
    assert build_digest(gh(approvals_at_head=None))["approvals_at_head"] is None


@pytest.mark.parametrize("bad", ["1", True, -1, 1.5, [], {}])
def test_approvals_at_head_that_is_not_a_count_is_unknown(bad: Any) -> None:
    assert build_digest(gh(approvals_at_head=bad))["approvals_at_head"] is None


def test_gitlab_comment_ids_come_from_note_ids() -> None:
    d = build_digest(gl())
    assert d["seen"]["comments"] == ["gl:n:901", "gl:n:902", "gl:n:903", "gl:n:904", "gl:n:905"]


def test_github_seen_sets_hold_comment_and_review_ids_sorted() -> None:
    d = build_digest(gh())
    assert d["seen"]["reviews"] == ["gh:r:PRR_kwDOexample0001", "gh:r:PRR_kwDOexample0002"]
    assert d["seen"]["comments"] == sorted(d["seen"]["comments"])
    assert "gh:rc:PRRC_kwDOexample0002" in d["seen"]["comments"]
    assert "gh:ic:IC_kwDOexample0001" in d["seen"]["comments"]
    assert all(i.startswith(("gh:ic:", "gh:rc:")) for i in d["seen"]["comments"])


def test_reviewers_keyed_by_id_then_lowercased_login() -> None:
    snap = gh()
    snap["latest_by_reviewer"] = [
        {"login": "User-A", "id": None, "state": "approved", "commit_sha": sha(1),
         "at": "2026-10-02T09:00:00Z", "can_push": True},
        {"login": "user-b", "id": None, "state": "changes_requested", "commit_sha": None,
         "at": None, "can_push": None},
        {"login": "user-c", "id": "77", "state": "dismissed", "commit_sha": sha(2),
         "at": None, "can_push": False},
    ]  # fmt: skip
    d = build_digest(snap)
    assert set(d["reviewers"]) == {"login:user-a", "login:user-b", "77"}
    assert d["reviewers"]["login:user-a"] == {
        "state": "approved", "commit_sha": sha(1), "at": "2026-10-02T09:00:00Z", "can_push": True,
    }  # fmt: skip
    assert d["reviewers"]["77"]["state"] == "dismissed"


def test_reviewer_key_function() -> None:
    assert reviewer_key({"id": "9", "login": "X"}) == "9"
    assert reviewer_key({"id": None, "login": "AbC"}) == "login:abc"
    assert reviewer_key({"id": None, "login": None}) is None
    assert reviewer_key({"id": "", "login": "x"}) == "login:x"
    assert reviewer_key({"id": 5, "login": "x"}) == "login:x"
    assert reviewer_key("nope") is None


def test_overlong_login_key_is_bounded_and_collision_free() -> None:
    a = reviewer_key({"id": None, "login": "a" * 3000 + "1"})
    b = reviewer_key({"id": None, "login": "a" * 3000 + "2"})
    assert a is not None and b is not None and a != b
    assert len(a) <= 256 and len(b) <= 256 and a.startswith("login:")


def test_reviewer_with_neither_id_nor_login_is_skipped_and_nulls_tolerated() -> None:
    snap = gh()
    snap["latest_by_reviewer"] = [
        {
            "login": None,
            "id": None,
            "state": "approved",
            "commit_sha": None,
            "at": None,
            "can_push": None,
        },
        {
            "login": "ok",
            "id": None,
            "state": "approved",
            "commit_sha": None,
            "at": None,
            "can_push": None,
        },
    ]
    d = build_digest(snap)
    assert list(d["reviewers"]) == ["login:ok"]


def test_unknown_reviewer_state_is_marked_unknown_without_previous() -> None:
    snap = gh()
    snap["latest_by_reviewer"][0]["state"] = "brand_new"
    d = build_digest(snap)
    assert next(iter(d["reviewers"].values()))["state"] == "unknown"


def test_unknown_reviewer_state_keeps_the_previous_entry() -> None:
    previous = build_digest(gh())
    snap = gh()
    snap["latest_by_reviewer"][0]["state"] = "brand_new"
    d = build_digest(snap, previous)
    assert d["reviewers"] == previous["reviewers"]


def test_duplicate_reviewer_entries_resolve_deterministically() -> None:
    e1 = {"login": "u", "id": "1", "state": "approved", "commit_sha": None,
          "at": "2026-10-02T09:00:00Z", "can_push": None}  # fmt: skip
    e2 = {**e1, "state": "changes_requested", "at": "2026-10-03T09:00:00Z"}
    a, b = gh(), gh()
    a["latest_by_reviewer"] = [e1, e2]
    b["latest_by_reviewer"] = [e2, e1]
    assert digest_to_json(build_digest(a)) == digest_to_json(build_digest(b))
    assert build_digest(a)["reviewers"]["1"]["state"] == "changes_requested"


# --- checks --------------------------------------------------------------------------------


def test_only_counted_checks_are_kept_keyed_by_hash_of_name_and_url() -> None:
    snap = with_checks(
        gh(),
        [
            check("build", "passing", "https://example.org/1"),
            check("lint", "failing", "https://example.org/2", counted=False),
            check("skip", "skipped", None, counted=False),
        ],
    )
    d = build_digest(snap)
    key = hashlib.sha256(b"build\x00https://example.org/1").hexdigest()[:32]
    assert d["ci"]["checks"] == {key: {"name": "build", "state": "passing"}}
    assert check_key("build", "https://example.org/1") == key


def test_check_without_url_hashes_an_empty_url() -> None:
    assert check_key("build", None) == hashlib.sha256(b"build\x00").hexdigest()[:32]


def test_two_checks_with_one_name_do_not_collide() -> None:
    snap = with_checks(
        gh(),
        [
            check("build", "passing", "https://example.org/1"),
            check("build", "failing", "https://example.org/2"),
        ],
    )
    d = build_digest(snap)
    assert sorted(v["state"] for v in d["ci"]["checks"].values()) == ["failing", "passing"]


def test_two_checks_with_one_name_and_no_url_resolve_to_the_worst_state_in_any_order() -> None:
    a = with_checks(gh(), [check("t", "passing"), check("t", "failing"), check("t", "pending")])
    b = with_checks(gh(), [check("t", "pending"), check("t", "failing"), check("t", "passing")])
    assert digest_to_json(build_digest(a)) == digest_to_json(build_digest(b))
    (entry,) = build_digest(a)["ci"]["checks"].values()
    assert entry["state"] == "failing"


def test_long_check_name_with_control_characters_is_truncated_at_a_marker_boundary() -> None:
    # 4000 characters, 300 of them control characters, in a repeating pattern so that truncation
    # at 256 would land inside a marker if markers were cut naively
    raw = "".join("\x01" if i % 13 == 0 else "n" for i in range(4000))
    assert sum(c == "\x01" for c in raw) >= 300
    d = build_digest(with_checks(gh(), [check(raw, "passing", "https://example.org/a")]))
    ((key, entry),) = d["ci"]["checks"].items()
    name = entry["name"]
    assert len(name) <= 256 and name.endswith("...")
    body = name[:-3]
    assert "\x01" not in name
    # no partial marker: removing whole markers leaves no stray angle bracket
    assert "<" not in MARKER.sub("", body) and ">" not in MARKER.sub("", body)
    assert key == check_key(raw, "https://example.org/a")


def test_truncation_never_splits_a_literal_marker_from_forgectl() -> None:
    # forgectl already replaced the control characters: the markers arrive as text
    for pad in range(0, 12):
        raw = "a" * pad + "<U+0001>" * 100
        name = next(iter(build_digest(with_checks(gh(), [check(raw)]))["ci"]["checks"].values()))[
            "name"
        ]
        assert len(name) <= 256 and name.endswith("...")
        body = name[:-3]
        assert "<" not in MARKER.sub("", body) and ">" not in MARKER.sub("", body)


def test_names_that_truncate_to_the_same_text_keep_distinct_keys() -> None:
    long_a = "x" * 600 + "A"
    long_b = "x" * 600 + "B"
    d = build_digest(with_checks(gh(), [check(long_a), check(long_b)]))
    assert len(d["ci"]["checks"]) == 2
    assert len({v["name"] for v in d["ci"]["checks"].values()}) == 1


def test_key_is_taken_from_the_full_sanitized_value_not_the_truncated_one() -> None:
    # forgectl pre-sanitizes text, so a raw control character and the literal marker text for it
    # never both arrive in one snapshot; hashing the sanitized value is therefore collision-free.
    assert check_key("x" * 600 + "A", None) != check_key("x" * 600 + "B", None)
    assert check_key("a\x01b", None) == check_key("a<U+0001>b", None)


def test_check_overflow_is_bounded_deterministic_and_counted() -> None:
    checks = [
        check(f"job-{i}", "passing", f"https://example.org/{i}") for i in range(MAX_CHECKS + 37)
    ]
    a = with_checks(gh(), checks)
    b = with_checks(gh(), list(reversed(checks)))
    da, db = build_digest(a), build_digest(b)
    assert len(da["ci"]["checks"]) == MAX_CHECKS == 500
    assert da["ci"]["checks_more"] == 37
    assert digest_to_json(da) == digest_to_json(db)


def test_unknown_check_state_is_marked_unknown() -> None:
    d = build_digest(with_checks(gh(), [check("a", "wobbling")]))
    assert next(iter(d["ci"]["checks"].values()))["state"] == "unknown"


def test_malformed_checks_are_skipped() -> None:
    snap = with_checks(
        gh(),
        [
            "x",
            {"name": 5, "state": "passing", "counted": True},
            {"name": "", "state": "passing", "counted": 1},
            check("ok"),
        ],
    )  # type: ignore[list-item]
    assert [v["name"] for v in build_digest(snap)["ci"]["checks"].values()] == ["ok"]


# --- hostile text and huge snapshots -------------------------------------------------------


def test_hostile_text_in_stored_fields_is_sanitized_and_bounded() -> None:
    name = "ev‮il\x00\x1b[2J " + "z" * 500
    d = build_digest(with_checks(gh(), [check(name)]))
    stored = next(iter(d["ci"]["checks"].values()))["name"]
    assert len(stored) <= 256
    assert not any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 or c in "‮ " for c in stored)
    assert stored.startswith("ev<U+202E>il<U+0000><U+001B>[2J<U+2028>")


def test_lone_surrogate_in_a_name_is_sanitized() -> None:
    d = build_digest(with_checks(gh(), [check("a\ud800b")]))
    assert next(iter(d["ci"]["checks"].values()))["name"] == "a<U+D800>b"
    digest_to_json(d).encode("ascii")


def test_thousands_of_comments_keep_the_digest_bounded() -> None:
    snap = gh()
    n = 4800
    snap["threads"] = [thread(f"gh:ic:IC{i}", [f"gh:ic:IC{i}"]) for i in range(n)]
    snap["reviews"] = [
        {"id": f"gh:r:R{i}", "author": snap["author"], "state": "commented", "commit_sha": None,
         "submitted_at": "2026-10-02T09:00:00Z", "body": "B" * 5000}
        for i in range(n)
    ]  # fmt: skip
    d = build_digest(snap)
    assert len(d["seen"]["comments"]) == n and len(d["seen"]["reviews"]) == n
    text = digest_to_json(d)
    assert len(text) < 200 * 1024  # ids only, never bodies
    assert "BBBB" not in text


def test_over_cap_seen_set_makes_its_part_incomplete_instead_of_truncating() -> None:
    snap = gh()
    snap["threads"] = [thread(f"gh:ic:IC{i}", [f"gh:ic:IC{i}"]) for i in range(MAX_SEEN + 1)]
    assert "threads" in incomplete_parts(snap)
    previous = build_digest(gh())
    d = build_digest(snap, previous)
    assert d["seen"]["comments"] == previous["seen"]["comments"]
    assert d["baselined"]["threads"] is True  # carried from previous, not claimed by this snapshot


def test_ids_are_deduplicated_validated_and_bounded() -> None:
    snap = gh()
    snap["threads"] = [
        thread("t1", ["gh:ic:A", "gh:ic:A", "gh:ic:B"]),
        thread("t2", ["gh:ic:" + "z" * 500]),
        thread("t3", ["x\ny"]),
        {"id": "t4", "comments": "nope"},
        "junk",
    ]
    assert comment_ids(snap) == ["gh:ic:A", "gh:ic:B"]
    snap["reviews"] = [{"id": "gh:r:1"}, {"id": None}, {"id": "gh:r:1"}, "junk", {"id": 5}]
    assert review_ids(snap) == ["gh:r:1"]


# --- unknown values and nulls (forgectl client note on #8) ---------------------------------


def test_unknown_state_is_not_taken_over_without_previous() -> None:
    assert build_digest(gh(state="archived"))["state"] is None


def test_unknown_state_keeps_the_previous_value() -> None:
    previous = build_digest(gh(state="closed"))
    assert build_digest(gh(state="archived"), previous)["state"] == "closed"


def test_non_boolean_draft_is_not_taken_over() -> None:
    previous = build_digest(gh(draft=True))
    assert build_digest(gh(draft="yes"), previous)["draft"] is True
    assert build_digest(gh(draft="yes"))["draft"] is None


def test_unknown_ci_state_makes_the_ci_part_unreadable_and_carries_previous() -> None:
    previous = build_digest(gh())
    snap = gh(ci={"state": "exploding"})
    assert incomplete_parts(snap) == frozenset({"ci"})
    d = build_digest(snap, previous)
    assert d["ci"] == previous["ci"]
    assert d["baselined"]["ci"] is True


EMPTY_CI = {
    "state": None,
    "head_sha": None,
    "hold": 0,
    "hold_to": None,
    "checks": {},
    "checks_more": 0,
}


def test_unreadable_ci_without_previous_is_the_empty_ci_and_unbaselined() -> None:
    d = build_digest(gh(ci={"state": "exploding"}))
    assert d["ci"] == EMPTY_CI
    assert d["baselined"]["ci"] is False


def test_nullable_fields_are_tolerated() -> None:
    snap = gh()
    snap["head"]["pushed_at"] = None
    snap["head"]["pushed_at_source"] = None
    snap["rate_limit"] = {"host": None, "remaining": None, "reset_at": None}
    snap["latest_by_reviewer"][0].update(commit_sha=None, at=None, can_push=None)
    snap["mergeable"] = None
    d = build_digest(snap)
    assert d["pushed_at"] is None and d["mergeable"] is None
    (entry,) = d["reviewers"].values()
    assert entry == {"state": "approved", "commit_sha": None, "at": None, "can_push": None}


def test_invalid_sha_and_timestamps_become_null() -> None:
    snap = gh(merged_head_sha="nothex", merge_commit_sha=12)
    snap["head"]["pushed_at"] = "yesterday"
    snap["latest_by_reviewer"][0]["at"] = "soon"
    snap["latest_by_reviewer"][0]["commit_sha"] = "short"
    d = build_digest(snap)
    assert d["merged_head_sha"] is None and d["merge_commit_sha"] is None
    assert d["pushed_at"] is None
    (entry,) = d["reviewers"].values()
    assert entry["at"] is None and entry["commit_sha"] is None


def test_unknown_review_decision_keeps_previous_or_null() -> None:
    previous = build_digest(gh())
    assert build_digest(gh(review_decision="brand_new"), previous)["review_decision"] == "approved"
    assert build_digest(gh(review_decision="brand_new"))["review_decision"] is None
    assert build_digest(gh(review_decision=None))["review_decision"] is None


def test_garbage_does_not_raise() -> None:
    for snap in (
        {},
        {
            "head": None,
            "ci": [],
            "threads": {},
            "reviews": 5,
            "latest_by_reviewer": "x",
            "blocked_by": 3,
        },
        {"head": {"sha": 5}, "incomplete": "reviews", "state": ["open"]},
    ):
        d = build_digest(snap)
        digest_to_json(d)


# --- incomplete parts ----------------------------------------------------------------------


def test_complete_snapshot_has_no_incomplete_parts() -> None:
    assert incomplete_parts(gh()) == frozenset()
    assert incomplete_parts(gl()) == frozenset()


def test_incomplete_parts_lists_only_the_parts_pr_watch_uses() -> None:
    snap = gh(complete=False, incomplete=["review_requests", "threads", "bogus", 5])
    assert incomplete_parts(snap) == frozenset({"threads"})


def test_ci_with_an_incomplete_check_list_counts_as_incomplete() -> None:
    snap = gh()
    snap["ci"]["complete"] = False
    snap["ci"]["state"] = "pending"
    assert incomplete_parts(snap) == frozenset({"ci"})


@pytest.mark.parametrize(
    ("part", "stored"),
    [
        ("reviews", ("seen", "reviews")),
        ("threads", ("seen", "comments")),
    ],
)
def test_incomplete_list_part_is_not_overwritten(part: str, stored: tuple[str, str]) -> None:
    previous = build_digest(gh())
    snap = gh(incomplete=[part], complete=False)
    snap["threads"] = [thread("t", ["gh:ic:NEW"])]
    snap["reviews"] = [{"id": "gh:r:NEW"}]
    snap["latest_by_reviewer"] = []
    snap["review_decision"] = "changes_requested"
    snap["approvals_at_head"] = 0
    d = build_digest(snap, previous)
    assert d["baselined"][part] is True
    if part == "reviews":
        for k in ("reviewers", "review_decision", "approvals_at_head"):
            assert d[k] == previous[k]
        assert d["seen"]["reviews"] == previous["seen"]["reviews"]
        assert d["seen"]["comments"] == ["gh:ic:NEW"]
    else:
        assert d["seen"]["comments"] == previous["seen"]["comments"]
        assert d["seen"]["reviews"] == ["gh:r:NEW"]
        assert d["review_decision"] == "changes_requested"


def test_incomplete_ci_is_carried_over_including_hold() -> None:
    previous = build_digest(gh())
    previous["ci"]["hold"] = 1
    previous["ci"]["hold_to"] = "none"
    snap = gh(ci={"state": "failing"}, incomplete=["ci"], complete=False)
    d = build_digest(snap, previous)
    assert d["ci"] == previous["ci"]


def test_incomplete_part_without_previous_is_empty_and_unbaselined() -> None:
    snap = gh(incomplete=["reviews", "threads", "ci"], complete=False)
    d = build_digest(snap)
    assert d["baselined"] == {"reviews": False, "threads": False, "ci": False}
    assert d["seen"] == {"comments": [], "reviews": []}
    assert d["reviewers"] == {} and d["review_decision"] is None and d["approvals_at_head"] is None
    assert d["ci"] == EMPTY_CI
    assert d["head_sha"] == snap["head"]["sha"]  # the PR object is always present


def test_incomplete_part_stays_unbaselined_when_previous_was_unbaselined() -> None:
    previous = build_digest(gh(incomplete=["threads"], complete=False))
    d = build_digest(gh(incomplete=["threads"], complete=False), previous)
    assert d["baselined"]["threads"] is False
    d2 = build_digest(gh(), d)
    assert d2["baselined"]["threads"] is True


def test_pr_object_fields_are_always_refreshed_even_when_parts_are_incomplete() -> None:
    previous = build_digest(gh())
    snap = gh(incomplete=["reviews", "threads", "ci"], complete=False, head={"sha": sha(9)})
    assert build_digest(snap, previous)["head_sha"] == sha(9)


def test_corrupt_previous_is_ignored() -> None:
    snap = gh(incomplete=["ci"], complete=False)
    for previous in ({"ci": "x", "baselined": 4, "seen": 5, "reviewers": []}, {}, None):
        d = build_digest(snap, previous)  # type: ignore[arg-type]
        assert d["ci"] == EMPTY_CI and d["baselined"]["ci"] is False


def test_build_does_not_mutate_its_inputs() -> None:
    snap = gh()
    previous = build_digest(gh())
    before = (copy.deepcopy(snap), copy.deepcopy(previous))
    build_digest(snap, previous)
    assert (snap, previous) == before


# --- review round 1 --------------------------------------------------------------------------


def _entry(**changes: Any) -> dict[str, Any]:
    base = {"login": "u", "id": "1", "state": "approved", "commit_sha": None,
            "at": "2026-10-02T09:00:00Z", "can_push": None}  # fmt: skip
    return {**base, **changes}


def _reviewers_of(entries: list[dict[str, Any]]) -> dict[str, Any]:
    snap = gh()
    snap["latest_by_reviewer"] = entries
    return build_digest(snap)["reviewers"]


def test_duplicate_reviewers_rank_by_instant_not_by_text() -> None:
    early_text_late_instant = _entry(state="approved", at="2026-10-02T09:00:00-05:00")  # 14:00Z
    late_text_early_instant = _entry(state="changes_requested", at="2026-10-02T11:00:00+00:00")
    for order in ([early_text_late_instant, late_text_early_instant],
                  [late_text_early_instant, early_text_late_instant]):  # fmt: skip
        assert _reviewers_of(order)["1"]["state"] == "approved"


def test_duplicate_reviewers_unparseable_time_ranks_lowest() -> None:
    bad = _entry(state="changes_requested", at="2026-13-45T99:00:00Z")
    good = _entry(state="approved", at="2026-10-02T09:00:00Z")
    assert _reviewers_of([bad, good])["1"]["state"] == "approved"
    assert _reviewers_of([good, bad])["1"]["state"] == "approved"


def test_duplicate_reviewers_full_tie_is_independent_of_input_order() -> None:
    a = _entry(state="approved", can_push=True)
    b = _entry(state="changes_requested", can_push=False)
    c = _entry(state="dismissed", can_push=None)
    results = {
        json.dumps(_reviewers_of(list(order)), sort_keys=True)
        for order in ([a, b, c], [c, b, a], [b, a, c])
    }
    assert len(results) == 1
    assert _reviewers_of([a, b, c])["1"]["state"] == "changes_requested"


def test_duplicate_reviewers_same_instant_ties_resolve_by_commit_then_can_push() -> None:
    lo = _entry(commit_sha=sha(1), at="2026-10-02T09:00:00Z")
    hi = _entry(commit_sha=sha(2), at="2026-10-02T10:00:00+01:00")  # the same instant
    assert _reviewers_of([lo, hi])["1"]["commit_sha"] == sha(2)
    assert _reviewers_of([hi, lo])["1"]["commit_sha"] == sha(2)
    no, yes = _entry(can_push=False), _entry(can_push=True)
    assert _reviewers_of([no, yes])["1"]["can_push"] is True
    assert _reviewers_of([yes, no])["1"]["can_push"] is True


def test_empty_ci_when_ci_is_incomplete_and_previous_ci_is_missing_or_malformed() -> None:
    snap = gh(ci={"state": "exploding"})
    assert build_digest(snap, {"ci": None})["ci"] == EMPTY_CI
    assert build_digest(snap, {"ci": {"state": 5}})["ci"] == EMPTY_CI


def test_blocked_by_is_bounded_to_the_smallest_keys() -> None:
    items = [{"code": "other", "detail": f"d{i:04d}"} for i in range(MAX_BLOCKED_BY + 50)]
    a = build_digest(gh(blocked_by=items))
    b = build_digest(gh(blocked_by=list(reversed(items))))
    assert len(a["blocked_by"]) == MAX_BLOCKED_BY == 100
    assert a["blocked_by"][0]["detail"] == "d0000"
    assert digest_to_json(a) == digest_to_json(b)


def test_too_many_reviewers_make_the_reviews_part_incomplete() -> None:
    snap = gh()
    snap["latest_by_reviewer"] = [
        _entry(id=str(i), login=f"u{i}") for i in range(MAX_REVIEWERS + 1)
    ]
    assert "reviews" in incomplete_parts(snap)
    previous = build_digest(gh())
    d = build_digest(snap, previous)
    assert d["reviewers"] == previous["reviewers"]
    snap["latest_by_reviewer"] = snap["latest_by_reviewer"][:MAX_REVIEWERS]
    assert "reviews" not in incomplete_parts(snap)
    assert len(build_digest(snap)["reviewers"]) == MAX_REVIEWERS == 1000


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'])
def test_digest_from_json_rejects_non_finite_numbers(text: str) -> None:
    assert digest_from_json(text) is None


def test_previous_with_non_finite_numbers_is_shape_checked() -> None:
    previous = build_digest(gh())
    previous["ci"]["hold"] = math.nan
    previous["reviewers"] = {"1": {"state": "approved", "at": math.inf}}
    d = build_digest(gh(incomplete=["ci", "reviews"], complete=False), previous)
    assert d["ci"] == EMPTY_CI
    assert d["reviewers"] == {}
    digest_to_json(d)


def test_previous_with_a_huge_ci_state_is_bounded() -> None:
    previous = build_digest(gh())
    previous["ci"]["state"] = "x" * 1_000_000
    d = build_digest(gh(incomplete=["ci"], complete=False), previous)
    assert len(digest_to_json(d)) < 100_000


def test_valid_previous_ci_and_reviewers_are_copied_for_incomplete_parts() -> None:
    previous = build_digest(with_checks(gh(), [check("a", "failing")]))
    d = build_digest(gh(incomplete=["ci", "reviews"], complete=False), previous)
    assert d["ci"] == previous["ci"]
    assert d["reviewers"] == previous["reviewers"]


@pytest.mark.parametrize("snap", [None, [], "x", 5])
def test_non_dict_snapshot_is_an_all_unknown_digest(snap: Any) -> None:
    d = build_digest(snap)
    digest_to_json(d)
    assert d["head_sha"] is None and d["state"] is None
    assert d["baselined"] == {"reviews": False, "threads": False, "ci": False}
    assert incomplete_parts(snap) == frozenset(PARTS)


def test_rebaseline_unbaselines_incomplete_parts_only() -> None:
    previous = build_digest(gh())
    snap = gh(incomplete=["threads"], complete=False)
    assert build_digest(snap, previous)["baselined"]["threads"] is True
    d = build_digest(snap, previous, rebaseline=True)
    assert d["baselined"] == {"reviews": True, "threads": False, "ci": True}
    assert d["seen"]["comments"] == previous["seen"]["comments"]  # old data is kept
    assert build_digest(gh(), d, rebaseline=True)["baselined"]["threads"] is True


def test_approvals_at_head_above_2_pow_53_is_unknown() -> None:
    previous = build_digest(gh(approvals_at_head=2))
    assert build_digest(gh(approvals_at_head=2**53), previous)["approvals_at_head"] == 2**53
    assert build_digest(gh(approvals_at_head=2**53 + 1), previous)["approvals_at_head"] is None


def test_checks_beyond_the_cap_keep_the_smallest_keys_and_only_those_get_display_names() -> None:
    checks = [
        check(f"job-{i}", "passing", f"https://example.org/{i}") for i in range(MAX_CHECKS + 5)
    ]
    d = build_digest(with_checks(gh(), checks))
    expected = sorted(
        check_key(f"job-{i}", f"https://example.org/{i}") for i in range(MAX_CHECKS + 5)
    )
    assert sorted(d["ci"]["checks"]) == expected[:MAX_CHECKS]


_GOOD_REVIEWER = {"state": "approved", "commit_sha": None, "at": None, "can_push": None}


@pytest.mark.parametrize(
    "entry",
    [
        {**_GOOD_REVIEWER, "state": "brand_new"},
        {**_GOOD_REVIEWER, "state": None},
        {**_GOOD_REVIEWER, "state": ["approved"]},
        {**_GOOD_REVIEWER, "commit_sha": 5},
        {**_GOOD_REVIEWER, "at": 5},
        {**_GOOD_REVIEWER, "can_push": "yes"},
        {**_GOOD_REVIEWER, "extra": 1},
        {"state": "approved"},
    ],
)
def test_invalid_previous_reviewer_entry_is_dropped(entry: dict[str, Any]) -> None:
    previous = build_digest(gh())
    key = next(iter(previous["reviewers"]))
    previous["reviewers"] = {key: entry}
    d = build_digest(gh(incomplete=["reviews"], complete=False), previous)
    assert d["reviewers"] == {}
    snap = gh()
    snap["latest_by_reviewer"][0]["state"] = "brand_new"
    assert build_digest(snap, previous)["reviewers"][key]["state"] == "unknown"


def test_previous_reviewer_entry_with_unknown_state_is_valid() -> None:
    previous = build_digest(gh())
    key = next(iter(previous["reviewers"]))
    previous["reviewers"] = {key: {**_GOOD_REVIEWER, "state": "unknown", "can_push": True}}
    d = build_digest(gh(incomplete=["reviews"], complete=False), previous)
    assert d["reviewers"] == previous["reviewers"]


@pytest.mark.parametrize("state", ["exploding", 5, ["passing"]])
def test_previous_ci_with_an_invalid_state_falls_back_to_the_empty_ci(state: Any) -> None:
    previous = build_digest(with_checks(gh(), [check("a", "failing")]))
    previous["ci"]["state"] = state
    assert build_digest(gh(incomplete=["ci"], complete=False), previous)["ci"] == EMPTY_CI


def test_previous_ci_with_a_null_state_is_valid() -> None:
    previous = build_digest(gh())
    previous["ci"]["state"] = None
    d = build_digest(gh(incomplete=["ci"], complete=False), previous)
    assert d["ci"] == previous["ci"]


def test_previous_ci_with_an_invalid_check_state_falls_back_to_the_empty_ci() -> None:
    previous = build_digest(with_checks(gh(), [check("a", "failing")]))
    key = next(iter(previous["ci"]["checks"]))
    previous["ci"]["checks"][key]["state"] = "exploding"
    assert build_digest(gh(incomplete=["ci"], complete=False), previous)["ci"] == EMPTY_CI


def test_previous_ci_with_an_unknown_check_state_is_valid() -> None:
    previous = build_digest(with_checks(gh(), [check("a", "failing")]))
    key = next(iter(previous["ci"]["checks"]))
    previous["ci"]["checks"][key]["state"] = "unknown"
    d = build_digest(gh(incomplete=["ci"], complete=False), previous)
    assert d["ci"] == previous["ci"]


@pytest.mark.parametrize(("length", "kept"), [(40, True), (41, False), (63, False), (64, True)])
def test_sha_is_40_or_64_hex(length: int, kept: bool) -> None:
    d = build_digest(gh(head={"sha": "c" * length}))
    assert d["head_sha"] == ("c" * length if kept else None)
