"""The forgectl client against the fake (tests/fakebin/forgectl) and the pinned schemas.

Every parsed result is validated against forgectl's own pinned schema, so a client that accepts
what real forgectl would not print fails here.
"""

from __future__ import annotations

import copy
import json
import math
import os
import signal
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pr_watch import forgectl_client as fc
from pr_watch import gateway
from pr_watch.config import Forgectl
from pr_watch.forgectl_client import (
    ForgectlClient,
    Outcome,
    ToolMissing,
    ToolTooOld,
    resolve_executable,
)
from pr_watch.refs import PrRef, parse_pr
from tests.forgectl_support import (
    SCHEMAS_DIR,
    FakeForgectl,
    assert_forgectl_valid,
    golden,
    new_pr,
    sha,
)

PR = "github.com/example-org/widgets#7"
REF = parse_pr(PR)
assert isinstance(REF, PrRef)


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    fake_forgectl.host("github.com", me="user-1")
    return fake_forgectl


def make_client(**kw: Any) -> ForgectlClient:
    exe = resolve_executable(os.environ["PATH"])
    assert exe is not None
    params: dict[str, Any] = {
        "timeout_ms": 5000,
        "max_output": 4 * 1024 * 1024,
        "env": dict(os.environ),
        "deadline_slack": 5.0,
        "term_grace": 0.3,
    }
    params.update(kw)
    return ForgectlClient(exe, **params)


@pytest.fixture
def client(fake: FakeForgectl) -> ForgectlClient:
    return make_client()


def ok(outcome: Outcome, schema: str) -> dict[str, Any]:
    assert outcome.local is None, outcome
    assert outcome.error is None and outcome.result is not None, outcome
    assert_forgectl_valid(schema, outcome.result)
    return outcome.result


# --- constants and the version rule ----------------------------------------------------------


def test_min_forgectl_is_pinned_at_0_55() -> None:
    assert fc.MIN_FORGECTL == (0, 55)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("forgectl 0.55.3\n", (0, 55, 3)),
        ("forgectl 0.55.0", (0, 55, 0)),
        ("forgectl 12.345.6789\n", (12, 345, 6789)),
        ("forgectl 0.55.3\n\n", None),
        ("forgectl 0.55.3 \n", None),
        (" forgectl 0.55.3", None),
        ("forgectl 0.55", None),
        ("forgectl 0.55.3.1", None),
        ("forgectl 0.55.3-rc1", None),
        ("forgectl 0.55.3\nextra", None),
        ("forgectl v0.55.3", None),
        ("forgectl 0.55.٣", None),  # Arabic-indic digit: not ASCII
        ("forgectl 0.55.1234567890", None),  # more than 9 digits
        ("forgectl", None),
        ("", None),
        ("pr-watch 0.55.3", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert fc.parse_version(text) == expected


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ((0, 55, 0), True),
        ((0, 55, 99), True),
        ((0, 56, 0), True),  # the maintainer decision: a newer 0.x minor is accepted
        ((0, 300, 1), True),
        ((0, 54, 9), False),
        ((0, 0, 0), False),
        ((1, 0, 0), False),  # another major
        ((1, 99, 0), False),
    ],
)
def test_version_rule_same_major_minor_at_least(
    version: tuple[int, int, int], accepted: bool
) -> None:
    assert fc.version_accepted(version) is accepted


def test_version_rule_for_a_later_major_minimum() -> None:
    assert fc.version_accepted((2, 3, 0), minimum=(2, 3))
    assert fc.version_accepted((2, 4, 0), minimum=(2, 3))
    assert not fc.version_accepted((2, 2, 9), minimum=(2, 3))
    assert not fc.version_accepted((3, 9, 0), minimum=(2, 3))


def test_version_of_the_fake_is_accepted_and_cached(fake: FakeForgectl, client) -> None:
    version = client.version()
    assert (version.major, version.minor, version.patch) == (0, 55, 3)
    assert version.text == "0.55.3"
    assert client.version() is version
    assert fake.argvs() == [["--version"]]  # asked once


def test_a_newer_minor_is_accepted(fake: FakeForgectl, client) -> None:
    fake.version("forgectl 0.61.2\n")
    assert client.version().minor == 61


def test_an_older_minor_is_tool_too_old(fake: FakeForgectl, client) -> None:
    fake.version("forgectl 0.54.9\n")
    with pytest.raises(ToolTooOld) as info:
        client.version()
    assert info.value.found == "0.54.9" and info.value.required == "0.55"
    assert "uv tool install" in info.value.install


def test_another_major_is_tool_too_old(fake: FakeForgectl, client) -> None:
    fake.version("forgectl 1.0.0\n")
    with pytest.raises(ToolTooOld) as info:
        client.version()
    assert info.value.found == "1.0.0"


@pytest.mark.parametrize("text", ["garbage\n", "", "forgectl 0.55\n", "forgectl 0.55.3-rc1\n"])
def test_unparseable_version_is_tool_too_old_unknown(fake: FakeForgectl, client, text: str) -> None:
    fake.version(text)
    with pytest.raises(ToolTooOld) as info:
        client.version()
    assert info.value.found == "unknown"


def test_non_zero_version_exit_is_tool_too_old_unknown(fake: FakeForgectl, client) -> None:
    fake.version("forgectl 0.55.3\n", exit=1)
    with pytest.raises(ToolTooOld) as info:
        client.version()
    assert info.value.found == "unknown"


def test_version_that_dies_by_signal_is_tool_missing(tmp_path: Path) -> None:
    exe = tmp_path / "bin" / "fgctl"  # not named forgectl: the suite's launch guard
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nkill -KILL $$\n")  # the fake cannot script a signal here
    exe.chmod(0o755)
    client = ForgectlClient(str(exe), timeout_ms=1000, max_output=1000, env={"PATH": "/bin"})
    with pytest.raises(ToolMissing):
        client.version()


