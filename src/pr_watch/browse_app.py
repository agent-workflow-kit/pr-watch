"""The Textual application of `pr-watch browse` (docs/interface.md, TUI).

Only `browse.py` imports this module, and only inside its handler: nothing else in pr-watch
imports Textual, so `browse` can report a missing extra.

Read-only. Every read goes through `browse_model` (the read-only open: nothing is written or
created, no host is contacted). The app holds the last good snapshot; a failed re-read keeps it on
screen and marks it stale in the status line and the sub-title (never blanked). Reads are short
local queries run on the event loop, bounded by a one second busy timeout.

Untrusted text. Every string shown comes from `browse_model`, already sanitized, and reaches the
widgets as plain text: `rich.text.Text` cells (a `str` cell of a `DataTable` is parsed as markup)
and `Static(markup=False)`. Error messages are sanitized here the same way.

Terminal. Colors come from the terminal (Textual's `ansi-dark` theme, no hard-coded background).
SIGINT, SIGTERM and SIGHUP end the app through its normal exit path, so Textual's driver restores
the terminal; the exit code is 128 plus the signal number (docs/interface.md, Signals).
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

from . import browse_model as bm
from .cli import CliError
from .config import LoadedConfig
from .text import bounded_one_line

REFRESH_SECONDS: Final = 5.0
TITLE: Final = "pr-watch browse"
EMPTY_TEXT: Final = (
    "No watches, PRs or events stored yet.\n"
    "Add a watch with `pr-watch watch add`; `pr-watch poll` fills the store."
)
HELP_TEXT: Final = """\
j/k, arrows   move            enter  open the detail (arrows scroll it, escape returns)
tab, 1 2 3    PRs / watches / events      shift+tab  previous view
/             filter (text; enter keeps it, escape clears)
s             status filter (PRs: all, active, terminal, unreachable)
l             label filter (cycles the labels present)
c             clear every filter          r  refresh now
?             this help                   q  quit

View only: nothing here changes any state. CLI equivalents:
  PRs, watches, status/label filters   pr-watch list [--status S] [--label K=V]
  one PR's detail                      pr-watch show PR
  events                               pr-watch events / pr-watch export
