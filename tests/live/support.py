"""Drivers of the `live` suite: a scratch workspace, runners for the real forgectl and pr-watch,
call-counting wrappers for gh and glab, and host actions on the throwaway repositories.

Rules every helper keeps:

- Tokens travel in the environment of the one child that needs them (never argv, never a file);
  every message that can reach a failure report goes through `Workspace.redact`.
- HOME, XDG_* and TMPDIR of every child are the test's scratch directory. PATH holds the scratch
  bin directory (the real forgectl by symlink, counting gh and glab wrappers) and the guard's
  sandbox bin (whose crontab stub exits 99). The real crontab is unreachable.
- Every call has a timeout; a child that overruns gets terminate, then kill, by its own handle.
- Host actions only touch the throwaway repositories named by the environment.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tests.harness import assert_valid
from tests.live_env import HostCfg, LiveEnv

PASS_THROUGH = (
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GLAB_CA_CERT",
)  # fmt: skip
TOKEN_VARS = {"github": "LIVE_GITHUB_TOKEN", "gitlab": "LIVE_GITLAB_TOKEN"}
OWNER = "live"
GIT_USERS = ("x-access-token", "oauth2")
FAIL_FAST = (401, 403, 404)  # a retry cannot fix these
GONE = (404, 422)  # already deleted, closed or merged: cleanup treats them as done
KILL_GRACE = 5.0
FORGECTL_MIN = (0, 55)
# forgectl's own RFC 3339 `ts` (defs.json): optional fraction, `Z` or a numeric offset.
TS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_STATUS = (
    re.compile(r"HTTP (\d{3})"),
    re.compile(
        r"\b(\d{3}) (?:Bad Request|Unauthorized|Forbidden|Not Found|Method Not Allowed|Conflict"
        r"|Gone|Unprocessable Entity)\b"
    ),
)


class HostError(RuntimeError):
    """A host action failed (the message is already redacted). `status` is the HTTP status the
    tool reported, when it said one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def http_status(text: str) -> int | None:
    """The HTTP status a gh or glab failure text names, else None."""
    for pattern in _STATUS:
        found = pattern.search(text)
        if found:
            return int(found.group(1))
    return None


def fail_fast(exc: BaseException) -> bool:
    return isinstance(exc, HostError) and exc.status in FAIL_FAST


def github_token_variable(host: str) -> str:
    """The variable gh reads the token from, chosen like forgectl's gateway: `GH_TOKEN` only for
    github.com and *.ghe.com, `GH_ENTERPRISE_TOKEN` for every other GitHub host."""
    name = host.partition(":")[0]
    return (
        "GH_TOKEN" if name == "github.com" or name.endswith(".ghe.com") else "GH_ENTERPRISE_TOKEN"
    )


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.upper()).astimezone(UTC)


def forgectl_version(text: str) -> tuple[int, int, int] | None:
    found = re.fullmatch(r"forgectl ([0-9]+)\.([0-9]+)\.([0-9]+)\n?", text)
    return None if found is None else (int(found[1]), int(found[2]), int(found[3]))


def forgectl_new_enough(text: str) -> bool:
    version = forgectl_version(text)
    return version is not None and version[:2] >= FORGECTL_MIN


def secret_forms(secret: str) -> list[str]:
    """Every form a token can take in an output: raw, URL-encoded, and base64 (also as the basic
    auth pair `user:token` git and curl send), longest first."""
    forms = {
        secret,
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote(secret, safe="/"),
        urllib.parse.quote_plus(secret),
    }
    for raw in (secret, f":{secret}", *(f"{user}:{secret}" for user in GIT_USERS)):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(raw.encode()).decode()
            forms.update((encoded, encoded.rstrip("=")))
    return sorted((f for f in forms if f), key=len, reverse=True)


def graphql_errors(data: Any) -> list[str]:
    """Every error a GraphQL answer carries: top-level `errors` and any `errors` list nested in
    the payload (a mutation reports its refusals there), or one entry for an unusable answer."""
    if not isinstance(data, dict):
        return ["the answer is not an object"]
    found: list[str] = []
    if data.get("errors"):
        found.append(str(data["errors"])[:200])
    payload = data.get("data")
    if payload is None:
        found.append("the answer has no data")
    else:
        for path, value in walk(payload):
            if path and path[-1] == "errors" and value:
                found.append(str(value)[:200])
    return found


