"""YAML plus drop-ins, environment overrides and validation (docs/interface.md, Config).

Sources, lowest to highest: built-in defaults, `config.yaml`, `config.d/*.yaml` (bytewise lexical
order; maps merge, hook lists append, every other value, including `hooks.env_allow`, is
replaced by the later file), then `PR_WATCH_*` environment variables. `Config.host_settings` and
`Config.repo_settings` apply the `hosts.<h>` and `repos.<id>` entries on top of that.

Every failure is a `ConfigError` (`code` is `config_invalid`) naming the source (file or
environment variable) and the offending key, never an input value: YAML parser and pydantic
messages can quote the input, so only positions and rule descriptions are reported.

YAML is read with a SafeLoader subclass that refuses anchors, aliases (no expansion bombs),
explicit tags, non-string keys and duplicate keys, and resolves scalars by the YAML 1.2 core
schema (`yes`/`no`/`on`/`off` are strings, integers are plain decimal).

The patterns below are those of schemas/config.json, with `$` written `\\Z`: JSON Schema regexes
are ECMA-262, where `$` does not match before a trailing newline, but Python's does.
tests/test_config_schema.py keeps the model and the schema in agreement.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from .refs import (
    CMD,
    DURATION,
    HOST,
    INTERVAL,
    REPO,
    check_hook_cmd,
    duration_ms,
    ecma,
    interval_divides,
)

MAX_FILE_BYTES: Final = 256 * 1024
MAX_TOTAL_BYTES: Final = 1024 * 1024
MAX_DROP_INS: Final = 256
_MAX_ERRORS_SHOWN: Final = 5
_MAX_KEY_CHARS: Final = 80


_ecma = ecma


_DURATION: Final = DURATION
_INTERVAL: Final = INTERVAL
_RETENTION: Final = _ecma(r"^(?:[3-9][0-9]|[1-9][0-9]{2,8})d$")
_NO_CONTROLS: Final = _ecma(r"^[^\u0000-\u001f\u007f-\u009f]+$")
_CMD: Final = CMD
# Host and repo grammars belong to refs (one owner), shared with the CLI value checks.
_HOST: Final = HOST
_REPO: Final = REPO
_ENV_NAME: Final = re.compile(_ecma(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"))
_RESERVED_PREFIXES: Final = ("PR_WATCH_", "LD_", "DYLD_")
MAX_HOST_LOW_WATER: Final = 1_000_000
MAX_WARNING_CHARS: Final = 256
_SECRET_WORDS: Final = ("TOKEN", "SECRET", "KEY", "PASSWORD")


def _duration_parses(text: str) -> str:
    try:
        duration_ms(text)
    except ValueError:
        raise ValueError("duration is too large (at most 3650d)") from None
    return text


def _interval_divides(text: str) -> str:
    _duration_parses(text)
    if not interval_divides(text):
        raise ValueError("must divide 60 minutes or 24 hours (for example 5m, 15m, 1h, 6h)")
    return text


def _env_allow_name(name: str) -> str:
    if name.upper().startswith(_RESERVED_PREFIXES):
        raise ValueError("names starting with PR_WATCH_, LD_ or DYLD_ are not allowed")
    if not _ENV_NAME.fullmatch(name):
        raise ValueError("is not a valid environment variable name")
    return name


Duration = Annotated[str, StringConstraints(pattern=_DURATION), AfterValidator(_duration_parses)]
IntervalHint = Annotated[
    str, StringConstraints(pattern=_INTERVAL), AfterValidator(_interval_divides)
]
RetentionDays = Annotated[
    str, StringConstraints(pattern=_RETENTION), AfterValidator(_duration_parses)
]
HostKey = Annotated[str, StringConstraints(pattern=_HOST)]
RepoKey = Annotated[str, StringConstraints(pattern=_REPO)]
EnvAllowName = Annotated[str, AfterValidator(_env_allow_name)]
NoControls = Annotated[str, StringConstraints(pattern=_NO_CONTROLS)]


def _unique_ignoring_case(cls: Any, value: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, str] = {}
    for key in value:
        other = seen.setdefault(key.casefold(), key)
        if other != key:
            raise ValueError(
                f"{_esc(key)} and {_esc(other)} differ only in case (repo ids match "
                "case-insensitively)"
            )
    return value


def _not_null(cls: Any, value: Any) -> Any:
    if value is None:
        raise ValueError("must not be null")
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, regex_engine="python-re")


class Me(_Model):
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=_NO_CONTROLS)
    _nn = field_validator("name", mode="before")(_not_null)


class Poll(_Model):
    interval_hint: IntervalHint = "5m"
    full_every: int | None = Field(default=None, ge=1, le=10000)
    concurrency: int | None = Field(default=None, ge=1, le=64)
    host_low_water: int = Field(default=100, ge=0, le=MAX_HOST_LOW_WATER)
    overlap: Duration = "60s"
    hosts_ttl: Duration = "1h"
    max_duration: Duration = "15m"
    max_snapshots: int = Field(default=40, ge=0, le=1000)
    _nn = field_validator("full_every", "concurrency", mode="before")(_not_null)

    @field_validator("max_duration", mode="after")
    @classmethod
    def _max_duration_is_positive(cls, value: str) -> str:
        # 0 would start every snapshot stage out of time: refused, not read as "no limit"
        if duration_ms(value) < 1:
            raise ValueError("must be at least 1ms (use a long duration for no practical limit)")
        return value


class HostOverride(_Model):
    full_every: int | None = Field(default=None, ge=1, le=10000)
    concurrency: int | None = Field(default=None, ge=1, le=64)
    _nn = field_validator("full_every", "concurrency", mode="before")(_not_null)


class Retention(_Model):
    events: RetentionDays = "30d"


class Expiry(_Model):
    after_terminal: Duration = "7d"


class Failures(_Model):
    unreachable_after: int = Field(default=5, ge=1, le=1000)


class Ci(_Model):
    settle_polls: int = Field(default=1, ge=0, le=10)
    pending_max_age: Duration = "6h"


class Forgectl(_Model):
    timeout: Duration = "120s"
    max_output_mib: int = Field(default=32, ge=1, le=256)

    @field_validator("timeout", mode="after")
    @classmethod
    def _timeout_is_positive(cls, value: str) -> str:
        if duration_ms(value) < 1:
            raise ValueError("must be at least 1ms (forgectl refuses a zero timeout)")
        return value


class HookCmd(_Model):
    cmd: str = Field(min_length=1, max_length=4096, pattern=_CMD)
    timeout: int = Field(default=30, ge=1, le=600)
    async_: bool = Field(default=True, alias="async")

    @field_validator("cmd", mode="after")
    @classmethod
    def _cmd_is_a_plain_command(cls, value: str) -> str:
        if not check_hook_cmd(value):
            raise ValueError("must be a plain command (no format or separator characters)")
        return value


class Hooks(_Model):
    max_per_poll: int = Field(default=50, ge=0, le=100000)
    max_per_pr_per_poll: int = Field(default=10, ge=1, le=1000)
    concurrency: int = Field(default=4, ge=1, le=32)
    env_allow: list[EnvAllowName] = Field(default_factory=list)
    on_pushed: list[HookCmd] = Field(default_factory=list, alias="on-pushed")
    on_comment: list[HookCmd] = Field(default_factory=list, alias="on-comment")
    on_review_comment: list[HookCmd] = Field(default_factory=list, alias="on-review_comment")
    on_review: list[HookCmd] = Field(default_factory=list, alias="on-review")
    on_approved: list[HookCmd] = Field(default_factory=list, alias="on-approved")
    on_changes_requested: list[HookCmd] = Field(default_factory=list, alias="on-changes_requested")
    on_ci: list[HookCmd] = Field(default_factory=list, alias="on-ci")
    on_ready: list[HookCmd] = Field(default_factory=list, alias="on-ready")
    on_merged: list[HookCmd] = Field(default_factory=list, alias="on-merged")
    on_closed: list[HookCmd] = Field(default_factory=list, alias="on-closed")
    post_poll: list[HookCmd] = Field(default_factory=list, alias="post-poll")


HOOK_NAMES: Final = frozenset(f.alias for f in Hooks.model_fields.values() if f.alias is not None)


class Cron(_Model):
    env_file: str | None = Field(default=None, min_length=1, max_length=4096, pattern=_NO_CONTROLS)


class RepoCi(_Model):
    settle_polls: int | None = Field(default=None, ge=0, le=10)
    _nn = field_validator("settle_polls", mode="before")(_not_null)


class RepoFailures(_Model):
    unreachable_after: int | None = Field(default=None, ge=1, le=1000)
    _nn = field_validator("unreachable_after", mode="before")(_not_null)


class RepoOverride(_Model):
    detect_force_push: bool | None = None
    ci: RepoCi = Field(default_factory=RepoCi)
    failures: RepoFailures = Field(default_factory=RepoFailures)
    _nn = field_validator("detect_force_push", mode="before")(_not_null)


@dataclass(frozen=True)
class HostSettings:
    full_every: int
    concurrency: int


@dataclass(frozen=True)
class RepoSettings:
    detect_force_push: bool
    settle_polls: int
    unreachable_after: int


_BUILTIN_HOST: Final = {"github": (6, 4), "gitlab": (12, 2)}  # (full_every, concurrency)


class Config(_Model):
    me: Me = Field(default_factory=Me)
    poll: Poll = Field(default_factory=Poll)
    hosts: dict[HostKey, HostOverride] = Field(default_factory=dict)
    retention: Retention = Field(default_factory=Retention)
    expiry: Expiry = Field(default_factory=Expiry)
    failures: Failures = Field(default_factory=Failures)
    detect_force_push: bool = False
    ci: Ci = Field(default_factory=Ci)
    forgectl: Forgectl = Field(default_factory=Forgectl)
    hooks: Hooks = Field(default_factory=Hooks)
    cron: Cron = Field(default_factory=Cron)
    repos: dict[RepoKey, RepoOverride] = Field(default_factory=dict)
    _repos_unique = field_validator("repos", mode="after")(_unique_ignoring_case)

    def host_settings(self, host: str, backend: Literal["github", "gitlab"]) -> HostSettings:
        """`hosts.<host>`, then `poll` (environment over files), then the backend's built-in."""
        if backend not in _BUILTIN_HOST:
            raise ValueError(f"unknown backend: {backend!r}")
        built_full, built_conc = _BUILTIN_HOST[backend]
        entry = self.hosts.get(host.lower())
        full = _first(entry.full_every if entry else None, self.poll.full_every, built_full)
        conc = _first(entry.concurrency if entry else None, self.poll.concurrency, built_conc)
        return HostSettings(full_every=full, concurrency=conc)

    def repo_settings(self, repo_id: str) -> RepoSettings:
        """`repos.<repo_id>` (matched case-insensitively), then the global values (environment
        over files over defaults)."""
        wanted = repo_id.casefold()
        entry = next((v for k, v in self.repos.items() if k.casefold() == wanted), None)
        return RepoSettings(
            detect_force_push=_first(
                entry.detect_force_push if entry else None, self.detect_force_push
            ),
            settle_polls=_first(entry.ci.settle_polls if entry else None, self.ci.settle_polls),
            unreachable_after=_first(
                entry.failures.unreachable_after if entry else None, self.failures.unreachable_after
            ),
        )


