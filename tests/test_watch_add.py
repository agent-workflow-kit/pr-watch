"""`watch add`, through main(), against the fake forgectl (tests/fakebin/forgectl)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import forgectl_client as fc
from pr_watch import prune as prune_mod
from pr_watch import watch_add as wa
from pr_watch import watches as w
from pr_watch.cli import main
from pr_watch.refs import PrRef, parse_label, parse_pr
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, format_ts, open_store
from tests.forgectl_support import FakeForgectl, new_pr, sha
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "watch_add"
NOW = datetime.now(UTC)
A, B = "human:a", "agent:b"
PR = "github.com/example-org/widgets#7"
LONG_AGO = format_ts(NOW - timedelta(days=30))
RECENT = format_ts(NOW - timedelta(days=1))


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def add(
    capsys: pytest.CaptureFixture[str], *argv: str, owner: str = A
) -> tuple[int, dict[str, Any]]:
    code, out, _ = run(capsys, "--json", "watch", "add", *argv, "--owner", owner)
    return code, json.loads(out)


def ok(capsys: pytest.CaptureFixture[str], *argv: str, owner: str = A) -> dict[str, Any]:
    code, doc = add(capsys, *argv, owner=owner)
    assert code == 0, doc
    assert_valid("cli/watch-add", doc)
    return doc


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    fake_forgectl.host("github.com")
    fake_forgectl.add(new_pr(number=7))
    return fake_forgectl


def cmds(fake: FakeForgectl) -> list[str | None]:
    """Commands called, the `--version` checks (command None) left out."""
    return [c.command for c in fake.calls() if c.command is not None]


def counts(store: Store) -> dict[str, int]:
    return {
        t: int(store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # noqa: S608
        for t in ("watches", "owners", "labels", "sources", "prs", "events")
    }


def row(store: Store, pr_id: str | None = None) -> Any:
    sql = "SELECT * FROM prs" + (" WHERE pr_id = ?" if pr_id else "")
    store.conn.row_factory = __import__("sqlite3").Row
    try:
        return store.conn.execute(sql, (pr_id,) if pr_id else ()).fetchone()
    finally:
        store.conn.row_factory = None


def pr_id_of(fake: FakeForgectl, pr: str = PR) -> str:
    return str(fake.scenario["prs"][pr]["snapshot"]["pr_id"])


def seed_row(
    store: Store,
    pr_id: str,
    pr: str = PR,
    *,
    digest: str | None = "{}",
    status: str = "active",
    terminal_at: str | None = None,
    owners: tuple[str, ...] = (A,),
) -> str:
    kind = "merged" if status == "terminal" else None
    with store.transaction() as c:
        c.execute(
            "INSERT INTO prs (pr_id, pr, repo, host, digest, status, terminal_at, terminal_kind)"
            " VALUES (?, ?, ?, 'github.com', ?, ?, ?, ?)",
            (pr_id, pr, pr.split("#")[0], digest, status, terminal_at, kind),
        )
        wid, _ = w.ensure_watch(c, "pr", pr_id, NOW)
        w.add_source(c, pr_id, wid)
        for o in owners:
            w.add_owner(c, wid, o)
    return wid


def event_types(store: Store) -> list[str]:
    return [str(r[0]) for r in store.conn.execute("SELECT type FROM events ORDER BY seq")]


# --- per-PR form: baseline ----------------------------------------------------------------------


def test_new_open_pr_is_baselined_with_one_snapshot(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = ok(capsys, PR)
    assert (doc["created"], doc["changed"], doc["baselined"], doc["events_emitted"]) == (
        True, True, True, 0,
    )  # fmt: skip
    assert doc["owner"] == A and doc["watch"]["kind"] == "pr" and doc["watch"]["owners"] == [A]
    assert (
        doc["pr"]["pr"] == PR and doc["pr"]["state"] == "open" and doc["pr"]["status"] == "active"
    )
    assert doc["pr"]["baselined_at"] is not None and doc["pr"]["sources"] == [doc["watch"]["id"]]
    assert cmds(fake) == ["snapshot"]  # the version check is not a command
    assert row(store)["digest"] is not None and row(store)["rev"] == 1
    assert counts(store)["events"] == 0


def test_baseline_on_an_unsettled_pr_sets_unsettled_since_and_snap_updated_at(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = new_pr(number=7, ci={"state": "pending"}, updated_at="2026-10-02T10:30:00Z")
    fake.add(snap)
    ok(capsys, PR)
    r = row(store)
    assert r["unsettled_since"] is not None  # the TODO(#22) of #18: set on the baseline
    assert r["snap_updated_at"] == "2026-10-02T10:30:00.000000Z"


def test_baseline_on_a_settled_pr_leaves_unsettled_since_null(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.add(new_pr(number=7, ci={"state": "passing"}))
    ok(capsys, PR)
    assert row(store)["unsettled_since"] is None


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_new_terminal_pr_stores_its_one_event_after_the_cursor(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    snap = new_pr(number=7, state=state)
    if state == "merged":
        snap.update(merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    else:
        snap.update(closed_at="2026-01-01T00:00:00Z")
    fake.add(snap)
    before = store.head_seq()
    doc = ok(capsys, PR)
    assert (doc["baselined"], doc["events_emitted"]) == (True, 1)
    assert event_types(store) == [state]
    assert doc["pr"]["status"] == "terminal" and doc["pr"]["terminal_kind"] == state
    assert doc["pr"]["terminal_at"] is not None
    # the cursor was read before the event: events after it include the one just stored
    assert store.head_seq() == before + 1
    assert doc["cursor"].endswith(f".{before}")  # read before the event was stored


def test_no_baseline_calls_show_and_stores_a_row_without_digest(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = ok(capsys, PR, "--no-baseline")
    assert (doc["created"], doc["baselined"], doc["events_emitted"]) == (True, False, 0)
    assert cmds(fake) == ["show"]
    r = row(store)
    assert r["digest"] is None and r["rebaseline"] == 0 and r["rev"] == 0
    p = doc["pr"]
    assert (p["state"], p["draft"], p["ci_state"], p["head_sha"], p["last_polled_at"]) == (
        None, None, None, None, None,
    )  # fmt: skip
    assert p["baselined"] is None and p["pr"] == PR


def test_no_baseline_of_a_merged_pr_emits_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    doc = ok(capsys, PR, "--no-baseline")
    assert doc["events_emitted"] == 0 and counts(store)["events"] == 0
    assert doc["pr"]["status"] == "active"  # the first poll baselines and stores the event


def test_second_owner_on_a_digestless_row_baselines(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR, "--no-baseline")
    doc = ok(capsys, PR, owner=B)
    assert (doc["created"], doc["changed"], doc["baselined"]) == (False, True, True)
    assert doc["watch"]["owners"] == [B, A]
    assert cmds(fake) == ["show", "snapshot"]
    assert row(store)["digest"] is not None


def test_no_baseline_again_on_a_digestless_row_contacts_nobody(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR, "--no-baseline")
    doc = ok(capsys, PR, "--no-baseline", owner=B)
    assert (doc["baselined"], doc["changed"]) == (False, True)
    assert cmds(fake) == ["show"]


def test_second_owner_on_a_row_with_a_digest_does_not_rebaseline(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR)
    stored = (row(store)["digest"], row(store)["rev"])
    fake.update(PR, head={"sha": sha(5)})  # a push the next poll, not watch add, reports
    doc = ok(capsys, PR, owner=B)
    assert (doc["created"], doc["changed"], doc["baselined"], doc["events_emitted"]) == (
        False, True, False, 0,
    )  # fmt: skip
    assert len(fake.calls("snapshot")) == 1  # none for the second add
    assert (row(store)["digest"], row(store)["rev"]) == stored
    assert counts(store)["events"] == 0 and doc["watch"]["owners"] == [B, A]


def test_same_owner_again_is_unchanged(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    first = ok(capsys, PR, "--label", "k=v")
    before = counts(store)
    doc = ok(capsys, PR, "--label", "k=v")
    assert (doc["created"], doc["changed"], doc["baselined"]) == (False, False, False)
    assert doc["watch"]["id"] == first["watch"]["id"] and counts(store) == before


def test_member_of_a_query_watch_gets_a_per_pr_source_without_contact(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_id = pr_id_of(fake)
    with store.transaction() as c:
        c.execute(
            "INSERT INTO prs (pr_id, pr, repo, host, digest) VALUES (?, ?, ?, 'github.com', '{}')",
            (pr_id, PR, PR.split("#")[0]),
        )
        qid, _ = w.ensure_watch(c, "query", "mine+open", NOW)
        w.add_owner(c, qid, B)
        w.add_source(c, pr_id, qid)
    doc = ok(capsys, PR)
    assert (doc["created"], doc["baselined"]) == (True, False)
    assert not cmds(fake) and sorted(doc["pr"]["sources"]) == sorted([qid, doc["watch"]["id"]])


def test_url_reference_matches_the_stored_row(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR)
    doc = ok(capsys, "https://github.com/example-org/widgets/pull/7/files", owner=B)
    assert doc["baselined"] is False and len(fake.calls("snapshot")) == 1


# --- expiry -----


def test_expired_unpruned_terminal_row_is_treated_as_absent(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    pr_id = pr_id_of(fake)
    old = seed_row(store, pr_id, status="terminal", terminal_at=LONG_AGO, owners=("old:owner",))
    with store.transaction() as c:
        w.add_labels(c, old, "old:owner", [])
    doc = ok(capsys, PR)
    assert doc["created"] is True and doc["baselined"] is True and doc["events_emitted"] == 1
    assert doc["watch"]["id"] != old and doc["watch"]["owners"] == [A]  # the old owner went too
    assert w.watch_exists(store.conn, old) is False
    assert doc["pr"]["status"] == "terminal" and doc["pr"]["terminal_at"] > LONG_AGO
    assert event_types(store) == ["merged"]
    assert counts(store)["prs"] == 1


def test_terminal_row_inside_expiry_is_kept_and_emits_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    seed_row(store, pr_id_of(fake), status="terminal", terminal_at=RECENT)
    doc = ok(capsys, PR, owner=B)
    assert (doc["baselined"], doc["events_emitted"], doc["created"]) == (False, 0, False)
    assert doc["pr"]["status"] == "terminal" and doc["pr"]["terminal_at"] == RECENT
    assert doc["pr"]["terminal_kind"] == "merged" and not cmds(fake)


def test_expired_row_of_a_pr_that_is_open_again_is_baselined_as_a_new_active_row(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_row(store, pr_id_of(fake), status="terminal", terminal_at=LONG_AGO)  # forge: open now
    doc = ok(capsys, PR)
    assert (doc["baselined"], doc["events_emitted"], doc["pr"]["status"]) == (True, 0, "active")
    assert doc["pr"]["terminal_at"] is None


def test_a_re_added_expired_row_survives_the_next_prune(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    seed_row(store, pr_id_of(fake), status="terminal", terminal_at=LONG_AGO, owners=("old:owner",))
    watch_id = str(ok(capsys, PR)["watch"]["id"])  # refreshes the expired row: terminal_at is now
    result = prune_mod.prune(store, datetime.now(UTC), "30d", "7d")
    assert result.prs_deleted == 0 and result.watches_ended == 0
    assert counts(store)["prs"] == 1
    assert w.watch_exists(store.conn, watch_id)


def test_prune_with_a_stale_expired_list_keeps_a_row_refreshed_since(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id list is read before the drop; a `watch add` that refreshed the row in between (it
    cannot run inside prune's own write transaction, so it is simulated by a stale list) is not
    deleted: the predicate is re-evaluated per row at the drop."""
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    pr_id = pr_id_of(fake)
    seed_row(store, pr_id, status="terminal", terminal_at=LONG_AGO)
    ok(capsys, PR)  # refreshed
    monkeypatch.setattr(w, "expired_pr_ids", lambda _conn, _cutoff: [pr_id])  # stale listing
    result = prune_mod.prune(store, datetime.now(UTC), "30d", "7d")
    assert result.prs_deleted == 0
    assert counts(store)["prs"] == 1


