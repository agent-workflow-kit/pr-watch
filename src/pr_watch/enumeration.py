"""Poll cycle steps 1 to 3: the host list, the listings and the membership rules
(docs/interface.md: Poll cycle, Watches, owners and members, State transaction 1, Failures).

`enumerate_scopes` is the one entry point of the pipeline stage. It reads the store, calls
`whoami` (cached `poll.hosts_ttl`), lists the open PRs of the account on every host in scope that a
query watch applies to, lists every repo that holds a per-PR watch (windowed by the persisted
watermark), and applies each listing in its own transaction. It returns an `EnumerationResult`:
the facts the later stages read (what was listed, what is a new member, what was reactivated, per
host state and every failure) and nothing else. The persisted columns (`listed_updated_at`,
`left_count`, `rebaseline`, the marks of an incomplete listing) are the real triggers of snapshot
selection (#22); the result only says what this cycle touched.

Rules owned here:

- A listing changes the store only through `apply_*` in one `Store.transaction()` per listing. A
  failed listing writes nothing. A failure never changes a PR (no counter, no status, no event);
  `closed` and `merged` are never inferred from a PR missing from a list.
- A query watch is read before the call and verified again inside the transaction: a watch removed
  meanwhile gets no source, no `left_count` change. The row of a new member is inserted before its
  source, and a source is only ever inserted for a watch that still exists.
- `left_count` rises by one for a member of the host absent from a **complete** open listing (a
  complete listing with no malformed item) and is reset to 0 by a listed open item that matches the
  watch. It never exceeds 2; a source is removed only by the snapshot commit (`commit.py`).
- An incomplete listing (`complete: false`, a partial result with `errors[]`, or any malformed
  item) is used for the PRs it lists and never to conclude that a member left. An incomplete repo
  listing marks every `active` per-PR row of that repo as changed, so trigger (b) snapshots it.
- An item is accepted only when its `pr` and `repo` are well-formed and agree, its host is the host
  asked, `pr_id` matches the store's `PR_ID_RE` and `updated_at` is a timestamp with a zone, in
  1970 or later; anything else is ignored and makes the listing incomplete.

Judgment calls where the interface is silent (kept conservative):

- Only a query listing sets `hosts.last_enumerated_at` (the value `list` prints and
  `enumerated_at` derives from); a per-PR repo listing does not.
- A partial `whoami` result (exit 12 with `hosts`) is used for this cycle and not cached, so the
  failing host is asked again next cycle; its `errors[]` entries are failed hosts.
- A `whoami` failure that names no host (the call as a whole) is a `Failure` with `ref` None; the
  report stage chooses how to show it. With a stale cache the cache is reused, the failure is
  `EnumerationResult.stale_whoami` (not in `failures`) and a warning is logged.
- A `--repo` poll also lists its host's open PRs (host list and query listing for that host only).
  That listing writes `listed_updated_at` for every listed row, and joins members, resets
  `left_count` and reactivates only for items of the restricted repo; it counts no absence and
  does not set `last_enumerated_at`.
- Items of a query listing are matched to a watch by the watch's filters only: a member that is
  still listed but no longer matches the `--repo` filter counts as absent for that watch.
- An empty first listing of a repo with no `scopes` row stores the start it used, without the
  overlap, as the watermark, so the scope exists from then on.

TODO(#22): choose snapshots from the persisted triggers; TODO(#23): pause and low water from
`HostFacts`; TODO(#24): over-cap fallback; TODO(#25): the report, `meta`, housekeeping.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from . import failures as table
from .config import Config
from .forgectl_client import ForgectlClient, Outcome
from .log import Log
from .refs import RefError, duration_ms, parse_host
from .store import PR_ID_RE, PR_RE, REPO_RE, Store, format_ts
from .watches import QUERY_KIND, watch_exists

LISTING_LIMIT: Final = 1000
MAX_REF_CHARS: Final = 256
MAX_LEFT: Final = 2
MAX_STALE_WARNINGS: Final = 20
CACHE_KEY: Final = "hosts_cache"
_MICRO: Final = timedelta(microseconds=1)
_BACKENDS: Final = frozenset({"github", "gitlab"})
_MIN_YEAR: Final = 1970  # an earlier updated_at is not a real PR time (and would store unpadded)


# --- results ---


@dataclass(frozen=True)
class Failure:
    """One entry of the report's `failed` (`ref` is None only for a `whoami` call that names no
    host). `poll_exit` is the table's exit code for the effect (12 or 0)."""

    kind: str  # host, repo or query
    ref: str | None
    code: str
    poll_exit: int = 0


