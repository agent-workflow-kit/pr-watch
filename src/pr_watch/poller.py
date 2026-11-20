"""Snapshot selection, pools, pacing, host pause, budgets, report.

Enumeration (poll steps 1 to 3: host list, listings, membership) is `pr_watch.enumeration`;
`enumerate_scopes` returns the `EnumerationResult` this module consumes.

Snapshot selection is `pr_watch.selection`, the pool and the per-PR commit `pr_watch.snapshots`
(#22); the unsettled columns are `pr_watch.settle`. Pacing, host pause, low water, the full
pass and the budgets are `pr_watch.pacing` (#23).
TODO(#25): the `poll` command and report.
"""
