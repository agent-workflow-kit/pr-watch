"""Hook payloads, the bounded in-process pool, caps, process groups, timeouts (docs/interface.md:
Hooks).

Hooks are post-hooks: they report a change that already happened and cannot veto anything. The
poller queues a PR's `on-<event>` hooks after that PR's commit transaction
(`HookRunner.submit_events` is the `on_events` callback of the snapshot pool) and `post-poll`
after the report is built; a pool of `hooks.concurrency` worker threads runs them while
snapshots continue, and `finish()` waits for the pool to drain.

Everything that touches a process goes through `gateway.run`, which owns the process group (own
session, SIGTERM then SIGKILL only while the leader is unreaped) and the shutdown contract (a
poll's stop signal SIGTERMs every live group, `SHUTDOWN_KILL_AFTER` later SIGKILLs it, and refuses
new launches). This module adds what is hook specific:

- argv is `["/bin/sh", "-c", cmd]` where `cmd` is the user's trusted configuration (or `--hook`)
  string, never built from host text. The payload (the only place untrusted text appears) goes to
  stdin; stdout and stderr go to /dev/null; cwd is `<state dir>/hooks-cwd`.
- The environment is built from scratch (`hook_environment`): PATH (absolute entries only), HOME
  (absolute, else the passwd home),
  LANG, TERM, XDG_*, the `PR_WATCH_*` identifiers and the names in `hooks.env_allow`.
- Delivery is at-most-once: the queue lives in memory, so a poll that is signalled or killed loses
  what was not started (the events are already committed; consumers read `events`). A signalled
  poll starts no further hook.

Judgment calls where the interface is silent (kept conservative):

- `hooks.max_per_poll: 0` means no hook runs at all, the exempt ones (`on-merged`, `on-closed`,
  `post-poll`) included: they are skipped and counted, with a `hook_cap` record.
- The caps count accepted invocations when they are queued, in queue order; an accepted one the
  drain bound later skips still used its slot. The `hook_cap` record for a cap and the one for the
  drain bound are separate records (one each per round).
- A hook killed by a signal is logged with `exit_code` 128 plus the signal number (the shell
  convention); `null` is only for a hook that never started or was abandoned.
- `hooks-cwd` is created or verified once, on the first hook; when that fails every hook of the
  poll fails (counted, logged with `exit_code: null`) and the poll goes on.
- Only the leader is bounded. Any descendant that outlives the leader is neither bounded nor
  killed, in the hook's group (a plain `cmd &`: the group is signalled only at a timeout or a
  shutdown, never after a normal exit) or out of it (`setsid`, a double fork), and pr-watch does
  not wait for it (it does not wait for pipe EOF; stdout and stderr are /dev/null anyway). This
  is a documented limit (Residual risks).
"""

from __future__ import annotations

import bisect
import json
import os
import pwd
import re
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from . import gateway
from .config import Config
from .gateway import LaunchError
from .log import NULL_LOG, PATTERNS, Log
from .refs import duration_ms
from .state_dir import StateDir, StateDirError, ensure_hooks_cwd
from .store import StoredEvent

TOOL: Final = "pr-watch"
SHELL: Final = "/bin/sh"
TERM_GRACE: Final = 5.0  # seconds between a timeout's SIGTERM and its SIGKILL
ABANDON_AFTER: Final = 5.0  # seconds to wait for the leader after SIGKILL, then abandon the slot
DRAIN_EXTRA: Final = 120.0  # seconds past `poll.max_duration` after which no capped hook starts
DEFAULT_TIMEOUT: Final = 30
EXEMPT: Final = frozenset({"on-merged", "on-closed", "post-poll"})  # never counted, never capped
_IDLE_WAIT: Final = 0.05  # a worker re-checks the stop flag this often (a handler cannot notify)
_HOST_RE: Final = re.compile(PATTERNS["host"])
_CODE_RE: Final = re.compile(PATTERNS["code"])
_WHOAMI_REF: Final = "whoami"  # `failed[].ref` of a `whoami` call that names no host
_ENV_NAME_OK: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_DEFAULT_PATH: Final = "/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class HookSpec:
    name: str  # `on-<event>` or `post-poll`
    cmd: str
    timeout: int = DEFAULT_TIMEOUT
    is_async: bool = True