@dataclass(frozen=True)
class HostError:
    """One entry of the report's `hosts.<h>.errors` (a listing failed this cycle)."""

    code: str
    retry_after: float | None = None


@dataclass
class HostFacts:
    """What this cycle learned about one host from its listings (#23 reads pause and rate)."""

    host: str
    queried: bool = False  # a query listing was made
    skipped: str | None = None  # no_credential or not_in_scope
    incomplete_listing: bool = False
    rate_limited: bool = False  # a listing hit rate_limited: pause the host
    retry_after: float | None = None
    cancelled: bool = False  # the rest of the host's listings were cancelled
    errors: list[HostError] = field(default_factory=list)
    lowest_remaining: int | None = None  # lowest numeric rate_limit.remaining of its listings
    reset_at: str | None = None  # the reset_at of that reading
    listings: int = 0  # successful or partial listing calls


@dataclass(frozen=True)
class Listed:
    """A listed item reduced to what the later stages use."""

    pr_id: str
    pr: str
    repo: str
    host: str
    updated_at: str  # UTC, schemas/defs.json `ts`
    state: str


@dataclass(frozen=True)
class HostEntry:
    host: str
    backend: str | None
    in_scope: bool


@dataclass(frozen=True)
class HostList:
    hosts: tuple[HostEntry, ...]
    failed: frozenset[str]  # hosts a partial `whoami` could not resolve this cycle
    available: bool  # False: no list at all (no cache and `whoami` failed)
    stale: bool = False  # the cache was reused after a failed call


@dataclass
class EnumerationResult:
    host_list: HostList
    hosts: dict[str, HostFacts] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stale_whoami: Failure | None = None
    whoami_retry_after: float | None = None  # whoami was rate limited
    listed: dict[str, Listed] = field(default_factory=dict)  # newest listing per pr_id
    new_members: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    candidates: set[str] = field(default_factory=set)  # listed PRs that have a stored row
    scopes_listed: list[str] = field(default_factory=list)  # repos listed successfully
    interrupted: bool = False

    def facts(self, host: str) -> HostFacts:
        found = self.hosts.get(host)
        if found is None:
            found = self.hosts[host] = HostFacts(host)
        return found


# --- helpers ---


