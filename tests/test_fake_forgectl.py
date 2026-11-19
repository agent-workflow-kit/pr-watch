"""The fake forgectl (tests/fakebin/forgectl) against forgectl v0.55.3's documented behavior."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.forgectl_support import (
    GOLDEN_DIR,
    SCHEMAS_DIR,
    FakeForgectl,
    assert_forgectl_valid,
    fake_module,
    forgectl_errors,
    golden,
    new_pr,
    pin,
    sha,
)

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
PR = "github.com/example-org/widgets#7"


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    fake_forgectl.host("github.com", me="user-1")
    return fake_forgectl


def run_json(fake: FakeForgectl, *argv: str) -> tuple[int, dict, str]:
    proc = fake.run(*argv, "--json")
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else {}), proc.stderr


# --- the pin -------------------------------------------------------------------------------


def test_pin_records_version_and_source_commit() -> None:
    p = pin()
    assert p["forgectl_version"] == "0.55.3"
    assert re.fullmatch(r"[0-9a-f]{40}", p["forgectl_commit"])
    for name in p["schemas"]:
        assert (SCHEMAS_DIR / f"{name}.json").is_file()


def test_fake_version_is_the_pinned_version() -> None:
    fake_source = (FAKEBIN / "_forgectl_fake.py").read_text(encoding="utf-8")
    assert re.search(rf'^VERSION = "{re.escape(pin()["forgectl_version"])}"$', fake_source, re.M)


@pytest.mark.parametrize(
    ("golden_name", "schema"),
    [
        ("snapshot-github-open", "snapshot"),
        ("snapshot-github-no-push-time", "snapshot"),
        ("snapshot-gitlab-open", "snapshot"),
        ("prs-github-open", "prs"),
        ("prs-gitlab-repo", "prs"),
        ("show-github-merged", "show"),
        ("show-github-open-clean", "show"),
        ("show-gitlab-open-ready", "show"),
        ("compare-github-ahead", "compare"),
        ("compare-gitlab-ahead", "compare"),
        ("whoami-github", "whoami"),
        ("whoami-gitlab", "whoami"),
    ],
)
def test_pinned_goldens_validate_against_pinned_schemas(golden_name: str, schema: str) -> None:
    assert_forgectl_valid(schema, golden(golden_name))


def test_pinned_schema_rejects_a_broken_document() -> None:
    broken = golden("snapshot-github-open")
    del broken["pr_id"]
    assert forgectl_errors("snapshot", broken)


def test_pinned_data_has_only_synthetic_names() -> None:
    text = "".join(p.read_text(encoding="utf-8") for p in GOLDEN_DIR.glob("*.json"))
    hosts = set(re.findall(r"https?://([a-z0-9.-]+)", text))
    assert hosts <= {"github.com", "gitlab.com", "ci.example.test", "ghe.example.test"} | {
        h for h in hosts if h.endswith(".example.test")
    }
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)


# --- --version and the argv surface --------------------------------------------------------


def test_version_is_plain_text_on_stdout(fake: FakeForgectl) -> None:
    proc = fake.run("--version")
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "forgectl 0.55.3\n", "")
    assert re.fullmatch(r"forgectl (\d+)\.(\d+)\.(\d+)\n", proc.stdout)


def test_version_after_a_command_is_a_usage_error(fake: FakeForgectl) -> None:
    code, out, err = run_json(fake, "whoami", "--version")
    assert code == 2 and out["error"]["code"] == "usage"
    assert err.startswith("forgectl: error: usage: ")


@pytest.mark.parametrize(
    "argv",
    [
        ["prs", "--author", "me", "--host", "github.com", "--limit", "0"],
        ["prs", "--author", "me", "--limit", "1001"],
        ["prs", "--author", "me", "--limit", "x"],
        ["prs", "--host", "github.com"],  # needs --author or --review-requested-from
        ["prs", "--repo", "github.com/example-org/widgets", "--host", "github.com"],
        ["prs", "--author", "me", "--state", "bogus"],
        ["prs", "--author", "me", "--state", "open", "--state", "all"],  # given twice
        ["prs", "--author", "me", "--json", "--json"],
        ["prs", "--author", "me", "--stat", "open"],  # no abbreviations
        ["prs", "--author", "me", "--draft", "--no-draft"],
        ["prs", "--author", "me", "--updated-since", "2026-10-01T00:00:00"],  # no offset
        ["prs", "--author", "me", "--updated-since", "yesterday"],
        ["prs", "--author", "-me"],
        ["snapshot", "-https://github.com/example-org/widgets/pull/7"],  # an option, not a PR
        ["prs", "--author", "me", "--verbose", "--quiet"],
        ["--yes", "whoami"],
        ["whoami", "--timeout", "0"],
        ["bogus"],
        [],
    ],
)
def test_usage_errors_exit_2_with_the_envelope(fake: FakeForgectl, argv: list[str]) -> None:
    proc = fake.run(*argv, "--json")
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["error"]["code"] == "usage"
    assert proc.stderr.startswith("forgectl: error: usage: ")
    assert proc.stderr.count("\n") == 1


def test_a_usage_error_without_json_prints_nothing_on_stdout(fake: FakeForgectl) -> None:
    proc = fake.run("prs")
    assert proc.returncode == 2 and proc.stdout == "" and "usage" in proc.stderr


@pytest.mark.parametrize(
    "pr",
    [
        "github.com/example-org/widgets#0",
        "github.com/example-org/widgets#01",
        "github.com/example-org/widgets#7abc",
        "github.com/example-org/widgets",
        "https://github.com/example-org/widgets/issues/7",
        "github.com/example-org/widgets/extra#7",  # three segments on GitHub
        "github.com/-evil/widgets#7",
        "github.com/example-org/../widgets#7",
        "user@github.com/example-org/widgets#7",
        "github.com/example-org/widgets#7\n",
        "https://gitlab.com/example-group/proj/-/merge_requests/7",  # a GitLab URL on GitHub
    ],
)
def test_bad_pr_references_are_invalid_pr_exit_2(fake: FakeForgectl, pr: str) -> None:
    fake.host("gitlab.com")
    if "gitlab" in pr:
        pr = pr.replace("gitlab.com/example-group/proj", "github.com/example-org/widgets")
    code, out, _ = run_json(fake, "snapshot", pr)
    assert code == 2 and out["error"]["code"] == "invalid_pr"
    assert fake.calls("snapshot")[0].exit == 2


def test_an_undeclared_host_is_refused_exit_20(fake: FakeForgectl) -> None:
    for argv in (
        ["snapshot", "git.example.test/org/repo#1"],
        ["prs", "--host", "git.example.test", "--author", "me"],
        ["prs", "--repo", "git.example.test/org/repo"],
    ):
        code, out, err = run_json(fake, *argv)
        assert code == 20
        assert out["error"]["code"] == "refused" and out["error"]["reason"] == "unknown_host"
        assert "refused (unknown_host)" in err


def test_a_declared_host_is_accepted(fake: FakeForgectl) -> None:
    fake.host("git.example.test", backend="gitlab", me="user-2")
    pr = fake.add(new_pr("git.example.test", "group/sub/proj", 3, backend="gitlab"))
    code, out, _ = run_json(
        fake, "snapshot", "https://git.example.test/group/sub/proj/-/merge_requests/3"
    )
    assert code == 0 and out["pr"] == pr


@pytest.mark.parametrize(
    "argv",
    [
        ["diff", "github.com/example-org/widgets#7"],
        ["snapshots", "github.com/example-org/widgets#7"],
        ["poll"],
        ["--json", "comments", PR],
        ["--timeout", "5s", "ci", PR],
        ["prs", "--author", "me", "--query", "draft"],
        ["prs", "--author", "me", "--default-query"],
    ],
)
def test_real_but_unmodeled_argv_fails_loudly_with_exit_98(
    fake: FakeForgectl, argv: list[str]
) -> None:
    proc = fake.run(*argv)
    assert proc.returncode == 98
    assert proc.stdout == ""
    assert proc.stderr.startswith("fake-forgectl: ")


@pytest.mark.parametrize("argv", [["--help"], ["-h"], ["prs", "--help"], ["whoami", "-h"]])
def test_help_is_exit_0_text_on_stdout(fake: FakeForgectl, argv: list[str]) -> None:
    proc = fake.run(*argv)
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.startswith("usage: forgectl")
    [call] = fake.calls()
    assert call.kind == "help"


def test_top_level_help_lists_every_real_command(fake: FakeForgectl) -> None:
    proc = fake.run("--help")
    for name in ("prs", "show", "snapshot", "compare", "whoami", "diff", "merge", "export"):
        assert re.search(rf"^\s+{name}\s", proc.stdout, re.M), name
    assert "machine-readable output" in proc.stdout


def test_an_invalid_command_names_all_22_real_commands(fake: FakeForgectl) -> None:
    proc = fake.run("bogus", "--json")
    message = json.loads(proc.stdout)["error"]["message"]
    listed = message.split("choose from", 1)[1].strip().rstrip(")")
    names = [name.strip(" '") for name in listed.split(",")]  # quoted or not, by Python version
    assert proc.returncode == 2 and len(names) == 22 and "browse" in names


@pytest.mark.parametrize(
    "argv",
    [
        ["diff"],  # no PR
        ["--bogus", "diff", PR],
        ["merge", PR, "--method", "fast"],
        ["review", PR],  # --file is required
        ["snapshots"],
        ["access", "github.com/example-org/widgets"],
    ],
)
def test_unmodeled_commands_get_real_usage_errors_before_the_fake_refusal(
    fake: FakeForgectl, argv: list[str]
) -> None:
    proc = fake.run(*argv)
    assert proc.returncode == 2, proc.stderr


def test_version_wins_before_an_unmodeled_command(fake: FakeForgectl) -> None:
    proc = fake.run("--version", "diff")
    assert (proc.returncode, proc.stdout) == (0, "forgectl 0.55.3\n")


def test_an_unscripted_compare_is_exit_98_not_a_guess(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    proc = fake.run("compare", PR, "--from", sha(1), "--to", sha(2), "--json")
    assert proc.returncode == 98 and proc.stdout == "" and "unscripted compare" in proc.stderr


def test_no_scenario_is_exit_98(tmp_path: Path, fake_forgectl: FakeForgectl) -> None:
    proc = fake_forgectl.run(
        "--version", env={**os.environ, "FAKE_FORGECTL_DIR": str(tmp_path / "none")}
    )
    assert proc.returncode == 98 and "no scenario" in proc.stderr


# --- snapshot, show, compare ---------------------------------------------------------------


def test_snapshot_prints_one_json_line_equal_to_the_stored_snapshot(fake: FakeForgectl) -> None:
    snap = new_pr()
    fake.add(snap)
    proc = fake.run("snapshot", PR, "--json")
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.count("\n") == 1 and proc.stdout.endswith("\n")
    assert json.loads(proc.stdout) == snap
    assert proc.stdout.isascii()


def test_snapshot_accepts_global_flags_before_or_after_the_command(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    a = fake.run("--json", "--timeout", "30s", "snapshot", PR)
    b = fake.run("snapshot", PR, "--timeout", "30s", "--json")
    assert a.returncode == b.returncode == 0 and a.stdout == b.stdout


def test_snapshot_resolves_urls_and_case_to_the_canonical_pr(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    code, out, _ = run_json(fake, "snapshot", "https://GitHub.com/EXAMPLE-ORG/widgets/pull/7/files")
    assert code == 0 and out["pr"] == PR


def test_snapshot_not_found_is_exit_10(fake: FakeForgectl) -> None:
    code, out, err = run_json(fake, "snapshot", "github.com/example-org/widgets#99")
    assert code == 10 and out["error"]["code"] == "not_found"
    assert err.startswith("forgectl: error: not_found: ")


def test_snapshot_no_bodies_drops_review_and_comment_bodies_only(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    _, full, _ = run_json(fake, "snapshot", PR)
    _, lean, _ = run_json(fake, "snapshot", PR, "--no-bodies")
    assert any("body" in r for r in full["reviews"])
    assert all("body" not in r for r in lean["reviews"])
    assert all("body" not in c for t in lean["threads"] for c in t["comments"])
    assert lean["body"] == full["body"]


def test_show_is_the_pr_object_without_the_snapshot_parts(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    code, out, _ = run_json(fake, "show", PR)
    assert code == 0
    assert "threads" not in out and "ci" not in out and "pushed_at" not in out["head"]


def test_compare_returns_the_scripted_status_and_diff(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.compare(PR, sha(1), sha(2), "ahead", "diff --git a/x b/x\n")
    fake.compare(PR, sha(2), sha(3), "diverged")
    code, ahead, _ = run_json(fake, "compare", PR, "--from", sha(1), "--to", sha(2))
    assert code == 0 and ahead["status"] == "ahead" and ahead["diff"].startswith("diff --git")
    _, diverged, _ = run_json(fake, "compare", PR, "--from", sha(2).upper(), "--to", sha(3))
    assert diverged["status"] == "diverged" and diverged["diff"] is None
    _, lean, _ = run_json(fake, "compare", "--no-diff", PR, "--from", sha(1), "--to", sha(2))
    assert "diff" not in lean and lean["status"] == "ahead"


@pytest.mark.parametrize("flags", [[], ["--from", sha(1)], ["--from", "abc", "--to", sha(2)]])
def test_compare_needs_full_shas_and_both_ends(fake: FakeForgectl, flags: list[str]) -> None:
    fake.add(new_pr())
    code, out, _ = run_json(fake, "compare", PR, *flags)
    assert code == 2 and out["error"]["code"] == "usage"


# --- prs -----------------------------------------------------------------------------------


def seed_prs(fake: FakeForgectl) -> None:
    fake.add(new_pr(number=1, updated_at="2026-10-01T10:00:00Z"))
    fake.add(new_pr(number=2, updated_at="2026-10-03T10:00:00Z", state="merged"))
    fake.add(new_pr(number=3, updated_at="2026-10-02T10:00:00Z", draft=True))
    fake.add(new_pr(number=4, updated_at="2026-10-02T10:00:00Z", author={"login": "user-9"}))
    fake.add(new_pr(repo="example-org/gadgets", number=5, updated_at="2026-09-01T10:00:00Z"))


def numbers(out: dict) -> list[str]:
    return [i["pr"].split("/", 1)[1] for i in out["items"]]


def test_prs_lists_mine_open_newest_first(fake: FakeForgectl) -> None:
    seed_prs(fake)
    code, out, _ = run_json(
        fake, "prs", "--host", "github.com", "--author", "me", "--state", "open"
    )
    assert code == 0
    assert numbers(out) == [
        "example-org/widgets#3",
        "example-org/widgets#1",
        "example-org/gadgets#5",
    ]
    assert (
        out["complete"] is True and out["errors"] == [] and out["hosts_queried"] == ["github.com"]
    )
    assert out["rate_limit"] == {
        "host": "github.com", "remaining": 4000, "reset_at": "2026-10-02T12:00:00Z",
    }  # fmt: skip


def test_prs_default_state_is_open_and_all_includes_merged(fake: FakeForgectl) -> None:
    seed_prs(fake)
    _, default, _ = run_json(fake, "prs", "--author", "me")
    _, everything, _ = run_json(fake, "prs", "--author", "me", "--state", "all")
    _, merged, _ = run_json(fake, "prs", "--author", "me", "--state", "merged")
    assert "example-org/widgets#2" not in numbers(default)
    assert "example-org/widgets#2" in numbers(everything)
    assert numbers(merged) == ["example-org/widgets#2"]


def test_prs_repo_filter_draft_and_author(fake: FakeForgectl) -> None:
    seed_prs(fake)
    _, out, _ = run_json(fake, "prs", "--repo", "GitHub.com/example-org/Gadgets")
    assert numbers(out) == ["example-org/gadgets#5"]  # --repo alone needs no author
    _, out, _ = run_json(fake, "prs", "--author", "me", "--draft")
    assert numbers(out) == ["example-org/widgets#3"]
    _, out, _ = run_json(fake, "prs", "--author", "USER-9")
    assert numbers(out) == ["example-org/widgets#4"]


def test_prs_updated_since_is_inclusive_and_takes_durations(fake: FakeForgectl) -> None:
    seed_prs(fake)
    _, out, _ = run_json(fake, "prs", "--author", "me", "--updated-since", "2026-10-02T10:00:00Z")
    assert numbers(out) == ["example-org/widgets#3"]
    fake.set_now("2026-10-03T00:00:00Z")
    _, out, _ = run_json(fake, "prs", "--author", "me", "--updated-since", "36h")
    assert numbers(out) == ["example-org/widgets#3"]


def test_prs_limit_cuts_and_marks_incomplete(fake: FakeForgectl) -> None:
    seed_prs(fake)
    _, out, _ = run_json(fake, "prs", "--author", "me", "--limit", "2")
    assert len(out["items"]) == 2 and out["complete"] is False


def test_prs_items_carry_the_listing_override(fake: FakeForgectl) -> None:
    pr = fake.add(new_pr(), listing={"updated_at": "2026-10-09T00:00:00Z"})
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert out["items"][0]["updated_at"] == "2026-10-09T00:00:00Z"
    _, snap, _ = run_json(fake, "snapshot", pr)
    assert snap["updated_at"] != out["items"][0]["updated_at"]


def test_prs_hidden_repo_is_never_listed_and_not_a_flag(fake: FakeForgectl) -> None:
    seed_prs(fake)
    fake.hide_repo("github.com/example-org/gadgets")
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert "example-org/gadgets#5" not in numbers(out) and out["complete"] is True


def test_prs_with_no_host_in_scope_is_empty_and_says_so(fake_forgectl: FakeForgectl) -> None:
    code, out, _ = run_json(fake_forgectl, "prs", "--author", "me")
    assert code == 0 and out["items"] == [] and out["complete"] is True
    assert out["hosts_queried"] == []
    assert out["rate_limit"] == {"host": None, "remaining": None, "reset_at": None}


def test_prs_scope_without_host_is_the_declared_hosts(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.add(new_pr("gitlab.com", "example-group/proj", 3, author={"login": "user-3"}))
    fake.add(new_pr())
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert out["hosts_queried"] == ["github.com", "gitlab.com"]
    assert sorted(numbers(out)) == ["example-group/proj#3", "example-org/widgets#7"]


def test_prs_gitlab_without_rate_limit_headers_reports_nulls(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3", remaining=None)
    _, out, _ = run_json(fake, "prs", "--host", "gitlab.com", "--author", "me")
    assert out["rate_limit"] == {"host": "gitlab.com", "remaining": None, "reset_at": None}


# --- whoami --------------------------------------------------------------------------------


def test_whoami_lists_built_ins_and_declared_hosts_sorted(fake: FakeForgectl) -> None:
    fake.host("git.example.test", backend="gitlab", me="user-2")
    code, out, _ = run_json(fake, "whoami")
    assert code == 0 and out["errors"] == []
    assert [h["host"] for h in out["hosts"]] == ["git.example.test", "github.com", "gitlab.com"]
    gitlab = out["hosts"][2]
    assert gitlab["token_source"] == "none" and gitlab["login"] is None


def test_whoami_host_filter_and_mismatch_warning(fake: FakeForgectl) -> None:
    fake.host(
        "github.com",
        me="user-1",
        whoami={"login": "user-2", "id": "1", "me": "user-1", "me_matches_login": False,
                "token_source": "token_env", "scopes": None},
    )  # fmt: skip
    proc = fake.run("whoami", "--host", "github.com", "--json")
    assert proc.returncode == 0
    assert [h["host"] for h in json.loads(proc.stdout)["hosts"]] == ["github.com"]
    assert proc.stderr == (
        "forgectl: warning: github.com: me is user-1 but the authenticated login is user-2;"
        " prs uses me, posts and rules act as the authenticated account\n"
    )


def test_whoami_host_failure_is_partial_results_with_the_object(fake: FakeForgectl) -> None:
    fake.auth_required("whoami", host="github.com")
    proc = fake.run("whoami", "--host", "github.com", "--json")
    assert proc.returncode == 12
    out = json.loads(proc.stdout)
    assert out["errors"][0]["code"] == "auth_required"
    assert "partial_results" in proc.stderr


def test_whoami_failed_host_keeps_its_entry_with_nulls_and_token_source(
    fake: FakeForgectl,
) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.auth_required("whoami", host="github.com")
    proc = fake.run("whoami", "--json")
    out = json.loads(proc.stdout)
    assert proc.returncode == 12
    failed = out["hosts"][0]
    assert failed["host"] == "github.com" and failed["token_source"] == "token_env"
    assert all(failed[k] is None for k in ("login", "id", "me", "me_matches_login", "scopes"))
    assert out["hosts"][1]["login"] == "user-3" and len(out["errors"]) == 1
    assert proc.stderr.splitlines()[-1] == (
        "forgectl: error: partial_results: 1 of 2 hosts failed: github.com (auth_required)"
    )


# --- scripted failures: every class in the interface's Failures table ----------------------


@pytest.mark.parametrize("retry_after", [None, 90.0])
def test_rate_limited_exit_12_with_and_without_retry_after(fake: FakeForgectl, retry_after) -> None:
    fake.add(new_pr())
    fake.rate_limited("snapshot", retry_after=retry_after)
    code, out, err = run_json(fake, "snapshot", PR)
    assert code == 12 and out["error"]["code"] == "rate_limited"
    assert ("retry_after" in out["error"]) == (retry_after is not None)
    assert out["error"].get("retry_after") == retry_after
    assert err.startswith("forgectl: error: rate_limited: ")


@pytest.mark.parametrize(
    ("method", "code", "exit_code"),
    [("network_error", "network_error", 12), ("not_found", "not_found", 10),
     ("permission_denied", "permission_denied", 12), ("auth_required", "auth_required", 12)],
)  # fmt: skip
def test_error_classes(fake: FakeForgectl, method: str, code: str, exit_code: int) -> None:
    fake.add(new_pr())
    getattr(fake, method)("snapshot")
    got, out, _ = run_json(fake, "snapshot", PR)
    assert (got, out["error"]["code"]) == (exit_code, code)


@pytest.mark.parametrize(
    ("code", "extra", "exit_code"),
    [
        ("host_rejected", {"details": {"status": 422}}, 12),
        ("upstream_error", {}, 12),
        ("wrong_host_response", {"details": {"observed": "elsewhere.example.test"}}, 12),
        ("unsupported_host_version", {}, 12),
        ("tool_missing", {"details": {"install": "install gh"}}, 12),
        ("internal", {}, 1),
        ("refused", {"reason": "unknown_host"}, 20),
    ],
)
def test_other_error_codes_use_the_documented_exit(
    fake: FakeForgectl, code, extra, exit_code
) -> None:
    fake.add(new_pr())
    fake.fail("show", code, **extra)
    got, out, _ = run_json(fake, "show", PR)
    assert got == exit_code and out["error"]["code"] == code


def test_too_large_is_a_refusal_of_compare(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.fail("compare", "refused", reason="too_large")
    got, out, _ = run_json(fake, "compare", PR, "--from", sha(1), "--to", sha(2))
    assert got == 20 and out["error"]["reason"] == "too_large"


@pytest.mark.parametrize(
    ("command", "code", "reason"),
    [
        ("show", "refused", "stale_head"),
        ("show", "refused", "too_large"),
        ("snapshot", "refused", "not_open"),
        ("prs", "refused", "too_large"),
        ("whoami", "refused", "too_large"),
        ("prs", "invalid_pr", None),
        ("whoami", "invalid_pr", None),
        ("show", "vetoed", None),
        ("show", "merge_unverified", None),
        ("snapshot", "partial_results", None),
        ("prs", "confirmation_required", None),
    ],
)
def test_an_error_the_command_cannot_end_with_is_refused(
    fake: FakeForgectl, command: str, code: str, reason: str | None
) -> None:
    with pytest.raises(ValueError, match="cannot"):
        fake.fail(command, code, reason=reason)


def test_the_fake_refuses_an_impossible_error_written_straight_into_the_scenario(
    fake: FakeForgectl,
) -> None:
    fake.add(new_pr())
    fake.script("show", {"kind": "error", "code": "refused", "reason": "stale_head"})
    proc = fake.run("show", PR, "--json")
    assert proc.returncode == 98 and "cannot be refused" in proc.stderr


def test_required_details_follow_the_full_real_tables(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.fail("compare", "refused", reason="unknown_host")  # no required details
    fake.fail("show", "config_invalid", details={"key": "hosts"})
    assert fake.run("show", PR, "--json").returncode == 2
    module = fake_module()
    assert module.REQUIRED_DETAILS_BY_CODE["merge_unverified"] == ("observed", "head_sha")
    assert module.REQUIRED_DETAILS_BY_REASON["not_ready"] == ("blocked_by",)


def test_an_envelope_real_forgectl_cannot_print_is_a_fake_bug(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.fail("show", "tool_missing")  # real tool_missing always has details.install
    proc = fake.run("show", PR, "--json")
    assert proc.returncode == 98 and "needs details" in proc.stderr


def test_rules_fire_in_order_after_and_times(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.network_error("snapshot", after=1, times=2)
    codes = [fake.run("snapshot", PR, "--json").returncode for _ in range(5)]
    assert codes == [0, 12, 12, 0, 0]


def test_rules_match_on_pr_and_host(fake: FakeForgectl) -> None:
    other = fake.add(new_pr(number=8))
    fake.add(new_pr())
    fake.not_found("snapshot", pr=other)
    assert fake.run("snapshot", PR, "--json").returncode == 0
    assert fake.run("snapshot", other, "--json").returncode == 10


def test_prs_failure_of_the_only_host_uses_the_ordinary_envelope(fake: FakeForgectl) -> None:
    fake.rate_limited("prs", retry_after=30)
    code, out, _ = run_json(fake, "prs", "--host", "github.com", "--author", "me")
    assert (
        code == 12 and out["error"]["code"] == "rate_limited" and out["error"]["retry_after"] == 30
    )


def test_prs_partial_results_only_when_two_hosts_are_asked(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.add(new_pr())
    fake.add(new_pr("gitlab.com", "example-group/proj", 3))
    fake.network_error("prs", host="gitlab.com")
    proc = fake.run("prs", "--author", "me", "--json")
    assert proc.returncode == 12
    out = json.loads(proc.stdout)
    assert out["complete"] is False and [i["pr"] for i in out["items"]] == [PR]
    assert out["errors"] == [
        {"host": "gitlab.com", "code": "network_error", "message": "could not reach the host"}
    ]
    assert "partial_results" in proc.stderr
    # one host asked: ordinary envelope
    assert (
        json.loads(fake.run("prs", "--host", "gitlab.com", "--author", "me", "--json").stdout)[
            "error"
        ]["code"]
        == "network_error"
    )


def test_garbage_truncated_and_contract_violations_are_unvalidated_exit_0(
    fake: FakeForgectl,
) -> None:
    fake.add(new_pr())
    fake.garbage("snapshot", times=1)
    fake.truncated("snapshot", times=1)
    fake.contract_violation("snapshot", ["pr_id"], times=1)
    outs = [fake.run("snapshot", PR, "--json") for _ in range(3)]
    assert [o.returncode for o in outs] == [0, 0, 0]
    with pytest.raises(json.JSONDecodeError):
        json.loads(outs[0].stdout)
    with pytest.raises(json.JSONDecodeError):
        json.loads(outs[1].stdout)
    assert "pr_id" not in json.loads(outs[2].stdout)


def test_oversize_pads_the_body_and_stays_valid(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("snapshot", 2_000_000)
    proc = fake.run("snapshot", PR, "--json")
    assert proc.returncode == 0 and len(proc.stdout) > 2_000_000
    assert_forgectl_valid("snapshot", json.loads(proc.stdout))


def test_oversize_survives_a_reader_that_goes_away(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("snapshot", 20_000_000)
    proc = subprocess.Popen(
        ["forgectl", "snapshot", PR, "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert proc.stdout is not None
    proc.stdout.read(1000)
    proc.stdout.close()
    assert proc.wait(timeout=30) == 0  # a closed pipe never changes the exit code
    assert proc.stderr is not None
    proc.stderr.close()


def test_oversize_can_pad_exactly_40_mib(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("snapshot", 40 * 1024 * 1024)
    proc = fake.run("snapshot", PR, "--json")
    assert proc.returncode == 0 and len(proc.stdout) >= 40 * 1024 * 1024


def test_sigterm_while_blocked_writing_a_large_answer_exits_143(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("snapshot", 40 * 1024 * 1024)
    proc = start(fake, "snapshot", PR, "--json")  # nobody reads: the write blocks
    try:
        wait_for_start(fake)
        time.sleep(1.5)  # long enough to have built the answer and blocked on the pipe
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 143
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    [call] = fake.calls("snapshot")
    assert (call.exit, call.kind) == (143, "signalled")


def test_old_version_and_failing_version(fake: FakeForgectl) -> None:
    fake.version("forgectl 0.54.9\n", times=1)
    assert fake.run("--version").stdout == "forgectl 0.54.9\n"
    fake.version("", exit=1)
    assert fake.run("--version").returncode == 1


def test_slow_answers_normally_after_the_delay(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.slow("snapshot", 0.4)
    started = time.monotonic()
    assert fake.run("snapshot", PR, "--json").returncode == 0
    assert time.monotonic() - started >= 0.4


def test_hang_respecting_timeout_exits_12_network_error(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.hang("snapshot", seconds=30, respect_timeout=True)
    code, out, _ = run_json(fake, "snapshot", PR, "--timeout", "0.3s")
    assert code == 12 and out["error"]["code"] == "network_error"


# --- processes: signals, hangs, descendants, the call log ----------------------------------


def process_state(pid: int) -> str | None:
    """The state letter of `pid` from /proc, or None when it is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return text.rsplit(")", 1)[1].split()[0]


