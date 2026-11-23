"""Opt-in and configuration of the `live` suite (docs/live.md lists every variable).

Nothing here reaches a host. `capture` reads the environment once, at pytest_configure and before
tests/conftest.py scrubs it, and only when `PR_WATCH_LIVE=1`; `parse` turns the captured values
into a validated configuration; `guard_allowance` names the single binary the isolation guard may
let a `live`-marked test launch (the real forgectl). Everything is a pure function of its input,
so tests/test_live_support.py covers it without a host.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

OPT_IN = "PR_WATCH_LIVE"
PREFIX = "PR_WATCH_LIVE_"
KINDS = ("github", "gitlab")
FORGECTL_VAR = f"{PREFIX}FORGECTL"
TOOL_VARS = {"git": f"{PREFIX}GIT", "gh": f"{PREFIX}GH", "glab": f"{PREFIX}GLAB"}

MIN_TOKEN_LENGTH = 8
# Every pattern is applied with `fullmatch` (a `$` would accept a trailing newline).
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[1-9][0-9]{0,4})?")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,100}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_PREFIX_PATH = re.compile(r"(/[A-Za-z0-9._-]{1,100}){1,8}")
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_MARK_WORD = re.compile(r"\w+")


class LiveConfigError(Exception):
    """The opt-in is set but a value is malformed (never carries a value, only a variable name)."""


@dataclass(frozen=True)
class HostCfg:
    kind: str  # "github" or "gitlab"
    host: str
    path: str  # owner/repo, or the full GitLab project path
    token: str = field(repr=False)
    token2: str | None = field(repr=False)
    base_branch: str
    branch_prefix: str
    api_prefix: str | None
    rename_to: str | None
    ci: bool  # GitLab only: the project has a runner, so a pushed .gitlab-ci.yml runs

    @property
    def repo_id(self) -> str:
        return f"{self.host}/{self.path}"


@dataclass(frozen=True)
class LiveEnv:
    forgectl: Path
    git: Path
    gh: Path | None
    glab: Path | None
    hosts: Mapping[str, HostCfg]
    extra_forgectl_config: Path | None
    huge_pr: str | None
    results_file: Path | None
    skipped: Mapping[str, str]  # kind -> why that host's tests skip


def capture(environ: Mapping[str, str]) -> dict[str, str] | None:
    """The `PR_WATCH_LIVE*` variables (plus the resolved git, gh and glab paths), or None.

    None unless `PR_WATCH_LIVE` is exactly `1`: without the opt-in nothing is kept."""
    if environ.get(OPT_IN) != "1":
        return None
    raw = {name: value for name, value in environ.items() if name.startswith(PREFIX)}
    search = environ.get("PATH", os.defpath)
    for tool, var in TOOL_VARS.items():
        if not raw.get(var):
            found = shutil.which(tool, path=search)
            if found:
                raw[var] = found
    return raw


def _abs_file(raw: Mapping[str, str], var: str) -> Path | None:
    value = raw.get(var)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise LiveConfigError(f"{var} must be the absolute path of an executable file")
    if "crontab" in (path.name, path.resolve().name):
        raise LiveConfigError(f"{var} must not be crontab")
    return path


def _segment_ok(value: str) -> bool:
    """One repository or group name: plain characters, not only dots, no `.git` or `.lock` end."""
    return (
        _SEGMENT.fullmatch(value) is not None
        and value.strip(".") != ""
        and not value.lower().endswith((".git", ".lock"))
    )


def _repo(var: str, value: str, kind: str) -> tuple[str, str]:
    host, _, path = value.partition("/")
    segments = path.split("/") if path else []
    ok = (
        _HOST.fullmatch(host) is not None
        and ".." not in host
        and bool(segments)
        and all(_segment_ok(s) for s in segments)
        and (len(segments) == 2 if kind == "github" else True)
    )
    if not ok:
        raise LiveConfigError(
            f"{var} must look like host/owner/repo (host/group/.../project), "
            "without a .git suffix or dot-only names"
        )
    return host, path


def _token(var: str, value: str) -> str:
    if not value or re.search(r"\s", value) or len(value) < MIN_TOKEN_LENGTH:
        raise LiveConfigError(
            f"{var} must be a token without whitespace of at least {MIN_TOKEN_LENGTH} characters"
        )
    return value


def branch_name_ok(name: str) -> bool:
    """A conservative subset of git's ref-name rules (plain characters only)."""
    if _BRANCH.fullmatch(name) is None:
        return False
    if ".." in name or "//" in name or name.endswith(("/", ".")):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/")
    )


def _huge_pr(var: str, value: str, hosts: Mapping[str, HostCfg]) -> str:
    """`host/path#N` on one of the configured throwaway hosts (never a flag, never another host)."""
    repo, sep, number = value.rpartition("#")
    host = repo.partition("/")[0]
    kinds = [c.kind for c in hosts.values() if c.host == host]
    if value.startswith("-") or not sep or _PR_NUMBER.fullmatch(number) is None or not kinds:
        raise LiveConfigError(
            f"{var} must look like host/path#N on one of the configured throwaway hosts"
        )
    _repo(var, repo, kinds[0])
    return value


def selects_live(markexpr: str) -> bool:
    """True when a `-m` expression explicitly selects `live`: the word appears and is not
    directly preceded by `not` (so `not live` and `not slow` do not select the live suite)."""
    words = _MARK_WORD.findall(markexpr or "")
    return any(w == "live" and (i == 0 or words[i - 1] != "not") for i, w in enumerate(words))


