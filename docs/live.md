# Live tests

The `live` suite in `tests/live/` runs pr-watch and the real forgectl against real GitHub and GitLab hosts. It settles the rows of [design.md: Live verification](design.md#live-verification) that need a host and the unverified-live lists posted on issue #31. It never runs in CI or in `make verify`: the `live` marker is deselected by default (`addopts = "-m 'not live'"`), and only `make live` selects it.

The suite is written to be run by a person, once per forgectl or pr-watch release that touches host behavior, and once before 1.0. Nothing in it runs unless you opt in.

## Safety

- Every test creates branches, pull requests (merge requests on GitLab), comments, reviews and approvals in the **throwaway repositories you name**, and closes the PRs and deletes the branches in teardown. Merge tests merge into the throwaway repository's base branch. Use repositories that hold nothing you care about. Never name a real project.
- Tokens come from the environment only. A test child gets the token it needs in its own environment; no token is written to a file, passed in argv or printed (failure output is redacted).
- Every child runs with a scratch `HOME`, `XDG_*` and `TMPDIR`, so pr-watch's state, forgectl's config and log, and `gh` and `glab` configuration never touch your real ones.
- The real `crontab` is unreachable: the suite never calls it (cron behavior is a manual step below).
- The isolation guard in `tests/conftest.py` stays in force. Its one opt-in: a test carrying the `live` marker, with `PR_WATCH_LIVE=1`, may launch the single file named by `PR_WATCH_LIVE_FORGECTL` (by its real path). Any other test, any other forgectl and every crontab stay refused. `tests/test_live_support.py` proves it.
- Each call has a timeout; a child that overruns has its own process group (created at launch, pid captured) killed and its output collected within 5 seconds, so a grandchild holding a pipe cannot hang the run. An interrupt does the same. PIDs of every child are kept in `Workspace.pids`.
- Selection: even with `PR_WATCH_LIVE=1` and a full configuration, the suite runs only when the `-m` expression explicitly selects `live` (the word `live` not directly preceded by `not`). `-m "not slow"` skips every live test.
- Leaks: every child's output is checked, before redaction, for the tokens in raw, URL-encoded, base64 and basic-auth forms (the second account's included); tests assert no leak, and the failure names the program, never a secret. Tokens shorter than 8 characters are refused.
- Throwaway confirmation: a configured host is used only when `PR_WATCH_LIVE_<KIND>_THROWAWAY` is exactly equal to `PR_WATCH_LIVE_<KIND>_REPO`. The target is printed once (`LIVE-TARGET`) before the first write, and the repository's canonical name on the host must equal the configured one or the host's tests fail with a clear message.

## Setup

1. **Throwaway repositories.** One on GitHub (or GitHub Enterprise) and one on a GitLab server (gitlab.com or self-managed, https only). Each needs a default branch with at least one commit. Create them private. A GitLab project needs merge requests enabled and no required approvals or pipelines that block merging (the merge tests merge with no checks).
2. **Primary account.** One account that owns or has maintainer rights on both repositories. It authors every PR and is forgectl's "me".
3. **Token scopes** for the primary account (forgectl reads and the tests write with it):
   - GitHub: a classic token with `repo`, or a fine-grained token on the repository with Contents, Pull requests and Commit statuses read and write, Administration write only for the rename test and Metadata read.
   - GitLab: a personal access token with `api` (the suite pushes through git over https and uses the REST and GraphQL APIs).
