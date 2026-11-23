"""Repository renames, readiness codes, listing filters and the over-cap fallback."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.live.support import HostOps, Workspace, wait_until

pytestmark = pytest.mark.live

Rec = Callable[..., None]


def codes(snapshot: dict[str, Any]) -> list[str]:
    return sorted(str(b["code"]) for b in snapshot["blocked_by"])


def test_repo_rename_redirect_and_case(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#83 items 1, 2, 4 and 8: after a rename the old reference resolves through the host
    redirect to the new canonical `pr`/`repo` with the same `pr_id`; `--author me` shows the new
    name; pr-watch fires `renamed` once; a case-only rename keeps the `pr_id` and changes the
    case. The repository is renamed back in teardown (verify it after a crash, docs/live.md)."""
    new_name = ops.cfg.rename_to
    if not new_name:
        pytest.skip(
            f"manual step: set PR_WATCH_LIVE_{ops.kind.upper()}_RENAME_TO (needs admin on the "
            "throwaway repository; docs/live.md, Renames)"
        )
    pr = ops.open_pr("rename", with_ci=False)
    cursor = ws.watch(pr.ref)
    before = ws.snapshot(pr.ref)
    old_repo = ops.repo_id
    ops.rename_repo(new_name)
    new_repo = ops.repo_id
    new_ref = ops.ref(pr.number)

    def resolved() -> dict[str, Any] | None:
        res = ws.forgectl("snapshot", pr.ref, "--no-bodies", "--json")
        return res.json() if res.rc == 0 else None

    snap, waited = wait_until(resolved, timeout=120, interval=4, what="old name resolves")
    assert snap["pr_id"] == before["pr_id"] and snap["number"] == before["number"]
    assert snap["pr"].lower() == new_ref.lower() and snap["repo"].lower() == new_repo.lower()
    show = ws.fg_json("show", pr.ref)
    assert show["pr_id"] == before["pr_id"] and show["pr"].lower() == new_ref.lower()
    cmp = ws.fg_json("compare", pr.ref, f"--from={pr.head_sha}", f"--to={pr.head_sha}", "--no-diff")
    assert cmp["status"] == "identical" and cmp["pr"].lower() == new_ref.lower()
    old_listing = ws.forgectl("prs", f"--repo={old_repo}", "--json")
    old_items = old_listing.json().get("items") if old_listing.rc == 0 else None
    wait_until(
        lambda: any(
            i["number"] == pr.number and i["repo"].lower() == new_repo.lower()
            for i in ws.fg_json("prs", f"--host={ops.cfg.host}", "--author=me", "--limit=200")[
                "items"
            ]
        ),
        timeout=180,
        interval=5,
        what="--author me shows the new name",
    )
    events, cursor, _ = ws.poll_for(pr.ref, cursor, "renamed")
    assert [e["type"] for e in events].count("renamed") == 1

    upper = new_name.upper() if new_name.upper() != new_name else new_name.lower()
    ops.rename_repo(upper)
    cased, _ = wait_until(
        lambda: (lambda s: s if s and s["repo"].endswith(upper) else None)(resolved()),
        timeout=120,
        interval=4,
        what="case-only rename visible",
    )
    assert cased["pr_id"] == before["pr_id"]
    rec(
        host=ops.kind,
        rename_visible_s=round(waited),
        old_listing=f"rc={old_listing.rc} items={None if old_items is None else len(old_items)}",
        case_only_pr=cased["pr"],
    )