"""
_TYPING_BLOCKS: Final = frozenset(
    {"view", "show_view", "status_filter", "label_filter", "clear_filters", "refresh", "help"}
    | {"cursor", "open"}
)
Reader = Callable[[LoadedConfig], bm.Snapshot]
DetailReader = Callable[[LoadedConfig, str], str | None]


def _now() -> datetime:
    return datetime.now(UTC)


def error_text(exc: BaseException) -> str:
    """One sanitized line naming a failed read: the CLI's own message for a `CliError`, else only
    the exception type (a library message could carry stored text)."""
    if isinstance(exc, CliError):
        return bounded_one_line(f"{exc.code}: {exc.message}")
    return f"internal error ({type(exc).__name__})"


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar = [Binding("escape,q,question_mark,enter", "close", "close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 90%; max-width: 100; height: auto; border: thick $primary; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, markup=False, id="help")

    def action_close(self) -> None:
        self.dismiss(None)


class BrowseApp(App[int]):
    ENABLE_COMMAND_PALETTE = False
    TITLE = TITLE
    CSS = """
    #views { height: 1; padding: 0 1; }
    #table { height: 1fr; border-bottom: solid $primary; }
    #detail-scroll { height: 1fr; }
    #detail { padding: 0 1; }
    #filter { dock: bottom; }
    #status { dock: bottom; height: 1; padding: 0 1; }
    """
    BINDINGS: ClassVar = [
        Binding("j", "cursor(1)", "down", show=False),
        Binding("k", "cursor(-1)", "up", show=False),
        Binding("tab", "view(1)", "view", priority=True),
        Binding("shift+tab", "view(-1)", "prev view", show=False, priority=True),
        Binding("1", "show_view('prs')", "PRs", show=False),
        Binding("2", "show_view('watches')", "watches", show=False),
        Binding("3", "show_view('events')", "events", show=False),
        Binding("enter", "open", "open", show=False),
        Binding("escape", "dismiss_filter", "back", show=False),
        Binding("slash", "filter", "filter"),
        Binding("s", "status_filter", "status"),
        Binding("l", "label_filter", "label"),
        Binding("c", "clear_filters", "clear", show=False),
        Binding("r", "refresh", "refresh"),
        Binding("question_mark", "help", "help"),
        Binding("q", "quit_app", "quit"),
        Binding("ctrl+c", "quit_app", "quit", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "quit", show=False, priority=True),
    ]

    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        reader: Reader = bm.load_snapshot,
        detail_reader: DetailReader | None = None,
        focus_pr_id: str | None = None,
        refresh_seconds: float = REFRESH_SECONDS,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        super().__init__()
        self.theme = "ansi-dark"  # the terminal's own colors
        self._loaded = loaded
        self._reader = reader
        self._detail_reader: DetailReader = detail_reader or (
            lambda cfg, pr_id: bm.load_pr_detail(cfg, pr_id)
        )
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._snap = bm.Snapshot()
        self._read_ok_at: datetime | None = None
        self._error: str | None = None
        self._view = "prs"
        self._selected: dict[str, str | None] = {v: None for v in bm.VIEWS}
        self._focus = focus_pr_id
        self._text = ""
        self._status: str | None = None
        self._label: str | None = None
        self._shown: list[bm.Item] = []
        self._detail_note = ""
        self.exit_message: str | None = None  # a line for stderr after the terminal is restored

    # -- layout --------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", markup=False, id="views")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="detail-scroll"):
            yield Static("", markup=False, id="detail")
        yield Input(placeholder="filter text", id="filter")
        yield Static("", markup=False, id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#filter", Input).display = False
        self.query_one("#detail-scroll").can_focus = True
        self.query_one("#table", DataTable).focus()
        self._install_signal_handlers()
        self.action_refresh()
        self.set_interval(self._refresh_seconds, self.action_refresh)
        self.set_interval(1.0, self._render_status)

    # -- signals and failures ------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(number, self._signaled, number)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # not the main thread of a Unix loop (headless tests): defaults stay

    def _remove_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.remove_signal_handler(number)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    def _signaled(self, number: int) -> None:
        self._finish(128 + number)

    def on_unmount(self) -> None:
        self._remove_signal_handlers()

    def _handle_exception(self, error: Exception) -> None:
        # Textual would print a traceback (with local variables, which can hold stored text);
        # report the type only and exit 1.
        self.exit_message = f"internal error ({type(error).__name__})"
        self._finish(1)

    def _finish(self, code: int) -> None:
        try:
            self.exit(code)
        except Exception:  # the screen stack is already gone
            pass

    def action_quit_app(self) -> None:
        self._finish(0)

    # -- reading -------------------------------------------------------------------------

    def action_refresh(self) -> None:
        try:
            snap = self._reader(self._loaded)
        except Exception as exc:
            self._error = error_text(exc)
        else:
            self._snap, self._read_ok_at, self._error = snap, self._clock(), None
        self._populate()

    # -- tables --------------------------------------------------------------------------

    def _table(self) -> DataTable[Text]:
        return self.query_one("#table", DataTable)

    def _items(self) -> list[bm.Item]:
        return bm.visible(
            self._snap.items(self._view),
            text=self._text,
            status=self._status if self._view == "prs" else None,
            label=self._label,
        )

    def _populate(self, *, columns: bool = False) -> None:
        """Fill the table from the snapshot and the filters, keeping the cursor on the same key."""
        table = self._table()
        items = self._items()
        previous_index = table.cursor_row
        want = self._selected[self._view]
        if self._focus is not None and self._view == "prs":
            if any(i.key == self._focus for i in self._snap.prs):
                want, self._focus = self._focus, None
                self._text, self._status, self._label = "", None, None
                items = self._items()
        self._shown = items
        with table.prevent(DataTable.RowHighlighted):
            table.clear(columns=columns)
            if columns or not table.columns:
                table.add_columns(*bm.COLUMNS[self._view])
            for item in items:
                table.add_row(*(Text(c) for c in item.cells), key=item.key)
            if items:
                keys = [i.key for i in items]
                row = keys.index(want) if want in keys else min(previous_index, len(items) - 1)
                table.move_cursor(row=max(row, 0), animate=False)
        self._selected[self._view] = items[table.cursor_row].key if items else None
        self._render_views()
        self._render_detail()
        self._render_status()

    def _current(self) -> bm.Item | None:
        key = self._selected[self._view]
        return next((i for i in self._shown if i.key == key), None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._selected[self._view] = key
        self._render_detail()

    # -- rendering -----------------------------------------------------------------------

    def _render_views(self) -> None:
        names = {"prs": "PRs", "watches": "watches", "events": "events"}
        parts = [
            f"[{names[v]} {len(self._snap.items(v))}]" if v == self._view else f"{names[v]}"
            for v in bm.VIEWS
        ]
        self.query_one("#views", Static).update("  ".join(parts))

    def _render_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        item = self._current()
        if item is None:
            if self._read_ok_at is None and self._error is not None:
                text = f"Cannot read the state.\n{self._error}"
            elif self._snap.empty:
                text = EMPTY_TEXT
            else:
                text = "Nothing matches the filters (c clears them)."
        else:
            text = item.detail
            if self._view == "prs":
                text = self._pr_detail(item)
        detail.update(text)

    def _pr_detail(self, item: bm.Item) -> str:
        try:
            full = self._detail_reader(self._loaded, item.key)
        except Exception as exc:
            return f"{item.detail}\n\n(detail unavailable: {error_text(exc)})"
        return item.detail if full is None else full

    def _render_status(self) -> None:
        now = self._clock()
        shown = len(self._items())
        total = len(self._snap.items(self._view))
        parts = [f"{shown}/{total} shown"]
        if self._read_ok_at is not None:
            parts.append(f"data {bm.age_text(self._read_ok_at, now)} old")
        else:
            parts.append("no data read yet")
        if self._snap.last_poll is not None:
            parts.append(f"last poll {self._snap.last_poll}")
        elif not self._snap.empty or self._read_ok_at is not None:
            parts.append("never polled")
        filters = [
            f"text={bounded_one_line(self._text, 40)}" if self._text else "",
            f"status={self._status}" if self._status else "",
            f"label={bounded_one_line(self._label, 40)}" if self._label else "",
        ]
        if any(filters):
            parts.append("filter: " + " ".join(f for f in filters if f))
        line = "  |  ".join(parts)
        if self._error is not None:
            line = f"STALE: read failed ({self._error})  |  {line}"
        self.sub_title = "STALE" if self._error is not None else ""
        self.query_one("#status", Static).update(line)

    # -- actions -------------------------------------------------------------------------

    def action_cursor(self, step: int) -> None:
        table = self._table()
        if step > 0:
            table.action_cursor_down()
        else:
            table.action_cursor_up()

    def action_show_view(self, view: str) -> None:
        if view == self._view:
            return
        self._view = view
        self._label = (
            None if self._label not in bm.label_choices(self._snap.items(view)) else self._label
        )
        self._populate(columns=True)

    def action_view(self, step: int) -> None:
        index = (bm.VIEWS.index(self._view) + step) % len(bm.VIEWS)
        self.action_show_view(bm.VIEWS[index])

    def action_open(self) -> None:
        if isinstance(self.focused, Input):
            return
        self.query_one("#detail-scroll").focus()

    def action_dismiss_filter(self) -> None:
        filt = self.query_one("#filter", Input)
        if filt.display:
            self._text = ""
            filt.value = ""
            filt.display = False
            self._table().focus()
            self._populate()
        else:
            self._table().focus()

    def action_filter(self) -> None:
        filt = self.query_one("#filter", Input)
        filt.display = True
        filt.value = self._text
        filt.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._text = event.value
        self._populate()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one("#filter", Input).display = bool(self._text)
        self._table().focus()

    def action_status_filter(self) -> None:
        if self._view != "prs":
            self.notify("the status filter applies to PRs", timeout=2)
            return
        cycle: list[str | None] = [None, *bm.STATUSES]
        self._status = cycle[(cycle.index(self._status) + 1) % len(cycle)]
        self._populate()

    def action_label_filter(self) -> None:
        cycle: list[str | None] = [None, *bm.label_choices(self._snap.items(self._view))]
        at = cycle.index(self._label) if self._label in cycle else 0
        self._label = cycle[(at + 1) % len(cycle)]
        self._populate()

    def action_clear_filters(self) -> None:
        self._text, self._status, self._label = "", None, None
        filt = self.query_one("#filter", Input)
        filt.value = ""
        filt.display = False
        self._populate()

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def check_action(self, action: str, parameters: tuple[Any, ...]) -> bool | None:
        # while the filter input has focus, the view keys must not act (typing goes to the input)
        if isinstance(self.focused, Input) and action in _TYPING_BLOCKS:
            return False
        return True