4. **Second account** (optional, enables the review tests). A second user with write access (GitHub: collaborator; GitLab: Developer) and its own token with the same scopes. It requests changes, approves and is requested as reviewer. Without it those tests skip with a manual-step message.
5. **Tools** on `PATH` when you run `make live`: `git`, `gh` (GitHub tests), `glab` (GitLab tests), and the real `forgectl` at v0.55 or later (its path goes in `PR_WATCH_LIVE_FORGECTL`). forgectl itself needs `gh` and `glab`; the suite gives it counting wrappers of the same binaries through its config.
6. **GitLab pipelines** (optional): a project with a runner, and `PR_WATCH_LIVE_GITLAB_CI=1`. Each PR then carries a `.gitlab-ci.yml` whose one job sleeps 20 seconds, which the CI-completion trigger test needs. Without it that part is skipped.
7. **forgectl config.** The suite writes forgectl's config itself (scratch `XDG_CONFIG_HOME`): `gh_bin`, `glab_bin`, and for each configured host `backend` and `token_env`. Extra forgectl settings (a `hosts.<h>.me`, `retries`, a proxy-related key) go in a YAML file named by `PR_WATCH_LIVE_FORGECTL_EXTRA_CONFIG`, copied to `config.d/50-extra.yaml`.

## Environment variables

Nothing runs unless `PR_WATCH_LIVE=1`. Without it every live test skips. With it, a missing required variable skips the tests it affects (the skip message names the variable); a malformed value fails with the variable's name, never its value.

| Variable | Required | Meaning |
|---|---|---|
| `PR_WATCH_LIVE` | yes | `1` to opt in. Any other value keeps the suite off. |
| `PR_WATCH_LIVE_FORGECTL` | yes | Absolute path of the real forgectl executable (not a wrapper that is named `crontab`). |
| `PR_WATCH_LIVE_GIT`, `PR_WATCH_LIVE_GH`, `PR_WATCH_LIVE_GLAB` | no | Absolute paths of git, gh, glab; default: found on `PATH`. |
| `PR_WATCH_LIVE_GITHUB_REPO` | for GitHub tests | The throwaway repo id, `host/owner/repo` (for example `github.com/example-owner/scratch-repo`). |
| `PR_WATCH_LIVE_GITHUB_TOKEN` | for GitHub tests | The primary account's token. |
| `PR_WATCH_LIVE_GITHUB_TOKEN2` | no | The second account's token (review tests); not the same as the primary token. |
| `PR_WATCH_LIVE_GITHUB_THROWAWAY` | for GitHub tests | Must equal `PR_WATCH_LIVE_GITHUB_REPO` exactly; confirms the repository is a throwaway. Missing: the GitHub tests skip, naming this variable. Different: the run fails, naming it. |
| `PR_WATCH_LIVE_GITHUB_BASE_BRANCH` | no | The default branch; default `main`. |
| `PR_WATCH_LIVE_GITHUB_BRANCH_PREFIX` | no | Scratch branches are `<prefix>/<run id>/<name>`; default `pr-watch-live`. |
| `PR_WATCH_LIVE_GITHUB_RENAME_TO` | no | A new repository name (one segment) enabling the rename test; needs admin on the repo. |
| `PR_WATCH_LIVE_GITLAB_REPO` | for GitLab tests | `host/group/.../project`, for example `gitlab.example.org/example-group/scratch-project`. |
| `PR_WATCH_LIVE_GITLAB_TOKEN`, `_TOKEN2`, `_THROWAWAY`, `_BASE_BRANCH`, `_BRANCH_PREFIX`, `_RENAME_TO` | as above | The same meanings for GitLab. |
| `PR_WATCH_LIVE_GITLAB_API_PREFIX` | no | A path prefix for a GitLab served under a path, such as `/gitlab` (forgectl's `api_prefix`). |
| `PR_WATCH_LIVE_GITLAB_CI` | no | `1` when the project has a runner (step 6); `0` or empty otherwise; anything else is an error. |
| `PR_WATCH_LIVE_FORGECTL_EXTRA_CONFIG` | no | Absolute path of an existing YAML file copied into forgectl's `config.d`. |
| `PR_WATCH_LIVE_HUGE_PR` | no | An existing PR (`host/path#N`, on a configured host) whose snapshot exceeds 1 MiB, for the over-cap test (read only). |
| `PR_WATCH_LIVE_RESULTS_FILE` | no | Absolute path in an existing writable directory (not a link); every test appends one scrubbed JSON line of its result, the file is created mode 0600 (see What to record). |

Proxy and CA variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `GLAB_CA_CERT`) pass through to the children, and `SSL_CERT_FILE` also reaches git. The tokens' variable names `LIVE_GITHUB_TOKEN` and `LIVE_GITLAB_TOKEN` exist only inside the children.

