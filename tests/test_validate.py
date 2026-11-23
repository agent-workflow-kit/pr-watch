"""`validate`: offline, read-only configuration and environment check, through main()."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pr_watch.cli import main
from pr_watch.state_dir import open_state_dir
from pr_watch.store import open_store
from tests.forgectl_support import FakeForgectl
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "validate"


def write_config(text: str) -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "pr-watch" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["validate", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def jrun(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    code, out, err = run(capsys, "--json", *argv)
    assert code == 0, (out, err)
    doc = json.loads(out)
    assert_valid("cli/validate", doc)
    return doc  # type: ignore[no-any-return]


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    fake_forgectl.host("github.com", me="user-1")
    return fake_forgectl


def tree(root: Path) -> dict[str, str]:
    """Every path under `root` with a digest of its content (or a marker for a directory)."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        found[rel] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return found


# --- success -----------------------------------------------------------------------------------


def test_defaults_are_valid_with_no_warnings(capsys: pytest.CaptureFixture[str], fake) -> None:
    doc = jrun(capsys)
    assert doc["ok"] is True
    assert doc["warnings"] == []
    assert [c["name"] for c in doc["checks"]] == ["config", "forgectl", "state_dir"]
    assert all(c["ok"] for c in doc["checks"])
    assert doc["forgectl"]["version"] == "0.55.3"
    assert doc["probe"] is None


def test_golden_json(capsys: pytest.CaptureFixture[str], fake) -> None:
    write_config(
        "hosts:\n  github.com:\n    concurrency: 2\nhooks:\n  on-pushed:\n    - cmd: notify\n"
    )
    doc = jrun(capsys)
    doc["forgectl"]["path"] = "<path>"
    path = GOLDEN / "ok.json"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        pytest.fail("golden written; review it")
    assert doc == json.loads(path.read_text())


def test_human_output_and_quiet(capsys: pytest.CaptureFixture[str], fake) -> None:
    code, out, _ = run(capsys)
    assert code == 0
    assert "config: ok" in out and "forgectl: ok" in out and "state_dir: ok" in out
    code, out, _ = run(capsys, "--quiet")
    assert (code, out) == (0, "")