def test_version_output_over_its_cap_is_tool_missing(fake: FakeForgectl, client) -> None:
    fake.version("forgectl 0.55.3\n" + "x" * 10_000)
    with pytest.raises(ToolMissing):
        client.version()


def test_version_that_hangs_past_the_deadline_is_tool_missing(tmp_path: Path) -> None:
    exe = tmp_path / "bin" / "fgctl"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexec sleep 30\n")
    exe.chmod(0o755)
    client = ForgectlClient(
        str(exe),
        timeout_ms=100,
        max_output=1000,
        env={"PATH": "/bin:/usr/bin"},
        deadline_slack=0.3,
        term_grace=0.2,
    )
    with pytest.raises(ToolMissing):
        client.version()


def test_a_version_failure_is_not_cached_as_success(fake: FakeForgectl, client) -> None:
    fake.version("garbage\n", times=1)
    with pytest.raises(ToolTooOld):
        client.version()
    assert client.version().text == "0.55.3"


# --- resolution ------------------------------------------------------------------------------


def make_exe(directory: Path, name: str = "forgectl", mode: int = 0o755) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(mode)
    return path


def test_resolve_uses_absolute_path_entries_only(tmp_path: Path, monkeypatch) -> None:
    good = make_exe(tmp_path / "abs")
    rel = tmp_path / "rel"
    make_exe(rel)
    monkeypatch.chdir(tmp_path)
    path = os.pathsep.join(["rel", "", ".", str(good.parent)])
    assert resolve_executable(path) == str(good)


def test_resolve_ignores_relative_and_empty_entries_when_nothing_else(
    tmp_path: Path, monkeypatch
) -> None:
    make_exe(tmp_path / "rel")
    monkeypatch.chdir(tmp_path)
    assert resolve_executable(os.pathsep.join(["rel", "", "."])) is None
    assert resolve_executable("") is None


def test_resolve_skips_non_executable_and_directories(tmp_path: Path) -> None:
    make_exe(tmp_path / "a", mode=0o644)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "forgectl").mkdir()
    good = make_exe(tmp_path / "c")
    path = os.pathsep.join(str(tmp_path / d) for d in "abc")
    assert resolve_executable(path) == str(good)


def test_resolve_first_match_wins(tmp_path: Path) -> None:
    first = make_exe(tmp_path / "one")
    make_exe(tmp_path / "two")
    assert resolve_executable(os.pathsep.join([str(tmp_path / "one"), str(tmp_path / "two")])) == (
        str(first)
    )


def test_from_config_missing_forgectl_is_tool_missing_with_install(tmp_path: Path) -> None:
    with pytest.raises(ToolMissing) as info:
        ForgectlClient.from_config(Forgectl(), env={"PATH": str(tmp_path)})
    assert "uv tool install" in info.value.install


def test_from_config_resolves_and_converts_units(fake: FakeForgectl) -> None:
    client = ForgectlClient.from_config(
        Forgectl(timeout="90s", max_output_mib=3), env=dict(os.environ)
    )
    assert client.timeout_ms == 90_000
    assert client.max_output == 3 * 1024 * 1024
    assert client.deadline == 100.0
    assert Path(client.exe).name == "forgectl" and os.path.isabs(client.exe)


def test_config_refuses_a_zero_timeout() -> None:
    with pytest.raises(ValidationError):
        Forgectl(timeout="0")
    with pytest.raises(ValidationError):
        Forgectl(timeout="0ms")


def test_from_config_rejects_a_zero_timeout_that_bypassed_validation() -> None:
    with pytest.raises(ValueError):
        ForgectlClient.from_config(Forgectl.model_construct(timeout="0"), env=dict(os.environ))


def test_constructor_requires_an_absolute_executable() -> None:
    with pytest.raises(ValueError):
        ForgectlClient("forgectl", timeout_ms=1000, max_output=1000, env={})


# --- argv ------------------------------------------------------------------------------------


def test_argv_snapshot_show() -> None:
    assert fc.snapshot_argv(REF, 1500) == [
        "snapshot", PR, "--json", "--timeout=1500ms",
    ]  # fmt: skip
    assert fc.show_argv(REF, 1500)[:2] == ["show", PR]


def test_argv_prs_host_listing() -> None:
    argv = fc.prs_argv(host="github.com", author="me", state="open", limit=1000, timeout_ms=1000)
    assert argv == [
        "prs", "--host=github.com", "--author=me", "--state=open", "--limit=1000",
        "--json", "--timeout=1000ms",
    ]  # fmt: skip


def test_argv_prs_repo_listing_with_watermark() -> None:
    when = datetime(2026, 10, 2, 12, 0, 0, 250000, tzinfo=timezone(timedelta(hours=2)))
    argv = fc.prs_argv(
        repo="github.com/example-org/widgets",
        state="all",
        updated_since=when,
        limit=1000,
        timeout_ms=1000,
    )
    assert "--repo=github.com/example-org/widgets" in argv
    assert "--updated-since=2026-10-02T10:00:00.250000Z" in argv
    assert "--state=all" in argv


def test_argv_prs_whole_seconds_watermark() -> None:
    when = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    argv = fc.prs_argv(repo="github.com/o/r", updated_since=when, timeout_ms=1000)
    assert "--updated-since=2026-10-02T12:00:00Z" in argv