def parse_stamp(text: object) -> datetime | None:
    """An aware timestamp (any offset) as UTC, else None."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    try:
        moment = datetime.fromisoformat(text.upper())  # forgectl's `ts` allows t and z
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    try:
        return moment.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _later(old: str | None, new: str) -> str:
    """The larger of two stored timestamps (an unreadable old value loses)."""
    before = parse_stamp(old)
    after = parse_stamp(new)
    if before is None or after is None:
        return new
    return format_ts(max(before, after))


def query_filters(key: str) -> tuple[frozenset[str], frozenset[str]]:
    """`(hosts, repos)` of a query watch key (`mine+open;host=a,b;repo=c`), lowercase."""
    hosts: set[str] = set()
    repos: set[str] = set()
    for part in key.split(";")[1:]:
        name, _, value = part.partition("=")
        names = [v for v in value.split(",") if v]
        if name == "host":
            hosts.update(names)
        elif name == "repo":
            repos.update(names)
    return frozenset(hosts), frozenset(repos)


@dataclass(frozen=True)
class QueryWatch:
    id: str
    hosts: frozenset[str]
    repos: frozenset[str]

    def covers(self, host: str) -> bool:
        """Whether the query applies to a host (a repo implies its host)."""
        if self.hosts and host not in self.hosts:
            return False
        if self.repos and host not in {r.partition("/")[0] for r in self.repos}:
            return False
        return True

    def matches(self, item: Listed) -> bool:
        return not self.repos or item.repo.lower() in self.repos


def read_query_watches(conn: sqlite3.Connection) -> list[QueryWatch]:
    rows = conn.execute(
        "SELECT id, key FROM watches WHERE kind = ? ORDER BY created_at, id", (QUERY_KIND,)
    ).fetchall()
    return [QueryWatch(str(r[0]), *query_filters(str(r[1]))) for r in rows]


def _item(raw: object, host: str) -> Listed | None:
    """A listed item that is safe to store, or None."""
    if not isinstance(raw, dict):
        return None
    pr, repo, pr_id = raw.get("pr"), raw.get("repo"), raw.get("pr_id")
    state, updated = raw.get("state"), parse_stamp(raw.get("updated_at"))
    if not (isinstance(pr, str) and isinstance(repo, str) and isinstance(pr_id, str)):
        return None
    if not isinstance(state, str) or updated is None:
        return None
    if len(pr) > MAX_REF_CHARS or len(repo) > MAX_REF_CHARS:
        return None
    if not PR_RE.match(pr) or not REPO_RE.match(repo):
        return None
    if pr.rpartition("#")[0].lower() != repo.lower() or pr.partition("/")[0] != host:
        return None
    if not PR_ID_RE.match(pr_id) or updated.year < _MIN_YEAR:
        return None
    return Listed(pr_id, pr, repo, host, format_ts(updated), state)


def _items(outcome_result: dict[str, Any], host: str) -> tuple[list[Listed], bool]:
    """The accepted items and whether every item was accepted."""
    raw = outcome_result.get("items")
    if not isinstance(raw, list):
        return [], False
    items: list[Listed] = []
    clean = True
    for entry in raw:
        item = _item(entry, host)
        if item is None:
            clean = False
        else:
            items.append(item)
    return items, clean


def _is_complete(result: dict[str, Any], clean: bool) -> bool:
    errors = result.get("errors")
    return bool(
        result.get("complete") is True and clean and isinstance(errors, list) and not errors
    )


# --- the host list (step 1) ---


def _read_cache(conn: sqlite3.Connection) -> tuple[datetime, tuple[HostEntry, ...]] | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (CACHE_KEY,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        doc = json.loads(str(row[0]))
        fetched = parse_stamp(doc["fetched_at"])
        entries = tuple(
            HostEntry(str(h["host"]), h.get("backend"), h["in_scope"] is True)
            for h in doc["hosts"]
            if _valid_host(h["host"])
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if fetched is None:
        return None
    return fetched, entries


def _valid_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    try:
        return parse_host(host) == host
    except RefError:
        return False


def _write_cache(store: Store, now: datetime, hosts: Iterable[HostEntry]) -> None:
    doc = {
        "fetched_at": format_ts(now),
        "hosts": [{"host": h.host, "backend": h.backend, "in_scope": h.in_scope} for h in hosts],
    }
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CACHE_KEY, json.dumps(doc, separators=(",", ":"))),
        )


def _entries(result: dict[str, Any]) -> tuple[HostEntry, ...]:
    entries: dict[str, HostEntry] = {}
    for raw in result.get("hosts") or []:
        if not isinstance(raw, dict) or not _valid_host(raw.get("host")):
            continue
        backend = raw.get("backend")
        token = raw.get("token_source")
        entries[raw["host"]] = HostEntry(
            raw["host"],
            backend if backend in _BACKENDS else None,
            isinstance(token, str) and token != "none",
        )
    return tuple(sorted(entries.values(), key=lambda e: e.host))


def load_host_list(
    store: Store,
    client: ForgectlClient,
    *,
    now: datetime,
    ttl_ms: int,
    result: EnumerationResult | None = None,
    log: Log | None = None,
    restrict_host: str | None = None,
) -> tuple[HostList, EnumerationResult]:
    """Step 1: the host list from the cache (younger than `ttl_ms`) or `whoami`.

    A failed call reuses the stale cache with a warning; with no cache the list is unavailable
    and the query watches are skipped this cycle. With `restrict_host` (a `--repo` poll) a partial
    result reports only that host's errors."""
    res = result if result is not None else EnumerationResult(HostList((), frozenset(), False))
    cached = _read_cache(store.conn)
    if cached is not None:
        age = (now - cached[0]).total_seconds() * 1000
        if 0 <= age < ttl_ms:
            res.host_list = HostList(cached[1], frozenset(), True)
            return res.host_list, res
    outcome = client.whoami()
    decision = table.decide(outcome, "whoami")
    effect = decision.effect
    if effect.commit or effect.use_result:
        host_list = _from_whoami(store, outcome, decision, now, res, restrict_host)
        res.host_list = host_list
        return host_list, res
    code = decision.code or "internal"
    failure = Failure("host", None, code, effect.poll_exit)
    if effect.pause != "none":
        res.whoami_retry_after = decision.retry_after
    if cached is not None:
        res.warnings.append(f"whoami failed ({code}); the cached host list is reused")
        res.stale_whoami = failure
        for entry in cached[1][:MAX_STALE_WARNINGS]:
            if log is not None:
                log.write("host_failed", "warn", host=entry.host, code=code,
                          message="whoami failed; the cached host list is reused")  # fmt: skip
        res.host_list = HostList(cached[1], frozenset(), True, stale=True)
        return res.host_list, res
    res.warnings.append(f"whoami failed ({code}) and there is no cached host list")
    res.failures.append(failure)
    res.host_list = HostList((), frozenset(), False)
    return res.host_list, res


