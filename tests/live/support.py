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

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tests.live_env import HostCfg, LiveEnv

PASS_THROUGH = (
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GLAB_CA_CERT",
)  # fmt: skip
TOKEN_VARS = {"github": "LIVE_GITHUB_TOKEN", "gitlab": "LIVE_GITLAB_TOKEN"}
OWNER = "live"


class HostError(RuntimeError):
    """A host action failed (the message is already redacted)."""


@dataclass
class Result:
    argv0: str
    rc: int
    out: str
    err: str
    elapsed: float
    pid: int

    def json(self) -> Any:
        try:
            return json.loads(self.out)
        except ValueError as exc:
            raise AssertionError(
                f"{self.argv0} rc={self.rc}: stdout is not JSON: {self.out[:300]!r} "
                f"stderr: {self.err[:300]!r}"
            ) from exc


def wait_until(
    check: Callable[[], Any], *, timeout: float, interval: float = 2.0, what: str
) -> tuple[Any, float]:
    """Call `check` until it returns something truthy; returns (value, seconds waited).

    Bounded: raises AssertionError after `timeout` seconds."""
    start = time.monotonic()
    while True:
        value = check()
        if value:
            return value, time.monotonic() - start
        if time.monotonic() - start >= timeout:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what}")
        time.sleep(interval)


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
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def close(self) -> None:
        self.secrets = []

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
    ) -> Result:
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
        try:
            out, err = proc.communicate(stdin.encode() if stdin is not None else None, timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise AssertionError(
                f"{Path(argv[0]).name} (pid {proc.pid}) overran its {timeout:.0f}s bound"
            ) from None
        return Result(
            Path(argv[0]).name,
            proc.returncode,
            self.redact(out.decode("utf-8", "replace")),
            self.redact(err.decode("utf-8", "replace")),
            time.monotonic() - start,
            proc.pid,
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

    def pw_json(self, *args: str, ok: tuple[int, ...] = (0,), **kw: Any) -> Any:
        res = self.prwatch(*args, **kw)
        assert res.rc in ok, (
            f"pr-watch {' '.join(args[:2])} exit {res.rc}: {res.out[:500]!r} "
            f"stderr: {res.err[:500]!r}"
        )
        return res.json()

    def watch(self, ref: str, **kw: Any) -> str:
        """`watch add` (baselines now); returns the events cursor."""
        data = self.pw_json("watch", "add", ref, "--owner", OWNER, **kw)
        assert data["baselined"] is True, data
        return str(data["cursor"])

    def poll_pr(self, ref: str, **kw: Any) -> dict[str, Any]:
        return self.pw_json("poll", "--pr", ref, "--no-hooks", **kw)

    def events(self, cursor: str) -> tuple[list[dict[str, Any]], str]:
        data = self.pw_json("events", "--since", cursor, "--limit", "1000")
        return list(data["events"]), str(data["cursor"])

    def poll_for(
        self, ref: str, cursor: str, want: str | set[str], timeout: float = 120.0, **kw: Any
    ) -> tuple[list[dict[str, Any]], str, float]:
        """Poll the PR until an event of type `want` is stored; (new events, cursor, seconds)."""
        wanted = {want} if isinstance(want, str) else set(want)
        collected: list[dict[str, Any]] = []
        state = {"cursor": cursor}

        def check() -> bool:
            self.poll_pr(ref, **kw)
            events, state["cursor"] = self.events(state["cursor"])
            collected.extend(events)
            return any(e["type"] in wanted for e in collected)

        _, waited = wait_until(check, timeout=timeout, interval=4.0, what=f"event {sorted(wanted)}")
        return collected, state["cursor"], waited


def record(test: str, results_file: Path | None, **fields: Any) -> None:
    """One result line for the record (docs/live.md): printed (run with -s) and, when
    PR_WATCH_LIVE_RESULTS_FILE is set, appended there as JSON."""
    line = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"LIVE-RESULT {test} {line}", flush=True)
    if results_file is not None:
        row = {"test": test, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
        with results_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")


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
        self._seq = 0

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
            raise HostError(f"{what} failed ({res.rc}): {res.out[:300]} {res.err[:300]}")
        text = res.out.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

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

    def close(self, number: int) -> None:
        raise NotImplementedError

    def reopen(self, number: int) -> None:
        raise NotImplementedError

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

    def user(self, token: str | None = None) -> tuple[str, str]:
        """(login, numeric id) of the account behind a token (default: the primary)."""
        raise NotImplementedError

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

    def merge(self, number: int, *, delete_branch: bool = False) -> None:
        raise NotImplementedError

    def delete_branch(self, branch: str) -> None:
        raise NotImplementedError

    def rename_repo(self, new_name: str) -> None:
        raise NotImplementedError

    def pr_updated_at(self, number: int) -> str:
        raise NotImplementedError

    def cleanup(self) -> list[str]:
        """Close every PR and delete every branch this run made; returns the failures."""
        problems: list[str] = []
        if self.path != self.original_path:
            try:
                self.rename_repo(self.original_path.rsplit("/", 1)[-1])
            except Exception as exc:  # noqa: BLE001
                problems.append(f"RENAME BACK FAILED, repository is now {self.path}: {exc}")
        for number in self._prs:
            try:
                self.close(number)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"close #{number}: {exc}")
        for branch in self._branches:
            try:
                self.delete_branch(branch)
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
                "GH_TOKEN": token or self.cfg.token,
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

    def close(self, number: int) -> None:
        self._patch(number, {"state": "closed"})

    def reopen(self, number: int) -> None:
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

    def user(self, token: str | None = None) -> tuple[str, str]:
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

    def merge(self, number: int, *, delete_branch: bool = False) -> None:
        pr = self.api("GET", f"{self._repo()}/pulls/{number}")
        branch = pr["head"]["ref"]

        def attempt() -> bool:
            try:
                self.api("PUT", f"{self._repo()}/pulls/{number}/merge", {"merge_method": "merge"})
                return True
            except HostError:
                return False

        wait_until(attempt, timeout=90, interval=5, what=f"merge of #{number}")
        if delete_branch:
            self.delete_branch(branch)

    def delete_branch(self, branch: str) -> None:
        ref = urllib.parse.quote(f"heads/{branch}", safe="/")
        self.api("DELETE", f"{self._repo()}/git/refs/{ref}")

    def rename_repo(self, new_name: str) -> None:
        owner = self.path.split("/")[0]
        self.api("PATCH", self._repo(), {"name": new_name})
        self.path = f"{owner}/{new_name}"

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
                "NO_PROMPT": "1",
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

    def close(self, number: int) -> None:
        self._put(number, {"state_event": "close"})

    def reopen(self, number: int) -> None:
        self._put(number, {"state_event": "reopen"})

    def retarget(self, number: int, base: str) -> None:
        self._put(number, {"target_branch": base})

    def set_draft(self, number: int, draft: bool) -> None:
        title = self._titles[number]
        self._put(number, {"title": f"Draft: {title}" if draft else title})

    def comment(self, number: int, body: str) -> None:
        self.api("POST", f"{self._proj()}/merge_requests/{number}/notes", {"body": body})

    def inline_thread(self, number: int, path: str, body: str) -> None:
        mr = self.api("GET", f"{self._proj()}/merge_requests/{number}")
        refs = mr["diff_refs"]
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

    def user(self, token: str | None = None) -> tuple[str, str]:
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
        if isinstance(data, dict) and (data.get("errors") or data.get("data") is None):
            raise HostError(f"request changes refused: {str(data)[:200]}")

    def merge(self, number: int, *, delete_branch: bool = False) -> None:
        def attempt() -> bool:
            try:
                self.api(
                    "PUT",
                    f"{self._proj()}/merge_requests/{number}/merge",
                    {"should_remove_source_branch": delete_branch},
                )
                return True
            except HostError:
                return False

        wait_until(attempt, timeout=120, interval=5, what=f"merge of !{number}")

    def delete_branch(self, branch: str) -> None:
        self.api(
            "DELETE", f"{self._proj()}/repository/branches/{urllib.parse.quote(branch, safe='')}"
        )

    def rename_repo(self, new_name: str) -> None:
        parent = self.path.rsplit("/", 1)[0] if "/" in self.path else ""
        self.api("PUT", self._proj(), {"path": new_name, "name": new_name})
        self.path = f"{parent}/{new_name}" if parent else new_name

    def pipelines_done(self, number: int) -> bool:
        pipes = self.api("GET", f"{self._proj()}/merge_requests/{number}/pipelines")
        return bool(pipes) and all(p["status"] in ("success", "failed") for p in pipes)

    def pr_updated_at(self, number: int) -> str:
        return str(self.api("GET", f"{self._proj()}/merge_requests/{number}")["updated_at"])


def make_ops(ws: Workspace, cfg: HostCfg) -> HostOps:
    return GitHubOps(ws, cfg) if cfg.kind == "github" else GitLabOps(ws, cfg)
