"""`validate`: an offline, read-only check of the configuration and the environment.

It loads the config (already done by the CLI frame: an invalid one never gets here), resolves
forgectl on the absolute `PATH` entries and runs `forgectl --version` against the pinned minimum
(a local child process, no network), and verifies the state directory when it exists. It creates
nothing (no state directory, no `state.db`, no log record), never prompts and never reads or
writes the database. A failed check is the error envelope of the same failure elsewhere
(`config_invalid`, `tool_missing`, `tool_too_old`, `state_unsafe`); a tolerated oddity is a
warning, and the command still exits 0.

`--probe` adds one `forgectl whoami` call (the only call that may reach a host) and warns about a
`hosts.<host>` key forgectl does not know.
"""

from __future__ import annotations

import argparse
from typing import Any

from .cli import Context
from .failures import decide, decide_tool, to_cli_error
from .forgectl_client import ForgectlClient, ToolError
from .hooks import collect_specs
from .text import one_line

# `forgectl --version` is a local call: a hung one ends validate after this many seconds
# (`tool_missing`) instead of after `forgectl.timeout` plus the deadline slack.
VERSION_DEADLINE = 10.0


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _check(name: str, message: str) -> dict[str, Any]:
    return {"name": name, "ok": True, "message": one_line(message, 256)}


def _warning(code: str, key: str, message: str) -> dict[str, str]:
    return {"code": code, "key": one_line(key, 256), "message": one_line(message, 256)}


def _probe(
    ctx: Context, client: ForgectlClient, warnings: list[dict[str, str]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        outcome = client.whoami()
    except ToolError as exc:
        raise to_cli_error(decide_tool(exc)) from None
    decision = decide(outcome, "whoami")
    if decision.effect.cli_code is not None or outcome.result is None:
        raise to_cli_error(decision)
    result = outcome.result
    known = {
        str(item["host"]).lower()
        for item in result.get("hosts", [])
        if isinstance(item, dict) and isinstance(item.get("host"), str)
    }
    if result.get("errors"):
        warnings.append(
            _warning("probe_partial", "forgectl", "whoami reported errors for some hosts")
        )
    else:
        for host in sorted(ctx.loaded.config.hosts):
            if host.lower() not in known:
                warnings.append(
                    _warning("host_unknown", f"hosts.{host}", "forgectl does not know this host")
                )
    count = len(known)
    return _check("probe", f"forgectl knows {_plural(count, 'host')}"), {"hosts": count}


def validate_command(ctx: Context, args: argparse.Namespace) -> int:
    config = ctx.loaded.config
    warnings = [_warning(w.code, w.key, w.message) for w in ctx.loaded.warnings]
    hooks = sum(len(specs) for specs in collect_specs(config).values())
    checks = [
        _check(
            "config",
            f"{_plural(len(ctx.loaded.files), 'file')}, "
            f"{_plural(len(config.hosts), 'host')}, {_plural(hooks, 'hook')}",
        )
    ]
    try:
        client = ForgectlClient.from_config(config.forgectl)
        version = client.version(deadline=VERSION_DEADLINE)
    except ToolError as exc:
        raise to_cli_error(decide_tool(exc)) from None
    checks.append(_check("forgectl", f"forgectl {version.text}"))
    # `_execute` opened the state directory without creating it: None means it does not exist.
    checks.append(
        _check(
            "state_dir",
            "not created yet" if ctx.state is None else "exists and is safe",
        )
    )
    probe: dict[str, Any] | None = None
    if args.probe:
        check, probe = _probe(ctx, client, warnings)
        checks.append(check)
    doc = {
        "ok": True,
        "checks": checks,
        "forgectl": {"path": client.exe, "version": version.text},
        "probe": probe,
        "warnings": warnings,
    }
    lines = [f"{c['name']}: ok: {c['message']}" for c in checks]
    lines += [f"warning: {w['key']}: {w['message']}" for w in warnings]
    ctx.emit(doc, human="\n".join(lines))
    return 0
