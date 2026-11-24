"""The interface-coverage map (issue #91): every interface item and the tests that exercise it.

tests/test_interface_coverage.py enumerates the items from their sources of truth (the argparse
parser, schemas/, docs/interface.md) and fails when an item has no entry here, when an entry names
an item that no longer exists, or when a named test is gone. Add the new item's tests here in the
same change that adds the item. docs/coverage.md explains the rules.

Each value is a list of test node ids (`tests/<file>::<function>`, parametrized ids omitted) that
exercise the item with a schema validation, a golden file, or an assertion on the exact behavior.
UNCOVERABLE items name a written reason instead of a test.
"""

from __future__ import annotations

# Leaf commands of the parser.
COMMANDS: dict[str, list[str]] = {
    "watch add": [
        "tests/test_watch_add.py::test_a_re_added_expired_row_survives_the_next_prune",
        "tests/test_cli.py::test_watch_add_per_pr_and_query_forms_parse",
    ],
    "watch remove": [
        "tests/test_cli_watches.py::test_remove_the_last_owner_ends_the_watch",
        "tests/test_cli_watches.py::test_remove_is_idempotent",
    ],
    "poll": ["tests/test_poll.py::test_a_closed_pr_is_not_snapshotted_again"],
    "list": ["tests/test_list.py::test_a_selected_watch_keeps_all_its_owners"],
    "show": ["tests/test_events.py::test_show_by_url_and_pr_id"],
    "events": ["tests/test_events.py::test_events_after_the_cursor_oldest_first"],
    "browse": ["tests/test_browse_cli.py::test_json_is_refused"],
    "validate": ["tests/test_validate.py::test_defaults_are_valid_with_no_warnings"],
    "label add": ["tests/test_cli_watches.py::test_label_add_by_pr_and_by_watch_id"],
    "label remove": ["tests/test_cli_watches.py::test_label_remove"],
    "prune": ["tests/test_prune.py::test_dry_run_writes_nothing"],
    "cron install": ["tests/test_cron.py::test_install_with_an_env_file_golden"],
    "cron uninstall": [
        "tests/test_cron.py::test_uninstall_dry_run_names_the_block_a_real_run_removes"
    ],
    "export": ["tests/test_events.py::test_export_golden"],
}

# Exit codes of docs/interface.md `Exit codes` ("signal" is the 128+N row).
EXIT_CODES: dict[str, list[str]] = {
    "0": [
        "tests/test_list.py::test_a_successful_command_exits_0_and_prints_its_result",
        "tests/test_cli.py::test_version_prints_version_and_exits_zero",
    ],
    "1": [
        "tests/test_cli_watches.py::test_a_corrupt_store_is_internal_exit_1",
        "tests/test_cli.py::test_handler_oserror_is_still_an_internal_error",
    ],
    "2": ["tests/test_cli.py::test_usage_errors_never_echo_argv_values"],
    "10": ["tests/test_events.py::test_show_an_unstored_pr_is_exit_10"],
    "12": [
        "tests/test_cli_watches.py::test_a_newer_store_is_store_too_new_exit_12",
        "tests/test_validate.py::test_probe_with_no_usable_result_is_exit_12_host_failed",
    ],
    "20": ["tests/test_validate.py::test_probe_refusal_is_exit_20"],
    "signal": [
        "tests/test_signals.py::test_every_stop_signal_inside_a_watch_add_commit_exits_128_plus_it"
    ],
}

# Items no offline test can exercise, each with its reason (`<category>: <item>`).
UNCOVERABLE: dict[str, str] = {}

