# pr-watch: implementation plan

This plan implements [interface.md](interface.md) (the normative spec) and [design.md](design.md) (decisions, modules, data model, test strategy). Issues are expanded into bite-sized test-first steps when they are picked up; this plan fixes the stages, the conventions every stage shares and the version roadmap.

## Conventions for every stage

- **Gateway boundary.** Every subprocess call (forgectl, hooks, crontab) goes through `src/pr_watch/gateway.py`. Tests substitute fakes there or through `tests/fakebin/`.
- **Fakes and pinned schemas.** `tests/fakebin/forgectl` is a fake driven by recorded and synthetic `snapshot`, `prs`, `show`, `compare` and `whoami` results (private data removed) plus failure scripts for every class in [Failures](interface.md#failures). forgectl's own schemas for those commands are copied into `tests/` pinned to a forgectl version and used to validate the fake's output. Nothing imports forgectl.
- **Process tests and live tests.** Unit and process tests (kill during commit, concurrent polls, hook timeout, signals) run in CI. `live` tests (real GitHub and GitLab through a real forgectl, the spike regressions in design.md's Live verification table) are deselected by default and never run in CI.
- **Golden files.** Every `--json` output and every hook payload has a golden file and is validated against `schemas/`; the poll report and `list` and `show` outputs are covered with several state shapes.
- **Spike regressions.** Each behavior the design relies on that a spike measured becomes a test: a `live` one where it needs a host, otherwise a fake-driven one.
- **Safety invariants.** No host text beyond the one-line fields in events, state, hooks or logs; no free text in argv or environment; hooks get a clean environment and explicit stdio; every number read from forgectl is bounded; nothing is ever signaled by pattern.
- **`make verify`** (ruff, pyright, pytest, markdown lint) passes before every commit; CHANGELOG and the version are never edited by hand.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `gateway` | 0 | all subprocess access; fakes plug in here |
| `config` | 0 | YAML plus drop-ins, environment overrides, validation against `schemas/config.json` |
| `cli` | 0 | argparse, `--json`, exit codes, error envelope, `--version` |
| `refs` | 1 | validation of every user value before argv; offline reference parsing |
| `forgectl_client` | 1 | argv building, byte-capped `--json` parsing, version check, error classification |
| `store` | 2 | SQLite, additive migrations, leases, compare-and-set commit, event append |
| `watches` | 2 | watch keys, owners, labels, members, expiry |
| `digest` | 3 | building a digest from a snapshot; seen sets |
| `diff` | 3 | digest pair to ordered events |
| `poller` | 4 | enumeration, snapshot selection, pools, pacing, host pause, budgets, report |
| `hooks` | 5 | payloads, the bounded in-process pool, caps, process groups, timeouts |
| `cron` | 6 | the tagged crontab block |

## Stage 0: Skeleton, config and CLI frame

**Goal**: A package that parses its commands, loads and validates config, and emits the error envelope and exit codes, with the gateway and test harness in place.
**Success Criteria**: `pr-watch --version` and `--help` work; every command exists and exits 2 on bad usage; config validates against the schema and fails fast naming the key; `make verify` and CI are green.
**Tests**: config defaults, drop-in merge, per-host and per-repo overrides, env overrides, invalid values; exit-code and envelope golden files; gateway fake plumbing.
**Status**: Not Started

## Stage 1: forgectl client and reference handling

**Goal**: Call forgectl safely and classify every result.
**Success Criteria**: version check against the declared minimum with an install message; stdout read under a byte cap and a call deadline; every failure class mapped as in the failure table; user values validated before they reach argv; offline reference parsing works.
**Tests**: failure matrix against the fake (rate limit with and without `retry_after`, network error, 404, 403, auth, timeout, partial `prs` results, oversized output, contract violation); argv injection cases; `--version` parsing; spike C regressions.
**Status**: Not Started

## Stage 2: State store, watches and the commit protocol

**Goal**: The SQLite state, watches with owners, labels and members, leases and the compare-and-set commit.
**Success Criteria**: migrations create and upgrade the schema; `watch add` for a per-PR watch resolves and baselines through the client; `watch remove`, `label` and `list` behave as specified and idempotently; a lease is taken over when its holder is dead or expired; a commit that lost its source or its revision is discarded; `prune` applies retention and expiry.
**Tests**: owner and label semantics, watch keys, member rules, expiry, takeover (PID reuse, boot id), concurrent `watch add` and `poll`, kill during commit leaves no partial state, newer-database refusal.
**Status**: Not Started

## Stage 3: Digest and diff

**Goal**: Turn a snapshot into a digest and two digests into ordered events.
**Success Criteria**: every event rule in [Events](interface.md#events) holds, including the baseline, the CI hold, incomplete parts, GitLab differences, per-reviewer approval keys, reopen re-baseline and rename by `pr_id`; text fields are bounded and sanitized.
**Tests**: a table-driven diff test per rule; GitHub and GitLab recorded snapshots (spike data without private content); determinism; first-sight and terminal-at-add cases; `forced` with `compare` fakes.
**Status**: Not Started

## Stage 4: Poll cycle

**Goal**: `poll` end to end: enumeration, snapshot selection, pools, pacing, failures, budgets and the report.
**Success Criteria**: query watches list the open set and per-PR repos use the windowed listing as specified; changed, unsettled and full-pass PRs are chosen; per-host backoff and budgets hold; oversized snapshots fall back to `show`; events and the digest commit in one transaction; the report matches its schema; signals leave nothing partial.
**Tests**: scripted multi-cycle scenarios on the fake (first poll with many members, pending CI storm, full pass deferral, host pause, `auth_required` host failure, unreachable PRs, closed and reopened PRs, rename); two concurrent polls; SIGTERM mid-poll; `live` cost and trigger tests.
**Status**: Not Started

## Stage 5: Events command and hooks

**Goal**: `events`, `show` and `export`, and the `on-<event>` and `post-poll` hooks.
**Success Criteria**: cursors, gaps, filters and limits behave as specified; hooks run in the bounded pool after commit with the specified payload, environment, stdio and timeouts; the caps and the exemptions hold; every hook run is logged.
**Tests**: cursor grammar and gap cases (retention, reset, future), filter combinations, hook payload goldens and schema validation, timeout and process-group kill, flood against the caps, an unkillable hook, a hook that closes stdin.
**Status**: Not Started

## Stage 6: Cron, docs and live suite

**Goal**: `cron install|uninstall`, the README and the `live` suite.
**Success Criteria**: the managed block matches the golden text and replaces the harness's block; the live tests pass against GitHub and a GitLab server; README and `config.example.yaml` match the interface.
**Tests**: golden crontab for install, reinstall, uninstall and foreign entries; GitLab transition shapes (the still-to-verify item); macOS checklist.
**Status**: Not Started

## Version roadmap

- **0.x**: stages 0 to 6, in order. Every 0.x release keeps the interface additive.
- **1.0** when: the live suite has passed on GitHub and on a GitLab server, the GitLab transition shapes are confirmed, a real poll loop has run for a week under cron without a stuck PR or a lost event, forgectl is at a stable version, and the macOS checklist is done.
- **1.x (additive)**: the candidates listed in design.md under "Not in 0.1" (`opened`, `reopened`, `review_requested`, back-to-draft, approval withdrawn, retarget, rename, unreachable and recovered, a readiness event), a `browse` TUI, a body-free forgectl projection when forgectl offers one, per-owner event filters if consumers need more, and richer pacing.
- **2.0**: only if the event model changes incompatibly; nothing is planned.
