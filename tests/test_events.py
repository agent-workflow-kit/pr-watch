"""`events`, `show` and `export`: offline, read-only, through main()."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pr_watch import events as events_module
from pr_watch import show as show_module
from pr_watch.cli import main
from pr_watch.state_dir import open_state_dir
from pr_watch.store import NewEvent, Store, open_store
from tests.harness import assert_valid
from tests.test_list import add_pr, add_watch, digest, fingerprint, link, set_meta, settled

GOLDEN = Path(__file__).parent / "golden" / "events"
PLACEHOLDER = "v1.aaaaaaaa.0"
NOW = datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
D0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
PR1, PR2 = "github.com/o/r#1", "github.com/o/r#2"
BASE = {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []}


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture(autouse=True)
def pinned_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_module, "_now", lambda: NOW)
    monkeypatch.setattr(show_module, "_now", lambda: NOW)


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


def cur(store: Store, seq: int) -> str:
    return f"v1.{store.store_id}.{seq}"


def add_event(
    store: Store,
    type_: str = "closed",
    *,
    pr_id: str = "P1",
    pr: str = PR1,
    labels: tuple[str, ...] = (),
    detected: datetime = D0,
    **extra: Any,
) -> int:
    payload: dict[str, Any] = {**BASE, **extra}
    if type_ == "pushed":
        payload = {"from_sha": "b" * 40, "to_sha": "a" * 40, "forced": None, **payload}
    if type_ in ("ready", "converted_to_draft"):
        payload = {"from_blocked_by": [], "blocked_by": [], **payload}
    if type_ == "renamed":
        payload = {
            "from_pr": "example.net/o/old#1", "to_pr": "example.net/o/r#1",
            "from_repo": "example.net/o/old", "to_repo": "example.net/o/r", **payload,
        }  # fmt: skip
    if type_ == "retargeted":
        payload = {"from": "main", "to": "dev", **payload}
    if type_ == "approval_withdrawn":
        payload = {
            "reviewer": {"id": "1", "login": "a", "is_bot": False}, "to": "dismissed",
            "commit_sha": None, "at": None, "approvals_at_head": None, **payload,
        }  # fmt: skip
    if type_ == "review_requested":
        payload = {
            "reviewer": {"id": None, "login": "core", "kind": "team"}, "code_owner": None,
            **payload,
        }  # fmt: skip
    with store.transaction():
        return store.append_event(NewEvent(type_, pr_id, pr, payload, labels), detected).seq


def add_many(store: Store, n: int, **kw: Any) -> list[int]:
    return [add_event(store, **kw) for _ in range(n)]


def events(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    code, out, err = run(capsys, "events", "--json", *argv)
    assert code == 0, (out, err)
    d = json.loads(out)
    assert_valid("cli/events", d)
    return d  # type: ignore[no-any-return]


def seqs(d: dict[str, Any]) -> list[int]:
    return [e["seq"] for e in d["events"]]


def error_of(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code, out, _ = run(capsys, *argv, "--json")
    err = json.loads(out)["error"]
    assert_valid("cli/error", json.loads(out))
    return code, err  # type: ignore[no-any-return]


def check_golden(name: str, actual: Any) -> None:
    path = GOLDEN / f"{name}.json"
    if not path.exists():  # first run: write it, fail, and review the file before committing
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.fail(f"golden {path.name} was missing and has been written; review it")
    assert actual == json.loads(path.read_text())


# --- no store, from now -------------------------------------------------------------------------


def test_missing_state_dir_is_an_empty_store_and_creates_nothing(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = events(capsys)
    assert d == {
        "events": [],
        "cursor": PLACEHOLDER,
        "gap": False,
        "more": False,
        "last_poll_finished_at": None,
    }
    assert not state_dir.exists()
    d = events(capsys, "--since", "v1.bbbbbbbb.7")
    assert d["gap"] is True and d["events"] == [] and d["cursor"] == PLACEHOLDER
    assert not state_dir.exists()


def test_missing_store_placeholder_cursor_above_zero_is_a_gap(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert events(capsys, "--since", "v1.aaaaaaaa.5")["gap"] is True
    assert events(capsys, "--since", "v1.aaaaaaaa.0")["gap"] is False
    assert not state_dir.exists()


def test_without_since_returns_no_event_and_the_head(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 3)
    set_meta(store, "last_poll_finished_at", "2026-06-02T00:00:00.000000Z")
    d = events(capsys)
    assert d == {
        "events": [],
        "cursor": cur(store, 3),
        "gap": False,
        "more": False,
        "last_poll_finished_at": "2026-06-02T00:00:00.000000Z",
    }


def test_the_head_survives_a_prune_of_every_event(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 3)
    with store.transaction() as c:
        c.execute("DELETE FROM events")
        c.execute("UPDATE meta SET value = '3' WHERE key = 'pruned_through_seq'")
    assert events(capsys)["cursor"] == cur(store, 3)
    d = events(capsys, "--since", cur(store, 3))
    assert (d["gap"], d["events"]) == (False, [])


# --- cursor grammar and gaps --------------------------------------------------------------------


def test_events_after_the_cursor_oldest_first(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 4)
    d = events(capsys, "--since", cur(store, 1))
    assert seqs(d) == [2, 3, 4]
    assert (d["cursor"], d["more"], d["gap"]) == (cur(store, 4), False, False)


def test_a_cursor_at_the_head_returns_nothing_and_no_gap(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 2)
    d = events(capsys, "--since", cur(store, 2))
    assert (d["events"], d["cursor"], d["gap"], d["more"]) == ([], cur(store, 2), False, False)


def test_a_cursor_below_pruned_through_seq_is_a_gap_and_returns_the_retained_events(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 5)
    with store.transaction() as c:
        c.execute("DELETE FROM events WHERE seq <= 3")
        c.execute("UPDATE meta SET value = '3' WHERE key = 'pruned_through_seq'")
    for seq, gap in ((0, True), (2, True), (3, False), (4, False)):
        d = events(capsys, "--since", cur(store, seq))
        assert d["gap"] is gap, seq
    d = events(capsys, "--since", cur(store, 2))
    assert seqs(d) == [4, 5] and d["cursor"] == cur(store, 5)
    assert seqs(events(capsys, "--since", cur(store, 3))) == [4, 5]


def test_a_surviving_event_below_pruned_through_seq_does_not_hide_the_gap(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 4)
    with store.transaction() as c:
        c.execute("DELETE FROM events WHERE seq IN (1, 3)")
        c.execute("UPDATE meta SET value = '3' WHERE key = 'pruned_through_seq'")
    d = events(capsys, "--since", cur(store, 1))
    assert d["gap"] is True and seqs(d) == [2, 4]


def test_a_new_store_id_is_a_gap_and_returns_the_whole_store(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 3)
    other = "v1.aaaaaaaa.2" if store.store_id != "aaaaaaaa" else "v1.bbbbbbbb.2"
    d = events(capsys, "--since", other)
    assert d["gap"] is True and seqs(d) == [1, 2, 3] and d["cursor"] == cur(store, 3)


def test_a_future_cursor_is_a_gap_and_returns_the_whole_store(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 3)
    d = events(capsys, "--since", cur(store, 4))
    assert d["gap"] is True and seqs(d) == [1, 2, 3] and d["cursor"] == cur(store, 3)


def test_a_cursor_at_50_with_an_empty_table_and_head_100_is_a_gap(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 3)
    with store.transaction() as c:
        c.execute("DELETE FROM events")
        c.execute("UPDATE sqlite_sequence SET seq = 100 WHERE name = 'events'")
        c.execute("UPDATE meta SET value = '100' WHERE key = 'pruned_through_seq'")
    d = events(capsys, "--since", cur(store, 50))
    assert (d["gap"], d["events"], d["cursor"]) == (True, [], cur(store, 100))


def test_a_huge_cursor_seq_is_a_gap_not_an_overflow(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 1)
    d = events(capsys, "--since", f"v1.{store.store_id}.{'9' * 18}")
    assert d["gap"] is True and seqs(d) == [1]


@pytest.mark.parametrize(
    "bad", ["", "v1", "v2.aaaaaaaa.1", "v1.AAAAAAAA.1", "v1.aaaaaaaa.-1", "v1.aaaaaaaa.1x",
            "v1.aaaaaaaa." + "1" * 19, "v1.aaaaaaa.1", "v1.aaaaaaaa.1\n", "v1.aaaaaaa1.1"],
)  # fmt: skip
def test_a_malformed_cursor_is_a_usage_error(capsys: pytest.CaptureFixture[str], bad: str) -> None:
    code, err = error_of(capsys, "events", f"--since={bad}")
    assert code == 2 and err["code"] == "usage" and err["details"]["kind"] == "bad_cursor"


@pytest.mark.parametrize("bad", ["0", "1001", "-1", "x", "", "1.5", " 1"])
def test_a_bad_limit_is_a_usage_error(capsys: pytest.CaptureFixture[str], bad: str) -> None:
    code, err = error_of(capsys, "events", f"--limit={bad}")
    assert code == 2 and err["details"]["kind"] == "bad_limit"


def test_bad_type_and_bad_label_are_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert error_of(capsys, "events", "--type", "nope")[1]["details"]["kind"] == "bad_type"
    assert error_of(capsys, "events", "--label", "x")[1]["details"]["kind"] == "bad_label"
    assert error_of(capsys, "export", "--type", "nope")[1]["details"]["kind"] == "bad_type"


# --- limits and paging --------------------------------------------------------------------------


def test_the_default_limit_is_100_and_the_maximum_is_1000(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    with store.transaction():
        for _ in range(1001):
            store.append_event(NewEvent("closed", "P1", PR1, BASE, ()), D0)
    d = events(capsys, "--since", cur(store, 0))
    assert len(d["events"]) == 100 and d["more"] is True and d["cursor"] == cur(store, 100)
    d = events(capsys, "--since", cur(store, 0), "--limit", "1000")
    assert len(d["events"]) == 1000 and d["more"] is True and d["cursor"] == cur(store, 1000)
    d = events(capsys, "--since", cur(store, 1000), "--limit", "1000")
    assert seqs(d) == [1001] and d["more"] is False


def test_paging_never_skips_or_repeats(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    add_many(store, 7)
    got: list[int] = []
    cursor = cur(store, 0)
    pages = 0
    while True:
        d = events(capsys, "--since", cursor, "--limit", "3")
        got += seqs(d)
        cursor = d["cursor"]
        pages += 1
        if not d["more"]:
            break
    assert got == [1, 2, 3, 4, 5, 6, 7] and pages == 3 and cursor == cur(store, 7)


def test_exactly_limit_events_is_not_more(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    add_many(store, 3)
    d = events(capsys, "--since", cur(store, 0), "--limit", "3")
    assert seqs(d) == [1, 2, 3] and d["more"] is False and d["cursor"] == cur(store, 3)


def test_an_event_appended_between_pages_is_neither_skipped_nor_repeated(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 3)
    first = events(capsys, "--since", cur(store, 0), "--limit", "2")
    assert seqs(first) == [1, 2] and first["more"] is True
    add_many(store, 2)  # a poll commits between the two reads
    second = events(capsys, "--since", first["cursor"], "--limit", "2")
    assert seqs(second) == [3, 4] and second["more"] is True
    third = events(capsys, "--since", second["cursor"], "--limit", "2")
    assert seqs(third) == [5] and third["more"] is False and third["cursor"] == cur(store, 5)


def test_the_cursor_follows_the_head_read_with_the_rows(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An event committed after the read transaction began is not in the rows and so must not be
    behind the returned cursor."""
    add_many(store, 2)
    real = events_module._read_rows

    def racing(*a: Any, **k: Any) -> Any:
        out = real(*a, **k)
        add_event(store)  # a writer commits after the rows were read
        return out

    monkeypatch.setattr(events_module, "_read_rows", racing)
    d = events(capsys, "--since", cur(store, 0))
    assert seqs(d) == [1, 2] and d["cursor"] == cur(store, 2)
    monkeypatch.setattr(events_module, "_read_rows", real)
    assert seqs(events(capsys, "--since", d["cursor"])) == [3]