def _from_whoami(
    store: Store,
    outcome: Outcome,
    decision: table.Decision,
    now: datetime,
    res: EnumerationResult,
    restrict_host: str | None = None,
) -> HostList:
    assert outcome.result is not None
    entries = _entries(outcome.result)
    failed: set[str] = set()
    if decision.effect.use_result:
        for err in outcome.result.get("errors") or []:
            if not isinstance(err, dict) or not isinstance(err.get("code"), str):
                continue
            reason = err.get("reason")
            one = table.decide_code(
                err["code"], reason if isinstance(reason, str) else None, "whoami"
            )
            host = err.get("host")
            ref = host if _valid_host(host) else None
            if restrict_host is not None and ref is not None and ref != restrict_host:
                continue  # a restricted poll reports only its repo's host
            if ref is not None:
                failed.add(ref)
                if one.effect.pause != "none" or one.effect.cancel_host:
                    facts = res.facts(ref)
                    facts.cancelled = True
                    if one.effect.pause != "none":
                        facts.rate_limited = True
                        facts.retry_after = one.retry_after
            if one.effect.failed_kind is not None:
                res.failures.append(
                    Failure("host", ref, one.code or "internal", one.effect.poll_exit)
                )
    else:
        _write_cache(store, now, entries)
    return HostList(entries, frozenset(failed), True)


# --- applying a listing (step 3, State transaction 1) ---


def _upsert_listed(conn: sqlite3.Connection, item: Listed) -> bool:
    """Record the listing on a stored row: `listed_updated_at` (the larger), `pr` and `repo`.
    Returns whether the row exists."""
    row = conn.execute(
        "SELECT listed_updated_at FROM prs WHERE pr_id = ? AND host = ?", (item.pr_id, item.host)
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE prs SET listed_updated_at = ?, pr = ?, repo = ? WHERE pr_id = ? AND host = ?",
        (_later(row[0], item.updated_at), item.pr, item.repo, item.pr_id, item.host),
    )
    return True


def _reactivate(conn: sqlite3.Connection, item: Listed) -> bool:
    """A closed terminal row listed `open` becomes `active` and is re-baselined."""
    if item.state != "open":
        return False
    cur = conn.execute(
        "UPDATE prs SET status = 'active', terminal_at = NULL, terminal_kind = NULL, "
        "rebaseline = 1 WHERE pr_id = ? AND host = ? AND status = 'terminal' "
        "AND terminal_kind = 'closed'",
        (item.pr_id, item.host),
    )
    return cur.rowcount > 0


def _newer(items: Iterable[Listed], into: dict[str, Listed]) -> None:
    for item in items:
        old = into.get(item.pr_id)
        if old is None or item.updated_at >= old.updated_at:
            into[item.pr_id] = item


@dataclass
class _Applied:
    new_members: list[str] = field(default_factory=list)
    reactivated: list[tuple[str, str]] = field(default_factory=list)  # (pr, pr_id)
    candidates: set[str] = field(default_factory=set)


