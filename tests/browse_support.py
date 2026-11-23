"""Headless driving of the browse app (Textual's `run_test` pilot) and reading what it shows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from textual.pilot import Pilot
from textual.widgets import DataTable, Static

from pr_watch.browse_app import BrowseApp
from pr_watch.config import load_config

Script = Callable[[BrowseApp, "Pilot[int]"], Awaitable[None]]


def drive(script: Script, *, refresh: float = 3600.0, **kw: Any) -> int | None:
    """Run `script` against a running app; returns what the app returned (None while it still
    runs when the script ends: the pilot then closes it)."""

    async def main() -> int | None:
        app = BrowseApp(load_config(None), refresh_seconds=refresh, **kw)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await script(app, pilot)
            if app.is_running:
                await pilot.pause()
        return app.return_value

    return asyncio.run(asyncio.wait_for(main(), 60))


def rows(app: BrowseApp) -> list[list[str]]:
    table = app.query_one("#table", DataTable)
    return [[c.plain for c in table.get_row_at(i)] for i in range(table.row_count)]


def keys(app: BrowseApp) -> list[str]:
    table = app.query_one("#table", DataTable)
    return [str(k.value) for k in table.rows]


def cursor_key(app: BrowseApp) -> str | None:
    table = app.query_one("#table", DataTable)
    if not table.row_count:
        return None
    return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)


def detail(app: BrowseApp) -> str:
    return app.query_one("#detail", Static).visual.plain  # type: ignore[attr-defined]


def status(app: BrowseApp) -> str:
    return app.query_one("#status", Static).visual.plain  # type: ignore[attr-defined]


def views(app: BrowseApp) -> str:
    return app.query_one("#views", Static).visual.plain  # type: ignore[attr-defined]


def everything_shown(app: BrowseApp) -> str:
    return "\n".join(
        [*(" ".join(r) for r in rows(app)), detail(app), status(app), views(app), app.sub_title]
    )