def end_group(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """End a child started with `start_new_session` by killing its own process group (the pid
    was captured at launch) and collect its output within a bound; never blocks for good."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        return proc.communicate(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        try:
            proc.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        return b"", b""


def install_sigterm_handler() -> Callable[[], None]:
    """Turn SIGTERM into a KeyboardInterrupt so pytest ends the session normally (teardown and
    cleanup run). One-shot: later SIGTERMs are ignored so teardown always finishes. Returns the
    function that restores the previous handler."""

    def handler(signum: int, frame: Any) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt("SIGTERM")

    previous = signal.signal(signal.SIGTERM, handler)

    def restore() -> None:
        signal.signal(signal.SIGTERM, previous)

    return restore


@dataclass
class Result:
    argv0: str
    rc: int
    out: str
    err: str
    elapsed: float
    pid: int
    leaked: bool = False  # a secret was in the UNREDACTED output (out and err are redacted)
    latency: float | None = None  # seconds from the signal sent by `run(signal_after=...)`

    def json(self) -> Any:
        try:
            return json.loads(self.out)
        except ValueError as exc:
            raise AssertionError(
                f"{self.argv0} rc={self.rc}: stdout is not JSON: {self.out[:300]!r} "
                f"stderr: {self.err[:300]!r}"
            ) from exc


def assert_no_leak(*results: Result) -> None:
    """The failure message names the programs, never a secret."""
    names = [r.argv0 for r in results if r.leaked]
    assert not names, f"a secret was present in the unredacted output of: {', '.join(names)}"


def wait_until(
    check: Callable[[], Any],
    *,
    timeout: float,
    interval: float = 2.0,
    what: str,
    give_up: Callable[[BaseException], bool] | None = None,
) -> tuple[Any, float]:
    """Call `check` until it returns something truthy; returns (value, seconds waited).

    Bounded: after `timeout` seconds it raises. An AssertionError or HostError from `check` is
    retried until then (unless `give_up` says it is final) and the last one is re-raised at the
    deadline, with the wait described in a note; otherwise the failure is an AssertionError."""
    start = time.monotonic()
    last: Exception | None = None
    while True:
        try:
            value = check()
        except (AssertionError, HostError) as exc:
            if give_up is not None and give_up(exc):
                raise
            last = exc
        else:
            if value:
                return value, time.monotonic() - start
        waited = time.monotonic() - start
        if waited >= timeout:
            note = f"timed out after {timeout:.0f}s waiting for: {what}"
            if last is not None:
                last.add_note(note)
                raise last
            raise AssertionError(note)
        time.sleep(min(interval, max(0.0, timeout - waited)))


def walk(value: Any, path: tuple[str, ...] = ()) -> Any:
    """Yield (path, value) for every node of a JSON document."""
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, (*path, str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, (*path, str(i)))


def nested_bodies(snapshot: Mapping[str, Any]) -> list[str]:
    """Paths of every `body` key below the top level (reviews, comments); the PR's own `body`
    is the one top-level key and is not listed."""
    return [".".join(path) for path, _ in walk(snapshot) if len(path) > 1 and path[-1] == "body"]


class Workspace:
    """Everything one test needs, under one scratch root. `close()` is a no-op for files (pytest
    removes tmp_path) but forgets the secrets."""

    def __init__(self, root: Path, sandbox_bin: Path, env: LiveEnv, run_id: str) -> None:
        self.root = root
        self.env = env
        self.run_id = run_id
        self.sandbox_bin = sandbox_bin
        self.home = root / "home"
        self.config_home = self.home / ".config"
        self.state_home = self.home / ".local" / "state"
        self.tmp = root / "tmp"
        self.bin = root / "bin"
        self.pids: list[int] = []
        self.calls_log = root / "calls.log"
        for d in (self.home, self.config_home, self.state_home, self.tmp, self.bin):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            d.chmod(0o700)
        for d in (self.home / ".local" / "share", self.home / ".cache"):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.calls_log.write_text("", encoding="utf-8")
        self.secrets = [c.token for c in env.hosts.values()] + [
            c.token2 for c in env.hosts.values() if c.token2
        ]
        self.forms = sorted(
            {form for secret in self.secrets for form in secret_forms(secret)},
            key=len,
            reverse=True,
        )
        self.leaked_runs: list[str] = []
        self._scrub: dict[str, str] = {}
        (self.bin / "forgectl").symlink_to(env.forgectl)
        for name, real in (("gh", env.gh), ("glab", env.glab)):
            if real is not None:
                self._counting_wrapper(self.bin / name, name, real)
        self.askpass = self.bin / "askpass"
        self.askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in Username*) printf "%s\\n" "$LIVE_GIT_USER";;'
            ' *) printf "%s\\n" "$LIVE_GIT_PASS";; esac\n',
            encoding="utf-8",
        )
        self.askpass.chmod(0o700)
        self.write_forgectl_config()

    # --- plumbing ----------------------------

    def _counting_wrapper(self, path: Path, name: str, real: Path) -> None:
        """A wrapper that logs one line per call (the arguments only: bodies and tokens travel
        by stdin and environment) and then execs the real binary."""
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s %s\\n' {shlex.quote(name)} \"$*\" >> {shlex.quote(str(self.calls_log))}\n"
            f'exec {shlex.quote(str(real))} "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o700)

    def redact(self, text: str) -> str:
        for form in self.forms:
            text = text.replace(form, "<redacted>")
        return text

    def leaks(self, text: str) -> bool:
        """True when `text` (UNREDACTED) holds a secret in any form."""
        return any(form in text for form in self.forms)

    def add_scrub(self, term: str, label: str) -> None:
        """Names (host, repository path, account login) that `scrub` hides from recorded output."""
        if len(term) >= 3:
            self._scrub[term] = label

    def scrub(self, text: str) -> str:
        """`redact`, then host, repository and login names replaced by placeholders."""
        text = self.redact(text)
        for term in sorted(self._scrub, key=len, reverse=True):
            pattern = r"(?<![A-Za-z0-9_-])" + re.escape(term) + r"(?![A-Za-z0-9_-])"
            text = re.sub(pattern, self._scrub[term], text, flags=re.IGNORECASE)
        return text

    def close(self) -> None:
        self.secrets = []
        self.forms = []

    def base_env(self) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{self.sandbox_bin}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
        for name in PASS_THROUGH:
            if name in os.environ:
                env[name] = os.environ[name]
        return env

    def tool_env(self, tokens: bool = True) -> dict[str, str]:
        """The environment of a forgectl or pr-watch child, with the primary tokens when asked."""
        env = self.base_env()
        if tokens:
            for kind, cfg in self.env.hosts.items():
                env[TOKEN_VARS[kind]] = cfg.token
        return env

    def run(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        stdin: str | None = None,
        timeout: float = 120.0,
        cwd: Path | None = None,
        signal_after: float | None = None,
        sig: signal.Signals = signal.SIGTERM,
    ) -> Result:
        """Run a child to its end within `timeout` seconds. A child that overruns, or a run that
        is interrupted, has its own process group killed (the pid was captured at launch) and the
        wait for its output is bounded. With `signal_after`, `sig` is sent to the child that long
        after the start and `Result.latency` is the time from that signal to the end."""
        start = time.monotonic()
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=str(cwd or self.root),
            start_new_session=True,
        )
        self.pids.append(proc.pid)
        data = stdin.encode() if stdin is not None else None
        sent: float | None = None
        try:
            if signal_after is None:
                out, err = proc.communicate(data, timeout)
            else:
                try:
                    out, err = proc.communicate(data, signal_after)
                except subprocess.TimeoutExpired:
                    sent = time.monotonic()
                    proc.send_signal(sig)
                    out, err = proc.communicate(None, timeout)
        except subprocess.TimeoutExpired:
            end_group(proc)
            raise AssertionError(
                f"{Path(argv[0]).name} (pid {proc.pid}) overran its {timeout:.0f}s bound"
            ) from None
        except BaseException:
            end_group(proc)
            raise
        raw_out = out.decode("utf-8", "replace")
        raw_err = err.decode("utf-8", "replace")
        leaked = self.leaks(raw_out) or self.leaks(raw_err)
        if leaked:
            self.leaked_runs.append(Path(argv[0]).name)
        return Result(
            Path(argv[0]).name,
            proc.returncode,
            self.redact(raw_out),
            self.redact(raw_err),
            time.monotonic() - start,
            proc.pid,
            leaked,
            None if sent is None else time.monotonic() - sent,
        )

    # --- forgectl ----------------------------

    def write_forgectl_config(
        self, me: Mapping[str, str] | None = None, token_env: bool = True
    ) -> Path:
        """Write forgectl's config into the scratch XDG_CONFIG_HOME: the counting gh and glab,
        and one entry per configured host that reads its token from LIVE_<KIND>_TOKEN."""
        hosts: dict[str, Any] = {}
        for kind, cfg in self.env.hosts.items():
            entry: dict[str, Any] = {"backend": kind}
            if token_env:
                entry["token_env"] = TOKEN_VARS[kind]
            if cfg.api_prefix:
                entry["api_prefix"] = cfg.api_prefix
            if me and kind in me:
                entry["me"] = me[kind]
            hosts[cfg.host] = entry
        doc: dict[str, Any] = {"hosts": hosts, "timeout": "90s"}
        if self.env.gh is not None:
            doc["gh_bin"] = str(self.bin / "gh")
        if self.env.glab is not None:
            doc["glab_bin"] = str(self.bin / "glab")
        cfgdir = self.config_home / "forgectl"
        cfgdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfgdir.chmod(0o700)
        path = cfgdir / "config.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        path.chmod(0o600)
        extra = self.env.extra_forgectl_config
        if extra is not None:
            drop = cfgdir / "config.d"
            drop.mkdir(exist_ok=True, mode=0o700)
            drop.chmod(0o700)
            target = drop / "50-extra.yaml"
            target.write_text(extra.read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o600)
        return path

    def forgectl(
        self, *args: str, tokens: bool = True, timeout: float = 150.0, stdin: str | None = None
    ) -> Result:
        """The real forgectl by its absolute path (the isolation guard's narrow opt-in)."""
        return self.run(
            [str(self.env.forgectl), *args],
            env=self.tool_env(tokens),
            stdin=stdin,
            timeout=timeout,
        )

    def fg_json(self, *args: str, timeout: float = 150.0) -> Any:
        res = self.forgectl(*args, "--json", timeout=timeout)
        assert res.rc == 0, (
            f"forgectl {args[0]} exit {res.rc}: {res.out[:400]!r} stderr: {res.err[:400]!r}"
        )
        return res.json()

    def snapshot(self, ref: str, *, bodies: bool = False) -> dict[str, Any]:
        args = ["snapshot", ref] if bodies else ["snapshot", ref, "--no-bodies"]
        return self.fg_json(*args)

    def listing(self, repo_id: str, *extra: str) -> dict[str, Any]:
        return self.fg_json("prs", f"--repo={repo_id}", *extra)

    def list_item(self, repo_id: str, number: int, state: str = "open") -> dict[str, Any] | None:
        data = self.listing(repo_id, f"--state={state}", "--limit=100")
        for item in data["items"]:
            if item["number"] == number:
                return item
        return None

    def wait_listed(
        self, repo_id: str, number: int, state: str = "open", timeout: float = 180.0
    ) -> dict[str, Any]:
        """The listing item of a PR once the index shows it (the listing lags the host)."""
        item, _ = wait_until(
            lambda: self.list_item(repo_id, number, state),
            timeout=timeout,
            interval=4,
            what=f"#{number} is listed as {state}",
        )
        return dict(item)

    def settled_stamps(
        self, repo_id: str, ref: str, number: int, timeout: float = 240.0
    ) -> tuple[str, str]:
        """(listing, show) `updated_at` once the listing has caught up with `show`."""

        def caught_up() -> tuple[str, str] | None:
            item = self.list_item(repo_id, number)
            shown = str(self.fg_json("show", ref)["updated_at"])
            if item is None or str(item["updated_at"]) != shown:
                return None
            return shown, shown

        value, _ = wait_until(
            caught_up, timeout=timeout, interval=4, what="the listing's updated_at equals show's"
        )
        return value

    # --- call counting ----------------------------

    def mark(self) -> int:
        return len(self.calls_log.read_text(encoding="utf-8").splitlines())

    def api_calls(self, since: int, tool: str) -> list[str]:
        lines = self.calls_log.read_text(encoding="utf-8").splitlines()[since:]
        out = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == tool and parts[1] == "api":
                out.append(line)
        return out

    # --- pr-watch --------------------------------------------------------------------------------

    def prwatch(
        self,
        *args: str,
        timeout: float = 240.0,
        extra_env: Mapping[str, str] | None = None,
        tokens: bool = True,
    ) -> Result:
        env = self.tool_env(tokens)
        env.update(extra_env or {})
        return self.run(
            [sys.executable, "-m", "pr_watch", "--json", *args], env=env, timeout=timeout
        )

    def pw_json(
        self, *args: str, ok: tuple[int, ...] = (0,), schema: str | None = None, **kw: Any
    ) -> Any:
        """Run pr-watch, require an exit code in `ok` and, with `schema` (a name under
        schemas/, for example `cli/poll`), that the output validates against it."""
        res = self.prwatch(*args, **kw)
        assert res.rc in ok, (
            f"pr-watch {' '.join(args[:2])} exit {res.rc}: {res.out[:500]!r} "
            f"stderr: {res.err[:500]!r}"
        )
        doc = res.json()
        if schema is not None:
            assert_valid(schema, doc)
        return doc

    def watch(self, ref: str, **kw: Any) -> str:
        """`watch add` (baselines now); returns the events cursor."""
        data = self.pw_json("watch", "add", ref, "--owner", OWNER, schema="cli/watch-add", **kw)
        assert data["baselined"] is True, data
        return str(data["cursor"])

    def poll_pr(self, ref: str, **kw: Any) -> dict[str, Any]:
        return self.pw_json("poll", "--pr", ref, "--no-hooks", schema="cli/poll", **kw)

    def events(self, cursor: str, timeout: float = 240.0) -> tuple[list[dict[str, Any]], str]:
        data = self.pw_json(
            "events", "--since", cursor, "--limit", "1000", schema="cli/events", timeout=timeout
        )
        for event in data["events"]:
            assert_valid("event", event)
        return list(data["events"]), str(data["cursor"])

    def poll_for(
        self, ref: str, cursor: str, want: str | set[str], timeout: float = 120.0, **kw: Any
    ) -> tuple[list[dict[str, Any]], str, float]:
        """Poll the PR until an event of type `want` is stored; (new events, cursor, seconds).
        No single call outlives the overall deadline."""
        wanted = {want} if isinstance(want, str) else set(want)
        collected: list[dict[str, Any]] = []
        state = {"cursor": cursor}
        deadline = time.monotonic() + timeout

        def check() -> bool:
            remaining = deadline - time.monotonic()
            if remaining < 1.0:
                return False
            per_call = min(kw.get("timeout", 240.0), remaining)
            self.poll_pr(ref, **{**kw, "timeout": per_call})
            events, state["cursor"] = self.events(state["cursor"], timeout=per_call)
            collected.extend(events)
            return any(e["type"] in wanted for e in collected)

        _, waited = wait_until(check, timeout=timeout, interval=4.0, what=f"event {sorted(wanted)}")
        return collected, state["cursor"], waited


def scrub_value(value: Any, scrub: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {str(k): scrub_value(v, scrub) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [scrub_value(v, scrub) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value))


def append_result(results_file: Path, row: Mapping[str, Any]) -> None:
    """Append one JSON line; the file is created 0600 and a link is never followed."""
    fd = os.open(results_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def record(
    test: str,
    results_file: Path | None,
    scrub: Callable[[str], str] = lambda text: text,
    **fields: Any,
) -> None:
    """One result line for the record (docs/live.md): printed (run with -s) and, when
    PR_WATCH_LIVE_RESULTS_FILE is set, appended there as JSON. Values pass through `scrub`
    (tokens redacted; host, repository and login names replaced) before they leave."""
    clean = {k: scrub_value(v, scrub) for k, v in fields.items()}
    line = " ".join(f"{k}={v}" for k, v in clean.items())
    print(f"LIVE-RESULT {test} {line}", flush=True)
    if results_file is not None:
        row = {"test": test, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **clean}
        append_result(results_file, row)


@dataclass
class Pr:
    number: int
    ref: str
    branch: str
    head_sha: str
    title: str
    created_local: float = field(default_factory=time.time)


class HostOps:
    """Actions on one throwaway repository. Subclasses speak the host's API."""

    kind = ""
    git_user = ""

    def __init__(self, ws: Workspace, cfg: HostCfg) -> None:
        self.ws = ws
        self.cfg = cfg
        self.path = cfg.path
        self.original_path = cfg.path
        self.clone = ws.root / f"clone-{cfg.kind}"
        self._cloned = False
        self._prs: list[int] = []
        self._branches: list[str] = []
        self._closed: set[int] = set()
        self._merged: set[int] = set()
        self._deleted: set[str] = set()
        self._seq = 0
        self.rename_pending: str | None = None  # the path a rename call was aimed at
        owner_rename = f"{cfg.path.rpartition('/')[0]}/{cfg.rename_to}".lstrip("/")
        for term, label in (
            (cfg.repo_id, "<repo>"),
            (f"{cfg.host}/{owner_rename}" if cfg.rename_to else "", "<repo>"),
            (cfg.path, "<repo-path>"),
            (owner_rename if cfg.rename_to else "", "<repo-path>"),
            (cfg.host, "<host>"),
        ):
            ws.add_scrub(term, label)

    # --- generic ----------------------------

    @property
    def repo_id(self) -> str:
        return f"{self.cfg.host}/{self.path}"

    def ref(self, number: int) -> str:
        return f"{self.repo_id}#{number}"

    def branch_name(self, tag: str) -> str:
        return f"{self.cfg.branch_prefix}/{self.ws.run_id}/{tag}"

    def unique(self) -> str:
        self._seq += 1
        return f"{self.ws.run_id}-{self._seq}-{time.time_ns()}"

    def unique_path(self, name: str) -> str:
        """A repository path no other PR of any run has used: file content is never identical
        across PRs, so a commit is never empty."""
        return f"live/{self.unique()}/{name}"

    def need_token2(self) -> str:
        if not self.cfg.token2:
            import pytest

            pytest.skip(
                f"manual step: set PR_WATCH_LIVE_{self.kind.upper()}_TOKEN2 (a second account "
                "with write access; docs/live.md, Second account)"
            )
        return self.cfg.token2

    # --- git ----------------------------

    def _remote_url(self) -> str:
        prefix = self.cfg.api_prefix or ""
        return f"https://{self.cfg.host}{prefix}/{self.path}.git"

    def _git_env(self) -> dict[str, str]:
        env = self.ws.base_env()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(self.ws.askpass),
                "LIVE_GIT_USER": self.git_user,
                "LIVE_GIT_PASS": self.cfg.token,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "pr-watch live",
                "GIT_AUTHOR_EMAIL": "live@example.invalid",
                "GIT_COMMITTER_NAME": "pr-watch live",
                "GIT_COMMITTER_EMAIL": "live@example.invalid",
            }
        )
        if "SSL_CERT_FILE" in os.environ:
            env["GIT_SSL_CAINFO"] = os.environ["SSL_CERT_FILE"]
        return env

    def git(self, *args: str, timeout: float = 180.0) -> str:
        res = self.ws.run(
            [str(self.ws.env.git), *args],
            env=self._git_env(),
            timeout=timeout,
            cwd=self.clone if self._cloned else self.ws.root,
        )
        if res.rc != 0:
            raise HostError(f"git {args[0]} failed ({res.rc}): {res.err[:400]}")
        return res.out.strip()

    def _ensure_clone(self) -> None:
        if self._cloned:
            return
        self.git("clone", "--quiet", self._remote_url(), str(self.clone))
        self._cloned = True
        self.git("fetch", "--quiet", "origin")

    def push_branch(
        self, branch: str, files: Mapping[str, str], message: str, *, start: str | None = None
    ) -> str:
        """Commit `files` on `branch` (created from `start`, default origin/<base>, or continued
        when it already exists locally) and push; returns the new head sha."""
        self._ensure_clone()
        if start is not None or branch not in self._branches:
            self.git("fetch", "--quiet", "origin")
            self.git("switch", "--quiet", "-C", branch, start or f"origin/{self.cfg.base_branch}")
        else:
            self.git("switch", "--quiet", branch)
        for rel, text in files.items():
            target = self.clone / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)
        self.git("push", "--quiet", "origin", f"{branch}:{branch}")
        if branch not in self._branches:
            self._branches.append(branch)
        return self.git("rev-parse", "HEAD")

    def add_commit(self, branch: str, message: str = "live: another commit") -> str:
        return self.push_branch(
            branch, {f"live/{self.unique()}.txt": self.unique() + "\n"}, message
        )

    def force_rewrite(self, branch: str) -> tuple[str, str]:
        """Replace the branch's last commit by a different one (a force push): (old, new)."""
        self._ensure_clone()
        self.git("switch", "--quiet", branch)
        old = self.git("rev-parse", "HEAD")
        self.git("switch", "--quiet", "-C", branch, "HEAD~1")
        (self.clone / "live").mkdir(exist_ok=True)
        (self.clone / "live" / f"{self.unique()}.txt").write_text("rewritten\n", encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "live: rewritten commit")
        self.git("push", "--quiet", "--force", "origin", f"{branch}:{branch}")
        return old, self.git("rev-parse", "HEAD")

    def branch_files(self) -> dict[str, str]:
        return {f"live/{self.unique()}.txt": f"{self.unique()}\n"}

    # --- API plumbing (subclasses) ----------------------------

    def api(self, method: str, endpoint: str, body: Any = None, *, token: str | None = None) -> Any:
        raise NotImplementedError

    def _api_result(self, res: Result, what: str) -> Any:
        if res.rc != 0:
            raise HostError(
                f"{what} failed ({res.rc}): {res.out[:300]} {res.err[:300]}",
                status=http_status(f"{res.out}\n{res.err}"),
            )
        text = res.out.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise HostError(f"{what}: the answer is not JSON: {text[:200]!r}") from None

    # --- PR actions (subclasses) ----------------------------

    def open_pr(
        self,
        tag: str,
        *,
        draft: bool = False,
        body: str = "pr-watch live test; safe to delete",
        files: Mapping[str, str] | None = None,
        base: str | None = None,
        start: str | None = None,
        with_ci: bool = True,
    ) -> Pr:
        branch = self.branch_name(tag)
        title = f"live {self.ws.run_id} {tag}"
        all_files = dict(files) if files is not None else self.branch_files()
        if with_ci:
            all_files.update(self.ci_files())
        sha = self.push_branch(branch, all_files, f"live: {tag}", start=start)
        number = self.create_pr(branch, title, body=body, draft=draft, base=base)
        self._prs.append(number)
        return Pr(number, self.ref(number), branch, sha, title)

    def ci_files(self) -> dict[str, str]:
        return {}

    def create_pr(
        self, branch: str, title: str, *, body: str, draft: bool, base: str | None
    ) -> int:
        raise NotImplementedError

    def _close(self, number: int) -> None:
        raise NotImplementedError

    def close(self, number: int) -> None:
        self._close(number)
        self._closed.add(number)

    def _reopen(self, number: int) -> None:
        raise NotImplementedError

    def reopen(self, number: int) -> None:
        self._reopen(number)
        self._closed.discard(number)

    def retarget(self, number: int, base: str) -> None:
        raise NotImplementedError

    def set_draft(self, number: int, draft: bool) -> None:
        raise NotImplementedError

    def comment(self, number: int, body: str) -> None:
        raise NotImplementedError

    def inline_thread(self, number: int, path: str, body: str) -> None:
        raise NotImplementedError

    def resolve_thread(self, number: int, thread_id: str) -> None:
        raise NotImplementedError

    def _user(self, token: str | None) -> tuple[str, str]:
        raise NotImplementedError

    def user(self, token: str | None = None) -> tuple[str, str]:
        """(login, numeric id) of the account behind a token (default: the primary). The login
        joins the names recorded output hides."""
        login, user_id = self._user(token)
        self.ws.add_scrub(login, "<login>")
        return login, user_id

    def request_review(self, number: int, login: str, user_id: str) -> None:
        raise NotImplementedError

    def remove_review_request(self, number: int, login: str, user_id: str) -> None:
        raise NotImplementedError

    def approve(self, number: int) -> None:
        """Approve as the second account."""
        raise NotImplementedError

    def withdraw_approval(self, number: int) -> None:
        """Dismiss (GitHub, by the primary account) or unapprove (GitLab, by the second)."""
        raise NotImplementedError

    def request_changes(self, number: int) -> None:
        raise NotImplementedError

    def _merge(self, number: int, *, delete_branch: bool) -> None:
        raise NotImplementedError

    def merge(self, number: int, *, delete_branch: bool = False) -> None:
        self._merge(number, delete_branch=delete_branch)
        self._merged.add(number)

    def _delete_branch(self, branch: str) -> None:
        raise NotImplementedError

    def delete_branch(self, branch: str) -> None:
        self._delete_branch(branch)
        self._deleted.add(branch)

    def _rename_api(self, current_path: str, new_name: str) -> None:
        """The host call that renames the repository now at `current_path`."""
        raise NotImplementedError

    def canonical_path(self) -> str:
        """The repository's canonical `owner/repo` (or project path) as the host reports it."""
        raise NotImplementedError

    def verify_target(self) -> None:
        """Fail unless the configured repository is the host's canonical name for it (a rename
        left behind by a killed run, or a redirect, would make later writes go elsewhere)."""
        try:
            canonical = self.canonical_path()
        except HostError as exc:
            raise HostError(
                f"the {self.kind} throwaway repository could not be read: {exc}", exc.status
            ) from None
        if canonical.casefold() != self.cfg.path.casefold():
            raise HostError(
                f"the repository named by PR_WATCH_LIVE_{self.kind.upper()}_REPO is not the "
                "canonical name the host reports (compared without regard to case; an earlier "
                "run may have been killed after a rename: see docs/live.md, Recovering after a "
                "killed run); fix the variable "
                "or rename the repository back before running"
            )

    # --- rename with a recovery marker ----------------------------

    def _pending_files(self) -> list[Path]:
        files = [self.ws.root / f"RENAME-PENDING-{self.kind}.txt"]
        results = self.ws.env.results_file
        if results is not None:
            files.append(results.with_name(f"{results.name}.rename-pending-{self.kind}"))
        return files

    def rename_repo(self, new_name: str) -> None:
        """Rename the repository. A marker (in memory and in files) is written first and stays
        until `cleanup` has put the original name back, whatever this call ends up doing."""
        new_path = f"{self.path.rpartition('/')[0]}/{new_name}".lstrip("/")
        self.rename_pending = new_path
        note = (
            f"A {self.kind} throwaway repository may have been renamed to a different name by "
            f"run {self.ws.run_id}. Its original name is {self.original_path} on host "
            f"{self.cfg.host}. If it no longer answers to that name, rename it back (docs/live.md, "
            "Recovering after a killed run).\n"
        )
        for marker in self._pending_files():
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(note)
            except OSError:
                pass
        self._rename_api(self.path, new_name)
        self.path = new_path

    def _restore_name(self) -> list[str]:
        """Rename back, unconditionally when a marker is set (the call may have gone through
        even though it errored or timed out): try the current name, the aimed-at name, the
        original one."""
        if self.rename_pending is None and self.path == self.original_path:
            return []
        original_name = self.original_path.rsplit("/", 1)[-1]
        candidates = [self.path, self.rename_pending or self.path, self.original_path]
        last: Exception | None = None
        for candidate in dict.fromkeys(candidates):
            try:
                self._rename_api(candidate, original_name)
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
            self.path = self.original_path
            self.rename_pending = None
            for marker in self._pending_files():
                marker.unlink(missing_ok=True)
            return []
        return [f"RENAME BACK FAILED, now {self.path} or {self.rename_pending}: {last}"]

    def pr_updated_at(self, number: int) -> str:
        raise NotImplementedError

    def cleanup(self) -> list[str]:
        """Close every PR and delete every branch this run made; returns the real failures. A PR
        this run closed or merged and a branch it deleted are not touched again, and a host
        answer that says it is already gone (404, 422) counts as done."""
        problems = self._restore_name()
        for number in self._prs:
            if number in self._closed or number in self._merged:
                continue
            try:
                self.close(number)
            except HostError as exc:
                if exc.status not in GONE:
                    problems.append(f"close #{number}: {exc}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"close #{number}: {exc}")
        for branch in self._branches:
            if branch in self._deleted:
                continue
            try:
                self.delete_branch(branch)
            except HostError as exc:
                if exc.status not in GONE:
                    problems.append(f"delete branch {branch}: {exc}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"delete branch {branch}: {exc}")
        return problems


class GitHubOps(HostOps):
    kind = "github"
    git_user = "x-access-token"

    def __init__(self, ws: Workspace, cfg: HostCfg) -> None:
        super().__init__(ws, cfg)
        self._node: dict[int, str] = {}
        self._review_ids: dict[int, int] = {}

    def api(self, method: str, endpoint: str, body: Any = None, *, token: str | None = None) -> Any:
        gh = self.ws.env.gh
        assert gh is not None
        argv = [str(gh), "api", "--hostname", self.cfg.host, "-X", method, endpoint]
        argv += ["-H", "Accept: application/vnd.github+json"]
        if body is not None:
            argv += ["--input", "-"]
        env = self.ws.base_env()
        env.update(
            {
                github_token_variable(self.cfg.host): token or self.cfg.token,
                "GH_CONFIG_DIR": str(self.ws.root / "gh-config"),
                "GH_PROMPT_DISABLED": "1",
                "GH_NO_UPDATE_NOTIFIER": "1",
            }
        )
        res = self.ws.run(
            argv, env=env, stdin=json.dumps(body) if body is not None else None, timeout=90
        )
        return self._api_result(res, f"gh api {method} {endpoint}")

    def _repo(self) -> str:
        return f"repos/{self.path}"

    def create_pr(
        self, branch: str, title: str, *, body: str, draft: bool, base: str | None
    ) -> int:
        data = self.api(
            "POST",
            f"{self._repo()}/pulls",
            {
                "title": title,
                "head": branch,
                "base": base or self.cfg.base_branch,
                "body": body,
                "draft": draft,
            },
        )
        self._node[data["number"]] = data["node_id"]
        return int(data["number"])

    def _patch(self, number: int, body: dict[str, Any]) -> Any:
        return self.api("PATCH", f"{self._repo()}/pulls/{number}", body)

    def _close(self, number: int) -> None:
        self._patch(number, {"state": "closed"})

    def _reopen(self, number: int) -> None:
        self._patch(number, {"state": "open"})

    def retarget(self, number: int, base: str) -> None:
        self._patch(number, {"base": base})

    def _graphql(self, query: str, variables: dict[str, Any]) -> Any:
        return self.api("POST", "graphql", {"query": query, "variables": variables})

    def set_draft(self, number: int, draft: bool) -> None:
        node = self._node[number]
        if draft:
            q = (
                "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id})"
                "{pullRequest{isDraft}}}"
            )
        else:
            q = (
                "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
                "{pullRequest{isDraft}}}"
            )
        data = self._graphql(q, {"id": node})
        if isinstance(data, dict) and data.get("errors"):
            raise HostError(f"draft toggle refused: {str(data['errors'])[:200]}")

    def comment(self, number: int, body: str) -> None:
        self.api("POST", f"{self._repo()}/issues/{number}/comments", {"body": body})

    def inline_thread(self, number: int, path: str, body: str) -> None:
        pr = self.api("GET", f"{self._repo()}/pulls/{number}")
        self.api(
            "POST",
            f"{self._repo()}/pulls/{number}/reviews",
            {
                "commit_id": pr["head"]["sha"],
                "event": "COMMENT",
                "body": "live review body",
                "comments": [{"path": path, "line": 1, "side": "RIGHT", "body": body}],
            },
        )

    def resolve_thread(self, number: int, thread_id: str) -> None:
        node = thread_id.removeprefix("gh:rt:")
        q = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
        data = self._graphql(q, {"id": node})
        if isinstance(data, dict) and data.get("errors"):
            raise HostError(f"resolve refused: {str(data['errors'])[:200]}")

    def _user(self, token: str | None) -> tuple[str, str]:
        data = self.api("GET", "user", token=token)
        return str(data["login"]), str(data["id"])

    def request_review(self, number: int, login: str, user_id: str) -> None:
        self.api(
            "POST", f"{self._repo()}/pulls/{number}/requested_reviewers", {"reviewers": [login]}
        )

    def remove_review_request(self, number: int, login: str, user_id: str) -> None:
        self.api(
            "DELETE", f"{self._repo()}/pulls/{number}/requested_reviewers", {"reviewers": [login]}
        )

    def approve(self, number: int) -> None:
        data = self.api(
            "POST",
            f"{self._repo()}/pulls/{number}/reviews",
            {"event": "APPROVE", "body": "live approval"},
            token=self.need_token2(),
        )
        self._review_ids[number] = int(data["id"])

    def withdraw_approval(self, number: int) -> None:
        self.api(
            "PUT",
            f"{self._repo()}/pulls/{number}/reviews/{self._review_ids[number]}/dismissals",
            {"message": "live dismissal"},
        )

    def request_changes(self, number: int) -> None:
        data = self.api(
            "POST",
            f"{self._repo()}/pulls/{number}/reviews",
            {"event": "REQUEST_CHANGES", "body": "live: please change"},
            token=self.need_token2(),
        )
        self._review_ids[number] = int(data["id"])

    def _merge(self, number: int, *, delete_branch: bool) -> None:
        pr = self.api("GET", f"{self._repo()}/pulls/{number}")
        branch = pr["head"]["ref"]

        def attempt() -> bool:
            self.api("PUT", f"{self._repo()}/pulls/{number}/merge", {"merge_method": "merge"})
            return True

        wait_until(attempt, timeout=90, interval=5, what=f"merge of #{number}", give_up=fail_fast)
        if delete_branch:
            self.delete_branch(branch)

    def _delete_branch(self, branch: str) -> None:
        ref = urllib.parse.quote(f"heads/{branch}", safe="/")
        self.api("DELETE", f"{self._repo()}/git/refs/{ref}")

    def _rename_api(self, current_path: str, new_name: str) -> None:
        self.api("PATCH", f"repos/{current_path}", {"name": new_name})

    def canonical_path(self) -> str:
        return str(self.api("GET", self._repo())["full_name"])

    def set_status(self, sha: str, state: str) -> None:
        """A commit status on the PR head (`pending`, `success`, `failure`): CI without runners."""
        self.api(
            "POST",
            f"{self._repo()}/statuses/{sha}",
            {"state": state, "context": "live/ci", "description": f"live {state}"},
        )

    def pr_updated_at(self, number: int) -> str:
        return str(self.api("GET", f"{self._repo()}/pulls/{number}")["updated_at"])


class GitLabOps(HostOps):
    kind = "gitlab"
    git_user = "oauth2"

    def __init__(self, ws: Workspace, cfg: HostCfg) -> None:
        super().__init__(ws, cfg)
        self._project_id: str | None = None
        self._titles: dict[int, str] = {}

    def api(self, method: str, endpoint: str, body: Any = None, *, token: str | None = None) -> Any:
        glab = self.ws.env.glab
        assert glab is not None
        argv = [str(glab), "api"]
        if self.cfg.host == "gitlab.com":
            argv += ["--hostname", "gitlab.com"]
        argv += ["-X", method, endpoint, "-H", "Content-Type: application/json"]
        if body is not None:
            argv += ["--input", "-"]
        env = self.ws.base_env()
        env.update(
            {
                "GITLAB_TOKEN": token or self.cfg.token,
                "GLAB_CONFIG_DIR": str(self.ws.root / "glab-config"),
                "GLAB_NO_PROMPT": "1",  # never NO_PROMPT: glab prints a deprecation notice for it
                "GLAB_SHOW_WHATS_NEW": "false",
                "GLAB_SEND_TELEMETRY": "false",
                "GLAB_CHECK_UPDATE": "false",
            }
        )
        if self.cfg.host != "gitlab.com":
            env["GITLAB_HOST"] = f"{self.cfg.host}{self.cfg.api_prefix or ''}"
        res = self.ws.run(
            argv, env=env, stdin=json.dumps(body) if body is not None else None, timeout=90
        )
        return self._api_result(res, f"glab api {method} {endpoint}")

    def _proj(self) -> str:
        if self._project_id is None:
            data = self.api("GET", f"projects/{urllib.parse.quote(self.path, safe='')}")
            self._project_id = str(data["id"])
        return f"projects/{self._project_id}"

    def ci_files(self) -> dict[str, str]:
        if not self.cfg.ci:
            return {}
        return {".gitlab-ci.yml": "live-job:\n  script:\n    - sleep 20\n"}

    def create_pr(
        self, branch: str, title: str, *, body: str, draft: bool, base: str | None
    ) -> int:
        full = f"Draft: {title}" if draft else title
        data = self.api(
            "POST",
            f"{self._proj()}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": base or self.cfg.base_branch,
                "title": full,
                "description": body,
                "remove_source_branch": False,
            },
        )
        self._titles[int(data["iid"])] = title
        return int(data["iid"])

    def _put(self, number: int, body: dict[str, Any]) -> Any:
        return self.api("PUT", f"{self._proj()}/merge_requests/{number}", body)

    def _close(self, number: int) -> None:
        self._put(number, {"state_event": "close"})

    def _reopen(self, number: int) -> None:
        self._put(number, {"state_event": "reopen"})

    def retarget(self, number: int, base: str) -> None:
        self._put(number, {"target_branch": base})

    def set_draft(self, number: int, draft: bool) -> None:
        title = self._titles[number]
        self._put(number, {"title": f"Draft: {title}" if draft else title})

    def comment(self, number: int, body: str) -> None:
        self.api("POST", f"{self._proj()}/merge_requests/{number}/notes", {"body": body})

    def inline_thread(self, number: int, path: str, body: str) -> None:
        # diff_refs is null for a moment after the merge request is created
        refs, _ = wait_until(
            lambda: (self.api("GET", f"{self._proj()}/merge_requests/{number}") or {}).get(
                "diff_refs"
            ),
            timeout=90,
            interval=3,
            what="diff_refs of the merge request",
        )
        self.api(
            "POST",
            f"{self._proj()}/merge_requests/{number}/discussions",
            {
                "body": body,
                "position": {
                    "position_type": "text",
                    "base_sha": refs["base_sha"],
                    "start_sha": refs["start_sha"],
                    "head_sha": refs["head_sha"],
                    "new_path": path,
                    "old_path": path,
                    "new_line": 1,
                },
            },
        )

    def resolve_thread(self, number: int, thread_id: str) -> None:
        did = thread_id.removeprefix("gl:d:")
        self.api(
            "PUT", f"{self._proj()}/merge_requests/{number}/discussions/{did}", {"resolved": True}
        )

    def _user(self, token: str | None) -> tuple[str, str]:
        data = self.api("GET", "user", token=token)
        return str(data["username"]), str(data["id"])

    def request_review(self, number: int, login: str, user_id: str) -> None:
        self._put(number, {"reviewer_ids": [int(user_id)]})

    def remove_review_request(self, number: int, login: str, user_id: str) -> None:
        self._put(number, {"reviewer_ids": []})

    def approve(self, number: int) -> None:
        self.api(
            "POST", f"{self._proj()}/merge_requests/{number}/approve", {}, token=self.need_token2()
        )

    def withdraw_approval(self, number: int) -> None:
        self.api(
            "POST",
            f"{self._proj()}/merge_requests/{number}/unapprove",
            {},
            token=self.need_token2(),
        )

    def request_changes(self, number: int) -> None:
        query = (
            "mutation($p:ID!,$i:String!){mergeRequestRequestChanges(input:"
            "{projectPath:$p,iid:$i}){errors}}"
        )
        data = self.api(
            "POST",
            "graphql",
            {"query": query, "variables": {"p": self.path, "i": str(number)}},
            token=self.need_token2(),
        )
        errors = graphql_errors(data)
        if errors:
            raise HostError(f"request changes refused: {'; '.join(errors)[:300]}")

    def _merge(self, number: int, *, delete_branch: bool) -> None:
        def attempt() -> bool:
            self.api(
                "PUT",
                f"{self._proj()}/merge_requests/{number}/merge",
                {"should_remove_source_branch": delete_branch},
            )
            return True

        wait_until(attempt, timeout=120, interval=5, what=f"merge of !{number}", give_up=fail_fast)

    def _delete_branch(self, branch: str) -> None:
        self.api(
            "DELETE", f"{self._proj()}/repository/branches/{urllib.parse.quote(branch, safe='')}"
        )

    def _rename_api(self, current_path: str, new_name: str) -> None:
        # by project id (cached before any rename): works under whichever name the project has
        self.api("PUT", self._proj(), {"path": new_name})

    def canonical_path(self) -> str:
        data = self.api("GET", f"projects/{urllib.parse.quote(self.path, safe='')}")
        return str(data["path_with_namespace"])

    def pipelines_done(self, number: int) -> bool:
        pipes = self.api("GET", f"{self._proj()}/merge_requests/{number}/pipelines")
        return bool(pipes) and all(p["status"] in ("success", "failed") for p in pipes)

    def pr_updated_at(self, number: int) -> str:
        return str(self.api("GET", f"{self._proj()}/merge_requests/{number}")["updated_at"])


def make_ops(ws: Workspace, cfg: HostCfg) -> HostOps:
    return GitHubOps(ws, cfg) if cfg.kind == "github" else GitLabOps(ws, cfg)