def apply_query_listing(
    store: Store,
    host: str,
    items: list[Listed],
    watches: list[QueryWatch],
    *,
    complete: bool,
    now: datetime,
    restrict: str | None = None,
) -> _Applied:
    """State transaction 1 for one host's open listing.

    With `restrict` (a `--repo` poll) every listed row gets `listed_updated_at`, but only items
    of that repo (case-insensitive) become members, reset `left_count`, are reactivated or are
    candidates; no absence is counted and `hosts.last_enumerated_at` is not written."""
    out = _Applied()
    wanted = None if restrict is None else restrict.casefold()

    def inside(item: Listed) -> bool:
        return wanted is None or item.repo.casefold() == wanted

    open_items = [i for i in items if i.state == "open" and inside(i)]
    with store.transaction() as conn:
        for item in items:
            if _upsert_listed(conn, item) and inside(item):
                out.candidates.add(item.pr_id)
                if _reactivate(conn, item):
                    out.reactivated.append((item.pr, item.pr_id))
        for watch in watches:
            if not watch_exists(conn, watch.id):
                continue
            present: set[str] = set()
            for item in open_items:
                if not watch.matches(item):
                    continue
                if _foreign(conn, item):
                    continue
                present.add(item.pr_id)
                if _join(conn, watch.id, item):
                    out.new_members.append(item.pr_id)
                out.candidates.add(item.pr_id)
            if complete and restrict is None:
                _count_absent(conn, watch.id, host, present)
        if restrict is not None:
            return out
        conn.execute(
            "INSERT INTO hosts (host, last_enumerated_at) VALUES (?, ?) "
            "ON CONFLICT(host) DO UPDATE SET last_enumerated_at = excluded.last_enumerated_at",
            (host, format_ts(now)),
        )
    return out


def _foreign(conn: sqlite3.Connection, item: Listed) -> bool:
    """Whether the stored row of this `pr_id` belongs to another host (not this PR)."""
    row = conn.execute("SELECT host FROM prs WHERE pr_id = ?", (item.pr_id,)).fetchone()
    return row is not None and row[0] != item.host


def _join(conn: sqlite3.Connection, watch_id: str, item: Listed) -> bool:
    """Make the item a member (silently: no digest) or reset its `left_count`. True when the PR
    got a new source."""
    row = conn.execute("SELECT host FROM prs WHERE pr_id = ?", (item.pr_id,)).fetchone()
    if row is not None and row[0] != item.host:
        return False  # the stored row is another host's PR: never joined by this listing
    if row is None:
        conn.execute(
            "INSERT INTO prs (pr_id, pr, repo, host, listed_updated_at) VALUES (?, ?, ?, ?, ?)",
            (item.pr_id, item.pr, item.repo, item.host, item.updated_at),
        )
    cur = conn.execute(
        "INSERT OR IGNORE INTO sources (pr_id, watch_id, left_count) VALUES (?, ?, 0)",
        (item.pr_id, watch_id),
    )
    if cur.rowcount == 0:
        conn.execute(
            "UPDATE sources SET left_count = 0 WHERE pr_id = ? AND watch_id = ?",
            (item.pr_id, watch_id),
        )
    return cur.rowcount > 0


def _count_absent(conn: sqlite3.Connection, watch_id: str, host: str, present: set[str]) -> None:
    rows = conn.execute(
        "SELECT sources.pr_id FROM sources JOIN prs ON prs.pr_id = sources.pr_id "
        "WHERE sources.watch_id = ? AND prs.host = ? AND sources.left_count < ?",
        (watch_id, host, MAX_LEFT),
    ).fetchall()
    for (pr_id,) in rows:
        if pr_id not in present:
            conn.execute(
                "UPDATE sources SET left_count = left_count + 1 WHERE pr_id = ? AND watch_id = ?",
                (pr_id, watch_id),
            )


