"""pr-watch end to end on real hosts: events per host change, no events when nothing changed,
force-push detection, review requests and approvals, the poll report and a hook run."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.harness import assert_valid
from tests.live.support import GitHubOps, GitLabOps, HostError, HostOps, Workspace, wait_until

pytestmark = pytest.mark.live

Rec = Callable[..., None]
FORCE_ENV = {"PR_WATCH_DETECT_FORCE_PUSH": "true"}
NEVER = {"unreachable", "recovered", "renamed"}


def types(events: list[dict[str, Any]]) -> list[str]:
    return [str(e["type"]) for e in events]


def test_unchanged_pr_gives_no_events_and_a_sane_report(
    ws: Workspace, ops: HostOps, rec: Rec
) -> None:
    """A comparison of two polls on an unchanged PR gives no events; the poll report names the
    host's backend, its snapshots and `requests_estimate` (7 per GitHub snapshot, 21 on GitLab)."""
    pr = ops.open_pr("quiet", with_ci=False)
    cursor = ws.watch(pr.ref)
    time.sleep(10)
    first = ws.poll_pr(pr.ref)
    second = ws.poll_pr(pr.ref)
    full = ws.pw_json("poll", "--full", "--no-hooks", schema="cli/poll")
    events, _ = ws.events(cursor)
    assert events == [], types(events)
    assert first["polled"] is True and second["polled"] is True
    host = full["hosts"][ops.cfg.host]
    assert host["backend"] == ops.kind and host["snapshots"] >= 1
    per = 7 if ops.kind == "github" else 21
    assert host["requests_estimate"] == host["snapshots"] * per
    assert full["failed"] == [] and full["prs"]["unchanged"] >= 1
    rec(host=ops.kind, events=0, snapshots=host["snapshots"], estimate=host["requests_estimate"])


