"""Building a digest from a snapshot; seen sets (docs/interface.md, Digest).

`build_digest(snapshot, previous)` turns one forgectl snapshot into the compact digest the diff
compares: a JSON-ready dict holding ids, hashes and one-line fields only (no body, title, URL,
branch name or other host text). It is deterministic: the same snapshot (apart from `fetched_at`,
`rate_limit` and the fields the digest does not hold) gives a byte-identical `digest_to_json`.

Tolerance (the forgectl client validates shapes, not enum values): a value that is not one the
digest understands (an unknown enum value, a wrong type) never raises. A scalar field then keeps
the `previous` digest's value, or null when there is none; a whole part that cannot be read
(`ci` with an unknown state, a list that is not a list) is treated like a part forgectl lists in
`incomplete`. Parts in `incomplete` are neither compared nor overwritten: the builder copies
`previous`'s fields of the part (empty and unbaselined when there is none) and keeps its
`baselined` flag.

Seams: the digest built here has `ci.hold` 0 and `ci.hold_to` null; the CI hold (which keeps the
previous `ci` and counts snapshots) is applied by the diff. TODO(#15): CI hold over this digest.
TODO(#14): the diff reads new comments and reviews from the snapshot, filtered by `comment_ids`
and `review_ids`, against `seen`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Final

from .text import bounded_one_line, sanitized

MAX_CHECKS: Final = 500
# forgectl lists at most 50 pages (about 5000 items) per list; a set larger than twice that is
# not a real snapshot, and dropping ids would make old comments fire again, so the part is
# treated as incomplete instead.
MAX_SEEN: Final = 10_000
MAX_FIELD_CHARS: Final = 256
PARTS: Final = ("reviews", "threads", "ci")

_BAD: Final = object()
_SHA = re.compile(r"[0-9a-f]{40,64}")
_TS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_ITEM_ID = re.compile(r"[A-Za-z0-9:_=-]{1,128}")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_STATES = ("open", "closed", "merged")
_CI_STATES = ("passing", "failing", "pending", "none")
_CHECK_STATES = ("passing", "failing", "pending", "skipped")
_CHECK_RANK = {"failing": 4, "unknown": 3, "pending": 2, "passing": 1, "skipped": 0}
_REVIEWER_STATES = ("approved", "changes_requested", "dismissed")
_REVIEW_DECISIONS = ("approved", "changes_requested", "review_required", "none")
_BLOCKER_CODES = (
    "draft",
    "conflicts",
    "checks_pending",
    "checks_failing",
    "reviews_required",
    "changes_requested",
    "discussions_unresolved",
    "branch_behind",
    "mergeability_unknown",
)


def _dict(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# --- scalar validators: return the value or _BAD ---------------------------------------------


def _sha(value: object) -> object:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else _BAD


def _ts(value: object) -> object:
    return value if isinstance(value, str) and _TS.fullmatch(value) else _BAD


def _enum(*allowed: str) -> Any:
    return lambda value: value if isinstance(value, str) and value in allowed else _BAD


def _bool(value: object) -> object:
    return value if isinstance(value, bool) else _BAD


def _count(value: object) -> object:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else _BAD


def _nullable(check: Any) -> Any:
    return lambda value: None if value is None else check(value)


def _scalar(new: object, check: Any, previous: Mapping[str, Any], key: str) -> Any:
    """`new` when valid; else the previous digest's value when valid; else null."""
    value = check(new)
    if value is not _BAD:
        return value
    old = check(previous.get(key))
    return None if old is _BAD else old


# --- identifiers -----------------------------------------------------------------------------


def _valid_ids(items: object, per_item: Any) -> list[str]:
    seen: set[str] = set()
    for item in items if isinstance(items, list) else ():
        for value in per_item(item):
            if isinstance(value, str) and _ITEM_ID.fullmatch(value):
                seen.add(value)
    return sorted(seen)


def comment_ids(snapshot: Mapping[str, Any]) -> list[str]:
    """Sorted, de-duplicated valid ids of every comment in `threads` (all kinds)."""

    def ids(thread: object) -> list[object]:
        comments = _dict(thread).get("comments")
        return [_dict(c).get("id") for c in comments] if isinstance(comments, list) else []

    return _valid_ids(snapshot.get("threads"), ids)


