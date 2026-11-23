"""`browse`: a read-only terminal browser over watches, PRs and events (docs/interface.md, TUI).

The TUI is the optional `browse` extra. Textual is imported only inside the handler (every command
module is imported lazily from `cli`, and `browse_model` needs no Textual), so without the extra
the command exits 2 (`usage`, `details.kind: tui_missing`) with an install hint and nothing else
in pr-watch needs the library.

Order of the checks: `--json` (refused), the extra, a terminal on stdin and stdout, `--focus`, then
the TUI. A store that cannot be read is not an exit code: the TUI shows it as a failed read.
"""

from __future__ import annotations

import argparse
import sys

from .cli import CliError, Context, UsageError
from .text import one_line

INSTALL = "uv tool install 'pr-watch[browse] @ git+https://github.com/agent-workflow-kit/pr-watch'"


def _import_app() -> object:
    try:
        import textual  # noqa: F401  (the extra; nothing else in pr-watch imports it)
    except ImportError:
        raise UsageError(
            "browse needs the optional `browse` extra (Textual): " + INSTALL,
            kind="tui_missing",
            details={"install": INSTALL},
        ) from None
    from . import browse_app

    return browse_app


def _is_tty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False


def browse_command(ctx: Context, args: argparse.Namespace) -> int:
    if ctx.json:
        raise UsageError("browse is a terminal UI and has no --json form", kind="bad_combination")
    app_module = _import_app()
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        raise UsageError("browse needs a terminal on standard input and output", kind="no_terminal")
    from . import browse_model

    focus: str | None = None
    if args.focus is not None:
        focus = browse_model.resolve_focus(ctx.loaded, args.focus)
        if focus is None:
            raise CliError("not_found", "no stored PR matches the reference")
    app = app_module.BrowseApp(ctx.loaded, focus_pr_id=focus)  # type: ignore[attr-defined]
    code = int(app.run() or 0)
    if app.exit_message is not None:
        sys.stderr.write(f"pr-watch: error: {one_line(app.exit_message)}\n")
    return code