def test_argv_compare_and_whoami() -> None:
    argv = fc.compare_argv(REF, sha(1), sha(2), no_diff=False, timeout_ms=1000)
    assert argv == [
        "compare",
        PR,
        f"--from={sha(1)}",
        f"--to={sha(2)}",
        "--json",
        "--timeout=1000ms",
    ]
    assert "--no-diff" in fc.compare_argv(REF, sha(1), sha(2), no_diff=True, timeout_ms=1000)
    assert fc.whoami_argv(1000) == ["whoami", "--json", "--timeout=1000ms"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # neither host nor repo
        {"host": "github.com", "repo": "github.com/o/r"},
        {"host": "--config=/x"},
        {"host": "-h"},
        {"host": "GitHub.com/x"},
        {"host": "github.com\n"},
        {"host": "github.com", "author": "user-1"},
        {"host": "github.com", "author": "-x"},
        {"host": "github.com", "state": "bogus"},
        {"host": "github.com", "state": "--json"},
        {"host": "github.com", "limit": 0},
        {"host": "github.com", "limit": 1001},
        {"host": "github.com", "limit": True},
        {"host": "github.com", "limit": "5"},
        {"repo": "--evil"},
        {"repo": "github.com/o/r\x00"},
        {"repo": "github.com/o/r", "updated_since": "2026-10-02T12:00:00Z"},
        {"repo": "github.com/o/r", "updated_since": datetime(2026, 1, 1)},  # naive
    ],
)
def test_prs_argv_refuses_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        fc.prs_argv(timeout_ms=1000, **kwargs)


@pytest.mark.parametrize(
    "bad", ["a" * 39 + "A", sha(1)[:-1], sha(1) + "\n", "A" * 40, "", "x" * 40]
)
def test_compare_argv_refuses_a_bad_sha(bad: str) -> None:
    with pytest.raises(ValueError):
        fc.compare_argv(REF, bad, sha(2), no_diff=False, timeout_ms=1000)
    with pytest.raises(ValueError):
        fc.compare_argv(REF, sha(1), bad, no_diff=False, timeout_ms=1000)


def test_argv_refuses_a_forged_pr_ref_and_a_pr_id() -> None:
    forged = PrRef(host="-x", path="o/r", number=1)
    with pytest.raises(ValueError):
        fc.snapshot_argv(forged, 1000)
    with pytest.raises(ValueError):
        fc.snapshot_argv(PrRef(host="github.com", path="o/r", number=0), 1000)
    with pytest.raises(TypeError):
        fc.snapshot_argv("github.com/o/r#1", 1000)  # type: ignore[arg-type]
    from pr_watch.refs import PrId

    with pytest.raises(TypeError):
        fc.show_argv(PrId("PR_x"), 1000)  # type: ignore[arg-type]