def test_hooks_and_hosts_are_counted_in_the_config_check(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    write_config(
        "hosts:\n  github.com:\n    concurrency: 2\n"
        "hooks:\n  on-pushed:\n    - cmd: a\n    - cmd: b\n  post-poll:\n    - cmd: c\n"
    )
    doc = jrun(capsys)
    config = next(c for c in doc["checks"] if c["name"] == "config")
    assert "1 host" in config["message"] and "3 hook" in config["message"]


# --- config failures and warnings -------------------------------------------------------------


def test_invalid_config_is_exit_2_naming_the_key(capsys: pytest.CaptureFixture[str], fake) -> None:
    write_config("poll:\n  concurrency: 0\n")
    code, out, _ = run(capsys, "--json")
    assert code == 2
    err = json.loads(out)["error"]
    assert err["code"] == "config_invalid"
    assert err["details"]["key"] == "poll.concurrency"


def test_invalid_hook_is_config_invalid(capsys: pytest.CaptureFixture[str], fake) -> None:
    write_config("hooks:\n  on-pushed:\n    - cmd: a\n      timeout: 0\n")
    code, out, _ = run(capsys, "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "config_invalid"


def test_credential_like_env_allow_is_a_warning_not_a_failure(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    write_config("hooks:\n  env_allow: [MY_API_TOKEN]\n")
    doc = jrun(capsys)
    assert doc["ok"] is True
    assert [w["key"] for w in doc["warnings"]] == ["hooks.env_allow"]
    assert doc["warnings"][0]["code"] == "config_warning"
    code, out, _ = run(capsys)
    assert code == 0 and "warning" in out


# --- forgectl ----------------------------------------------------------------------------------


def test_forgectl_missing_is_exit_12_tool_missing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, sandbox_bin: Path
) -> None:
    monkeypatch.setenv("PATH", str(sandbox_bin.parent / "empty-dir-that-does-not-exist"))
    code, out, _ = run(capsys, "--json")
    assert code == 12
    err = json.loads(out)["error"]
    assert err["code"] == "tool_missing" and "install" in err["details"]


def test_relative_path_entries_do_not_resolve_forgectl(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "tests/fakebin")
    code, out, _ = run(capsys, "--json")
    assert code == 12 and json.loads(out)["error"]["code"] == "tool_missing"


def test_forgectl_too_old_is_exit_12_tool_too_old(capsys: pytest.CaptureFixture[str], fake) -> None:
    fake.version("forgectl 0.54.9\n")
    code, out, _ = run(capsys, "--json")
    assert code == 12
    err = json.loads(out)["error"]
    assert err["code"] == "tool_too_old"
    assert err["details"] == {"required": "0.55", "found": "0.54.9"}


def test_a_newer_forgectl_minor_is_accepted(capsys: pytest.CaptureFixture[str], fake) -> None:
    fake.version("forgectl 0.61.2\n")
    assert jrun(capsys)["forgectl"]["version"] == "0.61.2"


def test_only_version_is_asked_of_forgectl_by_default(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    jrun(capsys)
    assert fake.argvs() == [["--version"]]


# --- state dir ---------------------------------------------------------------------------------


def test_missing_state_dir_is_fine_and_stays_missing(
    capsys: pytest.CaptureFixture[str], fake, state_dir: Path
) -> None:
    doc = jrun(capsys)
    assert doc["warnings"] == []
    assert not state_dir.exists()
    check = next(c for c in doc["checks"] if c["name"] == "state_dir")
    assert "not created" in check["message"]


def test_unsafe_state_dir_is_exit_12_state_unsafe(
    capsys: pytest.CaptureFixture[str], fake, state_dir: Path
) -> None:
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o777)
    code, out, _ = run(capsys, "--json")
    assert code == 12 and json.loads(out)["error"]["code"] == "state_unsafe"


def test_existing_safe_state_dir_is_ok(
    capsys: pytest.CaptureFixture[str], fake, state_dir: Path
) -> None:
    with open_state_dir(state_dir):
        pass
    doc = jrun(capsys)
    check = next(c for c in doc["checks"] if c["name"] == "state_dir")
    assert check["ok"] and "not created" not in check["message"]


# --- read-only proof ---------------------------------------------------------------------------


def test_nothing_is_created_or_changed(
    capsys: pytest.CaptureFixture[str], fake, state_dir: Path, isolated_env: Path
) -> None:
    write_config("hooks:\n  env_allow: [MY_API_TOKEN]\n")
    before = tree(isolated_env / ".config")
    jrun(capsys)
    assert not state_dir.exists()
    assert tree(isolated_env / ".config") == before
    assert not (isolated_env / ".local" / "state" / "pr-watch").exists()


def test_existing_state_is_byte_identical(
    capsys: pytest.CaptureFixture[str], fake, state_dir: Path
) -> None:
    with open_state_dir(state_dir) as state, open_store(state):
        pass
    sqlite3.connect(state_dir / "state.db").close()
    before = tree(state_dir)
    jrun(capsys)
    run(capsys)
    assert tree(state_dir) == before


# --- probe -------------------------------------------------------------------------------------


def test_probe_asks_whoami_and_reports_hosts(capsys: pytest.CaptureFixture[str], fake) -> None:
    doc = jrun(capsys, "--probe")
    assert doc["probe"]["hosts"] >= 1
    assert doc["warnings"] == []
    assert [c["name"] for c in doc["checks"]] == ["config", "forgectl", "state_dir", "probe"]
    assert [a[0] for a in fake.argvs()] == ["--version", "whoami"]


def test_probe_warns_about_a_configured_host_forgectl_does_not_know(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    write_config("hosts:\n  git.example.test:\n    concurrency: 2\n")
    doc = jrun(capsys, "--probe")
    assert doc["warnings"] == [
        {
            "code": "host_unknown",
            "key": "hosts.git.example.test",
            "message": "forgectl does not know this host",
        }
    ]


def test_probe_warns_only_for_the_unknown_host_of_a_mixed_config(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    write_config(
        "hosts:\n  github.com:\n    concurrency: 2\n  git.example.test:\n    concurrency: 2\n"
    )
    doc = jrun(capsys, "--probe")
    assert [w["key"] for w in doc["warnings"]] == ["hosts.git.example.test"]


def test_no_probe_means_no_host_warning(capsys: pytest.CaptureFixture[str], fake) -> None:
    write_config("hosts:\n  git.example.test:\n    concurrency: 2\n")
    assert jrun(capsys)["warnings"] == []


def test_probe_with_no_known_hosts_warns_for_every_configured_host(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    write_config("hosts:\n  git.example.test:\n    concurrency: 2\n")
    fake.raw("whoami", stdout='{"hosts": [], "errors": []}')
    doc = jrun(capsys, "--probe")
    assert doc["probe"] == {"hosts": 0}
    assert [w["code"] for w in doc["warnings"]] == ["host_unknown"]


def test_probe_errors_suppress_the_host_unknown_warning(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    write_config("hosts:\n  git.example.test:\n    concurrency: 2\n")
    fake.network_error("whoami", host="github.com")
    doc = jrun(capsys, "--probe")
    assert [w["code"] for w in doc["warnings"]] == ["probe_partial"]


def test_probe_failure_ends_with_the_forgectl_error(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    fake.garbage("whoami")
    code, out, _ = run(capsys, "--json", "--probe")
    assert code == 12
    assert json.loads(out)["error"]["code"] == "contract"
    assert_valid("cli/error", json.loads(out))


def test_probe_with_a_failing_host_is_a_warning(capsys: pytest.CaptureFixture[str], fake) -> None:
    fake.network_error("whoami", host="github.com")
    doc = jrun(capsys, "--probe")
    assert [w["code"] for w in doc["warnings"]] == ["probe_partial"]


def test_probe_refusal_is_exit_20(capsys: pytest.CaptureFixture[str], fake) -> None:
    envelope = {"error": {"code": "refused", "reason": "unknown_host", "message": "unknown host"}}
    fake.raw("whoami", stdout=json.dumps(envelope), exit=20)
    code, out, _ = run(capsys, "--json", "--probe")
    assert code == 20
    err = json.loads(out)["error"]
    assert err["code"] == "refused" and err["reason"] == "unknown_host"
    assert_valid("cli/error", json.loads(out))


def test_probe_with_no_usable_result_is_exit_12_host_failed(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    envelope = {"error": {"code": "network_error", "message": "could not reach the host"}}
    fake.raw("whoami", stdout=json.dumps(envelope), exit=12)
    code, out, _ = run(capsys, "--json", "--probe")
    assert code == 12
    err = json.loads(out)["error"]
    assert err["code"] == "host_failed" and "lower_error" in err
    assert_valid("cli/error", json.loads(out))


# --- forgectl --version edge cases -------------------------------------------------------------


def test_garbage_version_is_tool_too_old_found_unknown(
    capsys: pytest.CaptureFixture[str], fake
) -> None:
    fake.version("not a version\n")
    code, out, _ = run(capsys, "--json")
    assert code == 12
    err = json.loads(out)["error"]
    assert err["code"] == "tool_too_old" and err["details"]["found"] == "unknown"


def test_hung_version_ends_as_tool_missing_after_the_short_deadline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake
) -> None:
    from pr_watch import validate

    fake.hang("--version", seconds=30)
    monkeypatch.setattr(validate, "VERSION_DEADLINE", 0.5)
    code, out, _ = run(capsys, "--json")
    assert code == 12 and json.loads(out)["error"]["code"] == "tool_missing"


def test_unsafe_state_dir_wins_over_a_missing_forgectl(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    sandbox_bin: Path,
    state_dir: Path,
) -> None:
    state_dir.mkdir(parents=True)
    state_dir.chmod(0o777)
    monkeypatch.setenv("PATH", str(sandbox_bin.parent / "empty-dir-that-does-not-exist"))
    code, out, _ = run(capsys, "--json")
    assert code == 12 and json.loads(out)["error"]["code"] == "state_unsafe"
