"""`show`: one stored PR, its watches, its digest summary and its latest events (docs/interface.md).

Offline and read-only, in one read transaction. Only a stored row is shown (a PR that only has
events is exit 10). The digest is stored data, so every field is re-checked on the way out and a
value that does not fit the schema becomes null (or is left out of `reviewers`).

Judgment calls where the interface is silent:

- A reviewer entry whose state is not one of the schema's (the digest's `unknown` placeholder)
  is left out of `reviewers`: it counts as no entry (Events, `approved`).
- A reviewer's `id` is its key, null for a `login:` key (Events, reviewer); its `at` is printed
  as UTC microsecond text (the digest keeps the host's form), null when it does not parse.
- `reviewers` are ordered by key, watches by `created_at` then id.
- `seen` counts the ids in the digest's lists (0 with no digest).
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Final

from .cli import CliError, Context, ref_error_to_cli, store_error_to_cli
from .digest import digest_from_json, instant
from .events import read_snapshot, stored_event
from .refs import RefError, duration_ms
from .rows import pr_row
from .store import classify_error, format_ts
from .text import bounded_one_line
from .watches import resolve_stored_pr, watch_view

RECENT: Final = 20
_CODE = re.compile(r"[a-z_]{1,64}")
_DECISION = re.compile(r"[a-z_]{1,32}")
_KEY = re.compile(r"[A-Za-z0-9:/_=-]{1,128}|login:.{1,256}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_STATES: Final = ("approved", "changes_requested", "dismissed")


def _now() -> datetime:
    return datetime.now(UTC)


def _blocked_by(d: dict[str, Any]) -> list[dict[str, str]] | None:
    raw = d.get("blocked_by")
    if not isinstance(raw, list):
        return None
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        code, detail = item.get("code"), item.get("detail")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            return None
        entry = {"code": code}
        if isinstance(detail, str) and len(detail) <= 256:
            entry["detail"] = detail
        out.append(entry)
    return out


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _reviewers(d: dict[str, Any]) -> list[dict[str, Any]]:
    raw = d.get("reviewers")
    if not isinstance(raw, dict):
        return []
    out: list[dict[str, Any]] = []
    for key in sorted(k for k in raw if isinstance(k, str)):
        entry = raw[key]
        if not (_KEY.fullmatch(key) and isinstance(entry, dict) and entry.get("state") in _STATES):
            continue
        sha, at, can_push = entry.get("commit_sha"), instant(entry.get("at")), entry.get("can_push")
        out.append(
            {
                "key": key,
                "id": None if key.startswith("login:") else key,
                "state": entry["state"],
                "commit_sha": sha if isinstance(sha, str) and _SHA.fullmatch(sha) else None,
                "at": None if at is None else format_ts(at),
                "can_push": can_push if isinstance(can_push, bool) else None,
            }
        )
    return out


def _seen(d: dict[str, Any]) -> dict[str, int]:
    seen = d.get("seen")
    seen = seen if isinstance(seen, dict) else {}
    return {
        k: len(seen[k]) if isinstance(seen.get(k), list) else 0 for k in ("comments", "reviews")
    }


def build(ctx: Context, args: argparse.Namespace, *, now: datetime | None = None) -> dict[str, Any]:
    store = ctx.store
    if store is None:
        raise CliError("not_found", "no stored PR matches the reference")
    conn = store.conn
    now = _now() if now is None else now
    pending_ms = duration_ms(ctx.loaded.config.ci.pending_max_age)
    try:
        with read_snapshot(conn):
            try:
                found = resolve_stored_pr(conn, args.pr)
            except RefError as exc:
                raise ref_error_to_cli(exc) from None
            if found is None:
                raise CliError("not_found", "no stored PR matches the reference")
            pr_id = found[0]
            row = pr_row(conn, pr_id, now=now, pending_max_age_ms=pending_ms)
            if row is None:
                raise CliError("not_found", "no stored PR matches the reference")
            text = conn.execute("SELECT digest FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()
            digest = digest_from_json(None if text is None else text[0])
            watch_ids = [
                str(r[0])
                for r in conn.execute("SELECT watch_id FROM sources WHERE pr_id = ?", (pr_id,))
            ]
            views = [v for w in watch_ids if (v := watch_view(conn, w)) is not None]
            views.sort(key=lambda v: (v["created_at"], v["id"]))
            recent = conn.execute(
                "SELECT seq, id, type, pr_id, pr, payload, detected_at, labels FROM events "
                "WHERE pr_id = ? ORDER BY seq DESC LIMIT ?",
                (pr_id, RECENT),
            ).fetchall()
    except sqlite3.Error as exc:
        raise store_error_to_cli(classify_error(exc)) from None
    d = digest if digest is not None else {}
    return {
        **row,
        "blocked_by": None if digest is None else _blocked_by(d),
        "review_decision": (
            d["review_decision"]
            if isinstance(d.get("review_decision"), str)
            and _DECISION.fullmatch(d["review_decision"])
            else None
        ),
        "approvals_at_head": _count(d.get("approvals_at_head")),
        "reviewers": _reviewers(d),
        "seen": _seen(d),
        "watches": views,
        "events": [stored_event(r).to_json() for r in reversed(recent)],
    }


# --- human text ----------------------------------------------------------------------------------


def _s(value: object) -> str:
    return "-" if value is None else bounded_one_line(str(value))


def render(d: dict[str, Any]) -> str:
    from .events import event_line  # shared one-line event form

    ci = "stale" if d["ci_stale"] else _s(d["ci_state"])
    blocked = (
        "-"
        if not d["blocked_by"]
        else ",".join(
            _s(b["code"]) + (f"({_s(b['detail'])})" if "detail" in b else "")
            for b in d["blocked_by"]
        )
    )
    lines = [
        _s(d["pr"]),
        f"  pr_id: {_s(d['pr_id'])}  status: {_s(d['status'])}  state: {_s(d['state'])}"
        f"  draft: {_s(d['draft'])}",
        f"  ci: {ci}  head: {_s(d['head_sha'])[:12]}  polled: {_s(d['last_polled_at'])}"
        f"  failures: {d['failures']}",
        f"  review: {_s(d['review_decision'])}  approvals_at_head: {_s(d['approvals_at_head'])}"
        f"  blocked_by: {blocked}",
        f"  seen: comments={d['seen']['comments']} reviews={d['seen']['reviews']}",
        f"reviewers ({len(d['reviewers'])})",
    ]
    for r in d["reviewers"]:
        lines.append(
            f"  {_s(r['key'])}  {_s(r['state'])}  at={_s(r['at'])}"
            f"  commit={_s(r['commit_sha'])[:12]}"
        )
    lines.append(f"watches ({len(d['watches'])})")
    for w in d["watches"]:
        subject = w["pr"] if w["kind"] == "pr" and w["pr"] is not None else w["key"]
        lines.append(
            f"  {_s(w['id'])}  {_s(w['kind'])}  {_s(subject)}"
            f"  owners={','.join(_s(o) for o in w['owners'])}"
            f"  labels={','.join(_s(x) for x in w['labels']) or '-'}"
        )
    lines.append(f"events ({len(d['events'])})")
    lines += [event_line(e) for e in d["events"]]
    return "\n".join(lines)


def show_command(ctx: Context, args: argparse.Namespace) -> int:
    data = build(ctx, args)
    ctx.emit(data, human=render(data))
    return 0