@dataclass
class HookCounts:
    run: int = 0
    skipped: int = 0
    failed: int = 0

    def as_report(self) -> dict[str, int]:
        return {"run": self.run, "skipped": self.skipped, "failed": self.failed}


def collect_specs(
    config: Config, extra: Sequence[tuple[str, str]] = (), *, no_hooks: bool = False
) -> dict[str, tuple[HookSpec, ...]]:
    """The hooks of a poll by name: the configured ones in config order, then each `--hook`
    (timeout 30, async). `--no-hooks` leaves nothing, `--hook` included."""
    if no_hooks:
        return {}
    found: dict[str, list[HookSpec]] = {}
    for field_ in type(config.hooks).model_fields.values():
        alias = field_.alias
        if alias is None:
            continue
        for item in getattr(config.hooks, _attr(config, alias)):
            found.setdefault(alias, []).append(HookSpec(alias, item.cmd, item.timeout, item.async_))
    for name, cmd in extra:
        found.setdefault(name, []).append(HookSpec(name, cmd))
    return {name: tuple(specs) for name, specs in found.items()}


def _attr(config: Config, alias: str) -> str:
    return alias.replace("-", "_")


# --- environment and payloads ------------------------------------------------------------------


def _absolute_path(value: str) -> str:
    kept = [p for p in value.split(os.pathsep) if p and os.path.isabs(p)]
    return os.pathsep.join(kept) if kept else _DEFAULT_PATH


def _passwd_home() -> str:
    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except (KeyError, OSError):
        return ""
    return home if os.path.isabs(home) else ""


def hook_environment(
    hook: str,
    object_id: str,
    event_id: str | None,
    object_type: str,
    *,
    environ: Mapping[str, str],
    env_allow: Sequence[str] = (),
) -> dict[str, str]:
    """The whole environment of one hook: nothing is inherited. Host text never appears here:
    `object_id` is a `pr_id` or a timestamp, `event_id` an event id."""
    env: dict[str, str] = {}
    for name in ("LANG", "TERM"):
        if name in environ:
            env[name] = environ[name]
    home = environ.get("HOME", "")
    if not os.path.isabs(home):
        home = _passwd_home()  # unset or relative: the account's home, never a cwd-relative path
    if home:
        env["HOME"] = home
    for name, value in environ.items():
        if name.startswith("XDG_") and os.path.isabs(value):
            env[name] = value
    for name in env_allow:
        # the config refuses reserved prefixes; the runner never lets an allowed name replace one
        if name in environ and name and set(name) <= _ENV_NAME_OK:
            env[name] = environ[name]
    env["PATH"] = _absolute_path(env.get("PATH", environ.get("PATH", "")))
    env["PR_WATCH_HOOK"] = hook
    env["PR_WATCH_ID"] = object_id
    env["PR_WATCH_EVENT_ID"] = "" if event_id is None else event_id
    env["PR_WATCH_OBJECT_TYPE"] = object_type
    return env


def event_payload(event: StoredEvent) -> dict[str, Any]:
    """The `on-<event>` payload: the event exactly as `events` prints it plus the hook fields."""
    return {
        **event.to_json(),
        "tool": TOOL,
        "hook": f"on-{event.type}",
        "object_type": "pr",
        "object_id": event.pr_id,
        "event_id": event.id,
    }


def encode_payload(payload: Mapping[str, Any]) -> bytes:
    """One JSON line (ASCII: a lone surrogate is escaped, never an encoding error)."""
    return (json.dumps(payload) + "\n").encode("ascii")


def should_post_poll(
    report: Mapping[str, Any], unreachable_at_start: set[str], newly_unreachable: Sequence[Any]
) -> bool:
    """`post-poll` fires when `failed` names a scope, a host or a PR that was not already
    unreachable at the start of the cycle, a host has listing or whole-host errors, or a PR became
    unreachable. An already unreachable PR that fails again is never alone a cause, and neither is
    a PR failing `too_large` (it is retried once per pass; it fires when it becomes unreachable)."""
    if newly_unreachable:
        return True
    if any(h["errors"] for h in report["hosts"].values()):
        return True
    return any(
        not (f["kind"] == "pr" and (f["ref"] in unreachable_at_start or f["code"] == "too_large"))
        for f in report["failed"]
    )


