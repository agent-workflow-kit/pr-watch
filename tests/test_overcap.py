"""The over-cap `show` fallback and force-push detection in the snapshot pool (#24)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import selection as sel
from pr_watch import snapshots as sn
from pr_watch.config import Config
from pr_watch.forgectl_client import ForgectlClient, Outcome
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
from tests.forgectl_support import FakeForgectl, sha
from tests.test_snapshots import (
    HOST,
    Rec,
    baseline,
    col,
    cycle,
    make_client,
    pr,
    seed,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_state_dir(tmp_path / "pr-watch") as sd, open_store(sd) as st:
        yield st


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


CAP = 200_000  # a snapshot padded past this is over the cap; a `show` is far below it
UNREACHABLE_AFTER = Config.model_validate({"failures": {"unreachable_after": 2}})


def capped_client() -> ForgectlClient:
    base = make_client()
    return ForgectlClient(
        base.exe, timeout_ms=5000, max_output=CAP, env=base.env, deadline_slack=5.0,
        term_grace=0.3,
    )  # fmt: skip


def over_cap(fake: FakeForgectl, **match: Any) -> None:
    fake.oversize("snapshot", 3 * CAP, **match)


def merged_pr(n: int, **ch: Any) -> dict[str, Any]:
    ch.setdefault("merged_at", "2026-10-02T10:00:00Z")
    ch.setdefault("merged_head_sha", sha(3))
    ch.setdefault("merge_commit_sha", sha(4))
    return pr(n, state="merged", **ch)


def full_pass() -> sel.HostPlan:
    """A full pass that started after every attempt so far."""
    return sel.HostPlan(HOST, 4, pass_started_at=datetime.now(UTC) + timedelta(seconds=5))


def events(store: Store) -> list[dict[str, Any]]:
    rows = store.conn.execute("SELECT type, pr, payload FROM events ORDER BY seq").fetchall()
    return [{"type": t, "pr": pr_, **json.loads(p)} for t, pr_, p in rows]


def run(store: Store, config: Config | None = None, **kw: Any) -> Any:
    return cycle(store, config, client=capped_client(), **kw)


# --- body-free snapshot: padded comments no longer reach the cap ---


def padded_note(cid: str, text: str) -> dict[str, Any]:
    return {
        "author": {"id": "1004", "is_bot": False, "login": "user-4"},
        "body": text, "created_at": "2026-10-02T10:45:00Z", "id": cid, "minimized": False,
        "reply_to": None, "review_id": None, "updated_at": "2026-10-02T10:45:00Z",
    }  # fmt: skip


def test_a_pr_padded_with_comment_bodies_still_produces_events(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    cid = "gh:ic:IC_padded0001"
    thread = {
        "comments": [padded_note(cid, "x" * (3 * CAP))], "commit_sha": None, "id": cid,
        "line": None, "outdated": False, "path": None, "resolvable": False, "resolved": False,
        "side": None, "start_line": None,
    }  # fmt: skip
    fake.update(
        snap["pr"],
        threads=[*snap["threads"], thread],
        reviews=[{**r, "body": "y" * (3 * CAP)} for r in snap["reviews"]],
        updated_at="2026-10-02T11:00:00Z",
    )
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    out, res = run(store)
    assert events(store)[-1]["type"] == "comment" and events(store)[-1]["comment_id"] == cid
    assert len(fake.calls("show")) == 0 and res.failures == []
    assert col(store, pr_id, "failure_count") == 0 and out.host(HOST).snapshots == 1


# --- over the cap: still open ---


def test_an_open_over_cap_pr_counts_one_failure_and_costs_one_show(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    over_cap(fake)
    log = Rec()
    out, res = run(store, log=log)
    assert len(fake.calls("show")) == 1
    assert col(store, pr_id, "failure_count") == 1
    assert col(store, pr_id, "last_failure_code") == "too_large"
    assert col(store, pr_id, "digest") is None and events(store) == []
    assert [(f.kind, f.code) for f in res.failures] == [("pr", "too_large")]
    assert out.host(HOST).snapshots == 0
    assert log.of("snapshot_failed")[0]["code"] == "too_large"


def test_an_over_cap_pr_becomes_unreachable_and_each_full_pass_costs_one_show(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    over_cap(fake)
    run(store, UNREACHABLE_AFTER)
    store.conn.execute("UPDATE prs SET rebaseline = 1")  # chosen again
    store.conn.commit()
    out, _ = run(store, UNREACHABLE_AFTER)
    assert col(store, pr_id, "status") == "unreachable"
    assert [(u[1], u[2]) for u in out.unreachable] == [(pr_id, "too_large")]
    assert len(fake.calls("show")) == 2
    for expected in (3, 4):  # full passes retry it, one show each
        run(store, UNREACHABLE_AFTER, plan=full_pass())
        assert len(fake.calls("show")) == expected
        assert col(store, pr_id, "status") == "unreachable"
    assert events(store) == []


def test_a_show_that_reports_open_is_the_counted_too_large_failure(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1, state="open", head={"sha": sha(7)})
    fake.add(snap)
    pr_id = baseline(store, fake, pr(2))  # an unrelated baselined row stays untouched
    seed(store, snap)
    over_cap(fake, pr=snap["pr"])
    run(store)
    over_id = snap["pr_id"]
    assert col(store, over_id, "failure_count") == 1
    assert col(store, over_id, "last_failure_code") == "too_large"
    assert col(store, over_id, "status") == "active" and col(store, over_id, "digest") is None
    assert events(store) == [] and col(store, pr_id, "failure_count") == 0


# --- over the cap: merged or closed ---


def test_a_merged_over_cap_pr_gets_one_merged_event_and_a_minimal_digest(
    store: Store, fake: FakeForgectl
) -> None:
    snap = merged_pr(1, head={"sha": sha(3)})
    fake.add(snap)
    pr_id = seed(store, snap, failure_count=3, last_failure_code="too_large")
    over_cap(fake)
    log = Rec()
    out, res = run(store, log=log)
    assert len(fake.calls("show")) == 1 and res.failures == []
    (ev,) = events(store)
    assert ev["type"] == "merged"
    assert (ev["merged_head_sha"], ev["merge_commit_sha"]) == (sha(3), sha(4))
    assert ev["head_sha"] == sha(3) and ev["watches"]
    assert col(store, pr_id, "failure_count") == 0
    assert (col(store, pr_id, "status"), col(store, pr_id, "terminal_kind")) == (
        "terminal", "merged",
    )  # fmt: skip
    digest = json.loads(col(store, pr_id, "digest"))
    assert digest["state"] == "merged"
    assert (digest["merged_head_sha"], digest["merge_commit_sha"]) == (sha(3), sha(4))
    assert digest["baselined"] == {
        "reviews": False, "threads": False, "ci": False, "review_requests": False,
    }  # fmt: skip
    assert digest["head_sha"] is None and digest["reviewers"] == {}
    assert out.host(HOST).snapshots == 1 and log.of("snapshot_failed") == []


def test_a_closed_over_cap_pr_with_a_digest_changes_only_the_terminal_fields(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    before = json.loads(col(store, pr_id, "digest"))
    fake.update(
        snap["pr"], state="closed", closed_at="2026-10-03T10:00:00Z", head={"sha": sha(9)},
        updated_at="2026-10-03T10:00:00Z", draft=not before["draft"],
    )  # fmt: skip
    store.conn.execute(
        "UPDATE prs SET listed_updated_at = '2026-10-03T10:00:00.000000Z', failure_count = 4, "
        "status = 'unreachable'"
    )
    store.conn.commit()
    over_cap(fake)
    run(store, plan=full_pass())
    assert [e["type"] for e in events(store)] == ["closed"]  # no pushed, ready or ci
    assert events(store)[0]["head_sha"] == before["head_sha"]
    after = json.loads(col(store, pr_id, "digest"))
    assert after == {**before, "state": "closed"}
    assert col(store, pr_id, "failure_count") == 0
    assert (col(store, pr_id, "status"), col(store, pr_id, "terminal_kind")) == (
        "terminal", "closed",
    )  # fmt: skip


def test_an_already_closed_row_that_shows_closed_over_the_cap_fires_nothing(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1, state="closed", closed_at="2026-10-02T10:00:00Z")
    pr_id = baseline(store, fake, snap)
    seen = len(events(store))  # first sight of a closed PR fires its one `closed`
    store.conn.execute(
        "UPDATE prs SET status = 'active', terminal_kind = NULL, terminal_at = NULL, rebaseline = 0"
    )
    store.conn.commit()
    over_cap(fake)
    run(store, plan=full_pass())
    assert len(fake.calls("show")) == 1
    assert len(events(store)) == seen and col(store, pr_id, "failure_count") == 0
    assert col(store, pr_id, "status") == "terminal"


def test_a_merged_row_stays_final_when_the_fallback_runs(store: Store, fake: FakeForgectl) -> None:
    snap = merged_pr(1)
    pr_id = baseline(store, fake, snap)
    digest = col(store, pr_id, "digest")
    store.conn.execute("UPDATE prs SET rebaseline = 1, status = 'active'")
    store.conn.commit()
    over_cap(fake)
    run(store, plan=full_pass())
    assert col(store, pr_id, "digest") == digest and len(events(store)) == 1  # the baseline's own


def test_a_stored_malformed_head_does_not_drop_the_terminal_event(
    store: Store, fake: FakeForgectl
) -> None:
    snap = merged_pr(1, head={"sha": sha(3)})
    fake.add(snap)
    pr_id = baseline(store, fake, pr(1, head={"sha": sha(5)}))
    digest = json.loads(col(store, pr_id, "digest"))
    digest["head_sha"] = "not-a-sha"
    store.conn.execute(
        "UPDATE prs SET digest = ?, listed_updated_at = '2026-10-03T10:00:00.000000Z'",
        (json.dumps(digest),),
    )
    store.conn.commit()
    over_cap(fake)
    fake.update(snap["pr"], state="merged", merged_at="2026-10-02T10:00:00Z",
                merged_head_sha=sha(3), merge_commit_sha=sha(4),
                updated_at="2026-10-03T10:00:00Z")  # fmt: skip
    run(store, plan=full_pass())
    (ev,) = events(store)
    assert ev["type"] == "merged" and ev["head_sha"] == sha(5)  # the head the show reports


def test_the_call_count_of_a_fallback_job_is_two(store: Store, fake: FakeForgectl) -> None:
    snap = merged_pr(1)
    fake.add(snap)
    seed(store, snap)
    over_cap(fake)
    out, _ = run(store)
    assert out.host(HOST).calls == 2


def test_the_lease_ttl_covers_two_calls_of_one_worker(store: Store) -> None:
    cfg = Config.model_validate({"forgectl": {"timeout": "5s"}})
    ttl = sn.make_leases(store, cfg).ttl
    assert ttl >= timedelta(seconds=2 * (5 + 10))


# --- over the cap: the show itself fails ---


def test_a_show_with_another_pr_id_is_pr_id_changed_and_stores_nothing(
    store: Store, fake: FakeForgectl
) -> None:
    snap = merged_pr(1)
    fake.add(snap)
    seed(store, {**snap, "pr_id": "PR_old"})
    over_cap(fake)
    out, res = run(store)
    assert len(fake.calls("show")) == 1
    assert col(store, "PR_old", "last_failure_code") == "pr_id_changed"
    assert col(store, "PR_old", "failure_count") == 1 and col(store, "PR_old", "digest") is None
    assert events(store) == [] and [f.code for f in res.failures] == ["pr_id_changed"]
    assert col(store, "PR_old", "status") == "active"


def bad_stamp_show(fake: FakeForgectl, snap: dict[str, Any]) -> None:
    """A `show` whose `updated_at` is unreadable (raw: the schema would reject it)."""
    fake.raw("show", stdout=json.dumps({**snap, "updated_at": "not a time"}))


def test_another_pr_id_with_an_unreadable_updated_at_is_pr_id_changed(
    store: Store, fake: FakeForgectl
) -> None:
    snap = merged_pr(1)
    fake.add(snap)
    bad_stamp_show(fake, snap)
    seed(store, {**snap, "pr_id": "PR_old"})
    over_cap(fake)
    run(store)
    assert col(store, "PR_old", "last_failure_code") == "pr_id_changed"


def test_an_open_show_with_an_unreadable_updated_at_is_too_large(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    bad_stamp_show(fake, snap)
    pr_id = seed(store, snap)
    over_cap(fake)
    run(store)
    assert col(store, pr_id, "last_failure_code") == "too_large"


def test_a_terminal_show_with_an_unreadable_updated_at_is_a_contract_failure(
    store: Store, fake: FakeForgectl
) -> None:
    snap = merged_pr(1)
    fake.add(snap)
    bad_stamp_show(fake, snap)
    pr_id = seed(store, snap)
    over_cap(fake)
    run(store)
    assert col(store, pr_id, "last_failure_code") == "contract" and events(store) == []


def test_a_failing_show_is_a_counted_failure_of_its_own_code(
    store: Store, fake: FakeForgectl
) -> None:
    snap = merged_pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    over_cap(fake)
    fake.network_error("show")
    _, res = run(store)
    assert col(store, pr_id, "failure_count") == 1
    assert col(store, pr_id, "last_failure_code") == "network_error"
    assert events(store) == [] and [f.code for f in res.failures] == ["network_error"]


def test_a_too_large_show_is_counted_and_never_asks_for_another_show(
    store: Store, fake: FakeForgectl
) -> None:
    snap = merged_pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    over_cap(fake)
    fake.oversize("show", 3 * CAP)
    run(store)
    assert len(fake.calls("show")) == 1
    assert col(store, pr_id, "failure_count") == 1
    assert col(store, pr_id, "last_failure_code") == "too_large" and events(store) == []


def test_a_rate_limited_show_pauses_the_host_without_counting(
    store: Store, fake: FakeForgectl
) -> None:
    ids = []
    for n in (1, 2, 3):
        s = merged_pr(n)
        fake.add(s)
        ids.append(seed(store, s))
    over_cap(fake)
    fake.rate_limited("show", retry_after=30)
    _, res = run(store, plan=sel.HostPlan(HOST, 1))
    assert [col(store, i, "failure_count") for i in ids] == [0, 0, 0]
    assert events(store) == [] and len(fake.calls("show")) == 1
    assert res.facts(HOST).rate_limited and res.facts(HOST).cancelled


def test_a_garbled_show_is_a_counted_contract_failure(store: Store, fake: FakeForgectl) -> None:
    snap = merged_pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    over_cap(fake)
    fake.contract_violation("show", ["pr_id"])
    run(store)
    assert col(store, pr_id, "last_failure_code") == "contract" and events(store) == []


def test_an_over_cap_snapshot_does_not_stop_the_other_prs(store: Store, fake: FakeForgectl) -> None:
    big, small = merged_pr(1), pr(2)
    fake.add(big)
    fake.add(small)
    seed(store, big)
    ok = seed(store, small)
    over_cap(fake, pr=big["pr"])
    out, _ = run(store)
    assert col(store, ok, "digest") is not None and out.host(HOST).snapshots == 2


# --- force-push detection ---


def push(fake: FakeForgectl, store: Store, snap: dict[str, Any], status: str | None) -> str:
    """Baseline `snap`, push a new head and script the compare (None: leave it unscripted)."""
    pr_id = baseline(store, fake, snap)
    old = json.loads(col(store, pr_id, "digest"))["head_sha"]
    fake.update(snap["pr"], head={"sha": sha(5)}, updated_at="2026-10-02T11:00:00Z")
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    if status is not None:
        fake.compare(snap["pr"], old, sha(5), status, diff="d" if status == "ahead" else None)
    return pr_id


FORCE_ON = Config.model_validate({"detect_force_push": True})


def pushed(store: Store) -> dict[str, Any]:
    (ev,) = events(store)
    assert ev["type"] == "pushed"
    return ev


@pytest.mark.parametrize(
    ("status", "forced"),
    [("diverged", True), ("behind", True), ("unavailable", True), ("ahead", False),
     ("identical", None)],
)  # fmt: skip
def test_forced_comes_from_the_compare_status(
    store: Store, fake: FakeForgectl, status: str, forced: bool | None
) -> None:
    push(fake, store, pr(1), status)
    cycle(store, FORCE_ON)
    assert pushed(store)["forced"] is forced
    (argv,) = fake.argvs("compare")
    assert "--no-diff" in argv  # only the status is read


def test_a_failed_compare_leaves_forced_null_and_the_push_is_stored(
    store: Store, fake: FakeForgectl
) -> None:
    pr_id = push(fake, store, pr(1), "diverged")
    fake.network_error("compare")
    out, res = cycle(store, FORCE_ON)
    assert pushed(store)["forced"] is None
    assert col(store, pr_id, "failure_count") == 0 and res.failures == []
    assert out.host(HOST).snapshots == 1


def test_an_unreadable_compare_leaves_forced_null(store: Store, fake: FakeForgectl) -> None:
    push(fake, store, pr(1), "diverged")
    fake.garbage("compare")
    cycle(store, FORCE_ON)
    assert pushed(store)["forced"] is None


def test_detect_force_push_off_makes_no_compare_call(store: Store, fake: FakeForgectl) -> None:
    push(fake, store, pr(1), "diverged")
    cycle(store)
    assert pushed(store)["forced"] is None and fake.calls("compare") == []


def test_detect_force_push_is_per_repo(store: Store, fake: FakeForgectl) -> None:
    push(fake, store, pr(1), "diverged")
    cfg = Config.model_validate(
        {"detect_force_push": True, "repos": {"github.com/example-org/widgets": {
            "detect_force_push": False}}}
    )  # fmt: skip
    cycle(store, cfg)
    assert pushed(store)["forced"] is None and fake.calls("compare") == []


def test_no_push_means_no_compare(store: Store, fake: FakeForgectl) -> None:
    snap = pr(1)
    baseline(store, fake, snap)
    fake.update(snap["pr"], title="new", updated_at="2026-10-02T11:00:00Z")
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    cycle(store, FORCE_ON)
    assert fake.calls("compare") == []


def test_compares_run_in_the_worker_threads_and_each_pr_gets_its_own_status(
    store: Store, fake: FakeForgectl
) -> None:
    statuses = {1: "diverged", 2: "ahead", 3: "behind", 4: "identical"}
    expected: dict[str, bool | None] = {}
    olds = {}
    for n in statuses:
        snap = pr(n)
        pr_id = baseline(store, fake, snap)
        olds[n] = (snap, json.loads(col(store, pr_id, "digest"))["head_sha"])
    for n, (snap, old) in olds.items():
        fake.update(snap["pr"], head={"sha": sha(5)}, updated_at="2026-10-02T11:00:00Z")
        fake.compare(snap["pr"], old, sha(5), statuses[n])
    fake.slow("compare", 0.3)
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    out, _ = cycle(store, FORCE_ON)
    expected = {"diverged": True, "ahead": False, "behind": True, "identical": None}
    got = {e["pr"]: e["forced"] for e in events(store) if e["type"] == "pushed"}
    assert got == {olds[n][0]["pr"]: expected[s] for n, s in statuses.items()}
    assert out.host(HOST).events == 4 and len(fake.calls("compare")) == 4


# --- compare_forced_for with a stub client ---


class Stub:
    def __init__(self, result: Any) -> None:
        self.result, self.calls = result, []

    def compare(self, ref: Any, a: str, b: str, *, no_diff: bool = False) -> Outcome:
        self.calls.append((ref.canonical, a, b, no_diff))
        if isinstance(self.result, BaseException):
            raise self.result
        return Outcome(command="compare", returncode=0, result={"status": self.result})  # type: ignore[call-arg]


def start(**kw: Any) -> sn.RowStart:
    from pr_watch.baseline import RowState

    state = RowState(pr_id="PR_x", pr="github.com/example-org/widgets#1", repo="r")
    base: dict[str, Any] = dict(
        state=state, rev=1, listed_updated_at=None, unsettled_since=None, failure_count=0,
        last_failure_code=None, settle_polls=1, unreachable_after=5, detect_force_push=True,
    )  # fmt: skip
    return sn.RowStart(**{**base, **kw})


def test_a_64_hex_head_makes_no_compare_call_and_forced_is_null() -> None:
    stub = Stub("diverged")
    check = sn.compare_forced_for(stub)(start())  # type: ignore[arg-type]
    assert check is not None
    assert check("a" * 64, "b" * 64) is None and check("a" * 40, "b" * 64) is None
    assert stub.calls == []
    assert check("a" * 40, "b" * 40) is True and len(stub.calls) == 1
    assert stub.calls[0][3] is True


def test_a_too_large_compare_is_forced_null() -> None:
    class Big(Stub):
        def compare(self, ref: Any, a: str, b: str, *, no_diff: bool = False) -> Outcome:
            return Outcome(command="compare", returncode=None, local="too_large", too_large=True)

    check = sn.compare_forced_for(Big(None))(start())  # type: ignore[arg-type]
    assert check is not None and check("a" * 40, "b" * 40) is None


def test_the_check_is_none_when_detection_is_off() -> None:
    assert sn.compare_forced_for(Stub("x"))(start(detect_force_push=False)) is None  # type: ignore[arg-type]


def test_a_raising_client_is_forced_null_not_a_failed_diff() -> None:
    check = sn.compare_forced_for(Stub(RuntimeError("boom")))(start())  # type: ignore[arg-type]
    assert check is not None and check("a" * 40, "b" * 40) is None


# --- plan_overcap ---


def test_plan_overcap_reads_only_terminal_states_and_keeps_64_hex_shas() -> None:
    from datetime import UTC, datetime

    from pr_watch.baseline import MalformedSnapshot, PrIdMismatch, RowState, plan_overcap

    repo = "github.com/example-org/widgets"
    row = RowState(pr_id="PR_x", pr=f"{repo}#1", repo=repo)
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "pr_id": "PR_x", "pr": row.pr, "repo": row.repo, "state": "open",
        "head": {"sha": "a" * 64}, "merged_head_sha": "a" * 64, "merge_commit_sha": "b" * 64,
        "merged_at": "2026-10-02T10:00:00Z",
    }  # fmt: skip
    assert plan_overcap(row, base, now=now) is None
    assert plan_overcap(row, {**base, "state": "weird"}, now=now) is None
    plan = plan_overcap(row, {**base, "state": "merged"}, now=now)
    assert plan is not None and [e.type for e in plan.events] == ["merged"]
    assert plan.events[0].payload["merge_commit_sha"] == "b" * 64
    assert plan.fields["failure_count"] == 0 and plan.fields["terminal_kind"] == "merged"
    with pytest.raises(PrIdMismatch):
        plan_overcap(row, {**base, "pr_id": "other", "state": "merged"}, now=now)
    with pytest.raises(MalformedSnapshot):
        plan_overcap(row, [], now=now)  # type: ignore[arg-type]