def test_failed_whoami_without_cache_is_reported(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """A host whose credential is missing fails the poll with the real `auth_required` details
    (exit 12), reported in `failed[]`."""
    pr = ops.open_pr("noauth", with_ci=False)
    ws.watch(pr.ref)
    res = ws.prwatch("poll", "--full", "--no-hooks", tokens=False)
    assert res.rc == 12, (res.rc, res.out[:400], res.err[:400])
    report = res.json()
    assert_valid("cli/poll", report)
    assert any(
        f.get("code") == "auth_required" and f.get("ref") == "whoami" for f in report["failed"]
    ), report["failed"]
    rec(host=ops.kind, failed=report["failed"][:2])


def _comment(ops: HostOps, pr: Any, thread_file: str) -> None:
    ops.comment(pr.number, "live comment")


def _inline(ops: HostOps, pr: Any, thread_file: str) -> None:
    ops.inline_thread(pr.number, thread_file, "live inline comment")


def _push(ops: HostOps, pr: Any, thread_file: str) -> None:
    ops.add_commit(pr.branch)


def _ci(ops: HostOps, pr: Any, thread_file: str) -> None:
    if isinstance(ops, GitHubOps):
        ops.set_status(pr.head_sha, "success")
    else:
        assert isinstance(ops, GitLabOps)
        wait_until(lambda: ops.pipelines_done(pr.number), timeout=240, interval=5,
                   what="the pipeline finished")  # fmt: skip


Step = Callable[[HostOps, Any, str], None]
STEPS: dict[str, tuple[Step, Callable[[str], set[str]]]] = {
    "comment": (_comment, lambda kind: {"comment"}),
    # a GitHub inline comment is posted inside a review, which is an event of its own
    "inline": (_inline, lambda kind: {"review", "review_comment"} if kind == "github"
               else {"review_comment"}),
    "push": (_push, lambda kind: {"pushed"}),
    "convert_to_draft": (lambda o, p, f: o.set_draft(p.number, True),
                         lambda kind: {"converted_to_draft"}),
    "close": (lambda o, p, f: o.close(p.number), lambda kind: {"closed"}),
    "merge": (lambda o, p, f: o.merge(p.number), lambda kind: {"merged"}),
    "ci": (_ci, lambda kind: {"ci"}),
}  # fmt: skip


@pytest.mark.parametrize("step", sorted(STEPS))
def test_event_per_host_change(ws: Workspace, ops: HostOps, rec: Rec, step: str) -> None:
    """V5 and the GitHub shapes: one host change gives exactly its expected set of event types
    (the `ci` step: a commit status on GitHub, a finished pipeline on GitLab with
    PR_WATCH_LIVE_GITLAB_CI=1) and, polled again, nothing more."""
    do, expect = STEPS[step]
    want = expect(ops.kind)
    if step == "ci" and ops.kind == "gitlab" and not ops.cfg.ci:
        pytest.skip(
            "manual step: the GitLab ci event needs PR_WATCH_LIVE_GITLAB_CI=1; docs/live.md"
        )
    thread_file = ops.unique_path("thread.txt")
    pr = ops.open_pr(f"ev-{step}", files={thread_file: "one line\n"}, with_ci=step == "ci")
    cursor = ws.watch(pr.ref)
    do(ops, pr, thread_file)
    events, cursor, waited = ws.poll_for(pr.ref, cursor, want)
    got = set(types(events))
    assert got == want, f"expected exactly {sorted(want)}, got {types(events)}"
    assert all(e["pr_id"] == events[0]["pr_id"] for e in events)
    # a second poll with nothing new adds nothing (the event is reported once)
    ws.poll_pr(pr.ref)
    again, _ = ws.events(cursor)
    assert types(again) == [], types(again)
    ev = next(e for e in events if e["type"] in want)
    rec(host=ops.kind, step=step, types=",".join(types(events)), seconds=round(waited),
        fields=",".join(sorted(ev)))  # fmt: skip
    if step == "push":
        assert ev["from_sha"] == pr.head_sha and ev["to_sha"] != pr.head_sha
    if step == "merge":
        assert ev["merged_head_sha"] == pr.head_sha and ev["merge_commit_sha"]


def test_ready_after_draft(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """draft to ready fires `ready`; the readiness reading holds across a computing spell."""
    pr = ops.open_pr("ready", draft=True, with_ci=False)
    cursor = ws.watch(pr.ref)
    ops.set_draft(pr.number, False)
    events, _, waited = ws.poll_for(pr.ref, cursor, "ready")
    assert "ready" in types(events) and not (set(types(events)) & NEVER)
    rec(host=ops.kind, types=",".join(types(events)), seconds=round(waited))


def test_close_reopen_timing(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#34: a reopened PR shows `open` again in the snapshot and the listing, and fires
    `reopened`; the delays are recorded."""
    pr = ops.open_pr("reopen", with_ci=False)
    cursor = ws.watch(pr.ref)
    ops.close(pr.number)
    events, cursor, _ = ws.poll_for(pr.ref, cursor, "closed")
    ops.reopen(pr.number)
    start = time.monotonic()
    wait_until(
        lambda: ws.snapshot(pr.ref)["state"] == "open", timeout=60, interval=1, what="snapshot open"
    )
    snap_s = time.monotonic() - start
    wait_until(
        lambda: ws.list_item(ops.repo_id, pr.number) is not None,
        timeout=180,
        interval=2,
        what="listing shows it open",
    )
    list_s = time.monotonic() - start
    events, _, _ = ws.poll_for(pr.ref, cursor, "reopened")
    assert "reopened" in types(events)
    rec(
        host=ops.kind, snapshot_open_after_s=round(snap_s, 1), listing_open_after_s=round(list_s, 1)
    )


def test_retargeted_and_merged_base(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#83 item 6: `base.ref` changes promptly after a base edit and pr-watch fires `retargeted`;
    a stacked PR retargets itself when its base PR merges and its branch is deleted."""
    alt = ops.branch_name("alt-base")
    ops.push_branch(alt, ops.branch_files(), "live: alternative base")
    pr = ops.open_pr("retarget", with_ci=False)
    cursor = ws.watch(pr.ref)
    ops.retarget(pr.number, alt)
    start = time.monotonic()
    wait_until(lambda: ws.snapshot(pr.ref)["base"]["ref"] == alt, timeout=60, interval=1,
               what="base.ref follows the edit")  # fmt: skip
    prompt = time.monotonic() - start
    events, _, _ = ws.poll_for(pr.ref, cursor, "retargeted")
    assert "retargeted" in types(events)

    parent = ops.open_pr("stack-a", with_ci=False)
    child = ops.open_pr("stack-b", with_ci=False, start=parent.branch, base=parent.branch)
    assert ws.snapshot(child.ref)["base"]["ref"] == parent.branch
    ops.merge(parent.number, delete_branch=True)
    base_after, waited = wait_until(
        lambda: (r := ws.snapshot(child.ref)["base"]["ref"]) != parent.branch and r,
        timeout=120,
        interval=3,
        what=(
            "the child retargets when its base PR merges and the branch is deleted (UNVERIFIED "
            "assumption of this test, docs/live.md: a failure here may be the host's behavior, "
            "not pr-watch's)"
        ),
    )
    merged_base = ws.snapshot(parent.ref)["base"]["ref"]
    rec(host=ops.kind, base_edit_visible_s=round(prompt, 1), auto_retarget_s=round(waited),
        child_base=base_after, merged_pr_base=merged_base)  # fmt: skip
    assert merged_base == ops.cfg.base_branch


def test_force_push_detection(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """`compare` says `ahead` for a normal push and `diverged` for a force push, and with
    `detect_force_push` pr-watch stores `forced` false and true accordingly."""
    pr = ops.open_pr("force", with_ci=False)
    cursor = ws.watch(pr.ref, extra_env=FORCE_ENV)
    second = ops.add_commit(pr.branch)
    wait_until(lambda: ws.snapshot(pr.ref)["head"]["sha"] == second, timeout=90,
               what="normal push visible")  # fmt: skip
    ahead = ws.fg_json("compare", pr.ref, f"--from={pr.head_sha}", f"--to={second}", "--no-diff")
    events, cursor, _ = ws.poll_for(pr.ref, cursor, "pushed", extra_env=FORCE_ENV)
    normal = next(e for e in events if e["type"] == "pushed")
    old, new = ops.force_rewrite(pr.branch)
    assert old == second
    wait_until(lambda: ws.snapshot(pr.ref)["head"]["sha"] == new, timeout=90,
               what="force push visible")  # fmt: skip
    diverged = ws.fg_json("compare", pr.ref, f"--from={old}", f"--to={new}", "--no-diff")
    events, _, _ = ws.poll_for(pr.ref, cursor, "pushed", extra_env=FORCE_ENV)
    forced = next(e for e in events if e["type"] == "pushed")
    rec(host=ops.kind, normal_status=ahead["status"], forced_status=diverged["status"],
        normal_forced=normal.get("forced"), force_forced=forced.get("forced"))  # fmt: skip
    assert ahead["status"] == "ahead" and diverged["status"] == "diverged"
    assert normal["forced"] is False and forced["forced"] is True


def test_review_requests_approvals_and_dismissals(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#35 and #36 shapes with a second account: a request is outstanding in the snapshot and
    listed by `--review-requested-from`; an approval fires `approved`; its dismissal (GitHub) or
    unapproval (GitLab) fires `approval_withdrawn`; the shapes are recorded."""
    token2 = ops.need_token2()
    login2, id2 = ops.user(token2)
    pr = ops.open_pr("reviews", with_ci=False)
    cursor = ws.watch(pr.ref)
    shapes: dict[str, Any] = {}

    before = ws.settled_stamps(ops.repo_id, pr.ref, pr.number)[0]
    ops.request_review(pr.number, login2, id2)
    events, cursor, _ = ws.poll_for(pr.ref, cursor, "review_requested")
    snap = ws.snapshot(pr.ref)
    mine = [r for r in snap["review_requests"] if r["reviewer"]["login"].lower() == login2.lower()]
    assert mine and mine[0]["state"] == "outstanding", snap["review_requests"]
    shapes["request"] = {k: mine[0]["reviewer"].get(k) for k in ("kind", "id")}
    shapes["teams_visible"] = snap["teams_visible"]
    shapes["request_incomplete"] = "review_requests" in snap["incomplete"]
    after = ws.settled_stamps(ops.repo_id, pr.ref, pr.number)[0]
    shapes["request_bumps_listing"] = before != after
    wait_until(
        lambda: any(
            i["number"] == pr.number
            for i in ws.fg_json(
                "prs", f"--repo={ops.repo_id}", f"--review-requested-from={login2}"
            )["items"]  # fmt: skip
        ),
        timeout=180,
        interval=4,
        what="--review-requested-from lists the PR",
    )

    ops.approve(pr.number)
    events, cursor, _ = ws.poll_for(pr.ref, cursor, "approved")
    approved = ws.snapshot(pr.ref)
    entry = [e for e in approved["latest_by_reviewer"] if e["login"].lower() == login2.lower()]
    assert entry and entry[0]["state"] == "approved", approved["latest_by_reviewer"]
    shapes["approved_entry"] = {k: entry[0].get(k) for k in ("at", "commit_sha", "can_push")}
    shapes["approvals_at_head"] = approved.get("approvals_at_head")
    shapes["request_after_approval"] = [r["state"] for r in approved["review_requests"]]

    ops.withdraw_approval(pr.number)
    events, cursor, _ = ws.poll_for(pr.ref, cursor, "approval_withdrawn")
    withdrawn = ws.snapshot(pr.ref)
    left = [
        e["state"] for e in withdrawn["latest_by_reviewer"] if e["login"].lower() == login2.lower()
    ]
    shapes["after_withdrawal_entry"] = left or "vanished"
    withdrawal = next(e for e in events if e["type"] == "approval_withdrawn")
    shapes["withdrawn_fields"] = ",".join(sorted(withdrawal))

    try:
        ops.request_changes(pr.number)
    except HostError as exc:
        shapes["changes_requested"] = f"not available: {str(exc)[:80]}"
    else:
        try:
            events, cursor, _ = ws.poll_for(pr.ref, cursor, "changes_requested")
        except AssertionError:
            shapes["changes_requested"] = "not observed"
        else:
            again = ws.snapshot(pr.ref)
            shapes["changes_requested_entry"] = [
                (e["state"], e.get("at"))
                for e in again["latest_by_reviewer"]
                if e["login"].lower() == login2.lower()  # fmt: skip
            ]

    ops.add_commit(pr.branch)
    wait_until(lambda: ws.snapshot(pr.ref)["head"]["sha"] != pr.head_sha, timeout=90, what="push")
    shapes["after_push_decision"] = ws.snapshot(pr.ref).get("review_decision")
    rec(host=ops.kind, **{k: json.dumps(v, default=str) for k, v in shapes.items()})


def test_hook_runs_for_a_real_event(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """A hook runs after a real comment event; `log.jsonl` records `hook_run` with exit 0."""
    pr = ops.open_pr("hook", with_ci=False)
    ws.watch(pr.ref)
    marker = ws.root / "hook-ran"
    ops.comment(pr.number, "live hook comment")

    def ran() -> bool:
        ws.pw_json("poll", "--pr", pr.ref, "--hook", f"on-comment=touch {marker}")
        return marker.exists()

    wait_until(ran, timeout=120, interval=5, what="the hook ran")
    log = ws.state_home / "pr-watch" / "log.jsonl"
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    runs = [r for r in rows if r.get("event") == "hook_run" and r.get("hook") == "on-comment"]
    assert runs and runs[-1]["exit_code"] == 0 and runs[-1]["timed_out"] is False, runs
    rec(host=ops.kind, hook_exit=runs[-1]["exit_code"], duration_ms=runs[-1]["duration_ms"])