def post_poll_payload(
    report: Mapping[str, Any], newly_unreachable: Sequence[tuple[str, str, str]]
) -> dict[str, Any]:
    """The `post-poll` payload: the report's counts (the hook counts exclude `post-poll` itself,
    so build this before it runs), per-host error codes and the PRs that became unreachable. No
    host text: PR references and ids are identifiers."""
    by_host: dict[str, list[str]] = {}
    for host, entry in report["hosts"].items():
        by_host.setdefault(host, []).extend(e["code"] for e in entry["errors"])
    for item in report["failed"]:  # a failed host may have no `hosts` entry
        ref, code = item["ref"], item["code"]
        if (
            item["kind"] == "host" and ref != _WHOAMI_REF and _HOST_RE.fullmatch(ref)
            and _CODE_RE.fullmatch(code)
        ):  # fmt: skip
            by_host.setdefault(ref, []).append(code)
    errors = [
        {"host": host, "codes": list(dict.fromkeys(codes))}
        for host, codes in by_host.items()
        if codes
    ]
    return {
        "tool": TOOL,
        "hook": "post-poll",
        "object_type": "poll",
        "object_id": report["started_at"],
        "started_at": report["started_at"],
        "duration_ms": report["duration_ms"],
        "full_pass": report["full_pass"],
        "prs": dict(report["prs"]) | {"incomplete": report["prs"]["incomplete"]},
        "events": dict(report["events"]),
        "hooks": dict(report["hooks"]),
        "errors": errors,
        "degraded": report["prs"]["incomplete"],
        "unreachable": [
            {"pr": pr, "pr_id": pr_id, "code": code} for pr, pr_id, code in newly_unreachable[:50]
        ],
    }


# --- the pool ----------------------------------------------------------------------------------


@dataclass
class _Job:
    spec: HookSpec
    stdin: bytes
    object_id: str
    event_id: str | None
    object_type: str
    order: tuple[int, int]  # (0 exempt / 1 other, queue counter): exempt hooks first, then FIFO


@dataclass
class _Skips:
    cap: list[str | None] = field(default_factory=list)  # event ids of capped invocations
    drain: list[str | None] = field(default_factory=list)
    encode: list[str | None] = field(default_factory=list)  # payload could not be encoded


