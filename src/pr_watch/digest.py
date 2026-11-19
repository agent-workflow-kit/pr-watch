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
`previous`'s fields of the part (empty and unbaselined when there is none; `ci` is then the empty
ci, never null) and keeps its `baselined` flag (or clears it, when `build_digest` is called with
`rebaseline`). A copied `ci` or `reviewers` is shape-checked and bounded first; one that does not
pass is replaced by the empty shape (`ci` all null or empty, `{}`), so a corrupt stored digest
never reaches the output.

Seams: the digest built here has `ci.hold` 0 and `ci.hold_to` null; the CI hold (which keeps the
previous `ci` and counts snapshots) is applied by the diff; `diff(...).digest` is the digest
to store.
TODO(#14): the diff reads new comments and reviews from the snapshot, filtered by `comment_ids`
and `review_ids`, against `seen`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from .text import bounded_one_line, sanitized

MAX_CHECKS: Final = 500
MAX_BLOCKED_BY: Final = 100
# more distinct reviewers than this is not a real snapshot: the reviews part is incomplete
MAX_REVIEWERS: Final = 1000
MAX_INT: Final = 2**53
# forgectl lists at most 50 pages (about 5000 items) per list; a set larger than twice that is
# not a real snapshot, and dropping ids would make old comments fire again, so the part is
# treated as incomplete instead.
MAX_SEEN: Final = 10_000
MAX_FIELD_CHARS: Final = 256
PARTS: Final = ("reviews", "threads", "ci")

_BAD: Final = object()
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
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
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_INT
    return value if ok else _BAD


_TS_PARTS = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?"
    r"(?:[Zz]|([+-])([0-9]{2}):([0-9]{2}))"
)


def _instant(value: object) -> datetime | None:
    """A valid timestamp as an aware UTC datetime, else null (never raises)."""
    match = _TS_PARTS.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    year, month, day, hour, minute, second, frac, sign, oh, om = match.groups()
    try:
        moment = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second),
            int((frac or "0")[:6].ljust(6, "0")), tzinfo=UTC,
        )  # fmt: skip
        if sign:
            offset = (int(oh) * 60 + int(om)) * (1 if sign == "+" else -1)
            moment = moment - timedelta(minutes=offset)
    except (ValueError, OverflowError):
        return None
    return moment


