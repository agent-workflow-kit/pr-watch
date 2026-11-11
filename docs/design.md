# pr-watch: design

Status: designed, 2026-11-10 (interface 0.1.0-design). The rules are in [interface.md](interface.md), the single normative source; this document records the decisions, the structure and the test strategy and links to the headings there instead of restating them.

## Role and layer

Layer 2 (composite). Watches PRs for changes and exposes them as events and hooks. Works fully standalone (architecture C2): it needs forgectl and nothing else of ours. Consumers (review-respond, ticket-work, review-watch, the harness) are unknown to it; behavior differs only by explicit arguments (`--owner`, `--label`).

## Uses

| Tool | Binding | What for |
|---|---|---|
| forgectl | A (hard) | `prs`, `snapshot`, `show`, `compare` (read-only, `--json`), `whoami` (host discovery); minimum version `MIN_FORGECTL`, a constant of the release |

Third parties: none directly (git-host access is forgectl's). Dependencies of the package itself: `pyyaml` (the stdlib cannot read YAML) and `pydantic` (config validation), as the design guide prescribes; nothing else beyond the standard library (`argparse`, `sqlite3`, `subprocess`, `concurrent.futures`).

## Decisions

| ID | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | `poll` is the only command that contacts a host; cron or the harness runs it; default hint 5 minutes | Consumers read `events` offline; one network path is easy to budget and to lock | [Poll cycle](interface.md#poll-cycle) |
| D-2 | Detection is snapshot diffing against a compact digest; query watches list the open set each cycle and per-PR repos use a host-time watermark; a persisted `listed_updated_at` newer than `snap_updated_at`, re-snapshots of unsettled PRs and a periodic full pass choose what to snapshot; an unsettled storm and a first poll are bounded by `poll.max_snapshots` | Spikes: `updated_at` misses CI completion everywhere and GitLab approvals; snapshot-all costs 5 to 22 requests per PR | [Poll cycle](interface.md#poll-cycle), [What is polled](interface.md#what-is-polled), [Digest](interface.md#digest) |
| D-3 | Events and hook payloads carry metadata only, never host free text | Untrusted text must not reach state, notifications or logs; consumers fetch text through forgectl by id | [Untrusted text](interface.md#untrusted-text), [Events](interface.md#events) |
| D-4 | Cursor `v1.<store_id>.<seq>`; a changed store id, a seq beyond the head or one below the pruned range is `gap: true` | Detects both retention loss and a reset database without per-consumer state | [`events`](interface.md#events-1) |
| D-5 | Watches are keyed (pr_id or canonical query), have owners and labels; query watches have members baselined silently | Reference counting by owner is in the architecture; members need a digest to diff | [Watches, owners and members](interface.md#watches-owners-and-members) |
| D-6 | Exact event definitions: per-reviewer approval keyed on `(state, at, commit_sha)`, `ci` held for a move to or from `none` (it carries counts of the counted checks; a change between `pending`, `passing` and `failing` is not held), `ready` is draft to ready only and carries `from_blocked_by` and `blocked_by` (S-3), `pushed` keys on the head sha | forgectl flaps right after a push (checks not registered, mergeability unknown); `pushed_at` can be minutes early | [Events](interface.md#events) |
| D-7 | Hooks are best-effort, at-most-once, run in a bounded pool inside the poll, and are capped per poll and per PR (`merged`, `closed` and `post-poll` are exempt); events are committed first | A notification is not a guarantee; the cursor is | [Hooks](interface.md#hooks) |
| D-8 | Command set: `poll`, `watch add|remove`, `list`, `show`, `events`, `label`, `prune`, `cron`, `export`; no TUI in 0.x | The consumers are tools; the harness dashboard shows events | [Commands](interface.md#commands) |
| D-9 | forgectl is binding A; per-host pause and per-PR failure counters; a failure never yields an event; one failure table with a column per call kind; whole-host failures are `auth_required`, `tool_missing` and host-call failures; `rate_limited` never counts | forgectl retries itself; `not_found` means unknown | [Failures](interface.md#failures), [Pacing and backoff](interface.md#pacing-and-backoff) |
| D-10 | Query scope by repeatable `--host` and `--repo` filters; enumeration per host; config keys with per-host and per-repo overrides | One shared `--limit` across hosts cut one host's list (spike B) | [Config](interface.md#config) |
| D-11 | `watch add <pr>` baselines synchronously with one `snapshot` (`--no-baseline` for the per-PR form only); a query `watch add` never baselines, its first `poll` does | A typo should fail at add time; no command that behaves differently under cron | [`watch add`](interface.md#watch-add) |
| D-12 | No audit log; `export` prints events | pr-watch takes no external action | [Audit and log](interface.md#audit-and-log) |
| D-13 | Query enumeration lists the open set each cycle; "changed" is `listed_updated_at > snap_updated_at`; a reopen sets a persisted re-baseline flag (silent except `pushed`, `merged` and `closed`) | Removes windows, clocks and the baseline hole | [Poll cycle](interface.md#poll-cycle), [PR status](interface.md#pr-status) |
| D-14 | Membership: enumeration adds and marks (`left_at`), only a snapshot commit removes a source | a merged member must keep its source so its event is stored; no enumeration-time removal | [Watches, owners and members](interface.md#watches-owners-and-members) |
| D-15 | Deviations from the design guide are recorded in Hooks and Labels of the interface | one place for the differences | [Hooks](interface.md#hooks), [Labels](interface.md#labels) |

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
| `latest_by_reviewer[].at` on GitLab | forgectl interface (Reviews) | resolved: documented as `approved_at` for approved and null otherwise; the approval key stays null-safe | unit test with null `at` |

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

SQLite (WAL), additive migrations. Tables: `meta` (`store_id`, `schema_version`, `pruned_through_seq`, `last_prune_at`, `last_poll_finished_at`, `hosts_cache`), `hosts` (per-host pacing, pause and low-water state), `scopes` (`repo`, `watermark`: the host-time watermark of a per-PR repo scope, created by a repo's first listing and deleted when no per-PR row refers to the repo), `watches`, `owners`, `labels` (watch, owner, label), `prs` (`pr_id`, canonical `pr`, `repo`, `host`, `status`, `terminal_at`, `terminal_kind`, `rev`, digest JSON, `snap_updated_at`, `listed_updated_at`, `last_polled_at`, `last_attempt_at`, `failure_count`, `last_failure_code`, `incomplete`, `unsettled_since`, `rebaseline`, `baselined_at`), `sources` (`pr_id`, `watch_id`, `left_at` for query sources), `events` (`seq` autoincrement, id, type, `pr_id`, `pr`, payload JSON, `detected_at`, labels) and `leases`; deletes cascade (watch to owners, labels and sources, PR to sources) and a PR row is dropped in the transaction of its last source, so no orphan exists. There is no alias table: offline commands match a reference against the stored `pr` and `pr_id`. Every persisted field and the transaction that writes it are listed in [State](interface.md#state). A digest is bounded: `seen` holds at most the ids forgectl can list (50 pages per list), checks at most 500, and both are dropped with the row.

## Concurrency

Leases are per PR and only around a snapshot; correctness comes from a compare-and-set commit (`rev` and a check that a source still exists, inside `BEGIN IMMEDIATE`), so `watch add` and `watch remove` take no lease and a takeover of a live holder cannot double-commit (a lease is taken over when its holder is dead or it is expired, whatever the holder's liveness, and `prune` deletes dead and expired leases; there is no `reconcile`); the rules are in [Locks and commit](interface.md#locks-and-commit) and signals in [Signals](interface.md#signals). A second `poll` skips locked PRs and reports `already_running` when every PR that needed a snapshot was locked. Snapshot workers are a bounded thread pool per host calling forgectl subprocesses; the digest and events of one PR are committed together by the single coordinator thread. Hooks run in a bounded pool inside the poll process, each in its own process group; the poll exits when the pool drains.

## Error handling

Classification and its consequences are in [Failures](interface.md#failures). A failed snapshot never changes a digest; a failed commit leaves the previous digest, so the next cycle repeats the diff (events are never duplicated because they are stored in the same transaction as the digest). There is no separate changed-PR mark: the persisted trigger `listed_updated_at > snap_updated_at` is cleared only by a committed snapshot, so a failed or crashed snapshot is retried without extra state.

## Test strategy

- Every forgectl call goes through the gateway; `tests/fakebin/forgectl` is a fake driven by recorded and synthetic snapshots (private data removed), including failure scripts for each class in [Failures](interface.md#failures).
- forgectl's `snapshot`, `prs`, `show`, `compare` and `whoami` schemas are copied into `tests/` pinned to a forgectl version; fakes are validated against them.
- Golden files for every `--json` output (events include `occurred_at`; poll reports are golden too); schema validation of real output and of hook payloads.
- Process tests: kill during commit (no loss, no duplicate), two concurrent polls, takeover of a live lease holder (no duplicate events), `watch remove` during a snapshot, hook timeout and group kill, the per-poll hook cap, a flooded PR plus a `merged` on another PR in the same poll, a killed forgectl child, a fake `gh` that returns 40 MiB (over the output cap), a 4000-character check name with 300 control characters, `--config=/x` as a reference and `-x` as a host.
- Diff tests per event rule, including the baseline, the baseline of an incomplete part, `watch add` on a row that already has a digest, the CI hold (the counter resets, `settle_polls` 0, two checks with one name), re-approval at a new head fires, `incomplete` parts, GitLab (no `review`, null `approvals_at_head`, a reply in an old discussion fires), a comment first seen minimized, reopen with a new head emits `pushed` only, reopen then closed again before the snapshot emits `closed` once, rename by `pr_id`, two reviewers with null ids, and a GitLab second `changes_requested` after a push does not fire.
- Membership and polling tests: member merges and the event is stored, a member that no longer matches is dropped after one snapshot, a terminal member is never rescheduled, 100 unsettled members cause at most 40 snapshots per host per cycle with none starved, a push resets `unsettled_since`, a terminal and an unreachable row are not chosen by triggers (a) to (d), a full pass survives `max_snapshots` deferral, `--repo` polls do not advance the pass counter, a `--no-baseline` per-PR watch gets a listing from its first baselined row, a `complete: false` listing followed by `max_duration` still snapshots the cut PRs next cycle, a rename moves the scope.
- Failure and size tests: a fake forgectl answers `host_rejected` for one PR only and the other PRs of the host are snapshotted, `auth_required` on the first queued PR makes one call and cancels the queue, `rate_limited` for five cycles never makes a PR `unreachable`, a repo listing `not_found` changes no counter and names the repo in `failed`, a snapshot with another `pr_id`, a 40 MiB fake snapshot of a merged PR stores `merged` via `show`, a fake forgectl that hangs past its timeout is killed and classified `killed`.
- CI and event tests: hold out of `none`, the hold counter counts up and resets, uncounted checks are ignored, `ready` carries both `blocked_by` sets, labels of a watch removed during a snapshot are not on its events.
- Hook tests: five flooded PRs plus a `merged` on a sixth and a `post-poll` in one poll, an unkillable hook is abandoned, `--hook=bad name` is refused.
- Command tests: `watch add` of an expired but unpruned terminal row creates a live watch, `watch add` on a merged PR returns a cursor before its event, a second add with a new owner and no digest baselines, remove with a different path case matches, remove of a renamed name warns, removing a query watch removes its members, a dead lease is taken over.
- `live` tests (never in CI): the spike regressions in the table above.
- Platforms: Linux and macOS; CI is Linux only.

## Carry-forwards

- forgectl CF-a: a body-free projection of `snapshot` or a cap on its final output; until then pr-watch degrades to `forgectl show` for terminal detection only ([Residual risks](interface.md#residual-risks)).
- forgectl CF-h: `show` or `prs` item fields that would let a listing detect a repo rename.
- awkit-ci and the harness: the `awkit cron install` writer produces pr-watch's golden block with its end line ([`cron`](interface.md#cron)).
- review-respond and ticket-work: label watches with `consumer=<name>`, select events with `events --label` and keep a set of handled comment ids ([Consumer notes](interface.md#consumer-notes)).
