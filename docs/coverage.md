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
| commands | the leaf commands of the argparse parser (`build_parser`) | runs the command through `main()` and checks its result or error |
| flags | every long flag of every leaf command, plus the global flags `--json`, `--config`, `--verbose`, `--quiet`, `--version` (`* <flag>`) | passes the flag; at least one of the named tests mentions the flag in its own body |
| schemas | `schemas/cli/*.json`: the `--json` result of each command and the error envelope | validates real output against the schema (`assert_valid`), or compares it to a golden |
| hooks | `schemas/hooks/*.json`: the 20 `on-<event>` payloads and `post-poll` | builds the payload and validates it against its schema; golden files in `tests/golden/hooks/` |
| events | `event_type` in `schemas/defs.json` | validates an event of that type, as the poll or the diff produced it, against `schemas/event.json` |
| log | the `oneOf` branches of `schemas/log.json` | writes or provokes a record of that `event` and validates it |
| errors | `error_code` in `schemas/defs.json` | the envelope golden of that `code` (`tests/golden/cli/error-*.json`) and its exit code, plus a command that fails with it |
| exit codes | the table in interface.md `Exit codes`, plus the `128+N` signal row | a command that ends with that code |

## How the coverage test works

`tests/coverage_map.py` maps each item to test node ids (`tests/<file>::<function>`). `tests/test_interface_coverage.py` then fails when:

- an item has no entry, or an entry names an item that no longer exists (add the new item's tests to the map in the same change that adds the item);
- a named test does not exist, or does not mention the item. Flags are checked in the test's own body; the other categories in its module, because their schema checks sit in shared helpers beside the tests. A renamed or emptied test cannot keep a stale claim alive;
- an item is excused as uncoverable without a reason (`UNCOVERABLE`, keyed `<category>: <item>`, the reason naming `tests/live` when a real host is needed);
- interface.md and the parser disagree about a command's flags (the `pr-watch <command> ...` synopses of the Commands section against the parser);
- the table above is stale.

The audit tests itself: it is run on maps with a missing item, a stale entry, a ghost test, a test that does not mention its item and an empty list, and must report each.

The map names tests that exist; it does not run them. They run in the normal suite, so a failing named test fails `make verify`.

## README examples

`tests/test_readme_examples.py` extracts every `pr-watch` line from the README's fenced shell blocks and runs it through the installed `pr-watch` script, in README order so that state flows, against the fake forgectl in a scratch HOME. It asserts the exit code and, where the README shows output, its shape: the `watch add --json` result validates against its schema and its `cursor` feeds the `events --since` line, which validates and reports no gap. Synopsis lines (`[--every 5m|1h] [--dry-run]`) are expanded into their variants. Not run, by an explicit reason each (the extractor lists them and the test fails on an undocumented kind):

- shell comment lines (the numbered steps);
- the installer line (`uv tool install ...`), which would install software;
- non-shell blocks (the YAML hooks example is instead loaded as a real config by `validate`).

`cron install` and `cron uninstall` are only ever run with `--dry-run` (appended when the README line lacks it), and the test asserts that no crontab was written. The `Commands` bullets are inline code, not fenced blocks: a second test checks that every flag they name exists on the command they name.

## What this audit added

Before it, every category already had schema-validated tests except the hook payloads: nine event hooks (`on-approved`, `on-changes_requested`, `on-ci`, `on-closed`, `on-comment`, `on-ready`, `on-review`, `on-review_comment`, `on-merged`) had no payload golden, and eight of them were never validated against their schema. `tests/test_hook_payloads.py` adds a golden and a schema validation for each. The README examples test is new.

## Limits

- Coverage here means a named test exercises the item and checks its output shape or behavior; it does not measure that every rule in interface.md has a test, which the module tests and the spike regressions in [design.md](design.md) do.
- Behavior that needs a real host (the spike regressions) is in `tests/live` and is outside this audit; nothing in the eight categories needs it.