def apply_repo_listing(
    store: Store,
    repo: str,
    items: list[Listed],
    *,
    complete: bool,
    start: str | None,
    now: datetime,
) -> _Applied:
    """State transaction 1 for one repo's windowed listing: the listed facts, reactivations,
    the new watermark (the largest listed `updated_at`, never later than `now`, else the old one
    or `start`) and, for an incomplete listing, the changed mark of every `active` per-PR row of
    the repo. Items match stored rows by `pr_id` and host, so a renamed repo's rows are refreshed;
    a repo listing never creates a row."""
    out = _Applied()
    with store.transaction() as conn:
        for item in items:
            if _upsert_listed(conn, item):
                out.candidates.add(item.pr_id)
                if _reactivate(conn, item):
                    out.reactivated.append((item.pr, item.pr_id))
        if items:
            mark = min(max(i.updated_at for i in items), format_ts(now))  # ts sorts as text
        else:
            old = conn.execute("SELECT watermark FROM scopes WHERE repo = ?", (repo,)).fetchone()
            mark = old[0] if old is not None and old[0] is not None else start
        conn.execute(
            "INSERT INTO scopes (repo, watermark) VALUES (?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET watermark = excluded.watermark",
            (repo, mark),
        )
        if not complete:
            _mark_changed(conn, repo, out)
    return out


def _mark_changed(conn: sqlite3.Connection, repo: str, out: _Applied) -> None:
    rows = conn.execute(
        "SELECT pr_id, listed_updated_at, snap_updated_at FROM prs WHERE repo = ? "
        "AND status = 'active' AND snap_updated_at IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM sources JOIN watches ON watches.id = sources.watch_id "
        "WHERE sources.pr_id = prs.pr_id AND watches.kind = 'pr')",
        (repo,),
    ).fetchall()
    for pr_id, listed, snap in rows:
        snapped = parse_stamp(snap)
        if snapped is None:
            continue  # unreadable: left to the full pass
        try:
            target = format_ts(snapped + _MICRO)
        except OverflowError:
            continue  # unreadable: left to the full pass
        conn.execute(
            "UPDATE prs SET listed_updated_at = ? WHERE pr_id = ?", (_later(listed, target), pr_id)
        )
        out.candidates.add(str(pr_id))


# --- per-PR repo scopes (step 2b) ---

_LIVE_PR_ROWS: Final = (
    "EXISTS (SELECT 1 FROM sources JOIN watches ON watches.id = sources.watch_id "
    "WHERE sources.pr_id = prs.pr_id AND watches.kind = 'pr') "
    "AND (status = 'active' OR (status = 'terminal' AND terminal_kind = 'closed' "
    "AND terminal_at IS NOT NULL AND terminal_at >= ?))"
)


def plan_repo_scopes(
    store: Store, *, now: datetime, after_terminal_ms: int, overlap_ms: int
) -> list[tuple[str, datetime, str | None]]:
    """The repos to list: `(repo, updated_since, fallback start)`. Also deletes the `scopes`
    rows that no per-PR row (active, or closed inside the expiry) refers to, in the same
    transaction (also in a `--repo` poll, which filters the plan afterwards). `fallback start` is
    the stored watermark's replacement for a first listing."""
    cutoff = format_ts(now - timedelta(milliseconds=after_terminal_ms))
    overlap = timedelta(milliseconds=overlap_ms)
    plan: list[tuple[str, datetime, str | None]] = []
    with store.transaction() as conn:
        repos = [
            str(r[0])
            for r in conn.execute(
                f"SELECT DISTINCT repo FROM prs WHERE {_LIVE_PR_ROWS} ORDER BY repo",  # noqa: S608
                (cutoff,),
            )
        ]
        marks = {str(r[0]): r[1] for r in conn.execute("SELECT repo, watermark FROM scopes")}
        for repo in marks:
            if repo not in repos:
                conn.execute("DELETE FROM scopes WHERE repo = ?", (repo,))
        for repo in repos:
            mark = parse_stamp(marks.get(repo))
            if mark is not None:
                try:
                    plan.append((repo, mark - overlap, None))
                    continue
                except OverflowError:
                    pass  # unreadable watermark: as if there were none
            stamps = [
                s
                for (raw,) in conn.execute(
                    f"SELECT snap_updated_at FROM prs WHERE repo = ? AND digest IS NOT NULL "  # noqa: S608
                    f"AND {_LIVE_PR_ROWS}",
                    (repo, cutoff),
                )
                if (s := parse_stamp(raw)) is not None
            ]
            if stamps:  # a repo whose rows have no digest is not listed (trigger (a) covers it)
                first = min(stamps)
                try:
                    plan.append((repo, first - overlap, format_ts(first)))
                except OverflowError:
                    continue  # unreadable: left to the full pass
    return plan


# --- the pipeline stage ---


