# pr-watch: design

Status: designed, 2026-11-10 (interface 0.1.0-design). The rules are in [interface.md](interface.md), the single normative source; this document records the decisions, the structure and the test strategy and links to the headings there instead of restating them.

## Role and layer

Layer 2 (composite). Watches PRs for changes and exposes them as events and hooks. Works fully standalone (architecture C2): it needs forgectl and nothing else of ours. Consumers (review-respond, ticket-work, review-watch, the harness) are unknown to it; behavior differs only by explicit arguments (`--owner`, `--label`).

## Uses

| Tool | Binding | What for |
|---|---|---|
| forgectl | A (hard) | `prs`, `snapshot`, `show`, `compare` (read-only, `--json`), `whoami` (host discovery); minimum version declared in `forgectl.min_version` |

Third parties: none directly (git-host access is forgectl's). Dependencies of the package itself: `pyyaml` (the stdlib cannot read YAML) and `pydantic` (config validation), as the design guide prescribes; nothing else beyond the standard library (`argparse`, `sqlite3`, `subprocess`, `concurrent.futures`).

## Decisions

| ID | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | `poll` is the only command that contacts a host; cron or the harness runs it; default hint 5 minutes | Consumers read `events` offline; one network path is easy to budget and to lock | [Poll cycle](interface.md#poll-cycle) |
| D-2 | Detection is snapshot diffing against a compact digest; a cheap `prs --updated-since` trigger, pending-CI re-snapshots and a periodic full pass choose what to snapshot | Spikes: `updated_at` misses CI completion everywhere and GitLab approvals; snapshot-all costs 5 to 22 requests per PR | [What is polled](interface.md#what-is-polled), [Digest](interface.md#digest) |
| D-3 | Events and hook payloads carry metadata only, never host free text | Untrusted text must not reach state, notifications or logs; consumers fetch text through forgectl by id | [Untrusted text](interface.md#untrusted-text), [Events](interface.md#events) |
| D-4 | Cursor `v1.<store_id>.<seq>`; a changed store id or an old or future seq is `gap: true` | Detects both retention loss and a reset database without per-consumer state | [`events`](interface.md#events-1) |
| D-5 | Watches are keyed (pr_id or canonical query), have owners and labels; query watches have members baselined silently | Reference counting by owner is in the architecture; members need a digest to diff | [Watches, owners and members](interface.md#watches-owners-and-members) |
| D-6 | Exact event definitions: per-reviewer approvals, `ci` debounced through `none`, `ready` is draft to ready only, `pushed` keys on the head sha | forgectl flaps right after a push (checks not registered, mergeability unknown); `pushed_at` can be minutes early | [Events](interface.md#events) |
| D-7 | Hooks are best-effort, at-most-once, async by default, capped per poll; events are committed first | A notification is not a guarantee; the cursor is | [Hooks](interface.md#hooks) |
| D-8 | Command set: `poll`, `watch add|remove`, `list`, `show`, `events`, `label`, `prune`, `reconcile`, `cron`, `export`; no TUI in 0.x | The consumers are tools; the harness dashboard shows events | [Commands](interface.md#commands) |
| D-9 | forgectl is binding A; per-host pause and per-PR failure counters; a failure never yields an event | forgectl retries itself; `not_found` means unknown | [Failures](interface.md#failures), [Pacing and backoff](interface.md#pacing-and-backoff) |
| D-10 | Query scope by repeatable `--host` and `--repo` filters; enumeration per host; config keys with per-host and per-repo overrides | One shared `--limit` across hosts cut one host's list (spike B) | [Config](interface.md#config) |
| D-11 | `watch add` baselines synchronously (`show` then `snapshot`); query form may skip with `--no-baseline` | A typo should fail at add time | [`watch add`](interface.md#watch-add) |
| D-12 | No audit log; `export` prints events | pr-watch takes no external action | [Audit and log](interface.md#audit-and-log) |

## Resolved open topics

- **Polling cadence:** a 5 minute hint, per-host full-pass interval, pending-CI PRs every cycle (D-1, D-2; [Pacing and backoff](interface.md#pacing-and-backoff)).
- **Change detection:** digest diff (D-2).
- **Hook payloads:** the event plus the standard keys (D-3, D-7).
- **Cursor encoding:** D-4.

The seeded text in this repo's older README and design said "binding TBD"; it is now A. The architecture's section 5.2 and 20 entries are updated in architecture v1.17.

## Live verification

| Behavior | Source | Result | Regression |
|---|---|---|---|
| A snapshot costs 5 to 8 requests on GitHub, 19 to 22 on GitLab | spikes A, B | measured | `live` cost budget test |
| `updated_at` is blind to CI completion and thread resolution (GitHub), approvals, CI and bot edits (GitLab) | spikes A, B | measured on history; transitions observed on GitHub only | `live` trigger test; GitLab transition shapes **still to verify** (conservative default: GitHub shapes) |
| Unchanged PRs give byte-identical snapshots after dropping `fetched_at` and `rate_limit` | spikes A, B | 8 of 9 and 6 of 6 | golden-file determinism test |
| `compare` returns `diverged` for a force push, `ahead` for a normal one | spike A, B | confirmed | `live` force-push test |
| forgectl failure classes, `retry_after`, `prs` partial results (exit 12 with an object), SIGTERM exit 143, `--version` plain text | spike C | measured on the installed forgectl | process tests with fakes |
| GitLab reports no rate-limit headers (`rate_limit` null) | spike B | confirmed | unit test of the low-water rule |
| `approvals_at_head` is null on the tested GitLab server | spike B | confirmed | unit test (null means unknown) |

## Modules

| Module | Responsibility |
|---|---|
| `gateway` | every subprocess call (forgectl, hooks, cron); fakes plug in here |
| `forgectl_client` | argv building, `--json` parsing, version check, error classification |
| `store` | SQLite access, migrations, leases, event append in one transaction |
| `watches` | watch keys, owners, labels, members, expiry |
| `digest` | building a digest from a snapshot; the `seen` sets |
| `diff` | digest pair to ordered events (all rules in [Events](interface.md#events)) |
| `poller` | enumeration, snapshot selection, pools, pacing, host pause, report |
| `hooks` | payloads, process groups, timeouts, the per-poll cap |
| `config` | YAML plus drop-ins, env, validation |
| `cli` | argparse, output, exit codes, error envelope |
| `cron` | the tagged crontab block |

## Data model

SQLite (WAL), additive migrations: `meta`, `watches` (key, kind, query spec), `owners`, `labels`, `prs` (`pr_id`, canonical `pr`, repo, status, digest JSON, schedule, failure count, `terminal_at`), `sources` (PR to watch), `events` (`seq` autoincrement, id, type, `pr_id`, payload JSON, times, labels), `leases`. A digest is bounded: the `seen` sets grow with comment count only and are dropped with the row.

## Concurrency

Per-PR and per-query lease locks in `state.db`; a second `poll` skips locked objects and exits 0 `already_running` when nothing could be locked. Event append uses `BEGIN IMMEDIATE` and assigns `seq` inside the transaction. Snapshot workers are a bounded thread pool per host calling forgectl subprocesses; the digest and events of one PR are committed together by the single coordinator thread. Hooks run in their own process groups.

## Error handling

Classification and its consequences are in [Failures](interface.md#failures). A failed snapshot never changes a digest; a failed commit leaves the previous digest, so the next cycle repeats the diff (events are never duplicated because they are stored in the same transaction as the digest).

## Test strategy

- Every forgectl call goes through the gateway; `tests/fakebin/forgectl` is a fake driven by recorded and synthetic snapshots (private data removed), including failure scripts for each class in [Failures](interface.md#failures).
- forgectl's `snapshot`, `prs`, `show`, `compare` and `whoami` schemas are copied into `tests/` pinned to a forgectl version; fakes are validated against them.
- Golden files for every `--json` output; schema validation of real output and of hook payloads.
- Process tests: kill during commit (no loss, no duplicate), two concurrent polls, hook timeout and group kill, the per-poll hook cap, a killed forgectl child.
- Diff tests per event rule, including the baseline, the CI hold, `incomplete` parts, GitLab (no `review`, null `approvals_at_head`), reopen, and rename by `pr_id`.
- `live` tests (never in CI): the spike regressions in the table above.
- Platforms: Linux and macOS; CI is Linux only.
