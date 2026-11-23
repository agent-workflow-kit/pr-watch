"""The real forgectl's contract as pr-watch uses it (#8, #6 and #24 unverified-live lists, V1)."""

from __future__ import annotations

import re
import signal
from collections.abc import Callable
from typing import Any

import pytest

from tests.forgectl_support import assert_forgectl_valid
from tests.live.support import (
    HostOps,
    Result,
    Workspace,
    assert_no_leak,
    forgectl_new_enough,
    wait_until,
)

pytestmark = pytest.mark.live

Rec = Callable[..., None]


def _head_is(ws: Workspace, ref: str, sha: str) -> bool:
    return ws.snapshot(ref)["head"]["sha"] == sha


def test_argv_grammar_schemas_and_version(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#8 items 1-3 and 8: every subcommand with `--opt=value` flags after it, `--timeout=<N>ms`,
    `--version` plain text, outputs valid against forgectl's pinned schemas, compare echoes the
    requested lowercase shas and omits `diff` exactly under `--no-diff`."""
    version = ws.forgectl("--version")
    assert version.rc == 0 and re.fullmatch(r"forgectl [0-9]+\.[0-9]+\.[0-9]+\n", version.out), (
        version.out
    )
    assert forgectl_new_enough(version.out), f"forgectl >= 0.55 is required, got {version.out!r}"
    pr = ops.open_pr("grammar")
    second = ops.add_commit(pr.branch)
    wait_until(lambda: _head_is(ws, pr.ref, second), timeout=90, what="head moved on host")

    listing = ws.fg_json("prs", f"--repo={ops.repo_id}", "--state=open", "--timeout=60000ms")
    assert_forgectl_valid("prs", listing)
    assert any(i["number"] == pr.number for i in listing["items"])
    assert_forgectl_valid("show", ws.fg_json("show", pr.ref))
    snap = ws.fg_json("snapshot", pr.ref, "--no-bodies")
    assert_forgectl_valid("snapshot", snap)
    who = ws.fg_json("whoami", f"--host={ops.cfg.host}")
    assert_forgectl_valid("whoami", who)

    ahead = ws.fg_json("compare", pr.ref, f"--from={pr.head_sha}", f"--to={second}")
    assert_forgectl_valid("compare", ahead)
    assert (ahead["from_sha"], ahead["to_sha"], ahead["status"]) == (pr.head_sha, second, "ahead")
    assert isinstance(ahead["diff"], str) and ahead["diff"]
    nodiff = ws.fg_json("compare", pr.ref, f"--from={pr.head_sha}", f"--to={second}", "--no-diff")
    assert_forgectl_valid("compare", nodiff)
    assert "diff" not in nodiff and nodiff["status"] == "ahead"
    same = ws.fg_json("compare", pr.ref, f"--from={second}", f"--to={second}", "--no-diff")
    assert same["status"] == "identical" and "diff" not in same
    rec(host=ops.kind, forgectl=version.out.strip(), compare="ahead/identical ok")


def test_v1_double_dash_before_positionals(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """V1: does forgectl accept `--` before a positional? Either answer is recorded; pr-watch
    never relies on it. A third outcome (anything but accept or usage refusal) fails."""
    pr = ops.open_pr("v1")
    plain = ws.fg_json("snapshot", pr.ref, "--no-bodies")
    res = ws.forgectl("snapshot", "--json", "--no-bodies", "--", pr.ref)
    if res.rc == 0:
        assert res.json()["pr_id"] == plain["pr_id"]
        accepted = True
    else:
        assert res.rc == 2, f"unexpected exit {res.rc}: {res.out[:300]} {res.err[:300]}"
        accepted = False
    rec(host=ops.kind, V1_double_dash_accepted=accepted, rc=res.rc)


def test_error_envelopes_and_codes(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#6/#8: real exit codes and `error` envelopes (types of `retry_after` and `details`)."""
    pr = ops.open_pr("errors")
    seen: dict[str, str] = {}
    results: list[Result] = []

    def envelope(res: Result, want_rc: int, want_code: str) -> dict[str, Any]:
        results.append(res)
        assert res.rc == want_rc, f"{want_code}: exit {res.rc}: {res.out[:300]} {res.err[:300]}"
        err = res.json()["error"]
        assert err["code"] == want_code, err
        retry_after = err.get("retry_after")  # the schema does not require the key
        assert retry_after is None or isinstance(retry_after, (int, float))
        assert isinstance(err.get("details", {}), dict)
        assert len(res.err) < 4000
        seen[want_code] = str(err.get("details"))[:120]
        return err

    missing = ws.forgectl("snapshot", f"{ops.repo_id}#999999", "--json")
    envelope(missing, 10, "not_found")
    envelope(ws.forgectl("snapshot", pr.ref, "--json", tokens=False), 12, "auth_required")
    unknown = ws.forgectl("snapshot", "unknown.example.invalid/o/r#1", "--json")
    err = envelope(unknown, 20, "refused")
    assert err.get("reason") == "unknown_host"
    envelope(ws.forgectl("snapshot", "--json"), 2, "usage")
    env = ws.tool_env()
    env["FORGECTL_GH_BIN" if ops.kind == "github" else "FORGECTL_GLAB_BIN"] = "/nonexistent/cli"
    res = ws.run([str(ws.env.forgectl), "snapshot", pr.ref, "--json"], env=env, timeout=60)
    envelope(res, 12, "tool_missing")
    long_sha = "a" * 64
    big = ws.forgectl("compare", pr.ref, f"--from={long_sha}", f"--to={long_sha}", "--json")
    results.append(big)
    assert big.rc == 2, f"64-hex sha: exit {big.rc}: {big.out[:200]}"
    assert_no_leak(*results)
    rec(host=ops.kind, **{k: v.replace(" ", "_") for k, v in seen.items()})


def test_timeout_budget_and_zero(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#6/#8 item 7: an exhausted `--timeout` ends forgectl within the budget plus 10 s with a
    network_error envelope (exit 12); `--timeout=0ms` is a usage error."""
    pr = ops.open_pr("timeout")
    res = ws.forgectl("snapshot", pr.ref, "--no-bodies", "--json", "--timeout=1ms")
    assert res.rc == 12 and res.elapsed <= 0.001 + 10.0, (res.rc, res.elapsed, res.out[:300])
    code = res.json()["error"]["code"]
    assert code == "network_error", code
    zero = ws.forgectl("snapshot", pr.ref, "--json", "--timeout=0ms")
    assert zero.rc == 2, (zero.rc, zero.out[:200])
    assert_no_leak(res, zero)
    rec(host=ops.kind, timeout_1ms_code=code, elapsed_s=round(res.elapsed, 2))


def test_sigterm_exit_status_and_latency(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """#6/#8 item 6: SIGTERM to forgectl (the exact child started here) ends it with 143 (or -15
    in the startup window) within 5 s."""
    pr = ops.open_pr("sigterm")
    res = ws.run(
        [str(ws.env.forgectl), "snapshot", pr.ref, "--json"],
        env=ws.tool_env(),
        timeout=20,
        signal_after=0.3,
        sig=signal.SIGTERM,
    )
    assert res.latency is not None, "forgectl ended before SIGTERM was sent; nothing was tested"
    assert res.rc in (143, -15), (res.rc, res.out[:200], res.err[:200])
    assert res.latency < 5.0, f"SIGTERM took {res.latency:.1f}s"
    assert_no_leak(res)
    rec(host=ops.kind, sigterm_rc=res.rc, latency_s=round(res.latency, 2))


def test_whoami_with_and_without_credential(ws: Workspace, ops: HostOps, rec: Rec) -> None:
    """`whoami`: the token's login and `token_source`, then a host with no usable credential."""
    login, _ = ops.user()
    who = ws.fg_json("whoami", f"--host={ops.cfg.host}")
    assert_forgectl_valid("whoami", who)
    entry = who["hosts"][0]
    assert entry["login"].lower() == login.lower() and entry["token_source"] == "token_env"
    assert entry["backend"] == ops.kind
    ws.write_forgectl_config(token_env=False)
    try:
        anon = ws.forgectl("whoami", f"--host={ops.cfg.host}", "--json", tokens=False)
        assert anon.rc in (0, 12), (anon.rc, anon.out[:300], anon.err[:300])
        doc = anon.json()
        if "hosts" in doc:
            assert_forgectl_valid("whoami", doc)
        rec(
            host=ops.kind,
            scopes=entry.get("scopes"),
            anon_rc=anon.rc,
            anon_token_source=(doc.get("hosts") or [{}])[0].get("token_source"),
            anon_errors=len(doc.get("errors") or []),
        )
    finally:
        ws.write_forgectl_config()