def _note_rate(facts: HostFacts, result: dict[str, Any]) -> None:
    rate = result.get("rate_limit")
    if not isinstance(rate, dict):
        return
    left = rate.get("remaining")
    if isinstance(left, bool) or not isinstance(left, int) or left < 0:
        return
    if facts.lowest_remaining is None or left < facts.lowest_remaining:
        reset = rate.get("reset_at")
        facts.lowest_remaining = left
        facts.reset_at = reset if isinstance(reset, str) else None


def _record_failure(
    res: EnumerationResult,
    facts: HostFacts,
    decision: table.Decision,
    *,
    kind_ref: str,
    log: Log | None,
) -> None:
    """Act on one failed (or per-entry) decision of a listing call. `kind_ref` is the repo id; a
    host or query failure is named by the host."""
    effect = decision.effect
    code = decision.code or "internal"
    if effect.failed_kind is not None:
        ref = kind_ref if effect.failed_kind in ("repo", "pr") else facts.host
        res.failures.append(Failure(effect.failed_kind, ref, code, effect.poll_exit))
        if effect.failed_kind in ("host", "query") and log is not None:
            log.write("host_failed", "error" if effect.log_error else "warn",
                      host=facts.host, code=code, **({"message": effect.log_error}
                                                      if effect.log_error else {}))  # fmt: skip
    if effect.pause != "none":
        facts.rate_limited = True
        facts.retry_after = decision.retry_after
    if effect.pause != "none" or effect.cancel_host:
        facts.cancelled = True
    facts.errors.append(HostError(code, decision.retry_after))


def _stopped(res: EnumerationResult, should_stop: Callable[[], bool] | None) -> bool:
    if should_stop is not None and should_stop():
        res.interrupted = True
    return res.interrupted


