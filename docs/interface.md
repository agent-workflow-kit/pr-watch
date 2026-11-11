# pr-watch: interface

Status: designed, 2026-11-10 (interface 0.1.0-design). This document is the single normative source for pr-watch's rules. `design.md` records the decisions and the internal structure and links to the headings here instead of restating them; the README, the architecture and the schemas follow it. Machine-readable shapes are in `schemas/`.

## Concepts

pr-watch turns "what changed on these PRs since I last looked?" into a stream of events. A tool or a person registers **watches** on pull requests (GitHub) or merge requests (GitLab), a periodic `poll` compares what the host reports now with what it reported last time, and every difference becomes an **event** that consumers read with their own cursor or receive through an `on-<event>` hook. pr-watch reads the host only through forgectl, never posts or changes anything on a host, and takes no external action, so it keeps no audit log. Terms defined later are used earlier in places: Poll cycle, Digest, Events, Pacing and backoff, Residual risks.

### Identifiers

- **Repo id** and **PR reference:** as in the design guide, section 4.1. `host/<full project path>` for a repo; a PR is a URL or `host/<full project path>#N` (GitLab numbers written without `!`); output always uses `#N`. A reference is resolved by forgectl, which is the only place the grammar is enforced (an invalid reference is exit 2, `code: invalid_pr`).
- **`pr_id`:** the opaque, stable id forgectl reports for a PR (it survives repository renames and moves). pr-watch keys every stored PR on `pr_id` and prints the canonical `pr` form beside it; the `pr` form is refreshed on every successful snapshot. A reference that now names a different `pr_id` than the stored one (a number reused after a repo was deleted and recreated) is a different PR.
- **Owner:** `^[a-z0-9][a-z0-9_.:@-]{0,63}$`, a free string naming who holds a reference to a watch (for example `review-respond`, `human:<name>`). pr-watch assigns no meaning to an owner. `watch add` without `--owner` uses `human:<me.name>` (the name is cut and cleaned to the charset the way the design guide's actor rule does it).
- **Watch key:** a per-PR watch is keyed by `pr_id`. A query watch is keyed by its canonical query string: `mine+open` followed, when filters were given, by `;host=<h>,...;repo=<id>,...` with each list sorted and deduplicated. Two `watch add` calls with the same key are the same watch.
- **Watch id:** `w-` followed by 10 lowercase base32 characters (`a-z`, `2-7`), generated once when a watch is created. Printed by `watch add` and `list`; not needed to remove a watch (a watch is removed by its reference or query).
- **Event id:** `ev-` followed by 12 lowercase base32 characters, generated when the event is stored. It is the deduplication key for hook consumers.
- **Cursor:** `v1.<store_id>.<seq>`: `store_id` is 8 lowercase base32 characters created once with `state.db`, and `seq` is a non-negative decimal integer, the event stream position. A cursor is opaque to consumers (they store and return it verbatim). The grammar is `^v1\.[a-z2-7]{8}\.[0-9]{1,18}$`.
- **Label:** `key=value`; the key matches `^[a-z][a-z0-9_-]{0,31}$` and the value `^[A-Za-z0-9._:@/#+-]{1,128}$` (design guide section 9; no key has built-in meaning).
- **Time:** every stored and printed timestamp is UTC as `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Timestamps read from forgectl are converted to that form. `--since`-style durations take `0` or an integer and a unit: `ms`, `s`, `m`, `h`, `d`.

### Watches, owners and members

- A **per-PR watch** follows one PR. `watch add <pr>` resolves it and records the watch.
- A **query watch** follows a query. Version 0.x has one query, `--mine --open`: the PRs authored by the account forgectl calls "me" on each host in scope. The optional `--host <host>` and `--repo <id>` filters (both repeatable) narrow it and become part of its key. With no filter the hosts in scope are those `forgectl whoami --json` reports for the configured hosts (declared or credentialed); pr-watch caches that list for `hosts_ttl` (default 1 hour) and a host forgectl cannot query appears in the poll report as `skipped`, never as a failure.
- Every watch has a set of **owners** and a multiset of **labels**. Adding an owner that is already present, or an identical label, is a no-op. `watch remove` removes only the named owner's reference; the watch ends when its last owner is gone.
- A query watch has **members**: the PRs its enumeration found open. A member is a PR row (below) that lists the query watch among its sources. A PR can be a member of several query watches and the subject of a per-PR watch at once; it then has one row, one digest and one event stream, and an event carries the union of the labels of every watch that includes the PR (labels as they were when the event was detected).
- A PR row is dropped when none of its sources remains (all its per-PR watches were removed and it is no longer a member of any query watch that still exists). Its stored events stay until retention.

### PR status

Each stored PR has a status:

- `active`: polled normally.
- `terminal`: it has been seen `merged` or `closed`. A merged PR is final. A closed one is reactivated silently when enumeration later shows it open (a reopen is not an event; its later changes are). Terminal PRs get no snapshots; only the cheap enumeration can reactivate a closed one.
- `unreachable`: `failures.unreachable_after` consecutive failures, or a permanent failure (Failures). Polled only on `poll --full` or every Nth cycle (`poll.full_every`), and it clears on the first success.

A terminal PR's row (digest included) is deleted `expiry.after_terminal` after the terminal state was first seen, and so are the watches that only referred to it (a per-PR watch ends; a member is dropped).

### Untrusted text

pr-watch stores and prints no free text from a host: no comment body, title, description, branch name or check output. The only host text it handles is one-line: a login, a file path and a check name. forgectl has already replaced control characters and bidi marks in them with `<U+XXXX>` markers; pr-watch passes them through unchanged, marks them `x-untrusted` in the schemas, and never puts them in argv or environment variables (hooks receive them on stdin only). Anything that needs the text reads it through forgectl by id (`comments`, `reviews`). Consumers must treat the login, path and check-name fields as untrusted.

## Detection

### Poll cycle

`poll` is the only command that contacts a host. Cron or a caller runs it; `events`, `list`, `show` and `watch` never do, except `watch add`'s baseline (see `watch add`). One cycle:

1. **Lease.** Take the per-PR lease locks and one per-query-watch lease (design guide section 4.3: holder PID plus start time plus expiry, taken over when the holder is dead). A PR or query whose lease is held is skipped and counted in the report; when nothing at all could be locked, `poll` exits 0 with `already_running: true`.
2. **Enumerate, per host.** For every host in scope, one `forgectl prs --host <h> --author me --state all --updated-since <t> --limit 1000 --json` covers the query watches, and one `forgectl prs --repo <id> --state all --updated-since <t> --limit 1000 --json` per repo that has an explicit per-PR watch covers those. `<t>` is the time the previous successful enumeration of that scope started, minus `poll.overlap` (default 60 s); a scope never enumerated before uses no `--updated-since` (a query watch's first enumeration is its baseline). Hosts are enumerated one at a time, never in one call, because the shared `--limit` across hosts cuts one host's list. A list with `complete: false` makes this cycle snapshot every PR of that scope (the cheap trigger cannot be trusted).
3. **Members.** A listed item with state `open` that is not yet a member of the query watch becomes one and is baselined silently (step 5 without events). A listed item for a known PR with state `merged` or `closed` is scheduled for a snapshot (it carries no merge data). A listed item that is not a member and is already terminal is ignored.
4. **Choose snapshots.** A PR is snapshotted when it appeared in an enumeration since its last snapshot (the host says it changed), or its stored digest had `ci.state: pending` or an unsettled CI hold, or it is due for the full pass (every `poll.full_every` cycles per host, counted by `poll` runs that reached that host; `--full` forces all), or it is `unreachable` and its turn has come. `--pr <ref>` restricts the cycle to that watched PR and forces its snapshot.
5. **Snapshot.** `forgectl snapshot <pr> --json` through a pool of `poll.concurrency` workers per host (defaults: 4, and 2 for hosts pr-watch has seen report no rate limit). Each result is classified (Failures), then turned into a digest and diffed against the stored one (Digest, Events).
6. **Commit.** All events and the new digest of one PR are stored in one transaction (`BEGIN IMMEDIATE`); the sequence numbers are assigned inside it. A crash before commit loses nothing and repeats the work; after commit the events are durable.
7. **Hooks.** After commit, the `on-<event>` hooks run (Hooks).
8. **Report and housekeeping.** The poll report is printed; `post-poll` fires when any error occurred; `prune` work (retention, expiry) runs at most once a day at the end of a `poll`.

A poll never writes to a host and never blocks on a hook.

### What is polled

The cheap trigger is `updated_at` through `prs`. It is reliable for new comments, reviews, pushes, labels, review requests and draft changes, and blind to check completion, thread resolution (GitHub) and, on GitLab, approvals, finished CI jobs and bot-note edits (spikes A and B). That is why a PR that had pending CI is snapshotted on every cycle until its CI settles, and why a periodic full pass exists. A change that only the full pass can see (a GitLab approval, for example) can therefore be reported up to `poll.full_every` cycles late; the report and `show` print `last_full_at`.

### Digest

The digest is the only per-PR state the diff reads. It holds:

- `head_sha`, `state` (`open`, `closed`, `merged`), `draft`, `base_ref`, `mergeable`;
- `blocked_by`: the sorted list of codes (a code is `{code}` or `{code: other, detail}`, kept as forgectl prints it, including host-specific ones);
- `ci`: `state` (`passing`, `failing`, `pending`, `none`), its settle counter, and the checks keyed by `(name, url)` with each `state`;
- `review_decision` and `approvals_at_head` (a number, or `null` meaning unknown, never zero);
- `reviewers`: per `reviewer.id`, the `latest_by_reviewer` `state`, `commit_sha` and `at`;
- `seen`: the sets of comment ids, thread-comment ids and review ids already reported (ids are unordered; a set, never a watermark);
- `pushed_at`, `merged_head_sha`, `merge_commit_sha`, and the bookkeeping fields (last snapshot time, failure count, status).

It never holds a body, a title or any text beyond the one-line fields above. Parts forgectl lists in `incomplete` (`review_requests`, `reviews`, `threads`, `ci`) are not compared and not overwritten: absence from an incomplete part means nothing.

### Events

An event is one detected difference. The types and the exact rule for each:

| Type | Fires when | Extra fields |
|---|---|---|
| `pushed` | `head_sha` differs from the stored one. Never keyed on `pushed_at` (a normal push reports a committer date up to minutes early). | `from_sha`, `to_sha`, `forced` |
| `comment` | A general comment id (`gh:ic:` on GitHub, a GitLab discussion without a position) is not in `seen`. Edits, minimizing and deletions are not events. | `comment_id`, `thread_id`, `author` |
| `review_comment` | An inline comment id (`gh:rc:`, or a positioned GitLab thread) is not in `seen`. | `comment_id`, `thread_id`, `author`, `path`, `line`, `side` |
| `review` | A review id (`gh:r:`) is not in `seen`. GitHub only; GitLab has no review objects and never emits it. | `review_id`, `author`, `review_state` |
| `approved` | A reviewer's latest state becomes `approved` (it was another state, or absent). One event per reviewer. A new approval after a push counts again. | `reviewer`, `commit_sha`, `approvals_at_head` |
| `changes_requested` | A reviewer's latest state becomes `changes_requested`. A dismissal is not an event. | `reviewer`, `commit_sha` |
| `ci` | `ci.state` changes between `pending`, `passing` and `failing` (and to or from `none` after the hold below). One event per change. | `from`, `to`, `checks` |
| `ready` | `draft` goes from true to false. It does not mean mergeable; consumers read `ci` and `blocked_by`. | `blocked_by` |
| `merged` | `state` becomes `merged`. | `merged_head_sha`, `merge_commit_sha` |
| `closed` | `state` becomes `closed` (not merged). | none |

Common fields on every event: `id`, `seq`, `type`, `pr`, `pr_id`, `repo`, `detected_at`, `occurred_at` (when the host gave one, else `null`), `labels` (the union multiset), `head_sha` (the digest's head after the change), `watches` (the ids of the watches that include the PR). `author` and `reviewer` are `{login, is_bot}` (`login` may be `null`, `is_bot` may be `null`; a consumer must treat `is_bot: null` as a bot). `checks` lists at most 20 `{name, from, to}` entries and `checks_more: <n>` for the rest. `forced` is `true` when forgectl's `compare` of the old and new head reports `diverged` or `unavailable`, `false` when it reports `ahead`, and `null` when `detect_force_push` is off or the call failed. The `compare` call is made only when `detect_force_push` is true.

Rules that apply to all types:

- **Baseline.** The first snapshot of a PR (a per-PR `watch add`, or a query member found by enumeration) fills the digest and emits nothing, with one exception: a PR that is already `merged` or `closed` at that moment emits that one event, once, and then expires normally.
- **Several changes in one snapshot** produce several events, in this fixed order: `pushed`, `review`, `comment`, `review_comment`, `approved`, `changes_requested`, `ci`, `ready`, then `merged` or `closed`, and within a type by ascending `(created_at, id)` of the source item.
- **CI hold.** A move of `ci.state` into `none` is held for `ci.settle_polls` further snapshots (default 1) before it counts, because right after a push the host has not registered its checks yet; the digest keeps the previous state meanwhile and the PR is snapshotted on the next cycle. A move out of `none` is not held. `passing` is never inferred from `none`, and a `ci` event is never emitted from a snapshot whose `ci` part is incomplete.
- **GitLab.** `review` never fires. `approved` and `changes_requested` come from `latest_by_reviewer` on both hosts. `approvals_at_head` is `null` where the project does not reset approvals on push, which means unknown. A GitLab `outdated` value is not used.
- **Terminal.** After `merged`, a PR is final. After `closed`, enumeration may reactivate it silently.

### Pacing and backoff

- **Host pause.** A `rate_limited` failure with `retry_after` pauses that host until then. Without `retry_after` the pause is 1 minute and doubles on each further `rate_limited` result up to 15 minutes; a success resets it. A paused host is skipped (reported as `paused_until`), other hosts continue.
- **Low water.** Where forgectl reports a numeric `rate_limit.remaining` for a host and it is below `poll.host_low_water` (default 100), `poll` still runs the enumeration but snapshots only PRs whose CI is pending or whose enumeration showed a change, until `reset_at`. A host that reports `null` (a self-hosted GitLab without limit headers) is never low-water paused; its cost is limited by the pool size alone.
- **Pool.** `poll.concurrency` workers per host; `hosts.<host>.concurrency` overrides it.
- **Cost accounting.** A GitHub snapshot is 5 to 8 requests (5 when forgectl's `hosts.<host>.me` is set); a GitLab snapshot was 19 to 22 requests and 11 to 14 seconds in the design spikes. The poll report counts snapshots per host so a user can budget.

### Failures

forgectl results are classified by exit code and error `code`:

- **Success:** exit 0 with an object (a snapshot with `complete: false` is a success with `incomplete` parts).
- **Retryable:** `rate_limited`, `network_error` (also a timeout), `upstream_error`. forgectl already retried; pr-watch does not retry inside a poll. The PR keeps its digest, its failure counter goes up, and it is tried again next cycle.
- **Permanent for the PR:** `not_found`, `permission_denied`, `auth_required`. `not_found` means "unknown", never "closed". The failure counter goes up; the PR is `unreachable` after `failures.unreachable_after` consecutive failures (default 5), or at once for `auth_required` on its host (then the whole host is reported).
- **Whole-host:** `auth_required` on a host and `prs` returning exit 12 with `errors` mark that host failed for this cycle; `prs` partial results still count for the hosts that answered.
- **Tool:** forgectl missing (`tool_missing`) or older than the minimum (`tool_too_old`, below) makes `poll` exit 12 before any work.
- **Interruption:** forgectl exiting 128+N means pr-watch itself is being stopped; the poll ends, nothing partial is committed for the PR in flight.

A failure never produces an event. `closed` and `merged` are never inferred from a failure or from a PR vanishing from a list.

The minimum forgectl version is the `forgectl.min_version` that this pr-watch release declares (it is read from `forgectl --version`, a plain-text `forgectl X.Y.Z` line, and compared on major and minor). A `snapshot` or `prs` result missing a field this document relies on is an `upstream_error`-class failure for that call (`code: contract`), logged with the forgectl version.

## Commands

All commands take `--json`, `--config PATH`, `--verbose`, `--quiet` (Global flags). Every command that prints a PR prints it in `#N` form.

### `watch add`

`pr-watch watch add <pr> [--label k=v]... [--owner O] [--no-baseline]`
`pr-watch watch add --mine --open [--host H]... [--repo ID]... [--label k=v]... [--owner O] [--no-baseline]`

Adds the owner's reference to the watch (creating it), and unions the labels. For a per-PR watch it resolves and baselines synchronously: `forgectl show <pr>` (canonical `pr`, `pr_id`) and then `forgectl snapshot <pr>` (the baseline digest). An invalid reference exits 2 and a missing PR exits 10, recording nothing; a transient failure exits 12, recording nothing (so a retry is safe). A PR already `merged` or `closed` emits that one event now and then follows normal expiry. For a query watch, it runs the enumeration once and baselines the members it finds (potentially many snapshots; the result prints the count). `--no-baseline` records only the watch and leaves enumeration to the next `poll`; it is the default for the query form when stdin is not a TTY (the flag `--baseline` forces a baseline there). Adding an existing owner and labels to an existing watch prints `changed: false` and does nothing else; a new owner on an existing watch adds the reference without a new baseline. `--no-baseline` on a per-PR watch is accepted: `show` is still called (to resolve the PR) and the first `poll` baselines it.

### `watch remove`

`pr-watch watch remove <pr> [--owner O]` and `pr-watch watch remove --mine --open [--host H]... [--repo ID]... [--owner O]`

Removes only the named owner's reference (default `human:<me.name>`). When the owner holds no reference, exit 10. The watch ends when its last owner is gone; PR rows with no remaining source are dropped with their digests; stored events stay until retention. Removing never contacts a host. A query watch is addressed by its exact filters (the key); there is no partial match.

### `poll`

`pr-watch poll [--pr <ref>] [--repo <id>] [--full] [--no-hooks]`

Runs one cycle (Poll cycle). `--repo` limits the cycle to PRs of that repo; `--full` snapshots every active PR of the scopes it covers, ignoring the cheap trigger; `--no-hooks` stores events and skips hooks (for a catch-up run). Exit 0 when the poll ran, including `already_running` and a partial result where every failed object was classified (the report shows per-host and per-PR errors). Exit 12 when a host failed in a way that left PRs unchecked (`failed` in the report lists them) or forgectl is missing or too old. Exit 10 when `--pr` names a PR that is not watched. `poll` is safe to run concurrently; a second one exits 0 with `already_running`.

### `list`

`pr-watch list [--label k=v]... [--owner O] [--pr <ref>] [--status active|terminal|unreachable]`

Prints the watches (id, kind, key or reference, owners, labels, member count) and the PRs (canonical `pr`, `pr_id`, status, state, draft, `ci_state`, `head_sha`, `last_polled_at`, `last_full_at`, failure count, sources). Never contacts a host. `--label` and `--owner` filter watches and, with them, their PRs; `--pr` selects one PR.

### `show`

`pr-watch show <pr>`

One watched PR: its row, its watches and the digest summary (the same fields as in `list`, plus `blocked_by`, `review_decision`, `approvals_at_head`, the reviewers' latest states, and the counts in `seen`) and its most recent 20 events. Exit 10 when the PR is not stored. `<pr>` is matched on the canonical form, or on `pr_id` through a stored row; a reference to a renamed repo is resolved by `pr_id` only through the stored `pr` aliases pr-watch has seen.

### `events`

`pr-watch events [--label k=v]... [--pr <ref>] [--type T]... [--since CURSOR] [--limit N]`

Prints events after the cursor, oldest first. Never contacts a host and never changes state (non-destructive: each consumer keeps its own cursor). Without `--since`, no event is returned and the cursor is the current head ("from now"). The result carries `events`, `cursor` (the position after the last event examined, so filters do not rescan) and `gap`. `--limit` defaults to 100 (maximum 1000); when more matching events exist the result has `more: true` and the cursor is that of the last returned event. `--label` is an AND filter over the labels stored on the event; `--type` is repeatable (OR); `--pr` selects by `pr_id` after resolving the reference through the stored rows (an unknown reference exits 10).

`gap` is `true` when the cursor's `seq` is older than the oldest retained event, or its `store_id` differs from the current store (the state was reset or rebuilt), or its `seq` is greater than the head. With a gap, the events from the oldest retained one are still returned (all of the current store when the id differs) and the cursor is moved; the consumer must re-sync current state through forgectl and treat the events as possibly incomplete. A cursor that does not match the grammar exits 2 (`code: usage`, `details.kind: bad_cursor`).

### `label`

`pr-watch label add <pr|--watch ID> k=v...` and `label remove ...`

Adds or removes labels on a watch (a watch is named by its id, or by a per-PR watch's reference). Idempotent. Labels already stored on past events do not change.

### `prune`

`pr-watch prune [--dry-run]`

Deletes events older than `retention.events`, rows past `expiry.after_terminal`, and the watches and members that only referred to them. It also runs at most once a day at the end of `poll`. It never deletes an event younger than 30 days even if the config says less (the config minimum is 30 days).

### `reconcile`

`pr-watch reconcile`

Repairs state against itself (there is no host to compare): drops members whose query watch no longer exists, orphaned digests and expired leases, and reports what it changed. Idempotent. Safe to run from cron.

### `cron`

`pr-watch cron install [--every 5m]` and `cron uninstall`: writes (or removes) a tagged `# pr-watch managed` block containing `pr-watch poll --quiet` and `pr-watch reconcile --quiet` entries with an explicit `PATH` that includes `~/.local/bin` and the directory of `forgectl`, and an optional user-owned env-file prefix (config `cron.env_file`). The harness's `awkit cron install` writes the same block; each replaces the other's.

### `export`

`pr-watch export [--since T]`

Prints the stored events as JSON lines (`schemas/log.json` describes `log.jsonl`; this is the event stream). pr-watch takes no external action, so it writes no `audit.jsonl` and `export` has no audit records to print.

### Global flags

`--json`, `--config PATH`, `--verbose`, `--quiet`, `--version`. `--dry-run` exists on `prune`; `watch add` and `watch remove` do not take it (they write only local state). `--yes` and `--actor` are not accepted: no command takes an external action or asks for a confirmation.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including `already_running`, a no-op, and a clean partial poll) |
| 1 | unexpected or internal error |
| 2 | usage error (bad flag, bad cursor, invalid PR reference, invalid config) |
| 10 | not found (PR not stored or not watched, owner holds no reference, PR missing on the host) |
| 12 | failed for a reportable reason (host unreachable or failing, forgectl missing or too old, forgectl contract violation) |
| 20 | refused (a forgectl refusal passed through, for example `unknown_host`, with its `reason` and `lower_error`) |

Codes 11, 13 to 19 and 21 are unused: pr-watch has no pre-hooks, no pending store and no session. A signal ends the process with 128 plus its number and, as for every tool, no output beyond what had already been printed.

## Output by exit code

- **0:** the command's result object (`schemas/cli/<command>.json`); with `--json` always valid JSON on stdout.
- **12 from `poll`:** the normal poll report (`failed` lists what was left unchecked) and, on stderr, one line per error. Exit 12 for forgectl missing or too old, or a contract violation, prints the error envelope instead.
- **Any other non-zero exit:** the error envelope on stdout (and a text line on stderr).

### Error envelope

`{"error": {"code": "...", "message": "...", "reason": "...", "details": {...}, "lower_error": {...}}}` (`schemas/cli/error.json`). `code` is one of `usage`, `config_invalid`, `invalid_pr`, `not_found`, `host_failed`, `tool_missing` (with `details.install`), `tool_too_old` (with `details.required` and `details.found`), `contract`, `refused` (with `reason`), `internal`. `lower_error` carries forgectl's own `code`, `reason`, `retry_after` and `message` when a forgectl call caused the error. Messages are sanitized one-line text.

### Result objects

The result objects of `watch add`, `watch remove`, `label add|remove`, `prune`, `reconcile`, `cron install|uninstall`, `list` and `show` are defined by `schemas/cli/<command>.json`, which are normative for their field names. Rules that go beyond the schemas: every mutating command prints `changed` (true or false); a PR stored without a digest (added with `--no-baseline`, not yet polled) prints `null` for `state`, `draft`, `ci_state`, `head_sha` and `last_polled_at`; `events.more` is always present; a `poll` that exits 0 with `already_running` still prints the full report shape with zero counts.

### Poll report

`poll --json` prints: `already_running`, `started_at`, `duration_ms`, `hosts` (per host: `queried`, `snapshots`, `requests_estimate`, `paused_until`, `skipped`, `errors`), `prs` (`snapshotted`, `unchanged`, `skipped_locked`, `unreachable`), `events` (count by type), `hooks` (`run`, `skipped`, `failed`), `failed` (the PRs or scopes left unchecked, each with the failure code) and `full_pass` (whether this cycle was a full pass).

## Hooks

Hook points (design guide section 8). All are post-hooks that report a change that already happened: they cannot veto, escalate or undo anything, and their failures are logged and warned, never fatal.

| Hook | Fires | Payload |
|---|---|---|
| `on-pushed`, `on-comment`, `on-review_comment`, `on-review`, `on-approved`, `on-changes_requested`, `on-ci`, `on-ready`, `on-merged`, `on-closed` | once per stored event of that type, after its transaction committed | the event (Events) plus `tool`, `hook`, `object_type: pr`, `object_id` (the `pr_id`) and `repo` |
| `post-poll` | once at the end of a `poll` that had any error | counts, per-host error codes, no PR text; its `object_id` is the poll's `started_at` |

- **Delivery** is at-most-once. Events are committed first; hooks run afterward, one invocation per event, in event order. If `poll` is killed, the events are safe but their hooks are not re-run; a consumer that needs a guarantee reads `events` with a cursor. The payload carries `event_id` (the same value as the event's `id`) as the deduplication key.
- **Execution:** `sh -c`, own process group, JSON on stdin, no template substitution. Environment: `PR_WATCH_HOOK=<hook name>`, `PR_WATCH_ID=<event id>` (for `post-poll`, the empty string) and `PR_WATCH_OBJECT_TYPE=pr` (`poll` for `post-poll`). Hooks receive a clean environment (`PATH`, `HOME`, `LANG`, `TERM`, the `XDG_*` variables and the `PR_WATCH_*` ones above) plus the names in `hooks.env_allow`; they never inherit forgectl's token variables.
- **Timing:** hooks are `async: true` by default (detached); `async: false` runs one at a time within the poll's budget. Each has a timeout (default 30 s); on timeout the process group is signaled (SIGTERM, then SIGKILL after 5 s), only the group pr-watch created. Hook decisions and stdout are ignored.
- **Cap:** at most `hooks.max_per_poll` (default 50) hook invocations start in one poll. The rest are skipped with one warning naming the number skipped (also in the report); their events are stored.
- **Logging:** every hook run writes a `log.jsonl` record with the event id and its exit code, so a loss is visible.
- **`--hook <name>=<cmd>`** (repeatable) on `poll` adds a per-invocation hook to the configured ones.

Payload shapes are in `schemas/hooks/<hook>.json`; the event object is `schemas/event.json`. Every field that carries host text (`author.login`, `reviewer.login`, `path`, check names) is marked `x-untrusted`.

## Config

File `~/.config/pr-watch/config.yaml` plus `config.d/*.yaml` (design guide section 5). Keys (schema `schemas/config.json`, example `config.example.yaml`; every value shown is the default):

```yaml
poll:
  interval_hint: 5m        # pacing and the cron install line only
  full_every: 6            # cycles between full passes, per host
  concurrency: 4           # snapshot workers per host
  host_low_water: 100      # forgectl rate_limit.remaining below this: snapshot only what must be
  overlap: 60s             # subtracted from the enumeration time
  hosts_ttl: 1h            # how long whoami's host list is cached
hosts:                     # per-host overrides
  # <host>: {full_every: 12, concurrency: 2}
retention:
  events: 30d              # at least 30d
expiry:
  after_terminal: 7d
failures:
  unreachable_after: 5
detect_force_push: false   # call forgectl compare to fill `forced`
ci:
  settle_polls: 1
forgectl:
  timeout: 120s            # forgectl --timeout per call
hooks:
  max_per_poll: 50
  env_allow: []
  # on-approved: [{cmd: "...", timeout: 30, async: true}]
cron:
  env_file: null
repos:                     # per-repo overrides (design guide section 5)
  # <host/path>: {detect_force_push: true, ci: {settle_polls: 2}, failures: {unreachable_after: 8}}
```

A hook's `timeout` is an integer number of seconds; every other duration is a string (`5m`, `30d`). `retention.events` below `30d` (or not a whole number of days) is invalid config (exit 2, `code: config_invalid`), not clamped. `forgectl.min_version` is a constant of the release, not a config key. `me.name` is an optional key (the default owner's name). The built-in per-host defaults when not set: `full_every` 6, and `concurrency` 4 for hosts reporting a rate limit and 2 for hosts that report none (GitLab self-hosted in the design spikes). Per-repo overrides apply to `detect_force_push`, `ci.settle_polls` and `failures.unreachable_after`. The identity ("me") is forgectl's: set `me` and `hosts.<host>.me` in forgectl's config (setting `hosts.<host>.me` saves one request per snapshot on GitHub); pr-watch has no `me` key of its own, apart from `me.name` used for the default owner. Invalid config fails fast naming the key. pr-watch holds no credentials: git-host access is forgectl's.

## Environment variables

Config keys may be set as `PR_WATCH_<KEY>` (nested keys joined with `__`, for example `PR_WATCH_POLL__CONCURRENCY`). `PR_WATCH_HOOK`, `PR_WATCH_ID` and `PR_WATCH_OBJECT_TYPE` are set for hooks only and are not config keys. pr-watch passes its own environment through to forgectl unchanged (forgectl reads its token variables from it); `FORGECTL_GH_BIN` and `FORGECTL_GLAB_BIN` therefore apply. A required variable that forgectl needs and is missing surfaces as forgectl's `auth_required`.

## Audit and log

pr-watch takes no external action, so it writes no `audit.jsonl`. `log.jsonl` (`schemas/log.json`) is a public, append-only record of polls (start, end, per-host summary), snapshot failures, state changes of a PR (`status` transitions), hook runs (event id, hook, exit code, duration) and configuration warnings; it never holds host text beyond the one-line fields. The event stream itself is the product: it lives in `state.db`, is read through `events`, and `export` prints it.

## Files

- `~/.config/pr-watch/config.yaml`, `config.d/*.yaml`.
- `~/.local/state/pr-watch/state.db`: private (SQLite, WAL, versioned additive migrations): `meta` (`store_id`, schema version, host list cache), `watches`, `owners`, `labels`, `prs` (digest JSON, status, schedule), `sources`, `events`, `leases`.
- `~/.local/state/pr-watch/log.jsonl`: public.
- Directories are mode 0700, files 0600. XDG variables are respected; macOS uses the same paths.

## Labels

Design guide section 9 applies to watches: repeated `--label key=value` on `watch add`, `label add` and `label remove`, a stored multiset, returned verbatim in `--json`, filtered with repeatable `--label` (AND). A PR's labels on an event are the union over its watches at detection time. No key has built-in meaning.

## TUI

None in 0.x. The harness dashboard is the view over events. A `browse` TUI, if wanted, is a 1.x item and follows design guide section 10.

## Upgrades

- Migrations are additive and run on first use; a database written by a newer version is refused (exit 12, `code: contract`, naming the versions) rather than read.
- `--json` outputs and hook payloads follow semver; new fields are additive (consumers ignore unknown fields). A new event type is a minor change; consumers must ignore types they do not know.
- A new forgectl minimum is a minor change recorded in the changelog and `forgectl.min_version`.
- A reset or restore of `state.db` creates a new `store_id`, so every consumer's cursor reports `gap: true` once.

## Residual risks

- **Late detection.** A change the cheap trigger cannot see (CI completion, GitLab approvals) is found by the pending-CI rule or the full pass, up to `poll.full_every` cycles late (about 30 minutes at the defaults on GitHub, an hour on GitLab).
- **At-most-once hooks.** A killed poll leaves stored events whose hooks never ran.
- **GitLab approvals.** `approvals_at_head` is `null` (unknown) where the project does not reset approvals on push; an `approved` event there says nothing about the head. GitLab transition shapes for new notes, new checks and head changes were not observed live in the design spikes (the server was quiet) and are a `live` regression item.
- **Enumeration limits.** GitHub search caps at 1000 results; `complete: false` makes the cycle snapshot everything in scope, which costs more.
- **Edits are invisible.** An edited or deleted comment is not an event; a minimized comment stays in `seen`.
- **Reopen.** A closed PR that is reopened is reactivated silently; a merged PR is final.
- **Large snapshots.** A snapshot for a very large PR can be 17 MB (forgectl's own bound) and is held in memory while parsed.
- **Same-user processes** can read and edit `state.db`; the event stream is not tamper-proof.
- **forgectl as a boundary.** pr-watch trusts forgectl's sanitizing of one-line text and its classification of failures; a forgectl bug that returns unsanitized text would reach event payloads (the schemas bound the length of every text field to 256 characters).

## Platform notes

Linux, WSL and macOS behave the same; nothing here depends on tmux, inotify or a notifier. cron needs `forgectl` on its `PATH` (`cron install` writes the directory). WSL: keep the state dir on the Linux filesystem, not under `/mnt/c`.