## Run

```sh
export PR_WATCH_LIVE=1
export PR_WATCH_LIVE_FORGECTL=/path/to/forgectl
export PR_WATCH_LIVE_GITHUB_REPO=github.com/example-owner/scratch-repo
export PR_WATCH_LIVE_GITHUB_THROWAWAY=$PR_WATCH_LIVE_GITHUB_REPO
read -rs PR_WATCH_LIVE_GITHUB_TOKEN && export PR_WATCH_LIVE_GITHUB_TOKEN
export PR_WATCH_LIVE_GITLAB_REPO=gitlab.example.org/example-group/scratch-project
export PR_WATCH_LIVE_GITLAB_THROWAWAY=$PR_WATCH_LIVE_GITLAB_REPO
read -rs PR_WATCH_LIVE_GITLAB_TOKEN && export PR_WATCH_LIVE_GITLAB_TOKEN
export PR_WATCH_LIVE_RESULTS_FILE=$HOME/pr-watch-live-results.jsonl
make live
```

`make live` is `uv run pytest -m live -s -rs tests/live`. `-s` shows the one-line `LIVE-RESULT` summaries, `-rs` lists skipped tests with their reasons. Select with `-k`, for example `uv run pytest -m live -s -rs tests/live -k "cost or force"`; run one host with `-k github` or `-k gitlab`.

