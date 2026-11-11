# pr-watch

Watches pull requests and merge requests for changes and exposes them as events and hooks.

## Status

Designed (interface 0.1.0-design); not implemented yet. See [docs/interface.md](docs/interface.md) (the normative spec) and [docs/design.md](docs/design.md).

## Layer

L2 (composite). Works fully standalone; it does not need the `awkit` harness.

## What it does

- You (or another tool) register watches: one PR, or the query "my open PRs" (`--mine --open`, optionally narrowed by host or repo). Watches are reference-counted by owner and carry labels.
- `pr-watch poll` (run it from cron, every 5 minutes by default) asks the git host for what changed and stores events. It is the only command that talks to a host, and it only reads, through forgectl.
- Events: `pushed`, `comment`, `review_comment`, `review` (GitHub only), `approved`, `changes_requested`, `ci`, `ready`, `merged`, `closed`. Each carries metadata and ids, never comment text, titles or descriptions; read text through forgectl by id.
- `pr-watch events --since <cursor>` is non-destructive: every consumer keeps its own opaque cursor. Without `--since` you get nothing and a cursor for "from now". An old cursor returns `"gap": true`: re-sync current state through forgectl and reset your cursor.
- `on-<event>` hooks (JSON on stdin) report changes to notifiers or other tools; they are best-effort and cannot veto.
- A new watch records a baseline and emits no events, except that a PR already merged or closed emits that one event.
- Events are kept at least 30 days; a PR's state is dropped 7 days after it merged or closed (both configurable).

## Commands

Finalized in [docs/interface.md](docs/interface.md).

- `pr-watch watch add <pr> [--label k=v] [--owner <tool>] [--no-baseline]`
- `pr-watch watch add --mine --open [--host H] [--repo ID] [--owner <tool>] [--no-baseline]`
- `pr-watch watch remove (<pr> | --mine --open ...) [--owner <tool>]`
- `pr-watch poll [--pr <ref>] [--repo <id>] [--full]`
- `pr-watch list`, `show <pr>`, `events [--label k=v] [--pr <ref>] [--type T] [--since <cursor>]`
- `pr-watch label add|remove`, `prune`, `reconcile`, `cron install|uninstall`, `export`

All commands take `--json`. Exit codes: 0 success, 2 usage, 10 not found, 12 failed (host failing, forgectl missing or too old), 20 refused (a forgectl refusal passed through).

## Dependencies

- forgectl: binding A (hard). All host access goes through it; it holds the credentials. Install: `uv tool install git+https://github.com/agent-workflow-kit/forgectl`.

## Configuration

`~/.config/pr-watch/config.yaml` (plus `config.d/*.yaml`); every key is documented in [config.example.yaml](config.example.yaml). pr-watch holds no credentials and reads no environment variable of its own besides `PR_WATCH_<KEY>` config overrides; set up forgectl's hosts and tokens first.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/pr-watch
pr-watch cron install
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