class HookRunner:
    """Queues hooks and runs them on `concurrency` worker threads.

    `started` and the clock are `time.monotonic` readings of the poll's start, for the drain bound.
    `hooks_cwd` returns the verified working directory or raises `StateDirError`.
    `should_stop` is the poll's stop flag: once true nothing more is started and the queue is
    dropped (running hooks have been signalled by the gateway).
    """

    def __init__(
        self,
        specs: Mapping[str, Sequence[HookSpec]],
        *,
        max_per_poll: int,
        max_per_pr: int,
        concurrency: int,
        hooks_cwd: Callable[[], Path],
        log: Log = NULL_LOG,
        started: float | None = None,
        drain_after: float = 3600.0,
        should_stop: Callable[[], bool] | None = None,
        environ: Mapping[str, str] | None = None,
        env_allow: Sequence[str] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.specs = {name: tuple(items) for name, items in specs.items() if items}
        self.max_per_poll, self.max_per_pr, self.concurrency = max_per_poll, max_per_pr, concurrency
        self.counts = HookCounts()
        self._hooks_cwd = hooks_cwd
        self._log = log
        self._clock = clock
        self._started = clock() if started is None else started
        self._drain_after = drain_after
        self._should_stop = should_stop or (lambda: False)
        self._environ = dict(os.environ if environ is None else environ)
        self._env_allow = tuple(env_allow)
        self._cond = threading.Condition()
        self._queue: list[_Job] = []
        self._threads: list[threading.Thread] = []
        self._counter = 0
        self._accepted = 0
        self._per_pr: Counter[str] = Counter()
        self._sync_busy = False
        self._running = 0
        self._closing = False
        self._skips = _Skips()
        self._cwd_lock = threading.Lock()
        self._cwd: tuple[Path | None, str | None] | None = None

    @classmethod
    def from_config(
        cls,
        config: Config,
        state: StateDir | None,
        *,
        log: Log,
        started: float,
        should_stop: Callable[[], bool],
        extra: Sequence[tuple[str, str]] = (),
        no_hooks: bool = False,
    ) -> HookRunner:
        def cwd() -> Path:
            if state is None:
                raise StateDirError("no state directory")
            return ensure_hooks_cwd(state)

        hooks = config.hooks
        return cls(
            collect_specs(config, extra, no_hooks=no_hooks),
            max_per_poll=hooks.max_per_poll, max_per_pr=hooks.max_per_pr_per_poll,
            concurrency=hooks.concurrency, hooks_cwd=cwd, log=log, started=started,
            drain_after=duration_ms(config.poll.max_duration) / 1000 + DRAIN_EXTRA,
            should_stop=should_stop, env_allow=hooks.env_allow,
        )  # fmt: skip

    @property
    def active(self) -> bool:
        return bool(self.specs)

    # -- queueing --

    def submit_events(self, events: Sequence[StoredEvent]) -> None:
        """Queue the `on-<event>` hooks of events that are committed, in event order. Never raises
        (it runs inside the snapshot pool): a payload that cannot be built skips that invocation."""
        if not self.specs or self._should_stop():
            return
        for event in events:
            for spec in self.specs.get(f"on-{event.type}", ()):
                try:
                    body = encode_payload(event_payload(event))
                except (TypeError, ValueError, UnicodeError):
                    self._count_skipped(event.id)
                    continue
                self._enqueue(spec, body, event.pr_id, event.id, "pr", event.pr_id)

    def submit_post_poll(self, payload: Mapping[str, Any]) -> None:
        for spec in self.specs.get("post-poll", ()):
            self._enqueue(spec, encode_payload(payload), str(payload["object_id"]), None,
                          "poll", None)  # fmt: skip

    def _count_skipped(self, event_id: str | None) -> None:
        with self._cond:
            self.counts.skipped += 1
            self._skips.encode.append(event_id)

    def _enqueue(
        self,
        spec: HookSpec,
        body: bytes,
        object_id: str,
        event_id: str | None,
        object_type: str,
        pr_id: str | None,
    ) -> None:
        exempt = spec.name in EXEMPT
        with self._cond:
            if self.max_per_poll == 0:  # "0 means no hook runs": the exempt ones too
                self.counts.skipped += 1
                self._skips.cap.append(event_id)
                return
            if not exempt:
                if self._accepted >= self.max_per_poll or (
                    pr_id is not None and self._per_pr[pr_id] >= self.max_per_pr
                ):
                    self.counts.skipped += 1
                    self._skips.cap.append(event_id)
                    return
                self._accepted += 1
                if pr_id is not None:
                    self._per_pr[pr_id] += 1
            self._counter += 1
            job = _Job(spec, body, object_id, event_id, object_type,
                       (0 if exempt else 1, self._counter))  # fmt: skip
            bisect.insort(self._queue, job, key=lambda j: j.order)
            self._start_workers()
            self._cond.notify_all()

    def _start_workers(self) -> None:  # caller holds the condition
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self.concurrency:
            thread = threading.Thread(target=self._work, name="pr-watch-hook", daemon=True)
            self._threads.append(thread)
            thread.start()

    # -- running --

    def finish(self) -> HookCounts:
        """Wait until every queued hook has run or been skipped (or dropped by a stop), write the
        `hook_cap` records and return the counts. Safe to call again after more was queued."""
        with self._cond:
            while True:
                self._threads = [t for t in self._threads if t.is_alive()]
                if self._should_stop():
                    self._queue.clear()
                if not self._queue and not self._busy():
                    break
                if self._queue and not self._threads:
                    self._start_workers()
                self._cond.wait(_IDLE_WAIT)
            threads, self._threads = self._threads, []
            self._closing = True  # idle workers exit
            self._cond.notify_all()
        for thread in threads:
            thread.join()
        with self._cond:
            self._closing = False
        self._flush_skips()
        return self.counts

    def _busy(self) -> bool:  # caller holds the condition
        return self._running > 0

    def _work(self) -> None:
        while True:
            job = self._take()
            if job is None:
                return
            try:
                self._execute(job)
            finally:
                with self._cond:
                    self._running -= 1
                    if not job.spec.is_async:
                        self._sync_busy = False
                    self._cond.notify_all()

    def _take(self) -> _Job | None:
        with self._cond:
            while True:
                if self._should_stop():
                    self._queue.clear()
                    self._cond.notify_all()
                    return None
                job = self._next()
                if job is not None:
                    return job
                if self._closing and not self._queue:
                    return None
                self._cond.wait(_IDLE_WAIT)

    def _next(self) -> _Job | None:  # caller holds the condition; marks the job as running
        index = 0
        while index < len(self._queue):
            job = self._queue[index]
            exempt = job.spec.name in EXEMPT
            if not exempt and self._clock() - self._started >= self._drain_after:
                del self._queue[index]
                self.counts.skipped += 1
                self._skips.drain.append(job.event_id)
                continue
            if not job.spec.is_async and self._sync_busy:
                index += 1  # one `async: false` hook at a time, in queue order
                continue
            del self._queue[index]
            self._running += 1
            if not job.spec.is_async:
                self._sync_busy = True
            self.counts.run += 1
            return job
        return None

    def _cwd_or_error(self) -> tuple[Path | None, str | None]:
        with self._cwd_lock:
            if self._cwd is None:
                try:
                    self._cwd = (self._hooks_cwd(), None)
                except (StateDirError, OSError) as exc:
                    self._cwd = (None, str(exc)[:200])
            return self._cwd

    def _execute(self, job: _Job) -> None:
        spec = job.spec
        began = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        failure: str | None = None
        by_shutdown = False  # ended by our own shutdown, not by the hook
        cwd, cwd_error = self._cwd_or_error()
        if cwd is None:
            failure = f"hooks-cwd unavailable: {cwd_error}"
        else:
            env = hook_environment(
                spec.name, job.object_id, job.event_id, job.object_type,
                environ=self._environ, env_allow=self._env_allow,
            )  # fmt: skip
            try:
                result = gateway.run(
                    [SHELL, "-c", spec.cmd], env=env, timeout=float(spec.timeout),
                    max_output=1, stdin=job.stdin, cwd=cwd, term_grace=TERM_GRACE,
                    abandon_after=ABANDON_AFTER, discard_output=True,
                )  # fmt: skip
            except (OSError, ValueError, TypeError) as exc:  # LaunchError is an OSError
                failure = f"hook could not be started: {type(exc).__name__}"
                by_shutdown = isinstance(exc, LaunchError) and "shutting down" in str(exc)
            else:
                timed_out = result.timed_out
                code = result.returncode
                exit_code = None if code is None else (128 - code if code < 0 else code)
                by_shutdown = not timed_out and (
                    result.abandoned or code in (-signal.SIGTERM, -signal.SIGKILL)
                )
        ok = failure is None and exit_code == 0 and not timed_out
        duration = int((time.monotonic() - began) * 1000)
        if self._should_stop() and not ok and by_shutdown:
            # the poll was signalled: our shutdown stopped the hook (or refused its launch); a
            # hook that failed on its own is still logged
            with self._cond:
                self.counts.failed += 1
            return
        fields: dict[str, Any] = {
            "event_id": job.event_id, "hook": spec.name, "exit_code": exit_code,
            "duration_ms": duration, "timed_out": timed_out,
        }  # fmt: skip
        if failure is not None:
            fields["message"] = failure
        self._log.write("hook_run", "info" if ok else "warn", **fields)
        if not ok:
            with self._cond:
                self.counts.failed += 1

    def _flush_skips(self) -> None:
        with self._cond:
            skips, self._skips = self._skips, _Skips()
        for ids, message in (
            (skips.cap, "hooks skipped by the cap"),
            (skips.drain, "hooks skipped by the drain bound"),
            (skips.encode, "hooks skipped: payload could not be encoded"),
        ):
            if not ids:
                continue
            fields: dict[str, Any] = {"skipped": len(ids), "message": message}
            named = [i for i in ids if i is not None]  # a skipped `post-poll` has no event id
            if named:
                fields |= {"first_event_id": named[0], "last_event_id": named[-1]}
            self._log.write("hook_cap", "warn", **fields)
