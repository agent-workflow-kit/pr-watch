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
| D-2 | Detection is snapshot diffing against a compact digest; query watches list the open set each cycle and per-PR repos use a host-time watermark; a persisted `listed_updated_at` newer than `snap_updated_at`, re-snapshots of unsettled PRs and a periodic full pass choose what to snapshot | Spikes: `updated_at` misses CI completion everywhere and GitLab approvals; snapshot-all costs 5 to 22 requests per PR | [Poll cycle](interface.md#poll-cycle), [What is polled](interface.md#what-is-polled), [Digest](interface.md#digest) |
| D-3 | Events and hook payloads carry metadata only, never host free text | Untrusted text must not reach state, notifications or logs; consumers fetch text through forgectl by id | [Untrusted text](interface.md#untrusted-text), [Events](interface.md#events) |
| D-4 | Cursor `v1.<store_id>.<seq>`; a changed store id, a seq beyond the head or one below the pruned range is `gap: true` | Detects both retention loss and a reset database without per-consumer state | [`events`](interface.md#events-1) |
| D-5 | Watches are keyed (pr_id or canonical query), have owners and labels; query watches have members baselined silently | Reference counting by owner is in the architecture; members need a digest to diff | [Watches, owners and members](interface.md#watches-owners-and-members) |
| D-6 | Exact event definitions: per-reviewer approval keyed on `(state, at, commit_sha)`, `ci` debounced through `none` (it carries counts; `passing` is not held), `ready` is draft to ready only, `pushed` keys on the head sha | forgectl flaps right after a push (checks not registered, mergeability unknown); `pushed_at` can be minutes early | [Events](interface.md#events) |
| D-7 | Hooks are best-effort, at-most-once, run in a bounded pool inside the poll, and are capped per poll and per PR; events are committed first | A notification is not a guarantee; the cursor is | [Hooks](interface.md#hooks) |
| D-8 | Command set: `poll`, `watch add|remove`, `list`, `show`, `events`, `label`, `prune`, `reconcile`, `cron`, `export`; no TUI in 0.x | The consumers are tools; the harness dashboard shows events | [Commands](interface.md#commands) |
| D-9 | forgectl is binding A; per-host pause and per-PR failure counters; a failure never yields an event; one failure table, and whole-host failures never touch PR counters | forgectl retries itself; `not_found` means unknown | [Failures](interface.md#failures), [Pacing and backoff](interface.md#pacing-and-backoff) |
| D-10 | Query scope by repeatable `--host` and `--repo` filters; enumeration per host; config keys with per-host and per-repo overrides | One shared `--limit` across hosts cut one host's list (spike B) | [Config](interface.md#config) |
| D-11 | `watch add <pr>` baselines synchronously with one `snapshot` (`--no-baseline` for the per-PR form only); a query `watch add` never baselines, its first `poll` does | A typo should fail at add time; no command that behaves differently under cron | [`watch add`](interface.md#watch-add) |
| D-12 | No audit log; `export` prints events | pr-watch takes no external action | [Audit and log](interface.md#audit-and-log) |
| D-13 | Query enumeration lists the open set each cycle; "changed" is `listed_updated_at > snap_updated_at`; a reopen is a silent re-baseline | Removes windows, clocks and the baseline hole | [Poll cycle](interface.md#poll-cycle), [PR status](interface.md#pr-status) |

## Resolved open topics

- **Polling cadence:** a 5 minute hint, per-host full-pass interval, unsettled PRs every cycle (D-1, D-2; [Pacing and backoff](interface.md#pacing-and-backoff)).
- **Change detection:** digest diff (D-2).
- **Hook payloads:** the event plus the standard keys (D-3, D-7).
- **Cursor encoding:** D-4.

The seeded text in this repo's older README and design said "binding TBD"; it is now A. The architecture's section 5.2 and 20 entries are updated in architecture v1.17.

## Not in 0.1

No extra event types in 0.1: the events a PR can lack are documented as non-events in [Consumer notes](interface.md#consumer-notes), and consumers re-read forgectl for state. Candidates for 1.x: `opened` (needs `created_at` from forgectl `prs`), `reopened`, `review_requested`, back-to-draft, `approval_withdrawn` or `review_dismissed`, `retargeted`, `renamed`, `unreachable` and `recovered`, a mergeability or readiness event.

## Live verification

| Behavior | Source | Result | Regression |
|---|---|---|---|
| A snapshot costs 5 to 8 requests on GitHub, 19 to 22 on GitLab | spikes A, B | measured | `live` cost budget test |
| `updated_at` is blind to CI completion and thread resolution (GitHub), approvals, CI and bot edits (GitLab) | spikes A, B | measured on history; transitions observed on GitHub only | `live` trigger test; GitLab transition shapes **still to verify** (conservative default: GitHub shapes) |
| Unchanged PRs give byte-identical snapshots after dropping `fetched_at` and `rate_limit` | spikes A, B | 8 of 9 and 6 of 6 | golden-file determinism test |
| `compare` returns `diverged` for a force push, `ahead` for a normal one | spike A, B | confirmed | `live` force-push test |
| forgectl failure classes, `retry_after`, `whoami` exit 12 with an object, `prs` partial results only when two or more hosts are asked (pr-watch asks one), SIGTERM exit 143, `--version` plain text | spike C | measured on the installed forgectl | process tests with fakes |
| GitLab reports no rate-limit headers (`rate_limit` null) | spike B | confirmed | unit test of the low-water rule |
| `approvals_at_head` is null on the tested GitLab server | spike B | confirmed | unit test (null means unknown) |
| V1: forgectl accepts `--` before positionals | none | **still to verify** (conservative default: validate every value, never rely on `--`) | `live` argv test |
| V2: GitHub search index lag behind `updated_at` | none | **still to verify** (default: `poll.overlap` 60 s plus the persisted `listed_updated_at`) | `live` lag test |
| V3: the GitHub repository-count limit of search (forgectl issue 31, unverified) applies to `--state open --author me` | none | **still to verify** (default: a residual-risk line and a visible `last_enumerated_at`) | `live` query test |
| V4: `snapshot.updated_at` is at least the `prs` item's `updated_at` | none | **still to verify** (default: `snap_updated_at` is the larger of the two read at snapshot start) | `live` comparison test |
| V5: GitLab transition shapes | none | **still to verify** (see the trigger row above; default: GitHub shapes) | `live` GitLab transition test |
| V6: `latest_by_reviewer[].at` is null on GitLab | none | **still to verify** (default: the approval key `(state, commit_sha, at)` with null-safe equality) | unit test with null `at` |

## Modules

| Module | Responsibility |
|---|---|
| `gateway` | every subprocess call (forgectl, hooks, cron); fakes plug in here |
| `forgectl_client` | argv building, `--json` parsing, version check, error classification |
| `store` | SQLite access, migrations, leases, event append in one transaction |
| `watches` | watch keys, owners, labels, members, expiry |
| `digest` | building a digest from a snapshot; the `seen` sets |
| `diff` | digest pair to ordered events (all rules in [Events](interface.md#events)) |
| `poller` | enumeration, snapshot selection, pools, pacing, host pause, `max_duration`, report |
| `hooks` | payloads, the bounded in-process pool, process groups, timeouts, the per-poll and per-PR caps |
| `refs` | validation of every user value before it reaches argv, and the offline reference parser |
| `config` | YAML plus drop-ins, env, validation |
| `cli` | argparse, output, exit codes, error envelope |
| `cron` | the tagged crontab block |

## Data model

SQLite (WAL), additive migrations. Tables: `meta` (store id, schema version, `pruned_through_seq`, last prune and reconcile times, host cache), `hosts` (per-host pacing, pause and low-water state), `scopes` (the host-time watermark of a per-PR repo scope), `watches`, `owners`, `labels` (watch, owner, label), `prs` (`pr_id`, canonical `pr`, status, `rev`, digest JSON, `snap_updated_at`, `listed_updated_at`, failure and `incomplete` bookkeeping), `sources` (PR to watch), `events` (`seq` autoincrement, id, type, `pr_id`, `pr`, payload JSON, `detected_at`, labels) and `leases`. There is no alias table: offline commands match a reference against the stored `pr` and `pr_id`. Every persisted field and the transaction that writes it are listed in [State](interface.md#state). A digest is bounded: `seen` holds at most the ids forgectl can list (50 pages per list), checks at most 500, and both are dropped with the row.

## Concurrency

Leases are per PR and only around a snapshot; correctness comes from a compare-and-set commit (`rev` and a check that a source still exists, inside `BEGIN IMMEDIATE`), so `watch add` and `watch remove` take no lease and a takeover of a live holder cannot double-commit; the rules are in [Locks and commit](interface.md#locks-and-commit) and signals in [Signals](interface.md#signals). A second `poll` skips locked PRs and reports `already_running` when every PR that needed a snapshot was locked. Snapshot workers are a bounded thread pool per host calling forgectl subprocesses; the digest and events of one PR are committed together by the single coordinator thread. Hooks run in a bounded pool inside the poll process, each in its own process group; the poll exits when the pool drains.

## Error handling

Classification and its consequences are in [Failures](interface.md#failures). A failed snapshot never changes a digest; a failed commit leaves the previous digest, so the next cycle repeats the diff (events are never duplicated because they are stored in the same transaction as the digest). There is no separate changed-PR mark: the persisted trigger `listed_updated_at > snap_updated_at` is cleared only by a committed snapshot, so a failed or crashed snapshot is retried without extra state.

## Test strategy

- Every forgectl call goes through the gateway; `tests/fakebin/forgectl` is a fake driven by recorded and synthetic snapshots (private data removed), including failure scripts for each class in [Failures](interface.md#failures).
- forgectl's `snapshot`, `prs`, `show`, `compare` and `whoami` schemas are copied into `tests/` pinned to a forgectl version; fakes are validated against them.
- Golden files for every `--json` output (events include `occurred_at`; poll reports are golden too); schema validation of real output and of hook payloads.
- Process tests: kill during commit (no loss, no duplicate), two concurrent polls, takeover of a live lease holder (no duplicate events), `watch remove` during a snapshot, hook timeout and group kill, the per-poll hook cap, a flooded PR plus a `merged` on another PR in the same poll, a killed forgectl child, a fake `gh` that returns 40 MiB (over the output cap), a 4000-character check name with 300 control characters, `--config=/x` as a reference and `-x` as a host.
- Diff tests per event rule, including the baseline, the baseline of an incomplete part, `watch add` on a row that already has a digest, the CI hold (the counter resets, `settle_polls` 0, two checks with one name), re-approval at a new head fires, `incomplete` parts, GitLab (no `review`, null `approvals_at_head`, a reply in an old discussion fires), a comment first seen minimized, reopen re-baselines and a second close emits `closed` again, and rename by `pr_id`.
- `live` tests (never in CI): the spike regressions in the table above.
- Platforms: Linux and macOS; CI is Linux only.
