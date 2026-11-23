# pr-watch

Watches pull requests and merge requests for changes and exposes them as events and hooks.

pr-watch is an awkit L2 (composite) tool. It works fully standalone: it does not need the `awkit` harness, a bus or a daemon. A short `pr-watch poll` (run by cron or by hand) asks the git host what changed, stores one event per difference, and runs your `on-<event>` hooks. Consumers read the events with their own cursor.

This README is a guide. The normative rules, exact JSON shapes and every edge case are in [docs/interface.md](docs/interface.md); the guide links into it instead of repeating it.

**What it is not:**

- Not a notifier: it reports changes through events and hooks and takes no external action.
- Not a writer: it never posts, comments, merges or changes anything on a host.
- Not a credential holder: all host access goes through forgectl.
- Not a text store: events carry metadata and ids, never comment text, titles or descriptions.

## Status

Implemented and released (see [CHANGELOG.md](CHANGELOG.md)); the interface is documented in [docs/interface.md](docs/interface.md) and the design decisions in [docs/design.md](docs/design.md). There is no TUI in 0.x. Platforms: Linux, WSL and macOS ([Platform notes](docs/interface.md#platform-notes)).

## Dependencies

- Python 3.12 or newer.
- forgectl: binding A (hard). All host access goes through it, and it holds the credentials; set up its hosts and tokens first. pr-watch checks its version and exits 12 with an install hint when it is missing or too old. Install: `uv tool install git+https://github.com/agent-workflow-kit/forgectl`.
- cron (optional), only for `pr-watch cron install`.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/pr-watch
```

For development, clone the repository and run `uv sync`, then `uv run pr-watch ...`.

## Quick start

Use `--json` whenever a script reads the output; the human text has no stability guarantee. The hosts and repositories below are placeholders.

```sh
# 1. Watch one PR (it is baselined at once; --owner is required outside a terminal).
#    On a fresh store, take your "from now" cursor from the `cursor` in this result.
pr-watch watch add example.com/group/project#42 --owner my-tool --label consumer=my-tool --json
# 2. Or watch every open PR you authored, on every host forgectl has a credential for.
pr-watch watch add --mine --open --owner my-tool
# 3. Poll (the only command that contacts a host), then read what changed.
pr-watch poll
pr-watch events --since <cursor> --label consumer=my-tool --json
# 4. Run the poll every 5 minutes from cron.
pr-watch cron install
```

`watch add <pr>` baselines the PR synchronously, and a query watch is baselined by its first poll. Neither emits events, except that a PR already merged or closed emits that one event. See [`watch add`](docs/interface.md#watch-add).

### Commands

Every command takes `--json`, `--config PATH`, `--verbose` and `--quiet` ([Global flags](docs/interface.md#global-flags)). A PR is `host/<project path>#N` or a URL.

- `pr-watch watch add <pr> [--label k=v] [--owner O] [--no-baseline]`
- `pr-watch watch add --mine --open [--host H] [--repo ID] [--label k=v] [--owner O]` (`--owner` is required when stdin is not a TTY: tool callers must pass it)
- `pr-watch watch remove (<pr> | --mine --open ...) [--owner O]`
- `pr-watch poll [--pr <ref>] [--repo <id>] [--full] [--no-hooks] [--hook NAME=CMD]`
- `pr-watch list [--label k=v] [--owner O] [--pr <ref>] [--status S]`, `show <pr>`
- `pr-watch events [--label k=v]... [--pr <ref>]... [--type T]... [--since <cursor>] [--limit N]`
- `pr-watch label add|remove <pr|--watch ID> k=v... [--owner O]`
- `pr-watch prune [--dry-run]`, `cron install|uninstall [--dry-run]`
- `pr-watch export [--since D] [--label k=v]... [--type T]... [--pr <ref>]...`

A watch has owners (it ends when its last owner removes its reference) and labels; `watch remove` removes only the named owner's reference. Details: [Watches, owners and members](docs/interface.md#watches-owners-and-members).

## Events and the consumer cursor

Events: `unreachable`, `recovered`, `renamed`, `opened`, `reopened`, `pushed`, `retargeted`, `comment`, `review_comment`, `review` (GitHub only), `review_requested`, `approved`, `changes_requested`, `approval_withdrawn`, `ci`, `ready`, `converted_to_draft`, `mergeability`, `merged`, `closed`. Each carries metadata and ids, never comment text, titles or descriptions: read text through forgectl by id. The rule for each type is in [Events](docs/interface.md#events).

`pr-watch events` never changes state, so every consumer keeps its own opaque cursor:

1. On a fresh store, take your starting cursor from the `cursor` of the first `watch add` result (a cursor taken from an empty store, which has a placeholder `store_id`, reports `"gap": true` after the first `watch add`). Once the store has data, `events` without `--since` returns no events and a cursor for "from now".
2. Call `events --since <cursor>` and store the `cursor` it returns. While `more` is true, call again.
3. Keep a set of the comment ids you have handled, and match PRs on `pr_id` (the `pr` name can change).
4. When a result has `"gap": true` (the cursor is too old, pruned, or from another store), the events returned may be incomplete: re-sync current state through forgectl, then reset your cursor.

Events are hints, not state: they can be late, absent or duplicated, so a consumer that acts on readiness re-reads forgectl too. Read [Consumer notes](docs/interface.md#consumer-notes) before you build one. Events are kept at least 30 days; a PR's state is dropped 7 days after it merged or closed (both configurable).

## Cron

```sh
pr-watch cron install [--every 5m|1h] [--dry-run]
pr-watch cron uninstall [--dry-run]
```

`cron install` writes one tagged block to your crontab: a `PATH=` line and one `pr-watch poll --quiet` entry. `--every` takes `Nm` (N divides 60) or `Nh` (N divides 24) and wins over `poll.interval_hint` (default `5m`). `cron uninstall` removes the block. `--dry-run` shows what would change without touching the crontab or creating anything.

- Lines outside the block are kept byte for byte; a newline is added only after a final line that lacks one.
- A block with a missing end line, an orphan end line or a second block is refused untouched (exit 2) with a hint to repair it by hand.
- `cron.env_file`, if set, is sourced before the poll. It must be an absolute regular file you own with mode 0600, in a directory you own that others cannot write.
- The entry needs a POSIX `sh`: a foreign `SHELL=` line earlier in the crontab breaks the `. env &&` form.
- A block line is at most 999 bytes.
- `poll` already runs `prune` at most once a day, so no second cron job is needed.

Details: [`cron`](docs/interface.md#cron).

## Hooks

`on-unreachable`, `on-recovered`, `on-renamed`, `on-opened`, `on-reopened`, `on-pushed`, `on-retargeted`, `on-comment`, `on-review_comment`, `on-review`, `on-review_requested`, `on-approved`, `on-changes_requested`, `on-approval_withdrawn`, `on-ci`, `on-ready`, `on-converted_to_draft`, `on-mergeability`, `on-merged`, `on-closed` and `post-poll` run a command with a JSON payload on stdin. They are post-hooks: best-effort, at-most-once, capped per poll, and unable to veto anything. A consumer that must not miss an event reads `events` with its cursor; the hook is an early nudge.

```yaml
# ~/.config/pr-watch/config.yaml
hooks:
  on-approved: [{cmd: "my-notifier --stdin", timeout: 30, async: true}]
```

Add a hook for one run with `poll --hook NAME=CMD` (repeatable) and drop all of them with `--no-hooks` (which also drops `--hook` entries). A hook's environment is clean (`PATH`, `HOME`, `LANG`, `TERM`, `XDG_*`, `PR_WATCH_*` identifiers, and names in `hooks.env_allow`); untrusted host text reaches it on stdin only, so never interpolate payload fields into a shell command line. Payload shapes are in `schemas/hooks/`. Details: [Hooks](docs/interface.md#hooks).

## Configuration

`~/.config/pr-watch/config.yaml` plus `config.d/*.yaml` (read in bytewise order); `--config PATH` replaces the main file. Every key, with its default and a one-line comment, is in [config.example.yaml](config.example.yaml), and the schema is `schemas/config.json`. The loader rules, bounds and merging are in [Config](docs/interface.md#config).

- A section such as `hosts:` or `repos:` must be a mapping: write `hosts: {}`, not an empty `hosts:`.
- Durations are strings such as `60s`, `5m`, `30d` (at most `3650d`); a hook's `timeout` is an integer number of seconds. Integers are plain decimal.
- Maps merge across files, hook lists append, and any other value (`hooks.env_allow` included) is replaced by the later file.
- Scalar keys can be overridden as `PR_WATCH_<KEY>` with nested keys joined by `__` (`PR_WATCH_POLL__CONCURRENCY=2`); booleans are `true` or `false`. Hook lists, `hooks.env_allow`, `cron`, `hosts` and `repos` are file-only. pr-watch also reads `PATH` and `XDG_*`, and passes its environment through to forgectl.
- A config file or `config.d` that is owned by another user or writable by group or others is refused.

State lives in `~/.local/state/pr-watch/` (`state.db`, `log.jsonl`); see [Files](docs/interface.md#files).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including `already_running`, a no-op, an idempotent removal and a clean partial poll) |
| 1 | unexpected or internal error |
| 2 | usage error (bad flag, bad cursor, invalid PR reference, invalid config, owner required) |
| 10 | not found (PR not stored or not watched, no watch to label, PR missing on the host) |
| 12 | failed for a reportable reason (a whole host failed, forgectl missing or too old, a forgectl contract violation in a direct command's single call (during a poll, a per-PR failure with exit 0), store too new, state unsafe) |
| 20 | refused (a forgectl refusal passed through) |
| 128+N | ended by signal N (SIGINT, SIGTERM, SIGHUP), quietly |

Codes 11, 13 to 19 and 21 are unused. A poll that exits 12 prints its normal report, except when the cause is forgectl missing or too old, a store too new or unsafe state: that prints the error envelope. Any other non-zero exit prints an error envelope on stdout ([Output by exit code](docs/interface.md#output-by-exit-code)).

## Safety

- pr-watch holds no credentials and never reads a token. forgectl owns host access and credentials; the tokens are in forgectl's configuration or environment.
- It only reads from a host. It never posts, comments, merges or edits, and it keeps no audit log because it takes no external action.
- It stores no free text from a host. The few one-line fields it keeps (a login, a file path, a check name) are untrusted: never put them in a command line.
- State is private (directories 0700, files 0600), and unsafe ownership or modes are refused.

## Documentation

- [docs/interface.md](docs/interface.md): the normative interface
- [docs/design.md](docs/design.md): decisions and structure
- [awkit architecture](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md)
- [awkit design guide](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md)

## License

MIT, see [LICENSE](LICENSE).