# --- failures record nothing -----


def test_invalid_reference_exits_2_and_records_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = add(capsys, "not a pr")
    assert code == 2 and doc["error"]["code"] == "invalid_pr"
    assert counts(store) == dict.fromkeys(counts(store), 0) and not cmds(fake)


FAILURES: list[tuple[str, Any, int, str]] = [
    ("not_found", lambda f: f.not_found("snapshot"), 10, "not_found"),
    ("network", lambda f: f.network_error("snapshot"), 12, "host_failed"),
    ("rate_limited", lambda f: f.rate_limited("snapshot", retry_after=5), 12, "host_failed"),
    ("auth_required", lambda f: f.auth_required("snapshot"), 12, "host_failed"),
    ("permission_denied", lambda f: f.permission_denied("snapshot"), 12, "host_failed"),
    ("garbage", lambda f: f.garbage("snapshot"), 12, "contract"),
    ("missing_field", lambda f: f.contract_violation("snapshot", ["pr_id"]), 12, "contract"),
    ("killed", lambda f: f.die_by_signal("snapshot"), 12, "host_failed"),
    ("old", lambda f: f.version("forgectl 0.1.0"), 12, "tool_too_old"),
    ("unparseable_version", lambda f: f.version("hello"), 12, "tool_too_old"),
]