def start(fake: FakeForgectl, *argv: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["forgectl", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )


def wait_for_start(fake: FakeForgectl, count: int = 1) -> None:
    deadline = time.monotonic() + 10
    while len(fake.calls()) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(fake.calls()) >= count
    time.sleep(0.1)  # past the signal handlers' installation


@pytest.mark.parametrize(
    ("signum", "expected"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
)
def test_a_signal_exits_128_plus_n_with_no_envelope(fake: FakeForgectl, signum, expected) -> None:
    fake.add(new_pr())
    fake.hang("snapshot", seconds=60)
    proc = start(fake, "snapshot", PR, "--json")
    try:
        wait_for_start(fake)
        proc.send_signal(signum)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == expected and out == b"" and err == b""


def test_term_delay_is_the_time_between_the_signal_and_exit_143(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.hang("snapshot", seconds=60)
    fake.term_delay(0.6)
    proc = start(fake, "snapshot", PR, "--json")
    try:
        wait_for_start(fake)
        sent = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 143
        assert time.monotonic() - sent >= 0.6
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def test_ignore_term_hang_needs_sigkill_and_the_call_log_shows_no_exit(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.hang("snapshot", seconds=60, ignore_term=True)
    proc = start(fake, "snapshot", PR, "--json")
    try:
        fake.wait_ready()  # SIGTERM is ignored from here on; before it the fake would die of it
        proc.terminate()
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.5)
        proc.kill()
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    [call] = fake.calls("snapshot")
    assert call.exit is None and call.pid == proc.pid


def test_a_descendant_holding_the_pipes_is_recorded_and_reaped(fake_forgectl: FakeForgectl) -> None:
    fake = fake_forgectl.host("github.com")
    fake.add(new_pr())
    fake.orphan("snapshot", seconds=30)
    proc = start(fake, "snapshot", PR, "--json")
    assert proc.stdout is not None
    first = proc.stdout.readline()
    assert proc.wait(timeout=10) == 0 and json.loads(first)["pr"] == PR
    [call] = fake.calls("snapshot")
    assert len(call.descendants) == 1
    kid = call.descendants[0]
    assert process_state(kid) not in (None, "Z")  # alive before the reap
    fake.reap()
    deadline = time.monotonic() + 5
    while process_state(kid) not in (None, "Z") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process_state(kid) in (None, "Z")  # gone, or killed and not yet reaped by its parent
    proc.stdout.close()
    assert proc.stderr is not None
    proc.stderr.close()


def test_die_by_signal_reports_the_signal_to_the_parent(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.die_by_signal("snapshot", signal.SIGKILL)
    assert fake.run("snapshot", PR, "--json").returncode == -signal.SIGKILL


def test_every_call_is_logged_with_argv_pid_and_env_names(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.run("snapshot", PR, "--json")
    fake.run("--version")
    snap, version = fake.calls()
    assert snap.argv == ["snapshot", PR, "--json"] and snap.command == "snapshot"
    assert snap.exit == 0 and snap.kind == "ok" and snap.pid > 0
    assert "PATH" in snap.env_names and "HOME" in snap.env_names
    assert version.argv == ["--version"] and version.command is None
    assert fake.argvs("snapshot") == [["snapshot", PR, "--json"]]


def test_concurrent_calls_are_all_logged_and_rules_count_exactly(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.network_error("snapshot", times=3)
    procs = [start(fake, "snapshot", PR, "--json") for _ in range(8)]
    codes = []
    for p in procs:
        p.communicate(timeout=30)
        codes.append(p.returncode)
    assert sorted(codes) == [0] * 5 + [12] * 3
    assert len(fake.calls("snapshot")) == 8


def test_the_fake_wins_over_the_guard_stub_and_leaves_no_hit(
    fake: FakeForgectl, sandbox_bin: Path
) -> None:
    assert (sandbox_bin / "hits.log").read_text() == ""
    assert fake.run("--version").returncode == 0
    assert (sandbox_bin / "hits.log").read_text() == ""


def test_human_output_without_json_is_key_value_lines(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    compare_proc = fake.run("show", PR)
    assert compare_proc.returncode == 0
    assert f"pr: {PR}" in compare_proc.stdout.splitlines()


def test_schema_validation_runs_on_every_fake_output_at_teardown(
    fake_forgectl: FakeForgectl,
) -> None:
    fake_forgectl.host("github.com")
    snap = new_pr()
    del snap["pr_id"]  # a broken scenario: the fake would print a document real forgectl never does
    fake_forgectl.add(snap)
    fake_forgectl.run("snapshot", PR, "--json")
    with pytest.raises(AssertionError, match="schema rejected"):
        fake_forgectl.validate_outputs()
    fake_forgectl.scenario["prs"].clear()  # keep the real teardown check green
    fake_forgectl._write()
    (fake_forgectl.dir / "calls.jsonl").write_text("")


# --- scripted actions are honored by every command, or refused loudly ----------------------


def assert_fake_refusal(proc: subprocess.CompletedProcess[str], text: str = "") -> None:
    assert proc.returncode == 98, proc.stderr
    assert proc.stdout == "" and proc.stderr.startswith("fake-forgectl: ")
    assert "Traceback" not in proc.stderr and text in proc.stderr


@pytest.mark.parametrize(
    ("command", "action", "match", "text"),
    [
        ("show", {"kind": "explode"}, {}, "unknown action kind"),
        ("prs", {"kind": "explode"}, {}, "unknown action kind"),
        ("show", {}, {}, "unknown action kind"),
        ("show", {"kind": "error"}, {}, "needs"),
        ("show", {"kind": "pad"}, {}, "needs"),
        ("whoami", {"kind": "pad", "bytes": 10}, {}, "cannot honor"),
        ("--version", {"kind": "truncate"}, {}, "cannot honor"),
        ("show", {"kind": "truncate", "secs": 1}, {}, "no field"),
        ("show", {"kind": "truncate"}, {"prr": PR}, "cannot match on"),
        ("prs", {"kind": "truncate"}, {"pr": PR}, "cannot match on"),
        ("whoami", {"kind": "truncate"}, {"repo": "github.com/x/y"}, "cannot match on"),
    ],
)
def test_a_rule_the_command_cannot_honor_is_exit_98_for_every_call(
    fake: FakeForgectl, command: str, action: dict, match: dict, text: str
) -> None:
    fake.add(new_pr())
    fake.script(command, action, **match)
    for argv in (["show", PR], ["prs", "--author", "me"], ["whoami"], ["--version"]):
        assert_fake_refusal(fake.run(*argv, "--json"), text)


def test_an_unknown_command_in_a_rule_is_exit_98(fake: FakeForgectl) -> None:
    fake.script("diff", {"kind": "truncate"})
    assert_fake_refusal(fake.run("whoami"), "unknown command")


@pytest.mark.parametrize(
    ("scenario_text", "state_text", "text"),
    [
        ("{", None, "JSONDecodeError"),
        ("[]", None, "must be an object"),
        (None, "", "state.json"),
        (None, "{", "state.json"),
        (None, "[]", "state.json"),
    ],
)
def test_a_malformed_scenario_or_state_file_is_exit_98_never_a_traceback(
    fake: FakeForgectl, scenario_text: str | None, state_text: str | None, text: str
) -> None:
    fake.add(new_pr())
    fake.network_error("snapshot")
    if scenario_text is not None:
        (fake.dir / "scenario.json").write_text(scenario_text)
    if state_text is not None:
        (fake.dir / "state.json").write_text(state_text)
    assert_fake_refusal(fake.run("snapshot", PR, "--json"), text)


def test_state_is_replaced_whole_and_counts_across_calls(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.network_error("snapshot", times=1)
    assert fake.run("snapshot", PR, "--json").returncode == 12
    state = json.loads((fake.dir / "state.json").read_text())
    assert state["0"] == {"seen": 1, "fired": 1}
    assert not list(fake.dir.glob("state.json.*.tmp"))


def test_prs_and_whoami_honor_slow(fake: FakeForgectl) -> None:
    fake.slow("prs", 0.4)
    fake.slow("whoami", 0.4)
    for argv in (["prs", "--author", "me"], ["whoami"]):
        started = time.monotonic()
        assert fake.run(*argv, "--json").returncode == 0
        assert time.monotonic() - started >= 0.4


def test_prs_and_whoami_honor_raw_garbage(fake: FakeForgectl) -> None:
    fake.garbage("prs", stdout="not json", exit=3)
    fake.raw("whoami", stdout="x", stderr="y\n", exit=4)
    prs = fake.run("prs", "--author", "me", "--json")
    who = fake.run("whoami", "--json")
    assert (prs.returncode, prs.stdout) == (3, "not json")
    assert (who.returncode, who.stdout, who.stderr) == (4, "x", "y\n")


def test_prs_and_whoami_honor_truncate_and_drop(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.truncated("prs", times=1)
    fake.contract_violation("prs", ["hosts_queried"], times=1)
    fake.contract_violation("whoami", ["errors"])
    cut = fake.run("prs", "--author", "me", "--json")
    with pytest.raises(json.JSONDecodeError):
        json.loads(cut.stdout)
    assert "hosts_queried" not in json.loads(fake.run("prs", "--author", "me", "--json").stdout)
    assert "errors" not in json.loads(fake.run("whoami", "--json").stdout)


def test_prs_and_whoami_honor_orphan(fake: FakeForgectl) -> None:
    fake.orphan("prs", seconds=5)
    fake.orphan("whoami", seconds=5, host="github.com")
    assert fake.run("prs", "--author", "me", "--json").returncode == 0
    assert fake.run("whoami", "--json").returncode == 0
    assert [len(c.descendants) for c in fake.calls()] == [1, 1]


# --- pad and drop work on the result, with or without --json -------------------------------


def test_pad_and_drop_without_json(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("snapshot", 100_000, times=1)
    fake.contract_violation("show", ["pr_id"], times=1)
    padded = fake.run("snapshot", PR)
    assert padded.returncode == 0 and len(padded.stdout) > 100_000
    dropped = fake.run("show", PR)
    assert dropped.returncode == 0 and "pr_id:" not in dropped.stdout


def test_pad_on_prs_makes_a_title_longer(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.oversize("prs", 50_000)
    out = json.loads(fake.run("prs", "--author", "me", "--json").stdout)
    assert len(out["items"][0]["title"]) > 50_000
    assert_forgectl_valid("prs", out)


def test_pad_on_prs_with_nothing_listed_is_exit_98(fake: FakeForgectl) -> None:
    fake.oversize("prs", 50_000)
    assert_fake_refusal(fake.run("prs", "--author", "me"), "pad needs")


def test_a_partial_prs_that_cannot_be_padded_is_a_clean_refusal(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.fail("prs", "network_error", host="gitlab.com")
    fake.oversize("prs", 50_000)
    proc = fake.run("prs", "--author", "x", "--json")
    assert "Traceback" not in proc.stderr
    assert_fake_refusal(proc, "pad needs")


def test_one_pad_rule_on_two_hosts_pads_once(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.add(new_pr())
    fake.oversize("prs", 50_000)
    out = json.loads(fake.run("prs", "--author", "me", "--json").stdout)
    assert 50_000 < len(out["items"][0]["title"]) < 100_000


# --- prs with several hosts ----------------------------------------------------------------


def test_a_hang_of_one_host_of_two_is_that_hosts_error(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.add(new_pr())
    fake.hang("prs", seconds=0.2, host="gitlab.com")
    proc = fake.run("prs", "--author", "me", "--json")
    out = json.loads(proc.stdout)
    assert proc.returncode == 12 and [i["pr"] for i in out["items"]] == [PR]
    assert [e["host"] for e in out["errors"]] == ["gitlab.com"]
    assert out["errors"][0]["code"] == "network_error" and out["complete"] is False


def test_prs_rule_counters_count_per_host(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.add(new_pr())
    fake.network_error("prs", after=1, times=1)  # github.com is the first ask, gitlab.com second
    out = json.loads(fake.run("prs", "--author", "me", "--json").stdout)
    assert [e["host"] for e in out["errors"]] == ["gitlab.com"]


def test_prs_partial_results_stderr_line(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    fake.network_error("prs", host="github.com")
    proc = fake.run("prs", "--author", "me", "--json")
    assert proc.stderr == (
        "forgectl: error: partial_results: 1 of 2 hosts failed: github.com (network_error)\n"
    )


def test_prs_rate_limit_is_null_with_several_hosts_and_no_reading(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3", remaining=None)
    fake.host("github.com", me="user-1", remaining=None)
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert out["rate_limit"] == {"host": None, "remaining": None, "reset_at": None}


def test_prs_rate_limit_is_the_lowest_reading_of_the_hosts_that_answered(
    fake: FakeForgectl,
) -> None:
    fake.host("gitlab.com", me="user-3", remaining=10, reset_at="2026-10-02T13:00:00Z")
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert out["rate_limit"]["host"] == "gitlab.com" and out["rate_limit"]["remaining"] == 10
    fake.network_error("prs", host="gitlab.com")
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert out["rate_limit"]["host"] == "github.com"  # a failed host gave no reading


def test_prs_of_a_hidden_repo_makes_no_call_and_has_no_reading(fake: FakeForgectl) -> None:
    fake.add(new_pr())
    fake.hide_repo("github.com/example-org/widgets")
    fake.network_error("prs")  # would fire if a host were asked
    code, out, _ = run_json(fake, "prs", "--repo", "github.com/example-org/widgets")
    assert code == 0 and out["items"] == [] and out["complete"] is True
    assert out["rate_limit"] == {"host": "github.com", "remaining": None, "reset_at": None}


def test_hidden_repos_are_globs(fake: FakeForgectl) -> None:
    seed_prs(fake)
    fake.add(new_pr(repo="other-org/tools", number=9))
    fake.hide_repo("GitHub.com/EXAMPLE-ORG/*")
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert numbers(out) == ["other-org/tools#9"]


@pytest.mark.parametrize(
    ("pattern", "hidden"),
    [
        ("github.com/example-org/wid?ets", True),
        ("github.com/*", True),
        ("*/widgets", True),
        ("github.com/example-org", False),
        ("github.com/example-org/w", False),
        ("github.com/example-org/widget??", False),
    ],
)
def test_hidden_glob_grammar(fake: FakeForgectl, pattern: str, hidden: bool) -> None:
    fake.add(new_pr())
    fake.hide_repo(pattern)
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert (out["items"] == []) is hidden


# --- prs input that real forgectl rejects --------------------------------------------------


@pytest.mark.parametrize("flag", ["--author", "--review-requested-from"])
def test_the_reserved_spelling_of_me_is_a_usage_error(fake: FakeForgectl, flag: str) -> None:
    code, out, err = run_json(fake, "prs", flag, "ME")
    assert code == 2 and out["error"]["code"] == "usage"
    assert "`me` is reserved" in err


@pytest.mark.parametrize("since", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
def test_updated_since_outside_years_1_to_9999_in_utc_is_usage(
    fake: FakeForgectl, since: str
) -> None:
    code, out, _ = run_json(fake, "prs", "--author", "me", "--updated-since", since)
    assert code == 2 and "years 1 to 9999" in out["error"]["message"]


def test_updated_since_at_the_edge_of_the_range_is_fine(fake: FakeForgectl) -> None:
    code, _, _ = run_json(
        fake, "prs", "--author", "me", "--updated-since", "0001-01-01T05:00:00+05:00"
    )
    assert code == 0


def test_a_login_must_fit_the_host_backend_grammar(fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-3")
    # valid on GitLab, not on GitHub
    code, out, _ = run_json(fake, "prs", "--host", "gitlab.com", "--author", "user.name")
    assert code == 0
    code, out, _ = run_json(fake, "prs", "--host", "github.com", "--author", "user.name")
    assert code == 2 and "not a valid github login" in out["error"]["message"]
    code, out, _ = run_json(fake, "prs", "--author", "user.name")  # github.com is in scope too
    assert code == 2 and out["error"]["code"] == "usage"


def test_me_falls_back_to_the_scripted_whoami_login(fake_forgectl: FakeForgectl) -> None:
    fake = fake_forgectl
    who = {"login": "user-5", "id": "5", "me": None, "me_matches_login": None,
           "token_source": "token_env", "scopes": None}  # fmt: skip
    fake.host("github.com", me=None, whoami=who)
    fake.add(new_pr(author={"login": "user-5"}))
    _, out, _ = run_json(fake, "prs", "--author", "me")
    assert numbers(out) == ["example-org/widgets#7"]


def test_me_with_neither_a_configured_me_nor_a_whoami_login_is_exit_98(
    fake_forgectl: FakeForgectl,
) -> None:
    fake_forgectl.host("github.com", me=None)
    fake_forgectl.add(new_pr())
    assert_fake_refusal(fake_forgectl.run("prs", "--author", "me", "--json"), "`me` needs")
    assert fake_forgectl.run("prs", "--author", "user-1", "--json").returncode == 0


# --- --config ------------------------------------------------------------------------------


def config_error(fake: FakeForgectl, path: Path, text: str) -> None:
    proc = fake.run("whoami", "--config", str(path), "--json")
    error = json.loads(proc.stdout)["error"]
    assert proc.returncode == 2 and error["code"] == "config_invalid"
    assert error["details"] == {"key": str(path)}
    assert error["message"] == f"{path}: {text}"


def test_a_missing_config_is_config_invalid_naming_the_path(
    fake: FakeForgectl, tmp_path: Path
) -> None:
    config_error(fake, tmp_path / "none.yaml", "no such file, or a symbolic link to nothing")


def test_a_directory_is_not_a_regular_file(fake: FakeForgectl, tmp_path: Path) -> None:
    config_error(fake, tmp_path, "not a regular file")


def test_invalid_yaml_is_config_invalid_with_the_line(fake: FakeForgectl, tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("a: 1\nb: [unclosed\n")
    proc = fake.run("whoami", "--config", str(path), "--json")
    message = json.loads(proc.stdout)["error"]["message"]
    assert proc.returncode == 2 and message.startswith(f"{path}: invalid YAML at line ")
    assert message.endswith(": syntax error")


def test_a_top_level_list_and_bad_utf8_are_config_invalid(
    fake: FakeForgectl, tmp_path: Path
) -> None:
    listed = tmp_path / "list.yaml"
    listed.write_text("- a\n")
    config_error(fake, listed, "the top level must be a mapping")
    binary = tmp_path / "bin.yaml"
    binary.write_bytes(b"\xff\xfe\x00")
    config_error(fake, binary, "not valid UTF-8")


def test_a_valid_config_is_accepted_and_relative_paths_are_made_absolute(
    fake: FakeForgectl, tmp_path: Path
) -> None:
    (tmp_path / "ok.yaml").write_text("hosts: {}\n")
    assert fake.run("whoami", "--config", str(tmp_path / "ok.yaml")).returncode == 0
    proc = subprocess.run(
        ["forgectl", "whoami", "--config", "nope.yaml", "--json"],
        capture_output=True, text=True, cwd=tmp_path, check=False, timeout=30,
        env={**os.environ, "FAKE_FORGECTL_DIR": str(fake.dir)},
    )  # fmt: skip
    assert json.loads(proc.stdout)["error"]["details"] == {"key": str(tmp_path / "nope.yaml")}