# Flags: "<command> <flag>"; "* <flag>" is a global flag.
FLAGS: dict[str, list[str]] = {
    "watch add --host": [
        "tests/test_watch_add.py::test_query_form_records_only_and_never_contacts_forgectl",
    ],
    "watch add --label": [
        "tests/test_watch_add.py::test_labels_are_owned_per_pair_and_unioned",
        "tests/test_watch_add.py::test_query_form_unfiltered_with_labels",
    ],
    "watch add --mine": [
        "tests/test_watch_add.py::test_query_form_records_only_and_never_contacts_forgectl",
    ],
    "watch add --no-baseline": [
        "tests/test_watch_add.py::test_no_baseline_calls_show_and_stores_a_row_without_digest",
    ],
    "watch add --open": [
        "tests/test_watch_add.py::test_query_form_records_only_and_never_contacts_forgectl",
    ],
    "watch add --owner": [
        "tests/test_watch_add.py::test_the_owner_flag_names_the_owner_of_the_watch",
    ],
    "watch add --repo": [
        "tests/test_watch_add.py::test_query_form_records_only_and_never_contacts_forgectl",
    ],
    "watch remove --host": [
        "tests/test_cli_watches.py::test_remove_query_watch_by_exact_key",
    ],
    "watch remove --mine": [
        "tests/test_cli_watches.py::test_remove_query_watch_by_exact_key",
    ],
    "watch remove --open": [
        "tests/test_cli_watches.py::test_remove_query_watch_by_exact_key",
    ],
    "watch remove --owner": [
        "tests/test_cli_values.py::test_bad_values_are_usage_errors_with_a_kind",
        "tests/test_cli_watches.py::test_remove_the_last_owner_ends_the_watch",
    ],
    "watch remove --repo": [
        "tests/test_cli_watches.py::test_remove_query_watch_by_exact_key",
    ],
    "poll --full": [
        "tests/test_cli.py::test_poll_options_parse",
        "tests/test_hooks_poll.py::test_post_poll_for_a_newly_unreachable_pr_lists_it_but_a_repeat_failure_alone_does_not",
    ],
    "poll --hook": [
        "tests/test_hooks_poll.py::test_the_hook_flag_adds_a_hook_and_no_hooks_skips_all_of_them",
    ],
    "poll --no-hooks": [
        "tests/test_cli.py::test_poll_options_parse",
        "tests/test_hooks_poll.py::test_the_hook_flag_adds_a_hook_and_no_hooks_skips_all_of_them",
    ],
    "poll --pr": [
        "tests/test_poll.py::test_pr_poll_refreshes_one_pr_and_leaves_last_poll_finished_at",
    ],
    "poll --repo": [
        "tests/test_poll.py::test_repo_poll_reports_only_its_host_and_never_sets_last_poll_finished_at",
    ],
    "list --label": [
        "tests/test_cli_values.py::test_bad_values_are_usage_errors_with_a_kind",
        "tests/test_list.py::test_label_filter_is_and_over_the_labels",
    ],
    "list --owner": ["tests/test_list.py::test_owner_filter_selects_watches_and_their_prs"],
    "list --pr": ["tests/test_list.py::test_pr_filter_by_name_url_and_id"],
    "list --status": [
        "tests/test_cli.py::test_list_status_choices",
        "tests/test_list.py::test_status_filters_prs_only",
    ],
    "events --label": [
        "tests/test_cli.py::test_events_options_parse_as_validated_values",
        "tests/test_events.py::test_label_filter_is_and",
    ],
    "events --limit": [
        "tests/test_cli.py::test_events_options_parse_as_validated_values",
        "tests/test_events.py::test_the_default_limit_is_100_and_the_maximum_is_1000",
    ],
    "events --pr": [
        "tests/test_cli.py::test_events_options_parse_as_validated_values",
        "tests/test_events.py::test_pr_filter_by_name_url_and_id_is_or",
    ],
    "events --since": [
        "tests/test_cli.py::test_events_options_parse_as_validated_values",
        "tests/test_events.py::test_events_after_the_cursor_oldest_first",
    ],
    "events --type": [
        "tests/test_cli.py::test_events_options_parse_as_validated_values",
        "tests/test_events.py::test_type_filter_is_or",
    ],
    "browse --focus": [
        "tests/test_browse_cli.py::test_focus_surfaces_the_stores_own_error_code",
        "tests/test_browse_cli.py::test_a_bad_focus_reference_is_exit_2",
    ],
    "validate --probe": ["tests/test_validate.py::test_probe_asks_whoami_and_reports_hosts"],
    "label add --owner": [
        "tests/test_cli.py::test_label_owner_may_come_before_the_labels",
        "tests/test_cli_values.py::test_handlers_receive_validated_values",
    ],
    "label add --watch": [
        "tests/test_cli.py::test_label_owner_may_come_before_the_labels",
        "tests/test_cli_values.py::test_handlers_receive_validated_values",
    ],
    "label remove --owner": [
        "tests/test_cli.py::test_label_owner_may_come_before_the_labels",
        "tests/test_cli_values.py::test_bad_values_are_usage_errors_with_a_kind",
    ],
    "label remove --watch": [
        "tests/test_cli.py::test_label_owner_may_come_before_the_labels",
        "tests/test_cli_values.py::test_bad_values_are_usage_errors_with_a_kind",
    ],
    "prune --dry-run": [
        "tests/test_cli.py::test_flags_pr_watch_does_not_have_are_rejected",
        "tests/test_prune.py::test_dry_run_writes_nothing",
    ],
    "cron install --dry-run": ["tests/test_cron.py::test_dry_run_human_output_says_it_would"],
    "cron install --every": [
        "tests/test_cli.py::test_export_cron_and_prune_options_parse",
        "tests/test_cron.py::test_every_forms_map_to_cron_schedules",
    ],
    "cron uninstall --dry-run": ["tests/test_cron.py::test_dry_run_human_output_says_it_would"],
    "export --label": [
        "tests/test_cli.py::test_export_cron_and_prune_options_parse",
        "tests/test_events.py::test_export_filters_match_events",
    ],
    "export --pr": [
        "tests/test_cli.py::test_export_cron_and_prune_options_parse",
        "tests/test_events.py::test_export_filters_match_events",
    ],
    "export --since": [
        "tests/test_cli.py::test_export_cron_and_prune_options_parse",
        "tests/test_events.py::test_export_since_selects_events_detected_within_the_last_duration",
    ],
    "export --type": [
        "tests/test_cli.py::test_export_cron_and_prune_options_parse",
        "tests/test_events.py::test_export_filters_match_events",
    ],
    "* --json": [
        "tests/test_cli.py::test_no_arguments_with_json_prints_the_envelope",
        "tests/test_events.py::test_export_is_json_whatever_the_global_flags",
    ],
    "* --config": [
        "tests/test_cli.py::test_config_flag_loads_the_file_and_reaches_the_handler",
        "tests/test_cli_values.py::test_validation_runs_before_the_config_is_read",
    ],
    "* --verbose": [
        "tests/test_cli.py::test_global_flags_are_accepted_before_and_after_the_command",
        "tests/test_cli.py::test_verbose_and_quiet_conflict",
    ],
    "* --quiet": [
        "tests/test_cli.py::test_quiet_silences_human_results_but_not_json",
        "tests/test_cli.py::test_quiet_does_not_silence_errors",
    ],
    "* --version": [
        "tests/test_cli.py::test_version_prints_version_and_exits_zero",
        "tests/test_cli_state.py::test_help_version_and_usage_errors_touch_no_state",
    ],
}

