# pr-watch: interface

Status: designed, 2026-11-10 (interface 0.1.0-design). This document is the single normative source for pr-watch's rules. `design.md` records the decisions and the internal structure and links to the headings here instead of restating them; the README, the architecture and the schemas follow it. Machine-readable shapes are in `schemas/`.

## Concepts

pr-watch turns "what changed on these PRs since I last looked?" into a stream of events. A tool or a person registers **watches** on pull requests (GitHub) or merge requests (GitLab), a periodic `poll` compares what the host reports now with what it reported last time, and every difference becomes an **event** that consumers read with their own cursor or receive through an `on-<event>` hook. pr-watch reads the host only through forgectl, never posts or changes anything on a host, and takes no external action, so it keeps no audit log. Terms defined later are used earlier in places: Poll cycle, Digest, State, Events, Pacing and backoff, Residual risks.

### Identifiers

- **Repo id** and **PR reference:** as in the design guide, section 4.1. `host/<full project path>` for a repo; a PR is a URL or `host/<full project path>#N` (GitLab numbers written without `!`); output always uses `#N`. pr-watch validates every user value before it builds argv: a PR argument must match the `pr` pattern of `schemas/defs.json` or be an `https://` URL without whitespace or control characters; `--host` must match the host grammar and `--repo` the repo-id grammar (exit 2 `usage` at `watch add` and `poll`); every stored `pr` and filter is re-validated before use; a value starting with `-` is refused; shas for `compare` match `^[0-9a-f]{40}$`. argv is built as `--option=value` where forgectl accepts it; a `--` before positionals is used only if confirmed (a forgectl question, see design.md Live verification), so validation is the primary defense. A stored free string is never put in argv; `pr_id` is never used in argv or a path. forgectl's grammar decides everything else (an invalid reference is exit 2, `code: invalid_pr`).
- **`pr_id`:** the opaque, stable id forgectl reports for a PR (it survives repository renames and moves). pr-watch keys every stored PR on `pr_id` and prints the canonical `pr` form beside it; the `pr` form is refreshed on every successful snapshot. A reference that now names a different `pr_id` than the stored one (a number reused after a repo was deleted and recreated) is a different PR.
- **Offline resolution:** commands that never contact a host (`show`, `list --pr`, `events --pr`, `watch remove`, `label`, `poll --pr`) resolve a reference in process: a canonical `host/path#N` (host lowercased), a `pr_id`, or a URL parsed by a small pattern (GitHub `https://<host>/<owner>/<repo>/pull/<N>`, GitLab `https://<host>/<path>/-/merge_requests/<N>`; a query, fragment or trailing path is ignored; a GitLab `api_prefix` host or any form the parser does not recognize is exit 2 `invalid_pr` with the hint to use `host/path#N`). A reference is matched against the stored canonical `pr` and `pr_id`; there are no stored aliases, so a stale name after a repo rename does not match a row (use the new name or the `pr_id`).
- **Owner:** `^[a-z0-9][a-z0-9_.:@-]{0,63}$`, a free string naming who holds a reference to a watch (for example `review-respond`, `human:<name>`). pr-watch assigns no meaning to an owner. `watch add`, `watch remove`, `label add` and `label remove` take `--owner O`. Without it: on a TTY the owner is `human:<me.name>`; when stdin is not a TTY the command exits 2 (`usage`, `details.kind: owner_required`, message "--owner is required when not run from a terminal"). `me.name` defaults to the OS user name; cleaning follows the design guide section 6 actor rule (lowercase, characters outside the owner charset replaced with `-`, cut to 64, a leading non-alphanumeric stripped); an empty result is exit 2 asking for `--owner`.
- **Watch key:** a per-PR watch is keyed by `pr_id`. A query watch is keyed by its canonical query string: `mine+open` followed, when filters were given, by `;host=<h>,...;repo=<id>,...` with each list sorted and deduplicated. Two `watch add` calls with the same key are the same watch.
- **Watch id:** `w-` followed by 10 lowercase base32 characters (`a-z`, `2-7`), generated once when a watch is created. Printed by `watch add` and `list`; not needed to remove a watch (a watch is removed by its reference or query).
- **Event id:** `ev-` followed by 12 lowercase base32 characters, generated when the event is stored. It is the deduplication key for hook consumers.
- **Cursor:** `v1.<store_id>.<seq>`: `store_id` is 8 lowercase base32 characters created once with `state.db`, and `seq` is a non-negative decimal integer, the event stream position. A cursor is opaque to consumers (they store and return it verbatim). The grammar is `^v1\.[a-z2-7]{8}\.[0-9]{1,18}$`.
- **Label:** `key=value`; the key matches `^[a-z][a-z0-9_-]{0,31}$` and the value `^[A-Za-z0-9._:@/#+-]{1,128}$` (design guide section 9; no key has built-in meaning).
- **Time:** every stored and printed timestamp is UTC as `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Timestamps read from forgectl are converted to that form. `--since`-style durations take `0` or an integer and a unit: `ms`, `s`, `m`, `h`, `d`.

### Watches, owners and members

- A **per-PR watch** follows one PR. `watch add <pr>` resolves it and records the watch.
- A **query watch** follows a query. Version 0.x has one query, `--mine --open`: the PRs authored by the account forgectl calls "me" on each host in scope. The optional `--host <host>` and `--repo <id>` filters (both repeatable) narrow it and become part of its key. A query's members are the listed items that match its filters: the `--host` list (OR), the `--repo` list (OR), the two lists ANDed; a repo implies its host (a `--repo` whose host is not in the `--host` list is exit 2 `usage` at `watch add`). Hosts in scope are those `forgectl whoami` lists with `token_source` other than `none`; a declared host with `none` is reported `skipped: no_credential`; a per-PR watch's host is always attempted. pr-watch caches the host list for `hosts_ttl` (default 1 hour).
- Every watch has a set of **owners** and a set of **labels**. A label belongs to the (watch, owner) pair that added it and is removed with that owner's reference; the labels of a watch are the union over its owners; adding an owner or a label that is already present is a no-op. `watch remove` removes only the named owner's reference (and its labels); the watch ends when its last owner is gone. An event's labels are the union over every watch that includes the PR, as they were at detection; a label added later does not reach stored events.
- A query watch has **members**: the PRs its enumeration found open. A member is a PR row (State) that lists the query watch among its sources. A PR can be a member of several query watches and the subject of a per-PR watch at once; it then has one row, one digest and one event stream.
- A PR row is dropped when none of its sources remains (all its per-PR watches were removed and it is no longer a member of any query watch that still exists). Its stored events stay until retention.

### PR status

Each stored PR has a status:

- `active`: polled normally.
- `terminal`: seen `merged` (final) or `closed`. A closed PR keeps its row (digest included) for `expiry.after_terminal` after `terminal_at`. When an enumeration lists a closed PR's row as `open` (the query's open listing, or a per-PR repo listing), the PR is reactivated: `status` becomes `active`, `terminal_at` is cleared, and its next snapshot replaces the digest without events (a silent re-baseline; activity while it was closed is not reported, and a later close emits `closed` again). Repos that hold a terminal-closed per-PR PR inside the expiry stay in the per-PR listing step of the Poll cycle. A terminal PR gets no snapshots and is never `unreachable`.
- `unreachable`: `failures.unreachable_after` consecutive per-PR failures (Failures). Snapshotted only on its host's full pass, and it clears on the first success. Whole-host failures never change PR status.

A terminal PR's row (digest included) is deleted `expiry.after_terminal` after `terminal_at`, and so are the watches that only referred to them (a per-PR watch ends; a member is dropped). The deletion happens in `prune` (so up to a day after the expiry).

### Untrusted text

pr-watch stores and prints no free text from a host: no comment body, title, description or check output. The only host text it handles is one-line: a login, a file path, a check name and a `blocked_by.detail`. forgectl has already replaced control characters and bidi marks in them with `<U+XXXX>` markers; pr-watch passes them through, marks them `x-untrusted` in the schemas, and never puts them in argv or environment variables (hooks receive them on stdin only). Anything that needs the text reads it through forgectl by id (`comments`, `reviews`). Consumers must treat the login, path, check-name and detail fields as untrusted.

Every host-derived one-line field (login, path, check name, `blocked_by.detail`) is truncated to 256 characters at a marker boundary (never inside a `<U+XXXX>` marker) with a trailing `...` kept inside the limit, before it is stored, keyed or validated; truncation never fails a snapshot or a commit; keys and `seen` use ids and hashes of the full value, so truncated values cannot collide into one check. A truncated `path` is not usable for matching. A marker `<U+XXXX>` is not proof that a character was removed (an author can type it); consumers match check names exactly with forgectl's `required` flag and never by substring. The CLI's human output replaces any remaining C0, C1 or ESC character in every host-derived string (defense in depth).

## Detection

### Poll cycle

`poll` is the only command that contacts a host. Cron or a caller runs it; `events`, `list`, `show` and `watch` never do, except the per-PR `watch add`'s baseline (see `watch add`). One cycle:

1. **Preflight.** Resolve forgectl once (absolute `PATH` entries only), check its version, load the host list (`whoami`, cached `hosts_ttl`) (Failures).
2. **Enumerate, one host at a time.** (a) Query watches: for every host in scope that some query applies to, one `forgectl prs --host <h> --author me --state open --limit 1000 --json`. `--repo` filters are applied client-side to the host's list (forgectl rejects `--repo` with `--host`, so no `--author me --repo` call is made). A listing with `complete: false` (more than 1000 open PRs, or a cut) is processed for the PRs it lists, reported as `hosts.<h>.incomplete_listing: true`, and never used to conclude that a member left the set. (b) Per-PR watches: for every repo that has a per-PR watch whose PR is `active` or terminal-closed inside `expiry.after_terminal`, one `forgectl prs --repo <id> --state all --updated-since <w> --limit 1000 --json`, where `w` is the repo scope's `watermark` minus `poll.overlap`. The watermark is host time: the largest `updated_at` of the previous listing (kept when the listing was empty); a scope created by a baseline starts at that PR's `snapshot.updated_at`. A `complete: false` listing makes the cycle snapshot every active per-PR PR of that repo and then sets the watermark to the largest listed `updated_at`. Each listing is written in one transaction (State, transaction 1). Hosts are listed one at a time, never in one call; the snapshots of different hosts then run in parallel pools after all listings.
3. **Members and reactivation.** A listed open item for a query that is not yet a member becomes one (a row with no digest, to be baselined silently by the snapshot). A member absent from a complete open listing is scheduled for a snapshot (it merged, closed, lost its author match or left the filter; `not_found` keeps it a member). A listed open item whose row is `terminal` with kind `closed` is reactivated (PR status). A listed item that is merged or closed (per-PR repo listing) and whose row is active is scheduled for a snapshot (a listing item carries no merge data). A listed item that is terminal and not a member is ignored.
4. **Choose snapshots.** A PR is snapshotted when any of these holds, once per cycle: (a) it has no digest (a member just found, or added with `--no-baseline`); (b) `listed_updated_at` is greater than `snap_updated_at`; (c) it is unsettled and `now - unsettled_since` is below `ci.pending_max_age` (default 6h); after that it is snapshotted only on full passes and `show` prints `ci_stuck: true`; (d) it is a member scheduled by step 3; (e) the host is due for a full pass (every `poll.full_every` polls that reached it; `--full` forces it) and the PR is `active`; (f) it is `unreachable` and the host is on a full pass. Order: (a), (b), (c), (d) first, then (e) and (f); inside a group, oldest `last_polled_at` first. The cycle stops starting snapshots after `poll.max_duration` (default 15m): the report has `truncated: true` and `deferred: n`; deferred PRs keep their triggers and go first next cycle. `snap_updated_at` is set to the larger of the snapshot's `updated_at` and the `listed_updated_at` read when the snapshot started. A changed-PR mark is never separate from a successful commit: the trigger is a persisted comparison, so a failed or crashed snapshot is retried next cycle without any extra mark.
5. **Snapshot.** `forgectl snapshot <pr> --json` through a pool of `poll.concurrency` workers per host (Pacing and backoff), each PR under its own lease (Locks and commit). Each result is classified (Failures), then turned into a digest and diffed against the stored one (Digest, Events).
6. **Commit.** All events and the new digest of one PR are stored in one compare-and-set transaction (Locks and commit); the sequence numbers are assigned inside it. A crash before commit loses nothing and repeats the work; after commit the events are durable.
7. **Hooks.** After each PR's commit, its `on-<event>` hooks are queued to a bounded pool inside the poll process (Hooks); the poll exits when the pool drains.
8. **Report and housekeeping.** The poll report is printed; `post-poll` fires when any error occurred or a PR became unreachable; `prune` work (retention, expiry) runs at most once a day and `reconcile` at most once an hour at the end of a `poll`.

A poll never writes to a host.

### Locks and commit

- A lease is per PR, taken when that PR's snapshot starts (not for all PRs up front) and released at its commit or failure. Row: holder pid, start time, boot id (`/proc/sys/kernel/random/boot_id`, or the boot time on macOS), expiry. The expiry is `3 x forgectl.timeout` (6 minutes by default), renewed after each forgectl call by the worker. A lease is taken over when its holder is dead (pid, start time and boot id do not verify) or it is expired. There is no per-query lease: a query listing writes only through one transaction.
- Every digest write is a compare-and-set: `UPDATE prs SET ... , rev = rev + 1 WHERE pr_id = ? AND rev = ?` inside `BEGIN IMMEDIATE`, with the check that at least one `sources` row for the PR still exists. If either fails the transaction is rolled back, its events are discarded, `prs.skipped_conflict` is counted and nothing is committed. A poll and a `watch add` baseline may therefore both snapshot the same new PR; the second write is discarded. `watch add` and `watch remove` take no lease. `watch remove` of the last source drops the row; a commit in flight for it is discarded by the check.
- `reconcile` never drops a lease whose holder verifies as alive, whatever its expiry. A lease held by a live other poll counts as locked; `already_running: true` is reported when at least one PR needed a snapshot and every such PR was locked.
- `poll.max_duration` (default 15m) stops new snapshots (Poll cycle, step 4).

### What is polled

The cheap trigger is `updated_at` through `prs`. It is reliable for new comments, reviews, pushes, labels, review requests and draft changes, and blind to check completion, thread resolution (GitHub) and, on GitLab, approvals, finished CI jobs and bot-note edits (spikes A and B). That is why an unsettled PR (`ci.state` is `pending`, a CI hold is active, or `blocked_by` holds `mergeability_unknown`, so a late `mergeable` flip is seen) is snapshotted on every cycle until it settles or `ci.pending_max_age` passes, and why a periodic full pass exists. A change that only the full pass can see (a GitLab approval, for example) can therefore be reported up to `poll.full_every` cycles late; the report and `show` print `last_full_at`.

### Digest

The digest is the only per-PR state the diff reads. It holds: `head_sha`, `state` (`open`, `closed`, `merged`), `draft`, `mergeable` (shown in `show`, no event), `blocked_by` (sorted codes as forgectl prints them, a code is `{code}` or `{code: other, detail}`, `detail` kept truncated), `ci` (`state`, `head_sha` (forgectl's `ci.head_sha`), `hold` (the settle counter, 0 when none) and the `checks` map), `review_decision` and `approvals_at_head` (`null` is unknown, never zero), `reviewers` (per `reviewer.id`: `state`, `commit_sha`, `at`, `can_push`), `seen: {comments, reviews}` (sets of ids; ids are unordered; a set, never a watermark), `pushed_at`, `merged_head_sha`, `merge_commit_sha`, `baselined: {reviews, threads, ci}`, `unsettled_since`, and the bookkeeping fields in State. No body, title, URL, branch name or any text beyond the one-line fields.

Checks are keyed by the first 16 hex characters of sha256 of `name`, a NUL and `url`; the digest stores `{name (truncated), state}` per key, never the URL, at most 500 keys (an overflow count beyond).

Part map. PR object (always present): `head_sha`, `state`, `draft`, `mergeable`, `blocked_by`, `pushed_at`, `merged_head_sha`, `merge_commit_sha`. Part `reviews`: `seen.reviews`, `reviewers`, `review_decision`, `approvals_at_head`. Part `threads`: `seen.comments`. Part `ci`: `ci`. The part `review_requests` and `teams_visible` are not used. The schemas are pinned to a forgectl version in `tests/`.

Parts forgectl lists in `incomplete` (`reviews`, `threads`, `ci`; `review_requests` is not used) are neither compared nor overwritten: absence from an incomplete part means nothing, and no event of a part is emitted from a snapshot where that part is incomplete. A PR whose list exceeds forgectl's caps (more than about 5000 comments or reviews, or a GitLab fork pipeline) therefore never produces events of that part; this is permanent and visible. The row keeps `incomplete` as a map part to the number of consecutive snapshots in which it was incomplete. `list` and `show` print `incomplete` (array of part names) and `incomplete_snapshots` (max count); the poll report has `prs.incomplete` (count of PRs with a part incomplete at least `failures.unreachable_after` snapshots in a row) and `post-poll` has `degraded` (the same count). `log.jsonl` gets a `part_incomplete` record (`pr_id`, `pr`, `part`, `snapshots`) written once when the count reaches `failures.unreachable_after`.

A digest is bounded: `seen` holds at most the ids forgectl can list (50 pages per list), checks at most 500.

### State

Everything pr-watch persists is in `state.db` (Files). Tables:

- `meta`: `store_id`, `schema_version`, `pruned_through_seq` (highest seq ever deleted, 0 at creation), `last_prune_at`, `last_reconcile_at`, `hosts_cache` (JSON list of `{host, backend, in_scope}` and `fetched_at`).
- `hosts`: `host`, `backend`, `poll_count_since_full`, `last_full_at`, `last_enumerated_at`, `paused_until`, `pause_step`, `low_water_until`, `last_remaining`.
- `scopes`: `repo`, `watermark` (the host-time watermark of a per-PR repo scope, nothing else).
- `watches`, `owners` (watch, owner), `labels` (watch, owner, label), `sources` (pr_id, watch id), `leases`.
- `events`: `seq` (INTEGER PRIMARY KEY AUTOINCREMENT), `id`, `type`, `pr_id`, `pr`, payload JSON, `detected_at`, labels JSON.
- `prs`: `pr_id`, `pr`, `repo`, `host`, `status` (`active`, `terminal`, `unreachable`), `terminal_at`, `terminal_kind` (`merged`, `closed`), `rev` (integer, +1 on every digest write), `digest` (JSON, or null before the baseline), `snap_updated_at` (host `updated_at` of the last committed snapshot), `listed_updated_at` (largest host `updated_at` an enumeration reported for the PR), `last_polled_at` (last committed snapshot), `last_attempt_at`, `failure_count`, `last_failure_code`, `incomplete` (JSON map part to consecutive-snapshot count), `unsettled_since` (local time the current unsettled stretch began, null when settled).

Definitions. A **source** is a row of `sources`: a per-PR watch, or a query watch that includes the PR. The **settle counter** is the CI-hold counter in the digest (`ci.hold`). A PR is **unsettled** when `ci.state` is `pending`, a CI hold is active, or `blocked_by` holds `mergeability_unknown`. **changed**, in `watch add` results, is true when any stored fact changed.

Transactions. (1) Enumeration of one scope: one transaction writes `listed_updated_at` for known PRs, new member rows and sources, reactivations, `hosts.last_enumerated_at` and, for a per-PR repo scope, the new `watermark`. (2) Snapshot commit: events, digest, `rev` + 1, `snap_updated_at`, `last_polled_at`, `failure_count = 0`, `incomplete`, `status`. (3) Failure: `failure_count`, `last_failure_code`, `last_attempt_at`, `status`. (4) Host state (`paused_until`, `pause_step`, `low_water_until`, `poll_count_since_full`, `last_full_at`) is written after the host's cycle. (5) Hooks write only `log.jsonl`.

### Events

An event is one detected difference. The types and the exact rule for each:

| Type | Fires when | Extra fields |
|---|---|---|
| `pushed` | `head_sha` differs from the stored one. Never keyed on `pushed_at` (a normal push reports a committer date up to minutes early). | `from_sha`, `to_sha`, `forced` |
| `comment` | A general comment id (`gh:ic:`, or on GitLab a non-system note `gl:n:` of a discussion without a position) is not in `seen.comments`. Edits, minimizing and deletions are not events. | `comment_id`, `thread_id`, `author` |
| `review_comment` | An inline comment id (`gh:rc:`, or a non-system note of a positioned GitLab discussion) is not in `seen.comments`. | `comment_id`, `thread_id`, `author`, `path`, `line`, `side`, `commit_sha` |
| `review` | A review id (`gh:r:`) is not in `seen.reviews`. GitHub only; GitLab has no review objects and never emits it. | `review_id`, `author`, `review_state` |
| `approved` | A reviewer's `latest_by_reviewer` entry has state `approved` and the stored entry for that `reviewer.id` was not `approved`, or its `at` differs, or (both `at` null) its `commit_sha` differs. One event per reviewer per snapshot. A new approval after a push (same state, new `at` or `commit_sha`) fires again. The event is not proof that the head is approved: on GitLab `commit_sha` and `approvals_at_head` may be null (unknown), and `can_push` says whether the approval counts; consumers that merge on it read forgectl. | `reviewer`, `commit_sha`, `at`, `can_push`, `approvals_at_head` |
| `changes_requested` | Same key as `approved` with state `changes_requested`. A dismissal (state `dismissed`) is not an event. | `reviewer`, `commit_sha`, `at`, `can_push` |
| `ci` | `ci.state` changes between `pending`, `passing` and `failing` (and to or from `none` after the hold below). One event per change. | `from`, `to`, `checks`, `checks_more`, `counts`, `for_head` |
| `ready` | `draft` goes from true to false. It does not mean mergeable; consumers read `ci` and `blocked_by`. | `blocked_by` |
| `merged` | `state` becomes `merged`. | `merged_head_sha`, `merge_commit_sha` |
| `closed` | `state` becomes `closed` (not merged). | none |

Common fields on every event: `id`, `seq`, `type`, `pr`, `pr_id`, `repo`, `detected_at`, `occurred_at` (below, else `null`), `labels` (the union set), `head_sha` (the digest's head after the snapshot, the head at detection, not the head a comment was made on), `watches` (the ids of the watches that include the PR). `author` and `reviewer` are `{id, login, is_bot}`: `id` from forgectl (nullable, opaque; consumers compare ids, not logins), `login` may be `null`, `is_bot` may be `null`; a consumer must treat `is_bot: null` as not a human. `review_comment` also carries `commit_sha` (the thread's anchor commit, nullable). The event objects are the closed shape of this version; consumers must ignore unknown fields and types and must not validate strictly.

`occurred_at`:

| Type | `occurred_at` |
|---|---|
| `comment`, `review_comment` | the comment's `created_at` |
| `review` | `submitted_at` |
| `approved`, `changes_requested` | the entry's `at` |
| `pushed` | the snapshot's `head.pushed_at` (a committer date, may be early) |
| `merged` | `merged_at` |
| `closed` | `closed_at` |
| `ci`, `ready` | `null` |

`checks` lists in the `ci` event the checks whose key appears, disappears or changes state between the stored and the new digest, ordered by name then key, at most 20 (`checks_more` for the rest), each `{name, from, to}` with null `from` or `to` for a check that appears or disappears; the array may be empty. The event also carries `counts: {passing, failing, pending}` over the counted checks and `for_head` (the sha). `passing` means no counted registered check is failing or pending and one passed; it does not say every expected check registered, and a consumer that gates an action pins the head and re-reads `ci` through forgectl. `forced` is computed from `compare <pr> --from <stored head> --to <new head>` only when `detect_force_push` is true: `diverged`, `behind` or `unavailable` is `true`; `ahead` is `false`; `identical`, exit 20 (`too_large`) or a failed call is `null`; the call returns the diff on `ahead` (cost, bounded by `forgectl.max_output_mib`). `detect_force_push` defaults to false; consumers that need `forced` set it per repo.

Rules that apply to all types:

- **Baseline.** The first snapshot of a PR (a per-PR `watch add`, or the first snapshot of a query member) fills the digest and emits nothing, with one exception: a PR already `merged` or `closed` at that moment emits that one event once, and then expires normally. Baselines are per part: the digest keeps `baselined: {reviews, threads, ci}`; a part that is complete in a snapshot and not yet baselined is filled silently and flagged; a part listed in `incomplete` stays unbaselined and is baselined silently by the first snapshot where it is complete. Comments and reviews that exist at the baseline are never events (consumers read current state through forgectl on first sight).
- **Several changes in one snapshot** produce several events, in this fixed order: `pushed`, `review`, `comment`, `review_comment`, `approved`, `changes_requested`, `ci`, `ready`, then `merged` or `closed`; inside a type by ascending `(created_at, id)` of the source item (`approved` and `changes_requested`: by `reviewer.id`, then `at`; `ci` and `ready` are single). No order holds across PRs except by `seq`.
- **Comment unit.** The event unit is a comment id: `gh:ic:` fires `comment`; `gh:rc:` fires `review_comment`; on GitLab each non-system note (`gl:n:`) of a discussion fires `comment` when its discussion has no position and `review_comment` when it has one, so a reply in an old discussion fires. `thread_id` is the thread's id. `seen.comments` holds every comment id found in `threads` (all kinds); `seen.reviews` holds `gh:r:` ids. A comment first seen with `minimized: true` is added to `seen` without an event; later minimizing of a seen comment is not an event.
- **CI hold.** A move of `ci.state` into `none` is held for `ci.settle_polls` further snapshots (0 means no hold). The counter starts when `none` is first seen and is reset to 0 if the state leaves `none` before it expires; while held, the digest keeps the previous `ci.state` and its `head_sha` (so `list` and `show` print `ci_state: null` and `ci_stale: true` whenever `ci.head_sha` differs from the digest's `head_sha`, with `ci_head_sha`) and the PR is unsettled (snapshotted every cycle). A move out of `none` is not held. `passing` is never inferred from `none`. No `ci` event is emitted from a snapshot whose `ci` part is incomplete (the stored state is frozen).
- **GitLab.** `review` never fires. `approved` and `changes_requested` come from `latest_by_reviewer` on both hosts. `approvals_at_head` is `null` where the project does not reset approvals on push, which means unknown. A GitLab `outdated` value is not used.
- **Terminal.** After `merged`, a PR is final. After `closed`, a reopen is a silent re-baseline (PR status).
- **No per-snapshot event cap.** forgectl bounds every list to 50 pages (about 5000 items), so the events a PR can produce from ids are bounded; the cost of a flood is hook starvation, handled by the per-PR sub-cap (Hooks), and `events --limit` paging.

### Pacing and backoff

- **Host pause.** A `rate_limited` failure with `retry_after` pauses that host until then. Without `retry_after` the pause is 1 minute and doubles on each further `rate_limited` result up to 15 minutes (`pause_step` is persisted in `hosts`); a success resets it. A paused host is skipped (reported as `paused_until`), other hosts continue.
- **Low water.** When forgectl reports a numeric `rate_limit.remaining` below `poll.host_low_water` (default 100) for a host (the lowest value of that host's `prs` and snapshot results in this cycle, kept in `hosts.last_remaining` with `reset_at` as `low_water_until`), `poll` still lists, and snapshots only the groups (a) to (d) of the Poll cycle; it skips the full-pass groups (e), (f) and the `complete: false` fallback until `reset_at`. `--full` overrides low water, not a host pause. A host that reports `null` (a self-hosted GitLab without limit headers) is never low-water limited.
- **Pool.** `poll.concurrency` workers per host: `hosts.<h>.concurrency`, else `poll.concurrency` if set, else 4 on GitHub and 2 on GitLab (backend from `whoami`). The same holds for `full_every` (6 on GitHub, 12 on GitLab).
- **Cost accounting.** `requests_estimate` is `snapshots x 7` (GitHub) or `snapshots x 21` (GitLab) plus one per `prs` page called, an estimate. A GitHub snapshot is 5 to 8 requests (5 when forgectl's `hosts.<host>.me` is set); a GitLab snapshot was 19 to 22 requests and 11 to 14 seconds in the design spikes.

### Failures

forgectl results are classified by exit code and error `code`:

| forgectl outcome | Class | Action |
|---|---|---|
| exit 0 object | success | commit; `incomplete` parts as in Digest |
| exit 12 `rate_limited` (with or without `retry_after`) | retryable, host pause | counter +1; the pause rule (Pacing and backoff) |
| exit 12 `network_error`, `upstream_error`, unparseable stdout, a missing required field (local code `contract`), exit 1 `internal`, any unknown code, negative return code or 137 (`killed`), `too_large` (local, over `forgectl.max_output_mib`) | retryable per PR (`too_large` counted like the others) | keep the digest, counter +1, tried next cycle; `unreachable` after `failures.unreachable_after` |
| exit 10 `not_found`, exit 12 `permission_denied` | permanent for the PR | counter +1; `not_found` means unknown, never closed; same `unreachable` rule |
| exit 12 `auth_required` | whole host for this cycle | host listed in `failed`; no PR counter or status changes; the host's PRs are retried next cycle |
| exit 12 `wrong_host_response` | whole host, stop | `log.jsonl` level error, host marked failed with a prominent report entry and in `post-poll`; the token may have reached another host and should be rotated |
| exit 12 `host_rejected`, `unsupported_host_version` | whole host | as `auth_required` |
| exit 12 `tool_missing` reported by forgectl | backend binary missing | whole-host failure for that backend's hosts with `details.install` |
| exit 20 `unknown_host` | refused | for a query listing: host failure; for a PR: counted permanent |
| exit 20 `too_large` (from `compare`) | none | `forced: null` |
| exit 2 (`usage`, `config_invalid`, `invalid_pr`) | contract for stored data | a stored reference rejected is a bug: counted, `contract` warn; forgectl's own `config_invalid` stops the poll with exit 12 `host_failed` |
| exit 128+N while pr-watch was not signaled | interrupted PR | Signals |
| forgectl binary missing, not executable, `--version` failing | tool | exit 12 `tool_missing` before any work |
| version below the minimum | tool | exit 12 `tool_too_old` |

- **Version rule.** Parse `^forgectl (\d+)\.(\d+)\.(\d+)$` once per process (poll and per-PR `watch add`); accept the same major and a minor at least `forgectl.min_version`'s minor (a constant of the release, read from `forgectl --version`); a newer major, an unparseable string or a non-zero exit is `tool_too_old` (`details.found` is the string or `unknown`).
- **Resolution.** The `forgectl` executable is resolved once per process against absolute `PATH` entries only; the resolved path and the version are logged.
- **Host list.** `whoami` is called per `hosts_ttl`; exit 12 with an object is used (hosts in `errors` are failed this cycle); on a failed call the stale cache is reused with a warning, and with no cache the query watches are skipped this cycle and listed in `failed`.
- **Exit 12 from `poll`** means: forgectl missing or too old, or at least one whole-host failure (`auth_required`, `wrong_host_response`, `host_rejected`, `unsupported_host_version`, backend `tool_missing`, an enumeration that ended `network_error` or `upstream_error`) so that scopes were not listed. Pause, low water, `truncated`, per-PR failures and `incomplete_listing` are exit 0.
- **`unreachable`.** A PR becomes `unreachable` after `failures.unreachable_after` consecutive per-PR failures (default 5); it is snapshotted only on its host's full pass and clears on the first success. Whole-host failures never change PR status. PRs that became unreachable this cycle are named in `post-poll` (`unreachable: [{pr, pr_id, code}]`, at most 50), and `post-poll` fires when any occurred. A flap is not an event.

A failure never produces an event. `closed` and `merged` are never inferred from a failure or from a PR vanishing from a list.

### Signals

On SIGTERM, SIGINT or SIGHUP, pr-watch stops starting work, sends SIGTERM to the forgectl and hook process groups it started (only while their leaders are unreaped), releases its leases, writes `poll_end` with `interrupted: true` and exits 128 plus the number; nothing partial is committed for a PR in flight. A forgectl child exiting 128+N while pr-watch itself was not signaled is an interrupted PR: no counter change, tried next cycle, the poll continues.

## Commands

All commands take `--json`, `--config PATH`, `--verbose`, `--quiet` (Global flags). Every command that prints a PR prints it in `#N` form.