def review_ids(snapshot: Mapping[str, Any]) -> list[str]:
    """Sorted, de-duplicated valid ids of the snapshot's reviews (GitHub only)."""
    return _valid_ids(snapshot.get("reviews"), lambda review: [_dict(review).get("id")])


def check_key(name: str, url: str | None) -> str:
    """First 32 hex characters of sha256 of the full sanitized name, a NUL and the url."""
    material = sanitized(name) + "\0" + (sanitized(url) if isinstance(url, str) else "")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def reviewer_key(entry: object) -> str | None:
    """`id` when forgectl gives one, else `login:` plus the lowercased login; null when neither.
    A login too long to keep (256 characters) is replaced by a hash of the full value."""
    item = _dict(entry)
    ident = item.get("id")
    if isinstance(ident, str) and _ACCOUNT_ID.fullmatch(ident):
        return ident
    login = item.get("login")
    if not isinstance(login, str) or not login:
        return None
    full = sanitized(login).lower()
    key = f"login:{full}"
    if len(key) <= MAX_FIELD_CHARS:
        return key
    return "login:#" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:32]


# --- parts -----------------------------------------------------------------------------------


def _ci_readable(ci: object) -> bool:
    data = _dict(ci)
    return (
        _enum(*_CI_STATES)(data.get("state")) is not _BAD
        and _sha(data.get("head_sha")) is not _BAD
        and isinstance(data.get("checks"), list)
        and data.get("complete") is not False
    )


def incomplete_parts(snapshot: Mapping[str, Any]) -> frozenset[str]:
    """The parts (`reviews`, `threads`, `ci`) this snapshot cannot be compared or stored for:
    the ones forgectl lists in `incomplete` (`review_requests` and unknown names are ignored), and
    the ones whose data cannot be read or is over the cap (see the module docstring)."""
    listed = snapshot.get("incomplete")
    out = {p for p in (listed if isinstance(listed, list) else ()) if p in PARTS}
    if not isinstance(snapshot.get("reviews"), list) or not isinstance(
        snapshot.get("latest_by_reviewer"), list
    ):
        out.add("reviews")
    elif len(review_ids(snapshot)) > MAX_SEEN:
        out.add("reviews")
    if not isinstance(snapshot.get("threads"), list) or len(comment_ids(snapshot)) > MAX_SEEN:
        out.add("threads")
    if not _ci_readable(snapshot.get("ci")):
        out.add("ci")
    return frozenset(out)


def _blocked_by(value: object) -> object:
    if not isinstance(value, list):
        return _BAD
    out: set[tuple[str, str]] = set()
    for item in value:
        code = _dict(item).get("code")
        if not isinstance(code, str):
            continue
        if code in _BLOCKER_CODES:
            out.add((code, ""))
            continue
        detail = _dict(item).get("detail") if code == "other" else code
        if not isinstance(detail, str) or not detail:
            detail = "unknown"
        out.add(("other", bounded_one_line(detail, MAX_FIELD_CHARS)))
    return [{"code": c} if not d else {"code": c, "detail": d} for c, d in sorted(out)]


def _checks(ci: Mapping[str, Any]) -> tuple[dict[str, dict[str, str]], int]:
    found: dict[str, dict[str, str]] = {}
    for item in ci["checks"]:
        data = _dict(item)
        name = data.get("name")
        if data.get("counted") is not True or not isinstance(name, str):
            continue
        state = data.get("state")
        state = state if isinstance(state, str) and state in _CHECK_STATES else "unknown"
        key = check_key(name, data.get("url"))
        entry = {"name": bounded_one_line(name, MAX_FIELD_CHARS), "state": state}
        if key not in found or _CHECK_RANK[state] > _CHECK_RANK[found[key]["state"]]:
            found[key] = entry
    keys = sorted(found)
    return {k: found[k] for k in keys[:MAX_CHECKS]}, max(0, len(keys) - MAX_CHECKS)


def _ci(ci: Mapping[str, Any]) -> dict[str, Any]:
    checks, more = _checks(ci)
    return {
        "state": ci["state"],
        "head_sha": ci["head_sha"],
        "hold": 0,
        "hold_to": None,
        "checks": checks,
        "checks_more": more,
    }


