"""Cost budget, trigger blindness, search lag (V2), listing queries (V3), V4 and snapshot shape."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.forgectl_support import assert_forgectl_valid
from tests.live.support import GitHubOps, GitLabOps, HostOps, Workspace, nested_bodies, wait_until

pytestmark = pytest.mark.live

Rec = Callable[..., None]
BODY = "pr-watch live test; safe to delete"
THREAD_FILE = "live/thread.txt"
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$", re.I)
BUDGET = {"github": (5, 8), "gitlab": (19, 22)}


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text.upper().replace("Z", "+00:00")).astimezone(UTC)


def listed_at(ws: Workspace, ops: HostOps, number: int, state: str = "open") -> str | None:
    item = ws.list_item(ops.repo_id, number, state)
    return None if item is None else str(item["updated_at"])


def test_cost_budget(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """A snapshot costs 5 to 8 requests on GitHub and 19 to 22 on GitLab (counted at the gh and
    glab wrappers, one `api` call each); a configured `me` never costs more."""
    pr = ops.open_pr("cost", files={THREAD_FILE: "one line\n"})
    ops.comment(pr.number, "live cost comment")
    ops.inline_thread(pr.number, THREAD_FILE, "live cost thread")
    tool = "gh" if ops.kind == "github" else "glab"
    mark = ws.mark()
    ws.snapshot(pr.ref)
    plain = len(ws.api_calls(mark, tool))
    ws.write_forgectl_config(me={ops.kind: ops.user()[0]})
    try:
        mark = ws.mark()
        ws.snapshot(pr.ref)
        with_me = len(ws.api_calls(mark, tool))
    finally:
        ws.write_forgectl_config()
    low, high = BUDGET[ops.kind]
    rec(host=ops.kind, requests=plain, requests_with_me=with_me, budget=f"{low}-{high}")
    assert low <= plain <= high, f"{plain} requests, documented {low} to {high}"
    assert low <= with_me <= plain, f"with me {with_me}, without {plain}"


def test_trigger_blindness(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """`updated_at` is blind to CI completion and thread resolution (GitHub) and to approvals and
    CI (GitLab; bot edits are a manual step, docs/live.md). A new comment is the control: it
    must move `updated_at`, or the test proves nothing."""
    pr = ops.open_pr("trigger", files={THREAD_FILE: "one line\n"})
    ref = pr.ref
    results: dict[str, str] = {}

    def stamps() -> tuple[str, str]:
        return str(listed_at(ws, ops, pr.number)), str(ws.fg_json("show", ref)["updated_at"])

    def blind(name: str, do: Callable[[], None], done: Callable[[], Any]) -> None:
        time.sleep(15)
        before = stamps()
        do()
        wait_until(done, timeout=180, interval=4, what=f"{name} visible in the snapshot")
        time.sleep(15)
        after = stamps()
        results[name] = "blind" if after == before else f"BUMPED {before}->{after}"

    time.sleep(10)
    before_list = stamps()[0]
    ops.comment(pr.number, "live control comment")
    _, waited = wait_until(
        lambda: stamps()[0] != before_list,
        timeout=120,
        what="the control comment moves updated_at in the listing",
    )
    results["control_comment"] = f"bumped_after_{waited:.0f}s"

    ran = 0
    if isinstance(ops, GitHubOps):
        ops.inline_thread(pr.number, THREAD_FILE, "live thread")
        wait_until(lambda: ws.snapshot(ref)["threads"], timeout=120, what="the thread is listed")
        blind("ci_pending", lambda: ops.set_status(pr.head_sha, "pending"),
              lambda: ws.snapshot(ref)["ci"]["state"] == "pending")  # fmt: skip
        blind("ci_completion", lambda: ops.set_status(pr.head_sha, "success"),
              lambda: ws.snapshot(ref)["ci"]["state"] == "passing")  # fmt: skip
        blind("thread_resolution",
              lambda: ops.resolve_thread(pr.number, ws.snapshot(ref)["threads"][0]["id"]),
              lambda: ws.snapshot(ref)["threads"][0]["resolved"])  # fmt: skip
        ran = 3
    elif isinstance(ops, GitLabOps):
        if ops.cfg.token2:
            blind("approval", lambda: ops.approve(pr.number),
                  lambda: any(r["state"] == "approved"
                              for r in ws.snapshot(ref)["latest_by_reviewer"]))  # fmt: skip
            ran += 1
        if ops.cfg.ci:
            blind("ci_completion", lambda: None, lambda: ops.pipelines_done(pr.number))
            ran += 1
    rec(host=ops.kind, **results)
    bumped = {k: v for k, v in results.items() if v.startswith("BUMPED")}
    assert not bumped, f"updated_at moved for actions documented as invisible: {bumped}"
    if ran == 0:
        pytest.skip(
            "manual step: GitLab needs PR_WATCH_LIVE_GITLAB_TOKEN2 (approval) or "
            "PR_WATCH_LIVE_GITLAB_CI=1 (pipeline); docs/live.md"
        )


def test_v2_search_index_lag(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """V2: after the host's own `updated_at` moves, how long until the listing shows it?
    Three comments; the worst lag must stay below the 60 s default of `poll.overlap`."""
    pr = ops.open_pr("lag")
    lags: list[float] = []
    for i in range(3):
        time.sleep(5)
        seen = str(ws.fg_json("show", pr.ref)["updated_at"])
        ops.comment(pr.number, f"live lag comment {i}")
        _, until_show = wait_until(
            lambda seen=seen: str(ws.fg_json("show", pr.ref)["updated_at"]) != seen,
            timeout=120,
            interval=1,
            what="show reflects the comment",
        )
        target = ts(str(ws.fg_json("show", pr.ref)["updated_at"]))
        start = time.monotonic()
        wait_until(
            lambda target=target: (
                (a := listed_at(ws, ops, pr.number)) is not None and ts(a) >= target
            ),
            timeout=180,
            interval=2,
            what="the listing catches up with show",
        )
        lags.append(time.monotonic() - start)
    rec(host=ops.kind, V2_lag_seconds=[round(x, 1) for x in lags], overlap=60)
    assert max(lags) < 60, f"listing lag {max(lags):.0f}s exceeds the 60 s poll.overlap"


def test_v3_author_me_listing(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """V3 (partly manual): `--author me --state open` finds the throwaway PR, lists in the
    documented order and reports `complete`. Whether a repository-count limit cuts the list
    needs an account with open PRs in very many repositories (docs/live.md): the counts printed
    here are the evidence to judge it."""
    a = ops.open_pr("v3a")
    b = ops.open_pr("v3b")
    ops.comment(b.number, "live v3 touch")
    host = f"--host={ops.cfg.host}"

    def found() -> dict[str, Any] | None:
        data = ws.fg_json("prs", host, "--author=me", "--state=open", "--limit=1000")
        numbers = {i["number"] for i in data["items"] if i["repo"].lower() == ops.repo_id.lower()}
        return data if {a.number, b.number} <= numbers else None

    data, waited = wait_until(found, timeout=180, interval=4, what="--author me lists both PRs")
    assert_forgectl_valid("prs", data)
    items = data["items"]
    keys = [(ts(i["updated_at"]), i["pr"]) for i in items]
    assert keys == sorted(keys, key=lambda k: (-k[0].timestamp(), k[1])), (
        "items not ordered by updated_at descending, then pr"
    )
    assert isinstance(data["complete"], bool)
    if len(items) < 1000:
        assert data["complete"] is True, "complete is false below the limit"
    one = ws.fg_json("prs", host, "--author=me", "--state=open", "--limit=1")
    assert len(one["items"]) == 1 and one["complete"] is False
    repos = {i["repo"].lower() for i in items}
    rec(
        host=ops.kind,
        V3_items=len(items),
        V3_repos=len(repos),
        V3_complete=data["complete"],
        seconds_until_listed=round(waited),
        remaining=(data.get("rate_limit") or {}).get("remaining"),
    )


def test_v4_snapshot_updated_at_not_behind_listing(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """V4 and #24: `snapshot.updated_at` and `show.updated_at` are never behind the listing's
    `updated_at` for the same PR (checked after each of several changes)."""
    pr = ops.open_pr("v4")
    checks = 0
    for step in ("opened", "comment", "push"):
        if step == "comment":
            ops.comment(pr.number, "live v4 comment")
        elif step == "push":
            ops.add_commit(pr.branch)
        time.sleep(8)
        item = ws.list_item(ops.repo_id, pr.number)
        assert item is not None
        snap = ws.fg_json("snapshot", pr.ref, "--no-bodies")
        show = ws.fg_json("show", pr.ref)
        assert ts(snap["updated_at"]) >= ts(item["updated_at"]), (step, snap["updated_at"], item)
        assert ts(show["updated_at"]) >= ts(item["updated_at"]), (step, show["updated_at"], item)
        checks += 1
    rec(host=ops.kind, V4_checks=checks, V4_holds=True)


def test_show_of_closed_and_merged(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#24: `show` of a closed and of a merged PR carries `updated_at`, `head.sha`,
    `merge_commit_sha` and `merged_head_sha` (null when only closed)."""
    closed = ops.open_pr("closed")
    ops.close(closed.number)
    merged = ops.open_pr("merged")
    ops.merge(merged.number)
    wait_until(
        lambda: ws.fg_json("show", merged.ref)["state"] == "merged",
        timeout=120,
        what="merged state visible",
    )
    c = ws.fg_json("show", closed.ref)
    m = ws.fg_json("show", merged.ref)
    for doc in (c, m):
        assert TS.match(doc["updated_at"]) and doc["head"]["sha"]
    assert c["state"] == "closed" and c["merged_head_sha"] is None
    assert m["merged_head_sha"] == merged.head_sha and m["merge_commit_sha"]
    item = ws.list_item(ops.repo_id, closed.number, "closed")
    if item is not None:
        assert ts(c["updated_at"]) >= ts(item["updated_at"])
    rec(host=ops.kind, closed_merge_commit=c["merge_commit_sha"], merged_head_ok=True)


