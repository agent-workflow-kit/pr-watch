"""Snapshot selection, pools, pacing, host pause, budgets, report.

Enumeration (poll steps 1 to 3: host list, listings, membership) is `pr_watch.enumeration`;
`enumerate_scopes` returns the `EnumerationResult` this module consumes.

Snapshot selection is `pr_watch.selection`, the pool and the per-PR commit `pr_watch.snapshots`
(#22); the unsettled columns are `pr_watch.settle`. TODO(#23): pacing, host pause,
budgets; TODO(#24): over-cap fallback and force-push; TODO(#25): the `poll` command and report.
"""
