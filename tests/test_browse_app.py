"""`browse` app: Textual's headless pilot over a real (isolated) state directory."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import ComposeResult

from pr_watch import browse_model as bm
from pr_watch.browse_app import BrowseApp
from pr_watch.cli import CliError
from pr_watch.config import LoadedConfig, load_config
from pr_watch.state_dir import open_state_dir
from pr_watch.store import NewEvent, Store, open_store
from tests.browse_support import (
    cursor_key,
    detail,
    drive,
    everything_shown,
    keys,
    rows,
    status,
    views,
)
from tests.test_list import T0, add_pr, add_watch, digest, fingerprint, link, populate


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


def add_event(store: Store, type_: str = "closed", **labels: str) -> None:
    payload = {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []}
    with store.transaction():
        store.append_event(
            NewEvent(
                type_, "P1", "github.com/o/r#1", payload, [f"{k}={v}" for k, v in labels.items()]
            ),
            datetime(2026, 6, 1, tzinfo=UTC),
        )


def test_empty_store_shows_the_empty_state_and_creates_nothing(state_dir: Path) -> None:
    seen: dict[str, str] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        seen["detail"] = detail(app)
        seen["rows"] = str(rows(app))
        seen["status"] = status(app)

    assert drive(script) is None
    assert "No watches, PRs or events stored yet" in seen["detail"]
    assert seen["rows"] == "[]"
    assert "0/0 shown" in seen["status"] and "STALE" not in seen["status"]
    assert not state_dir.exists()


def test_populated_store_lists_prs_watches_and_events(store: Store) -> None:
    populate(store)
    add_event(store, "closed")
    seen: dict[str, object] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        seen["prs"] = keys(app)
        seen["pr_rows"] = rows(app)
        seen["views"] = views(app)
        await pilot.press("2")  # type: ignore[attr-defined]
        seen["watches"] = keys(app)
        await pilot.press("3")  # type: ignore[attr-defined]
        seen["events"] = rows(app)
        await pilot.press("tab")  # type: ignore[attr-defined]  # wraps to PRs
        seen["wrapped"] = keys(app)

    drive(script)
    assert seen["prs"] == ["P1", "P2", "P3", "P4", "P6"]
    assert {r[1] for r in seen["pr_rows"]} == {"active", "terminal", "unreachable"}  # type: ignore[union-attr]
    assert "[PRs 5]" in str(seen["views"])
    assert seen["watches"] == ["w-aaaaaaaaaa", "w-bbbbbbbbbb"]
    assert [r[2] for r in seen["events"]] == ["closed"]  # type: ignore[union-attr]
    assert seen["wrapped"] == seen["prs"]


def test_pr_detail_follows_the_cursor(store: Store) -> None:
    populate(store)
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: object) -> None:
        seen.append(detail(app))
        await pilot.press("j")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        seen.append(detail(app))

    drive(script)
    assert "github.com/o/r#1" in seen[0] and "reviewers (0)" in seen[0]
    assert "github.com/o/r#2" in seen[1]


def test_refresh_keeps_the_selection_on_the_same_item(store: Store) -> None:
    populate(store)
    seen: dict[str, object] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        await pilot.press("j", "j")  # type: ignore[attr-defined]
        seen["before"] = cursor_key(app)
        # a PR that sorts first appears: the cursor's row index moves, the item stays
        add_pr(store, "P0", "github.com/a/a#1", dig=digest())
        link(store, "P0", "w-aaaaaaaaaa")
        await pilot.press("r")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        seen["after"] = cursor_key(app)
        seen["keys"] = keys(app)
        seen["detail"] = detail(app)

    drive(script)
    assert seen["before"] == "P3" == seen["after"]
    assert seen["keys"][0] == "P0"  # type: ignore[index]
    assert "github.com/o/s#3" in str(seen["detail"])


def test_selection_that_disappears_clamps_to_a_row(store: Store) -> None:
    populate(store)
    seen: dict[str, object] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        await pilot.press("j", "j", "j", "j")  # type: ignore[attr-defined]  # last row, P6
        with store.transaction() as c:
            c.execute("DELETE FROM prs WHERE pr_id = 'P6'")
        await pilot.press("r")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        seen["key"] = cursor_key(app)

    drive(script)
    assert seen["key"] == "P4"


def test_a_failed_reread_marks_the_view_stale_and_keeps_the_data(store: Store) -> None:
    populate(store)
    state = {"fail": False}

    def reader(cfg: LoadedConfig) -> bm.Snapshot:
        if state["fail"]:
            raise CliError("internal", "state.db is locked\x1b[2J")
        return bm.load_snapshot(cfg)

    seen: dict[str, str | list[str]] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        await pilot.press("j")  # type: ignore[attr-defined]
        state["fail"] = True
        await pilot.press("r")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        seen["status"], seen["sub"], seen["keys"] = status(app), app.sub_title, keys(app)
        seen["cursor"] = str(cursor_key(app))
        seen["shown"] = everything_shown(app)
        state["fail"] = False
        await pilot.press("r")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        seen["after"] = status(app)

    drive(script, reader=reader)  # type: ignore[arg-type]
    assert "STALE: read failed (internal: state.db is locked<U+001B>[2J)" in seen["status"]
    assert seen["sub"] == "STALE"
    assert seen["keys"] == ["P1", "P2", "P3", "P4", "P6"] and seen["cursor"] == "P2"
    assert "\x1b" not in str(seen["shown"])
    assert "STALE" not in seen["after"]


def test_a_first_read_that_fails_is_an_error_state_not_an_empty_one() -> None:
    def reader(cfg: LoadedConfig) -> bm.Snapshot:
        raise RuntimeError("secret stored text")

    seen: dict[str, str] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        seen["detail"], seen["status"] = detail(app), status(app)

    drive(script, reader=reader)
    assert "Cannot read the state" in seen["detail"]
    assert "internal error (RuntimeError)" in seen["detail"]
    assert "secret" not in seen["detail"] + seen["status"]
    assert "STALE" in seen["status"] and "no data read yet" in seen["status"]


def test_data_age_is_shown_and_grows() -> None:
    clock = [datetime(2026, 6, 1, tzinfo=UTC)]
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: object) -> None:
        seen.append(status(app))
        clock[0] = datetime(2026, 6, 1, 0, 5, tzinfo=UTC)
        app._render_status()
        seen.append(status(app))

    drive(script, clock=lambda: clock[0])
    assert "data 0s old" in seen[0] and "data 5m old" in seen[1]


def test_filters_by_status_label_and_text(store: Store) -> None:
    populate(store)
    seen: dict[str, object] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        press = pilot.press  # type: ignore[attr-defined]
        await press("s")
        seen["active"] = keys(app)
        await press("s")
        seen["terminal"] = keys(app)
        await press("s", "s")  # unreachable, then all
        await press("l", "l", "l")  # team=x, team=y, tier=1
        seen["label"] = keys(app)
        seen["status_line"] = status(app)
        await press("c")
        await press("slash")
        await press(*"o/s#3")
        seen["text"] = keys(app)
        await press("escape")
        seen["cleared"] = keys(app)
        await press("s", "c")
        seen["after_clear"] = keys(app)

    drive(script)
    assert seen["active"] == ["P1", "P4", "P6"]
    assert seen["terminal"] == ["P2"]
    assert seen["label"] == ["P1"]
    assert "label=tier=1" in str(seen["status_line"])
    assert seen["text"] == ["P3"]
    assert seen["cleared"] == ["P1", "P2", "P3", "P4", "P6"]
    assert seen["after_clear"] == seen["cleared"]


def test_no_match_has_an_explicit_state(store: Store) -> None:
    populate(store)
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: object) -> None:
        await pilot.press("slash", *"zzzz")  # type: ignore[attr-defined]
        seen.append(detail(app))

    drive(script)
    assert "Nothing matches the filters" in seen[0]


def test_focus_selects_the_named_pr(store: Store) -> None:
    populate(store)
    seen: list[str | None] = []

    async def script(app: BrowseApp, pilot: object) -> None:
        seen.append(cursor_key(app))

    drive(script, focus_pr_id="P3")
    assert seen == ["P3"]


def test_quit_keys_exit_cleanly_with_zero(store: Store) -> None:
    async def q(app: BrowseApp, pilot: object) -> None:
        await pilot.press("q")  # type: ignore[attr-defined]

    async def ctrl_c(app: BrowseApp, pilot: object) -> None:
        await pilot.press("ctrl+c")  # type: ignore[attr-defined]

    assert drive(q) == 0
    assert drive(ctrl_c) == 0


def test_help_opens_and_closes_without_quitting() -> None:
    seen: list[str] = []

    async def script(app: BrowseApp, pilot: object) -> None:
        await pilot.press("question_mark")  # type: ignore[attr-defined]
        seen.append(type(app.screen).__name__)
        await pilot.press("escape")  # type: ignore[attr-defined]
        seen.append(type(app.screen).__name__)

    assert drive(script) is None
    assert seen[0] == "HelpScreen" and seen[1] != "HelpScreen"


def test_an_unexpected_exception_exits_1_without_a_traceback() -> None:
    async def script(app: BrowseApp, pilot: object) -> None:
        def boom() -> None:
            raise ValueError("stored text")

        app._render_views = boom  # type: ignore[method-assign]
        app.call_later(
            app.action_refresh
        )  # the exception handler exits the app; nothing to wait for
        await asyncio.sleep(0.3)

    assert drive(script) == 1


def test_hostile_stored_text_never_reaches_the_screen(store: Store) -> None:
    evil = "x\x1b[31m‮yy\nz[bold]w"
    add_watch(store, "w-cccccccccc", "query", "mine+open;host=h", {"human:a": [f"k={evil}"]})
    add_pr(store, "P9", f"github.com/o/{evil}#9", dig=digest(), sources=())
    link(store, "P9", "w-cccccccccc")
    with store.transaction() as c:
        c.execute(
            "INSERT INTO events (id, type, pr_id, pr, payload, detected_at, labels) "
            "VALUES ('e1', ?, 'P9', ?, ?, ?, ?)",
            (f"t{evil}", f"github.com/o/{evil}#9", json.dumps({"note": evil}), T0,
             json.dumps([f"k={evil}"])),
        )  # fmt: skip
    seen: dict[str, str] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        shown = [everything_shown(app), app.export_screenshot()]
        for key in ("2", "3"):
            await pilot.press(key)  # type: ignore[attr-defined]
            await pilot.pause()  # type: ignore[attr-defined]
            shown += [everything_shown(app), app.export_screenshot()]
        seen["text"] = "\n".join(shown)

    drive(script)
    text = seen["text"]
    assert "\x1b" not in text and "‮" not in text
    assert "&lt;U+001B&gt;" in text and "&lt;U+202E&gt;" in text
    assert "[bold]" in text  # markup-looking text is shown literally, not interpreted


def test_a_browse_session_leaves_the_state_directory_byte_identical(
    store: Store, state_dir: Path
) -> None:
    populate(store)
    add_event(store)
    store.close()
    before = fingerprint(state_dir)

    async def script(app: BrowseApp, pilot: object) -> None:
        for key in ("r", "j", "2", "r", "3", "r", "1", "s", "l", "c"):
            await pilot.press(key)  # type: ignore[attr-defined]
            await pilot.pause()  # type: ignore[attr-defined]

    drive(script)
    after = fingerprint(state_dir)
    assert after["state.db"] == before["state.db"]
    # A reader may leave an empty -wal and a -shm (as `list`, `show` and `events` do); nothing
    # else in the directory changes and no data is written.
    litter = {k for k in set(after) | set(before) if after.get(k) != before.get(k)}
    assert litter <= {"state.db-wal", "state.db-shm"}
    wal = after.get("state.db-wal")
    assert wal is None or wal == before.get("state.db-wal") or wal[1] == 0


def focused_id(app: BrowseApp) -> str | None:
    return app.focused.id if app.focused is not None else None


def test_enter_on_the_table_focuses_the_detail_pane_and_arrows_scroll_it(store: Store) -> None:
    populate(store)
    seen: dict[str, object] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        press = pilot.press  # type: ignore[attr-defined]
        await press("enter")
        await pilot.pause()  # type: ignore[attr-defined]
        seen["focus"] = focused_id(app)
        seen["cursor"] = cursor_key(app)
        await press("down")
        seen["cursor_after"] = cursor_key(app)  # the arrow went to the pane, not the table
        await press("escape")
        seen["back"] = focused_id(app)

    drive(script)
    assert seen["focus"] == "detail-scroll"
    assert seen["cursor"] == seen["cursor_after"]
    assert seen["back"] == "table"


def test_escape_in_the_detail_pane_keeps_a_kept_text_filter(store: Store) -> None:
    populate(store)
    seen: dict[str, object] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        press = pilot.press  # type: ignore[attr-defined]
        await press("slash")
        await press(*"o/s#3")
        await press("enter")  # keeps the filter, focus to the table
        await press("enter")  # detail pane
        await press("escape")
        seen["after_escape"] = keys(app)
        seen["focus"] = focused_id(app)
        await press("escape")  # from the table: clears
        seen["cleared"] = keys(app)

    drive(script)
    assert seen["after_escape"] == ["P3"] and seen["focus"] == "table"
    assert seen["cleared"] == ["P1", "P2", "P3", "P4", "P6"]


def test_tab_in_the_filter_input_keeps_focus_there(store: Store) -> None:
    populate(store)
    seen: list[object] = []

    async def script(app: BrowseApp, pilot: object) -> None:
        press = pilot.press  # type: ignore[attr-defined]
        await press("slash")
        await press("tab")
        seen.append(focused_id(app))
        await press("shift+tab")
        seen.append(focused_id(app))
        seen.append(views(app))

    drive(script)
    assert seen[:2] == ["filter", "filter"]
    assert "[PRs 5]" in str(seen[2])


@pytest.mark.parametrize(
    ("number", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)]
)
def test_a_stop_signal_exits_128_plus_its_number(number: int, code: int) -> None:
    async def script(app: BrowseApp, pilot: object) -> None:
        os.kill(os.getpid(), number)
        await asyncio.sleep(0.3)

    assert drive(script) == code


def test_a_signal_before_mount_still_takes_the_exit_path() -> None:
    class Early(BrowseApp):
        def compose(self) -> ComposeResult:
            os.kill(os.getpid(), signal.SIGTERM)  # after the driver-less Load, before on_mount
            return super().compose()

    async def main() -> int | None:
        app = Early(load_config(None), refresh_seconds=3600.0)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.3)
        return app.return_value

    assert asyncio.run(asyncio.wait_for(main(), 60)) == 143


def test_a_full_events_window_is_marked(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bm, "EVENT_WINDOW", 3)
    for _ in range(4):
        add_event(store)
    seen: dict[str, str] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        await pilot.press("3")  # type: ignore[attr-defined]
        seen["views"], seen["status"] = views(app), status(app)

    drive(script)
    assert "[events 3+]" in seen["views"]
    assert "3+" in seen["status"]


def test_a_label_filter_that_vanishes_after_a_refresh_is_reset(store: Store) -> None:
    populate(store)
    seen: dict[str, object] = {}

    async def script(app: BrowseApp, pilot: object) -> None:
        press = pilot.press  # type: ignore[attr-defined]
        await press("l", "l", "l")  # tier=1
        seen["before"] = keys(app)
        with store.transaction() as c:
            c.execute("DELETE FROM labels WHERE label = 'tier=1'")
        await press("r")
        await pilot.pause()  # type: ignore[attr-defined]
        seen["after"] = keys(app)
        seen["status"] = status(app)

    drive(script)
    assert seen["before"] == ["P1"]
    assert "label=" not in str(seen["status"]) and len(seen["after"]) == 5  # type: ignore[arg-type]


def test_a_view_switch_starts_on_the_first_row(store: Store) -> None:
    populate(store)
    seen: list[str | None] = []

    async def script(app: BrowseApp, pilot: object) -> None:
        press = pilot.press  # type: ignore[attr-defined]
        await press("j", "j", "j", "j")
        await press("2")
        seen.append(cursor_key(app))

    drive(script)
    assert seen == ["w-aaaaaaaaaa"]