# schemas/cli/*.json: the `--json` result and error documents.
SCHEMAS: dict[str, list[str]] = {
    "cli/cron-install": [
        "tests/test_cron.py::test_a_foreign_tail_without_a_final_newline_gains_only_that_newline",
        "tests/test_cron.py::test_a_group_or_world_writable_forgectl_dir_is_left_out_with_a_warning",
        "tests/test_cron.py::test_a_poll_line_inside_the_block_does_not_warn",
    ],
    "cli/cron-uninstall": [
        "tests/test_cron.py::test_uninstall_removes_only_the_block",
    ],
    "cli/error": [
        "tests/test_cli.py::test_bad_config_value_names_the_key_and_never_echoes_the_value"
    ],
    "cli/events": [
        "tests/test_events.py::test_a_cursor_at_50_with_an_empty_table_and_head_100_is_a_gap",
        "tests/test_events.py::test_a_cursor_at_the_head_returns_nothing_and_no_gap",
        "tests/test_events.py::test_a_cursor_below_pruned_through_seq_is_a_gap_and_returns_the_retained_events",
    ],
    "cli/export": [
        "tests/test_events.py::test_export_filters_match_events",
        "tests/test_events.py::test_export_golden",
        "tests/test_events.py::test_export_has_no_limit_and_streams_every_event",
    ],
    "cli/label-add": [
        "tests/test_cli_watches.py::test_label_add_acts_on_an_expired_but_unpruned_row",
        "tests/test_cli_watches.py::test_label_add_by_pr_and_by_watch_id",
        "tests/test_cli_watches.py::test_label_add_existing_is_a_no_op",
    ],
    "cli/label-remove": [
        "tests/test_cli_watches.py::test_label_remove",
        "tests/test_cli_watches.py::test_label_remove_by_one_owner_keeps_the_other_owners_label",
        "tests/test_cli_watches.py::test_label_remove_idempotent_reasons",
    ],
    "cli/list": [
        "tests/test_list.py::test_a_pending_pr_inside_the_age_limit_is_not_stuck",
        "tests/test_list.py::test_a_pr_with_a_ci_hold_is_stale_with_no_ci_state",
        "tests/test_list.py::test_a_selected_watch_keeps_all_its_owners",
    ],
    "cli/poll": [
        "tests/test_hooks_poll.py::test_a_flood_hits_the_per_poll_cap_and_the_report_and_log_say_so",
        "tests/test_poll.py::test_a_closed_pr_is_not_snapshotted_again",
    ],
    "cli/prune": [
        "tests/test_prune.py::test_a_clock_stepped_back_keeps_every_event",
        "tests/test_prune.py::test_a_lease_far_in_the_future_counts_as_expired",
        "tests/test_prune.py::test_a_member_only_row_is_dropped_without_a_watch_ending",
    ],
    "cli/show": [
        "tests/test_events.py::test_show_an_ambiguous_name_is_exit_2",
        "tests/test_events.py::test_show_before_the_first_snapshot_has_null_digest_fields",
        "tests/test_events.py::test_show_by_url_and_pr_id",
    ],
    "cli/validate": [
        "tests/test_validate.py::test_a_newer_forgectl_minor_is_accepted",
        "tests/test_validate.py::test_credential_like_env_allow_is_a_warning_not_a_failure",
        "tests/test_validate.py::test_defaults_are_valid_with_no_warnings",
    ],
    "cli/watch-add": [
        "tests/test_watch_add.py::test_a_baseline_committed_between_fetch_and_write_wins_and_the_snapshot_is_discarded",
        "tests/test_watch_add.py::test_a_newer_rename_committed_between_plan_and_write_is_kept",
        "tests/test_watch_add.py::test_a_re_added_expired_row_survives_the_next_prune",
    ],
    "cli/watch-remove": [
        "tests/test_cli_watches.py::test_remove_a_pr_that_is_only_a_query_member_is_not_held",
        "tests/test_cli_watches.py::test_remove_acts_on_an_expired_but_unpruned_row",
        "tests/test_cli_watches.py::test_remove_by_a_non_owner_keeps_the_watch",
    ],
}

