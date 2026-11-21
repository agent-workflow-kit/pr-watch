"""Row-level baseline rules: incomplete counters, reopen, rename and baselined_at (#16).

A small simulator plays the poller: it keeps the `prs` columns the plan reads, applies the
fields each plan returns, and feeds the next snapshot.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch.baseline import MalformedSnapshot, PrIdMismatch, RowState, SnapshotPlan, plan_snapshot
from pr_watch.commit import commit_snapshot
from pr_watch.digest import build_digest, digest_from_json
from pr_watch.state_dir import StateDir, open_state_dir
from pr_watch.store import Store, open_store
from tests.forgectl_support import new_pr, sha
from tests.harness import schema_errors
from tests.test_diff import general, inline, note, plus_reviews, plus_threads, review, thread

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def at(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


class Sim:
    """The stored row of one PR, advanced one snapshot at a time."""

    def __init__(self, snap: dict[str, Any] | None = None) -> None:
        self.row: dict[str, Any] = {
            "pr_id": "", "pr": "", "repo": "", "digest": None, "status": "active",
            "terminal_at": None, "terminal_kind": None, "rebaseline": 0,
            "incomplete": "{}", "baselined_at": None,
        }  # fmt: skip
        self.tick = 0
        self.plans: list[SnapshotPlan] = []
        if snap is not None:
            self.row.update(pr_id=snap["pr_id"], pr=snap["pr"], repo=snap["repo"])
            self.step(snap)

    def state(self) -> RowState:
        r = self.row
        return RowState(
            pr_id=r["pr_id"], pr=r["pr"], repo=r["repo"], digest=r["digest"], status=r["status"],
            terminal_at=r["terminal_at"], terminal_kind=r["terminal_kind"],
            rebaseline=bool(r["rebaseline"]), incomplete=r["incomplete"],
            baselined_at=r["baselined_at"],
        )  # fmt: skip

    def step(self, snap: dict[str, Any], **kw: Any) -> SnapshotPlan:
        self.tick += 1
        kw.setdefault("persistent_after", 5)
        plan = plan_snapshot(self.state(), snap, now=at(self.tick), **kw)
        self.row.update(plan.fields)
        self.plans.append(plan)
        return plan

    def reactivate(self) -> None:
        """Enumeration listed the closed row as open (PR status)."""
        self.row.update(status="active", terminal_at=None, rebaseline=1)

    def digest(self) -> dict[str, Any]:
        out = digest_from_json(self.row["digest"])
        assert out is not None
        return out

    def counters(self) -> dict[str, int]:
        return dict(json.loads(self.row["incomplete"]))


def gh(**changes: Any) -> dict[str, Any]:
    return new_pr(**changes)


def types(plan: SnapshotPlan) -> list[str]:
    return [e.type for e in plan.events]


def incomplete(snap: dict[str, Any], *parts: str) -> dict[str, Any]:
    out = copy.deepcopy(snap)
    out["incomplete"] = list(parts)
    return out


def closed(snap: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(snap)
    out.update(state="closed", closed_at="2026-10-03T10:00:00Z")
    return out


# --- incomplete parts --------------------------------------------------------------------------


def test_incomplete_counter_counts_consecutive_snapshots_then_resets() -> None:
    base = gh()
    sim = Sim(base)
    assert sim.counters() == {}
    for expected in (1, 2, 3):
        sim.step(incomplete(base, "threads"))
        assert sim.counters() == {"threads": expected}
    sim.step(incomplete(base, "threads", "ci"))
    assert sim.counters() == {"threads": 4, "ci": 1}
    sim.step(incomplete(base, "ci"))
    assert sim.counters() == {"ci": 2}
    sim.step(base)
    assert sim.counters() == {}
    assert sim.row["incomplete"] == "{}"


def test_incomplete_counter_uses_the_authoritative_part_set() -> None:
    base = gh()
    sim = Sim(base)
    bad = copy.deepcopy(base)
    bad["reviews"] = "nope"  # unreadable reviews: incomplete although forgectl listed nothing
    bad["ci"]["complete"] = False
    sim.step(bad)
    assert sim.counters() == {"ci": 1, "reviews": 1}


def test_a_malformed_stored_counter_counts_from_zero() -> None:
    base = gh()
    sim = Sim(base)
    for bad in ("not json", "[]", '{"threads": "x"}', '{"threads": -4}', '{"threads": true}', "7"):
        sim.row["incomplete"] = bad
        sim.step(incomplete(base, "threads"))
        assert sim.counters() == {"threads": 1}


def test_persistent_parts_are_reported_once_at_the_threshold() -> None:
    base = gh()
    sim = Sim(base)
    got = [sim.step(incomplete(base, "threads"), persistent_after=3).persistent for _ in range(5)]
    assert got == [(), (), ("threads",), (), ()]


def test_incomplete_part_never_produces_events_and_completes_silently() -> None:
    base = gh()
    sim = Sim(incomplete(base, "threads"))
    assert sim.digest()["baselined"] == {"reviews": True, "threads": False, "ci": True}
    assert sim.row["baselined_at"] == "2026-10-03T12:01:00.000000Z"
    # a comment arrives while the part is incomplete: nothing, the old seen is kept
    plan = sim.step(incomplete(plus_threads(base, general("gh:ic:IC_a")), "threads"))
    assert types(plan) == []
    # the part completes: the comment that came meanwhile is baselined silently
    plan = sim.step(plus_threads(base, general("gh:ic:IC_a")))
    assert types(plan) == []
    assert sim.digest()["baselined"]["threads"] is True
    assert sim.row["baselined_at"] == "2026-10-03T12:03:00.000000Z"
    assert sim.counters() == {}
    # and later comments fire normally
    plan = sim.step(plus_threads(base, general("gh:ic:IC_a"), general("gh:ic:IC_b")))
    assert types(plan) == ["comment"]
    assert sim.row["baselined_at"] == "2026-10-03T12:03:00.000000Z"


def test_over_cap_lists_never_produce_events_and_show_in_the_counter() -> None:
    base = gh()
    sim = Sim(base)
    kept = sim.digest()["seen"]["comments"]
    many = [general(f"gh:ic:IC_{i}") for i in range(3)]
    huge = plus_threads(base, *many)
    huge["threads"] = huge["threads"] + [
        thread(f"gh:rt:T{i}", [note(f"gh:rc:C{i}_{j}") for j in range(100)]) for i in range(101)
    ]
    plan = sim.step(huge)
    assert types(plan) == []
    assert sim.counters() == {"threads": 1}
    assert sim.digest()["baselined"]["threads"] is True  # baselined before, kept
    assert sim.digest()["seen"]["comments"] == kept  # the old data is kept, never overwritten


# --- baselined_at ------------------------------------------------------------------------------


def test_first_snapshot_baselines_and_sets_baselined_at() -> None:
    sim = Sim(gh())
    assert sim.row["baselined_at"] == "2026-10-03T12:01:00.000000Z"
    assert sim.row["rebaseline"] == 0
    assert sim.plans[0].events == []


def test_ordinary_snapshot_keeps_baselined_at() -> None:
    base = gh()
    sim = Sim(base)
    first = sim.row["baselined_at"]
    sim.step(plus_threads(base, general()))
    assert sim.row["baselined_at"] == first


def test_first_sight_terminal_emits_once_and_marks_the_row_terminal() -> None:
    sim = Sim()
    snap = gh(state="merged", merged_at="2026-10-03T09:00:00Z")
    sim.row.update(pr_id=snap["pr_id"], pr=snap["pr"], repo=snap["repo"])
    plan = sim.step(snap)
    assert types(plan) == ["merged"]
    assert (sim.row["status"], sim.row["terminal_kind"]) == ("terminal", "merged")
    assert sim.row["terminal_at"] == "2026-10-03T12:01:00.000000Z"
    assert types(sim.step(snap)) == []  # final


def test_first_sight_closed_emits_once() -> None:
    snap = closed(gh())
    sim = Sim()
    sim.row.update(pr_id=snap["pr_id"], pr=snap["pr"], repo=snap["repo"])
    assert types(sim.step(snap)) == ["closed"]
    assert sim.row["terminal_kind"] == "closed"
    assert types(sim.step(snap)) == []


def test_terminal_at_is_kept_while_the_row_stays_terminal() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    stamp = sim.row["terminal_at"]
    assert stamp is not None
    sim.step(closed(base))
    assert sim.row["terminal_at"] == stamp


def test_snapshot_success_clears_the_failure_count_and_stamps_the_poll() -> None:
    sim = Sim(gh())
    assert sim.plans[0].fields["failure_count"] == 0
    assert sim.plans[0].fields["last_polled_at"] == "2026-10-03T12:01:00.000000Z"


def test_unreadable_state_keeps_the_row_status() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    odd = copy.deepcopy(closed(base))
    odd["state"] = "weird"
    sim.step(odd)  # unreadable: the digest keeps `closed`
    assert (sim.row["status"], sim.row["terminal_kind"]) == ("terminal", "closed")


# --- reopen ------------------------------------------------------------------------------------


def busy(base: dict[str, Any], head: int | None = None) -> dict[str, Any]:
    out = plus_reviews(plus_threads(base, general("gh:ic:IC_c"), inline("gh:rc:PRRC_c")), review())
    out["ci"] = {**out["ci"], "state": "failing"}
    if head is not None:
        out["head"] = {**out["head"], "sha": sha(head)}
    return out


def test_reopen_with_new_comments_reviews_and_ci_emits_nothing() -> None:
    base = gh()
    sim = Sim(base)
    assert types(sim.step(closed(base))) == ["closed"]
    sim.reactivate()
    before = sim.row["baselined_at"]
    plan = sim.step(busy(base))
    assert types(plan) == []
    assert sim.row["rebaseline"] == 0
    assert sim.row["status"] == "active" and sim.row["terminal_kind"] is None
    assert sim.row["baselined_at"] != before
    assert "gh:ic:IC_c" in sim.digest()["seen"]["comments"]
    # afterwards ordinary rules apply
    plan = sim.step(plus_threads(busy(base), general("gh:ic:IC_d")))
    assert types(plan) == ["comment"]


def test_reopen_with_a_new_head_emits_pushed_only() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    sim.reactivate()
    plan = sim.step(busy(base, head=2))
    assert types(plan) == ["pushed"]
    assert plan.events[0].payload["from_sha"] == base["head"]["sha"]


def test_reopen_without_the_flag_is_detected_from_the_stored_state() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    plan = sim.step(busy(base, head=2))  # no reactivate(): closed then open
    assert types(plan) == ["pushed"]
    assert sim.digest()["baselined"] == {"reviews": True, "threads": True, "ci": True}


def test_reopen_then_closed_again_before_the_snapshot_emits_closed_once() -> None:
    base = gh()
    sim = Sim(base)
    assert types(sim.step(closed(base))) == ["closed"]
    sim.reactivate()
    plan = sim.step(closed(busy(base)))
    assert types(plan) == ["closed"]
    assert sim.row["status"] == "terminal" and sim.row["rebaseline"] == 0
    assert types(sim.step(closed(busy(base)))) == []  # not again


def test_reopen_then_merged_emits_merged() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    sim.reactivate()
    merged = busy(base, head=2)
    merged.update(state="merged", merged_at="2026-10-03T11:00:00Z")
    assert types(sim.step(merged)) == ["pushed", "merged"]


def test_reopen_with_an_incomplete_part_baselines_it_silently_later() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    plan = sim.step(incomplete(busy(base), "threads"))  # implicit reopen, threads incomplete
    assert types(plan) == []
    assert sim.digest()["baselined"]["threads"] is False
    plan = sim.step(busy(base))
    assert types(plan) == []
    assert sim.digest()["baselined"]["threads"] is True


def test_reopen_flag_with_an_incomplete_part_is_unbaselined() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    sim.reactivate()
    sim.step(incomplete(base, "ci"))
    assert sim.digest()["baselined"]["ci"] is False
    assert types(sim.step(base)) == []


def test_rebaseline_flag_on_a_row_without_a_digest_is_first_sight() -> None:
    snap = gh()
    sim = Sim()
    sim.row.update(pr_id=snap["pr_id"], pr=snap["pr"], repo=snap["repo"], rebaseline=1)
    assert types(sim.step(busy(snap))) == []
    assert sim.row["rebaseline"] == 0


def test_merged_is_final_even_with_rebaseline() -> None:
    base = gh()
    sim = Sim(base)
    merged = {**base, "state": "merged"}
    sim.step(merged)
    sim.row["rebaseline"] = 1
    before = dict(sim.row)
    plan = sim.step(busy(base, head=2))
    assert types(plan) == []
    for col in ("digest", "status", "terminal_kind", "terminal_at", "baselined_at"):
        assert sim.row[col] == before[col]
    assert sim.row["rebaseline"] == 0
    assert types(sim.step(busy(base, head=3))) == []


def _merged_sim() -> tuple[Sim, dict[str, Any]]:
    base = gh()
    sim = Sim(base)
    sim.step({**base, "state": "merged"})
    return sim, base


def test_open_snapshot_after_merged_keeps_the_row_final() -> None:
    sim, base = _merged_sim()
    before = dict(sim.row)
    assert types(sim.step(busy(base, head=2))) == []
    for col in ("digest", "status", "terminal_kind", "terminal_at"):
        assert sim.row[col] == before[col]
    assert sim.row["status"] == "terminal" and sim.row["terminal_kind"] == "merged"
    assert types(sim.step({**base, "state": "merged"})) == []  # merged never fires twice


def test_closed_snapshot_after_merged_keeps_the_row_merged() -> None:
    sim, base = _merged_sim()
    before = dict(sim.row)
    assert types(sim.step(closed(base))) == []
    for col in ("digest", "status", "terminal_kind", "terminal_at"):
        assert sim.row[col] == before[col]


def test_merged_row_still_counts_incomplete_parts_and_polls() -> None:
    sim, base = _merged_sim()
    sim.step(incomplete({**base, "state": "merged"}, "ci"))
    assert sim.counters() == {"ci": 1}


# --- rename ------------------------------------------------------------------------------------


def renamed(snap: dict[str, Any]) -> dict[str, Any]:
    out = new_pr("github.com", "example-org/gadgets", 7)
    out["pr_id"] = snap["pr_id"]
    for key in ("threads", "reviews", "ci", "head", "latest_by_reviewer"):
        out[key] = copy.deepcopy(snap[key])
    out["head"]["repo"] = out["repo"]
    return out


def test_rename_refreshes_pr_and_repo_without_events() -> None:
    base = gh()
    sim = Sim(base)
    plan = sim.step(renamed(base))
    assert types(plan) == []
    assert sim.row["pr"] == "github.com/example-org/gadgets#7"
    assert sim.row["repo"] == "github.com/example-org/gadgets"
    assert sim.row["pr_id"] == base["pr_id"]


def test_comment_after_a_rename_is_one_event_under_the_new_ref() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(renamed(base))
    plan = sim.step(plus_threads(renamed(base), general("gh:ic:IC_new")))
    (event,) = plan.events
    assert event.type == "comment"
    assert event.pr == "github.com/example-org/gadgets#7"
    assert event.pr_id == base["pr_id"]
    assert event.payload["repo"] == "github.com/example-org/gadgets"


def test_rename_and_comment_in_one_snapshot_is_one_event() -> None:
    base = gh()
    sim = Sim(base)
    plan = sim.step(plus_threads(renamed(base), general("gh:ic:IC_new")))
    assert types(plan) == ["comment"]
    assert plan.events[0].pr == "github.com/example-org/gadgets#7"
    assert sim.row["pr"] == "github.com/example-org/gadgets#7"


def test_a_malformed_ref_in_the_snapshot_keeps_the_stored_ref() -> None:
    base = gh()
    sim = Sim(base)
    bad = copy.deepcopy(base)
    bad.update(pr="not a ref", repo="x" * 600)
    plan = sim.step(bad)
    assert sim.row["pr"] == base["pr"] and sim.row["repo"] == base["repo"]
    assert "pr" not in plan.fields and "repo" not in plan.fields


def test_a_different_pr_id_is_refused() -> None:
    base = gh()
    sim = Sim(base)
    other = {**base, "pr_id": "PR_other"}
    with pytest.raises(PrIdMismatch):
        sim.step(other)


def test_a_foreign_host_keeps_both_stored_refs() -> None:
    base = gh()
    sim = Sim(base)
    other = new_pr("elsewhere.example", "example-org/widgets", 5)
    other.update(pr_id=base["pr_id"])
    plan = sim.step(other)
    assert sim.row["pr"] == base["pr"] and sim.row["repo"] == base["repo"]
    assert "pr" not in plan.fields and "repo" not in plan.fields


def test_a_mismatched_pr_repo_pair_keeps_both_stored_refs() -> None:
    base = gh()
    sim = Sim(base)
    bad = {**base, "repo": "github.com/example-org/other"}
    plan = sim.step(bad)
    assert "pr" not in plan.fields and "repo" not in plan.fields
    assert sim.row["repo"] == base["repo"]


@pytest.mark.parametrize("which", ["pr", "repo"])
def test_one_invalid_ref_keeps_both_stored_refs(which: str) -> None:
    base = gh()
    sim = Sim(base)
    plan = sim.step({**base, which: "not a ref"})
    assert "pr" not in plan.fields and "repo" not in plan.fields


def test_a_non_mapping_snapshot_is_malformed_not_a_pr_id_mismatch() -> None:
    sim = Sim(gh())
    for bad in (None, [], "x", 3):
        with pytest.raises(MalformedSnapshot):
            sim.step(bad)  # type: ignore[arg-type]
    assert not issubclass(MalformedSnapshot, PrIdMismatch)


@pytest.mark.parametrize("bad", [True, 0, -1, 2.0, "3", None])
def test_persistent_after_must_be_a_positive_int(bad: Any) -> None:
    sim = Sim(gh())
    with pytest.raises(ValueError, match="persistent_after"):
        sim.step(gh(), persistent_after=bad)


def test_persistent_after_is_required() -> None:
    sim = Sim(gh())
    with pytest.raises(TypeError):
        plan_snapshot(sim.state(), gh(), now=at(9))  # type: ignore[call-arg]


@pytest.mark.parametrize("status", ["unreachable"])
def test_a_successful_snapshot_ends_unreachable(status: str) -> None:
    base = gh()
    sim = Sim(base)
    sim.row["status"] = status
    sim.step(base)
    assert sim.row["status"] == "active"
    sim.row["status"] = status
    odd = {**base, "state": "weird"}
    sim.step(odd)  # unreadable state
    assert sim.row["status"] == "active"
    sim.row["status"] = status
    sim.step(closed(base))
    assert (sim.row["status"], sim.row["terminal_kind"]) == ("terminal", "closed")


def test_persistent_is_not_reported_for_a_terminal_result() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(incomplete(base, "threads"), persistent_after=2)
    plan = sim.step(incomplete(closed(base), "threads"), persistent_after=2)
    assert plan.persistent == ()
    assert sim.counters() == {"threads": 2}


def test_unchanged_snapshot_with_a_digest_and_no_flag_emits_nothing() -> None:
    base = gh()
    sim = Sim(base)
    assert types(sim.step(base)) == []
    assert sim.row["rebaseline"] == 0


def test_forced_mapping_through_plan_snapshot() -> None:
    base = gh()
    pushed = gh(head={"sha": sha(2)})
    for compare, expected in (
        (lambda a, b: True, True), (lambda a, b: False, False), (None, None),
    ):  # fmt: skip
        sim = Sim(base)
        (event,) = sim.step(pushed, forced=compare).events
        assert event.payload["forced"] is expected


def test_over_cap_part_never_emits_even_with_new_items() -> None:
    base = gh()
    sim = Sim(base)
    huge = plus_threads(base, general("gh:ic:IC_new"))
    huge["threads"] = huge["threads"] + [
        thread(f"gh:rt:T{i}", [note(f"gh:rc:C{i}_{j}") for j in range(100)]) for i in range(101)
    ]
    for _ in range(2):
        assert types(sim.step(huge)) == []


# --- determinism, schemas, commit --------------------------------------------------------------


def test_plan_is_deterministic_and_leaves_inputs_alone() -> None:
    base = gh()
    sim = Sim(base)
    sim.step(closed(base))
    sim.reactivate()
    snap = busy(base, head=2)
    frozen = copy.deepcopy(snap)
    state = sim.state()
    a = plan_snapshot(state, snap, now=at(9), persistent_after=5)
    b = plan_snapshot(state, snap, now=at(9), persistent_after=5)
    assert snap == frozen
    assert a.fields == b.fields
    assert [(e.type, dict(e.payload)) for e in a.events] == [
        (e.type, dict(e.payload)) for e in b.events
    ]


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDir]:
    with open_state_dir(tmp_path / "pr-watch") as s:
        yield s


@pytest.fixture
def store(state: StateDir) -> Iterator[Store]:
    with open_store(state) as s:
        yield s


def test_plan_fields_commit_and_events_validate_against_the_schema(store: Store) -> None:
    base = gh()
    store.conn.execute(
        "INSERT INTO watches VALUES ('w-aaaaaaaaaa', 'pr', 'k', '2026-01-01T00:00:00.000000Z')"
    )
    store.conn.execute(
        "INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, 'github.com/old/name#7', 'old', "
        "'github.com')",
        (base["pr_id"],),
    )
    store.conn.execute("INSERT INTO sources VALUES (?, 'w-aaaaaaaaaa', 0)", (base["pr_id"],))
    sim = Sim()
    sim.row.update(pr_id=base["pr_id"], pr="github.com/old/name#7", repo="old")
    rev = 0
    seen: set[str] = set()
    sequence = [
        base,
        plus_threads(base, general("gh:ic:IC_1")),
        closed(plus_threads(base, general("gh:ic:IC_1"))),
    ]
    for snap in sequence:
        plan = sim.step(snap)
        result = commit_snapshot(
            store,
            pr_id=base["pr_id"],
            expected_rev=rev,
            snapshot_pr_id=snap["pr_id"],
            fields=plan.fields,
            prepare=lambda conn, p=plan: [
                e.to_new_event(labels=(), watches=("w-aaaaaaaaaa",)) for e in p.events
            ],
            snapshot_updated_at=snap["updated_at"],
        )
        assert result.committed
        rev = result.new_rev or 0
        for stored in result.events:
            assert schema_errors("event", stored.to_json()) == []
            seen.add(stored.type)
    assert seen == {"comment", "closed"}
    row = store.conn.execute(
        "SELECT pr, repo, status, terminal_kind, rebaseline, baselined_at, incomplete, digest "
        "FROM prs WHERE pr_id = ?",
        (base["pr_id"],),
    ).fetchone()
    assert tuple(row[:7]) == (
        base["pr"], base["repo"], "terminal", "closed", 0, "2026-10-03T12:01:00.000000Z", "{}",
    )  # fmt: skip
    assert digest_from_json(row[7]) == sim.digest()


@pytest.mark.parametrize(
    "field,value", [("pr", "bad ref"), ("repo", "x" * 300), ("pr", 5), ("repo", None)]
)
def test_commit_snapshot_rejects_invalid_refs(store: Store, field: str, value: Any) -> None:
    with pytest.raises(ValueError, match="valid reference"):
        commit_snapshot(store, pr_id="p", expected_rev=0, snapshot_pr_id="p", fields={field: value})


def test_fields_only_hold_writable_columns() -> None:
    from pr_watch.commit import WRITABLE_FIELDS

    sim = Sim(gh())
    for plan in sim.plans:
        assert set(plan.fields) <= WRITABLE_FIELDS


def test_baselined_digest_matches_a_plain_build_on_first_sight() -> None:
    snap = gh()
    sim = Sim(snap)
    assert sim.digest() == build_digest(snap)
