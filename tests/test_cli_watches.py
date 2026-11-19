"""`watch remove`, `label add`, `label remove` and the store-error wiring, through main()."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pr_watch import watches as w
from pr_watch.cli import HANDLERS, main
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "watches"
NOW = datetime(2026, 6, 1, tzinfo=UTC)
A, B = "human:a", "agent:b"
PR = "github.com/o/r#1"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


def seed(store: Store, owners: tuple[str, ...] = (A,), pr_id: str = "P1", pr: str = PR) -> str:
    with store.transaction() as c:
        c.execute(
            "INSERT INTO prs (pr_id, pr, repo, host, digest) VALUES (?, ?, ?, 'github.com', '{}')",
            (pr_id, pr, pr.split("#")[0]),
        )
        wid, _ = w.ensure_watch(c, "pr", pr_id, NOW)
        w.add_source(c, pr_id, wid)
        for o in owners:
            w.add_owner(c, wid, o)
    return wid


def seed_query(store: Store, key: str = "mine+open", owners: tuple[str, ...] = (A,)) -> str:
    with store.transaction() as c:
        wid, _ = w.ensure_watch(c, "query", key, NOW)
        for o in owners:
            w.add_owner(c, wid, o)
    return wid


def doc(out: str, schema: str) -> dict[str, Any]:
    d = json.loads(out)
    assert_valid(schema, d)
    return d  # type: ignore[no-any-return]


def golden(name: str, d: dict[str, Any]) -> None:
    """Compare with the golden file, with ids and timestamps normalized."""
    text = json.dumps(d, sort_keys=True)
    norm = json.loads(text.replace(d.get("watch", {}) and d["watch"]["id"] or "-", "w-GOLDEN"))
    if isinstance(norm.get("watch"), dict):
        norm["watch"]["created_at"] = "TS"
    path = GOLDEN / f"{name}.json"
    assert norm == json.loads(path.read_text())


def counts(store: Store) -> dict[str, int]:
    return {
        t: int(store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # noqa: S608
        for t in ("watches", "owners", "labels", "sources", "prs")
    }


# --- watch remove ----------------------------------------------------------------------------


def test_remove_the_last_owner_ends_the_watch(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    code, out, _ = run(capsys, "watch", "remove", PR, "--owner", A, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0
    assert d == {
        "watch": None, "owner": A, "changed": True, "reason": None,
        "watch_ended": True, "prs_dropped": 1, "reference": PR,
    }  # fmt: skip
    assert set(counts(store).values()) == {0}
    golden("watch-remove-ended", d)


def test_remove_is_idempotent(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store)
    run(capsys, "watch", "remove", PR, "--owner", A)
    code, out, err = run(capsys, "watch", "remove", PR, "--owner", A, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0 and (d["changed"], d["reason"], d["watch"]) == (False, "pr_unknown", None)
    assert d["reference"] == PR and "warning" in err and PR in err
    golden("watch-remove-pr_unknown", d)


def test_remove_by_a_non_owner_keeps_the_watch(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    before = counts(store)
    code, out, err = run(capsys, "watch", "remove", PR, "--owner", B, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0 and (d["changed"], d["reason"], d["watch"]) == (False, "not_held", None)
    assert err == "" and counts(store) == before


def test_remove_one_of_two_owners_prints_the_remaining_watch(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store, (A, B))
    code, out, _ = run(capsys, "watch", "remove", PR, "--owner", A, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0 and d["changed"] and not d["watch_ended"] and d["prs_dropped"] == 0
    assert d["watch"]["owners"] == [B]
    golden("watch-remove-remaining", d)


def test_remove_by_pr_id_and_case_insensitive_url(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store, (A, B))
    assert run(capsys, "watch", "remove", "P1", "--owner", A, "--json")[0] == 0
    code, out, _ = run(
        capsys, "watch", "remove", "https://GitHub.com/O/R/pull/1", "--owner", B, "--json"
    )
    d = doc(out, "cli/watch-remove")
    assert code == 0 and d["watch_ended"] is True and d["reference"] == PR


def test_remove_unknown_pr_id_reports_the_id(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "watch", "remove", "Nope1", "--owner", A, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0 and d["reason"] == "pr_unknown" and d["reference"] == "Nope1"


def test_remove_quiet_suppresses_the_warning(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "watch", "remove", PR, "--owner", A, "--quiet")
    assert (code, out, err) == (0, "", "")


def test_remove_human_output(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store)
    code, out, _ = run(capsys, "watch", "remove", PR, "--owner", A)
    assert code == 0 and "the watch ended" in out


def test_remove_ambiguous_name_is_usage_with_pr_id_hint(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store, pr_id="P1")
    seed(store, pr_id="P2")
    before = counts(store)
    code, out, _ = run(capsys, "watch", "remove", PR, "--owner", A, "--json")
    err = json.loads(out)["error"]
    assert code == 2 and err["code"] == "usage"
    assert err["details"]["kind"] == "ambiguous_reference" and "pr_id" in err["message"]
    assert counts(store) == before
    assert run(capsys, "watch", "remove", "P2", "--owner", A)[0] == 0


def test_remove_a_pr_that_is_only_a_query_member_is_not_held(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    q = seed_query(store)
    with store.transaction() as c:
        c.execute("INSERT INTO prs (pr_id, pr, repo, host) VALUES ('P1', ?, 'x', 'h')", (PR,))
        w.add_source(c, "P1", q)
    before = counts(store)
    code, out, _ = run(capsys, "watch", "remove", PR, "--owner", A, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0 and (d["changed"], d["reason"], d["watch"]) == (False, "not_held", None)
    assert counts(store) == before


def test_remove_query_watch_by_exact_key(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed_query(store, "mine+open;host=github.com;repo=github.com/o/r")
    code, out, _ = run(
        capsys, "watch", "remove", "--mine", "--open", "--host", "GitHub.com",
        "--repo", "github.com/O/R", "--owner", A, "--json",
    )  # fmt: skip
    d = doc(out, "cli/watch-remove")
    assert code == 0 and d["changed"] and d["watch_ended"]
    assert d["reference"] == "mine+open;host=github.com;repo=github.com/o/r"


def test_remove_query_has_no_partial_match(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_query(store, "mine+open;host=github.com")
    code, out, _ = run(capsys, "watch", "remove", "--mine", "--open", "--owner", A, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0 and (d["changed"], d["reason"]) == (False, "not_held")
    assert d["reference"] == "mine+open" and counts(store)["watches"] == 1


def test_remove_query_drops_member_rows_without_other_sources(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    q = seed_query(store)
    wid = seed(store)
    with store.transaction() as c:
        c.execute(
            "INSERT INTO prs (pr_id, pr, repo, host) VALUES ('P2', 'github.com/o/r#2', 'x', 'h')"
        )
        w.add_source(c, "P2", q)
        w.add_source(c, "P1", q)
    code, out, _ = run(capsys, "watch", "remove", "--mine", "--open", "--owner", A, "--json")
    d = doc(out, "cli/watch-remove")
    assert code == 0 and d["prs_dropped"] == 1
    assert w.watch_view(store.conn, wid) is not None  # the per-PR watch and its row stay


def test_remove_requires_a_target_and_an_owner(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "watch", "remove", "--owner", A)[0] == 2
    assert run(capsys, "watch", "remove", PR)[0] == 2  # no tty: owner required


# --- label add ---------------------------------------------------------------------------------


def test_label_add_by_pr_and_by_watch_id(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    wid = seed(store)
    code, out, _ = run(capsys, "label", "add", PR, "k=v", "x=y", "--owner", A, "--json")
    d = doc(out, "cli/label-add")
    assert code == 0 and d["changed"] and d["watch"]["labels"] == ["k=v", "x=y"]
    golden("label-add", d)
    code, out, _ = run(capsys, "label", "add", "--watch", wid, "z=1", "--owner", A, "--json")
    d = doc(out, "cli/label-add")
    assert d["changed"] and d["watch"]["labels"] == ["k=v", "x=y", "z=1"]


def test_label_add_existing_is_a_no_op(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store)
    run(capsys, "label", "add", PR, "k=v", "--owner", A)
    code, out, _ = run(capsys, "label", "add", PR, "k=v", "--owner", A, "--json")
    d = doc(out, "cli/label-add")
    assert code == 0 and d["changed"] is False and d["watch"]["labels"] == ["k=v"]


def test_label_add_by_a_non_owner_is_exit_10_and_changes_nothing(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    before = counts(store)
    code, out, _ = run(capsys, "label", "add", PR, "k=v", "--owner", B, "--json")
    err = json.loads(out)["error"]
    assert code == 10 and err["code"] == "not_found" and err["details"]["kind"] == "owner_not_held"
    assert counts(store) == before
    golden_err = {"error": err}
    assert_valid("cli/error", golden_err)


def test_label_add_without_a_watch_is_exit_10(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (
        ["label", "add", PR, "k=v"],
        ["label", "add", "--watch", "w-aaaaaaaaaa", "k=v"],
    ):
        code, out, _ = run(capsys, *argv, "--owner", A, "--json")
        err = json.loads(out)["error"]
        assert code == 10 and err["details"]["kind"] == "no_watch"


def test_label_add_to_a_query_watch_by_id(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    wid = seed_query(store)
    code, out, _ = run(capsys, "label", "add", "--watch", wid, "k=v", "--owner", A, "--json")
    d = doc(out, "cli/label-add")
    assert code == 0 and d["watch"]["kind"] == "query" and d["watch"]["labels"] == ["k=v"]


def test_label_add_isolates_owners(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store, (A, B))
    run(capsys, "label", "add", PR, "k=v", "--owner", A)
    run(capsys, "label", "add", PR, "k=v", "--owner", B)
    run(capsys, "label", "remove", PR, "k=v", "--owner", A)
    rows = store.conn.execute("SELECT owner, label FROM labels").fetchall()
    assert rows == [(B, "k=v")]


@pytest.mark.parametrize("bad", ["K=v", "k=", "k", "k=a b", "k=v;drop", "=v"])
def test_label_add_rejects_hostile_labels_before_touching_state(
    bad: str, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(store)
    code, out, _ = run(capsys, "label", "add", PR, bad, "--owner", A, "--json")
    assert code == 2 and json.loads(out)["error"]["details"]["kind"] in (
        "bad_label",
        "missing_labels",
    )
    assert counts(store)["labels"] == 0


# --- label remove ------------------------------------------------------------------------------


def test_label_remove(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    seed(store)
    run(capsys, "label", "add", PR, "k=v", "x=y", "--owner", A)
    code, out, _ = run(capsys, "label", "remove", PR, "k=v", "--owner", A, "--json")
    d = doc(out, "cli/label-remove")
    assert code == 0 and d["changed"] and d["reason"] is None and d["watch"]["labels"] == ["x=y"]
    golden("label-remove", d)
    code, out, _ = run(capsys, "label", "remove", PR, "k=v", "--owner", A, "--json")
    d = doc(out, "cli/label-remove")
    assert code == 0 and d["changed"] is False and d["reason"] is None
    assert d["watch"]["labels"] == ["x=y"]


def test_label_remove_idempotent_reasons(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    wid = seed(store)
    code, out, _ = run(capsys, "label", "remove", PR, "k=v", "--owner", B, "--json")
    d = doc(out, "cli/label-remove")
    assert code == 0 and (d["watch"], d["changed"], d["reason"]) == (None, False, "not_held")
    for argv in (["label", "remove", "github.com/o/r#9", "k=v"],
                 ["label", "remove", "--watch", "w-aaaaaaaaaa", "k=v"]):  # fmt: skip
        code, out, _ = run(capsys, *argv, "--owner", A, "--json")
        d = doc(out, "cli/label-remove")
        assert code == 0 and (d["watch"], d["changed"], d["reason"]) == (None, False, "no_watch")
    assert w.watch_view(store.conn, wid) is not None


# --- store errors in the CLI frame ----------------------------------------------------------------


def test_a_newer_store_is_store_too_new_exit_12(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with open_state_dir(state_dir) as state, open_store(state):
        pass
    db = sqlite3.connect(state_dir / "state.db")
    db.execute("PRAGMA journal_mode = DELETE")
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    code, out, _ = run(capsys, "watch", "remove", PR, "--owner", A, "--json")
    assert code == 12 and json.loads(out)["error"]["code"] == "store_too_new"
    assert_valid("cli/error", json.loads(out))


def test_a_corrupt_store_is_internal_exit_1(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir.mkdir(mode=0o700)
    (state_dir / "state.db").write_bytes(b"not a database" * 100)
    (state_dir / "state.db").chmod(0o600)
    code, out, _ = run(capsys, "watch", "remove", PR, "--owner", A, "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "internal"
    assert "not a database" not in out


def test_commands_without_the_store_ignore_a_bad_database(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir.mkdir(mode=0o700)
    (state_dir / "state.db").write_bytes(b"junk" * 100)
    (state_dir / "state.db").chmod(0o600)
    monkeypatch.setitem(HANDLERS, "cron uninstall", lambda ctx, args: 0)
    assert run(capsys, "cron", "uninstall")[0] == 0


def test_store_error_exit_codes_are_stable() -> None:
    from pr_watch.cli import store_error_to_cli
    from pr_watch.store import StoreBusyError, StoreCorruptError, StoreTooNewError

    assert store_error_to_cli(StoreTooNewError(9, 1)).exit_code == 12
    assert store_error_to_cli(StoreCorruptError("x")).exit_code == 1
    assert store_error_to_cli(StoreBusyError("x")).exit_code == 1