@pytest.mark.parametrize(
    ("name", "setup", "exit_code", "code"), FAILURES, ids=lambda v: str(v)[:20]
)
def test_forgectl_failures_map_to_the_envelope_and_record_nothing(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    name: str,
    setup: Any,
    exit_code: int,
    code: str,
) -> None:
    setup(fake)
    got, doc = add(capsys, PR)
    assert (got, doc["error"]["code"]) == (exit_code, code)
    assert_valid("cli/error", doc)
    assert counts(store) == dict.fromkeys(counts(store), 0)


def test_unknown_host_is_refused_and_records_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.fail("snapshot", "refused", reason="unknown_host")
    got, doc = add(capsys, PR)
    assert (got, doc["error"]["code"]) == (20, "refused")
    assert counts(store) == dict.fromkeys(counts(store), 0)


def test_failure_on_a_second_owner_changes_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR, "--no-baseline")
    before = counts(store)
    fake.network_error("snapshot")
    got, _ = add(capsys, PR, owner=B)
    assert got == 12 and counts(store) == before


def test_no_baseline_failure_records_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.network_error("show")
    got, _ = add(capsys, PR, "--no-baseline")
    assert got == 12 and counts(store) == dict.fromkeys(counts(store), 0)


# --- hostile forgectl output -----