def _first[T](*values: T | None) -> T:
    for value in values:
        if value is not None:
            return value
    raise ValueError("no value")


# --- errors ------------------------------------------------------------------------------------


def _esc(text: str, limit: int = _MAX_KEY_CHARS) -> str:
    """`text` with control and non-ASCII characters escaped and a length cap (terminal safety)."""
    out = text.encode("unicode_escape").decode("ascii")
    return out if len(out) <= limit else out[:limit] + "..."


class ConfigError(Exception):
    """Invalid config (`code` is `config_invalid`, exit 2). Names `source` (a file or an
    environment variable) and `key` (dotted), never a value. `more` holds further problems."""

    code = "config_invalid"

    def __init__(
        self,
        source: str | None,
        key: str | None,
        message: str,
        more: tuple[str, ...] = (),
    ) -> None:
        self.source = source
        self.key = key
        self.message = message
        self.more = more
        head = ": ".join(p for p in (source, key, message) if p)
        super().__init__("; ".join((head, *more)))


@dataclass(frozen=True)
class ConfigWarning:
    """A `config_warning` log record's content (schemas/log.json: `message` and `key` are
    strings of at most 256 characters; `message` is cut to fit)."""

    code: str
    message: str
    key: str

    def __post_init__(self) -> None:
        if len(self.message) > MAX_WARNING_CHARS:
            object.__setattr__(self, "message", self.message[: MAX_WARNING_CHARS - 3] + "...")


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    warnings: tuple[ConfigWarning, ...]
    files: tuple[Path, ...]


