"""Hooks wired into `poll` (#28), in process through main() against the fake forgectl: when they
run (after the commit, before the report), what they receive, the report's counts, `--hook`,
`--no-hooks`, `post-poll` and its triggers. The runner's own behavior is tests/test_hooks.py; the
signal and process-group behavior of a real poll is tests/test_hooks_signals.py."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from pr_watch import hooks as hooks_module
from pr_watch.cli import main
from pr_watch.forgectl_client import ForgectlClient, ToolMissing
from pr_watch.state_dir import ensure_hooks_cwd
from pr_watch.store import Store
from tests.forgectl_support import FakeForgectl, new_pr, sha
from tests.harness import assert_valid
from tests.test_poll import (
    HOST,
    LATER,
    OWNER,
    events,
    fake,  # noqa: F401  (fixture)
    log_records,
    pr,
    store,  # noqa: F401  (fixture)
    watch_query,
)

LATE_2 = "2026-10-03T12:00:00Z"

# Reads the stored event named in the payload (stdin) from the committed database and records
# what it found; a hook that ran before the commit would see no row.
CHECK = """\
import json, os, sqlite3, sys
p = json.load(sys.stdin)
path = os.environ["XDG_STATE_HOME"] + "/pr-watch/state.db?mode=ro"
db = sqlite3.connect("file:" + path, uri=True)
n = db.execute("select count(*) from events where id=?", (p["event_id"],)).fetchone()[0]
open(sys.argv[1], "a").write(str(n) + " " + os.getcwd() + chr(10))
"""


def run(
    capsys: pytest.CaptureFixture[str], *argv: str, config: Path | None = None
) -> tuple[int, dict[str, Any], str]:
    head = ["--config", str(config)] if config is not None else []
    code = main([*head, "--json", *argv])
    captured = capsys.readouterr()
    doc = json.loads(captured.out) if captured.out.strip() else {}
    if argv[0] == "poll" and "error" not in doc:
        assert_valid("cli/poll", doc)
    return code, doc, captured.err


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def pushed(fake: FakeForgectl, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:  # noqa: F811
    """One baselined PR whose next poll sees a push."""
    snap = pr(1)
    fake.add(snap)
    watch_query(capsys)
    run(capsys, "poll")
    fake.update(snap["pr"], updated_at=LATER, head={"sha": sha(7)})
    return snap


def test_an_on_event_hook_runs_after_the_commit_with_the_event_on_stdin_in_hooks_cwd(
    pushed: dict[str, Any],
    store: Store,  # noqa: F811
    state_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen, body = tmp_path / "seen", tmp_path / "body"
    script = tmp_path / "check.py"
    script.write_text(CHECK, encoding="utf-8")
    cfg = write_config(
        tmp_path,
        f"hooks:\n  on-pushed:\n    - cmd: 'tee {body} | python3 {script} {seen}'\n",
    )
    code, doc, _ = run(capsys, "poll", config=cfg)
    assert code == 0 and doc["hooks"] == {"run": 1, "skipped": 0, "failed": 0}
    count, cwd = seen.read_text().split(" ", 1)
    assert count == "1"  # the event was committed before its hook ran
    assert Path(cwd.strip()).resolve() == (state_dir / "hooks-cwd").resolve()
    payload = json.loads(body.read_text())
    assert_valid("hooks/on-pushed", payload)
    assert payload["event_id"] == payload["id"] and payload["pr_id"] == payload["object_id"]
    assert events(store) == ["pushed"]
    (rec,) = [r for r in log_records(state_dir) if r["event"] == "hook_run"]
    assert rec["hook"] == "on-pushed" and rec["exit_code"] == 0 and rec["event_id"] == payload["id"]


def test_failing_hooks_are_counted_logged_and_never_change_the_exit(
    pushed: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    cfg = write_config(tmp_path, "hooks:\n  on-pushed:\n    - cmd: 'exit 3'\n    - cmd: 'true'\n")
    code, doc, _ = run(capsys, "poll", config=cfg)
    assert code == 0 and doc["hooks"] == {"run": 2, "skipped": 0, "failed": 1}
    codes = [r["exit_code"] for r in log_records(state_dir) if r["event"] == "hook_run"]
    assert sorted(codes) == [0, 3]


def test_the_hook_flag_adds_a_hook_and_no_hooks_skips_all_of_them(
    pushed: dict[str, Any],
    store: Store,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "out"
    cfg = write_config(tmp_path, f"hooks:\n  on-pushed:\n    - cmd: 'echo cfg >> {out}'\n")
    code, doc, _ = run(
        capsys, "poll", "--no-hooks", "--hook", f"on-pushed=echo flag >> {out}", config=cfg
    )
    assert code == 0 and doc["hooks"] == {"run": 0, "skipped": 0, "failed": 0}
    assert not out.exists() and events(store) == ["pushed"]  # stored, never run
    # the event is not run later: hooks are at-most-once, a later poll does not revisit it
    fake_again = run(capsys, "poll", "--hook", f"on-pushed=echo flag >> {out}", config=cfg)[1]
    assert fake_again["hooks"]["run"] == 0 and not out.exists()


def test_a_hook_flag_runs_with_a_configured_one_in_config_then_flag_order(
    pushed: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    cfg = write_config(
        tmp_path,
        "hooks:\n  concurrency: 1\n  on-pushed:\n"
        f"    - {{cmd: 'echo cfg >> {out}', async: false}}\n",
    )
    flag = f"on-pushed=echo flag >> {out}"
    _, doc, _ = run(capsys, "poll", "--hook", flag, config=cfg)
    assert doc["hooks"]["run"] == 2
    assert sorted(out.read_text().split()) == ["cfg", "flag"]


def test_a_flood_hits_the_per_poll_cap_and_the_report_and_log_say_so(
    fake: FakeForgectl,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
) -> None:
    snaps = [pr(n) for n in range(1, 6)]
    for snap in snaps:
        fake.add(snap)
    watch_query(capsys)
    run(capsys, "poll")
    for snap in snaps:
        fake.update(snap["pr"], updated_at=LATER, head={"sha": sha(7)})
    cfg = write_config(tmp_path, "hooks:\n  max_per_poll: 2\n  on-pushed:\n    - cmd: 'true'\n")
    code, doc, _ = run(capsys, "poll", config=cfg)
    assert code == 0 and doc["events"]["pushed"] == 5
    assert doc["hooks"] == {"run": 2, "skipped": 3, "failed": 0}
    (cap,) = [r for r in log_records(state_dir) if r["event"] == "hook_cap"]
    assert cap["skipped"] == 3


def test_watch_add_runs_no_hooks(
    fake: FakeForgectl,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state_dir: Path,
) -> None:
    snap = new_pr(HOST, "example-org/widgets", 9, state="merged", merged_at="2026-01-01T00:00:00Z",
                  merge_commit_sha=sha(3), updated_at="2026-10-02T10:30:00Z")  # fmt: skip
    fake.add(snap)
    out = tmp_path / "out"
    cfg = write_config(tmp_path, f"hooks:\n  on-merged:\n    - cmd: 'echo x >> {out}'\n")
    code, doc, _ = run(capsys, "watch", "add", snap["pr"], "--owner", OWNER, config=cfg)
    assert code == 0 and doc["events_emitted"] == 1
    assert not out.exists()
    assert [r for r in log_records(state_dir) if r["event"] == "hook_run"] == []


# --- post-poll -----------------------------------------------------------------------------------


def post_poll_config(tmp_path: Path, body: Path) -> Path:
    return write_config(
        tmp_path, f"hooks:\n  post-poll:\n    - cmd: 'cat >> {body}; echo >> {body}'\n"
    )


def payloads(body: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in body.read_text().splitlines() if line.strip()]


def test_post_poll_does_not_fire_after_a_clean_poll(
    pushed: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = tmp_path / "pp"
    _, doc, _ = run(capsys, "poll", config=post_poll_config(tmp_path, body))
    assert doc["failed"] == [] and not body.exists() and doc["hooks"]["run"] == 0


def test_post_poll_fires_for_a_failed_pr_with_counts_that_exclude_itself(
    fake: FakeForgectl,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = pr(1), pr(2)
    fake.add(a)
    fake.add(b)
    watch_query(capsys)
    run(capsys, "poll")
    fake.update(a["pr"], updated_at=LATER)
    fake.not_found("snapshot", pr=a["pr"])
    fake.update(b["pr"], updated_at=LATER, head={"sha": sha(7)})
    body = tmp_path / "pp"
    cfg = write_config(
        tmp_path,
        "hooks:\n  on-pushed:\n    - cmd: 'true'\n  post-poll:\n"
        f"    - cmd: 'cat >> {body}; echo >> {body}'\n",
    )
    code, doc, _ = run(capsys, "poll", config=cfg)
    assert code == 0 and doc["failed"][0]["code"] == "not_found"
    (payload,) = payloads(body)
    assert_valid("hooks/post-poll", payload)
    assert payload["hook"] == "post-poll" and payload["object_type"] == "poll"
    assert payload["object_id"] == payload["started_at"] == doc["started_at"]
    assert payload["hooks"] == {"run": 1, "skipped": 0, "failed": 0}  # the on-pushed, not itself
    assert doc["hooks"] == {"run": 2, "skipped": 0, "failed": 0}  # the report counts both
    assert payload["events"]["pushed"] == 1 and payload["prs"]["unreachable"] == 0
    assert payload["errors"] == [] and payload["unreachable"] == []
    # no host text and no per-PR failure detail: only counts and codes
    assert a["pr"] not in json.dumps(payload)


def test_post_poll_fires_for_a_host_error_naming_the_host_and_codes(
    fake: FakeForgectl,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake.add(pr(1))
    watch_query(capsys)
    fake.auth_required("prs", host=HOST)
    body = tmp_path / "pp"
    code, doc, _ = run(capsys, "poll", config=post_poll_config(tmp_path, body))
    assert code == 12
    (payload,) = payloads(body)
    assert_valid("hooks/post-poll", payload)
    assert payload["errors"] == [{"host": HOST, "codes": ["auth_required"]}]


def test_post_poll_for_a_newly_unreachable_pr_lists_it_but_a_repeat_failure_alone_does_not(
    fake: FakeForgectl,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_WATCH_FAILURES__UNREACHABLE_AFTER", "1")
    a = pr(1)
    fake.add(a)
    watch_query(capsys)
    run(capsys, "poll")
    fake.update(a["pr"], updated_at=LATER)
    fake.not_found("snapshot", pr=a["pr"])
    body = tmp_path / "pp"
    cfg = post_poll_config(tmp_path, body)
    _, doc, _ = run(capsys, "poll", config=cfg)
    assert doc["prs"]["unreachable"] == 1
    (payload,) = payloads(body)
    assert_valid("hooks/post-poll", payload)
    assert payload["unreachable"] == [{"pr": a["pr"], "pr_id": a["pr_id"], "code": "not_found"}]
    # it fails again on the next full pass: in `failed`, but never alone a cause for post-poll
    fake.update(a["pr"], updated_at=LATE_2)
    _, again, _ = run(capsys, "poll", "--full", config=cfg)
    assert again["failed"] and again["prs"]["unreachable"] == 0
    assert len(payloads(body)) == 1


def test_post_poll_is_not_counted_by_the_caps_and_no_hooks_skips_it(
    fake: FakeForgectl,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake.add(pr(1))
    watch_query(capsys)
    fake.auth_required("prs", host=HOST)
    body = tmp_path / "pp"
    cfg = write_config(
        tmp_path,
        "hooks:\n  max_per_pr_per_poll: 1\n  post-poll:\n"
        f"    - cmd: 'echo a >> {body}'\n    - cmd: 'echo b >> {body}'\n",
    )
    _, doc, _ = run(capsys, "poll", config=cfg)
    assert doc["hooks"] == {"run": 2, "skipped": 0, "failed": 0}
    body.unlink()
    _, doc, _ = run(capsys, "poll", "--no-hooks", config=cfg)
    assert not body.exists() and doc["hooks"]["run"] == 0


def test_a_hook_is_not_given_the_poll_environment_secrets(
    pushed: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "sekrit")
    monkeypatch.setenv("SOME_VAR", "shown")
    out = tmp_path / "env"
    cfg = write_config(
        tmp_path, f"hooks:\n  env_allow: [SOME_VAR]\n  on-pushed:\n    - cmd: 'env > {out}'\n"
    )
    code, doc, _ = run(capsys, "poll", config=cfg)
    assert doc["hooks"]["run"] == 1
    text = out.read_text()
    assert "sekrit" not in text and "GH_TOKEN" not in text and "SOME_VAR=shown" in text


def test_a_tool_error_still_runs_hooks_of_committed_events_but_no_post_poll_and_no_counts(
    fake: FakeForgectl,  # noqa: F811
    state_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = pr(1), pr(2)
    fake.add(a)
    fake.add(b)
    watch_query(capsys)
    run(capsys, "poll")
    fake.update(a["pr"], updated_at=LATER, head={"sha": sha(7)})
    fake.update(b["pr"], updated_at=LATER, head={"sha": sha(8)})
    marks, post = tmp_path / "marks", tmp_path / "post"
    cfg = write_config(
        tmp_path,
        f"hooks:\n  on-pushed:\n    - cmd: 'echo x >> {marks}'\n"
        f"  post-poll:\n    - cmd: 'echo x >> {post}'\n",
    )
    real = ForgectlClient.snapshot

    def snapshot(self: ForgectlClient, ref: Any) -> Any:
        if ref.canonical == b["pr"]:  # the tool fails only after a's commit and its hook ran
            deadline = time.monotonic() + 30
            while not marks.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            raise ToolMissing("forgectl vanished")
        return real(self, ref)

    monkeypatch.setattr(ForgectlClient, "snapshot", snapshot)
    code, doc, _ = run(capsys, "poll", config=cfg)
    assert code == 12 and doc["error"]["code"] == "tool_missing" and "hooks" not in doc
    assert marks.read_text() == "x\n"  # the committed event's hook ran (the envelope waited)
    assert not post.exists()  # no post-poll on a tool error
    runs = [r for r in log_records(state_dir) if r["event"] == "hook_run"]
    assert len(runs) == 1 and runs[0]["exit_code"] == 0


def test_an_unusable_hooks_cwd_fails_the_hook_is_logged_and_the_poll_exits_zero(
    pushed: dict[str, Any],
    state_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir(mode=0o700)

    def swapped(state: Any) -> Path:  # the directory was replaced after the start check
        path = state.path_for("hooks-cwd")
        path.rmdir()
        path.symlink_to(real_dir)
        return ensure_hooks_cwd(state)

    monkeypatch.setattr(hooks_module, "ensure_hooks_cwd", swapped)
    ran = tmp_path / "ran"
    cfg = write_config(tmp_path, f"hooks:\n  on-pushed:\n    - cmd: 'echo x > {ran}'\n")
    code, doc, _ = run(capsys, "poll", config=cfg)
    assert code == 0 and doc["hooks"] == {"run": 1, "skipped": 0, "failed": 1}
    assert not ran.exists() and list(real_dir.iterdir()) == []
    (rec,) = [r for r in log_records(state_dir) if r["event"] == "hook_run"]
    assert rec["exit_code"] is None and "hooks-cwd unavailable" in rec["message"]