def test_no_bodies_flag(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """`snapshot --no-bodies` drops the body of every review and comment and keeps the PR's own."""
    pr = ops.open_pr("bodies", files={THREAD_FILE: "one line\n"}, body=BODY)
    ops.comment(pr.number, "live general comment")
    ops.inline_thread(pr.number, THREAD_FILE, "live inline comment")
    wait_until(
        lambda: nested_bodies(ws.snapshot(pr.ref, bodies=True)),
        timeout=120,
        what="comment bodies in the full snapshot",
    )
    full = ws.snapshot(pr.ref, bodies=True)
    lean = ws.snapshot(pr.ref)
    assert nested_bodies(full), "the control snapshot holds no comment body"
    assert nested_bodies(lean) == [], nested_bodies(lean)
    assert lean["body"] == BODY
    assert_forgectl_valid("snapshot", lean)
    rec(host=ops.kind, bodies_in_full=len(nested_bodies(full)), bodies_in_lean=0)


def test_created_at_presence_format_and_skew(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#34: `created_at` is present and RFC 3339 UTC in the listing, `show` and snapshot, and the
    host clock is within two minutes of the local one (the `opened` window compares them)."""
    before = time.time()
    pr = ops.open_pr("created")
    after = time.time()
    item = ws.list_item(ops.repo_id, pr.number)
    assert item is not None
    docs = {"listing": item, "show": ws.fg_json("show", pr.ref),
            "snapshot": ws.snapshot(pr.ref)}  # fmt: skip
    for name, doc in docs.items():
        assert TS.match(str(doc.get("created_at"))), (name, doc.get("created_at"))
    created = ts(item["created_at"]).timestamp()
    assert before - 120 <= created <= after + 120, (before, created, after)
    assert len({ts(d["created_at"]) for d in docs.values()}) == 1
    rec(host=ops.kind, created_format=item["created_at"], skew_s=round(created - after, 1))


def test_unchanged_snapshots_are_identical(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """Unchanged PRs give identical snapshots after dropping `fetched_at` and `rate_limit`
    (the spikes saw 8 of 9 and 6 of 6): some consecutive pair of three must match."""
    pr = ops.open_pr("determinism")
    ops.comment(pr.number, "live determinism comment")
    time.sleep(10)

    def stable(doc: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in doc.items() if k not in ("fetched_at", "rate_limit")}

    docs = [stable(ws.snapshot(pr.ref)) for _ in range(4)]
    pairs = sum(1 for x, y in zip(docs, docs[1:], strict=False) if x == y)
    rec(host=ops.kind, identical_pairs=f"{pairs}/3")
    assert pairs >= 1, "no two consecutive snapshots of an unchanged PR were identical"


def test_rate_limit_shape(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """`rate_limit`: GitHub reports `remaining` and `reset_at`, GitLab nulls (design table)."""
    pr = ops.open_pr("ratelimit")
    snap = ws.snapshot(pr.ref)
    rl = snap["rate_limit"]
    assert rl["host"] == ops.cfg.host
    if ops.kind == "github":
        assert isinstance(rl["remaining"], int) and TS.match(str(rl["reset_at"])), rl
    else:
        assert rl["remaining"] is None, rl
    rec(host=ops.kind, rate_limit=rl)