def _name(path: Path | str) -> str:
    return _esc(str(path), 1024)


# --- YAML --------------------------------------------------------------------------------------


def _at(mark: Any) -> str:
    return f"line {mark.line + 1}, column {mark.column + 1}"


class _Rejected(Exception):
    """A refusal whose message carries its position and never an input value."""


class _StrictLoader(yaml.SafeLoader):
    yaml_implicit_resolvers: dict[Any, Any] = {}  # noqa: RUF012 (PyYAML's class-level table)

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        where = _at(event.start_mark)
        if isinstance(event, yaml.AliasEvent):
            raise _Rejected(f"aliases are not allowed ({where})")
        if getattr(event, "anchor", None) is not None:
            raise _Rejected(f"anchors are not allowed ({where})")
        if getattr(event, "tag", None) is not None:
            raise _Rejected(f"YAML tags are not allowed ({where})")
        return super().compose_node(parent, index)

    def construct_object(self, node: Any, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except (ValueError, OverflowError):
            raise _Rejected(f"invalid value ({_at(node.start_mark)})") from None

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise _Rejected(f"mapping keys must be strings ({_at(key_node.start_mark)})")
            if key in seen:
                raise _Rejected(f"duplicate key ({_at(key_node.start_mark)})")
            seen.add(key)
        return super().construct_mapping(node, deep)


# YAML 1.2 core schema, narrowed: decimal integers (-?0 or no leading zero, at most 18 digits;
# longer digit strings are not integers, so the model refuses them naming the key) without `_`;
# no timestamps, no merge key, no `=` value tag, no yes/no/on/off.
for _tag, _pattern, _first_chars in (
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)\Z", "tTfF"),
    ("int", r"^-?(?:0|[1-9][0-9]{0,17})\Z", "-0123456789"),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\Z",
        "-+0123456789.",
    ),
    ("null", r"^(?:~|null|Null|NULL|)\Z", ["~", "n", "N", ""]),
):
    _StrictLoader.add_implicit_resolver(
        f"tag:yaml.org,2002:{_tag}", re.compile(_pattern), list(_first_chars)
    )