def _reviewers(snapshot: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    old = _dict(previous.get("reviewers"))
    best: dict[str, dict[str, Any]] = {}
    for item in snapshot["latest_by_reviewer"]:
        key = reviewer_key(item)
        if key is None:
            continue
        data = _dict(item)
        commit_sha = _nullable(_sha)(data.get("commit_sha"))
        at = _nullable(_ts)(data.get("at"))
        can_push = data.get("can_push")
        entry = {
            "state": data.get("state"),
            "commit_sha": None if commit_sha is _BAD else commit_sha,
            "at": None if at is _BAD else at,
            "can_push": can_push if isinstance(can_push, bool) else None,
        }
        rank = (entry["at"] or "", entry["commit_sha"] or "", str(entry["state"]))
        if key not in best or rank > best[key]["rank"]:
            best[key] = {"rank": rank, "entry": entry}
    out: dict[str, Any] = {}
    for key, found in best.items():
        entry = found["entry"]
        if entry["state"] not in _REVIEWER_STATES:
            kept = old.get(key)
            if isinstance(kept, Mapping):
                out[key] = dict(kept)
                continue
            entry["state"] = "unknown"
        out[key] = entry
    return out


def _previous_list(previous: Mapping[str, Any], key: str) -> list[str]:
    value = _dict(previous.get("seen")).get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def build_digest(
    snapshot: Mapping[str, Any], previous: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The digest of `snapshot`; `previous` is the stored digest (parts that cannot be taken
    over from this snapshot keep its values)."""
    old = previous if isinstance(previous, Mapping) else {}
    incomplete = incomplete_parts(snapshot)
    head = _dict(snapshot.get("head"))
    old_baselined = _dict(old.get("baselined"))

    blocked = _blocked_by(snapshot.get("blocked_by"))
    if blocked is _BAD:
        blocked = old.get("blocked_by") if isinstance(old.get("blocked_by"), list) else []

    digest: dict[str, Any] = {
        "head_sha": _scalar(head.get("sha"), _sha, old, "head_sha"),
        "state": _scalar(snapshot.get("state"), _enum(*_STATES), old, "state"),
        "draft": _scalar(snapshot.get("draft"), _bool, old, "draft"),
        "mergeable": _scalar(snapshot.get("mergeable"), _nullable(_bool), old, "mergeable"),
        "blocked_by": blocked,
        "pushed_at": _scalar(head.get("pushed_at"), _nullable(_ts), old, "pushed_at"),
        "merged_head_sha": _scalar(
            snapshot.get("merged_head_sha"), _nullable(_sha), old, "merged_head_sha"
        ),
        "merge_commit_sha": _scalar(
            snapshot.get("merge_commit_sha"), _nullable(_sha), old, "merge_commit_sha"
        ),
    }

    if "reviews" in incomplete:
        digest["reviewers"] = dict(_dict(old.get("reviewers")))
        digest["review_decision"] = _scalar(
            old.get("review_decision"), _nullable(_enum(*_REVIEW_DECISIONS)), {}, ""
        )
        digest["approvals_at_head"] = _scalar(
            old.get("approvals_at_head"), _nullable(_count), {}, ""
        )
        seen_reviews = _previous_list(old, "reviews")
    else:
        digest["reviewers"] = _reviewers(snapshot, old)
        digest["review_decision"] = _scalar(
            snapshot.get("review_decision"),
            _nullable(_enum(*_REVIEW_DECISIONS)),
            old,
            "review_decision",
        )
        # null is unknown, never zero: anything that is not a count is unknown
        approvals = _nullable(_count)(snapshot.get("approvals_at_head"))
        digest["approvals_at_head"] = None if approvals is _BAD else approvals
        seen_reviews = review_ids(snapshot)
    seen_comments = (
        _previous_list(old, "comments") if "threads" in incomplete else comment_ids(snapshot)
    )
    digest["seen"] = {"comments": seen_comments, "reviews": seen_reviews}

    if "ci" in incomplete:
        kept = old.get("ci")
        digest["ci"] = dict(kept) if isinstance(kept, Mapping) else None
    else:
        digest["ci"] = _ci(_dict(snapshot.get("ci")))

    digest["baselined"] = {
        part: old_baselined.get(part) is True if part in incomplete else True for part in PARTS
    }
    return digest


def digest_to_json(digest: Mapping[str, Any]) -> str:
    """Canonical text of a digest: sorted keys, compact separators, ASCII only."""
    return json.dumps(
        digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest_from_json(text: str | None) -> dict[str, Any] | None:
    """A stored digest, or null when there is none or the text is not a JSON object."""
    if not text:
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None
