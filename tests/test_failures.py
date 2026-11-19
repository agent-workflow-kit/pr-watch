"""The failure table (docs/interface.md: Failures): every outcome of a forgectl call, decided.

The table is data (`failures.ROWS`); these tests pin it to the interface table row by row, to the
pinned forgectl error codes, to the CLI error codes and to the error envelope schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pr_watch import failures as f
from pr_watch.cli import EXIT_CODES, CliError, build_envelope
from pr_watch.forgectl_client import (
    INSTALL,
    ForgectlError,
    Outcome,
    ToolMissing,
    ToolTooOld,
)
from tests.fakebin._forgectl_fake import ALLOWED_CODES
from tests.forgectl_support import SCHEMAS_DIR
from tests.harness import validator

ROOT = Path(__file__).resolve().parent.parent
CALLS = ("pr", "repo", "query", "whoami")


def err(code: str, reason: str | None = None, retry_after: float | None = None) -> ForgectlError:
    return ForgectlError(code=code, message="m", reason=reason, retry_after=retry_after)


def out(code: str, *, reason: str | None = None, retry_after: float | None = None) -> Outcome:
    exit_code = {"not_found": 10, "usage": 2, "config_invalid": 2, "invalid_pr": 2}.get(code, 12)
    if reason is not None:
        exit_code = 20
        code = "refused"
    return Outcome(command="snapshot", returncode=exit_code, error=err(code, reason, retry_after))


def raw(returncode: int) -> Outcome:
    return Outcome(command="snapshot", returncode=returncode)


def local(name: str, returncode: int | None = None) -> Outcome:
    return Outcome(command="snapshot", returncode=returncode, local=name, detail="d")


# --- the table is the interface's table ---------------------------------------------------------


def interface_rows() -> list[str]:
    text = (ROOT / "docs" / "interface.md").read_text(encoding="utf-8")
    section = text.split("### Failures", 1)[1].split("\n- **Counters.**", 1)[0]
    lines = [ln for ln in section.splitlines() if ln.startswith("|")]
    return [ln.split("|")[1].strip() for ln in lines[2:]]  # drop the header and the rule


def test_the_interface_table_has_the_rows_we_know() -> None:
    assert len(interface_rows()) == 12


def test_every_interface_row_is_covered_and_every_doc_key_is_used() -> None:
    cells = interface_rows()
    keys = {row.doc for row in f.ROWS.values() if row.doc is not None}
    for cell in cells:
        assert sum(cell.startswith(key) for key in keys) == 1, cell
    for key in keys:
        assert any(cell.startswith(key) for cell in cells), key


def test_rows_not_in_the_interface_table_are_named() -> None:
    assert {r for r, row in f.ROWS.items() if row.doc is None} == {"partial"}


# --- error codes ----------------------------------------------------------------------------


def pinned_codes() -> set[str]:
    import json

    defs = json.loads((SCHEMAS_DIR / "defs.json").read_text(encoding="utf-8"))
    return set(defs["$defs"]["error_code"]["enum"])


def test_every_pinned_forgectl_code_has_a_row_or_is_deliberately_defaulted() -> None:
    codes = pinned_codes()
    assert set(f.CODE_ROWS) <= codes
    assert codes == set(f.CODE_ROWS) | f.DEFAULTED
    assert not set(f.CODE_ROWS) & f.DEFAULTED
    assert set(f.CODE_ROWS.values()) <= set(f.ROWS)


def test_every_code_the_five_commands_can_emit_has_its_own_row() -> None:
    emitted = set().union(*ALLOWED_CODES.values())
    assert emitted - {"refused"} <= set(f.CODE_ROWS)
    assert f.DEFAULTED.isdisjoint(emitted)


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("code", sorted(pinned_codes()))
def test_every_pinned_code_is_decided_for_every_call(code: str, call: str) -> None:
    decision = f.decide_code(code, None, call)
    # `not_found` of a host call is the `internal` row and is reported as `internal`
    expected = "internal" if code == "not_found" and call in ("query", "whoami") else code
    assert decision.code == expected
    assert decision.effect.cli_code in EXIT_CODES


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize(
    "code", ["", "brand_new_code", "BODY", "x" * 500, "vetoed", "partial_results", "refused"]
)
def test_unknown_codes_take_the_conservative_default_row(code: str, call: str) -> None:
    decision = f.decide_code(code, None, call)
    assert decision.row == "transient"
    assert decision.effect == f.ROWS["transient"].effects[call]


def test_a_refusal_other_than_unknown_host_is_the_default_row() -> None:
    assert f.decide_code("refused", "stale_head", "pr").row == "transient"
    assert f.decide_code("refused", "unknown_host", "pr").row == "unknown_host"


def test_code_and_reason_must_be_text() -> None:
    with pytest.raises(TypeError):
        f.decide_code(None, None, "pr")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        f.decide_code("internal", None, "nope")


# --- rows, one by one -----------------------------------------------------------------------


def test_success_commits_and_clears_the_counter_of_a_pr() -> None:
    ok = Outcome(command="snapshot", returncode=0, result={})
    pr = f.decide(ok, "pr").effect
    assert pr.commit and pr.counter == "reset" and pr.failed_kind is None and pr.poll_exit == 0
    for call in ("repo", "query", "whoami"):
        eff = f.decide(ok, call).effect
        assert eff.commit and eff.counter == "none"


@pytest.mark.parametrize("call", CALLS)
def test_rate_limited_pauses_the_host_and_changes_nothing_else(call: str) -> None:
    d = f.decide(out("rate_limited", retry_after=30.0), call)
    e = d.effect
    assert (e.scope, e.pause, e.cancel_host, e.counter, e.poll_exit) == (
        "host", "retry_after", True, "none", 0,
    )  # fmt: skip
    assert e.failed_kind is None and not e.commit and not e.record
    assert d.retry_after == 30.0
    assert f.decide(out("rate_limited"), call).retry_after is None


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("code", ["auth_required", "tool_missing"])
def test_whole_host_failures(call: str, code: str) -> None:
    e = f.decide(out(code), call).effect
    assert (e.scope, e.cancel_host, e.counter, e.failed_kind, e.poll_exit) == (
        "host", True, "none", "host", 12,
    )  # fmt: skip
    assert e.pause == "none" and not e.commit


def test_host_rejected_is_the_prs_failure_but_a_hosts_whole_failure() -> None:
    for code in ("host_rejected", "unsupported_host_version"):
        pr = f.decide(out(code), "pr").effect
        assert (pr.scope, pr.counter, pr.record, pr.prominent, pr.cancel_host) == (
            "pr", "increment", True, True, False,
        )  # fmt: skip
        assert pr.failed_kind == "pr" and pr.poll_exit == 0
        for call in ("repo", "query", "whoami"):
            e = f.decide(out(code), call).effect
            assert (e.scope, e.cancel_host, e.counter, e.poll_exit) == ("host", True, "none", 12)
            assert e.failed_kind == "host"


def test_wrong_host_response_is_whole_host_everywhere_and_logs_the_rotation_error() -> None:
    for call in CALLS:
        e = f.decide(out("wrong_host_response"), call).effect
        assert (e.scope, e.cancel_host, e.counter, e.poll_exit) == ("host", True, "none", 12)
        assert e.log_error is not None and "rotated" in e.log_error
    for call in CALLS:
        assert f.decide(out("auth_required"), call).effect.log_error is None


TRANSIENT_CODES = ["network_error", "upstream_error", "internal", "something_new"]


@pytest.mark.parametrize("code", TRANSIENT_CODES)
def test_transient_codes(code: str) -> None:
    pr = f.decide(out(code), "pr").effect
    assert (pr.scope, pr.counter, pr.record, pr.poll_exit) == ("pr", "increment", True, 0)
    assert pr.failed_kind == "pr" and not pr.commit and not pr.fallback_show
    repo = f.decide(out(code), "repo").effect
    assert (repo.scope, repo.counter, repo.failed_kind, repo.poll_exit) == (
        "repo",
        "none",
        "repo",
        0,
    )
    query = f.decide(out(code), "query").effect
    assert (query.scope, query.counter, query.failed_kind, query.poll_exit) == (
        "host", "none", "query", 12,
    )  # fmt: skip
    whoami = f.decide(out(code), "whoami").effect
    assert (whoami.failed_kind, whoami.counter, whoami.poll_exit) == ("host", "none", 12)
    assert not query.cancel_host and not whoami.cancel_host and not repo.cancel_host


def test_local_killed_and_exit_137_are_transient_with_the_code_killed() -> None:
    for outcome in (local("killed"), local("killed", -9), raw(137)):
        d = f.decide(outcome, "pr")
        assert d.row == "transient" and d.code == "killed"
        assert d.effect.counter == "increment"
    assert f.decide(local("killed"), "query").effect.poll_exit == 12


def test_contract_is_counted_per_pr_and_reports_contract() -> None:
    d = f.decide(local("contract", 0), "pr")
    assert d.row == "contract" and d.code == "contract"
    assert d.effect.counter == "increment" and d.effect.cli_code == "contract"
    assert f.decide(local("contract", 0), "repo").effect.counter == "none"
    assert f.decide(local("contract", 0), "whoami").effect.failed_kind == "host"


def test_too_large_counts_and_takes_the_show_fallback_for_a_pr_only() -> None:
    d = f.decide(local("too_large"), "pr")
    assert d.code == "too_large" and d.effect.counter == "increment" and d.effect.fallback_show
    for call in ("repo", "query", "whoami"):
        assert not f.decide(local("too_large"), call).effect.fallback_show


def test_not_found_is_permanent_for_a_pr_and_never_closed() -> None:
    d = f.decide(out("not_found"), "pr")
    e = d.effect
    assert (e.counter, e.record, e.cli_code, e.commit) == ("increment", True, "not_found", False)
    assert e.poll_exit == 0 and e.failed_kind == "pr"
    r = f.decide(out("not_found"), "repo").effect
    assert (r.counter, r.failed_kind, r.poll_exit) == ("none", "repo", 0)
    for call in ("query", "whoami"):  # not_found of a host call is `internal`
        assert f.decide(out("not_found"), call).effect == f.ROWS["transient"].effects[call]


def test_permission_denied_is_permanent_for_a_pr_and_a_failed_scope_otherwise() -> None:
    pr = f.decide(out("permission_denied"), "pr").effect
    assert (pr.counter, pr.cli_code) == ("increment", "host_failed")
    for call in ("repo", "query", "whoami"):
        assert (
            f.decide(out("permission_denied"), call).effect == (f.ROWS["transient"].effects[call])
        )


def test_unknown_host_is_permanent_and_refused() -> None:
    d = f.decide(out("refused", reason="unknown_host"), "pr")
    assert d.row == "unknown_host" and d.code == "refused"
    assert (d.effect.counter, d.effect.cli_code) == ("increment", "refused")
    repo = f.decide(out("refused", reason="unknown_host"), "repo").effect
    assert (repo.counter, repo.failed_kind, repo.poll_exit) == ("none", "repo", 0)
    q = f.decide(out("refused", reason="unknown_host"), "query").effect
    assert (q.failed_kind, q.poll_exit) == ("query", 12)
    w = f.decide(out("refused", reason="unknown_host"), "whoami").effect
    assert (w.failed_kind, w.poll_exit) == ("host", 12)


def test_pr_id_changed_is_a_pr_call_only_and_commits_nothing() -> None:
    d = f.decide_local("pr_id_changed", "pr")
    assert d.code == "pr_id_changed"
    e = d.effect
    assert (e.counter, e.record, e.commit, e.poll_exit, e.failed_kind) == (
        "increment", True, False, 0, "pr",
    )  # fmt: skip
    for call in ("repo", "query", "whoami"):
        with pytest.raises(ValueError):
            f.decide_local("pr_id_changed", call)


@pytest.mark.parametrize("returncode", [129, 130, 131, 143, 255])
def test_a_signaled_forgectl_is_interrupted_not_counted(returncode: int) -> None:
    d = f.decide(raw(returncode), "pr")
    assert d.row == "interrupted" and d.code == "interrupted"
    e = d.effect
    assert (e.counter, e.pause, e.poll_exit, e.failed_kind) == ("none", "none", 0, "pr")
    assert f.decide(raw(returncode), "repo").effect.failed_kind == "repo"
    q = f.decide(raw(returncode), "query").effect
    assert (q.failed_kind, q.poll_exit, q.counter) == ("query", 0, "none")
    w = f.decide(raw(returncode), "whoami").effect
    assert (w.failed_kind, w.poll_exit) == ("host", 0)


def test_an_outcome_with_nothing_to_read_below_129_is_conservatively_internal() -> None:
    d = f.decide(raw(1), "pr")
    assert d.row == "transient" and d.code == "internal"


@pytest.mark.parametrize("call", ["query", "whoami"])
def test_not_found_of_a_host_call_is_reported_as_internal(call: str) -> None:
    assert f.decide(out("not_found"), call).code == "internal"
    assert f.decide(out("not_found"), "pr").code == "not_found"
    assert f.decide(out("not_found"), "repo").code == "not_found"


def test_usage_codes_are_reported_as_forgectls_own() -> None:
    for code in ("usage", "invalid_pr", "config_invalid"):
        for call in CALLS:
            assert f.decide(out(code), call).code == code


@pytest.mark.parametrize("code", ["usage", "invalid_pr", "config_invalid"])
def test_a_rejected_stored_reference_is_internal_plus_a_contract_warning(code: str) -> None:
    for call in ("pr", "repo"):
        e = f.decide(out(code), call).effect
        t = f.ROWS["transient"].effects[call]
        assert e.warn_contract and e.counter == t.counter and e.failed_kind == t.failed_kind
        assert e.cli_code == "internal"


@pytest.mark.parametrize("call", ["query", "whoami"])
def test_forgectls_own_config_invalid_stops_the_poll(call: str) -> None:
    e = f.decide(out("config_invalid"), call).effect
    assert e.stop_poll and e.poll_exit == 12 and e.cli_code == "host_failed"
    assert e.failed_kind == "host"
    for code in ("usage", "invalid_pr"):
        u = f.decide(out(code), call).effect
        assert not u.stop_poll and u.warn_contract


def test_version_failures_are_tool_wide_and_exit_12() -> None:
    m = f.decide_tool(ToolMissing("gone")).effect
    assert (m.stop_poll, m.poll_exit, m.cli_code, m.scope) == (True, 12, "tool_missing", "poll")
    o = f.decide_tool(ToolTooOld("0.1.0", "0.55")).effect
    assert (o.stop_poll, o.poll_exit, o.cli_code) == (True, 12, "tool_too_old")
    for call in CALLS:
        assert f.ROWS["tool_missing"].effects[call].stop_poll
        assert f.ROWS["tool_too_old"].effects[call].stop_poll
        assert f.ROWS["tool_missing"].effects[call].cli_code == "tool_missing"
        assert f.ROWS["tool_too_old"].effects[call].cli_code == "tool_too_old"
    assert f.decide_tool(ToolMissing("x")).code == "tool_missing"
    assert f.decide_tool(ToolTooOld("0.1.0", "0.55")).code == "tool_too_old"


def test_partial_results_are_used_and_their_errors_decided_by_the_host_column() -> None:
    partial = Outcome(command="prs", returncode=12, result={"errors": []})
    d = f.decide(partial, "query")
    assert d.row == "partial" and d.effect.use_result and d.effect.poll_exit == 0
    assert f.decide(partial, "whoami").effect.use_result
    repo = f.decide(partial, "repo").effect
    assert (repo.scope, repo.use_result, repo.counter, repo.poll_exit) == ("repo", True, "none", 0)


def test_a_pr_call_with_a_result_at_a_nonzero_exit_is_a_contract_failure() -> None:
    for code in (12, 2, None):
        d = f.decide(Outcome(command="snapshot", returncode=code, result={}), "pr")
        assert d.row == "contract" and d.code == "contract"
        assert d.effect.counter == "increment"
    d = f.decide(Outcome(command="prs", returncode=None, result={}), "query")
    assert d.row == "contract"


@pytest.mark.parametrize("call", ["repo", "query", "whoami"])
@pytest.mark.parametrize(
    "code,reason",
    [
        ("rate_limited", None), ("auth_required", None), ("not_found", None),
        ("refused", "unknown_host"), ("network_error", None), ("config_invalid", None),
    ],
)  # fmt: skip
def test_partial_errors_entries_classify_as_the_same_call_kind(
    call: str, code: str, reason: str | None
) -> None:
    # `partial` only matters for a multi-host ask; pr-watch asks about one host (AC4), so
    # `errors` entries are decided one by one with the call kind of the listing.
    entry = f.decide_code(code, reason, call)
    assert entry.effect == f.ROWS[entry.row].effects[call]
    assert entry.effect.use_result is False


def test_a_failure_never_commits_or_changes_a_prs_state_by_itself() -> None:
    outcomes = [out(c) for c in sorted(pinned_codes() - {"refused"})]
    outcomes += [out("refused", reason="unknown_host"), local("killed"), local("contract", 0)]
    outcomes += [local("too_large"), raw(137), raw(130)]
    for outcome in outcomes:
        for call in CALLS:
            e = f.decide(outcome, call).effect
            assert not e.commit and not e.use_result
            if call != "pr":
                assert e.counter == "none" and not e.fallback_show and not e.record
            if e.scope != "pr":
                assert e.counter == "none"


def test_only_the_pr_column_ever_counts() -> None:
    for name, row in f.ROWS.items():
        for call, e in row.effects.items():
            if e.counter != "none":
                assert call == "pr", (name, call)
            if e.counter == "reset":
                assert e.commit


def test_every_effect_names_known_vocabulary() -> None:
    for name, row in f.ROWS.items():
        for call, e in row.effects.items():
            assert call in CALLS
            assert e.scope in f.SCOPES, (name, call)
            assert e.counter in ("none", "increment", "reset")
            assert e.pause in ("none", "retry_after")
            assert e.failed_kind in (None, "pr", "repo", "query", "host")
            assert e.poll_exit in (0, 12)
            assert (e.cli_code is None) == (e.commit or e.use_result)
            assert e.cli_code is None or e.cli_code in EXIT_CODES
            if e.cancel_host:
                assert e.scope == "host"
            if e.pause != "none":
                assert e.scope == "host" and e.poll_exit == 0


def test_exit_12_of_poll_is_exactly_the_rows_marked_so() -> None:
    twelve = {
        (name, call) for name, row in f.ROWS.items() for call, e in row.effects.items()
        if e.poll_exit == 12
    }  # fmt: skip
    names = {n for n, _ in twelve}
    assert names == {
        "host_down", "forgectl_tool_missing", "host_rejected", "wrong_host_response",
        "transient", "contract", "too_large", "unknown_host", "permission_denied", "not_found",
        "usage_rejected", "config_invalid", "tool_missing", "tool_too_old",
    }  # fmt: skip
    assert all(call != "pr" for n, call in twelve if n in {"transient", "contract", "too_large"})
    assert not any(e.poll_exit for e in f.ROWS["rate_limited"].effects.values())
    assert not any(e.poll_exit for e in f.ROWS["interrupted"].effects.values())


# --- pause, backoff, counter ----------------------------------------------------------------


def test_pause_uses_retry_after_clamped_to_an_hour_and_leaves_the_step() -> None:
    assert f.host_pause(30.0, 0) == (30.0, 0)
    assert f.host_pause(30.0, 120) == (30.0, 120)
    assert f.host_pause(10**9, 0) == (3600.0, 0)
    assert f.host_pause(0.0, 0) == (0.0, 0)


def test_pause_without_retry_after_doubles_from_a_minute_to_fifteen() -> None:
    step = 0
    seen = []
    for _ in range(7):
        seconds, step = f.host_pause(None, step)
        seen.append(seconds)
        assert seconds == step
    assert seen == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 900.0]


def test_pause_without_retry_after_is_never_below_a_minute() -> None:
    assert f.host_pause(None, 1) == (60.0, 60)
    assert f.host_pause(None, 29) == (60.0, 60)
    assert f.host_pause(None, 30) == (60.0, 60)
    assert f.host_pause(None, 31) == (62.0, 62)


def test_pause_rejects_nonsense() -> None:
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            f.host_pause(bad, 0)
    with pytest.raises(ValueError):
        f.host_pause(None, -1)


def test_counter_rises_resets_and_makes_a_pr_unreachable_at_the_threshold() -> None:
    inc = f.decide(out("network_error"), "pr").effect
    assert f.apply_counter(inc, 0, 5, "active") == (1, False)
    assert f.apply_counter(inc, 3, 5, "active") == (4, False)
    assert f.apply_counter(inc, 4, 5, "active") == (5, True)
    assert f.apply_counter(inc, 5, 5, "active") == (6, False)
    none = f.decide(out("rate_limited"), "pr").effect
    assert f.apply_counter(none, 4, 5, "active") == (4, False)
    ok = f.decide(Outcome(command="snapshot", returncode=0, result={}), "pr").effect
    assert f.apply_counter(ok, 9, 5, "unreachable") == (0, False)


@pytest.mark.parametrize("status", ["terminal", "unreachable"])
def test_only_an_active_pr_becomes_unreachable(status: str) -> None:
    inc = f.decide(out("network_error"), "pr").effect
    assert f.apply_counter(inc, 4, 5, status) == (5, False)


# --- the snapshot_failed record rule ----------------------------------------------------------


def test_should_record_first_failure_code_change_and_multiples() -> None:
    assert f.should_record(None, "internal", 1, 5)
    assert f.should_record("internal", "internal", 1, 5)  # a new streak after a success
    assert not f.should_record("internal", "internal", 2, 5)
    assert not f.should_record("internal", "internal", 4, 5)
    assert f.should_record("internal", "internal", 5, 5)
    assert f.should_record("internal", "internal", 10, 5)
    assert f.should_record("internal", "contract", 3, 5)
    assert not f.should_record("internal", "internal", 6, 5)


# --- direct commands: the envelope ----------------------------------------------------------


def envelope_errors(error: CliError) -> list[str]:
    return [e.message for e in validator("cli/error").iter_errors(build_envelope(error))]


def test_direct_command_error_codes() -> None:
    cases: list[tuple[Outcome, str, int]] = [
        (out("not_found"), "not_found", 10),
        (out("rate_limited", retry_after=5.0), "host_failed", 12),
        (out("auth_required"), "host_failed", 12),
        (out("permission_denied"), "host_failed", 12),
        (out("network_error"), "host_failed", 12),
        (out("internal"), "host_failed", 12),
        (out("host_rejected"), "host_failed", 12),
        (out("wrong_host_response"), "host_failed", 12),
        (out("refused", reason="unknown_host"), "refused", 20),
        (out("tool_missing"), "tool_missing", 12),
        (out("invalid_pr"), "internal", 1),
        (local("contract", 0), "contract", 12),
        (local("too_large"), "host_failed", 12),
        (local("killed"), "host_failed", 12),
        (raw(137), "host_failed", 12),
        (raw(130), "host_failed", 12),
    ]
    for outcome, code, exit_code in cases:
        error = f.to_cli_error(f.decide(outcome, "pr"))
        assert (error.code, error.exit_code) == (code, exit_code), outcome
        assert envelope_errors(error) == [], outcome


def test_a_refusal_with_any_reason_ends_a_direct_command_as_refused() -> None:
    error = f.to_cli_error(f.decide(out("refused", reason="too_large"), "pr"))
    assert (error.code, error.exit_code, error.reason) == ("refused", 20, "too_large")
    assert error.lower_error is not None and error.lower_error["reason"] == "too_large"
    assert envelope_errors(error) == []
    odd = f.to_cli_error(f.decide(out("refused", reason="Not A Reason"), "pr"))
    assert odd.code == "host_failed"
    # the poller-facing decision stays the default row
    assert f.decide(out("refused", reason="too_large"), "pr").row == "transient"


def test_a_too_large_show_does_not_ask_for_another_show() -> None:
    show = Outcome(command="show", returncode=None, local="too_large")
    d = f.decide(show, "pr")
    assert d.code == "too_large" and d.effect.counter == "increment"
    assert not d.effect.fallback_show
    assert f.decide(local("too_large"), "pr").effect.fallback_show


def test_pr_id_changed_message_says_what_happened() -> None:
    error = f.to_cli_error(f.decide_local("pr_id_changed", "pr"))
    assert error.message == "the stored PR id differs from the PR the reference now names"
    assert error.code == "host_failed" and envelope_errors(error) == []


def test_refused_carries_the_reason_and_lower_error() -> None:
    error = f.to_cli_error(f.decide(out("refused", reason="unknown_host"), "pr"))
    assert error.reason == "unknown_host"
    assert error.lower_error == {
        "code": "refused", "reason": "unknown_host", "retry_after": None, "message": "m",
    }  # fmt: skip


def test_lower_error_carries_forgectls_code_reason_retry_after_and_message() -> None:
    error = f.to_cli_error(f.decide(out("rate_limited", retry_after=42.0), "pr"))
    assert error.lower_error == {
        "code": "rate_limited", "reason": None, "retry_after": 42.0, "message": "m",
    }  # fmt: skip


def test_local_failures_have_no_lower_error() -> None:
    for outcome in (local("killed"), local("too_large"), local("contract", 0), raw(137)):
        assert f.to_cli_error(f.decide(outcome, "pr")).lower_error is None


def test_contract_message_is_our_words_from_the_outcome() -> None:
    error = f.to_cli_error(f.decide(local("contract", 0), "pr"))
    assert "d" in error.message and envelope_errors(error) == []


def test_tool_missing_from_forgectl_keeps_its_install_hint() -> None:
    outcome = Outcome(
        command="snapshot",
        returncode=12,
        error=ForgectlError(
            code="tool_missing", message="m", details={"install": "brew install thing"}
        ),
    )
    error = f.to_cli_error(f.decide(outcome, "pr"))
    assert error.code == "tool_missing" and error.details == {"install": "brew install thing"}
    assert envelope_errors(error) == []
    bare = f.to_cli_error(f.decide(out("tool_missing"), "pr"))
    assert bare.details == {"install": INSTALL}


def test_version_errors_become_envelopes() -> None:
    missing = f.to_cli_error(f.decide_tool(ToolMissing("forgectl was not found")))
    assert (missing.code, missing.exit_code) == ("tool_missing", 12)
    assert missing.details == {"install": INSTALL} and envelope_errors(missing) == []
    old = f.to_cli_error(f.decide_tool(ToolTooOld("0.1.0", "0.55")))
    assert (old.code, old.exit_code) == ("tool_too_old", 12)
    assert old.details == {"required": "0.55", "found": "0.1.0"}
    assert envelope_errors(old) == []


def test_every_row_and_call_makes_a_valid_cli_error() -> None:
    for name, row in f.ROWS.items():
        for call in row.effects:
            decision = f.decision_for(name, call)
            if name in ("tool_missing", "tool_too_old", "success", "partial"):
                continue
            error = f.to_cli_error(decision)
            cli = row.effects[call].cli_code
            assert cli is not None
            assert error.exit_code == EXIT_CODES[cli]
            assert envelope_errors(error) == [], (name, call)


def test_a_success_has_no_cli_error() -> None:
    with pytest.raises(ValueError):
        f.to_cli_error(f.decide(Outcome(command="snapshot", returncode=0, result={}), "pr"))


def test_hostile_forgectl_text_stays_inside_the_envelope() -> None:
    nasty = "\x1b[31m" + "A" * 1000 + "‮\n"
    outcome = Outcome(
        command="snapshot",
        returncode=12,
        error=ForgectlError(code="network_error", message=nasty),
    )
    error = f.to_cli_error(f.decide(outcome, "pr"))
    assert envelope_errors(error) == []
    assert "\x1b" not in str(build_envelope(error)) and len(nasty) > 256


def test_the_table_is_immutable() -> None:
    with pytest.raises(TypeError):
        f.ROWS["x"] = f.ROWS["transient"]  # type: ignore[index]
    with pytest.raises(TypeError):
        f.ROWS["transient"].effects["pr"] = f.ROWS["transient"].effects["repo"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        f.ROWS["transient"].effects["pr"].counter = "none"  # type: ignore[misc]


def test_decide_rejects_unknown_calls_and_non_outcomes() -> None:
    with pytest.raises(ValueError):
        f.decide(raw(137), "compare")
    with pytest.raises(TypeError):
        f.decide({"error": 1}, "pr")  # type: ignore[arg-type]