### `watch add`

`pr-watch watch add <pr> [--label k=v]... [--owner O] [--no-baseline]`
`pr-watch watch add --mine --open [--host H]... [--repo ID]... [--label k=v]... [--owner O]`

Adds the owner's reference to the watch (creating it), and unions the labels. The query form only records the watch (and validates its filters): it never baselines, and the first `poll` finds and baselines its members silently. The per-PR form resolves and baselines synchronously with one `forgectl snapshot <pr> --json` (it carries the canonical `pr` and `pr_id`); with `--no-baseline` it calls `forgectl show` instead and the first `poll` baselines. The write is a compare-and-set: if a row with a digest already exists (a member of a query watch, or another owner's watch, or a concurrent poll baselined it first), the fetched snapshot is discarded, only the source and owner are added, and no events are emitted. An invalid reference exits 2 and a missing PR exits 10, recording nothing; a transient failure exits 12, recording nothing (so a retry is safe). A PR already `merged` or `closed` stores its one event now; `watch add` runs no hooks (a consumer reads the event through `events`), and a re-add after expiry stores the event again (consumers dedupe on `pr_id` and `merge_commit_sha`). The result carries `cursor`: the events head read in the same transaction as the write, so a consumer can capture its starting cursor atomically with the baseline. Adding an existing owner and labels to an existing watch prints `changed: false` and does nothing else; a new owner on an existing watch adds the reference without a new baseline. `--no-baseline` applies to the per-PR form only.