# --- filters ------------------------------------------------------------------------------------


def populate(store: Store) -> None:
    add_event(store, "closed", labels=("team=x",))  # 1
    add_event(store, "pushed", labels=("team=x", "tier=1"))  # 2
    add_event(store, "ready", pr_id="P2", pr=PR2, labels=("team=y",))  # 3
    add_event(store, "pushed", pr_id="P2", pr=PR2, labels=("team=x", "tier=1"))  # 4
    add_event(store, "closed", pr_id="P2", pr=PR2)  # 5


def test_type_filter_is_or(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--type", "pushed")) == [2, 4]
    assert seqs(events(capsys, *since, "--type", "pushed", "--type", "ready")) == [2, 3, 4]


def test_type_filter_accepts_the_new_types(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, "opened")  # 1
    add_event(store, "reopened")  # 2
    add_event(store, "pushed")  # 3
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--type", "opened")) == [1]
    assert seqs(events(capsys, *since, "--type", "reopened", "--type", "opened")) == [1, 2]
    code, out, _ = run(capsys, "export", "--type", "reopened")
    assert code == 0 and [json.loads(line)["type"] for line in out.splitlines()] == ["reopened"]


def test_type_filter_accepts_review_requested_and_converted_to_draft(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, "review_requested")  # 1
    add_event(store, "converted_to_draft")  # 2
    add_event(store, "ready")  # 3
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--type", "review_requested")) == [1]
    assert seqs(events(capsys, *since, "--type", "converted_to_draft", "--type", "ready")) == [2, 3]
    code, out, _ = run(capsys, "export", "--type", "converted_to_draft")
    assert code == 0 and [json.loads(x)["type"] for x in out.splitlines()] == ["converted_to_draft"]


def test_type_filter_accepts_approval_withdrawn(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, "approval_withdrawn")  # 1
    add_event(store, "approved")  # 2
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--type", "approval_withdrawn")) == [1]
    code, out, _ = run(capsys, "export", "--type", "approval_withdrawn")
    assert code == 0 and [json.loads(x)["type"] for x in out.splitlines()] == ["approval_withdrawn"]


def test_type_filter_accepts_renamed_and_retargeted(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, "renamed")  # 1
    add_event(store, "retargeted")  # 2
    add_event(store, "pushed")  # 3
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--type", "renamed")) == [1]
    assert seqs(events(capsys, *since, "--type", "retargeted")) == [2]
    assert seqs(events(capsys, *since, "--type", "renamed", "--type", "retargeted")) == [1, 2]
    code, out, _ = run(capsys, "events", *since)
    lines = out.splitlines()[1:]  # the first line is the count
    assert code == 0 and "from example.net/o/old#1" in lines[0] and "main -> dev" in lines[1]


def test_label_filter_is_and(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--label", "team=x")) == [1, 2, 4]
    assert seqs(events(capsys, *since, "--label", "team=x", "--label", "tier=1")) == [2, 4]
    assert seqs(events(capsys, *since, "--label", "team=z")) == []


def test_a_label_filter_matches_whole_labels_only(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, labels=("team=xx",))
    add_event(store, labels=("team=x",))
    assert seqs(events(capsys, "--since", cur(store, 0), "--label", "team=x")) == [2]


def test_pr_filter_by_name_url_and_id_is_or(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    add_pr(store, "P1", PR1, dig=digest())
    add_pr(store, "P2", PR2, dig=digest())
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--pr", PR1)) == [1, 2]
    assert seqs(events(capsys, *since, "--pr", "https://GITHUB.com/o/r/pull/2")) == [3, 4, 5]
    assert seqs(events(capsys, *since, "--pr", "P1", "--pr", "P2")) == [1, 2, 3, 4, 5]


def test_filters_are_anded_with_each_other(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    add_pr(store, "P2", PR2, dig=digest())
    got = events(
        capsys, "--since", cur(store, 0), "--pr", PR2, "--type", "pushed", "--label", "tier=1"
    )
    assert seqs(got) == [4]
    assert seqs(events(capsys, "--since", cur(store, 0), "--pr", PR2, "--label", "team=z")) == []


def test_a_pr_whose_row_expired_resolves_through_its_events(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)  # no rows at all
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--pr", PR2)) == [3, 4, 5]
    assert seqs(events(capsys, *since, "--pr", "P1")) == [1, 2]


def test_a_renamed_pr_returns_the_events_of_every_name_it_was_detected_under(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, "closed", pr_id="P1", pr="github.com/o/old#1")
    add_event(store, "pushed", pr_id="P1", pr="github.com/o/new#1")
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--pr", "github.com/o/old#1")) == [1, 2]
    assert seqs(events(capsys, *since, "--pr", "github.com/o/new#1")) == [1, 2]


def test_a_stored_row_wins_over_event_names(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, "closed", pr_id="P1", pr="github.com/o/old#1")  # PR1's old name
    add_event(store, "closed", pr_id="P2", pr=PR1)  # a different pr_id once named PR1
    add_pr(store, "P2", PR1, dig=digest())  # the row says PR1 is P2 now
    assert seqs(events(capsys, "--since", cur(store, 0), "--pr", PR1)) == [2]


def test_repeated_pr_skips_unresolvable_references_and_exits_10_only_when_none_resolve(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    since = ("--since", cur(store, 0))
    assert seqs(events(capsys, *since, "--pr", "github.com/o/zz#9", "--pr", PR2)) == [3, 4, 5]
    code, err = error_of(capsys, "events", *since, "--pr", "github.com/o/zz#9")
    assert code == 10 and err["code"] == "not_found"
    code, err = error_of(capsys, "events", "--pr", "github.com/o/zz#9", "--pr", "nope-id")
    assert code == 10 and err["code"] == "not_found"


def test_an_unresolvable_pr_without_a_store_is_not_found(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = error_of(capsys, "events", "--pr", PR1)
    assert code == 10 and not state_dir.exists()


def test_an_ambiguous_pr_name_is_a_usage_error(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, pr_id="P1")
    add_event(store, pr_id="P2")  # the same name under two ids (a reused repo name)
    code, err = error_of(capsys, "events", "--pr", PR1)
    assert code == 2 and err["details"]["kind"] == "ambiguous_reference"
    assert seqs(events(capsys, "--since", cur(store, 0), "--pr", "P1")) == [1]


def test_filters_apply_to_the_backlog_of_a_gap(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    d = events(capsys, "--since", "v1.aaaaaaaa.0", "--type", "pushed")
    assert d["gap"] is True and seqs(d) == [2, 4]


def test_a_filtered_page_advances_to_the_head_when_complete_and_to_the_last_hit_otherwise(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    d = events(capsys, "--since", cur(store, 0), "--type", "closed")
    assert seqs(d) == [1, 5] and d["more"] is False and d["cursor"] == cur(store, 5)
    add_many(store, 2, type_="ready")  # non-matching events after the last hit
    d = events(capsys, "--since", cur(store, 0), "--type", "closed")
    assert d["cursor"] == cur(store, 7)
    d = events(capsys, "--since", cur(store, 0), "--type", "closed", "--limit", "1")
    assert seqs(d) == [1] and d["more"] is True and d["cursor"] == cur(store, 1)
    d = events(capsys, "--since", d["cursor"], "--type", "closed", "--limit", "1")
    assert seqs(d) == [5] and d["more"] is False and d["cursor"] == cur(store, 7)


# --- shape, goldens and human output ------------------------------------------------------------


def test_events_json_golden(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    add_event(store, "pushed", labels=("team=x",), detected=D0)
    add_event(
        store, "comment", comment_id="gh:ic:1", thread_id=None,
        author={"id": "u1", "login": "ann", "is_bot": False},
    )  # fmt: skip
    d = events(capsys, "--since", cur(store, 0))
    for e in d["events"]:
        e["id"] = "ev-" + "a" * 12
    d["cursor"] = d["cursor"].replace(store.store_id, "aaaaaaaa")
    check_golden("two", d)


def test_every_event_type_validates(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    author = {"id": "u", "login": "l", "is_bot": None}
    extras: dict[str, dict[str, Any]] = {
        "renamed": {
            "from_pr": "example.net/o/old#1", "to_pr": "example.net/o/r#1",
            "from_repo": "example.net/o/old", "to_repo": "example.net/o/r",
        },
        "opened": {},
        "reopened": {},
        "pushed": {"from_sha": "b" * 40, "to_sha": "a" * 40, "forced": True},
        "retargeted": {"from": "main", "to": "release/1.x"},
        "comment": {"comment_id": "gh:ic:1", "thread_id": None, "author": author},
        "review_comment": {
            "comment_id": "gh:rc:1", "thread_id": "gh:rt:1", "author": author, "path": "a/b.py",
            "line": 3, "side": "RIGHT", "commit_sha": None,
        },
        "review": {"review_id": "gh:r:1", "author": author, "review_state": "approved"},
        "approved": {
            "reviewer": author, "commit_sha": None, "at": None, "can_push": True,
            "approvals_at_head": 1,
        },
        "review_requested": {
            "reviewer": {"id": "7", "login": "core", "kind": "team"}, "code_owner": True,
        },
        "changes_requested": {"reviewer": author, "commit_sha": None, "at": None, "can_push": None},
        "approval_withdrawn": {
            "reviewer": author, "to": None, "commit_sha": None, "at": None,
            "approvals_at_head": None,
        },
        "ci": {
            "from": "pending", "to": "failing", "checks": [], "counts":
            {"passing": 0, "failing": 1, "pending": 0}, "for_head": "a" * 40,
        },
        "ready": {"from_blocked_by": [{"code": "draft"}], "blocked_by": []},
        "converted_to_draft": {"from_blocked_by": [], "blocked_by": [{"code": "draft"}]},
        "merged": {"merged_head_sha": None, "merge_commit_sha": None},
        "closed": {},
    }  # fmt: skip
    for t, extra in extras.items():
        add_event(store, t, **extra)
    d = events(capsys, "--since", cur(store, 0))
    assert [e["type"] for e in d["events"]] == list(extras)
    code, out, _ = run(capsys, "events", "--since", cur(store, 0))  # and the human form runs
    assert code == 0 and out.count("\n") >= len(extras)


def test_human_output_is_one_line_per_event_with_stored_text_scrubbed(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    evil = {"id": "u", "login": "x\n  9  forged\x1b[31m‮", "is_bot": False}
    add_event(store, "comment", comment_id="gh:ic:1", thread_id=None, author=evil)
    add_event(store, "pushed")
    code, out, _ = run(capsys, "events", "--since", cur(store, 0))
    assert code == 0
    assert "\x1b" not in out and "‮" not in out and "forged" in out
    lines = out.splitlines()
    event_lines = [ln for ln in lines if ln.lstrip().startswith(("1 ", "2 "))]
    assert len(event_lines) == 2
    assert all("forged" not in ln or ln.lstrip().startswith("1 ") for ln in lines)
    assert "cursor" in lines[-1] or any("cursor" in ln for ln in lines)


def test_human_output_says_gap_and_more(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    add_many(store, 3)
    code, out, _ = run(capsys, "events", "--since", "v1.aaaaaaaa.1", "--limit", "2")
    assert code == 0 and "gap" in out and "more" in out


def test_quiet_silences_the_human_result_only(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 1)
    code, out, _ = run(capsys, "events", "--quiet", "--since", cur(store, 0))
    assert code == 0 and out == ""
    code, out, _ = run(capsys, "events", "--quiet", "--json", "--since", cur(store, 0))
    assert json.loads(out)["events"]


# --- read-only proof ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["events"], ["events", "--since", "v1.aaaaaaaa.1"], ["events", "--json", "--pr", "P1"],
        ["export"], ["export", "--since", "1d"], ["show", "P1"], ["show", "--json", PR1],
    ],
)  # fmt: skip
def test_the_commands_leave_the_state_directory_byte_identical(
    store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    populate(store)
    add_pr(store, "P1", PR1, dig=digest(), sources=("w-aaaaaaaaaa",))
    store.close()
    before = fingerprint(state_dir)
    assert run(capsys, *argv)[0] == 0
    assert settled(fingerprint(state_dir)) == settled(before)
    assert not (state_dir / "log.jsonl").exists()


def test_a_zero_byte_state_db_is_an_empty_store(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir.mkdir(mode=0o700, parents=True)
    db = state_dir / "state.db"
    db.write_bytes(b"")
    db.chmod(0o600)
    assert events(capsys)["cursor"] == PLACEHOLDER
    assert run(capsys, "export")[1] == ""
    assert run(capsys, "show", "P1")[0] == 10
    assert db.read_bytes() == b"" and [p.name for p in state_dir.iterdir()] == ["state.db"]


# --- show ---------------------------------------------------------------------------------------


def show(capsys: pytest.CaptureFixture[str], ref: str) -> dict[str, Any]:
    code, out, err = run(capsys, "show", "--json", "--", ref)
    assert code == 0, (out, err)
    d = json.loads(out)
    assert_valid("cli/show", d)
    return d  # type: ignore[no-any-return]


FULL = json.dumps(
    {
        "state": "open", "draft": False, "head_sha": "a" * 40,
        "ci": {"state": "passing", "head_sha": "a" * 40, "hold": 0},
        "blocked_by": [{"code": "reviews_required"}, {"code": "other", "detail": "x"}],
        "review_decision": "review_required", "approvals_at_head": 1,
        "reviewers": {
            "u2": {"state": "approved", "commit_sha": "a" * 40, "at": "2026-06-01T10:00:00Z",
                   "can_push": True},
            "login:bob": {"state": "changes_requested", "commit_sha": None, "at": None,
                          "can_push": None},
            "u9": {"state": "unknown", "commit_sha": None, "at": None, "can_push": None},
        },
        "seen": {"comments": ["gh:ic:1", "gh:ic:2", "gh:rc:3"], "reviews": ["gh:r:1"]},
        "baselined": {"ci": True, "reviews": True, "threads": True},
    }
)  # fmt: skip


def seed_show(store: Store) -> None:
    add_watch(store, "w-aaaaaaaaaa", "pr", "P1", {"human:a": ["team=x"]})
    add_pr(store, "P1", PR1, dig=FULL)
    link(store, "P1", "w-aaaaaaaaaa")
    add_watch(store, "w-bbbbbbbbbb", "query", "mine+open;host=github.com", {"agent:b": []})
    link(store, "P1", "w-bbbbbbbbbb")


def test_show_prints_the_row_the_digest_summary_the_watches_and_the_events(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_show(store)
    add_event(store, "pushed")
    add_event(store, "closed", pr_id="P2", pr=PR2)
    add_event(store, "ready")
    d = show(capsys, PR1)
    assert d["pr_id"] == "P1" and d["state"] == "open"
    assert d["blocked_by"] == [{"code": "reviews_required"}, {"code": "other", "detail": "x"}]
    assert (d["review_decision"], d["approvals_at_head"]) == ("review_required", 1)
    assert d["seen"] == {"comments": 3, "reviews": 1}
    assert d["reviewers"] == [
        {"key": "login:bob", "id": None, "state": "changes_requested", "commit_sha": None,
         "at": None, "can_push": None},
        {"key": "u2", "id": "u2", "state": "approved", "commit_sha": "a" * 40,
         "at": "2026-06-01T10:00:00.000000Z", "can_push": True},
    ]  # fmt: skip
    assert [w["id"] for w in d["watches"]] == ["w-aaaaaaaaaa", "w-bbbbbbbbbb"]
    assert [e["seq"] for e in d["events"]] == [1, 3]
    assert d["sources"] == ["w-aaaaaaaaaa", "w-bbbbbbbbbb"]


def test_show_json_golden(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed_show(store)
    add_event(store, "pushed", watches=["w-aaaaaaaaaa"])
    d = show(capsys, PR1)
    d["events"][0]["id"] = "ev-" + "a" * 12
    check_golden("show", d)


def test_show_by_url_and_pr_id(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed_show(store)
    for ref in ("https://github.com/o/r/pull/1", "P1", "GITHUB.com/o/r#1"):
        assert show(capsys, ref)["pr_id"] == "P1"


def test_show_prints_the_20_most_recent_events_oldest_first(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_show(store)
    add_many(store, 25)
    add_event(store, "closed", pr_id="P2", pr=PR2)
    assert [e["seq"] for e in show(capsys, PR1)["events"]] == list(range(6, 26))


def test_show_events_are_matched_on_pr_id_whatever_name_they_were_detected_under(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_show(store)
    add_event(store, pr="github.com/o/old#1")
    add_event(store)
    assert [e["seq"] for e in show(capsys, PR1)["events"]] == [1, 2]


def test_show_before_the_first_snapshot_has_null_digest_fields(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_pr(store, "P1", PR1, dig=None, sources=("w-aaaaaaaaaa",))
    d = show(capsys, PR1)
    assert d["blocked_by"] is None and d["review_decision"] is None
    assert d["approvals_at_head"] is None and d["reviewers"] == []
    assert d["seen"] == {"comments": 0, "reviews": 0} and d["events"] == []


def test_show_tolerates_a_corrupt_digest(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    bad = json.dumps(
        {"blocked_by": "x", "review_decision": 5, "approvals_at_head": -3, "reviewers": [1],
         "seen": {"comments": "no", "reviews": None}}
    )  # fmt: skip
    add_pr(store, "P1", PR1, dig=bad, sources=("w-aaaaaaaaaa",))
    d = show(capsys, PR1)
    assert d["blocked_by"] is None and d["review_decision"] is None
    assert d["approvals_at_head"] is None and d["reviewers"] == []
    assert d["seen"] == {"comments": 0, "reviews": 0}


def test_show_an_unstored_pr_is_exit_10(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed_show(store)
    code, err = error_of(capsys, "show", "github.com/o/r#99")
    assert code == 10 and err["code"] == "not_found"
    code, _, _ = run(capsys, "show", "P99")
    assert code == 10


def test_show_without_a_store_is_exit_10_and_creates_nothing(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert error_of(capsys, "show", PR1)[0] == 10
    assert not state_dir.exists()


def test_show_does_not_resolve_a_pr_that_only_has_events(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store)  # the row expired; show is about stored rows
    assert error_of(capsys, "show", PR1)[0] == 10


def test_show_an_ambiguous_name_is_exit_2(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    add_pr(store, "P1", PR1, dig=digest(), sources=("w-aaaaaaaaaa",))
    add_pr(store, "P2", PR1, dig=digest(), sources=("w-aaaaaaaaaa",))
    code, err = error_of(capsys, "show", PR1)
    assert code == 2 and err["details"]["kind"] == "ambiguous_reference"
    assert show(capsys, "P2")["pr_id"] == "P2"


def test_show_human_output_scrubs_stored_text(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile = json.loads(FULL)
    hostile["blocked_by"] = [{"code": "other", "detail": "a\nFORGED  line\x1b[2J"}]
    hostile["reviewers"] = {"login:ev\nil\x07": hostile["reviewers"]["login:bob"]}
    add_pr(store, "P1", PR1, dig=json.dumps(hostile), sources=("w-aaaaaaaaaa",))
    add_event(
        store, "comment", comment_id="gh:ic:1", thread_id=None,
        author={"id": "u", "login": "x\ny\x1b[1m", "is_bot": False},
    )  # fmt: skip
    code, out, _ = run(capsys, "show", PR1)
    assert code == 0 and "\x1b" not in out and "\x07" not in out
    assert not any(ln.startswith("FORGED") for ln in out.splitlines())
    assert not any(ln.strip().startswith("y") for ln in out.splitlines())


# --- export -------------------------------------------------------------------------------------


def export_lines(capsys: pytest.CaptureFixture[str], *argv: str) -> list[dict[str, Any]]:
    code, out, err = run(capsys, "export", *argv)
    assert code == 0, (out, err)
    assert out == "" or out.endswith("\n")
    lines = [json.loads(line) for line in out.splitlines()]
    for ln in lines:
        assert_valid("cli/export", ln)
    return lines


def test_export_prints_every_retained_event_as_json_lines_oldest_first(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    out = export_lines(capsys)
    assert [e["seq"] for e in out] == [1, 2, 3, 4, 5]
    assert out[0] == events(capsys, "--since", cur(store, 0))["events"][0]


def test_export_is_json_whatever_the_global_flags(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    base = run(capsys, "export")[1]
    assert run(capsys, "export", "--json")[1] == base
    assert run(capsys, "export", "--quiet")[1] == base


def test_export_round_trips_with_events(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    assert (
        export_lines(capsys)
        == events(capsys, "--since", cur(store, 0), "--limit", "1000")["events"]
    )


def test_export_since_selects_events_detected_within_the_last_duration(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, detected=datetime(2026, 6, 1, tzinfo=UTC))  # 9 days before NOW
    add_event(store, detected=datetime(2026, 6, 8, 12, tzinfo=UTC))  # 1.5 days
    add_event(store, detected=datetime(2026, 6, 9, 20, tzinfo=UTC))  # 4 hours
    assert [e["seq"] for e in export_lines(capsys)] == [1, 2, 3]
    assert [e["seq"] for e in export_lines(capsys, "--since", "2d")] == [2, 3]
    assert [e["seq"] for e in export_lines(capsys, "--since", "1d")] == [3]
    assert [e["seq"] for e in export_lines(capsys, "--since", "5h")] == [3]
    assert [e["seq"] for e in export_lines(capsys, "--since", "3h")] == []
    assert export_lines(capsys, "--since", "0") == []


def test_export_filters_match_events(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    add_pr(store, "P2", PR2, dig=digest())
    assert [e["seq"] for e in export_lines(capsys, "--type", "pushed")] == [2, 4]
    assert [e["seq"] for e in export_lines(capsys, "--label", "tier=1", "--pr", PR2)] == [4]
    assert [e["seq"] for e in export_lines(capsys, "--pr", "P1", "--type", "closed")] == [1]


def test_export_with_no_events_or_no_store_prints_nothing(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "export") == (0, "", "")
    assert not state_dir.exists()


def test_export_pr_that_resolves_nowhere_is_exit_10(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    assert error_of(capsys, "export", "--pr", "github.com/o/zz#9")[0] == 10


def test_export_has_no_limit_and_streams_every_event(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    with store.transaction():
        for _ in range(1203):
            store.append_event(NewEvent("closed", "P1", PR1, BASE, ()), D0)
    assert len(export_lines(capsys)) == 1203


def test_export_json_lines_contain_stored_text_escaped_not_raw(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(
        store, "comment", comment_id="gh:ic:1", thread_id=None,
        author={"id": "u", "login": "a\nb c", "is_bot": False},
    )  # fmt: skip
    code, out, _ = run(capsys, "export")
    assert code == 0 and out.count("\n") == 1  # the newline in the login did not split the line
    assert json.loads(out)["author"]["login"] == "a\nb c"


def test_export_golden(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    add_event(store, "pushed", labels=("team=x",), detected=D0)
    add_event(
        store, "comment", comment_id="gh:ic:1", thread_id=None,
        author={"id": "u1", "login": "ann", "is_bot": False},
    )  # fmt: skip
    lines = export_lines(capsys)
    for e in lines:
        e["id"] = "ev-" + "a" * 12
    path = GOLDEN / "export.jsonl"
    text = "".join(json.dumps(e, sort_keys=True) + "\n" for e in lines)
    if not path.exists():  # first run: write it, fail, and review the file before committing
        path.write_text(text)
        pytest.fail(f"golden {path.name} was missing and has been written; review it")
    assert text == path.read_text()


# --- corrupt stored rows ------------------------------------------------------------------------

MALFORMED = "state.db holds a malformed event (seq 2)"


def corrupt(store: Store, column: str, value: str) -> None:
    with store.transaction() as c:
        c.execute(f"UPDATE events SET {column} = ? WHERE seq = 2", (value,))  # noqa: S608


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("payload", "{not json"),
        ("payload", "[1, 2]"),
        ("labels", "not json"),
        ("labels", '{"a": 1}'),
        ("labels", "[1, 2]"),
    ],
)
@pytest.mark.parametrize("argv", [["events", "--since", "CUR0"], ["export"]])
def test_malformed_stored_row_is_a_fixed_internal_error(
    store: Store, capsys: pytest.CaptureFixture[str], column: str, value: str, argv: list[str]
) -> None:
    add_many(store, 3)
    corrupt(store, column, value)
    argv = [cur(store, 0) if a == "CUR0" else a for a in argv]
    code, err = error_of(capsys, *argv)
    assert code == 1 and err["code"] == "internal" and err["message"] == MALFORMED


def test_label_filter_over_malformed_labels_reports_the_row_error_not_sqlite(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(store, 3, labels=("a=b",))
    corrupt(store, "labels", "not json")
    for argv in (
        ["events", "--since", cur(store, 0), "--label", "a=b"],
        ["export", "--label", "a=b"],
    ):
        code, err = error_of(capsys, *argv)
        assert code == 1 and err["code"] == "internal" and err["message"] == MALFORMED


@pytest.mark.parametrize("value", ["[1, 2]", '{"a": 1}', '"x"'])
def test_label_filter_surfaces_non_list_labels(
    store: Store, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    add_many(store, 3, labels=("a=b",))
    corrupt(store, "labels", value)
    for argv in (
        ["events", "--since", cur(store, 0), "--label", "a=b"],
        ["export", "--label", "a=b"],
    ):
        code, err = error_of(capsys, *argv)
        assert code == 1 and err["code"] == "internal" and err["message"] == MALFORMED


def test_label_filter_still_filters_valid_rows(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_event(store, labels=("a=b",))
    add_event(store, labels=("c=d",))
    assert seqs(events(capsys, "--since", cur(store, 0), "--label", "a=b")) == [1]
