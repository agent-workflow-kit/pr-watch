# pr-watch: interface coverage

This is the 1.0 qualification audit of [interface.md](interface.md) against the tests (issue #91, an item of #43). It is not a snapshot that rots: `tests/test_interface_coverage.py` re-derives every item below from its source of truth on each run and fails when one has no test.

## Result

| Category | Items | Covered | Uncoverable |
|---|---|---|---|
| commands | 14 | 14 | 0 |
| flags | 45 | 45 | 0 |
| schemas | 14 | 14 | 0 |
| hooks | 21 | 21 | 0 |
| events | 20 | 20 | 0 |
| log | 13 | 13 | 0 |
| errors | 12 | 12 | 0 |
| exit codes | 7 | 7 | 0 |

146 items, 146 covered, none uncoverable. The rows are checked against the audit by `test_docs_coverage_md_totals_match_the_audit`, so a new item makes the table fail until it is covered and counted.

What each category is, and where its items come from:

| Category | Source of the items | What a covering test does |
|---|---|---|
| commands | the leaf commands of the argparse parser (`build_parser`) | a test of the named file that mentions every word of the command; the named tests run the command through `main()` or its handler |
| flags | every long flag of every leaf command, plus the global flags `--json`, `--config`, `--verbose`, `--quiet`, `--version` (`* <flag>`) | a named test that mentions the flag in its own body; for flags that change behavior (`watch add`, `watch remove`, `poll`) the named tests run the command and check the effect, not only the parse |
| schemas | `schemas/cli/*.json`: the `--json` result of each command and the error envelope | a named test whose body (or a same-file helper it calls) names the schema, as `assert_valid("cli/<command>", ...)`, on real output or a golden |
| hooks | `schemas/hooks/*.json`: the 20 `on-<event>` payloads and `post-poll` | a named test that names the hook (or builds its name from the event) in its body or its helpers: it builds the payload and validates it against its schema; golden files in `tests/golden/hooks/` |
| events | `event_type` in `schemas/defs.json` | a named test that names the event type in its body or its helpers and validates an event of that type, as the poll or the diff produced it, against `schemas/event.json` |
| log | the `oneOf` branches of `schemas/log.json` | a named test that names the `event` in its body or its helpers: most provoke the record through a poll, a hook run or a config load and read it from the log; `test_every_record_validates_against_the_schema` also validates a synthetic sample of each |
| errors | `error_code` in `schemas/defs.json` | the envelope golden of that `code` (`tests/golden/cli/error-*.json`) and its exit code, plus a second named test that names the code; that one runs a real command that ends with it (through `main()`, the fake forgectl or a crafted state) |
| exit codes | the table in interface.md `Exit codes`, plus the `128+N` signal row | a named test that compares the literal code (`== 12`, `SystemExit`, `128 + signal`) in its body or its helpers; `0` is a command that succeeds, the others are commands that end with the code |

## How the coverage test works

`tests/coverage_map.py` maps each item to test node ids (`tests/<file>::<function>`). `tests/test_interface_coverage.py` then fails when:

- an item has no entry, or an entry names an item that no longer exists (add the new item's tests to the map in the same change that adds the item);
- a named test does not exist, or does not mention the item. Flags are checked in the test's own body. Schemas, hooks, events, log records and errors are checked in the test's own body, its decorators (a `parametrize` list) and, one level deep, the same-file functions and module-level tables it names, as a whole name (`on-ci` is not `ci`, `review` is not `review_comment`). Exit codes are checked as a literal compared in that same scope. Only commands are checked at the module level. A renamed or emptied test cannot keep a stale claim alive;
- an item is excused as uncoverable without a reason (`UNCOVERABLE`, keyed `<category>: <item>`, the reason naming `tests/live` when a real host is needed);
- interface.md and the parser disagree about a command's flags (the `pr-watch <command> ...` synopses of the Commands section against the parser);
- the table above is stale.

The audit tests itself: it is run on maps with a missing item, a stale entry, a ghost test, a test that does not mention its item and an empty list, and on wrong mappings of every category checked in the test's scope (an exit code a test never compares, another hook's golden, another command's schema test, an event or log record the test never names, an error code it never names), and must report each. The hooks are cross-checked against the events (one `on-<event>` each, plus `post-poll`), and the global flags are read from the root parser.

The map names tests that exist; it does not run them. They run in the normal suite, so a failing named test fails `make verify`.

## README examples

`tests/test_readme_examples.py` extracts every `pr-watch` line from the README's fenced shell blocks and runs it through the installed `pr-watch` script, in README order so that state flows, against the fake forgectl in a scratch HOME. It asserts the exit code and, where the README shows output, its shape: the `watch add --json` result validates against its schema and its `cursor` feeds the `events --since` line, which validates and reports no gap. Synopsis lines (`[--every 5m|1h] [--dry-run]`) are expanded into their variants. Not run, by an explicit reason each (the extractor lists them and the test fails on an undocumented kind):

- shell comment lines (the numbered steps);
- the installer line (`uv tool install ...`), which would install software;
- non-shell blocks: the test asserts that the YAML hooks example is the only one (loaded as a real config by `validate`). A block with no language, or `text` or `console`, that holds a `pr-watch` line is run as shell, never skipped; fences may be indented or `~~~`, and one that cannot be read fails the test.

`cron install` and `cron uninstall` are only ever run with `--dry-run` (appended when the README line lacks it), and the test asserts that no crontab was written. The `Commands` bullets are inline code, not fenced blocks: a second test checks that every flag they name exists on the command they name, including the bare spans that follow a `pr-watch` span on the same line (`cron install|uninstall [--dry-run]`).

## What this audit added

Before it, every category already had schema-validated tests except the hook payloads: nine event hooks (`on-approved`, `on-changes_requested`, `on-ci`, `on-closed`, `on-comment`, `on-ready`, `on-review`, `on-review_comment`, `on-merged`) had no payload golden, and eight of them were never validated against their schema. `tests/test_hook_payloads.py` adds a golden and a schema validation for each. The README examples test is new.

## Limits

- Coverage here means a named test exercises the item and checks its output shape or behavior; it does not measure that every rule in interface.md has a test, which the module tests and the spike regressions in [design.md](design.md) do.
- Behavior that needs a real host (the spike regressions) is in `tests/live` and is outside this audit; nothing in the eight categories needs it.