def _clean_json(value: object, depth: int = 0) -> Any:
    """A copy of a stored JSON value with strings bounded; raises ValueError for a value a digest
    never holds (non-finite or huge numbers, non-string keys, too deep, too long a key)."""
    if depth > 6:
        raise ValueError("too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_INT:
            raise ValueError("integer out of range")
        return value
    if isinstance(value, float):
        raise ValueError("float")
    if isinstance(value, str):
        return bounded_one_line(value, MAX_FIELD_CHARS)
    if isinstance(value, list):
        return [_clean_json(v, depth + 1) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str) or len(k) > MAX_FIELD_CHARS:
                raise ValueError("key")
            out[k] = _clean_json(v, depth + 1)
        return out
    raise ValueError("type")


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


def instant(value: object) -> datetime | None:
    """A valid timestamp as an aware UTC datetime, else null (never raises)."""
    return _instant(value)


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
    """First 32 hex characters of sha256 of the full sanitized name, a NUL and the url.
    forgectl pre-sanitizes its text, so a raw control character and the literal marker text for
    it never both arrive in one snapshot; hashing the sanitized value is collision-free there."""
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


def _reviewer_count(snapshot: Mapping[str, Any]) -> int:
    return len({reviewer_key(item) for item in snapshot["latest_by_reviewer"]} - {None})


def incomplete_parts(snapshot: Mapping[str, Any]) -> frozenset[str]:
    """The parts (`reviews`, `threads`, `ci`) this snapshot cannot be compared or stored for:
    the ones forgectl lists in `incomplete` (`review_requests` and unknown names are ignored), and
    the ones whose data cannot be read or is over the cap (see the module docstring)."""
    snapshot = _dict(snapshot)
    listed = snapshot.get("incomplete")
    out = {p for p in (listed if isinstance(listed, list) else ()) if p in PARTS}
    if not isinstance(snapshot.get("reviews"), list) or not isinstance(
        snapshot.get("latest_by_reviewer"), list
    ):
        out.add("reviews")
    elif len(review_ids(snapshot)) > MAX_SEEN or _reviewer_count(snapshot) > MAX_REVIEWERS:
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
    kept = sorted(out)[:MAX_BLOCKED_BY]
    return [{"code": c} if not d else {"code": c, "detail": d} for c, d in kept]


def counted_checks(ci: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Key -> (full name, state) of the counted checks of a readable `ci`, uncapped. Checks that
    share a key merge, the worst state winning, whatever the order."""
    found: dict[str, tuple[str, str]] = {}
    for item in ci["checks"]:
        data = _dict(item)
        name = data.get("name")
        if data.get("counted") is not True or not isinstance(name, str):
            continue
        state = data.get("state")
        state = state if isinstance(state, str) and state in _CHECK_STATES else "unknown"
        # the key is taken from the full sanitized value; only the kept entries get a display name
        key = check_key(name, data.get("url"))
        if key not in found or _CHECK_RANK[state] > _CHECK_RANK[found[key][1]]:
            found[key] = (name, state)
    return found


def _checks(ci: Mapping[str, Any]) -> tuple[dict[str, dict[str, str]], int]:
    found = counted_checks(ci)
    keys = sorted(found)
    kept = {
        k: {"name": bounded_one_line(found[k][0], MAX_FIELD_CHARS), "state": found[k][1]}
        for k in keys[:MAX_CHECKS]
    }
    return kept, max(0, len(keys) - MAX_CHECKS)


def _empty_ci() -> dict[str, Any]:
    return {
        "state": None,
        "head_sha": None,
        "hold": 0,
        "hold_to": None,
        "checks": {},
        "checks_more": 0,
    }


def _previous_ci(previous: Mapping[str, Any]) -> dict[str, Any]:
    """The previous digest's `ci` (bounded copy) if it has the expected shape, else the empty ci."""
    try:
        kept = _clean_json(previous.get("ci"))
    except ValueError:
        return _empty_ci()
    if not isinstance(kept, dict) or set(kept) != set(_empty_ci()):
        return _empty_ci()
    checks = kept["checks"]
    well_formed = (
        all(isinstance(kept[k], str) or kept[k] is None for k in ("head_sha", "hold_to"))
        and (kept["state"] is None or kept["state"] in _CI_STATES)
        and all(_count(kept[k]) is not _BAD for k in ("hold", "checks_more"))
        and isinstance(checks, dict)
        and all(
            isinstance(v, dict)
            and set(v) == {"name", "state"}
            and isinstance(v["name"], str)
            and v["state"] in (*_CHECK_STATES, "unknown")
            for v in checks.values()
        )
    )
    if not well_formed:
        return _empty_ci()
    kept["checks"] = {k: checks[k] for k in sorted(checks)[:MAX_CHECKS]}
    return kept


def _previous_reviewers(previous: Mapping[str, Any]) -> dict[str, Any]:
    try:
        kept = _clean_json(previous.get("reviewers"))
    except ValueError:
        return {}
    if not isinstance(kept, dict) or len(kept) > MAX_REVIEWERS:
        return {}
    return {k: v for k, v in kept.items() if _valid_reviewer_entry(v)}


def _valid_reviewer_entry(entry: object) -> bool:
    """A previous reviewer entry as `_reviewers` writes it: exactly its four keys, known values."""
    return (
        isinstance(entry, dict)
        and set(entry) == {"state", "commit_sha", "at", "can_push"}
        and entry["state"] in (*_REVIEWER_STATES, "unknown")
        and all(entry[k] is None or isinstance(entry[k], str) for k in ("commit_sha", "at"))
        and (entry["can_push"] is None or isinstance(entry["can_push"], bool))
    )


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


_MIN = datetime.min.replace(tzinfo=UTC)
_STATE_RANK = {"changes_requested": 3, "approved": 2, "dismissed": 1}


def _rank(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """Order of duplicate entries of one reviewer key; the highest wins. The instant of `at`
    (an unparseable one is lowest), then `commit_sha`, the state, `can_push`, and the entry's
    canonical text as the last resort, so the result never depends on the input order."""
    moment = _instant(entry["at"])
    can_push = entry["can_push"]
    return (
        moment is not None,
        moment or _MIN,
        entry["commit_sha"] or "",
        _STATE_RANK.get(entry["state"], 0),
        -1 if can_push is None else int(can_push),
        json.dumps(entry, sort_keys=True),
    )


def _reviewers(snapshot: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    old = _previous_reviewers(previous)
    best: dict[str, dict[str, Any]] = {}
    for item in snapshot["latest_by_reviewer"]:
        key = reviewer_key(item)
        if key is None:
            continue
        data = _dict(item)
        commit_sha = _nullable(_sha)(data.get("commit_sha"))
        at = _nullable(_ts)(data.get("at"))
        can_push = data.get("can_push")
        state = data.get("state")
        entry = {
            "state": state if isinstance(state, str) and state in _REVIEWER_STATES else None,
            "commit_sha": None if commit_sha is _BAD else commit_sha,
            "at": None if at is _BAD else at,
            "can_push": can_push if isinstance(can_push, bool) else None,
        }
        if key not in best or _rank(entry) > _rank(best[key]):
            best[key] = entry
    out: dict[str, Any] = {}
    for key, entry in best.items():
        if entry["state"] is None:
            kept = old.get(key)
            if kept is not None:
                out[key] = kept
                continue
            entry["state"] = "unknown"
        out[key] = entry
    return out


def _previous_list(previous: Mapping[str, Any], key: str) -> list[str]:
    value = _dict(previous.get("seen")).get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def build_digest(
    snapshot: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    *,
    rebaseline: bool = False,
) -> dict[str, Any]:
    """The digest of `snapshot` (a non-mapping counts as an empty snapshot); `previous` is the
    stored digest (parts that cannot be taken over from this snapshot keep its values). With
    `rebaseline`, every part that ends up incomplete is marked unbaselined (its old data is kept),
    so a part that completes later is baselined silently instead of diffed against stale data."""
    snapshot = _dict(snapshot)
    old = previous if isinstance(previous, Mapping) else {}
    incomplete = incomplete_parts(snapshot)
    head = _dict(snapshot.get("head"))
    old_baselined = _dict(old.get("baselined"))

    blocked = _blocked_by(snapshot.get("blocked_by"))
    if blocked is _BAD:
        blocked = _blocked_by(old.get("blocked_by"))
        if blocked is _BAD:
            blocked = []

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
        digest["reviewers"] = _previous_reviewers(old)
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
        digest["ci"] = _previous_ci(old)
    else:
        digest["ci"] = _ci(_dict(snapshot.get("ci")))

    digest["baselined"] = {
        part: (old_baselined.get(part) is True and not rebaseline) if part in incomplete else True
        for part in PARTS
    }
    return digest


def digest_to_json(digest: Mapping[str, Any]) -> str:
    """Canonical text of a digest: sorted keys, compact separators, ASCII only."""
    return json.dumps(
        digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _reject_constant(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


def digest_from_json(text: str | None) -> dict[str, Any] | None:
    """A stored digest, or null when there is none or the text is not a JSON object (NaN and
    Infinity count as corrupt)."""
    if not text:
        return None
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None