def test_mergeability_codes(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#38: `draft` and `conflicts` blockers appear in `blocked_by`; pr-watch fires
    `mergeability` when a sibling merge creates a conflict; the number of snapshots that still
    read `mergeability_unknown` is recorded, also right after a reopen."""
    path = ops.unique_path("conflict.txt")
    a = ops.open_pr("conflict-a", files={path: "from a\n"}, with_ci=False)
    b = ops.open_pr("conflict-b", files={path: "from b\n"}, with_ci=False)
    d = ops.open_pr("draft", draft=True, with_ci=False)
    assert "draft" in codes(ws.snapshot(d.ref))
    unknown = 0
    for _ in range(5):
        unknown += "mergeability_unknown" in codes(ws.snapshot(b.ref))
        time.sleep(2)
    cursor = ws.watch(b.ref)
    ops.merge(a.number)
    events, _, waited = ws.poll_for(b.ref, cursor, "mergeability", timeout=240)
    assert "conflicts" in codes(ws.snapshot(b.ref))
    ops.close(d.number)
    ops.reopen(d.number)
    after_reopen = codes(ws.snapshot(d.ref))
    rec(host=ops.kind, unknown_of_5=unknown, conflict_event_s=round(waited),
        reopen_codes=",".join(after_reopen))  # fmt: skip


def test_listing_filters(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """`prs`: `--state` closed means closed unmerged, merged and all behave, `--draft` filters,
    `--updated-since` takes RFC 3339, `--limit` cuts with `complete: false`, case of `pr`."""
    open_pr = ops.open_pr("list-open", with_ci=False)
    draft = ops.open_pr("list-draft", draft=True, with_ci=False)
    closed = ops.open_pr("list-closed", with_ci=False)
    ops.close(closed.number)
    merged = ops.open_pr("list-merged", with_ci=False)
    ops.merge(merged.number)
    repo = f"--repo={ops.repo_id}"

    def numbers(*flags: str) -> set[int]:
        return {i["number"] for i in ws.fg_json("prs", repo, "--limit=200", *flags)["items"]}

    def settled() -> bool:
        return {open_pr.number, draft.number} <= numbers("--state=open") and merged.number in (
            numbers("--state=merged")
        )

    wait_until(settled, timeout=240, interval=5, what="listing shows the new states")
    assert closed.number in numbers("--state=closed")
    assert merged.number not in numbers("--state=closed")
    everything = numbers("--state=all")
    assert {open_pr.number, draft.number, closed.number, merged.number} <= everything
    assert draft.number in numbers("--state=open", "--draft")
    assert draft.number not in numbers("--state=open", "--no-draft")
    assert open_pr.number in numbers("--state=open", "--no-draft")
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert numbers("--state=all", f"--updated-since={future}") == set()
    recent = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert open_pr.number in numbers("--state=all", f"--updated-since={recent}")
    cut = ws.fg_json("prs", repo, "--state=all", "--limit=1")
    assert len(cut["items"]) == 1 and cut["complete"] is False
    item = ws.wait_listed(ops.repo_id, open_pr.number)
    assert item["pr"].lower() == open_pr.ref.lower()
    rec(host=ops.kind, pr_exact_case=item["pr"] == open_pr.ref, repo_as_listed=item["repo"])


def test_over_cap_snapshot_falls_back(ws: Workspace, live_cfg: Any, rec: Rec) -> None:
    """#24 (needs a huge PR): a snapshot over `forgectl.max_output_mib` is killed at the cap and
    pr-watch falls back to `show` instead of failing the PR. Read-only on the named PR.
    Exit 0 is required. A merged or closed huge PR commits its terminal event (no failure, one
    snapshot); an open one is the documented counted `too_large` failure (the `show` saw it
    open). Anything else (`internal`, `contract`, a second failure) fails."""
    if not live_cfg.huge_pr:
        pytest.skip(
            "manual step: set PR_WATCH_LIVE_HUGE_PR to a PR (host/path#N) whose snapshot exceeds "
            "1 MiB; docs/live.md"
        )
    huge_host = live_cfg.huge_pr.partition("/")[0]
    assert any(c.host == huge_host for c in live_cfg.hosts.values()), "huge PR host not configured"
    env = {"PR_WATCH_FORGECTL__MAX_OUTPUT_MIB": "1"}
    added = ws.pw_json("watch", "add", live_cfg.huge_pr, "--owner", "live", "--no-baseline",
                       schema="cli/watch-add", extra_env=env)  # fmt: skip
    assert added["pr"] is not None
    report = ws.pw_json("poll", "--pr", live_cfg.huge_pr, "--no-hooks", schema="cli/poll",
                        extra_env=env)  # fmt: skip
    codes = [f.get("code") for f in report["failed"]]
    committed_terminal = codes == [] and report["prs"]["snapshotted"] >= 1
    counted_too_large = codes == ["too_large"]
    assert committed_terminal or counted_too_large, (codes, report["prs"])
    rec(failed=codes, polled=report.get("polled"), snapshotted=report["prs"]["snapshotted"],
        fallback="terminal_committed" if committed_terminal else "too_large_recorded")  # fmt: skip