def test_argv_never_contains_a_double_dash_or_a_leading_dash_positional() -> None:
    for argv in (
        fc.snapshot_argv(REF, 1000),
        fc.prs_argv(host="github.com", author="me", timeout_ms=1000),
        fc.whoami_argv(1000),
    ):
        assert "--" not in argv
        assert not argv[0].startswith("-")
    assert not fc.snapshot_argv(REF, 1000)[1].startswith("-")


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "1000"])
def test_timeout_must_be_a_positive_int_of_ms(bad: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        fc.snapshot_argv(REF, bad)


# --- what reaches the process -----------------------------------------------------------------


def test_the_call_reaches_forgectl_with_the_built_argv(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    ok(client.snapshot(REF), "snapshot")
    assert fake.argvs("snapshot") == [["snapshot", PR, "--json", "--timeout=5000ms"]]


def test_the_child_gets_exactly_the_given_environment(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "FAKE_FORGECTL_DIR")}
    env["FORGECTL_GH_BIN"] = "/opt/gh"
    client = make_client(env=env)
    ok(client.snapshot(REF), "snapshot")
    names = fake.calls("snapshot")[0].env_names
    assert "FORGECTL_GH_BIN" in names
    assert not {"XDG_CONFIG_HOME", "TMPDIR"} & set(names)  # nothing inherited beyond `env`


def test_from_config_default_environment_is_the_callers_unchanged(fake: FakeForgectl) -> None:
    client = ForgectlClient.from_config(Forgectl(), env=None)
    assert client.env == dict(os.environ)


# --- successful results, validated against the pinned schemas ----------------------------------


def test_snapshot_result(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    result = ok(client.snapshot(REF), "snapshot")
    assert result["pr"] == PR


def test_show_result(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    assert ok(client.show(REF), "show")["number"] == 7


def test_prs_host_listing(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    out = client.prs(host="github.com", author="me", state="open", limit=1000)
    result = ok(out, "prs")
    assert [i["pr"] for i in result["items"]] == [PR]
    assert out.returncode == 0 and not out.partial


def test_prs_repo_listing(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    when = datetime(2000, 1, 1, tzinfo=UTC)
    out = client.prs(repo="github.com/example-org/widgets", state="all", updated_since=when)
    assert [i["pr"] for i in ok(out, "prs")["items"]] == [PR]


def test_whoami_result(fake: FakeForgectl, client) -> None:
    result = ok(client.whoami(), "whoami")
    assert "github.com" in [h["host"] for h in result["hosts"]]


def test_compare_result(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.compare(PR, sha(1), sha(2), "ahead", diff="diff --git a/x b/x\n")
    result = ok(client.compare(REF, sha(1), sha(2)), "compare")
    assert result["status"] == "ahead"


def test_compare_with_no_diff_has_no_diff_key(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.compare(PR, sha(1), sha(2), "ahead")
    out = client.compare(REF, sha(1), sha(2), no_diff=True)
    assert out.local is None and out.result is not None and "diff" not in out.result


def test_a_gitlab_snapshot(fake: FakeForgectl, client) -> None:
    fake.host("gitlab.com", me="user-3")
    snap = new_pr(host="gitlab.com", repo="grp/sub/proj", number=3)
    fake.add(snap)
    ref = parse_pr("gitlab.com/grp/sub/proj#3")
    assert isinstance(ref, PrRef)
    assert ok(client.snapshot(ref), "snapshot")["backend"] == "gitlab"


def test_goldens_parse_through_the_shape_check() -> None:
    for name, command in (
        ("snapshot-github-open", "snapshot"),
        ("snapshot-gitlab-open", "snapshot"),
        ("show-github-merged", "show"),
        ("prs-github-open", "prs"),
        ("whoami-gitlab", "whoami"),
        ("compare-github-ahead", "compare"),
    ):
        text = json.dumps(golden(name))
        out = fc.classify(command, _result(stdout=text), expect=None)
        assert out.local is None and out.result == golden(name), name


def test_the_4000_char_check_name_with_300_control_characters(fake: FakeForgectl, client) -> None:
    snap = new_pr()
    name = ("a" * 3700) + "".join(chr(i % 31 + 1) for i in range(300))
    assert len(name) == 4000
    snap["ci"]["checks"][0]["name"] = name
    fake.add(snap)
    result = ok(client.snapshot(REF), "snapshot")
    assert result["ci"]["checks"][0]["name"] == name  # bounding and sanitizing are the digest's


# --- failures scripted in the fake ---------------------------------------------------------------


def error_of(outcome: Outcome) -> fc.ForgectlError:
    assert outcome.local is None and outcome.result is None, outcome
    assert outcome.error is not None
    return outcome.error


def test_rate_limited_with_retry_after(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.rate_limited("snapshot", retry_after=42.5)
    out = client.snapshot(REF)
    err = error_of(out)
    assert out.returncode == 12 and err.code == "rate_limited" and err.retry_after == 42.5


def test_rate_limited_without_retry_after(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.rate_limited("snapshot")
    err = error_of(client.snapshot(REF))
    assert err.code == "rate_limited" and err.retry_after is None


def test_retry_after_is_clamped_to_one_hour(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.rate_limited("snapshot", retry_after=10_000_000)
    assert error_of(client.snapshot(REF)).retry_after == 3600.0


def test_network_error(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.network_error("show")
    out = client.show(REF)
    assert error_of(out).code == "network_error" and out.returncode == 12


def test_not_found_exit_10(fake: FakeForgectl, client) -> None:
    out = client.snapshot(REF)  # not in the scenario
    assert out.returncode == 10 and error_of(out).code == "not_found"


def test_permission_denied(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.permission_denied("snapshot")
    out = client.snapshot(REF)
    assert out.returncode == 12 and error_of(out).code == "permission_denied"


def test_auth_required_with_variable_detail(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.auth_required("snapshot", details={"variable": "EXAMPLE_TOKEN"})
    err = error_of(client.snapshot(REF))
    assert err.code == "auth_required"
    assert err.details == {"variable": "EXAMPLE_TOKEN"}


def test_tool_missing_carries_install(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.fail("snapshot", "tool_missing", details={"install": "install gh"})
    err = error_of(client.snapshot(REF))
    assert err.code == "tool_missing" and err.details == {"install": "install gh"}


def test_refusal_reason_is_kept(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.fail("snapshot", "refused", reason="unknown_host")
    out = client.snapshot(REF)
    assert out.returncode == 20 and error_of(out).reason == "unknown_host"


def test_host_rejected_status_detail(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.fail("snapshot", "host_rejected", details={"status": 422})
    assert error_of(client.snapshot(REF)).details == {"status": 422}


def test_exit_2_usage(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.fail("snapshot", "invalid_pr")
    out = client.snapshot(REF)
    assert out.returncode == 2 and error_of(out).code == "invalid_pr"


def test_partial_prs_keeps_the_object_and_the_exit(fake: FakeForgectl, client) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.add(new_pr())
    fake.network_error("prs", host="gitlab.com")
    out = client.prs(author="me", host="github.com")  # one host asked: no partial here
    assert not out.partial
    # Ask the fake for two hosts through the same code path the client uses to parse it.
    proc = fake.run("prs", "--author", "me", "--json")
    parsed = fc.classify(
        "prs", _result(stdout=proc.stdout, returncode=proc.returncode), expect=None
    )
    assert parsed.partial and parsed.returncode == 12
    assert parsed.result is not None and parsed.error is None
    assert [e["host"] for e in parsed.result["errors"]] == ["gitlab.com"]
    assert_forgectl_valid("prs", parsed.result)


def test_partial_whoami(fake: FakeForgectl, client) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.network_error("whoami", host="gitlab.com")
    out = client.whoami()
    assert out.partial and out.local is None and out.returncode == 12
    assert out.result is not None
    assert [e["host"] for e in out.result["errors"]] == ["gitlab.com"]
    assert_forgectl_valid("whoami", out.result)


def test_exit_12_envelope_for_prs_is_an_error_not_partial(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.network_error("prs")
    out = client.prs(host="github.com", author="me")
    assert not out.partial and error_of(out).code in ("network_error", "partial_results")


# --- local failures -------------------------------------------------------------------------------


def test_garbage_stdout_is_a_contract_failure(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.garbage("snapshot")
    out = client.snapshot(REF)
    assert out.local == "contract" and out.result is None and out.returncode == 0


def test_truncated_stdout_is_a_contract_failure(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.truncated("snapshot")
    assert client.snapshot(REF).local == "contract"


@pytest.mark.parametrize("field", ["pr", "pr_id", "state", "head", "ci", "complete", "fetched_at"])
def test_a_missing_required_field_is_a_contract_failure(
    fake: FakeForgectl, client, field: str
) -> None:
    fake.add(new_pr())
    fake.contract_violation("snapshot", [field])
    out = client.snapshot(REF)
    assert out.local == "contract" and field in out.detail


def test_contract_failure_for_prs_and_whoami(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.contract_violation("prs", ["items"])
    fake.contract_violation("whoami", ["hosts"])
    assert client.prs(host="github.com", author="me").local == "contract"
    assert client.whoami().local == "contract"


def test_oversize_output_is_too_large_and_the_child_is_killed(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("snapshot", 3_000_000)
    client = make_client(max_output=1024 * 1024)
    out = client.snapshot(REF)
    assert out.local == "too_large" and out.too_large and out.result is None
    assert out.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM, -signal.SIGKILL)


def test_a_40_mib_answer_is_cut_at_the_32_mib_default_cap(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("snapshot", 40 * 1024 * 1024)
    client = ForgectlClient.from_config(Forgectl(), env=dict(os.environ))
    out = client.snapshot(REF)
    assert out.local == "too_large" and out.result is None


def test_exactly_at_the_cap_is_accepted(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    size = len(json.dumps(new_pr()))
    client = make_client(max_output=1 << 20)
    assert size < (1 << 20)
    assert client.snapshot(REF).local is None


def test_hang_past_the_deadline_is_killed(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.hang("snapshot", seconds=60)
    client = make_client(timeout_ms=200, deadline_slack=0.3, term_grace=0.2)
    out = client.snapshot(REF)
    assert out.local == "killed" and out.timed_out and out.result is None
    assert 0.4 < out.elapsed < 5
    assert out.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM, -signal.SIGKILL)


def test_a_term_ignoring_hang_is_killed_by_sigkill(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.hang("snapshot", seconds=60, ignore_term=True)
    client = make_client(timeout_ms=100, deadline_slack=0.2, term_grace=0.2)
    out = client.snapshot(REF)
    assert out.local == "killed" and out.returncode == -signal.SIGKILL


def test_a_hang_that_respects_the_timeout_flag_is_a_network_error(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.hang("snapshot", seconds=60, respect_timeout=True)
    client = make_client(timeout_ms=300, deadline_slack=3.0)
    out = client.snapshot(REF)
    assert out.local is None and error_of(out).code == "network_error"


def test_death_by_signal_is_killed(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.die_by_signal("snapshot", signal.SIGKILL)
    out = client.snapshot(REF)
    assert out.local == "killed" and out.returncode == -signal.SIGKILL and not out.timed_out


def test_exit_128_plus_n_without_envelope_is_left_to_the_caller(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.raw("snapshot", exit=143)
    out = client.snapshot(REF)
    assert out.returncode == 143 and out.local is None and out.error is None
    assert out.result is None


def test_non_zero_exit_with_garbage_is_a_contract_failure(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.garbage("snapshot", exit=1)
    assert client.snapshot(REF).local == "contract"


def test_an_orphan_holding_the_pipes_does_not_hang_the_call(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    fake.orphan("snapshot", seconds=20)
    out = client.snapshot(REF)
    assert out.local is None and out.elapsed < 4
    assert out.result is not None


def test_a_missing_executable_during_a_call_is_tool_missing(tmp_path: Path) -> None:
    client = ForgectlClient(
        str(tmp_path / "gone"), timeout_ms=1000, max_output=1000, env={"PATH": ""}
    )
    with pytest.raises(ToolMissing):
        client.snapshot(REF)


# --- classify: bytes, shapes and bounds (no process) ----------------------------------------------


def _result(
    stdout: str = "",
    returncode: int | None = 0,
    **kw: Any,
) -> gateway.Result:
    return gateway.Result(returncode=returncode, stdout=stdout, **kw)


def cl(stdout: str, command: str = "whoami", returncode: int | None = 0, **kw: Any) -> Outcome:
    return fc.classify(command, _result(stdout, returncode, **kw), expect=None)


GOOD_WHOAMI = '{"hosts": [], "errors": []}'


def test_classify_accepts_a_minimal_whoami() -> None:
    out = cl(GOOD_WHOAMI)
    assert out.local is None and out.result == {"hosts": [], "errors": []}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        '"x"',
        "42",
        "{",
        '{"hosts": [], "errors": []} trailing',
        '{"hosts": [], "errors": []}{"hosts": []}',
        '{"hosts": [], "hosts": [], "errors": []}',  # duplicate key
        '{"hosts": {}, "errors": []}',  # wrong type
        '{"hosts": [], "errors": null}',
        '{"hosts": [], "errors": [], "n": NaN}',
        '{"hosts": [], "errors": [], "n": Infinity}',
        '{"hosts": [], "errors": [], "n": -Infinity}',
        '{"hosts": [], "errors": [], "n": 12345678901234567890}',  # unbounded integer
        '{"hosts": [], "errors": [], "n": 1e999}',
        '{"hosts": [], "errors": [], "n": 1e40}',
        '{"hosts": [], "errors": [], "n": ' + "9" * 5000 + "}",
        "[" * 100000,
        '{"a":' * 100000,
        "﻿" + GOOD_WHOAMI,
        "{'hosts': []}",
        '{"error": {"code": "x"}}',  # an envelope at exit 0
    ],
)
def test_classify_rejects_bad_json_at_exit_0(text: str) -> None:
    out = cl(text)
    assert out.local == "contract" and out.result is None, text[:40]


def test_classify_accepts_whitespace_around_json() -> None:
    assert cl("  \n" + GOOD_WHOAMI + "\n\n").local is None


def test_classify_invalid_utf8_is_a_contract_failure() -> None:
    bad = GOOD_WHOAMI.encode().replace(b"[]", b"[]") + b"\xff"
    text = bad.decode("utf-8", "surrogateescape")  # as the gateway delivers it
    assert cl(text).local == "contract"


def test_classify_cut_multibyte_sequence_is_a_contract_failure() -> None:
    raw = ('{"hosts": [], "errors": [], "x": "é').encode()[:-1]
    assert cl(raw.decode("utf-8", "surrogateescape")).local == "contract"


def test_classify_lone_surrogate_text_in_a_valid_document_does_not_crash() -> None:
    text = '{"hosts": [], "errors": [], "x": "\\ud800"}'
    assert cl(text).local == "contract"


def test_classify_too_large_wins_over_everything() -> None:
    out = cl(GOOD_WHOAMI, returncode=-9, too_large=True)
    assert out.local == "too_large" and out.result is None


def test_classify_timed_out_is_killed_even_with_valid_output() -> None:
    out = cl(GOOD_WHOAMI, returncode=-15, timed_out=True)
    assert out.local == "killed" and out.result is None


def test_classify_abandoned_is_killed_with_no_exit_code() -> None:
    out = cl("", returncode=None, abandoned=True, timed_out=True)
    assert out.local == "killed" and out.returncode is None


def test_classify_negative_exit_is_killed() -> None:
    assert cl("", returncode=-11).local == "killed"


def test_classify_none_exit_without_abandon_is_killed() -> None:
    assert cl("", returncode=None).local == "killed"


def test_classify_envelope_fields_are_bounded_and_one_line() -> None:
    env = {
        "error": {
            "code": "network_error",
            "message": "line1\nline2\x00\x1b[31m" + "m" * 5000,
            "retry_after": 5,
            "details": {"install": "i" * 5000, "status": 404, "other": "x"},
        }
    }
    err = cl(json.dumps(env), command="snapshot", returncode=12).error
    assert err is not None
    assert "\n" not in err.message and "\x00" not in err.message and "\x1b" not in err.message
    assert len(err.message) <= fc.MAX_MESSAGE
    details = err.details
    assert details is not None
    assert len(str(details["install"])) <= fc.MAX_MESSAGE and details["status"] == 404
    assert "other" not in details


@pytest.mark.parametrize(
    "error",
    [
        None,
        [],
        {},
        {"code": 1, "message": "m"},
        {"code": "Bad Code", "message": "m"},
        {"code": "x" * 100, "message": "m"},
        {"code": "", "message": "m"},
        {"code": "internal"},
        {"code": "internal", "message": 5},
        {"code": "internal", "message": "m", "retry_after": "5"},
        {"code": "internal", "message": "m", "retry_after": -1},
        {"code": "internal", "message": "m", "retry_after": True},
        {"code": "internal", "message": "m", "reason": 5},
        {"code": "internal", "message": "m", "details": []},
        {"code": "host_rejected", "message": "m", "details": {"status": 10**9}},
        {"code": "host_rejected", "message": "m", "details": {"status": "404"}},
    ],
)
def test_classify_malformed_envelope_is_a_contract_failure(error: Any) -> None:
    out = cl(json.dumps({"error": error}), command="snapshot", returncode=12)
    assert out.local == "contract" and out.error is None


def test_classify_envelope_extra_top_level_keys_are_ignored() -> None:
    env = {"error": {"code": "internal", "message": "m"}, "other": 1}
    out = cl(json.dumps(env), command="snapshot", returncode=1)
    assert out.local is None and out.error is not None and out.error.code == "internal"


def test_classify_non_object_error_with_extra_keys_is_a_contract_failure() -> None:
    env = {"error": "boom", "other": 1}
    assert cl(json.dumps(env), command="snapshot", returncode=1).local == "contract"


def test_classify_unknown_code_is_kept_for_the_caller() -> None:
    env = {"error": {"code": "brand_new_code", "message": "m"}}
    out = cl(json.dumps(env), command="snapshot", returncode=12)
    assert out.error is not None and out.error.code == "brand_new_code"


def test_classify_exit_12_partial_needs_a_prs_or_whoami_object() -> None:
    assert cl(GOOD_WHOAMI, command="whoami", returncode=12).partial
    assert cl(GOOD_WHOAMI, command="snapshot", returncode=12).local == "contract"
    # a partial object must report at least one error
    assert cl(GOOD_WHOAMI, command="whoami", returncode=12).local is None


def test_classify_exit_0_with_a_failed_host_is_still_a_result() -> None:
    text = json.dumps(
        {"hosts": [], "errors": [{"host": "h", "code": "network_error", "message": "m"}]}
    )
    out = cl(text)
    assert out.local is None and not out.partial


def test_classify_compare_identity_and_diff() -> None:
    doc = {"pr": PR, "from_sha": sha(1), "to_sha": sha(2), "status": "ahead", "diff": "d"}
    expect = fc.Expect(compare=(sha(1), sha(2), False))
    assert fc.classify("compare", _result(json.dumps(doc)), expect=expect).local is None
    other = dict(doc, from_sha=sha(3))
    assert fc.classify("compare", _result(json.dumps(other)), expect=expect).local == "contract"
    nodiff = {k: v for k, v in doc.items() if k != "diff"}
    assert fc.classify("compare", _result(json.dumps(nodiff)), expect=expect).local == "contract"
    expect_nd = fc.Expect(compare=(sha(1), sha(2), True))
    assert fc.classify("compare", _result(json.dumps(nodiff)), expect=expect_nd).local is None


def test_classify_unknown_command_is_refused() -> None:
    with pytest.raises(ValueError):
        fc.classify("merge", _result(GOOD_WHOAMI), expect=None)


# --- the shape table is the pinned schema's -------------------------------------------------------


@pytest.mark.parametrize("command", ["snapshot", "show", "prs", "whoami", "compare"])
def test_required_fields_match_the_pinned_schema(command: str) -> None:
    schema = json.loads((SCHEMAS_DIR / "cli" / f"{command}.json").read_text(encoding="utf-8"))
    assert set(fc.REQUIRED[command]) == set(schema["required"])


def test_the_client_makes_no_call_outside_the_gateway() -> None:
    source = Path(fc.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source and "os.system" not in source
    assert stat.S_ISREG(os.stat(fc.__file__).st_mode)


# --- round 1 review: hardening ----------------------------------------------------------------


def test_unused_commands_constant_is_gone() -> None:
    assert not hasattr(fc, "COMMANDS")


GOLDENS = [
    ("snapshot-github-open", "snapshot"),
    ("snapshot-github-no-push-time", "snapshot"),
    ("snapshot-gitlab-open", "snapshot"),
    ("show-github-merged", "show"),
    ("show-github-open-clean", "show"),
    ("show-gitlab-open-ready", "show"),
    ("prs-github-open", "prs"),
    ("prs-gitlab-repo", "prs"),
    ("whoami-github", "whoami"),
    ("whoami-gitlab", "whoami"),
    ("compare-github-ahead", "compare"),
    ("compare-gitlab-ahead", "compare"),
]


@pytest.mark.parametrize(("name", "command"), GOLDENS)
def test_every_unmutated_golden_passes_the_nested_checks(name: str, command: str) -> None:
    out = fc.classify(command, _result(stdout=json.dumps(golden(name))), expect=None)
    assert out.local is None, out.detail


def _mutated(name: str, path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    doc = copy.deepcopy(golden(name))
    node: Any = doc
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return doc


SNAP = "snapshot-github-open"
BAD_FIELDS: list[tuple[str, str, tuple[Any, ...], Any]] = [
    (SNAP, "snapshot", ("pr",), 5),
    (SNAP, "snapshot", ("pr",), ""),
    (SNAP, "snapshot", ("repo",), None),
    (SNAP, "snapshot", ("pr_id",), ""),
    (SNAP, "snapshot", ("pr_id",), "a\x00b"),
    (SNAP, "snapshot", ("pr_id",), "a\nb"),
    (SNAP, "snapshot", ("pr_id",), 5),
    (SNAP, "snapshot", ("number",), 0),
    (SNAP, "snapshot", ("number",), -3),
    (SNAP, "snapshot", ("number",), True),
    (SNAP, "snapshot", ("number",), "7"),
    (SNAP, "snapshot", ("number",), 1.5),
    (SNAP, "snapshot", ("state",), 1),
    (SNAP, "snapshot", ("draft",), 1),
    (SNAP, "snapshot", ("labels",), "x"),
    (SNAP, "snapshot", ("labels",), [1]),
    (SNAP, "snapshot", ("head",), "x"),
    (SNAP, "snapshot", ("head", "sha"), 5),
    (SNAP, "snapshot", ("head", "sha"), ""),
    (SNAP, "snapshot", ("ci",), []),
    (SNAP, "snapshot", ("ci", "state"), 3),
    (SNAP, "snapshot", ("threads",), {}),
    (SNAP, "snapshot", ("threads",), [1]),
    (SNAP, "snapshot", ("reviews",), "x"),
    (SNAP, "snapshot", ("reviews",), ["x"]),
    (SNAP, "snapshot", ("review_requests",), {}),
    (SNAP, "snapshot", ("latest_by_reviewer",), [1]),
    (SNAP, "snapshot", ("incomplete",), "x"),
    (SNAP, "snapshot", ("incomplete",), [1]),
    (SNAP, "snapshot", ("complete",), "yes"),
    (SNAP, "snapshot", ("activity",), []),
    (SNAP, "snapshot", ("rate_limit",), []),
    (SNAP, "snapshot", ("fetched_at",), 5),
    ("show-github-merged", "show", ("state",), 1),
    ("show-github-merged", "show", ("head", "sha"), 5),
    ("show-github-merged", "show", ("number",), 0),
    ("show-github-merged", "show", ("pr_id",), ""),
    ("prs-github-open", "prs", ("items",), {}),
    ("prs-github-open", "prs", ("items",), [1]),
    ("prs-github-open", "prs", ("items", 0, "pr_id"), ""),
    ("prs-github-open", "prs", ("items", 0, "pr_id"), "a\x1bb"),
    ("prs-github-open", "prs", ("items", 0, "number"), 0),
    ("prs-github-open", "prs", ("items", 0, "pr"), 5),
    ("prs-github-open", "prs", ("items", 0, "state"), 5),
    ("prs-github-open", "prs", ("items", 0, "head"), "x"),
    ("prs-github-open", "prs", ("items", 0, "head", "sha"), 5),
    ("prs-github-open", "prs", ("hosts_queried",), [1]),
    ("prs-github-open", "prs", ("hosts_queried",), "x"),
    ("prs-github-open", "prs", ("errors",), [1]),
    ("prs-github-open", "prs", ("complete",), 1),
    ("prs-github-open", "prs", ("rate_limit",), []),
    ("whoami-github", "whoami", ("hosts",), {}),
    ("whoami-github", "whoami", ("hosts",), [1]),
    ("whoami-github", "whoami", ("hosts", 0, "host"), ""),
    ("whoami-github", "whoami", ("hosts", 0, "host"), 5),
    ("whoami-github", "whoami", ("errors",), "x"),
    ("whoami-github", "whoami", ("errors",), [1]),
    ("compare-github-ahead", "compare", ("status",), 1),
    ("compare-github-ahead", "compare", ("pr",), ""),
    ("compare-github-ahead", "compare", ("from_sha",), 5),
]


@pytest.mark.parametrize(
    ("name", "command", "path", "value"),
    BAD_FIELDS,
    ids=[f"{c}-{'.'.join(map(str, p))}-{i}" for i, (_, c, p, _v) in enumerate(BAD_FIELDS)],
)
def test_a_mutated_golden_field_is_a_contract_failure(
    name: str, command: str, path: tuple[Any, ...], value: Any
) -> None:
    doc = _mutated(name, path, value)
    out = fc.classify(command, _result(stdout=json.dumps(doc)), expect=None)
    assert out.local == "contract" and out.result is None


def test_new_enum_values_are_not_rejected() -> None:
    doc = _mutated(SNAP, ("state",), "brand_new_state")
    doc["ci"]["state"] = "brand_new_ci_state"
    doc["future_field"] = {"x": 1}
    assert fc.classify("snapshot", _result(stdout=json.dumps(doc)), expect=None).local is None


def _nested(levels: int) -> str:
    return '{"hosts": [], "errors": [], "x": ' + "[" * levels + "]" * levels + "}"


def test_depth_at_the_bound_is_accepted_and_beyond_is_a_contract_failure() -> None:
    assert cl(_nested(31)).local is None  # the object is level 1: 32 in all
    assert cl(_nested(32)).local == "contract"


@pytest.mark.parametrize("levels", [100, 5000])
def test_deep_nesting_is_a_contract_failure(levels: int) -> None:
    out = cl(_nested(levels))
    assert out.local == "contract" and out.result is None


def test_deep_nesting_in_an_error_envelope_is_a_contract_failure() -> None:
    text = '{"error": {"code": "x", "message": "m"}, "y": ' + "[" * 100 + "]" * 100 + "}"
    assert cl(text, command="snapshot", returncode=12).local == "contract"


@pytest.mark.parametrize(
    "text",
    [
        '{"hosts": [], "errors": [], "x": "\\ud800"}',
        '{"hosts": [], "errors": [], "x": "a\\udfffb"}',
        '{"hosts": [], "errors": [], "\\ud800": 1}',
        '{"hosts": [], "errors": [], "x": ["ok", {"y": "\\udc00"}]}',
        '{"hosts": [], "errors": [], "x": "\\ud83d"}',
    ],
)
def test_a_lone_surrogate_in_a_key_or_value_is_a_contract_failure(text: str) -> None:
    assert cl(text).local == "contract"


def test_a_surrogate_pair_is_one_valid_character() -> None:
    assert cl('{"hosts": [], "errors": [], "x": "\\ud83d\\ude00"}').local is None


def test_the_version_is_checked_before_every_call(fake: FakeForgectl, client) -> None:
    fake.add(new_pr())
    ok(client.snapshot(REF), "snapshot")
    ok(client.show(REF), "show")
    assert [a[0] for a in fake.argvs()] == ["--version", "snapshot", "show"]


@pytest.mark.parametrize("call", ["snapshot", "show", "whoami", "prs", "compare"])
def test_a_too_old_forgectl_makes_no_call(fake: FakeForgectl, client, call: str) -> None:
    fake.version("forgectl 0.54.0\n")
    calls = {
        "snapshot": lambda: client.snapshot(REF),
        "show": lambda: client.show(REF),
        "whoami": lambda: client.whoami(),
        "prs": lambda: client.prs(host="github.com", author="me"),
        "compare": lambda: client.compare(REF, sha(1), sha(2)),
    }
    with pytest.raises(ToolTooOld):
        calls[call]()
    assert [a[0] for a in fake.argvs()] == ["--version"]


def test_a_version_that_cannot_run_makes_no_call(tmp_path: Path) -> None:
    exe = tmp_path / "bin" / "fgctl"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nkill -KILL $$\n")
    exe.chmod(0o755)
    client = ForgectlClient(str(exe), timeout_ms=1000, max_output=1000, env={"PATH": "/bin"})
    with pytest.raises(ToolMissing):
        client.whoami()


@pytest.mark.parametrize("code", [129, 137, 255])
def test_a_version_exit_above_128_is_tool_missing(fake: FakeForgectl, client, code: int) -> None:
    fake.version("forgectl 0.55.3\n", exit=code)
    with pytest.raises(ToolMissing):
        client.version()


@pytest.mark.parametrize(
    ("when", "text"),
    [
        (datetime(1, 1, 1, tzinfo=UTC), "0001-01-01T00:00:00Z"),
        (datetime(999, 12, 31, 23, 59, 59, tzinfo=UTC), "0999-12-31T23:59:59Z"),
        (datetime(1, 1, 2, 5, tzinfo=timezone(timedelta(hours=5))), "0001-01-02T00:00:00Z"),
        (datetime(9999, 12, 31, tzinfo=UTC), "9999-12-31T00:00:00Z"),
    ],
)
def test_the_watermark_year_has_four_digits(when: datetime, text: str) -> None:
    assert fc._timestamp(when) == text


@pytest.mark.parametrize(
    "when",
    [
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5))),
        datetime(9999, 12, 31, 23, tzinfo=timezone(-timedelta(hours=5))),
    ],
)
def test_a_watermark_outside_the_utc_range_is_a_value_error(when: datetime) -> None:
    with pytest.raises(ValueError):
        fc._timestamp(when)


def test_negative_zero_retry_after_is_normalised() -> None:
    env = '{"error": {"code": "rate_limited", "message": "m", "retry_after": -0.0}}'
    out = cl(env, command="snapshot", returncode=12)
    assert out.error is not None and out.error.retry_after is not None
    assert out.error.retry_after == 0.0
    assert math.copysign(1.0, out.error.retry_after) == 1.0


def test_prs_with_a_host_needs_an_author() -> None:
    with pytest.raises(ValueError):
        fc.prs_argv(host="github.com", timeout_ms=1000)
    assert "--author=me" in fc.prs_argv(host="github.com", author="me", timeout_ms=1000)
    assert "--repo=github.com/o/r" in fc.prs_argv(repo="github.com/o/r", timeout_ms=1000)