# schemas/hooks/*.json: one payload schema per hook.
HOOKS: dict[str, list[str]] = {
    "on-unreachable": [
        "tests/test_hooks.py::test_the_health_and_mergeability_hook_payload_goldens",
        "tests/test_poll_health.py::test_the_hooks_run_with_the_documented_payloads_and_are_capped_like_other_events",
    ],
    "on-recovered": [
        "tests/test_hooks.py::test_the_health_and_mergeability_hook_payload_goldens",
        "tests/test_poll_health.py::test_the_hooks_run_with_the_documented_payloads_and_are_capped_like_other_events",
    ],
    "on-renamed": [
        "tests/test_hooks.py::test_the_on_renamed_payload_golden",
        "tests/test_hooks_poll.py::test_a_rename_found_by_a_listing_runs_on_renamed_once",
    ],
    "on-opened": ["tests/test_hooks.py::test_the_on_opened_and_on_reopened_payload_goldens"],
    "on-reopened": ["tests/test_hooks.py::test_the_on_opened_and_on_reopened_payload_goldens"],
    "on-pushed": [
        "tests/test_hooks.py::test_the_on_pushed_payload_golden",
        "tests/test_hooks_poll.py::test_an_on_event_hook_runs_after_the_commit_with_the_event_on_stdin_in_hooks_cwd",
    ],
    "on-retargeted": ["tests/test_hooks.py::test_the_on_retargeted_payload_golden"],
    "on-comment": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "on-review_comment": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "on-review": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "on-review_requested": ["tests/test_hooks.py::test_the_new_event_hook_payload_goldens"],
    "on-approved": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "on-changes_requested": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "on-approval_withdrawn": ["tests/test_hooks.py::test_the_on_approval_withdrawn_payload_golden"],
    "on-ci": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "on-ready": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "on-converted_to_draft": ["tests/test_hooks.py::test_the_new_event_hook_payload_goldens"],
    "on-mergeability": [
        "tests/test_hooks.py::test_the_health_and_mergeability_hook_payload_goldens",
        "tests/test_poll_mergeability.py::test_the_event_is_valid_listed_by_type_and_runs_its_hook",
    ],
    "on-merged": [
        "tests/test_hook_payloads.py::test_the_event_hook_payload_goldens",
        "tests/test_hooks.py::test_the_payload_is_the_event_plus_the_hook_fields_and_matches_its_schema",
    ],
    "on-closed": ["tests/test_hook_payloads.py::test_the_event_hook_payload_goldens"],
    "post-poll": [
        "tests/test_hooks.py::test_failed_host_entries_without_a_hosts_entry_are_in_post_poll_errors",
        "tests/test_hooks_poll.py::test_post_poll_fires_for_a_failed_pr_with_counts_that_exclude_itself",
        "tests/test_hooks.py::test_the_post_poll_payload_golden",
    ],
}