def enumerate_scopes(
    store: Store,
    client: ForgectlClient,
    config: Config,
    *,
    now: datetime,
    log: Log | None = None,
    paused_hosts: Collection[str] = (),
    restrict_repo: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> EnumerationResult:
    """Steps 1 to 3 of the poll cycle for the whole store.

    `paused_hosts` are skipped (their `paused_until` is the report's, #23); `restrict_repo` runs
    the `--repo` poll: that repo's scope plus the open listing of its host (the host list and the
    query watches that cover that host; see `apply_query_listing`). A host whose
    listing is rate limited or fails as a whole gets no further listing this cycle.
    `should_stop` is polled before every forgectl call (a signal stops the stage)."""
    res = EnumerationResult(HostList((), frozenset(), False))
    ttl = duration_ms(config.poll.hosts_ttl)
    queries = read_query_watches(store.conn)
    restrict_host = None if restrict_repo is None else restrict_repo.partition("/")[0].lower()
    if restrict_host is not None:
        queries = [q for q in queries if q.covers(restrict_host)]
    if queries and not _stopped(res, should_stop):
        load_host_list(
            store, client, now=now, ttl_ms=ttl, result=res, log=log, restrict_host=restrict_host
        )
    plan = plan_repo_scopes(
        store,
        now=now,
        after_terminal_ms=duration_ms(config.expiry.after_terminal),
        overlap_ms=duration_ms(config.poll.overlap),
    )
    if restrict_repo is not None:
        plan = [p for p in plan if p[0].casefold() == restrict_repo.casefold()]
    by_host: dict[str, list[tuple[str, datetime, str | None]]] = {}
    for entry in plan:
        by_host.setdefault(entry[0].partition("/")[0], []).append(entry)
    hosts_to_query = _query_hosts(res, queries, restrict_host)
    if restrict_host is not None:
        hosts_to_query &= {restrict_host}
    for host in sorted({*hosts_to_query, *by_host}):
        if _stopped(res, should_stop):
            break
        facts = res.facts(host)
        if host in paused_hosts:
            facts.cancelled = True
            continue
        if host in hosts_to_query and host not in res.host_list.failed:
            _list_query(store, client, res, facts, queries, now, log, restrict_repo)
        for repo, since, start in by_host.get(host, []):
            if facts.cancelled or _stopped(res, should_stop):
                break
            _list_repo(store, client, res, facts, repo, since, start, now, log)
    return res


def _query_hosts(
    res: EnumerationResult, queries: list[QueryWatch], restrict_host: str | None = None
) -> set[str]:
    """The hosts to list (in scope, some query applies); records the skipped ones."""
    if not queries or not res.host_list.available:
        return set()
    known = {e.host: e for e in res.host_list.hosts}
    wanted: set[str] = set()
    named: set[str] = set()
    for q in queries:
        named.update(q.hosts, (r.partition("/")[0] for r in q.repos))
        if restrict_host is not None:
            named &= {restrict_host}
        if not q.hosts and not q.repos:
            wanted.update(h for h, e in known.items() if e.in_scope)
    wanted.update(h for h in named if h in known and known[h].in_scope)
    for host in named:
        if host in res.host_list.failed:
            continue  # a failed host is reported failed, not skipped
        entry = known.get(host)
        if entry is None:
            res.facts(host).skipped = "not_in_scope"
        elif not entry.in_scope:
            res.facts(host).skipped = "no_credential"
    return {h for h in wanted if any(q.covers(h) for q in queries)}


def _list_query(
    store: Store,
    client: ForgectlClient,
    res: EnumerationResult,
    facts: HostFacts,
    queries: list[QueryWatch],
    now: datetime,
    log: Log | None,
    restrict: str | None,
) -> None:
    host = facts.host
    applicable = [q for q in queries if q.covers(host)]
    facts.queried = True
    outcome = client.prs(host=host, author="me", state="open", limit=LISTING_LIMIT)
    decision = table.decide(outcome, "query")
    effect = decision.effect
    if not (effect.commit or effect.use_result) or outcome.result is None:
        _record_failure(res, facts, decision, kind_ref=host, log=log)
        return
    facts.listings += 1
    _note_rate(facts, outcome.result)
    items, clean = _items(outcome.result, host)
    complete = _is_complete(outcome.result, clean) and effect.commit
    if effect.use_result:
        _partial_entries(res, facts, outcome.result, "query", host, log)
    if not complete:
        facts.incomplete_listing = True
    applied = apply_query_listing(
        store, host, items, applicable, complete=complete, now=now, restrict=restrict
    )
    if restrict is not None:
        items = [i for i in items if i.repo.casefold() == restrict.casefold()]
    _absorb(res, applied, items, log)


def _partial_entries(
    res: EnumerationResult,
    facts: HostFacts,
    result: dict[str, Any],
    call: str,
    ref: str,
    log: Log | None,
) -> None:
    """One decision per `errors[]` entry of a partial result (the table's partial row)."""
    for err in result.get("errors") or []:
        if not isinstance(err, dict) or not isinstance(err.get("code"), str):
            continue
        reason = err.get("reason")
        one = table.decide_code(err["code"], reason if isinstance(reason, str) else None, call)
        _record_failure(res, facts, one, kind_ref=ref, log=log)


def _list_repo(
    store: Store,
    client: ForgectlClient,
    res: EnumerationResult,
    facts: HostFacts,
    repo: str,
    since: datetime,
    start: str | None,
    now: datetime,
    log: Log | None,
) -> None:
    outcome = client.prs(repo=repo, state="all", updated_since=since, limit=LISTING_LIMIT)
    decision = table.decide(outcome, "repo")
    effect = decision.effect
    if not (effect.commit or effect.use_result) or outcome.result is None:
        _record_failure(res, facts, decision, kind_ref=repo, log=log)
        return
    facts.listings += 1
    _note_rate(facts, outcome.result)
    items, clean = _items(outcome.result, facts.host)
    complete = _is_complete(outcome.result, clean) and effect.commit
    if effect.use_result:
        _partial_entries(res, facts, outcome.result, "repo", repo, log)
    applied = apply_repo_listing(store, repo, items, complete=complete, start=start, now=now)
    res.scopes_listed.append(repo)
    _absorb(res, applied, items, log)


def _absorb(
    res: EnumerationResult, applied: _Applied, items: list[Listed], log: Log | None
) -> None:
    _newer(items, res.listed)
    res.candidates |= applied.candidates
    res.new_members.extend(p for p in applied.new_members if p not in res.new_members)
    for pr, pr_id in applied.reactivated:
        if pr_id not in res.reactivated:
            res.reactivated.append(pr_id)
            if log is not None:
                log.write("pr_status", pr=pr, pr_id=pr_id, **{"from": "terminal", "to": "active"})