### `watch remove`

`pr-watch watch remove <pr> [--owner O]` and `pr-watch watch remove --mine --open [--host H]... [--repo ID]... [--owner O]`

Removes only the named owner's reference (and the labels that owner added). Idempotent: when the owner holds no reference (including a PR that is not stored or whose watch expired) it exits 0 with `changed: false` and `reason: not_held` or `pr_unknown`. An invalid reference is still exit 2. The watch ends when its last owner is gone; PR rows with no remaining source are dropped with their digests; stored events stay until retention. Expiry removes owners' references without an event. Removing never contacts a host. A query watch is addressed by its exact filters (the key); there is no partial match.

### `poll`

`pr-watch poll [--pr <ref>] [--repo <id>] [--full] [--no-hooks] [--hook NAME=CMD]...`

Runs one cycle (Poll cycle). `--repo <id>` limits snapshots and listings to that repo's per-PR scope plus the host listing's matching items (it advances only that repo's watermark; the host listing still writes `listed_updated_at` for all listed rows). `--pr <ref>` does no listing, forces that PR's snapshot, advances no watermark, and exits 10 when the PR is not stored (members count as stored); under `already_running` the report prints `polled: false`. `--full` snapshots every active PR of the scopes it covers, ignoring the cheap trigger; `--no-hooks` stores events and skips hooks (for a catch-up run); `--hook <name>=<cmd>` (repeatable) adds a per-invocation hook to the configured ones (a static trusted string, never one built from event or host text). Exit 0 when the poll ran, including `already_running` and a partial result where every failed object was classified. Exit 12 as defined in Failures. `poll` is safe to run concurrently; a second one exits 0 with `already_running`.

### `list`

`pr-watch list [--label k=v]... [--owner O] [--pr <ref>] [--status active|terminal|unreachable]`

Prints the watches (id, kind, key or reference, owners, labels, member count), the PRs (canonical `pr`, `pr_id`, status, state, draft, `ci_state`, `ci_head_sha`, `ci_stale`, `ci_stuck`, `head_sha`, `last_polled_at`, `last_full_at`, `incomplete`, `incomplete_snapshots`, failure count, sources), `last_poll_finished_at` (time of the last `poll` that ran to its end, null if never) and per host `paused_until`, `last_enumerated_at`, `last_full_at`. Never contacts a host. `--label` and `--owner` filter watches and, with them, their PRs; `--pr` selects one PR.

### `show`

`pr-watch show <pr>`

One watched PR: its row, its watches and the digest summary (the same fields as in `list`, plus `blocked_by`, `review_decision`, `approvals_at_head`, the reviewers' latest states, and the counts in `seen`) and its most recent 20 events, oldest first. Exit 10 when the PR is not stored. `<pr>` is matched on the canonical `pr` or `pr_id` of a stored row.

### `events`

`pr-watch events [--label k=v]... [--pr <ref>]... [--type T]... [--since CURSOR] [--limit N]`

Prints events after the cursor, oldest first. Never contacts a host and never changes state (non-destructive: each consumer keeps its own cursor). Without `--since`, no event is returned and the cursor is the current head ("from now"). The result carries `events`, `cursor`, `gap`, `more` and `last_poll_finished_at` (time of the last `poll` that ran to its end, null if never, so a consumer can tell "quiet" from "not polled"). `--limit` defaults to 100 (maximum 1000). `--label` is an AND filter over the labels stored on the event; `--type` is repeatable (OR); `--pr` is repeatable (OR): a reference is resolved through stored rows first; if no row matches, through the `pr` and `pr_id` columns of stored events (an event whose `pr` equals the reference supplies its `pr_id`, and then all events of that `pr_id` are returned, whatever name they were detected under), so a PR whose row expired still resolves while its events are retained. Unknown: exit 10.

- `seq` is an AUTOINCREMENT integer (deleted numbers are never reused). `meta.pruned_through_seq` is the highest deleted seq. `gap` is true iff the cursor's `store_id` differs, or its `seq` is greater than the head, or its `seq` is less than `pruned_through_seq`; a cursor equal to `pruned_through_seq`, or at the head after a quiet month, is no gap, and a cursor at 50 with an empty table and head 100 is a gap. The head and the rows are read in one read transaction.
- `cursor` in the result is the head read in that transaction when `more` is false (everything up to it was examined, so filters do not rescan), and the `seq` of the last returned event when `more` is true. With a gap and a backlog the same rules apply and the filters apply to the backlog. With a gap, the events from the oldest retained one are still returned (all of the current store when the id differs); the consumer must re-sync current state through forgectl and treat the events as possibly incomplete.
- `--limit` outside 1 to 1000, an unknown `--type`, an invalid `--label` or a bad cursor is exit 2 (`code: usage`, `details.kind`: `bad_limit`, `bad_type`, `bad_label`, `bad_cursor`).
- Output without `--json` is human text with no stability guarantee; tests and consumers use `--json`.

### `label`

`pr-watch label add <pr|--watch ID> k=v... [--owner O]` and `label remove <pr|--watch ID> k=v... [--owner O]`

Adds or removes labels on a watch (a watch is named by its id, or by a per-PR watch's reference), owned by the owner (Identifiers). `label add` needs the owner to hold a reference (else exit 10, `details.kind: owner_not_held`) and a watch (a PR with no per-PR watch: exit 10, `details.kind: no_watch`); adding a label the owner already has is a no-op. `label remove` of an absent label is exit 0 with `changed: false`. Labels already stored on past events do not change.

### `prune`

`pr-watch prune [--dry-run]`

Deletes events older than `retention.events`, rows past `expiry.after_terminal`, and the watches and members that only referred to them (so expiry happens up to a day after `expiry.after_terminal`). It also runs at most once a day at the end of `poll`, and runs `PRAGMA wal_checkpoint(TRUNCATE)`.

### `reconcile`

`pr-watch reconcile`

A defensive repair against the store itself (there is no host to compare): drops members whose query watch no longer exists, orphaned digests and leases of dead holders, and reports what it changed; on a healthy database it reports `changed: false`. Idempotent. It runs at the end of `poll` at most once an hour; it can also be run by hand.

### `cron`

`pr-watch cron install [--every D]` and `cron uninstall`: `cron install` writes a tagged block: a first line `# pr-watch managed v1 every=<D>`, then one entry running `<absolute path of pr-watch> poll --quiet`. `--every` wins over `poll.interval_hint`; accepted forms: `Nm` where N divides 60 (`*/N * * * *`; `60m` is hourly) and `Nh` where N divides 24; anything else is exit 2. The block has one entry because `reconcile` runs at the end of `poll`. Validation: refuse any value containing a newline, `%`, quotes or `;`; paths without whitespace; `PATH` holds `~/.local/bin` and the absolute directory of the resolved `forgectl` only if it is owned by the user or root and not group- or world-writable; `cron.env_file` must be owned by the user and not group- or world-readable, and is written quoted. The crontab read-modify-write holds an `flock` on `state/cron.lock` and re-reads before writing; the result prints `replaced` (bool) and `previous_every`. The harness's `awkit cron install` writes the same block and each replaces the other's.

### `export`

`pr-watch export [--since T]`

Prints the stored events as JSON lines (`schemas/log.json` describes `log.jsonl`; this is the event stream). pr-watch takes no external action, so it writes no `audit.jsonl` and `export` has no audit records to print.

### Global flags

`--json`, `--config PATH`, `--verbose`, `--quiet`, `--version`. `--dry-run` exists on `prune`; `watch add` and `watch remove` do not take it (they write only local state). `--yes` and `--actor` are not accepted: no command takes an external action or asks for a confirmation.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including `already_running`, a no-op, an idempotent removal and a clean partial poll) |
| 1 | unexpected or internal error |
| 2 | usage error (bad flag, bad cursor, invalid PR reference, invalid config, owner required) |
| 10 | not found (PR not stored or not watched, no watch to label, PR missing on the host) |
| 12 | failed for a reportable reason (host unreachable or failing and a whole host failed leaving scopes unlisted, forgectl missing or too old, a forgectl result violating the contract in a single call, store too new, state unsafe) |
| 20 | refused (a forgectl refusal passed through, for example `unknown_host`, with its `reason` and `lower_error`) |

Codes 11, 13 to 19 and 21 are unused: pr-watch has no pre-hooks, no pending store and no session. A signal ends the process with 128 plus its number (Signals).

## Output by exit code

- **0:** the command's result object (`schemas/cli/<command>.json`); with `--json` always valid JSON on stdout.
- **12 from `poll`:** the normal poll report (`failed` lists what was left unchecked) and, on stderr, one line per error. Exit 12 for forgectl missing or too old, `store_too_new` or `state_unsafe` prints the error envelope instead.
- **Any other non-zero exit:** the error envelope on stdout (and a text line on stderr).

### Error envelope

`{"error": {"code": "...", "message": "...", "reason": "...", "details": {...}, "lower_error": {...}}}` (`schemas/cli/error.json`). `code` is one of `usage`, `config_invalid`, `invalid_pr`, `not_found`, `host_failed`, `tool_missing` (with `details.install`), `tool_too_old` (with `details.required` and `details.found`), `contract` (a forgectl result violated the documented shape; per call, never exit 12 for a poll alone), `store_too_new` (a database from a newer version, exit 12), `state_unsafe` (exit 12, Files), `refused` (with `reason`), `internal`. `lower_error` carries forgectl's own `code`, `reason`, `retry_after` and `message` when a forgectl call caused the error. Messages are sanitized one-line text.

### Result objects

The result objects of `watch add`, `watch remove`, `label add|remove`, `prune`, `reconcile`, `cron install|uninstall`, `list` and `show` are defined by `schemas/cli/<command>.json`, which are normative for their field names. Rules that go beyond the schemas: every mutating command prints `changed` (true or false); a PR stored without a digest (added with `--no-baseline`, not yet polled) prints `null` for `state`, `draft`, `ci_state`, `head_sha` and `last_polled_at`; `events.more` is always present; a `poll` that exits 0 with `already_running` still prints the full report shape with zero counts.

### Poll report

`poll --json` prints: `already_running`, `started_at`, `duration_ms`, `hosts`, `prs`, `events`, `hooks`, `failed`, `full_pass`, `truncated`, `deferred` and, with `--pr`, `polled`. Definitions:

- `hosts.<h>`: `queried` (a listing was made), `snapshots`, `skipped` (`no_credential`, `not_in_scope` or null), `incomplete_listing`, `paused_until`, `last_full_at`, `requests_estimate` (Pacing and backoff), `errors` (codes).
- `prs`: `snapshotted` (successful snapshots), `unchanged` (snapshots that produced no events), `skipped_locked`, `skipped_conflict`, `unreachable` (PRs that became unreachable this cycle), `incomplete` (Digest).
- `events`: counts by type in the event order. `hooks`: `run`, `skipped`, `failed`. `failed`: the PRs, scopes or hosts left unchecked, each with the failure code. `full_pass` is true when any host did a full pass. `truncated` and `deferred` are the `poll.max_duration` outcome (Poll cycle, step 4). `polled` is whether the `--pr` PR was refreshed.
- `list` and `show` rows carry `last_polled_at` (a row's last committed snapshot) and `last_full_at` (the host's, copied).

Exit codes are as in Failures. Golden poll reports are part of the test suite.

## Hooks

Hook points (design guide section 8). All are post-hooks that report a change that already happened: they cannot veto, escalate or undo anything, and their failures are logged and warned, never fatal.

| Hook | Fires | Payload |
|---|---|---|
| `on-pushed`, `on-comment`, `on-review_comment`, `on-review`, `on-approved`, `on-changes_requested`, `on-ci`, `on-ready`, `on-merged`, `on-closed` | once per stored event of that type, after its transaction committed (never from `watch add`) | the event (Events) plus `tool`, `hook`, `object_type: pr`, `object_id` (the `pr_id`) and `event_id` |
| `post-poll` | once at the end of a `poll` that had any error or any newly unreachable PR | counts, per-host error codes, `degraded`, `unreachable` (pr, pr_id, code), no host text; its `object_id` is the poll's `started_at` |

- **Delivery** is at-most-once. Events are committed first; hooks run afterward, one invocation per event. If `poll` is killed, the events are safe but their hooks are not re-run; a consumer that needs a guarantee reads `events` with a cursor. The payload carries `event_id` (the same value as the event's `id`) as the deduplication key.
- **Timing.** After each PR's transaction commits, its hooks are queued in event order. A pool of `hooks.concurrency` workers (default 4) runs them while snapshots continue; the poll exits after the pool drains, and each hook is bounded by its timeout (default 30 s; SIGTERM to its process group, SIGKILL after 5 s, the group is signaled only while its leader is not yet reaped). `async: true` (the default) means "in the pool, concurrently with other hooks"; `async: false` means "one at a time, in queue order, never concurrently with another `async: false` hook". Hook results are ignored. Order across hooks of one PR is queue order only for `async: false`; consumers order by `seq`.
- **Process.** `sh -c`, own session, stdin a pipe (pr-watch ignores SIGPIPE and writes the payload without blocking the coordinator, with a deadline), stdout and stderr to `/dev/null`, `close_fds`, no template substitution. A hook never receives untrusted text on argv or in the environment.
- **Environment.** Clean: `PATH`, `HOME`, `LANG`, `TERM`, `XDG_*`, `PR_WATCH_HOOK=<hook>`, `PR_WATCH_ID=<object id>` (the `pr_id`; for `post-poll` the poll's `started_at`), `PR_WATCH_EVENT_ID=<event id>` (empty for `post-poll`), `PR_WATCH_OBJECT_TYPE` (`pr` or `poll`), plus names in `hooks.env_allow`. They never include forgectl's token variables. This is stricter than design guide section 8 (which says hooks inherit the environment minus token variables); the deviation is intended.
- **Cap.** At most `hooks.max_per_poll` (default 50) hook invocations start in one poll, and at most `hooks.max_per_pr_per_poll` (default 10) per PR, so a flooded PR cannot use the whole budget. The rest are skipped; one `hook_cap` log record names the number skipped and the first and last skipped event ids; the report counts them; their events are stored.
- **Logging.** Every hook run writes a `hook_run` record (event id, hook, exit code or `timeout`, duration). `post-poll` and `--hook` runs are hooks and count toward the cap.
- **Hook authors.** Never interpolate payload fields into a shell command line or a template; use `jq -r ... | tool --stdin`. A hook receives labels and watch ids in the payload and filters on them itself.

Payload shapes are in `schemas/hooks/<hook>.json`; the event object is `schemas/event.json`. Every field that carries host text (`author.login`, `reviewer.login`, `path`, check names, `blocked_by.detail`) is marked `x-untrusted`.

## Config

File `~/.config/pr-watch/config.yaml` plus `config.d/*.yaml` (design guide section 5). Keys (schema `schemas/config.json`, example `config.example.yaml`; every value shown is the default, and unset keys use the stated built-ins):

```yaml
poll:
  interval_hint: 5m        # pacing and the cron install line only
  # full_every: 6          # cycles between full passes, per host; unset: 6 on GitHub, 12 on GitLab
  # concurrency: 4         # snapshot workers per host; unset: 4 on GitHub, 2 on GitLab
  host_low_water: 100      # forgectl rate_limit.remaining below this: snapshot only what must be
  overlap: 60s             # subtracted from the host-time watermark of a per-PR repo scope (covers search index lag)
  hosts_ttl: 1h            # how long whoami's host list is cached
  max_duration: 15m        # the cycle stops starting snapshots after this
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
  pending_max_age: 6h      # an unsettled PR is snapshotted every cycle for at most this long
forgectl:
  timeout: 120s            # forgectl --timeout per call
  max_output_mib: 32       # bytes read from one forgectl call (1 to 256)
hooks:
  max_per_poll: 50
  max_per_pr_per_poll: 10  # 1 to 1000
  concurrency: 4           # 1 to 32
  env_allow: []
  # on-approved: [{cmd: "...", timeout: 30, async: true}]
cron:
  env_file: null
repos:                     # per-repo overrides (design guide section 5)
  # <host/path>: {detect_force_push: true, ci: {settle_polls: 2}, failures: {unreachable_after: 8}}
```

A hook's `timeout` is an integer number of seconds; every other duration is a string (`5m`, `30d`). `retention.events` below `30d` (or not a whole number of days) is invalid config (exit 2, `code: config_invalid`), not clamped. `forgectl.min_version` is a constant of the release, not a config key. `me.name` is an optional key (the default owner's name). Built-in per-host defaults when not set: `full_every` 6 on GitHub and 12 on GitLab, `concurrency` 4 on GitHub and 2 on GitLab (backend from `whoami`). Per-repo overrides apply to `detect_force_push`, `ci.settle_polls` and `failures.unreachable_after`. The identity ("me") is forgectl's: set `me` and `hosts.<host>.me` in forgectl's config (setting `hosts.<host>.me` saves one request per snapshot on GitHub); pr-watch has no `me` key of its own, apart from `me.name` used for the default owner. Invalid config fails fast naming the key. pr-watch holds no credentials: git-host access is forgectl's.

- **Precedence:** `repos.<id>` and `hosts.<h>` entries, then `PR_WATCH_*` environment, then the config files (`config.d` after the main file), then built-ins; no CLI flag overrides a config key (only `--hook` adds a hook).
- A config file or `config.d` directory owned by another user or writable by group or others is refused (exit 2 `config_invalid`).
- `hooks.env_allow` names starting with `PR_WATCH_`, `LD_` or `DYLD_` are rejected (`config_invalid`); names containing `TOKEN`, `SECRET`, `KEY` or `PASSWORD` produce a `config_warning`.

## Environment variables

Config keys may be set as `PR_WATCH_<KEY>` (nested keys joined with `__`, for example `PR_WATCH_POLL__CONCURRENCY`). Only scalar keys of `poll`, `retention`, `expiry`, `failures`, `ci`, `forgectl`, `detect_force_push`, `hooks.max_per_poll`, `hooks.max_per_pr_per_poll`, `hooks.concurrency` and `me.name` can be set this way; never `hooks.<name>` lists, `hooks.env_allow`, `cron.*`, `hosts` or `repos`. Booleans are `true` or `false`, integers decimal, durations as in the file. `PR_WATCH_HOOK`, `PR_WATCH_ID`, `PR_WATCH_EVENT_ID` and `PR_WATCH_OBJECT_TYPE` are set for hooks only, are not config keys and are ignored by the loader (so a hook that runs `pr-watch` is not confused); any other unknown `PR_WATCH_*` name is ignored with a `config_warning` record. pr-watch passes its own environment through to forgectl unchanged (forgectl reads its token variables from it); `FORGECTL_GH_BIN` and `FORGECTL_GLAB_BIN` therefore apply. A required variable that forgectl needs and is missing surfaces as forgectl's `auth_required`.

## Audit and log

pr-watch takes no external action, so it writes no `audit.jsonl`. `log.jsonl` (`schemas/log.json`) is a public, append-only record of polls (start, end, per-host summary), snapshot failures, state changes of a PR (`status` transitions), hook runs (event id, hook, exit code, duration), `hook_cap`, `part_incomplete`, `commit_skipped`, host failures and configuration warnings; it never holds host text beyond the one-line fields. `log.jsonl` rotates at 10 MiB, keeping 3 files. A `snapshot_failed` record is written for the first failure of a streak, when its code changes, and when the count reaches a multiple of `failures.unreachable_after`; hook runs and `part_incomplete` are one record each. The event stream itself is the product: it lives in `state.db`, is read through `events`, and `export` prints it.

## Files

- `~/.config/pr-watch/config.yaml`, `config.d/*.yaml`.
- `~/.local/state/pr-watch/state.db`: private (SQLite, WAL, versioned additive migrations); the tables are listed in State. No alias table is needed (Identifiers).
- `~/.local/state/pr-watch/log.jsonl`: public.
- pr-watch creates directories with umask 077 (directories are mode 0700, files 0600); on start it checks the state and config directories and `state.db`: not owned by the user, group- or world-writable, or a symlink is refused (exit 12, `code: state_unsafe`). `-wal` and `-shm` inherit the database mode. SQLite settings: `busy_timeout` 5000 ms, `journal_size_limit` 64 MiB, `wal_checkpoint(TRUNCATE)` at the end of `poll` and `prune`; queries are parameterized. XDG variables are respected; macOS uses the same paths.

## Labels

Design guide section 9 applies to watches: repeated `--label key=value` on `watch add`, `label add` and `label remove`, a stored set owned by the (watch, owner) pair that added each label (Watches, owners and members), returned verbatim in `--json`, filtered with repeatable `--label` (AND). A PR's labels on an event are the union over its watches at detection time. No key has built-in meaning. Convention for consumers: label your watches with a key that names you (for example `consumer=review-respond`), so one `events --label` call selects your PRs, and a consumer's labels vanish with its reference.

## TUI

None in 0.x. A `browse` TUI, if wanted, is a 1.x item and follows design guide section 10.

## Consumer notes

- Events are edge-triggered hints, not state: they can be late (up to `full_every` cycles), absent (at-most-once hooks, incomplete parts, a reopened PR), duplicated across a store reset, and never authorization. Readiness changes with no event (a thread resolved, `blocked_by`, `mergeable`, a base moved, a withdrawn approval, a late GitLab approval) mean a consumer that merges or notifies on readiness re-reads forgectl `show` and re-evaluates on a schedule as well as on `approved`, `ci`, `ready` and `pushed`, and pins the head sha it evaluated (`--expected-head`).
- First run: store the head cursor from `events` (no `--since`) before `watch add` (or take the `cursor` from the `watch add` result); existing comments and reviews at first sight never produce events, so a consumer reads current state through forgectl; a consumer keeps a set of handled comment ids (ids are unordered; no watermark).
- A PR's activity before its first snapshot is not an event; a PR opened and commented on within one interval is baselined with the comment already seen.
- Not events in 0.x: a PR opened, reopened, retargeted or renamed, a review requested, a PR moved back to draft, an approval withdrawn or a review dismissed, a PR becoming unreachable or recovering (`post-poll` names newly unreachable PRs), a mergeability or readiness change. Consumers re-read forgectl.
- `pr` on an event is as of detection; match on `pr_id`. `author` and `reviewer` carry `id` (compare ids, else logins case-insensitively). `is_bot: null` is not a human.
- `watch remove` is idempotent. A re-added expired PR can emit `merged` again.
- `poll --pr` under `already_running` did not refresh the PR (`polled: false`); `events` and `list` carry `last_poll_finished_at`.
- Hook recipe: route only `type`, `pr`, `pr_id`, `repo`, ids and shas into notifications; never author, path or check names; get the link from forgectl.

## Upgrades

- Migrations are additive and run on first use; a database written by a newer version is refused (exit 12, `code: store_too_new`, naming the versions) rather than read.
- `--json` outputs and hook payloads follow semver; new fields are additive (consumers ignore unknown fields). A new event type is a minor change; consumers must ignore types they do not know.
- A new forgectl minimum is a minor change recorded in the changelog and `forgectl.min_version`.
- A deleted or recreated `state.db` creates a new `store_id` (every cursor reports `gap` once). A restored backup keeps its old `store_id`: cursors beyond its head report `gap`, but a consumer whose cursor is below the restored head and which already saw later events can miss events; this is a residual risk, so a restore should be followed by consumers re-syncing.

## Residual risks

- **Late detection.** A change the cheap trigger cannot see (CI completion, GitLab approvals) is found by the unsettled rule or the full pass, up to `poll.full_every` cycles late (about 30 minutes at the defaults on GitHub, an hour on GitLab).
- **At-most-once hooks.** A killed poll leaves stored events whose hooks never ran; a SIGKILL of the poll loses queued hooks; a skipped hook is logged (Hooks, Cap).
- **GitLab approvals.** `approvals_at_head` is `null` (unknown) where the project does not reset approvals on push; an `approved` event there says nothing about the head. GitLab transition shapes for new notes, new checks and head changes were not observed live in the design spikes (the server was quiet) and are a `live` regression item.
- **Enumeration limits.** GitHub search caps at 1000 results and, for a search not scoped to one repository, GitHub may search only a limited number of repositories so results beyond them can be missed while `complete` is true (forgectl's UNVERIFIED risk); a query watch can therefore miss an open PR; `list` prints `last_enumerated_at` per host. A query watch narrowed with `--repo` to a repo that is later renamed or deleted finds no members, and its existing members leave the set and are snapshotted individually. A per-PR repo scope whose first listing is wide (the watermark starts at a quiet PR's `updated_at`) costs up to 10 requests once.
- **Clock drift.** No enumeration uses the local clock; a host clock jump is the host's problem.
- **Edits are invisible.** An edited or deleted comment is not an event; a minimized comment stays in `seen`.
- **Reopen.** A closed PR that is reopened is re-baselined silently: comments, pushes and CI changes that happened while it was closed are not reported, and a PR reopened after `expiry.after_terminal` re-enters as a new silent baseline. A merged PR is final.
- **Large snapshots.** pr-watch reads at most `forgectl.max_output_mib` (default 32) MiB from one forgectl call, kills that child's process group above it, and records a per-PR failure `too_large` (counted toward `unreachable`, never an event). forgectl itself has no cap on its final output (only 32 MiB per host answer and 50 pages per list), so a PR with thousands of long comments is skipped while it stays over the cap; memory is bounded by `poll.concurrency` times the cap times the parsing overhead, so size the pool for it.
- **Persistent incomplete parts.** A part forgectl cannot list completely never produces events (Digest); the condition is visible in `list`, `show`, the report and `log.jsonl`.
- **Early green.** `passing` can be an early green: it does not say every expected check registered (Events).
- **Repo rename.** A repo rename changes `pr` on later events; consumers key on `pr_id` (agent-spawn `pr=` labels hold the `pr` string and must be refreshed by the consumer). GitHub can reuse a renamed repo's old name for another repo; a stale name never authorizes anything.
- **Restore of a backup** (Upgrades) can lose events for a consumer that was ahead of the backup.
- **Same-user processes** can read and edit `state.db`; the event stream is not tamper-proof.
- **forgectl as a boundary.** pr-watch trusts forgectl's sanitizing of one-line text and its classification of failures; a forgectl bug that returns unsanitized text would reach event payloads (the schemas bound the length of every text field to 256 characters).

## Platform notes

Linux, WSL and macOS behave the same; nothing here depends on tmux, inotify or a notifier. cron needs `forgectl` on its `PATH` (`cron install` writes the directory when it is safe). WSL: keep the state dir on the Linux filesystem, not under `/mnt/c`.