# schemas/event.json: one stored-event shape per type.
EVENTS: dict[str, list[str]] = {
    "unreachable": ["tests/test_poll_health.py::test_the_stored_events_validate_and_list_by_type"],
    "recovered": ["tests/test_poll_health.py::test_the_stored_events_validate_and_list_by_type"],
    "renamed": ["tests/test_poll_renamed.py::test_the_event_validates_and_filters_by_type"],
    "opened": [
        "tests/test_diff.py::test_opened_and_reopened_validate_against_the_event_schema",
        "tests/test_poll_opened.py::test_opened_payload_validates_and_names_the_watch",
    ],
    "reopened": ["tests/test_diff.py::test_opened_and_reopened_validate_against_the_event_schema"],
    "pushed": [
        "tests/test_diff.py::test_every_event_validates_against_the_event_schema",
        "tests/test_store.py::test_stored_event_matches_the_event_schema",
    ],
    "retargeted": [
        "tests/test_diff_retargeted.py::test_the_event_validates_against_the_schema",
        "tests/test_poll_retargeted.py::test_the_event_is_valid_and_comes_with_a_push_in_order",
    ],
    "comment": [
        "tests/test_baseline.py::test_plan_fields_commit_and_events_validate_against_the_schema",
        "tests/test_diff.py::test_every_event_validates_against_the_event_schema",
    ],
    "review_comment": [
        "tests/test_diff.py::test_every_event_validates_against_the_event_schema",
        "tests/test_diff.py::test_review_comment_line_bounded_by_store_limit",
    ],
    "review": [
        "tests/test_diff.py::test_every_event_validates_against_the_event_schema",
    ],
    "review_requested": [
        "tests/test_poll_review_requests.py::test_github_request_sequence_fires_once_per_new_request_and_again_after_a_review",
        "tests/test_review_requests.py::test_the_event_validates_against_the_schema",
    ],
    "approved": [
        "tests/test_diff_approvals.py::test_every_new_event_validates_against_the_event_schema"
    ],
    "changes_requested": [
        "tests/test_diff_approvals.py::test_every_new_event_validates_against_the_event_schema"
    ],
    "approval_withdrawn": [
        "tests/test_approval_withdrawn.py::test_the_event_validates_against_the_schema",
        "tests/test_poll_approval_withdrawn.py::test_github_approve_dismiss_reapprove_withdraw_sequence",
    ],
    "ci": ["tests/test_diff_approvals.py::test_every_new_event_validates_against_the_event_schema"],
    "ready": [
        "tests/test_diff_approvals.py::test_every_new_event_validates_against_the_event_schema"
    ],
    "converted_to_draft": [
        "tests/test_review_requests.py::test_converted_to_draft_event_validates"
    ],
    "mergeability": [
        "tests/test_diff_mergeability.py::test_the_event_validates_against_the_schema",
        "tests/test_poll_mergeability.py::test_an_oversized_blocker_list_still_commits_and_stores_a_bounded_event",
        "tests/test_diff_mergeability.py::test_the_schema_caps_the_payload_lists_at_twenty",
    ],
    "merged": ["tests/test_diff.py::test_every_event_validates_against_the_event_schema"],
    "closed": [
        "tests/test_baseline.py::test_plan_fields_commit_and_events_validate_against_the_schema",
        "tests/test_diff.py::test_every_event_validates_against_the_event_schema",
        "tests/test_store.py::test_stored_event_matches_the_event_schema",
    ],
}