def parse(raw: Mapping[str, str]) -> LiveEnv:
    """Validate the captured variables. Missing optional pieces make a host skip, never fail;
    a malformed value raises LiveConfigError."""
    forgectl = _abs_file(raw, FORGECTL_VAR)
    if forgectl is None:
        raise _Missing(f"{FORGECTL_VAR} (absolute path of the real forgectl) is not set")
    git = _abs_file(raw, TOOL_VARS["git"])
    if git is None:
        raise _Missing(f"git not found; set {TOOL_VARS['git']} to its absolute path")
    gh = _abs_file(raw, TOOL_VARS["gh"])
    glab = _abs_file(raw, TOOL_VARS["glab"])
    hosts: dict[str, HostCfg] = {}
    skipped: dict[str, str] = {}
    for kind in KINDS:
        p = f"{PREFIX}{kind.upper()}_"
        repo, token = raw.get(p + "REPO"), raw.get(p + "TOKEN")
        missing = [p + n for n, v in (("REPO", repo), ("TOKEN", token)) if not v]
        cli = gh if kind == "github" else glab
        if cli is None:
            missing.append(f"{'gh' if kind == 'github' else 'glab'} (not found on PATH)")
        if missing or repo is None or token is None:
            skipped[kind] = "not configured: " + ", ".join(missing)
            continue
        host, path = _repo(p + "REPO", repo, kind)
        throwaway = raw.get(p + "THROWAWAY")
        if not throwaway:
            skipped[kind] = (
                f"not confirmed as a throwaway: set {p}THROWAWAY to the value of {p}REPO "
                "(the suite writes to that repository)"
            )
            continue
        if throwaway != repo:
            raise LiveConfigError(f"{p}THROWAWAY must be exactly equal to {p}REPO")
        token_value = _token(p + "TOKEN", token)
        token2 = raw.get(p + "TOKEN2") or None
        if token2 is not None:
            _token(p + "TOKEN2", token2)
            if token2 == token_value:
                raise LiveConfigError(f"{p}TOKEN2 must be a different account's token than TOKEN")
        base = raw.get(p + "BASE_BRANCH") or "main"
        prefix = raw.get(p + "BRANCH_PREFIX") or "pr-watch-live"
        if not branch_name_ok(base) or not branch_name_ok(prefix):
            raise LiveConfigError(f"{p}BASE_BRANCH or {p}BRANCH_PREFIX is not a plain branch name")
        api_prefix = raw.get(p + "API_PREFIX") or None
        if api_prefix is not None and (
            kind != "gitlab"
            or _PREFIX_PATH.fullmatch(api_prefix) is None
            or any(part in (".", "..") for part in api_prefix.split("/"))
        ):
            raise LiveConfigError(
                f"{p}API_PREFIX must be a path like /gitlab with no trailing slash (GitLab only)"
            )
        rename = raw.get(p + "RENAME_TO") or None
        if rename is not None and (
            not _segment_ok(rename) or rename.lower() == path.rsplit("/", 1)[-1].lower()
        ):
            raise LiveConfigError(f"{p}RENAME_TO must be one repository name, a different one")
        ci_raw = raw.get(p + "CI") or "0"
        if ci_raw not in ("0", "1"):
            raise LiveConfigError(f"{p}CI must be 1 or 0")
        hosts[kind] = HostCfg(
            kind=kind,
            host=host,
            path=path,
            token=token_value,
            token2=token2,
            base_branch=base,
            branch_prefix=prefix,
            api_prefix=api_prefix,
            rename_to=rename,
            ci=kind == "gitlab" and ci_raw == "1",
        )
    extra = raw.get(f"{PREFIX}FORGECTL_EXTRA_CONFIG")
    results = raw.get(f"{PREFIX}RESULTS_FILE")
    for var, value in (
        (f"{PREFIX}FORGECTL_EXTRA_CONFIG", extra),
        (f"{PREFIX}RESULTS_FILE", results),
    ):
        if value and not Path(value).is_absolute():
            raise LiveConfigError(f"{var} must be an absolute path")
    if extra and not Path(extra).is_file():
        raise LiveConfigError(f"{PREFIX}FORGECTL_EXTRA_CONFIG must be an existing file")
    if results:
        target = Path(results)
        parent = target.parent
        if (
            not parent.is_dir()
            or not os.access(parent, os.W_OK | os.X_OK)
            or target.is_symlink()
            or (target.exists() and not target.is_file())
        ):
            raise LiveConfigError(
                f"{PREFIX}RESULTS_FILE must be a regular file (not a link) in an existing "
                "writable directory"
            )
    huge = raw.get(f"{PREFIX}HUGE_PR") or None
    if huge is not None:
        huge = _huge_pr(f"{PREFIX}HUGE_PR", huge, hosts)
    return LiveEnv(
        forgectl=forgectl,
        git=git,
        gh=gh,
        glab=glab,
        hosts=hosts,
        extra_forgectl_config=Path(extra) if extra else None,
        huge_pr=huge,
        results_file=Path(results) if results else None,
        skipped=skipped,
    )


class _Missing(LiveConfigError):
    """A required variable is absent: the suite skips (a malformed one fails)."""


def is_missing(exc: LiveConfigError) -> bool:
    return isinstance(exc, _Missing)


def guard_allowance(raw: Mapping[str, str] | None) -> tuple[Path, ...]:
    """The real paths the isolation guard may let a `live` test launch: the real forgectl only.

    Empty without the opt-in, with an unusable value or when the value is a crontab; `crontab`
    is never allowed (tests/conftest.py also refuses it by name)."""
    if not raw:
        return ()
    value = raw.get(FORGECTL_VAR)
    if not value or not os.path.isabs(value):
        return ()
    real = Path(os.path.realpath(value))
    if not real.is_file() or "crontab" in (real.name, Path(value).name):
        return ()
    return (real,)
