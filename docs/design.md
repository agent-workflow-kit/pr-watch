# pr-watch: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Layer 2 (composite). Watches PRs for changes and exposes them as events and hooks. Works fully standalone (architecture C2).

## Responsibilities (architecture §5.2)

Watches PRs for changes.

- Commands: `watch add <pr> [--label k=v] [--owner <tool>]`, `watch add --mine --open [--owner <tool>]` (query watch), `watch remove (<pr> | --mine --open) [--owner <tool>]`, `poll`, `list`, `events [--label k=v] [--pr <ref>] [--since <cursor>]`.
- Uses: forgectl.
- Notes: Detects new commits, comments, reviews, approvals, CI changes, readiness and merge/close. Event types include `comment`, `review_comment`, `review`, `approved`, `changes_requested`, `ci`, `ready`, `merged`, `closed`, `pushed`. Exposes `on-<event>` hooks per change type (e.g. `on-comment`, `on-approved`, `on-merged`); these report external changes and cannot veto. Watches are reference-counted by owner: `watch add --owner <tool>` adds that owner's reference, and `watch remove --owner <tool>` removes only that owner's reference. `events` is non-destructive: each consumer keeps its own cursor, an opaque string; without `--since`, events start from now. A cursor older than the retention window returns exit 0 with `"gap": true`; the consumer then re-syncs current state through forgectl and resets its cursor. Used by review-watch, review-respond, ticket-work and the harness.

Section numbers above refer to `../awkit-docs/architecture.md`.

## Uses

- forgectl: binding TBD (A/B/C), decided in this design session

## Cross-cutting sections to read

In `../awkit-docs/architecture.md` (plus `../awkit-docs/design-guide.md` throughout):

- §3.4: hooks everywhere (`on-<event>` hooks cannot veto), opaque labels, canonical PR identifiers
- §6: conventions summary (concurrency: per-object lease locks for `poll`)
- §10.1, §10.3: flows A and C, where PR events feed reviews and responses
- §15: notifications: `on-<event>` hooks are notification producers
- §13.3: credentials (git-host access goes through forgectl)

## Constraints (decided)

From architecture §20: The first poll after `watch add` records a baseline and emits no events, except terminal states (`merged`/`closed`) already reached; events are retained at least 30 days (configurable); a watch ends when its last owner reference is removed, and watches auto-expire N days after merge or close.

## Open topics

From architecture §20:

- polling cadence
- change detection
- hook payloads
- cursor encoding

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What polling cadence should `poll` assume, and should it be adaptive per PR state?
2. How are changes detected (snapshot diffing vs host timelines), and what is stored per watch?
3. What exactly do `on-<event>` hook payloads contain for each event type?
4. How is the opaque `events` cursor encoded so that retention gaps are detectable?