# schemas/log.json: one record shape per `event`.
LOG: dict[str, list[str]] = {
    "poll_start": [
        "tests/test_poll.py::test_poll_start_names_forgectl_once_and_poll_end_summarises",
        "tests/test_log.py::test_every_record_validates_against_the_schema",
    ],
    "poll_end": [
        "tests/test_poll.py::test_poll_start_names_forgectl_once_and_poll_end_summarises",
        "tests/test_hooks_signals.py::test_a_signal_while_a_hook_runs_stops_it_and_its_group_and_prints_no_report",
        "tests/test_log.py::test_every_record_validates_against_the_schema",
    ],
    "host_failed": [
        "tests/test_log.py::test_every_record_validates_against_the_schema",
        "tests/test_poll.py::test_whoami_failure_with_a_stale_cache_goes_on_and_is_not_exit_12",
        "tests/test_log.py::test_optional_message_validates",
    ],
    "part_incomplete": [
        "tests/test_snapshots.py::test_a_part_incomplete_for_unreachable_after_snapshots_is_logged_once",
        "tests/test_log.py::test_every_record_validates_against_the_schema",
    ],
    "commit_skipped": [
        "tests/test_log.py::test_every_record_validates_against_the_schema",
        "tests/test_snapshots.py::test_a_closed_snapshot_older_than_a_newer_listing_is_not_committed",
    ],
    "events_dropped": [
        "tests/test_enumeration.py::test_a_rename_without_a_valid_head_is_not_reported_and_is_logged",
        "tests/test_log.py::test_every_record_validates_against_the_schema",
        "tests/test_poll_health.py::test_a_recovery_of_a_row_that_never_had_a_digest_is_dropped_not_orphaned",
    ],
    "labels_capped": [
        "tests/test_log.py::test_every_record_validates_against_the_schema",
        "tests/test_poll_renamed.py::test_a_listed_rename_with_an_over_cap_label_union_is_stored_and_logged",
        "tests/test_snapshots.py::test_a_label_union_over_the_cap_is_cut_sorted_and_logged",
    ],
    "host_summary": [
        "tests/test_poll.py::test_poll_start_names_forgectl_once_and_poll_end_summarises",
        "tests/test_log.py::test_every_record_validates_against_the_schema",
    ],
    "snapshot_failed": [
        "tests/test_log.py::test_every_record_validates_against_the_schema",
        "tests/test_overcap.py::test_an_open_over_cap_pr_counts_one_failure_and_costs_one_show",
    ],
    "pr_status": [
        "tests/test_log.py::test_every_record_validates_against_the_schema",
        "tests/test_poll.py::test_closed_and_reopened_prs_are_reactivated_with_one_reopened_event",
    ],
    "hook_run": [
        "tests/test_hooks_poll.py::test_an_on_event_hook_runs_after_the_commit_with_the_event_on_stdin_in_hooks_cwd",
        "tests/test_log.py::test_every_record_validates_against_the_schema",
    ],
    "hook_cap": [
        "tests/test_hooks.py::test_a_skipped_post_poll_alone_still_writes_a_hook_cap_record_without_event_ids",
        "tests/test_hooks_poll.py::test_a_flood_hits_the_per_poll_cap_and_the_report_and_log_say_so",
        "tests/test_log.py::test_every_record_validates_against_the_schema",
    ],
    "config_warning": [
        "tests/test_cli_state.py::test_config_warnings_are_logged_before_the_handler_runs",
        "tests/test_config.py::test_r4_warnings_validate_against_the_log_schema",
    ],
}

# schemas/cli/error.json: the error envelope `code`s.
ERRORS: dict[str, list[str]] = {
    "usage": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_cli.py::test_bad_usage_exits_2_with_envelope",
    ],
    "config_invalid": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_cli.py::test_bad_config_value_names_the_key_and_never_echoes_the_value",
    ],
    "invalid_pr": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_watch_add.py::test_invalid_reference_exits_2_and_records_nothing",
    ],
    "not_found": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_events.py::test_show_an_unstored_pr_is_exit_10",
    ],
    "host_failed": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_validate.py::test_probe_with_no_usable_result_is_exit_12_host_failed",
    ],
    "tool_missing": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_validate.py::test_hung_version_ends_as_tool_missing_after_the_short_deadline",
    ],
    "tool_too_old": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_watch_add.py::test_forgectl_failures_map_to_the_envelope_and_record_nothing",
    ],
    "contract": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_validate.py::test_probe_failure_ends_with_the_forgectl_error",
    ],
    "store_too_new": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_cli_watches.py::test_a_newer_store_is_store_too_new_exit_12",
    ],
    "state_unsafe": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_validate.py::test_unsafe_state_dir_is_exit_12_state_unsafe",
    ],
    "refused": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_validate.py::test_probe_refusal_is_exit_20",
    ],
    "internal": [
        "tests/test_cli.py::test_envelope_golden_and_exit_code",
        "tests/test_cli_watches.py::test_a_corrupt_store_is_internal_exit_1",
    ],
}