@pytest.mark.parametrize(
    "changes",
    [
        {"pr": "other.example/o/r#7"},  # another host
        {"pr": "github.com/example-org/widgets#8"},  # another number
        {"pr": "github.com/example-org/widgets#7", "repo": "github.com/example-org/other"},
    ],
    ids=["host", "number", "repo_mismatch"],
)
def test_a_snapshot_that_does_not_match_the_reference_is_a_contract_failure(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, Any],
) -> None:
    snap = new_pr(number=7)
    snap.update(changes)
    fake.scenario["prs"][PR]["snapshot"] = snap  # bypass fake validation of a bad shape
    fake._write()
    got, doc = add(capsys, PR)
    assert got == 12 and doc["error"]["code"] in ("contract", "host_failed")
    assert counts(store) == dict.fromkeys(counts(store), 0)


def test_renamed_repo_uses_the_snapshots_canonical_pr(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = new_pr(number=7)
    renamed = "github.com/example-org/gadgets"
    snap.update(pr=f"{renamed}#7", repo=renamed)
    fake.scenario["prs"][PR]["snapshot"] = snap
    fake._write()
    doc = ok(capsys, PR)
    assert doc["pr"]["pr"] == f"{renamed}#7" and doc["pr"]["pr_id"] == snap["pr_id"]


def test_reused_name_takes_the_pr_id_from_the_snapshot(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_row(store, "OLD1", digest=None, owners=(B,))  # same name, another PR
    seed_row(store, "OLD2", digest=None, owners=(B,))  # two rows: the name is ambiguous
    doc = ok(capsys, PR)
    assert doc["pr"]["pr_id"] == pr_id_of(fake) and doc["baselined"] is True
    assert counts(store)["prs"] == 3
    assert row(store, "OLD1")["digest"] is None  # untouched


def test_row_found_by_name_with_another_pr_id_is_not_rebaselined(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_row(store, "OLD1", digest=None, owners=(B,))
    doc = ok(capsys, PR)
    assert doc["pr"]["pr_id"] == pr_id_of(fake)
    assert row(store, "OLD1")["digest"] is None and row(store, "OLD1")["rev"] == 0


# --- owners and labels -----


def test_labels_are_owned_per_pair_and_unioned(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR, "--label", "a=1", "--label", "b=2")
    doc = ok(capsys, PR, "--label", "b=2", "--label", "c=3", owner=B)
    assert doc["watch"]["labels"] == ["a=1", "b=2", "c=3"]
    assert w.labels_of(store.conn, doc["watch"]["id"], B) == ["b=2", "c=3"]


def test_labels_reach_the_stored_terminal_event(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    doc = ok(capsys, PR, "--label", "k=v")
    stored = store.conn.execute("SELECT labels, payload FROM events").fetchone()
    assert json.loads(stored[0]) == ["k=v"]
    assert json.loads(stored[1])["watches"] == [doc["watch"]["id"]]


def _labels(n: int) -> list[str]:
    args: list[str] = []
    for i in range(n):
        args += ["--label", f"k=v{i}"]
    return args


def test_label_cap_is_refused_before_anything_is_recorded(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    got, doc = add(capsys, PR, *_labels(w.MAX_LABELS_PER_WATCH + 1))
    assert (got, doc["error"]["code"], doc["error"]["details"]["kind"]) == (
        2, "usage", "too_many_labels",
    )  # fmt: skip
    assert counts(store) == dict.fromkeys(counts(store), 0)


def test_label_cap_counts_the_union_with_the_existing_watch(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR, *_labels(w.MAX_LABELS_PER_WATCH))
    before = counts(store)
    got, doc = add(capsys, PR, "--label", "extra=1", owner=B)
    assert got == 2 and doc["error"]["details"]["kind"] == "too_many_labels"
    assert counts(store) == before
    assert ok(capsys, PR, "--label", "k=v0", owner=B)["changed"] is True  # already in the union


def test_exactly_the_label_cap_is_accepted(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = ok(capsys, PR, *_labels(w.MAX_LABELS_PER_WATCH))
    assert len(doc["watch"]["labels"]) == w.MAX_LABELS_PER_WATCH


# --- query form -----


def test_query_form_records_only_and_never_contacts_forgectl(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = ok(capsys, "--mine", "--open", "--host", "GitHub.com", "--repo", "github.com/O/R")
    assert doc["watch"]["kind"] == "query"
    assert doc["watch"]["key"] == "mine+open;host=github.com;repo=github.com/o/r"
    assert (doc["created"], doc["changed"], doc["baselined"], doc["events_emitted"]) == (
        True, True, False, 0,
    )  # fmt: skip
    assert doc["pr"] is None and not cmds(fake)
    assert counts(store)["prs"] == 0
    again = ok(capsys, "--mine", "--open", "--repo", "github.com/o/r", "--host", "github.com")
    assert (again["created"], again["changed"]) == (False, False)
    assert again["watch"]["id"] == doc["watch"]["id"]
    second = ok(
        capsys, "--mine", "--open", "--host", "github.com", "--repo", "github.com/o/r", owner=B
    )
    assert (second["created"], second["changed"], second["watch"]["owners"]) == (
        False,
        True,
        [B, A],
    )


def test_query_form_unfiltered_with_labels(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = ok(capsys, "--mine", "--open", "--label", "k=v")
    assert doc["watch"]["key"] == "mine+open" and doc["watch"]["labels"] == ["k=v"]
    assert doc["watch"]["members"] == 0


def test_query_form_label_cap(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    got, doc = add(capsys, "--mine", "--open", *_labels(w.MAX_LABELS_PER_WATCH + 1))
    assert got == 2 and doc["error"]["details"]["kind"] == "too_many_labels"
    assert counts(store) == dict.fromkeys(counts(store), 0)


def test_query_cursor_is_the_event_head(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    first = ok(capsys, "--mine", "--open")
    second = ok(capsys, "--mine", "--open", owner=B)
    assert first["cursor"] == second["cursor"]


def test_host_and_repo_on_the_per_pr_form_are_a_usage_error(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    got, doc = add(capsys, PR, "--host", "github.com")
    assert got == 2 and doc["error"]["details"]["kind"] == "bad_combination"
    assert not cmds(fake)


# --- compare-and-set against a concurrent baseline -----


def test_a_baseline_committed_between_fetch_and_write_wins_and_the_snapshot_is_discarded(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    pr_id = pr_id_of(fake)
    real = fc.ForgectlClient.snapshot

    def racing(self: fc.ForgectlClient, ref: PrRef) -> fc.Outcome:
        outcome = real(self, ref)
        # a poll that found the PR first: row, source (a query member) and a committed digest
        with open_state_dir(Path(os.environ["HOME"]) / ".local" / "state" / "pr-watch") as st:
            with open_store(st) as other, other.transaction() as c:
                snap = outcome.result or {}
                c.execute(
                    "INSERT INTO prs (pr_id, pr, repo, host, digest, rev) VALUES "
                    "(?, ?, ?, 'github.com', '{\"state\":\"open\"}', 1)",
                    (pr_id, snap["pr"], snap["repo"]),
                )
                qid, _ = w.ensure_watch(c, "query", "mine+open", NOW)
                w.add_source(c, pr_id, qid)
        return outcome

    monkeypatch.setattr(fc.ForgectlClient, "snapshot", racing)
    doc = ok(capsys, PR)
    assert (doc["baselined"], doc["events_emitted"], doc["created"]) == (False, 0, True)
    assert row(store)["digest"] == '{"state":"open"}' and row(store)["rev"] == 1
    assert counts(store)["events"] == 0


def test_concurrent_adds_for_one_new_pr_store_one_baseline_in_real_processes(
    fake: FakeForgectl, store: Store, tmp_path: Path
) -> None:
    """Two concurrent `watch add` processes (the poll is a stub here; the add + poll variant is
    todo(#25)). The long `slow` makes both fetch before either writes; the assertions are on the
    outcome only, so a slow machine cannot make the test flaky."""
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    fake.slow("snapshot", 5.0)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "pr_watch", "--json", "watch", "add", PR, "--owner", owner],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        for owner in (A, B)
    ]
    docs = []
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            docs.append(json.loads(out))
    finally:
        for proc in procs:  # only the processes started here
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    for doc in docs:
        assert_valid("cli/watch-add", doc)
    assert sorted(d["baselined"] for d in docs) == [False, True]
    assert sum(d["events_emitted"] for d in docs) == 1
    assert event_types(store) == ["merged"]
    assert counts(store)["prs"] == 1 and row(store)["rev"] == 1


# --- write-time clock (R1) -----


def test_stamps_and_expiry_use_the_time_of_the_write_not_of_the_plan(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    t0 = datetime.now(UTC)
    later = t0 + timedelta(hours=2)
    clock = [t0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return clock[0]

    monkeypatch.setattr(wa, "datetime", Clock)
    # inside expiry (7d) at plan time, past it two hours later when the write happens
    old = seed_row(
        store,
        pr_id_of(fake),
        digest=None,
        status="terminal",
        terminal_at=format_ts(t0 - timedelta(days=7) + timedelta(hours=1)),
    )
    real = fc.ForgectlClient.snapshot

    def slow_fetch(self: fc.ForgectlClient, ref: PrRef) -> fc.Outcome:
        outcome = real(self, ref)
        clock[0] = later  # the host call took two hours
        return outcome

    monkeypatch.setattr(fc.ForgectlClient, "snapshot", slow_fetch)
    doc = ok(capsys, PR, owner=B)
    stamp = format_ts(later)
    assert w.watch_exists(store.conn, old) is False  # the write-time cutoff expired the row
    assert doc["created"] is True and doc["watch"]["created_at"] == stamp
    assert doc["pr"]["terminal_at"] == stamp and doc["pr"]["baselined_at"] == stamp


# --- snapshot validation (R2, R8) -----


@pytest.mark.parametrize(
    "changes",
    [
        {"pr_id": "has space"},
        {"pr_id": "../etc"},
        {"pr_id": "x" * 5000},
        {"pr_id": 7},
        {"updated_at": 5},
        {"updated_at": None},
    ],
    ids=["space", "dotdot", "oversize", "not_string", "updated_at_int", "updated_at_null"],
)
def test_a_snapshot_with_a_bad_pr_id_or_updated_at_is_a_contract_failure(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, Any],
) -> None:
    snap = new_pr(number=7)
    snap.update(changes)
    # a raw answer: the fake validates only the documents it builds itself
    fake.raw("snapshot", stdout=json.dumps(snap))
    got, doc = add(capsys, PR)
    assert (got, doc["error"]["code"]) == (12, "contract")
    assert counts(store) == dict.fromkeys(counts(store), 0)


# --- too large, retries, deferred baseline (R4) -----


def test_a_too_large_snapshot_ends_with_exit_12_and_no_baseline_still_works(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def huge(self: fc.ForgectlClient, ref: PrRef) -> fc.Outcome:
        return fc.Outcome(command="snapshot", returncode=None, local="too_large", detail="d")

    monkeypatch.setattr(fc.ForgectlClient, "snapshot", huge)
    got, doc = add(capsys, PR)
    assert (got, doc["error"]["code"]) == (12, "host_failed")
    assert counts(store) == dict.fromkeys(counts(store), 0)
    doc = ok(capsys, PR, "--no-baseline")
    assert (doc["created"], doc["baselined"]) == (True, False)
    assert row(store)["digest"] is None


def test_a_row_that_vanishes_between_plan_and_write_is_fetched_and_baselined(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ok(capsys, PR)
    real = wa._needs_snapshot
    state = {"done": False}

    def vanishing(*args: Any) -> str | None:
        found = real(*args)
        if found is not None and not state["done"]:
            state["done"] = True
            with store.transaction() as c:
                c.execute("DELETE FROM prs")
                c.execute("DELETE FROM watches")
        return found

    monkeypatch.setattr(wa, "_needs_snapshot", vanishing)
    doc = ok(capsys, PR, owner=B)
    assert (doc["created"], doc["baselined"]) == (True, True)
    assert len(fake.calls("snapshot")) == 2 and row(store)["rev"] == 1


def test_retries_are_exhausted_with_exit_12_and_nothing_recorded(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wa, "_needs_snapshot", lambda *a: "GONE")  # a row that is never there
    got, doc = add(capsys, PR)
    assert (got, doc["error"]["code"]) == (12, "host_failed")
    assert counts(store) == dict.fromkeys(counts(store), 0) and not cmds(fake)


def test_baseline_deferred_by_a_newer_listing_adds_source_and_owner_only(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.update(PR, state="closed", closed_at="2026-01-01T00:00:00Z")
    pr_id = pr_id_of(fake)
    seed_row(store, pr_id, digest=None, owners=(A,))
    with store.transaction() as c:
        c.execute("UPDATE prs SET listed_updated_at = '2999-01-01T00:00:00Z'")
    doc = ok(capsys, PR, owner=B)
    assert (doc["created"], doc["changed"], doc["baselined"], doc["events_emitted"]) == (
        False, True, False, 0,
    )  # fmt: skip
    assert doc["watch"]["owners"] == [B, A] and len(doc["pr"]["sources"]) == 1
    assert row(store)["digest"] is None and counts(store)["events"] == 0
    assert cmds(fake) == ["snapshot"]


# --- label cap before the host call (R6) -----


def test_label_cap_on_a_missing_pr_is_refused_before_any_forgectl_call(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    got, doc = add(capsys, PR, *_labels(w.MAX_LABELS_PER_WATCH + 1))
    assert (got, doc["error"]["details"]["kind"]) == (2, "too_many_labels")
    assert fake.calls() == [] and counts(store) == dict.fromkeys(counts(store), 0)


def test_label_cap_on_a_stored_watch_is_refused_before_the_fetch(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, PR, "--no-baseline", *_labels(w.MAX_LABELS_PER_WATCH))
    before, calls = counts(store), len(fake.calls())
    got, doc = add(capsys, PR, "--label", "extra=1", owner=B)  # would baseline: needs a snapshot
    assert (got, doc["error"]["details"]["kind"]) == (2, "too_many_labels")
    assert len(fake.calls()) == calls and counts(store) == before


# --- expiry predicate (R9) and renames (R10) -----


def test_an_active_row_with_an_old_terminal_at_is_not_expired(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_row(store, pr_id_of(fake), status="active", terminal_at=LONG_AGO)
    doc = ok(capsys, PR, owner=B)
    assert (doc["created"], doc["changed"], doc["baselined"]) == (False, True, False)
    assert not cmds(fake) and counts(store)["prs"] == 1


def test_a_rename_seen_by_a_discarded_snapshot_updates_the_stored_row(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    renamed = "github.com/example-org/gadgets"
    pr_id = pr_id_of(fake)
    seed_row(store, pr_id, digest="{}", owners=(A,))
    seed_row(store, "OTHER", digest="{}", owners=(A,))  # same name: the reference is ambiguous
    with store.transaction() as c:
        c.execute("UPDATE prs SET pr = ? WHERE pr_id = 'OTHER'", (PR,))
    snap = new_pr(number=7)
    snap.update(pr=f"{renamed}#7", repo=renamed)
    fake.scenario["prs"][PR]["snapshot"] = snap
    fake._write()
    doc = ok(capsys, PR, owner=B)
    assert doc["baselined"] is False and doc["changed"] is True
    assert doc["pr"]["pr"] == f"{renamed}#7"
    assert (row(store, pr_id)["pr"], row(store, pr_id)["repo"]) == (f"{renamed}#7", renamed)
    assert row(store, pr_id)["rev"] == 0


def test_an_expired_row_with_a_full_label_set_does_not_block_a_re_add(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.update(PR, state="merged", merged_at="2026-01-01T00:00:00Z", merge_commit_sha=sha(3))
    old = seed_row(store, pr_id_of(fake), status="terminal", terminal_at=LONG_AGO)
    with store.transaction() as c:
        w.add_labels(c, old, A, [parse_label(f"k=v{i}") for i in range(w.MAX_LABELS_PER_WATCH)])
    doc = ok(capsys, PR, "--label", "new=1")
    assert doc["created"] is True and doc["watch"]["labels"] == ["new=1"]


def test_a_newer_rename_committed_between_plan_and_write_is_kept(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr_id = pr_id_of(fake)
    seed_row(store, pr_id, digest="{}", owners=(A,))
    seed_row(store, "OTHER", digest="{}", owners=(A,))  # same name: the reference is ambiguous
    with store.transaction() as c:
        c.execute("UPDATE prs SET pr = ? WHERE pr_id = 'OTHER'", (PR,))
    stale = "github.com/example-org/stale"
    newer = "github.com/example-org/newer"
    snap = new_pr(number=7)
    snap.update(pr=f"{stale}#7", repo=stale)
    fake.scenario["prs"][PR]["snapshot"] = snap
    fake._write()
    real = wa._snapshot_of

    def racing(*a: Any) -> dict[str, Any]:
        got = real(*a)
        with store.transaction() as c:
            c.execute(
                "UPDATE prs SET pr = ?, repo = ? WHERE pr_id = ?", (f"{newer}#7", newer, pr_id)
            )
        return got

    monkeypatch.setattr(wa, "_snapshot_of", racing)
    ok(capsys, PR, owner=B)
    assert (row(store, pr_id)["pr"], row(store, pr_id)["repo"]) == (f"{newer}#7", newer)


# --- goldens -----


def _norm(doc: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(doc, sort_keys=True)
    norm = json.loads(text)
    norm["watch"]["id"] = "w-GOLDEN"
    norm["watch"]["created_at"] = "TS"
    norm["cursor"] = "CURSOR"
    pr = norm["pr"]
    if pr is not None:
        pr["sources"] = ["w-GOLDEN"]
        for key in ("last_polled_at", "baselined_at", "terminal_at"):
            if pr.get(key) is not None:
                pr[key] = "TS"
    return norm


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("per-pr-baselined", [PR, "--label", "k=v"]),
        ("per-pr-no-baseline", [PR, "--no-baseline"]),
        ("query", ["--mine", "--open", "--host", "github.com"]),
    ],
)
def test_golden_outputs(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    name: str,
    argv: list[str],
) -> None:
    norm = _norm(ok(capsys, *argv))
    path = GOLDEN / f"{name}.json"
    assert norm == json.loads(path.read_text())


def test_human_output_names_the_outcome(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "watch", "add", PR, "--owner", A)
    assert code == 0 and PR in out and "baselined" in out
    code, out, _ = run(capsys, "--quiet", "watch", "add", PR, "--owner", B)
    assert code == 0 and out == ""


def test_parse_pr_fixture_sanity() -> None:
    assert isinstance(parse_pr(PR), PrRef)
