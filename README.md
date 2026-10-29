# pr-watch

Watches PRs for changes and exposes them as events and hooks.

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L2 (composite). Works fully standalone; it does not need the `awkit` harness.

## What it does

- Detects new commits, comments, reviews, approvals, CI changes, readiness and merge/close on watched PRs.
- Event types include `comment`, `review_comment`, `review`, `approved`, `changes_requested`, `ci`, `ready`, `merged`, `closed`, `pushed`.
- Exposes `on-<event>` hooks per change type (e.g. `on-comment`, `on-approved`, `on-merged`); they report external changes and cannot veto.
- Watches are reference-counted by owner (`--owner <tool>`); query watches (`--mine --open`) are supported.
- `events` is non-destructive: each consumer keeps its own opaque cursor; a cursor older than the retention window returns exit 0 with `"gap": true`.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

- `pr-watch watch add <pr> [--label k=v] [--owner <tool>]`
- `pr-watch watch add --mine --open [--owner <tool>]  (query watch)`
- `pr-watch watch remove (<pr> | --mine --open) [--owner <tool>]`
- `pr-watch poll`
- `pr-watch list`
- `pr-watch events [--label k=v] [--pr <ref>] [--since <cursor>]`

## Dependencies

- forgectl: binding: TBD (A/B/C), decided in this tool's design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/pr-watch
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