def _euid() -> int:
    return os.geteuid()


def _check_owner_and_mode(info: os.stat_result, path: Path | str) -> None:
    if info.st_uid != _euid():
        raise ConfigError(_name(path), None, "owned by another user; refusing to load it")
    if info.st_mode & 0o022:
        raise ConfigError(
            _name(path), None, "writable by group or others; refusing to load it (chmod go-w)"
        )


def _read_file(path: Path) -> bytes:
    """The bytes of a regular, owned, not group/world-writable file (symlinks are followed and
    the target is checked), read through one descriptor so the checks apply to what is read."""
    name = _name(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise ConfigError(name, None, "not found") from None
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    except ValueError:
        raise ConfigError(name, None, "is not a valid path") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(name, None, "not a regular file")
        _check_owner_and_mode(info, path)
        if info.st_size > MAX_FILE_BYTES:
            raise ConfigError(name, None, f"larger than {MAX_FILE_BYTES} bytes")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ConfigError(name, None, f"larger than {MAX_FILE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    finally:
        os.close(fd)


def _parse(data: bytes, name: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(name, None, "not valid UTF-8") from None
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 (a SafeLoader subclass)
    except _Rejected as exc:
        raise ConfigError(name, None, str(exc)) from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" ({_at(mark)})" if mark else ""
        raise ConfigError(name, None, f"invalid YAML{where}") from None
    except RecursionError:
        raise ConfigError(name, None, "invalid YAML (nested too deeply)") from None
    except Exception:  # noqa: BLE001 (any parser failure; its text may quote the input)
        raise ConfigError(name, None, "invalid YAML") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigError(name, None, "the top level must be a mapping")
    return document


# --- merge and validation ----------------------------------------------------------------------


def _merge(
    base: dict[str, Any], over: dict[str, Any], path: tuple[str, ...] = ()
) -> dict[str, Any]:
    """`over` on top of `base`: maps merge, hook lists append, anything else is replaced."""
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _merge(current, value, (*path, key))
        elif (
            path == ("hooks",)
            and key in HOOK_NAMES
            and isinstance(current, list)
            and isinstance(value, list)
        ):
            out[key] = current + value
        else:
            out[key] = value
    return out


_DURATION_KEYS: Final = frozenset(
    {
        "poll.overlap", "poll.hosts_ttl", "poll.max_duration", "expiry.after_terminal",
        "ci.pending_max_age", "forgectl.timeout",
    }
)  # fmt: skip
_HINTS: Final = {
    "retention.events": "whole days, at least 30d (for example 30d)",
    "poll.interval_hint": "Nm with N dividing 60 or Nh with N dividing 24 (for example 5m, 1h)",
}


def _dotted(loc: tuple[Any, ...]) -> str:
    parts = [_esc(str(p)) for p in loc if p != "[key]"]
    return ".".join(parts) if parts else "(top level)"


def _message(error: Any, key: str) -> str:
    kind = error["type"]
    if kind == "string_pattern_mismatch":
        hint = _HINTS.get(key) or (
            "a duration such as 500ms, 30s, 5m, 2h or 7d" if key in _DURATION_KEYS else None
        )
        return "has an invalid format" + (f" ({hint})" if hint else "")
    message = str(error["msg"])
    return message.removeprefix("Value error, ")


def _validate(
    document: dict[str, Any], source: str, env_paths: Mapping[str, str] | None = None
) -> Config:
    try:
        return Config.model_validate(document)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
    problems: list[tuple[str, str, str]] = []  # (source, key, message)
    for error in errors:
        key = _dotted(error["loc"])
        where = (
            f"environment variable {env_paths[key]}" if env_paths and key in env_paths else source
        )
        problems.append((where, key, _message(error, key)))
    first, rest = problems[0], problems[1:]
    lines = [
        (f"{s}: " if s != first[0] else "") + f"{k}: {m}"
        for s, k, m in rest[: _MAX_ERRORS_SHOWN - 1]
    ]
    if len(rest) > _MAX_ERRORS_SHOWN - 1:
        lines.append(f"and {len(rest) - (_MAX_ERRORS_SHOWN - 1)} more")
    raise ConfigError(first[0], first[1], first[2], tuple(lines))


# --- environment -------------------------------------------------------------------------------

ENV_KEYS: Final[dict[str, str]] = {
    "poll.interval_hint": "str",
    "poll.full_every": "int",
    "poll.concurrency": "int",
    "poll.host_low_water": "int",
    "poll.overlap": "str",
    "poll.hosts_ttl": "str",
    "poll.max_duration": "str",
    "poll.max_snapshots": "int",
    "retention.events": "str",
    "expiry.after_terminal": "str",
    "failures.unreachable_after": "int",
    "detect_force_push": "bool",
    "ci.settle_polls": "int",
    "ci.pending_max_age": "str",
    "forgectl.timeout": "str",
    "forgectl.max_output_mib": "int",
    "hooks.max_per_poll": "int",
    "hooks.max_per_pr_per_poll": "int",
    "hooks.concurrency": "int",
    "me.name": "str",
}
_ENV_PREFIX: Final = "PR_WATCH_"
_ENV_BY_NAME: Final = {p.upper().replace(".", "__"): p for p in ENV_KEYS}
_HOOK_ONLY_VARS: Final = frozenset({"HOOK", "ID", "EVENT_ID", "OBJECT_TYPE"})
_NOT_FROM_ENV: Final = frozenset({"CRON", "HOSTS", "REPOS", "HOOKS"})
_INT: Final = re.compile(r"-?(?:0|[1-9][0-9]{0,17})")


def _env_value(var: str, path: str, raw: str) -> Any:
    kind = ENV_KEYS[path]
    source = f"environment variable {var}"
    if kind == "bool":
        if raw not in ("true", "false"):
            raise ConfigError(source, path, "must be true or false")
        return raw == "true"
    if kind == "int":
        if not _INT.fullmatch(raw):
            raise ConfigError(
                source,
                path,
                "must be a plain decimal integer of at most 18 digits (no leading zeros)",
            )
        return int(raw)
    return raw


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    node = document
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = value


def _env_overlay(
    environ: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, str], list[ConfigWarning]]:
    overlay: dict[str, Any] = {}
    origin: dict[str, str] = {}
    warnings: list[ConfigWarning] = []
    for var in sorted(environ):
        if not var.startswith(_ENV_PREFIX):
            continue
        rest = var[len(_ENV_PREFIX) :]
        if rest in _HOOK_ONLY_VARS:
            continue
        path = _ENV_BY_NAME.get(rest)
        if path is None:
            if rest.split("__", 1)[0] in _NOT_FROM_ENV:
                why = "cannot be set from the environment (file-only)"
            else:
                why = "is not a config variable"
            warnings.append(
                ConfigWarning("config_warning", f"{_esc(var)} {why}; ignored", _esc(var, 253))
            )
            continue
        _set_path(overlay, path, _env_value(var, path, environ[var]))
        origin[path] = var
    return overlay, origin, warnings


def _env_allow_warnings(config: Config) -> list[ConfigWarning]:
    out = []
    for name in config.hooks.env_allow:
        if any(word in name.upper() for word in _SECRET_WORDS):
            out.append(
                ConfigWarning(
                    "config_warning",
                    f"hooks.env_allow: {name} looks like a credential name; every hook receives it",
                    "hooks.env_allow",
                )
            )
    return out


# --- loading -----------------------------------------------------------------------------------


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """`$XDG_CONFIG_HOME/pr-watch/config.yaml` (only when absolute), else `~/.config/...`."""
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME", "")
    if xdg.startswith("/"):
        base = Path(xdg)
    else:
        home = env.get("HOME") or os.path.expanduser("~")
        base = Path(home) / ".config"
    return base / "pr-watch" / "config.yaml"


def _dropin_paths(directory: Path) -> list[Path]:
    name = _name(directory)
    try:
        info = os.stat(directory)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    except ValueError:
        raise ConfigError(name, None, "is not a valid path") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError(name, None, "not a directory")
    _check_owner_and_mode(info, directory)
    try:
        entries = sorted(os.listdir(directory), key=os.fsencode)
    except OSError as exc:
        raise ConfigError(name, None, f"cannot be read ({exc.strerror})") from None
    names = [n for n in entries if n.endswith(".yaml") and not n.startswith(".")]
    if len(names) > MAX_DROP_INS:
        raise ConfigError(name, None, f"more than {MAX_DROP_INS} drop-in files")
    return [directory / n for n in names]


def load_config(
    explicit: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> LoadedConfig:
    """Load, merge and validate the config.

    `explicit` is the `--config` file: it must exist and replaces `config.yaml`; drop-ins are read
    from the `config.d` beside it. Without it a missing default file means built-in defaults.
    Each file is validated alone (so an error names its file), then the merge plus the
    environment as a whole.
    """
    env = os.environ if environ is None else environ
    path = Path(os.fspath(explicit)) if explicit is not None else default_config_path(env)
    files: list[Path] = []
    if explicit is not None or os.path.lexists(path):
        files.append(path)
    files.extend(_dropin_paths(path.parent / "config.d"))
    merged: dict[str, Any] = {}
    total = 0
    for file in files:
        data = _read_file(file)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ConfigError(
                _name(file),
                None,
                f"the config files together are larger than {MAX_TOTAL_BYTES} bytes",
            )
        document = _parse(data, _name(file))
        _validate(document, _name(file))
        merged = _merge(merged, document)
    overlay, origin, warnings = _env_overlay(env)
    merged = _merge(merged, overlay)
    if len(files) == 1:
        label = _name(files[0])
    else:
        label = "merged config" if files else "environment"
    config = _validate(merged, label, origin)
    warnings.extend(_env_allow_warnings(config))
    return LoadedConfig(config=config, warnings=tuple(warnings), files=tuple(files))