Expected cost and time: about 34 tests per configured host (the over-cap test runs once per configured host too, and skips without `PR_WATCH_LIVE_HUGE_PR`), each creating one to four PRs. Most are one to three minutes (the host's index lag, `poll_for` loops and fixed settling sleeps dominate); the trigger and lag tests take longest. Budget 45 to 90 minutes per host. Each test makes some tens to a few hundred API requests, far below a rate limit, and a full run creates and closes about 60 PRs and as many branches on each throwaway repository. A failed run may leave PRs open and branches behind (see Cleanup).

## What each test does

Each row names the host actions, the claim and where the result is recorded. "Both" means once per configured host.

| Test (file) | Host actions | Settles |
|---|---|---|
| `test_argv_grammar_schemas_and_version` (contract) | opens a PR, pushes a second commit | #8 items 1, 2, 3, 8: `--opt=value` grammar, `--timeout=<N>ms`, `--version` text, pinned schemas for `prs`, `show`, `snapshot`, `whoami`, `compare`; compare echoes lowercase shas and omits `diff` exactly under `--no-diff` |
| `test_v1_double_dash_before_positionals` | opens a PR | **V1**: whether forgectl accepts `--`; either answer is recorded |
| `test_error_envelopes_and_codes` | opens a PR | #6 and #8 items 4, 9: real `not_found` (10), `auth_required` (12), `unknown_host` (20), `usage` (2), `tool_missing` (12) envelopes, `retry_after` and `details` types, no token in text; a 64-hex sha is a usage error (#24) |
| `test_timeout_budget_and_zero` | opens a PR | #6 hang and expiry, #8 item 7: `--timeout=1ms` exits 12 with `network_error` within the budget plus 10 s; `--timeout=0ms` is usage |
| `test_sigterm_exit_status_and_latency` | opens a PR | #8 item 6: SIGTERM gives 143 or -15 (not 0) within 5 s, latency recorded |
| `test_whoami_with_and_without_credential` | none | `token_source`, `scopes`, the credential-less answer for the host (`token_source: none` or a partial result) |
| `test_cost_budget` | opens a PR with a comment and an inline thread | A snapshot costs 5 to 8 requests on GitHub, 19 to 22 on GitLab (counted at the `gh` and `glab` wrappers), with and without `hosts.<h>.me` |
| `test_trigger_blindness` | comment (control), then GitHub: commit status pending and success, resolve a thread; GitLab: approval (needs `_TOKEN2`), pipeline (needs `_CI=1`) | `updated_at` is blind to CI completion and thread resolution (GitHub), approvals and CI (GitLab); the control comment must move it. Stamps are taken only after the listing has caught up with `show`. On GitLab the approval part depends on `_TOKEN2` and the pipeline part on `_CI=1`; without both the test skips as a manual step |
| `test_v2_search_index_lag` | three comments | **V2**: listing lag behind `show.updated_at`, worst case below the 60 s `poll.overlap` |
| `test_v3_author_me_listing` | two PRs, a comment | **V3** (partly manual): `--author me` finds both, order, `complete`, `--limit=1` cut, repo and item counts |
| `test_v4_snapshot_updated_at_not_behind_listing` | comment, push | **V4** and #24: snapshot and show `updated_at` never behind the listing's |
| `test_show_of_closed_and_merged` | closes one PR, merges another | #24: `updated_at`, `head.sha`, `merged_head_sha` (null when closed), `merge_commit_sha` |
| `test_no_bodies_flag` | comment and inline thread | `snapshot --no-bodies`: no comment or review body, the PR body stays |
| `test_created_at_presence_format_and_skew` | opens a PR | #34: `created_at` in listing, show and snapshot, format, host clock within two minutes |
| `test_unchanged_snapshots_are_identical` | comment | Of three consecutive pairs of snapshots of an unchanged PR (without `fetched_at` and `rate_limit`) at least one is identical; the spikes saw 8 of 9, so not all are required |
| `test_rate_limit_shape` | opens a PR | GitHub `remaining` and `reset_at`; GitLab: only the host is asserted (`remaining` may be a number on gitlab.com), the value is recorded |
| `test_unchanged_pr_gives_no_events_and_a_sane_report` | opens a PR | No events from repeated polls and a `--full` poll; report `hosts.<h>.backend`, `snapshots`, `requests_estimate` (7 or 21 per snapshot) |
| `test_failed_whoami_without_cache_is_reported` | opens a PR | A poll without credentials exits 12 and reports `auth_required` with `ref` `whoami` in `failed[]` |
| `test_event_per_host_change[step]` | per step: comment, inline comment, push, convert to draft, close, merge, ci (GitHub commit status; GitLab a finished pipeline, with `_CI=1`, else skipped) | **V5** and the GitHub shapes: exactly the expected set of event types (inline comment on GitHub: `review` and `review_comment`; GitLab: `review_comment`), nothing more on a second poll; GitLab shapes are the same assertions on GitLab. pr-watch's `watch add`, `poll` and `events` outputs and every event are validated against `schemas/` |
| `test_ready_after_draft` | draft PR made ready | `ready` fires |
| `test_close_reopen_timing` | close, reopen | #34: reopened state timing in snapshot and listing; `reopened` fires |
| `test_retargeted_and_merged_base` | alternative base branch; stacked PRs, merge with branch deletion | #83 item 6: `base.ref` follows an edit, automatic retarget, a merged PR's `base.ref`; `retargeted` fires. The retarget after the API-side branch delete is an **unverified assumption** (see below) |
| `test_force_push_detection` | normal push, then a force push | `compare` is `ahead` then `diverged`; pr-watch stores `forced` false then true |
| `test_review_requests_approvals_and_dismissals` | second account: request, approve, dismiss or unapprove, request changes | #35 and #36 shapes (see below); events `review_requested`, `approved`, `approval_withdrawn`, `changes_requested` |
| `test_hook_runs_for_a_real_event` | comment | A hook runs; `log.jsonl` has `hook_run` with exit 0 and `timed_out` false |
| `test_repo_rename_redirect_and_case` | renames the repository, then a case-only rename; renames back | #83 items 1, 2, 4, 8: old name resolves, same `pr_id`, `--author me` shows the new name, `renamed` once |
| `test_mergeability_codes` | two conflicting PRs, merge one; a draft; close and reopen | #38: `draft` and `conflicts` codes, `mergeability` event, `mergeability_unknown` counts |
| `test_listing_filters` | open, draft, closed and merged PRs | `prs` `--state`, `--draft`, `--updated-since`, `--limit` and `complete`, case of `pr` |
| `test_over_cap_snapshot_falls_back` | none (read only) | #24: over-cap snapshot falls back to `show`; needs `PR_WATCH_LIVE_HUGE_PR`. Exit 0, then either no failure and one committed snapshot (a merged or closed PR: terminal event from `show`) or exactly one `too_large` failure (an open PR: the documented counted failure, the `show` saw it open); anything else fails |

## Unverified assumptions of the suite

- A stacked PR whose base PR is merged and whose base branch is then deleted through the API retargets itself to the default branch on both hosts (`test_retargeted_and_merged_base`). A failure there may be the host's behavior; its message says so.
- The GitHub inline comment step posts one review with a body, so it expects a `review` event as well as `review_comment`.
- A GitLab approval and a pipeline are the only GitLab CI and approval shapes tried; bot edits are manual.

## What to record

Every test prints `LIVE-RESULT <test> key=value ...` and, with `PR_WATCH_LIVE_RESULTS_FILE`, appends the same as a JSON line. Values are scrubbed first: tokens redacted, and the host, the repository path and the account logins replaced by `<host>`, `<repo>`, `<repo-path>` and `<login>`. Scrubbing is best effort: still review every line before pasting it anywhere. Items the tests record without a pass or fail condition (the answer is the finding):

- **V1**: `V1_double_dash_accepted`. **V2**: `V2_lag_seconds`. **V3**: `V3_items`, `V3_repos`, `V3_complete`.
- **#35**: `request_bumps_listing` (does a review request bump `updated_at`), `teams_visible`, `request_incomplete`, `request` (reviewer `kind` and `id`), `request_after_approval`.
- **#36**: `after_withdrawal_entry` (`dismissed`, another state, or `vanished`), `withdrawn_fields`, `changes_requested_entry` (is the approval still reported), `after_push_decision`, `approved_entry`, `approvals_at_head`.
- **#34**: `skew_s`, `snapshot_open_after_s`, `listing_open_after_s`. **#38**: `unknown_of_5`, `reopen_codes`, `conflict_event_s`.
- **#83**: `base_edit_visible_s`, `auto_retarget_s`, `merged_pr_base`, `rename_visible_s`, `old_listing`, `case_only_pr`.
- **#6, #8**: `forgectl` (version text), `sigterm_rc`, `latency_s`, `timeout_1ms_code`, the `details` of each error code, `anon_token_source`, `scopes`.

After a run:

1. Paste the passing and failing test names and the `LIVE-RESULT` lines as a comment on issue #31, with the forgectl version, pr-watch version and host versions (GitLab server version, GitHub or GHES).
2. For each row of design.md's Live verification table that moved from "still to verify" to measured (V1 to V5, the GitLab transition shapes), edit the row's Result and keep the stated default unless the measurement contradicts it. A default that changes goes through a normal interface change (`feat` commit naming it, `docs/interface.md` and schemas), never silently from this suite.
3. Open issues for every failed assertion, with the `LIVE-RESULT` line and the exit code and envelope the test printed.

## Manual steps (the suite cannot do these)

| Item | Why it is manual | How |
|---|---|---|
| Approvals and review tests | need a second account | set `..._TOKEN2` (Setup step 4) |
| Bot edits do not bump `updated_at` (GitLab), bot reviewers (for example Copilot) and their ids (#35) | a bot is a host feature, not an API call | edit a label by a bot or request a Copilot review by hand on a throwaway PR, then compare `forgectl prs --repo` `updated_at` before and after, and read `snapshot.review_requests[].reviewer` |
| V3 repository-count limit | needs an account with open PRs in more repositories than the limit | with such an account, run `test_v3_author_me_listing`, compare `V3_repos` and `V3_complete` with the account's real open PR count from the host UI |
| Transfer or group move keeps `pr_id` (#83 item 5), reuse of an old repo name gives a different `pr_id` (item 3) | destructive on a second repository | rename or transfer by hand, then `forgectl snapshot <old ref>` and `pr-watch poll --pr`; create a new repository under the old name and snapshot the old ref |
| Control characters in `base.ref` (#83 item 7) | git refuses control characters in ref names | not testable on a host; unit-covered with the fake |
| Rate limits (`rate_limited`, `retry_after`, GitHub secondary limits), 5xx and network retry timing, a real hang | cannot be provoked safely | watch `forgectl` logs during a long real poll loop; record the first real `rate_limited` envelope |
| `prs` partial results (two hosts, one failing) | needs both hosts in `prs` scope and one broken | pr-watch asks one host; run `forgectl prs --author me` by hand with one token removed |
| Deleted accounts' approvals, forgectl's unknown-state mapping (#36) | needs a deleted account | by hand, if ever |
| Teams, `teams_visible` flapping (#35) | needs an organization with teams and a token without org scope | request a team review by hand and read `teams_visible` |
| `cron install` against a real crontab (#29): `crontab -l` with none, header lines, `\r` and non-UTF-8 bytes, 999 and 1000 byte lines, `PATH` and `forgectl` found by cron, `*/N` forms | the suite never reaches the real crontab | on a throwaway Linux account (or VM): `pr-watch cron install`, `crontab -l`, wait for one run, `pr-watch cron uninstall`; the checklist in the macOS notes covers macOS |
| Over-cap snapshot | needs a PR over 1 MiB | `PR_WATCH_LIVE_HUGE_PR` (read only) |
| `opened` event | needs a query watch and a PR created while it is watched | create a PR after `pr-watch watch add --author me`, poll, read `events` |
| Not covered by the suite: `prs --query` and `--default-query`, `hidden_repos`, marker sanitising and truncation, the 32 MiB output cap, `config_invalid`, children holding the caller's pipes, `compare` exit 20 `too_large`, `compare` `rate_limited`, the same `pr_id` on two hosts | need special data, hosts or limits | by hand, or unit-covered with the fakes |
| The lease TTL covers snapshot, `show` and `compare` at real latency (#24) | latency dependent | compare `duration_ms` of a `--full` poll in the report with the lease TTL |

## Cleanup

Teardown closes every PR and deletes every branch the test made, and renames a renamed repository back. A PR the test itself closed or merged and a branch it deleted are not touched again, and a host answer that the item is already gone (404, 422) counts as done, so `LIVE-CLEANUP-PROBLEMS` lists real leftovers only. Problems are also written to the results file, and they fail the session at its end (an error on the last test). SIGTERM is turned into a normal interrupt so teardown runs.

## Recovering after a killed run

A `kill -9`, a lost terminal or a power cut skips teardown. No journal or sweep exists; recover by hand:

1. List the open PRs (merge requests) titled `live <run id> ...` on the throwaway repository and close them; a run id is `YYYYMMDDHHMMSS-<pid>`. Delete the branches under `<prefix>/<run id>/`.
2. If `PR_WATCH_LIVE_*_RENAME_TO` was set, check the repository's current name. Before each rename call the suite writes `RENAME-PENDING-<kind>.txt` into the test's scratch directory and, with a results file, `<results file>.rename-pending-<kind>` next to it; either file means a rename may have happened. Rename the repository back to the name in `PR_WATCH_LIVE_*_REPO` (GitHub: Settings, General; GitLab: Settings, General, Advanced, Change path). The next run refuses a repository whose canonical name differs from the configured one.
3. Delete the throwaway tokens when you are done.

The scratch directories (state, config, clones, wrappers) live under pytest's temporary directory and hold no token; pytest removes them.
