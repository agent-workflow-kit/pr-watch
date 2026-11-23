"""Test support for forgectl: pinned schemas, synthetic data, a driver for tests/fakebin/forgectl.

The fake itself (tests/fakebin/_forgectl_fake.py) reads a scenario directory and appends to a
call log there; `FakeForgectl` is the test side of that contract: it builds scenarios, scripts
failures, reads the calls and, at teardown, validates every output the fake printed against
forgectl's own schemas pinned in tests/forgectl_pinned (so the fake cannot drift from the real
shapes without a failing test) and kills any leftover process the fake started, by pid plus
start time only.

Use the `fake_forgectl` fixture; the client under test must find `forgectl` through PATH
(tests/fakebin is first on it) and keep that PATH, which also holds the sandbox's `python3` the
fake's shebang needs, and `HOME` (or `FAKE_FORGECTL_DIR`) in the child's environment, since that
is how the fake finds its scenario.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

FAKE_PATH = Path(__file__).resolve().parent / "fakebin" / "_forgectl_fake.py"
PIN_DIR = Path(__file__).resolve().parent / "forgectl_pinned"
SCHEMAS_DIR = PIN_DIR / "schemas"
GOLDEN_DIR = PIN_DIR / "golden"
FAKE_EXIT = 98
DIR_ENV = "FAKE_FORGECTL_DIR"
VALIDATED_KINDS = ("ok", "partial", "error")


def pin() -> dict[str, Any]:
    return json.loads((PIN_DIR / "PIN.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def fake_module() -> ModuleType:
    """The fake's own module, loaded by path (never imported by name), for its constants."""
    spec = importlib.util.spec_from_file_location("_forgectl_fake_under_test", FAKE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def golden(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


# --- pinned schemas ------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    resources = []
    for path in sorted(SCHEMAS_DIR.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def forgectl_errors(schema: str, instance: Any) -> list[str]:
    """Violations of forgectl's pinned schema `schema` (`snapshot`, `prs`, `error`, ...)."""
    doc = json.loads((SCHEMAS_DIR / "cli" / f"{schema}.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(doc, registry=_registry())
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(instance)
    ]


def assert_forgectl_valid(schema: str, instance: Any) -> None:
    errors = forgectl_errors(schema, instance)
    if errors:
        raise AssertionError(
            f"forgectl {schema} schema rejected the document:\n" + "\n".join(errors)
        )


# --- synthetic data ------------------------------------------------------------------------


def sha(n: int) -> str:
    """A deterministic 40-hex sha: `sha(1)` is forty `1`s, `sha(10)` forty `a`s."""
    return format(n % 16, "x") * 40


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def new_pr(
    host: str = "github.com",
    repo: str = "example-org/widgets",
    number: int = 7,
    *,
    backend: str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """A synthetic snapshot of PR `host/repo#number`, from a pinned recorded one.

    `changes` is merged into the snapshot (nested dicts merge, anything else replaces), so
    `new_pr(state="merged", head={"sha": sha(2)})` works. Identity fields are rewritten
    consistently; the data is the pinned golden's synthetic data and nothing private.
    """
    backend = backend or ("gitlab" if "gitlab" in host else "github")
    base = golden("snapshot-gitlab-open" if backend == "gitlab" else "snapshot-github-open")
    snap = copy.deepcopy(base)
    old_pr, old_repo, old_host = snap["pr"], snap["repo"], snap["rate_limit"]["host"]
    path = f"{host}/{repo}"
    snap["pr"] = f"{path}#{number}"
    snap["repo"] = path
    snap["number"] = number
    snap["pr_id"] = f"PR_example_{host.replace('.', '_')}_{repo.replace('/', '_')}_{number}"
    snap["repo_id"] = f"R_example_{host.replace('.', '_')}_{repo.replace('/', '_')}"
    kind = "merge_requests" if backend == "gitlab" else "pull"
    sep = "/-/" if backend == "gitlab" else "/"
    snap["url"] = f"https://{path}{sep}{kind}/{number}"
    snap["head"]["repo"] = path
    snap["head_fetch_ref"] = (
        f"refs/merge-requests/{number}/head" if backend == "gitlab" else f"refs/pull/{number}/head"
    )
    snap["rate_limit"]["host"] = host
    assert old_pr and old_repo and old_host
    _merge(snap, changes)
    return snap


@dataclass(frozen=True)
class Call:
    """One recorded invocation of the fake."""

    id: str
    argv: list[str]
    pid: int
    cwd: str
    env_names: list[str]
    exit: int | None  # None: the process never finished (killed)
    kind: str | None
    stdout: str | None
    descendants: list[int] = field(default_factory=list)

    @property
    def command(self) -> str | None:
        skip = False
        for token in self.argv:
            if skip:
                skip = False
            elif token in ("--config", "--timeout"):
                skip = True
            elif not token.startswith("-"):
                return token
        return None


def _ticks(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return text.rsplit(")", 1)[1].split()[19]


class FakeForgectl:
    """Builds the scenario of tests/fakebin/forgectl and reads back what it was asked."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.scenario: dict[str, Any] = {"hosts": {}, "prs": {}, "compares": [], "rules": []}
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "calls.jsonl").touch()
        self._write()

    # -- scenario
    def _write(self) -> None:
        tmp = self.dir / "scenario.json.tmp"
        tmp.write_text(json.dumps(self.scenario), encoding="utf-8")
        os.replace(tmp, self.dir / "scenario.json")

    def host(
        self,
        host: str = "github.com",
        *,
        backend: str | None = None,
        me: str | None = "user-1",
        remaining: int | None = 4000,
        reset_at: str | None = "2026-10-02T12:00:00Z",
        whoami: dict[str, Any] | None = None,
        api_prefix: str | None = None,
    ) -> FakeForgectl:
        """Declare `host` (in scope for `prs` with no `--host`); built-ins need no declaring
        to be known, but only a declared host is queried by a bare `--author me`."""
        backend = backend or ("gitlab" if "gitlab" in host else "github")
        entry: dict[str, Any] = {"backend": backend, "me": me}
        if remaining is not None:
            entry["rate_limit"] = {"remaining": remaining, "reset_at": reset_at}
        entry["whoami"] = whoami or {
            "login": me, "id": "1001", "me": me, "me_matches_login": True,
            "token_source": "token_env", "scopes": None,
        }  # fmt: skip
        if api_prefix:
            entry["api_prefix"] = api_prefix
        self.scenario["hosts"][host] = entry
        self._write()
        return self

    def add(self, snapshot: dict[str, Any], *, listing: dict[str, Any] | None = None) -> str:
        """Add or replace a PR; `listing` overrides fields of its `prs` item (a stale index)."""
        entry: dict[str, Any] = {"snapshot": copy.deepcopy(snapshot)}
        if listing:
            entry["listing"] = listing
        self.scenario["prs"][snapshot["pr"]] = entry
        self._write()
        return str(snapshot["pr"])

    def update(self, pr: str, **changes: Any) -> None:
        """Change a stored snapshot (merge semantics of `new_pr`); the listing follows it."""
        _merge(self.scenario["prs"][pr]["snapshot"], changes)
        self._write()

    def rename(self, pr: str, repo: str) -> str:
        """Rename the repo of a stored PR to `repo` (`host/org/name`): its `pr`, `repo`, `url` and
        `head.repo` follow, `pr_id` and `repo_id` stay, listings show the new name, and the old
        `pr` keeps resolving for `snapshot`, `show` and `compare` (a host redirect). Returns the
        new `pr`."""
        entry = self.scenario["prs"].pop(pr)
        snap = entry["snapshot"]
        old_repo = snap["repo"]
        new = f"{repo}#{snap['number']}"
        snap["pr"], snap["repo"], snap["head"]["repo"] = new, repo, repo
        snap["url"] = snap["url"].replace(old_repo, repo)
        self.scenario["prs"][new] = entry
        self.scenario.setdefault("redirects", {})[pr.lower()] = new
        self._write()
        return new

    def remove(self, pr: str) -> None:
        del self.scenario["prs"][pr]
        self._write()

    def hide_repo(self, repo: str) -> None:
        """Hide the repos matching `repo`: a repo id or a glob (`*`, `?`) as `hidden_repos` is."""
        self.scenario.setdefault("hidden_repos", []).append(repo)
        self._write()

    def term_delay(self, seconds: float) -> None:
        """Make the fake take `seconds` between a SIGINT, SIGTERM or SIGHUP and its exit."""
        self.scenario["term_delay"] = seconds
        self._write()

    def set_now(self, iso: str) -> None:
        self.scenario["now"] = iso
        self._write()

    def compare(self, pr: str, from_sha: str, to_sha: str, status: str, diff: str | None = None):
        """Script `forgectl compare`; an unscripted compare of a known PR exits 98."""
        self.scenario["compares"].append(
            {"pr": pr, "from": from_sha, "to": to_sha, "status": status, "diff": diff}
        )
        self._write()

    # -- scripted failures (rules). Each takes match keywords: pr, host, repo, argv_has.
    def script(
        self,
        command: str,
        action: dict[str, Any],
        *,
        times: int | None = None,
        after: int = 0,
        **match: Any,
    ) -> None:
        """Fire `action` for calls of `command` that match, `after` matching calls later, up
        to `times` times (None: always). The first rule that matches and still fires wins."""
        self.scenario["rules"].append(
            {
                "match": {"command": command, **match},
                "action": action,
                "times": times,
                "after": after,
            }
        )
        self._write()

    def fail(
        self,
        command: str,
        code: str,
        message: str = "scripted failure",
        *,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
        delay: float = 0,
        times: int | None = None,
        after: int = 0,
        **match: Any,
    ) -> None:
        """Make matching calls fail with forgectl's error `code` (exit and envelope as real).

        A code or refusal reason that `command` cannot end with in real forgectl (say
        `refused(stale_head)` on `show`) is a ValueError here."""
        module = fake_module()
        try:
            module.check_error_allowed(command, code, reason)
        except module.FakeBug as bug:
            raise ValueError(str(bug)) from None
        action: dict[str, Any] = {"kind": "error", "code": code, "message": message}
        if reason is not None:
            action["reason"] = reason
        if details is not None:
            action["details"] = details
        if retry_after is not None:
            action["retry_after"] = retry_after
        if delay:
            action["delay"] = delay
        self.script(command, action, times=times, after=after, **match)

    def rate_limited(self, command: str, retry_after: float | None = None, **kw: Any) -> None:
        self.fail(command, "rate_limited", "rate limited", retry_after=retry_after, **kw)

    def network_error(self, command: str, **kw: Any) -> None:
        self.fail(command, "network_error", "could not reach the host", **kw)

    def not_found(self, command: str, **kw: Any) -> None:
        self.fail(command, "not_found", "pull request not found", **kw)

    def permission_denied(self, command: str, **kw: Any) -> None:
        self.fail(command, "permission_denied", "the token may not read this", **kw)

    def auth_required(self, command: str, **kw: Any) -> None:
        self.fail(command, "auth_required", "no usable credential", **kw)

    def hang(
        self,
        command: str,
        *,
        seconds: float = 60,
        ignore_term: bool = False,
        descendant: bool = False,
        respect_timeout: bool = False,
        **kw: Any,
    ) -> None:
        """Hang (bounded to 120 s). With `ignore_term` SIGTERM is ignored; with `descendant` a
        child holds the pipes (ignores SIGTERM, lives at most 30 s); with `respect_timeout` the
        fake gives up at its `--timeout` with exit 12 `network_error`, as the real one does."""
        action = {
            "kind": "hang", "seconds": seconds, "ignore_term": ignore_term,
            "descendant": descendant, "respect_timeout": respect_timeout,
        }  # fmt: skip
        self.script(command, action, **kw)

    def garbage(self, command: str, stdout: str = "this is not json{", exit: int = 0, **kw: Any):
        self.script(command, {"kind": "raw", "stdout": stdout, "exit": exit}, **kw)

    def truncated(self, command: str, **kw: Any) -> None:
        """Valid output cut in half, exit 0."""
        self.script(command, {"kind": "truncate"}, **kw)

    def contract_violation(self, command: str, drop: list[str], **kw: Any) -> None:
        """Valid JSON that lacks the named top-level fields, exit 0."""
        self.script(command, {"kind": "drop", "fields": drop}, **kw)

    def oversize(self, command: str, extra_bytes: int, **kw: Any) -> None:
        """A valid result whose `body` (or `diff`) is `extra_bytes` longer."""
        self.script(command, {"kind": "pad", "bytes": extra_bytes}, **kw)

    def slow(self, command: str, seconds: float, **kw: Any) -> None:
        """Answer normally after `seconds` (bounded to 120 s)."""
        self.script(command, {"kind": "slow", "seconds": seconds}, **kw)

    def orphan(self, command: str, seconds: float = 30, **kw: Any) -> None:
        """Answer normally and exit while a descendant keeps the pipes open."""
        self.script(command, {"kind": "orphan", "seconds": seconds}, **kw)

    def die_by_signal(self, command: str, signum: int = signal.SIGKILL, **kw: Any) -> None:
        self.script(command, {"kind": "signal", "signum": int(signum)}, **kw)

    def raw(
        self, command: str, *, stdout: str = "", stderr: str = "", exit: int = 0, **kw: Any
    ) -> None:
        self.script(
            command, {"kind": "raw", "stdout": stdout, "stderr": stderr, "exit": exit}, **kw
        )

    def version(self, text: str, exit: int = 0, **kw: Any) -> None:
        """What `forgectl --version` prints (stdout) and exits with."""
        self.raw("--version", stdout=text, exit=exit, **kw)

    # -- running and reading
    def run(
        self, *argv: str, timeout: float = 30, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run `forgectl` as a client would: found through PATH, with the caller's environment."""
        environ = dict(os.environ if env is None else env)
        environ.setdefault(DIR_ENV, str(self.dir))
        return subprocess.run(
            ["forgectl", *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environ,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    def _records(self) -> list[dict[str, Any]]:
        """The call log's records; a line the fake is still writing (no newline yet) is not one."""
        text = (self.dir / "calls.jsonl").read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text = text[: text.rfind("\n") + 1]
        return [json.loads(line) for line in text.splitlines()]

    def calls(self, command: str | None = None) -> list[Call]:
        starts: dict[str, dict[str, Any]] = {}
        ends: dict[str, dict[str, Any]] = {}
        kids: dict[str, list[int]] = {}
        order: list[str] = []
        for record in self._records():
            if record["phase"] == "start":
                starts[record["id"]] = record
                order.append(record["id"])
            elif record["phase"] == "end":
                ends[record["id"]] = record
            elif record["phase"] == "descendant":
                kids.setdefault(record["id"], []).append(record["pid"])
        result = []
        for call_id in order:
            s, e = starts[call_id], ends.get(call_id, {})
            call = Call(
                id=call_id, argv=s["argv"], pid=s["pid"], cwd=s["cwd"], env_names=s["env_names"],
                exit=e.get("exit"), kind=e.get("kind"), stdout=e.get("stdout"),
                descendants=kids.get(call_id, []),
            )  # fmt: skip
            if command is None or call.command == command:
                result.append(call)
        return result

    def wait_ready(self, timeout: float = 30.0) -> None:
        """Block until a call's `ignore_term` hang has installed its SIGTERM-ignore handler."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(record["phase"] == "ready" for record in self._records()):
                return
            time.sleep(0.02)
        raise AssertionError("the fake never reported that it ignores SIGTERM")

    def argvs(self, command: str | None = None) -> list[list[str]]:
        return [c.argv for c in self.calls(command)]

    # -- teardown
    def validate_outputs(self) -> None:
        """Every output the fake printed as a normal result or error fits forgectl's schemas."""
        for call in self.calls():
            if call.kind not in VALIDATED_KINDS or call.stdout is None:
                continue
            if "--json" not in call.argv or not call.stdout:
                continue
            document = json.loads(call.stdout)
            schema = "error" if call.kind == "error" else call.command
            assert schema is not None
            assert_forgectl_valid(schema, document)

    def reap(self) -> None:
        """Kill what the fake left running: exact pids whose start time still matches."""
        starts: dict[int, str | None] = {}
        for line in (self.dir / "calls.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["phase"] in ("start", "descendant"):
                starts[record["pid"]] = record.get("start_ticks")
        for pid, ticks in starts.items():
            if ticks is not None and _ticks(pid) == ticks:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.fixture
def fake_forgectl(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeForgectl]:
    """A scenario for tests/fakebin/forgectl, at `$HOME/.fake-forgectl` (HOME is per test) and
    named by `FAKE_FORGECTL_DIR`, so a client that keeps either variable reaches it."""
    fake = FakeForgectl(Path(os.environ["HOME"]) / ".fake-forgectl")
    monkeypatch.setenv(DIR_ENV, str(fake.dir))
    try:
        yield fake
        fake.validate_outputs()
    finally:
        fake.reap()
