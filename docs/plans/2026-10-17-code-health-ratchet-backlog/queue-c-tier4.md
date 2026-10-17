# Queue C — tier 4 (tests) blocking rule-family units

Part of [the code-health ratchet backlog](../2026-10-17-code-health-ratchet-backlog.md) (measured on main@6bb43667, after slice 1). Read that document's *How to take a unit* and *Rule-family playbooks* first; IDs are stable references for the Progress log.

| ID | path | family | rules × count | lines on main | tier | churn90 | file vs upstream | tests to run (`scripts/run_tests.sh …`) |
|---|---|---|---|---|---|---|---|---|
| C-1530 | `tests/compat/old_updater_dependencies.py` | correctness | F821×31 | 40, 62, 68, 75, 76, 77, 92, 102, 103, 109, 112, 118 … | T4 tests | 2 | identical | tests/compat/old_updater_dependencies.py |
| C-1531 | `tests/tools/test_local_env_blocklist.py` | correctness | F811×19, F821×1 | 70, 112, 142, 182, 215, 247, 344, 354, 386, 423, 449, 479 … | T4 tests | 61 | identical | tests/tools/test_local_env_blocklist.py |
| C-1532 | `tests/pm/test_worker_publication.py` | correctness | F811×18 | 19, 50, 109, 136, 161, 187, 277, 317, 359, 381, 395, 414 | T4 tests | 9 | identical | tests/pm/test_worker_publication.py |
| C-1533 | `tests/scripts/test_release_artifacts.py` | correctness | F811×12 | 68, 139, 187, 254, 289, 305, 391, 392, 417 | T4 tests | 18 | identical | tests/scripts/test_release_artifacts.py |
| C-1534 | `tests/hermes_cli/test_shared_metrics_model_attribution.py` | correctness | F811×10 | 83, 100, 114, 158, 196, 259, 273, 306, 325, 339 | T4 tests | 11 | identical | tests/hermes_cli/test_shared_metrics_model_attribution.py |
| C-1535 | `tests/hermes_cli/test_shared_metrics_engagement.py` | correctness | F811×9 | 60, 90, 123, 132, 158, 198, 215, 229, 253 | T4 tests | 6 | identical | tests/hermes_cli/test_shared_metrics_engagement.py |
| C-1536 | `tests/hermes_cli/test_shared_metrics_harness.py` | correctness | F811×9 | 43, 71, 111, 128, 143, 165, 194, 218, 240 | T4 tests | 4 | identical | tests/hermes_cli/test_shared_metrics_harness.py |
| C-1537 | `tests/ci/test_channel_build_publication.py` | correctness | F811×8 | 40, 97, 145, 175, 206, 250, 265, 343 | T4 tests | 19 | identical | tests/ci/test_channel_build_publication.py |
| C-1538 | `tests/hermes_cli/test_data_uninstall_holders.py` | correctness | F811×7 | 13, 34, 51, 109, 120, 136, 158 | T4 tests | 2 | diverged | tests/hermes_cli/test_data_uninstall_holders.py |
| C-1539 | `tests/hermes_cli/test_shared_metrics_efficiency.py` | correctness | F811×7 | 69, 95, 116, 147, 166, 176, 216 | T4 tests | 4 | identical | tests/hermes_cli/test_shared_metrics_efficiency.py |
| C-1540 | `tests/scripts/test_archive_inputs.py` | correctness | F811×7 | 68, 105, 136, 157, 193, 228, 253 | T4 tests | 7 | identical | tests/scripts/test_archive_inputs.py |
| C-1541 | `tests/scripts/test_release_handoff.py` | correctness | F811×6 | 13, 78, 98, 131, 159, 218 | T4 tests | 8 | identical | tests/scripts/test_release_handoff.py |
| C-1542 | `tests/tui_gateway/test_diagnostic_turn_end_to_end.py` | correctness | F811×6 | 12, 42, 59 | T4 tests | 1 | identical | tests/tui_gateway/test_diagnostic_turn_end_to_end.py |
| C-1543 | `tests/hermes_cli/test_web_ui_build.py` | correctness | F811×5 | 40, 53, 67, 77, 129 | T4 tests | 29 | identical | tests/hermes_cli/test_web_ui_build.py |
| C-1544 | `tests/tools/test_code_execution_modes.py` | correctness | F811×5 | 20, 77, 139, 143 | T4 tests | 19 | identical | tests/tools/test_code_execution_modes.py |
| C-1545 | `tests/ci/test_desktop_bundle_smoke.py` | correctness | F811×4 | 61, 98, 121, 185 | T4 tests | 17 | identical | tests/ci/test_desktop_bundle_smoke.py |
| C-1546 | `tests/gateway/test_relay_source_platform_metrics.py` | correctness | F811×4 | 29, 46, 57, 70 | T4 tests | 3 | identical | tests/gateway/test_relay_source_platform_metrics.py |
| C-1547 | `tests/hermes_cli/test_cmd_update.py` | correctness | F811×4 | 46, 64, 84, 101 | T4 tests | 60 | identical | tests/hermes_cli/test_cmd_update.py |
| C-1548 | `tests/hermes_cli/test_local_models_routes.py` | correctness | F811×4 | 530, 588, 623, 673 | T4 tests | 17 | diverged | tests/hermes_cli/test_local_models_routes.py |
| C-1549 | `tests/hermes_cli/test_shared_metrics_model.py` | correctness | F811×4 | 53, 82, 102, 133 | T4 tests | 3 | identical | tests/hermes_cli/test_shared_metrics_model.py |
| C-1550 | `tests/pm/test_download_mirrors.py` | correctness | F811×4 | 11, 41, 54, 74 | T4 tests | 3 | identical | tests/pm/test_download_mirrors.py |
| C-1551 | `tests/pm/test_worker.py` | correctness | F811×4 | 287, 350, 391, 531 | T4 tests | 19 | identical | tests/pm/test_worker.py |
| C-1552 | `tests/scripts/test_release_darwin.py` | correctness | F811×4 | 57, 141, 168, 194 | T4 tests | 7 | identical | tests/scripts/test_release_darwin.py |
| C-1553 | `tests/tui_gateway/test_warning_callback_conservation.py` | correctness | F811×4 | 56, 98 | T4 tests | 3 | identical | tests/tui_gateway/test_warning_callback_conservation.py |
| C-1554 | `tests/ci/test_commit_build_staging.py` | correctness | F811×3 | 29, 66, 126 | T4 tests | 15 | identical | tests/ci/test_commit_build_staging.py |
| C-1555 | `tests/ci/test_protected_canary_publication.py` | correctness | F811×3 | 51, 150, 197 | T4 tests | 10 | identical | tests/ci/test_protected_canary_publication.py |
| C-1556 | `tests/hermes_cli/test_cmd_uninstall.py` | correctness | F811×3 | 13, 42, 68 | T4 tests | 3 | identical | tests/hermes_cli/test_cmd_uninstall.py |
| C-1557 | `tests/hermes_cli/test_local_download_jobs.py` | correctness | F811×3 | 61, 105, 118 | T4 tests | 3 | identical | tests/hermes_cli/test_local_download_jobs.py |
| C-1558 | `tests/hermes_cli/test_plugins_update_sync.py` | correctness | F811×3 | 13, 37, 56 | T4 tests | 6 | identical | tests/hermes_cli/test_plugins_update_sync.py |
| C-1559 | `tests/pm/test_installed_package.py` | correctness | F811×3 | 16, 38, 57 | T4 tests | 8 | identical | tests/pm/test_installed_package.py |
| C-1560 | `tests/pm/test_source_update_launch.py` | correctness | F811×3 | 33, 367, 458 | T4 tests | 18 | identical | tests/pm/test_source_update_launch.py |
| C-1561 | `tests/tools/test_spawn_site_child_env.py` | correctness | F811×3 | 31, 45, 55 | T4 tests | 9 | identical | tests/tools/test_spawn_site_child_env.py |
| C-1562 | `tests/agent/test_stream_serving_provider.py` | correctness | F811×2 | 41, 71 | T4 tests | 3 | identical | tests/agent/test_stream_serving_provider.py |
| C-1563 | `tests/gateway/test_api_server_status_stream.py` | correctness | F811×2 | 20, 52 | T4 tests | 2 | identical | tests/gateway/test_api_server_status_stream.py |
| C-1564 | `tests/gateway/test_background_process_served_apiserver_wake.py` | correctness | F811×2 | 48, 82 | T4 tests | 2 | identical | tests/gateway/test_background_process_served_apiserver_wake.py |
| C-1565 | `tests/hermes_cli/test_local_component_jobs.py` | correctness | F811×2 | 24 | T4 tests | 3 | identical | tests/hermes_cli/test_local_component_jobs.py |
| C-1566 | `tests/hermes_cli/test_local_job_registry.py` | correctness | F811×2 | 13, 27 | T4 tests | 2 | identical | tests/hermes_cli/test_local_job_registry.py |
| C-1567 | `tests/hermes_cli/test_model_switch_confirm_thread.py` | correctness | F811×2 | 118, 155 | T4 tests | 3 | diverged | tests/hermes_cli/test_model_switch_confirm_thread.py |
| C-1568 | `tests/hermes_cli/test_plugin_dependency_consent.py` | correctness | F811×2 | 14, 69 | T4 tests | 8 | identical | tests/hermes_cli/test_plugin_dependency_consent.py |
| C-1569 | `tests/hermes_cli/test_plugin_install_manifest_version.py` | correctness | F811×2 | 44, 64 | T4 tests | 5 | identical | tests/hermes_cli/test_plugin_install_manifest_version.py |
| C-1570 | `tests/hermes_cli/test_plugins_cmd.py` | correctness | F811×2 | 775, 958 | T4 tests | 35 | identical | tests/hermes_cli/test_plugins_cmd.py |
| C-1571 | `tests/hermes_cli/test_skills_hub.py` | correctness | F811×2 | 600, 633 | T4 tests | 36 | identical | tests/hermes_cli/test_skills_hub.py |
| C-1572 | `tests/hermes_cli/test_venv_sync_currency.py` | correctness | F811×2 | 17, 109 | T4 tests | 8 | identical | tests/hermes_cli/test_venv_sync_currency.py |
| C-1573 | `tests/pm/test_download_progress.py` | correctness | F811×2 | 9, 25 | T4 tests | 2 | identical | tests/pm/test_download_progress.py |
| C-1574 | `tests/pm/test_download_retry_limits.py` | correctness | F811×2 | 17, 66 | T4 tests | 4 | identical | tests/pm/test_download_retry_limits.py |
| C-1575 | `tests/pm/test_network_retries.py` | correctness | F811×2 | 23, 94 | T4 tests | 5 | identical | tests/pm/test_network_retries.py |
| C-1576 | `tests/pm/test_worker_registry.py` | correctness | F811×2 | 31 | T4 tests | 8 | identical | tests/pm/test_worker_registry.py |
| C-1577 | `tests/scripts/test_render_builds_publication.py` | correctness | F811×2 | 39, 111 | T4 tests | 6 | identical | tests/scripts/test_render_builds_publication.py |
| C-1578 | `tests/tools/test_browser_process_tree.py` | correctness | F811×2 | 118, 197 | T4 tests | 5 | identical | tests/tools/test_browser_process_tree.py |
| C-1579 | `tests/tui_gateway/test_foreground_notification_snapshot.py` | correctness | F811×2 | 28 | T4 tests | 4 | identical | tests/tui_gateway/test_foreground_notification_snapshot.py |
| C-1580 | `tests/agent/test_probe_cache_followups.py` | correctness | F811×1 | 162 | T4 tests | 16 | identical | tests/agent/test_probe_cache_followups.py |
| C-1581 | `tests/agent/transports/test_codex_app_server_runtime.py` | correctness | F811×1 | 115 | T4 tests | 16 | identical | tests/agent/transports/test_codex_app_server_runtime.py |
| C-1582 | `tests/ci/test_tag_builds_summary.py` | correctness | F811×1 | 19 | T4 tests | 7 | identical | tests/ci/test_tag_builds_summary.py |
| C-1583 | `tests/gateway/test_slack_reply_expected.py` | correctness | F811×1 | 36 | T4 tests | 4 | identical | tests/gateway/test_slack_reply_expected.py |
| C-1584 | `tests/hermes_cli/test_desktop_source_build.py` | correctness | F811×1 | 12 | T4 tests | 4 | identical | tests/hermes_cli/test_desktop_source_build.py |
| C-1585 | `tests/hermes_cli/test_local_runtime.py` | correctness | F821×1 | 24 | T4 tests | 30 | diverged | tests/hermes_cli/test_local_runtime.py |
| C-1586 | `tests/hermes_cli/test_plugin_update_transaction.py` | correctness | F811×1 | 48 | T4 tests | 5 | identical | tests/hermes_cli/test_plugin_update_transaction.py |
| C-1587 | `tests/hermes_cli/test_plugins_cmd_catalog.py` | correctness | F811×1 | 33 | T4 tests | 33 | identical | tests/hermes_cli/test_plugins_cmd_catalog.py |
| C-1588 | `tests/hermes_cli/test_shared_metrics_loop.py` | correctness | F811×1 | 33 | T4 tests | 6 | identical | tests/hermes_cli/test_shared_metrics_loop.py |
| C-1589 | `tests/hermes_cli/test_tui_npm_install.py` | correctness | F811×1 | 39 | T4 tests | 26 | identical | tests/hermes_cli/test_tui_npm_install.py |
| C-1590 | `tests/hermes_cli/test_update_autostash.py` | correctness | F811×1 | 21 | T4 tests | 52 | identical | tests/hermes_cli/test_update_autostash.py |
| C-1591 | `tests/hermes_cli/test_update_completion_routing.py` | correctness | F811×1 | 28 | T4 tests | 5 | identical | tests/hermes_cli/test_update_completion_routing.py |
| C-1592 | `tests/hermes_cli/test_update_existing_branch_baseline.py` | correctness | F811×1 | 224 | T4 tests | 12 | identical | tests/hermes_cli/test_update_existing_branch_baseline.py |
| C-1593 | `tests/hermes_cli/test_update_missing_configured_deps.py` | correctness | F811×1 | 25 | T4 tests | 7 | identical | tests/hermes_cli/test_update_missing_configured_deps.py |
| C-1594 | `tests/hermes_cli/test_update_no_gateway_restart.py` | correctness | F811×1 | 11 | T4 tests | 5 | identical | tests/hermes_cli/test_update_no_gateway_restart.py |
| C-1595 | `tests/pm/test_activation_runtime.py` | correctness | F811×1 | 32 | T4 tests | 12 | identical | tests/pm/test_activation_runtime.py |
| C-1596 | `tests/pm/test_activation_setup.py` | correctness | F811×1 | 44 | T4 tests | 9 | identical | tests/pm/test_activation_setup.py |
| C-1597 | `tests/pm/test_artifact_mirror.py` | correctness | F811×1 | 18 | T4 tests | 5 | identical | tests/pm/test_artifact_mirror.py |
| C-1598 | `tests/pm/test_download_serial_fallback.py` | correctness | F811×1 | 12 | T4 tests | 2 | identical | tests/pm/test_download_serial_fallback.py |
| C-1599 | `tests/pm/test_downloader_integrity.py` | correctness | F811×1 | 259 | T4 tests | 3 | identical | tests/pm/test_downloader_integrity.py |
| C-1600 | `tests/pm/test_install_download_control.py` | correctness | F811×1 | 46 | T4 tests | 9 | identical | tests/pm/test_install_download_control.py |
| C-1601 | `tests/pm/test_npm_dist_tags.py` | correctness | F811×1 | 9 | T4 tests | 3 | identical | tests/pm/test_npm_dist_tags.py |
| C-1602 | `tests/pm/test_pm_authority.py` | correctness | F811×1 | 27 | T4 tests | 13 | identical | tests/pm/test_pm_authority.py |
| C-1603 | `tests/pm/test_setup_lock_format.py` | correctness | F811×1 | 30 | T4 tests | 9 | identical | tests/pm/test_setup_lock_format.py |
| C-1604 | `tests/pm/test_store_extensions.py` | correctness | F811×1 | 11 | T4 tests | 7 | identical | tests/pm/test_store_extensions.py |
| C-1605 | `tests/pm/test_update_artifact_refresh.py` | correctness | F811×1 | 22 | T4 tests | 5 | identical | tests/pm/test_update_artifact_refresh.py |
| C-1606 | `tests/pm/test_update_request_reuse.py` | correctness | F811×1 | 22 | T4 tests | 7 | identical | tests/pm/test_update_request_reuse.py |
| C-1607 | `tests/pm/test_workspace.py` | correctness | F811×1 | 28 | T4 tests | 23 | identical | tests/pm/test_workspace.py |
| C-1608 | `tests/scripts/test_appinstaller.py` | correctness | F811×1 | 14 | T4 tests | 4 | identical | tests/scripts/test_appinstaller.py |
| C-1609 | `tests/scripts/test_release_canary_git.py` | correctness | F811×1 | 12 | T4 tests | 6 | identical | tests/scripts/test_release_canary_git.py |
| C-1610 | `tests/scripts/test_release_r2.py` | correctness | F811×1 | 734 | T4 tests | 14 | identical | tests/scripts/test_release_r2.py |
| C-1611 | `tests/scripts/test_setup_toolchain.py` | correctness | F811×1 | 19 | T4 tests | 10 | identical | tests/scripts/test_setup_toolchain.py |
| C-1612 | `tests/tools/test_bot_relay_windows_paths.py` | correctness | F811×1 | 29 | T4 tests | 8 | diverged | tests/tools/test_bot_relay_windows_paths.py |
| C-1613 | `tests/tools/test_build_subprocess_env.py` | correctness | F811×1 | 12 | T4 tests | 5 | diverged | tests/tools/test_build_subprocess_env.py |
| C-1614 | `tests/tools/test_delegate_fallback_matrix.py` | correctness | F821×1 | 244 | T4 tests | 6 | identical | tests/tools/test_delegate_fallback_matrix.py |
| C-1615 | `tests/tools/test_execute_code_approval_cluster.py` | correctness | F811×1 | 92 | T4 tests | 17 | identical | tests/tools/test_execute_code_approval_cluster.py |
| C-1616 | `tests/tools/test_file_ops_cwd_tracking.py` | correctness | F811×1 | 21 | T4 tests | 13 | identical | tests/tools/test_file_ops_cwd_tracking.py |
| C-1617 | `tests/pm/test_update_request_reuse.py` | structure | HX011×2 | 66, 138 | T4 tests | 7 | identical | tests/pm/test_update_request_reuse.py |
| C-1618 | `tests/agent/test_actual_auxiliary_routing.py` | structure | HX011×1 | 305 | T4 tests | 6 | identical | tests/agent/test_actual_auxiliary_routing.py |
| C-1619 | `tests/agent/test_bedrock_context_cache.py` | structure | HX011×1 | 91 | T4 tests | 3 | identical | tests/agent/test_bedrock_context_cache.py |
| C-1620 | `tests/agent/test_bounded_response.py` | structure | HX011×1 | 37 | T4 tests | 9 | identical | tests/agent/test_bounded_response.py |
| C-1621 | `tests/agent/test_command_token_source.py` | structure | HX011×1 | 385 | T4 tests | 12 | identical | tests/agent/test_command_token_source.py |
| C-1622 | `tests/agent/test_save_url_image.py` | structure | HX011×1 | 34 | T4 tests | 13 | identical | tests/agent/test_save_url_image.py |
| C-1623 | `tests/gateway/test_telegram_mention_context.py` | structure | HX011×1 | 79 | T4 tests | 3 | identical | tests/gateway/test_telegram_mention_context.py |
| C-1624 | `tests/hermes_cli/test_diff_replay_no_stale_cells.py` | structure | HX011×1 | 106 | T4 tests | 1 | fork-only | tests/hermes_cli/test_diff_replay_no_stale_cells.py |
| C-1625 | `tests/hermes_cli/test_local_runtime.py` | structure | HX011×1 | 71 | T4 tests | 30 | diverged | tests/hermes_cli/test_local_runtime.py |
| C-1626 | `tests/hermes_cli/test_manual_serve_deferral.py` | structure | HX011×1 | 31 | T4 tests | 13 | diverged | tests/hermes_cli/test_manual_serve_deferral.py |
| C-1627 | `tests/hermes_cli/test_refresh_singleflight.py` | structure | HX011×1 | 71 | T4 tests | 2 | identical | tests/hermes_cli/test_refresh_singleflight.py |
| C-1628 | `tests/hermes_cli/test_source_channel_integration.py` | structure | HX011×1 | 507 | T4 tests | 10 | identical | tests/hermes_cli/test_source_channel_integration.py |
| C-1629 | `tests/hermes_state/test_clean_close_residual_poison.py` | structure | HX011×1 | 80 | T4 tests | 3 | identical | tests/hermes_state/test_clean_close_residual_poison.py |
| C-1630 | `tests/pm/test_downloader_integrity.py` | structure | HX011×1 | 28 | T4 tests | 3 | identical | tests/pm/test_downloader_integrity.py |
| C-1631 | `tests/pm/test_pm_authority.py` | structure | HX011×1 | 161 | T4 tests | 13 | identical | tests/pm/test_pm_authority.py |
| C-1632 | `tests/scripts/test_agent_build.py` | structure | HX011×1 | 196 | T4 tests | 9 | identical | tests/scripts/test_agent_build.py |
| C-1633 | `tests/scripts/test_release_sequencer.py` | structure | HX011×1 | 497 | T4 tests | 21 | identical | tests/scripts/test_release_sequencer.py |
| C-1634 | `tests/scripts/test_source_driver.py` | structure | HX011×1 | 223 | T4 tests | 5 | identical | tests/scripts/test_source_driver.py |
| C-1635 | `tests/scripts/test_termux_wheelhouse_cache.py` | structure | HX011×1 | 47 | T4 tests | 2 | identical | tests/scripts/test_termux_wheelhouse_cache.py |
| C-1636 | `tests/tools/test_skill_ledger_delta.py` | structure | HX011×1 | 150 | T4 tests | 15 | identical | tests/tools/test_skill_ledger_delta.py |
| C-1637 | `tests/tools/test_spawn_site_child_env.py` | structure | HX011×1 | 66 | T4 tests | 9 | identical | tests/tools/test_spawn_site_child_env.py |
| C-1638 | `tests/tui_gateway/test_isolated_orphan_activity.py` | structure | HX011×1 | 26 | T4 tests | 5 | identical | tests/tui_gateway/test_isolated_orphan_activity.py |
| C-1639 | `tests/gateway/test_run_progress_topics.py` | test-sleep | HX013×19 | 242, 244, 259, 330, 332, 336, 338, 352, 374, 389, 391, 423 … | T4 tests | 42 | identical | tests/gateway/test_run_progress_topics.py |
| C-1640 | `tests/gateway/test_stream_consumer.py` | test-sleep | HX013×19 | 361, 363, 406, 408, 410, 447, 574, 606, 608, 651, 658, 705 … | T4 tests | 21 | diverged | tests/gateway/test_stream_consumer.py |
| C-1641 | `tests/gateway/test_stream_consumer_draft.py` | test-sleep | HX013×14 | 113, 115, 158, 160, 162, 216, 218, 243, 254, 285, 366, 368 … | T4 tests | 13 | identical | tests/gateway/test_stream_consumer_draft.py |
| C-1642 | `tests/agent/test_authorization_gate.py` | test-sleep | HX013×12 | 65, 71, 89, 99, 151, 209, 237, 240, 251, 257, 264, 289 | T4 tests | 2 | identical | tests/agent/test_authorization_gate.py |
| C-1643 | `tests/gateway/test_stream_consumer_wecom_native.py` | test-sleep | HX013×12 | 144, 166, 168, 192, 217, 221, 263, 288, 318, 349, 375, 514 | T4 tests | 3 | identical | tests/gateway/test_stream_consumer_wecom_native.py |
| C-1644 | `tests/gateway/test_api_server_runs.py` | test-sleep | HX013×10 | 753, 762, 1299, 1334, 1355, 1376, 1383, 1533, 1572, 1592 | T4 tests | 48 | diverged | tests/gateway/test_api_server_runs.py |
| C-1645 | `tests/gateway/test_wecom.py` | test-sleep | HX013×10 | 615, 619, 657, 742, 747, 767, 793, 795, 799, 816 | T4 tests | 20 | identical | tests/gateway/test_wecom.py |
| C-1646 | `tests/gateway/test_feishu_ws_multiplex_isolation.py` | test-sleep | HX013×9 | 34, 78, 121, 129, 164, 172, 201, 212, 337 | T4 tests | 6 | identical | tests/gateway/test_feishu_ws_multiplex_isolation.py |
| C-1647 | `tests/gateway/test_slack_socket_reconnect_heal.py` | test-sleep | HX013×9 | 99, 129, 141, 169, 177, 222, 226, 258, 261 | T4 tests | 3 | identical | tests/gateway/test_slack_socket_reconnect_heal.py |
| C-1648 | `tests/gateway/test_text_batching.py` | test-sleep | HX013×9 | 73, 85, 90, 130, 140, 144, 184, 194, 198 | T4 tests | 11 | identical | tests/gateway/test_text_batching.py |
| C-1649 | `tests/scripts/desktop_update/test_desktop_update_posix_handoff_protocol.py` | test-sleep | HX013×9 | 309, 378, 400, 428, 459, 488, 496, 534, 604 | T4 tests | 16 | identical | tests/scripts/desktop_update/test_desktop_update_posix_handoff_protocol.py |
| C-1650 | `tests/agent/test_codex_ttfb_watchdog.py` | test-sleep | HX013×8 | 224, 278, 354, 357, 392, 402, 407, 422 | T4 tests | 20 | identical | tests/agent/test_codex_ttfb_watchdog.py |
| C-1651 | `tests/agent/test_interrupt_propagation.py` | test-sleep | HX013×8 | 147, 155, 184, 191, 269, 277, 286, 288 | T4 tests | 2 | diverged | tests/agent/test_interrupt_propagation.py |
| C-1652 | `tests/gateway/relay/test_ws_transport.py` | test-sleep | HX013×8 | 132, 175, 208, 253, 295, 316, 439, 532 | T4 tests | 8 | identical | tests/gateway/relay/test_ws_transport.py |
| C-1653 | `tests/gateway/test_approval_boundary.py` | test-sleep | HX013×8 | 80, 121, 168, 213, 229, 281, 321, 360 | T4 tests | 2 | identical | tests/gateway/test_approval_boundary.py |
| C-1654 | `tests/gateway/test_telegram_text_batching.py` | test-sleep | HX013×8 | 76, 88, 95, 117, 128, 282, 283, 284 | T4 tests | 14 | identical | tests/gateway/test_telegram_text_batching.py |
| C-1655 | `tests/hermes_cli/test_update_custody.py` | test-sleep | HX013×8 | 409, 468, 471, 514, 569, 587, 600, 603 | T4 tests | 19 | identical | tests/hermes_cli/test_update_custody.py |
| C-1656 | `tests/agent/test_deadline.py` | test-sleep | HX013×7 | 239, 309, 352, 365, 383, 642, 718 | T4 tests | 11 | identical | tests/agent/test_deadline.py |
| C-1657 | `tests/agent/test_tool_activity_heartbeat.py` | test-sleep | HX013×7 | 107, 205, 238, 270, 309, 315, 364 | T4 tests | 2 | diverged | tests/agent/test_tool_activity_heartbeat.py |
| C-1658 | `tests/gateway/test_api_server.py` | test-sleep | HX013×7 | 604, 635, 1216, 1294, 1362, 1806, 1876 | T4 tests | 57 | identical | tests/gateway/test_api_server.py |
| C-1659 | `tests/gateway/test_buzz_websocket.py` | test-sleep | HX013×7 | 284, 413, 438, 486, 511, 645, 669 | T4 tests | 11 | identical | tests/gateway/test_buzz_websocket.py |
| C-1660 | `tests/gateway/test_startup_watchdog.py` | test-sleep | HX013×7 | 207, 239, 296, 335, 441, 502, 508 | T4 tests | 11 | identical | tests/gateway/test_startup_watchdog.py |
| C-1661 | `tests/gateway/test_stream_consumer_fresh_final.py` | test-sleep | HX013×7 | 122, 124, 151, 154, 202, 249, 281 | T4 tests | 8 | identical | tests/gateway/test_stream_consumer_fresh_final.py |
| C-1662 | `tests/tools/test_mcp_tool.py` | test-sleep | HX013×7 | 1013, 1027, 1060, 1366, 1978, 2088, 3450 | T4 tests | 63 | diverged | tests/tools/test_mcp_tool.py |
| C-1663 | `tests/tui_gateway/test_tui_gateway_server.py` | test-sleep | HX013×7 | 1178, 1217, 1350, 3868, 14952, 19732, 21297 | T4 tests | 115 | diverged | tests/tui_gateway/test_tui_gateway_server.py |
| C-1664 | `tests/agent/test_compress_context_progress_timeout.py` | test-sleep | HX013×6 | 231, 259, 289, 366, 428, 609 | T4 tests | 15 | identical | tests/agent/test_compress_context_progress_timeout.py |
| C-1665 | `tests/gateway/test_keep_typing_timeout.py` | test-sleep | HX013×6 | 67, 84, 130, 158, 213, 245 | T4 tests | 9 | identical | tests/gateway/test_keep_typing_timeout.py |
| C-1666 | `tests/gateway/test_update_streaming.py` | test-sleep | HX013×6 | 70, 196, 200, 203, 272, 274 | T4 tests | 15 | identical | tests/gateway/test_update_streaming.py |
| C-1667 | `tests/hermes_cli/test_cli_light_mode.py` | test-sleep | HX013×6 | 236, 281, 288, 361, 363, 373 | T4 tests | 3 | diverged | tests/hermes_cli/test_cli_light_mode.py |
| C-1668 | `tests/hermes_cli/test_pty_session.py` | test-sleep | HX013×6 | 93, 143, 158, 275, 291, 460 | T4 tests | 15 | identical | tests/hermes_cli/test_pty_session.py |
| C-1669 | `tests/tools/test_async_delegation.py` | test-sleep | HX013×6 | 331, 400, 471, 650, 1921, 2013 | T4 tests | 50 | diverged | tests/tools/test_async_delegation.py |
| C-1670 | `tests/tools/test_wake_word.py` | test-sleep | HX013×6 | 467, 514, 522, 698, 721, 744 | T4 tests | 42 | identical | tests/tools/test_wake_word.py |
| C-1671 | `tests/acp_adapter/test_session_construction_off_loop.py` | test-sleep | HX013×5 | 24, 42, 66, 77, 96 | T4 tests | 3 | identical | tests/acp_adapter/test_session_construction_off_loop.py |
| C-1672 | `tests/agent/test_codex_aux_no_progress_timeout.py` | test-sleep | HX013×5 | 71, 95, 115, 137, 191 | T4 tests | 3 | identical | tests/agent/test_codex_aux_no_progress_timeout.py |
| C-1673 | `tests/gateway/test_clarify_delivery_fallback.py` | test-sleep | HX013×5 | 127, 151, 158, 183, 208 | T4 tests | 4 | identical | tests/gateway/test_clarify_delivery_fallback.py |
| C-1674 | `tests/gateway/test_scale_to_zero_watcher.py` | test-sleep | HX013×5 | 44, 288, 388, 638, 660 | T4 tests | 18 | identical | tests/gateway/test_scale_to_zero_watcher.py |
| C-1675 | `tests/gateway/test_stream_consumer_tool_progress.py` | test-sleep | HX013×5 | 101, 105, 134, 137, 170 | T4 tests | 2 | identical | tests/gateway/test_stream_consumer_tool_progress.py |
| C-1676 | `tests/gateway/test_telegram_network_reconnect.py` | test-sleep | HX013×5 | 614, 704, 940, 975, 987 | T4 tests | 23 | identical | tests/gateway/test_telegram_network_reconnect.py |
| C-1677 | `tests/hermes_cli/test_serve_skill_maintenance.py` | test-sleep | HX013×5 | 29, 42, 46, 97, 98 | T4 tests | 5 | identical | tests/hermes_cli/test_serve_skill_maintenance.py |
| C-1678 | `tests/hermes_cli/test_web_server_config_offloop.py` | test-sleep | HX013×5 | 79, 155, 228, 294, 304 | T4 tests | 4 | identical | tests/hermes_cli/test_web_server_config_offloop.py |
| C-1679 | `tests/tools/test_clarify_gateway.py` | test-sleep | HX013×5 | 38, 90, 114, 149, 360 | T4 tests | 23 | diverged | tests/tools/test_clarify_gateway.py |
| C-1680 | `tests/cron/test_scheduler.py` | test-sleep | HX013×4 | 1749, 1811, 1819, 1838 | T4 tests | 66 | diverged | tests/cron/test_scheduler.py |
| C-1681 | `tests/gateway/test_telegram_init_deadline.py` | test-sleep | HX013×4 | 28, 58, 71, 104 | T4 tests | 12 | identical | tests/gateway/test_telegram_init_deadline.py |
| C-1682 | `tests/gateway/test_turn_lease.py` | test-sleep | HX013×4 | 56, 61, 80, 410 | T4 tests | 15 | identical | tests/gateway/test_turn_lease.py |
| C-1683 | `tests/gateway/test_webhook_adapter.py` | test-sleep | HX013×4 | 437, 480, 695, 1237 | T4 tests | 31 | diverged | tests/gateway/test_webhook_adapter.py |
| C-1684 | `tests/gateway/test_weixin.py` | test-sleep | HX013×4 | 194, 199, 599, 648 | T4 tests | 22 | fork-only | tests/gateway/test_weixin.py |
| C-1685 | `tests/hermes_cli/test_modify_other_keys_aliases.py` | test-sleep | HX013×4 | 417, 421, 469, 473 | T4 tests | 5 | identical | tests/hermes_cli/test_modify_other_keys_aliases.py |
| C-1686 | `tests/hermes_cli/test_plugins.py` | test-sleep | HX013×4 | 1385, 1388, 1487, 2586 | T4 tests | 61 | diverged | tests/hermes_cli/test_plugins.py |
| C-1687 | `tests/hermes_cli/test_proxy_off_loop.py` | test-sleep | HX013×4 | 115, 123, 139, 204 | T4 tests | 5 | identical | tests/hermes_cli/test_proxy_off_loop.py |
| C-1688 | `tests/hermes_state/test_session_turn_lease.py` | test-sleep | HX013×4 | 34, 290, 313, 529 | T4 tests | 4 | identical | tests/hermes_state/test_session_turn_lease.py |
| C-1689 | `tests/hermes_state/test_state_db_repair_loop_mtime.py` | test-sleep | HX013×4 | 51, 83, 108, 216 | T4 tests | 2 | identical | tests/hermes_state/test_state_db_repair_loop_mtime.py |
| C-1690 | `tests/tools/test_browser_supervisor.py` | test-sleep | HX013×4 | 194, 232, 256, 320 | T4 tests | 13 | identical | tests/tools/test_browser_supervisor.py |
| C-1691 | `tests/tools/test_file_state_registry.py` | test-sleep | HX013×4 | 71, 88, 115, 148 | T4 tests | 14 | identical | tests/tools/test_file_state_registry.py |
| C-1692 | `tests/agent/test_auxiliary_client.py` | test-sleep | HX013×3 | 3917, 3949, 4680 | T4 tests | 127 | diverged | tests/agent/test_auxiliary_client.py |
| C-1693 | `tests/agent/test_auxiliary_concurrency.py` | test-sleep | HX013×3 | 98, 215, 243 | T4 tests | 3 | identical | tests/agent/test_auxiliary_concurrency.py |
| C-1694 | `tests/agent/test_compression_review.py` | test-sleep | HX013×3 | 361, 441, 512 | T4 tests | 4 | diverged | tests/agent/test_compression_review.py |
| C-1695 | `tests/agent/test_cross_process_turn_lease.py` | test-sleep | HX013×3 | 618, 670, 680 | T4 tests | 14 | identical | tests/agent/test_cross_process_turn_lease.py |
| C-1696 | `tests/agent/test_fast_compression_lane.py` | test-sleep | HX013×3 | 109, 120, 278 | T4 tests | 11 | identical | tests/agent/test_fast_compression_lane.py |
| C-1697 | `tests/agent/test_sequential_tool_interrupt.py` | test-sleep | HX013×3 | 59, 74, 105 | T4 tests | 3 | identical | tests/agent/test_sequential_tool_interrupt.py |
| C-1698 | `tests/agent/test_turn_liveness_watchdog.py` | test-sleep | HX013×3 | 267, 312, 367 | T4 tests | 5 | identical | tests/agent/test_turn_liveness_watchdog.py |
| C-1699 | `tests/gateway/relay/test_relay_seal_cancellation.py` | test-sleep | HX013×3 | 33, 48, 71 | T4 tests | 1 | identical | tests/gateway/relay/test_relay_seal_cancellation.py |
| C-1700 | `tests/gateway/test_busy_requeue_hot_loop.py` | test-sleep | HX013×3 | 123, 147, 151 | T4 tests | 6 | identical | tests/gateway/test_busy_requeue_hot_loop.py |
| C-1701 | `tests/gateway/test_cancel_background_drain.py` | test-sleep | HX013×3 | 64, 69, 74 | T4 tests | 8 | identical | tests/gateway/test_cancel_background_drain.py |
| C-1702 | `tests/gateway/test_external_drain_control.py` | test-sleep | HX013×3 | 284, 287, 290 | T4 tests | 11 | identical | tests/gateway/test_external_drain_control.py |
| C-1703 | `tests/gateway/test_notify_fatal_error_shield.py` | test-sleep | HX013×3 | 45, 60, 78 | T4 tests | 2 | identical | tests/gateway/test_notify_fatal_error_shield.py |
| C-1704 | `tests/gateway/test_restart_drain.py` | test-sleep | HX013×3 | 174, 236, 517 | T4 tests | 25 | identical | tests/gateway/test_restart_drain.py |
| C-1705 | `tests/gateway/test_run_cleanup_progress.py` | test-sleep | HX013×3 | 118, 120, 133 | T4 tests | 10 | identical | tests/gateway/test_run_cleanup_progress.py |
| C-1706 | `tests/gateway/test_split_final_suffix_reconcile.py` | test-sleep | HX013×3 | 63, 93, 106 | T4 tests | 1 | identical | tests/gateway/test_split_final_suffix_reconcile.py |
| C-1707 | `tests/gateway/test_sse_agent_cancel.py` | test-sleep | HX013×3 | 137, 393, 428 | T4 tests | 13 | identical | tests/gateway/test_sse_agent_cancel.py |
| C-1708 | `tests/gateway/test_startup_connect_parallel.py` | test-sleep | HX013×3 | 67, 145, 244 | T4 tests | 4 | identical | tests/gateway/test_startup_connect_parallel.py |
| C-1709 | `tests/gateway/test_stream_abandon_on_turn_death.py` | test-sleep | HX013×3 | 55, 70, 96 | T4 tests | 2 | identical | tests/gateway/test_stream_abandon_on_turn_death.py |
| C-1710 | `tests/gateway/test_telegram_rich_messages.py` | test-sleep | HX013×3 | 465, 535, 587 | T4 tests | 19 | identical | tests/gateway/test_telegram_rich_messages.py |
| C-1711 | `tests/gateway/test_webhook_coalescing.py` | test-sleep | HX013×3 | 85, 109, 110 | T4 tests | 2 | diverged | tests/gateway/test_webhook_coalescing.py |
| C-1712 | `tests/gateway/test_wecom_per_turn.py` | test-sleep | HX013×3 | 257, 330, 383 | T4 tests | 3 | identical | tests/gateway/test_wecom_per_turn.py |
| C-1713 | `tests/gateway/test_yuanbao_pipeline.py` | test-sleep | HX013×3 | 1009, 1045, 1078 | T4 tests | 3 | diverged | tests/gateway/test_yuanbao_pipeline.py |
| C-1714 | `tests/hermes_cli/test_display_ws_drop_keeps_lease.py` | test-sleep | HX013×3 | 27, 43, 112 | T4 tests | 5 | identical | tests/hermes_cli/test_display_ws_drop_keeps_lease.py |
| C-1715 | `tests/hermes_cli/test_local_runtime.py` | test-sleep | HX013×3 | 432, 470, 939 | T4 tests | 30 | diverged | tests/hermes_cli/test_local_runtime.py |
| C-1716 | `tests/hermes_cli/test_pty_bridge.py` | test-sleep | HX013×3 | 237, 342, 410 | T4 tests | 16 | identical | tests/hermes_cli/test_pty_bridge.py |
| C-1717 | `tests/hermes_cli/test_update_lock_windows_live.py` | test-sleep | HX013×3 | 288, 450, 599 | T4 tests | 17 | identical | tests/hermes_cli/test_update_lock_windows_live.py |
| C-1718 | `tests/hermes_cli/test_update_pause_unmapped_home_proof.py` | test-sleep | HX013×3 | 57, 79, 96 | T4 tests | 1 | identical | tests/hermes_cli/test_update_pause_unmapped_home_proof.py |
| C-1719 | `tests/hermes_cli/test_web_server_boot_handshake.py` | test-sleep | HX013×3 | 33, 121, 166 | T4 tests | 16 | diverged | tests/hermes_cli/test_web_server_boot_handshake.py |
| C-1720 | `tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py` | test-sleep | HX013×3 | 50, 73, 143 | T4 tests | 4 | identical | tests/scripts/desktop_update/test_desktop_update_windows_marker_lock.py |
| C-1721 | `tests/tools/test_approval.py` | test-sleep | HX013×3 | 787, 1894, 2093 | T4 tests | 43 | diverged | tests/tools/test_approval.py |
| C-1722 | `tests/tools/test_code_kernel.py` | test-sleep | HX013×3 | 139, 382, 566 | T4 tests | 19 | identical | tests/tools/test_code_kernel.py |
| C-1723 | `tests/tools/test_image_generation_interrupt.py` | test-sleep | HX013×3 | 20, 33, 62 | T4 tests | 2 | identical | tests/tools/test_image_generation_interrupt.py |
| C-1724 | `tests/tools/test_mcp_cancelled_error_propagation.py` | test-sleep | HX013×3 | 27, 43, 81 | T4 tests | 8 | identical | tests/tools/test_mcp_cancelled_error_propagation.py |
| C-1725 | `tests/tools/test_mcp_stdio_fastfail_reconnect.py` | test-sleep | HX013×3 | 148, 157, 203 | T4 tests | 9 | identical | tests/tools/test_mcp_stdio_fastfail_reconnect.py |
| C-1726 | `tests/tools/test_mcp_tool_issue_948.py` | test-sleep | HX013×3 | 147, 155, 183 | T4 tests | 24 | identical | tests/tools/test_mcp_tool_issue_948.py |
| C-1727 | `tests/tools/test_oneshot_completion_linger.py` | test-sleep | HX013×3 | 93, 107, 162 | T4 tests | 11 | identical | tests/tools/test_oneshot_completion_linger.py |
| C-1728 | `tests/tools/test_tts_streaming.py` | test-sleep | HX013×3 | 797, 953, 961 | T4 tests | 30 | identical | tests/tools/test_tts_streaming.py |
| C-1729 | `tests/tui_gateway/test_change_watcher.py` | test-sleep | HX013×3 | 115, 136, 317 | T4 tests | 14 | diverged | tests/tui_gateway/test_change_watcher.py |
| C-1730 | `tests/tui_gateway/test_hosted_room_driver_runtime.py` | test-sleep | HX013×3 | 1096, 1128, 1501 | T4 tests | 7 | diverged | tests/tui_gateway/test_hosted_room_driver_runtime.py |
| C-1731 | `tests/tui_gateway/test_protocol.py` | test-sleep | HX013×3 | 446, 469, 1856 | T4 tests | 83 | diverged | tests/tui_gateway/test_protocol.py |
| C-1732 | `tests/tui_gateway/test_serve_exit_flush.py` | test-sleep | HX013×3 | 124, 139, 193 | T4 tests | 7 | identical | tests/tui_gateway/test_serve_exit_flush.py |
| C-1733 | `tests/acp_adapter/test_acp_mcp_discovery.py` | test-sleep | HX013×2 | 263, 326 | T4 tests | 5 | identical | tests/acp_adapter/test_acp_mcp_discovery.py |
| C-1734 | `tests/acp_adapter/test_approval_isolation.py` | test-sleep | HX013×2 | 67, 73 | T4 tests | 3 | identical | tests/acp_adapter/test_approval_isolation.py |
| C-1735 | `tests/agent/lsp/_mock_lsp_server.py` | test-sleep | HX013×2 | 139, 237 | T4 tests | 17 | diverged | tests/agent/lsp/_mock_lsp_server.py |
| C-1736 | `tests/agent/test_anthropic_oauth_stress.py` | test-sleep | HX013×2 | 92, 156 | T4 tests | 8 | identical | tests/agent/test_anthropic_oauth_stress.py |
| C-1737 | `tests/agent/test_aux_progress_streaming.py` | test-sleep | HX013×2 | 300, 421 | T4 tests | 18 | identical | tests/agent/test_aux_progress_streaming.py |
| C-1738 | `tests/agent/test_aux_stream_host_deadline_sibling_wires.py` | test-sleep | HX013×2 | 67, 119 | T4 tests | 2 | identical | tests/agent/test_aux_stream_host_deadline_sibling_wires.py |
| C-1739 | `tests/agent/test_cascading_interrupt.py` | test-sleep | HX013×2 | 62, 124 | T4 tests | 1 | identical | tests/agent/test_cascading_interrupt.py |
| C-1740 | `tests/agent/test_codex_aux_timeout_fd_ownership.py` | test-sleep | HX013×2 | 106, 192 | T4 tests | 5 | identical | tests/agent/test_codex_aux_timeout_fd_ownership.py |
| C-1741 | `tests/agent/test_compression_concurrent_fork.py` | test-sleep | HX013×2 | 76, 1956 | T4 tests | 48 | identical | tests/agent/test_compression_concurrent_fork.py |
| C-1742 | `tests/agent/test_compression_stall_deterministic_fallback.py` | test-sleep | HX013×2 | 195, 257 | T4 tests | 7 | identical | tests/agent/test_compression_stall_deterministic_fallback.py |
| C-1743 | `tests/agent/test_compression_worker_isolation.py` | test-sleep | HX013×2 | 134, 315 | T4 tests | 1 | identical | tests/agent/test_compression_worker_isolation.py |
| C-1744 | `tests/agent/test_direct_contexts_stream_inline.py` | test-sleep | HX013×2 | 73, 226 | T4 tests | 3 | identical | tests/agent/test_direct_contexts_stream_inline.py |
| C-1745 | `tests/agent/test_memory_async_sync.py` | test-sleep | HX013×2 | 55, 59 | T4 tests | 7 | identical | tests/agent/test_memory_async_sync.py |
| C-1746 | `tests/agent/test_periodic_scheduler.py` | test-sleep | HX013×2 | 31, 61 | T4 tests | 4 | identical | tests/agent/test_periodic_scheduler.py |
| C-1747 | `tests/agent/test_run_agent.py` | test-sleep | HX013×2 | 1917, 6811 | T4 tests | 46 | diverged | tests/agent/test_run_agent.py |
| C-1748 | `tests/agent/test_start_order_gate.py` | test-sleep | HX013×2 | 227, 251 | T4 tests | 3 | diverged | tests/agent/test_start_order_gate.py |
| C-1749 | `tests/agent/test_thread_scoped_output.py` | test-sleep | HX013×2 | 116, 120 | T4 tests | 12 | diverged | tests/agent/test_thread_scoped_output.py |
| C-1750 | `tests/agent/transports/test_codex_app_server.py` | test-sleep | HX013×2 | 47, 89 | T4 tests | 3 | identical | tests/agent/transports/test_codex_app_server.py |
| C-1751 | `tests/agent/transports/test_codex_app_server_session.py` | test-sleep | HX013×2 | 83, 1140 | T4 tests | 30 | identical | tests/agent/transports/test_codex_app_server_session.py |
| C-1752 | `tests/cron/test_cron_direct_api_call.py` | test-sleep | HX013×2 | 118, 120 | T4 tests | 4 | identical | tests/cron/test_cron_direct_api_call.py |
| C-1753 | `tests/cron/test_cron_direct_api_call_watchdog.py` | test-sleep | HX013×2 | 191, 400 | T4 tests | 9 | identical | tests/cron/test_cron_direct_api_call_watchdog.py |
| C-1754 | `tests/cron/test_cron_inactivity_timeout.py` | test-sleep | HX013×2 | 33, 73 | T4 tests | 13 | identical | tests/cron/test_cron_inactivity_timeout.py |
| C-1755 | `tests/fakes/providers/copilot_acp.py` | test-sleep | HX013×2 | 327, 436 | T4 tests | 3 | identical | tests/fakes/providers/copilot_acp.py |
| C-1756 | `tests/gateway/relay/test_relay_going_idle.py` | test-sleep | HX013×2 | 130, 167 | T4 tests | 9 | identical | tests/gateway/relay/test_relay_going_idle.py |
| C-1757 | `tests/gateway/relay/test_relay_prompt_ack_stream_isolation.py` | test-sleep | HX013×2 | 123, 146 | T4 tests | 4 | identical | tests/gateway/relay/test_relay_prompt_ack_stream_isolation.py |
| C-1758 | `tests/gateway/relay/test_relay_threads.py` | test-sleep | HX013×2 | 511, 552 | T4 tests | 9 | identical | tests/gateway/relay/test_relay_threads.py |
| C-1759 | `tests/gateway/relay/test_ws_transport_hardening.py` | test-sleep | HX013×2 | 154, 354 | T4 tests | 10 | identical | tests/gateway/relay/test_ws_transport_hardening.py |
| C-1760 | `tests/gateway/test_api_server_active_work_drain.py` | test-sleep | HX013×2 | 157, 195 | T4 tests | 22 | identical | tests/gateway/test_api_server_active_work_drain.py |
| C-1761 | `tests/gateway/test_api_server_sse_keepalive.py` | test-sleep | HX013×2 | 56, 83 | T4 tests | 3 | identical | tests/gateway/test_api_server_sse_keepalive.py |
| C-1762 | `tests/gateway/test_cleanup_off_loop.py` | test-sleep | HX013×2 | 84, 98 | T4 tests | 2 | identical | tests/gateway/test_cleanup_off_loop.py |
| C-1763 | `tests/gateway/test_compress_command.py` | test-sleep | HX013×2 | 475, 484 | T4 tests | 24 | identical | tests/gateway/test_compress_command.py |
| C-1764 | `tests/gateway/test_cron_drain_floor.py` | test-sleep | HX013×2 | 57, 79 | T4 tests | 4 | identical | tests/gateway/test_cron_drain_floor.py |
| C-1765 | `tests/gateway/test_discord_free_response.py` | test-sleep | HX013×2 | 1049, 1054 | T4 tests | 18 | identical | tests/gateway/test_discord_free_response.py |
| C-1766 | `tests/gateway/test_discord_missed_message_backfill.py` | test-sleep | HX013×2 | 380, 391 | T4 tests | 23 | identical | tests/gateway/test_discord_missed_message_backfill.py |
| C-1767 | `tests/gateway/test_feishu.py` | test-sleep | HX013×2 | 1681, 1686 | T4 tests | 44 | identical | tests/gateway/test_feishu.py |
| C-1768 | `tests/gateway/test_gateway_shutdown.py` | test-sleep | HX013×2 | 375, 383 | T4 tests | 29 | identical | tests/gateway/test_gateway_shutdown.py |
| C-1769 | `tests/gateway/test_loop_liveness_watchdog.py` | test-sleep | HX013×2 | 308, 318 | T4 tests | 17 | identical | tests/gateway/test_loop_liveness_watchdog.py |
| C-1770 | `tests/gateway/test_matrix.py` | test-sleep | HX013×2 | 1826, 1849 | T4 tests | 30 | identical | tests/gateway/test_matrix.py |
| C-1771 | `tests/gateway/test_media_resend_dedup.py` | test-sleep | HX013×2 | 314, 343 | T4 tests | 3 | identical | tests/gateway/test_media_resend_dedup.py |
| C-1772 | `tests/gateway/test_multiplex_hot_serve.py` | test-sleep | HX013×2 | 142, 291 | T4 tests | 14 | diverged | tests/gateway/test_multiplex_hot_serve.py |
| C-1773 | `tests/gateway/test_planned_stop_watcher.py` | test-sleep | HX013×2 | 124, 174 | T4 tests | 9 | identical | tests/gateway/test_planned_stop_watcher.py |
| C-1774 | `tests/gateway/test_platform_reconnect.py` | test-sleep | HX013×2 | 811, 1029 | T4 tests | 39 | identical | tests/gateway/test_platform_reconnect.py |
| C-1775 | `tests/gateway/test_relay_delivery_followups.py` | test-sleep | HX013×2 | 192, 342 | T4 tests | 2 | identical | tests/gateway/test_relay_delivery_followups.py |
| C-1776 | `tests/gateway/test_reset_button_deadlock.py` | test-sleep | HX013×2 | 115, 132 | T4 tests | 2 | identical | tests/gateway/test_reset_button_deadlock.py |
| C-1777 | `tests/gateway/test_restart_resume_pending.py` | test-sleep | HX013×2 | 1117, 1214 | T4 tests | 25 | identical | tests/gateway/test_restart_resume_pending.py |
| C-1778 | `tests/gateway/test_run_progress_interrupt.py` | test-sleep | HX013×2 | 74, 105 | T4 tests | 7 | identical | tests/gateway/test_run_progress_interrupt.py |
| C-1779 | `tests/gateway/test_session_env.py` | test-sleep | HX013×2 | 172, 180 | T4 tests | 11 | diverged | tests/gateway/test_session_env.py |
| C-1780 | `tests/gateway/test_startup_warmup_logger_offloop.py` | test-sleep | HX013×2 | 47, 60 | T4 tests | 1 | identical | tests/gateway/test_startup_warmup_logger_offloop.py |
| C-1781 | `tests/gateway/test_stream_consumer_oversized_leftover.py` | test-sleep | HX013×2 | 98, 106 | T4 tests | 4 | identical | tests/gateway/test_stream_consumer_oversized_leftover.py |
| C-1782 | `tests/gateway/test_stream_edit_flood_retry_after.py` | test-sleep | HX013×2 | 27, 30 | T4 tests | 1 | identical | tests/gateway/test_stream_edit_flood_retry_after.py |
| C-1783 | `tests/gateway/test_stream_final_contract.py` | test-sleep | HX013×2 | 77, 102 | T4 tests | 1 | identical | tests/gateway/test_stream_final_contract.py |
| C-1784 | `tests/gateway/test_streaming_tts_consumer.py` | test-sleep | HX013×2 | 575, 654 | T4 tests | 10 | identical | tests/gateway/test_streaming_tts_consumer.py |
| C-1785 | `tests/gateway/test_telegram_send_path_health.py` | test-sleep | HX013×2 | 89, 110 | T4 tests | 14 | identical | tests/gateway/test_telegram_send_path_health.py |
| C-1786 | `tests/gateway/test_telegram_send_reconnect_wait.py` | test-sleep | HX013×2 | 41, 66 | T4 tests | 2 | identical | tests/gateway/test_telegram_send_reconnect_wait.py |
| C-1787 | `tests/gateway/test_voice_command.py` | test-sleep | HX013×2 | 1494, 1496 | T4 tests | 25 | identical | tests/gateway/test_voice_command.py |
| C-1788 | `tests/hermes_cli/test_api_key_providers.py` | test-sleep | HX013×2 | 612, 638 | T4 tests | 29 | identical | tests/hermes_cli/test_api_key_providers.py |
| C-1789 | `tests/hermes_cli/test_approval_transport.py` | test-sleep | HX013×2 | 170, 190 | T4 tests | 7 | identical | tests/hermes_cli/test_approval_transport.py |
| C-1790 | `tests/hermes_cli/test_credential_pool_off_loop.py` | test-sleep | HX013×2 | 105, 140 | T4 tests | 4 | identical | tests/hermes_cli/test_credential_pool_off_loop.py |
| C-1791 | `tests/hermes_cli/test_cross_profile_kill_refusal.py` | test-sleep | HX013×2 | 133, 192 | T4 tests | 3 | identical | tests/hermes_cli/test_cross_profile_kill_refusal.py |
| C-1792 | `tests/hermes_cli/test_gitlock.py` | test-sleep | HX013×2 | 554, 556 | T4 tests | 22 | diverged | tests/hermes_cli/test_gitlock.py |
| C-1793 | `tests/hermes_cli/test_service_manager.py` | test-sleep | HX013×2 | 554, 559 | T4 tests | 21 | diverged | tests/hermes_cli/test_service_manager.py |
| C-1794 | `tests/hermes_cli/test_slash_confirm_windows.py` | test-sleep | HX013×2 | 237, 293 | T4 tests | 4 | identical | tests/hermes_cli/test_slash_confirm_windows.py |
| C-1795 | `tests/hermes_cli/test_update_wedged_gateway.py` | test-sleep | HX013×2 | 122, 130 | T4 tests | 19 | identical | tests/hermes_cli/test_update_wedged_gateway.py |
| C-1796 | `tests/hermes_cli/test_web_read_coalescing.py` | test-sleep | HX013×2 | 12, 56 | T4 tests | 3 | identical | tests/hermes_cli/test_web_read_coalescing.py |
| C-1797 | `tests/hermes_cli/test_win_pty_bridge.py` | test-sleep | HX013×2 | 137, 165 | T4 tests | 12 | identical | tests/hermes_cli/test_win_pty_bridge.py |
| C-1798 | `tests/hermes_state/test_shared_session_db_registry.py` | test-sleep | HX013×2 | 232, 283 | T4 tests | 8 | identical | tests/hermes_state/test_shared_session_db_registry.py |
| C-1799 | `tests/hermes_state/test_write_lock_patience.py` | test-sleep | HX013×2 | 36, 166 | T4 tests | 2 | identical | tests/hermes_state/test_write_lock_patience.py |
| C-1800 | `tests/plugins/test_plugin_utils.py` | test-sleep | HX013×2 | 63, 122 | T4 tests | 1 | identical | tests/plugins/test_plugin_utils.py |
| C-1801 | `tests/plugins/test_telegram_update_admission.py` | test-sleep | HX013×2 | 828, 830 | T4 tests | 11 | identical | tests/plugins/test_telegram_update_admission.py |
| C-1802 | `tests/pm/_range_server.py` | test-sleep | HX013×2 | 54, 88 | T4 tests | 4 | identical | tests/pm/_range_server.py |
| C-1803 | `tests/run_interrupt_test.py` | test-sleep | HX013×2 | 59, 114 | T4 tests | 7 | identical | tests/run_interrupt_test.py |
| C-1804 | `tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py` | test-sleep | HX013×2 | 73, 108 | T4 tests | 3 | identical | tests/scripts/desktop_update/test_desktop_update_windows_handoff_lineage.py |
| C-1805 | `tests/test_model_tools_async_bridge.py` | test-sleep | HX013×2 | 149, 211 | T4 tests | 10 | identical | tests/test_model_tools_async_bridge.py |
| C-1806 | `tests/tools/test_bot_mode_dm.py` | test-sleep | HX013×2 | 672, 1250 | T4 tests | 41 | diverged | tests/tools/test_bot_mode_dm.py |
| C-1807 | `tests/tools/test_browser_supervisor_capture_dispatch.py` | test-sleep | HX013×2 | 25, 66 | T4 tests | 3 | identical | tests/tools/test_browser_supervisor_capture_dispatch.py |
| C-1808 | `tests/tools/test_code_execution.py` | test-sleep | HX013×2 | 390, 787 | T4 tests | 38 | identical | tests/tools/test_code_execution.py |
| C-1809 | `tests/tools/test_file_staleness.py` | test-sleep | HX013×2 | 219, 262 | T4 tests | 12 | identical | tests/tools/test_file_staleness.py |
| C-1810 | `tests/tools/test_local_interrupt_cleanup.py` | test-sleep | HX013×2 | 292, 311 | T4 tests | 16 | identical | tests/tools/test_local_interrupt_cleanup.py |
| C-1811 | `tests/tools/test_mcp_poll_loop_oom_integration.py` | test-sleep | HX013×2 | 40, 65 | T4 tests | 2 | identical | tests/tools/test_mcp_poll_loop_oom_integration.py |
| C-1812 | `tests/tools/test_mcp_stability.py` | test-sleep | HX013×2 | 549, 591 | T4 tests | 18 | identical | tests/tools/test_mcp_stability.py |
| C-1813 | `tests/tools/test_memory_extraction.py` | test-sleep | HX013×2 | 461, 491 | T4 tests | 5 | fork-only | tests/tools/test_memory_extraction.py |
| C-1814 | `tests/tools/test_send_message_cross_loop.py` | test-sleep | HX013×2 | 149, 185 | T4 tests | 1 | identical | tests/tools/test_send_message_cross_loop.py |
| C-1815 | `tests/tools/test_terminal_bounded_execute.py` | test-sleep | HX013×2 | 27, 78 | T4 tests | 2 | identical | tests/tools/test_terminal_bounded_execute.py |
| C-1816 | `tests/tools/test_vision_tools.py` | test-sleep | HX013×2 | 1072, 1086 | T4 tests | 19 | identical | tests/tools/test_vision_tools.py |
| C-1817 | `tests/tools/test_web_extract_timeout.py` | test-sleep | HX013×2 | 20, 31 | T4 tests | 1 | identical | tests/tools/test_web_extract_timeout.py |
| C-1818 | `tests/tui_gateway/test_readiness_singleflight.py` | test-sleep | HX013×2 | 249, 251 | T4 tests | 2 | identical | tests/tui_gateway/test_readiness_singleflight.py |
| C-1819 | `tests/tui_gateway/test_tui_gateway_ws.py` | test-sleep | HX013×2 | 262, 302 | T4 tests | 7 | diverged | tests/tui_gateway/test_tui_gateway_ws.py |
| C-1820 | `tests/acp_adapter/test_ping_suppression.py` | test-sleep | HX013×1 | 169 | T4 tests | 3 | identical | tests/acp_adapter/test_ping_suppression.py |
| C-1821 | `tests/acp_adapter/test_session_provenance.py` | test-sleep | HX013×1 | 42 | T4 tests | 1 | identical | tests/acp_adapter/test_session_provenance.py |
| C-1822 | `tests/agent/lsp/test_broken_set.py` | test-sleep | HX013×1 | 173 | T4 tests | 9 | identical | tests/agent/lsp/test_broken_set.py |
| C-1823 | `tests/agent/lsp/test_versionless_push_during_send.py` | test-sleep | HX013×1 | 42 | T4 tests | 1 | identical | tests/agent/lsp/test_versionless_push_during_send.py |
| C-1824 | `tests/agent/test_account_usage_fetch.py` | test-sleep | HX013×1 | 356 | T4 tests | 9 | diverged | tests/agent/test_account_usage_fetch.py |
| C-1825 | `tests/agent/test_anthropic_keychain.py` | test-sleep | HX013×1 | 400 | T4 tests | 17 | diverged | tests/agent/test_anthropic_keychain.py |
| C-1826 | `tests/agent/test_async_token_accounting.py` | test-sleep | HX013×1 | 215 | T4 tests | 7 | identical | tests/agent/test_async_token_accounting.py |
| C-1827 | `tests/agent/test_async_utils.py` | test-sleep | HX013×1 | 53 | T4 tests | 7 | identical | tests/agent/test_async_utils.py |
| C-1828 | `tests/agent/test_auxiliary_explicit_cancellation.py` | test-sleep | HX013×1 | 298 | T4 tests | 5 | identical | tests/agent/test_auxiliary_explicit_cancellation.py |
| C-1829 | `tests/agent/test_bounded_response.py` | test-sleep | HX013×1 | 55 | T4 tests | 9 | identical | tests/agent/test_bounded_response.py |
| C-1830 | `tests/agent/test_compression_attempt_lifecycle.py` | test-sleep | HX013×1 | 94 | T4 tests | 11 | identical | tests/agent/test_compression_attempt_lifecycle.py |
| C-1831 | `tests/agent/test_compression_stall_fallback.py` | test-sleep | HX013×1 | 283 | T4 tests | 4 | diverged | tests/agent/test_compression_stall_fallback.py |
| C-1832 | `tests/agent/test_credential_pool_anthropic_refresh_race.py` | test-sleep | HX013×1 | 106 | T4 tests | 8 | identical | tests/agent/test_credential_pool_anthropic_refresh_race.py |
| C-1833 | `tests/agent/test_credential_pool_refresh_sweep_spacing.py` | test-sleep | HX013×1 | 30 | T4 tests | 2 | identical | tests/agent/test_credential_pool_refresh_sweep_spacing.py |
| C-1834 | `tests/agent/test_memory_boundary_commit.py` | test-sleep | HX013×1 | 49 | T4 tests | 8 | identical | tests/agent/test_memory_boundary_commit.py |
| C-1835 | `tests/agent/test_plugin_stream_hooks.py` | test-sleep | HX013×1 | 48 | T4 tests | 4 | identical | tests/agent/test_plugin_stream_hooks.py |
| C-1836 | `tests/agent/test_prompt_builder.py` | test-sleep | HX013×1 | 1028 | T4 tests | 46 | identical | tests/agent/test_prompt_builder.py |
| C-1837 | `tests/agent/test_responses_watchdog_accommodations.py` | test-sleep | HX013×1 | 130 | T4 tests | 4 | identical | tests/agent/test_responses_watchdog_accommodations.py |
| C-1838 | `tests/agent/test_rotation_flush_persisted_boundary.py` | test-sleep | HX013×1 | 58 | T4 tests | 1 | identical | tests/agent/test_rotation_flush_persisted_boundary.py |
| C-1839 | `tests/agent/test_sequential_tool_timeout.py` | test-sleep | HX013×1 | 315 | T4 tests | 3 | identical | tests/agent/test_sequential_tool_timeout.py |
| C-1840 | `tests/agent/test_shell_hooks_consent.py` | test-sleep | HX013×1 | 175 | T4 tests | 10 | identical | tests/agent/test_shell_hooks_consent.py |
| C-1841 | `tests/agent/test_shell_hooks_tree_kill.py` | test-sleep | HX013×1 | 121 | T4 tests | 5 | identical | tests/agent/test_shell_hooks_tree_kill.py |
| C-1842 | `tests/agent/test_skill_bundles.py` | test-sleep | HX013×1 | 115 | T4 tests | 10 | diverged | tests/agent/test_skill_bundles.py |
| C-1843 | `tests/agent/test_stream_interrupt_retry.py` | test-sleep | HX013×1 | 208 | T4 tests | 3 | identical | tests/agent/test_stream_interrupt_retry.py |
| C-1844 | `tests/agent/test_stream_runaway_repetition.py` | test-sleep | HX013×1 | 107 | T4 tests | 5 | diverged | tests/agent/test_stream_runaway_repetition.py |
| C-1845 | `tests/agent/test_stream_stale_circuit_breaker.py` | test-sleep | HX013×1 | 189 | T4 tests | 4 | diverged | tests/agent/test_stream_stale_circuit_breaker.py |
| C-1846 | `tests/agent/test_subdirectory_hints.py` | test-sleep | HX013×1 | 179 | T4 tests | 25 | identical | tests/agent/test_subdirectory_hints.py |
| C-1847 | `tests/agent/test_treekill_consolidation.py` | test-sleep | HX013×1 | 147 | T4 tests | 8 | identical | tests/agent/test_treekill_consolidation.py |
| C-1848 | `tests/cron/test_claim_job_for_fire.py` | test-sleep | HX013×1 | 204 | T4 tests | 19 | identical | tests/cron/test_claim_job_for_fire.py |
| C-1849 | `tests/cron/test_cron_context_from.py` | test-sleep | HX013×1 | 86 | T4 tests | 11 | identical | tests/cron/test_cron_context_from.py |
| C-1850 | `tests/cron/test_jobs_crossprocess_lock.py` | test-sleep | HX013×1 | 115 | T4 tests | 6 | identical | tests/cron/test_jobs_crossprocess_lock.py |
| C-1851 | `tests/cron/test_parallel_pool.py` | test-sleep | HX013×1 | 234 | T4 tests | 19 | identical | tests/cron/test_parallel_pool.py |
| C-1852 | `tests/cron/test_script_claim_heartbeat.py` | test-sleep | HX013×1 | 379 | T4 tests | 27 | diverged | tests/cron/test_script_claim_heartbeat.py |
| C-1853 | `tests/fakes/fake_llm_provider.py` | test-sleep | HX013×1 | 337 | T4 tests | 11 | diverged | tests/fakes/fake_llm_provider.py |
| C-1854 | `tests/fakes/providers/codex_app_server.py` | test-sleep | HX013×1 | 572 | T4 tests | 2 | diverged | tests/fakes/providers/codex_app_server.py |
| C-1855 | `tests/fakes/providers/gemini_native.py` | test-sleep | HX013×1 | 723 | T4 tests | 5 | identical | tests/fakes/providers/gemini_native.py |
| C-1856 | `tests/gateway/relay/test_relay_adapter.py` | test-sleep | HX013×1 | 268 | T4 tests | 17 | identical | tests/gateway/relay/test_relay_adapter.py |
| C-1857 | `tests/gateway/relay/test_relay_interactive.py` | test-sleep | HX013×1 | 440 | T4 tests | 8 | identical | tests/gateway/relay/test_relay_interactive.py |
| C-1858 | `tests/gateway/test_active_session_text_merge.py` | test-sleep | HX013×1 | 218 | T4 tests | 10 | identical | tests/gateway/test_active_session_text_merge.py |
| C-1859 | `tests/gateway/test_adapter_session_key_seam.py` | test-sleep | HX013×1 | 60 | T4 tests | 1 | diverged | tests/gateway/test_adapter_session_key_seam.py |
| C-1860 | `tests/gateway/test_api_server_artifact_store_async.py` | test-sleep | HX013×1 | 26 | T4 tests | 1 | identical | tests/gateway/test_api_server_artifact_store_async.py |
| C-1861 | `tests/gateway/test_approve_deny_commands.py` | test-sleep | HX013×1 | 125 | T4 tests | 12 | identical | tests/gateway/test_approve_deny_commands.py |
| C-1862 | `tests/gateway/test_background_process_notifications.py` | test-sleep | HX013×1 | 211 | T4 tests | 27 | diverged | tests/gateway/test_background_process_notifications.py |
| C-1863 | `tests/gateway/test_bounded_adapter_teardown.py` | test-sleep | HX013×1 | 39 | T4 tests | 9 | identical | tests/gateway/test_bounded_adapter_teardown.py |
| C-1864 | `tests/gateway/test_buzz_adapter.py` | test-sleep | HX013×1 | 1103 | T4 tests | 52 | diverged | tests/gateway/test_buzz_adapter.py |
| C-1865 | `tests/gateway/test_codex_hygiene_compaction.py` | test-sleep | HX013×1 | 270 | T4 tests | 5 | identical | tests/gateway/test_codex_hygiene_compaction.py |
| C-1866 | `tests/gateway/test_compression_concurrent_sessions.py` | test-sleep | HX013×1 | 59 | T4 tests | 7 | identical | tests/gateway/test_compression_concurrent_sessions.py |
| C-1867 | `tests/gateway/test_cron_active_work_drain.py` | test-sleep | HX013×1 | 64 | T4 tests | 14 | identical | tests/gateway/test_cron_active_work_drain.py |
| C-1868 | `tests/gateway/test_cron_fire_startup_gate.py` | test-sleep | HX013×1 | 177 | T4 tests | 9 | identical | tests/gateway/test_cron_fire_startup_gate.py |
| C-1869 | `tests/gateway/test_cron_fire_webhook.py` | test-sleep | HX013×1 | 289 | T4 tests | 12 | identical | tests/gateway/test_cron_fire_webhook.py |
| C-1870 | `tests/gateway/test_cron_shutdown_drain.py` | test-sleep | HX013×1 | 29 | T4 tests | 7 | identical | tests/gateway/test_cron_shutdown_drain.py |
| C-1871 | `tests/gateway/test_dingtalk.py` | test-sleep | HX013×1 | 494 | T4 tests | 13 | identical | tests/gateway/test_dingtalk.py |
| C-1872 | `tests/gateway/test_discord_event_silence.py` | test-sleep | HX013×1 | 82 | T4 tests | 5 | identical | tests/gateway/test_discord_event_silence.py |
| C-1873 | `tests/gateway/test_discord_race_polish.py` | test-sleep | HX013×1 | 70 | T4 tests | 6 | identical | tests/gateway/test_discord_race_polish.py |
| C-1874 | `tests/gateway/test_discord_send.py` | test-sleep | HX013×1 | 249 | T4 tests | 18 | identical | tests/gateway/test_discord_send.py |
| C-1875 | `tests/gateway/test_discord_voice_stale_binding.py` | test-sleep | HX013×1 | 67 | T4 tests | 6 | identical | tests/gateway/test_discord_voice_stale_binding.py |
| C-1876 | `tests/gateway/test_faulthandler_no_stderr.py` | test-sleep | HX013×1 | 42 | T4 tests | 3 | identical | tests/gateway/test_faulthandler_no_stderr.py |
| C-1877 | `tests/gateway/test_finalize_session_off_loop.py` | test-sleep | HX013×1 | 53 | T4 tests | 4 | identical | tests/gateway/test_finalize_session_off_loop.py |
| C-1878 | `tests/gateway/test_goal_continuation_drain.py` | test-sleep | HX013×1 | 198 | T4 tests | 5 | identical | tests/gateway/test_goal_continuation_drain.py |
| C-1879 | `tests/gateway/test_goal_max_turns_config.py` | test-sleep | HX013×1 | 103 | T4 tests | 12 | identical | tests/gateway/test_goal_max_turns_config.py |
| C-1880 | `tests/gateway/test_google_chat.py` | test-sleep | HX013×1 | 862 | T4 tests | 20 | identical | tests/gateway/test_google_chat.py |
| C-1881 | `tests/gateway/test_handoff_watcher_resilience.py` | test-sleep | HX013×1 | 147 | T4 tests | 4 | identical | tests/gateway/test_handoff_watcher_resilience.py |
| C-1882 | `tests/gateway/test_hosted_room_gateway_lifecycle.py` | test-sleep | HX013×1 | 227 | T4 tests | 2 | identical | tests/gateway/test_hosted_room_gateway_lifecycle.py |
| C-1883 | `tests/gateway/test_hygiene_deferred_work_drain.py` | test-sleep | HX013×1 | 91 | T4 tests | 3 | identical | tests/gateway/test_hygiene_deferred_work_drain.py |
| C-1884 | `tests/gateway/test_identity_canonical_ingress.py` | test-sleep | HX013×1 | 79 | T4 tests | 2 | identical | tests/gateway/test_identity_canonical_ingress.py |
| C-1885 | `tests/gateway/test_login_command.py` | test-sleep | HX013×1 | 294 | T4 tests | 3 | diverged | tests/gateway/test_login_command.py |
| C-1886 | `tests/gateway/test_matrix_mention.py` | test-sleep | HX013×1 | 281 | T4 tests | 16 | identical | tests/gateway/test_matrix_mention.py |
| C-1887 | `tests/gateway/test_memory_flush_on_reset.py` | test-sleep | HX013×1 | 75 | T4 tests | 1 | identical | tests/gateway/test_memory_flush_on_reset.py |
| C-1888 | `tests/gateway/test_msgraph_webhook.py` | test-sleep | HX013×1 | 137 | T4 tests | 7 | diverged | tests/gateway/test_msgraph_webhook.py |
| C-1889 | `tests/gateway/test_multiplex_adapter_registry.py` | test-sleep | HX013×1 | 287 | T4 tests | 39 | identical | tests/gateway/test_multiplex_adapter_registry.py |
| C-1890 | `tests/gateway/test_ntfy_plugin.py` | test-sleep | HX013×1 | 170 | T4 tests | 11 | identical | tests/gateway/test_ntfy_plugin.py |
| C-1891 | `tests/gateway/test_peer_dm_into_open_bot_chat.py` | test-sleep | HX013×1 | 337 | T4 tests | 5 | diverged | tests/gateway/test_peer_dm_into_open_bot_chat.py |
| C-1892 | `tests/gateway/test_pending_drain_no_recursion.py` | test-sleep | HX013×1 | 200 | T4 tests | 10 | identical | tests/gateway/test_pending_drain_no_recursion.py |
| C-1893 | `tests/gateway/test_qqbot.py` | test-sleep | HX013×1 | 420 | T4 tests | 19 | diverged | tests/gateway/test_qqbot.py |
| C-1894 | `tests/gateway/test_reaped_eviction_interrupts_run.py` | test-sleep | HX013×1 | 160 | T4 tests | 7 | identical | tests/gateway/test_reaped_eviction_interrupts_run.py |
| C-1895 | `tests/gateway/test_relay_seal_failure.py` | test-sleep | HX013×1 | 124 | T4 tests | 1 | identical | tests/gateway/test_relay_seal_failure.py |
| C-1896 | `tests/gateway/test_relay_teardown_drain.py` | test-sleep | HX013×1 | 43 | T4 tests | 1 | identical | tests/gateway/test_relay_teardown_drain.py |
| C-1897 | `tests/gateway/test_rich_sent_store_off_loop.py` | test-sleep | HX013×1 | 45 | T4 tests | 3 | identical | tests/gateway/test_rich_sent_store_off_loop.py |
| C-1898 | `tests/gateway/test_safe_adapter_disconnect.py` | test-sleep | HX013×1 | 36 | T4 tests | 8 | identical | tests/gateway/test_safe_adapter_disconnect.py |
| C-1899 | `tests/gateway/test_scale_to_zero.py` | test-sleep | HX013×1 | 138 | T4 tests | 18 | identical | tests/gateway/test_scale_to_zero.py |
| C-1900 | `tests/gateway/test_session_hygiene_turnhold_adoption.py` | test-sleep | HX013×1 | 427 | T4 tests | 2 | identical | tests/gateway/test_session_hygiene_turnhold_adoption.py |
| C-1901 | `tests/gateway/test_session_stall_watchdog.py` | test-sleep | HX013×1 | 269 | T4 tests | 10 | identical | tests/gateway/test_session_stall_watchdog.py |
| C-1902 | `tests/gateway/test_shared_adapter_utils.py` | test-sleep | HX013×1 | 22 | T4 tests | 2 | diverged | tests/gateway/test_shared_adapter_utils.py |
| C-1903 | `tests/gateway/test_shutdown_executor_quiesce.py` | test-sleep | HX013×1 | 136 | T4 tests | 11 | identical | tests/gateway/test_shutdown_executor_quiesce.py |
| C-1904 | `tests/gateway/test_simplex_plugin.py` | test-sleep | HX013×1 | 374 | T4 tests | 21 | identical | tests/gateway/test_simplex_plugin.py |
| C-1905 | `tests/gateway/test_slack.py` | test-sleep | HX013×1 | 712 | T4 tests | 93 | diverged | tests/gateway/test_slack.py |
| C-1906 | `tests/gateway/test_slack_unfurl_duplicate_turn.py` | test-sleep | HX013×1 | 230 | T4 tests | 2 | identical | tests/gateway/test_slack_unfurl_duplicate_turn.py |
| C-1907 | `tests/gateway/test_stale_finalize_suppression.py` | test-sleep | HX013×1 | 514 | T4 tests | 10 | identical | tests/gateway/test_stale_finalize_suppression.py |
| C-1908 | `tests/gateway/test_status.py` | test-sleep | HX013×1 | 658 | T4 tests | 58 | diverged | tests/gateway/test_status.py |
| C-1909 | `tests/gateway/test_suppression_contract_matrix.py` | test-sleep | HX013×1 | 149 | T4 tests | 3 | identical | tests/gateway/test_suppression_contract_matrix.py |
| C-1910 | `tests/gateway/test_systemd_notify.py` | test-sleep | HX013×1 | 73 | T4 tests | 8 | identical | tests/gateway/test_systemd_notify.py |
| C-1911 | `tests/gateway/test_teams.py` | test-sleep | HX013×1 | 819 | T4 tests | 24 | identical | tests/gateway/test_teams.py |
| C-1912 | `tests/gateway/test_telegram_closewait_windows_live.py` | test-sleep | HX013×1 | 108 | T4 tests | 4 | identical | tests/gateway/test_telegram_closewait_windows_live.py |
| C-1913 | `tests/gateway/test_telegram_documents.py` | test-sleep | HX013×1 | 297 | T4 tests | 14 | identical | tests/gateway/test_telegram_documents.py |
| C-1914 | `tests/gateway/test_telegram_long_command_batching.py` | test-sleep | HX013×1 | 105 | T4 tests | 1 | identical | tests/gateway/test_telegram_long_command_batching.py |
| C-1915 | `tests/gateway/test_telegram_network.py` | test-sleep | HX013×1 | 471 | T4 tests | 15 | identical | tests/gateway/test_telegram_network.py |
| C-1916 | `tests/gateway/test_telegram_start_polling_timeout.py` | test-sleep | HX013×1 | 25 | T4 tests | 12 | identical | tests/gateway/test_telegram_start_polling_timeout.py |
| C-1917 | `tests/gateway/test_text_batch_flush_invariants.py` | test-sleep | HX013×1 | 78 | T4 tests | 2 | identical | tests/gateway/test_text_batch_flush_invariants.py |
| C-1918 | `tests/gateway/test_thread_tracker_mark_off_loop.py` | test-sleep | HX013×1 | 98 | T4 tests | 7 | identical | tests/gateway/test_thread_tracker_mark_off_loop.py |
| C-1919 | `tests/gateway/test_tool_log_mode.py` | test-sleep | HX013×1 | 37 | T4 tests | 10 | identical | tests/gateway/test_tool_log_mode.py |
| C-1920 | `tests/gateway/test_typing_indicator_toggle.py` | test-sleep | HX013×1 | 75 | T4 tests | 8 | identical | tests/gateway/test_typing_indicator_toggle.py |
| C-1921 | `tests/gateway/test_unknown_command.py` | test-sleep | HX013×1 | 245 | T4 tests | 11 | identical | tests/gateway/test_unknown_command.py |
| C-1922 | `tests/gateway/test_webhook_deliver_only.py` | test-sleep | HX013×1 | 111 | T4 tests | 10 | identical | tests/gateway/test_webhook_deliver_only.py |
| C-1923 | `tests/gateway/test_webhook_integration.py` | test-sleep | HX013×1 | 133 | T4 tests | 11 | diverged | tests/gateway/test_webhook_integration.py |
| C-1924 | `tests/gateway/test_webhook_offloop_delivery.py` | test-sleep | HX013×1 | 49 | T4 tests | 4 | identical | tests/gateway/test_webhook_offloop_delivery.py |
| C-1925 | `tests/gateway/test_webhook_route_toolsets.py` | test-sleep | HX013×1 | 172 | T4 tests | 4 | diverged | tests/gateway/test_webhook_route_toolsets.py |
| C-1926 | `tests/gateway/test_webhook_session_close.py` | test-sleep | HX013×1 | 94 | T4 tests | 8 | diverged | tests/gateway/test_webhook_session_close.py |
| C-1927 | `tests/gateway/test_wecom_callback.py` | test-sleep | HX013×1 | 180 | T4 tests | 9 | identical | tests/gateway/test_wecom_callback.py |
| C-1928 | `tests/gateway/test_whatsapp_bridge_pidfile.py` | test-sleep | HX013×1 | 79 | T4 tests | 10 | identical | tests/gateway/test_whatsapp_bridge_pidfile.py |
| C-1929 | `tests/gateway/test_yuanbao_shutdown.py` | test-sleep | HX013×1 | 51 | T4 tests | 2 | diverged | tests/gateway/test_yuanbao_shutdown.py |
| C-1930 | `tests/hermes_cli/test_anon_sign_in_flow.py` | test-sleep | HX013×1 | 399 | T4 tests | 5 | identical | tests/hermes_cli/test_anon_sign_in_flow.py |
| C-1931 | `tests/hermes_cli/test_bundled_desktop_launch.py` | test-sleep | HX013×1 | 210 | T4 tests | 10 | identical | tests/hermes_cli/test_bundled_desktop_launch.py |
| C-1932 | `tests/hermes_cli/test_claw.py` | test-sleep | HX013×1 | 319 | T4 tests | 15 | identical | tests/hermes_cli/test_claw.py |
| C-1933 | `tests/hermes_cli/test_cli_approval_ui.py` | test-sleep | HX013×1 | 705 | T4 tests | 11 | diverged | tests/hermes_cli/test_cli_approval_ui.py |
| C-1934 | `tests/hermes_cli/test_cli_interrupt_ack_race.py` | test-sleep | HX013×1 | 96 | T4 tests | 1 | identical | tests/hermes_cli/test_cli_interrupt_ack_race.py |
| C-1935 | `tests/hermes_cli/test_cli_interrupt_subagent.py` | test-sleep | HX013×1 | 139 | T4 tests | 2 | identical | tests/hermes_cli/test_cli_interrupt_subagent.py |
| C-1936 | `tests/hermes_cli/test_codex_token_writethrough.py` | test-sleep | HX013×1 | 107 | T4 tests | 7 | identical | tests/hermes_cli/test_codex_token_writethrough.py |
| C-1937 | `tests/hermes_cli/test_config.py` | test-sleep | HX013×1 | 209 | T4 tests | 65 | diverged | tests/hermes_cli/test_config.py |
| C-1938 | `tests/hermes_cli/test_cron_fire_dashboard.py` | test-sleep | HX013×1 | 247 | T4 tests | 19 | identical | tests/hermes_cli/test_cron_fire_dashboard.py |
| C-1939 | `tests/hermes_cli/test_exit_watchdog_signal_arm.py` | test-sleep | HX013×1 | 115 | T4 tests | 3 | identical | tests/hermes_cli/test_exit_watchdog_signal_arm.py |
| C-1940 | `tests/hermes_cli/test_goals_db_bootstrap_off_loop.py` | test-sleep | HX013×1 | 133 | T4 tests | 7 | identical | tests/hermes_cli/test_goals_db_bootstrap_off_loop.py |
| C-1941 | `tests/hermes_cli/test_install_identity.py` | test-sleep | HX013×1 | 96 | T4 tests | 4 | identical | tests/hermes_cli/test_install_identity.py |
| C-1942 | `tests/hermes_cli/test_kanban_core_functionality.py` | test-sleep | HX013×1 | 651 | T4 tests | 18 | diverged | tests/hermes_cli/test_kanban_core_functionality.py |
| C-1943 | `tests/hermes_cli/test_kanban_terminal_worker_reaper.py` | test-sleep | HX013×1 | 45 | T4 tests | 3 | identical | tests/hermes_cli/test_kanban_terminal_worker_reaper.py |
| C-1944 | `tests/hermes_cli/test_linux_desktop_entry.py` | test-sleep | HX013×1 | 1336 | T4 tests | 26 | identical | tests/hermes_cli/test_linux_desktop_entry.py |
| C-1945 | `tests/hermes_cli/test_mcp_login_window.py` | test-sleep | HX013×1 | 43 | T4 tests | 1 | identical | tests/hermes_cli/test_mcp_login_window.py |
| C-1946 | `tests/hermes_cli/test_model_cache_parallel_prefetch.py` | test-sleep | HX013×1 | 150 | T4 tests | 5 | diverged | tests/hermes_cli/test_model_cache_parallel_prefetch.py |
| C-1947 | `tests/hermes_cli/test_model_info_probe_timeout.py` | test-sleep | HX013×1 | 31 | T4 tests | 1 | identical | tests/hermes_cli/test_model_info_probe_timeout.py |
| C-1948 | `tests/hermes_cli/test_oneshot_usage_file.py` | test-sleep | HX013×1 | 106 | T4 tests | 9 | identical | tests/hermes_cli/test_oneshot_usage_file.py |
| C-1949 | `tests/hermes_cli/test_plugin_ownership_ledger.py` | test-sleep | HX013×1 | 1317 | T4 tests | 5 | identical | tests/hermes_cli/test_plugin_ownership_ledger.py |
| C-1950 | `tests/hermes_cli/test_profiles_sidebar_cache.py` | test-sleep | HX013×1 | 92 | T4 tests | 7 | identical | tests/hermes_cli/test_profiles_sidebar_cache.py |
| C-1951 | `tests/hermes_cli/test_proxy.py` | test-sleep | HX013×1 | 86 | T4 tests | 17 | identical | tests/hermes_cli/test_proxy.py |
| C-1952 | `tests/hermes_cli/test_pty_keepalive_ws.py` | test-sleep | HX013×1 | 241 | T4 tests | 5 | identical | tests/hermes_cli/test_pty_keepalive_ws.py |
| C-1953 | `tests/hermes_cli/test_relay_shared_metrics.py` | test-sleep | HX013×1 | 1690 | T4 tests | 38 | identical | tests/hermes_cli/test_relay_shared_metrics.py |
| C-1954 | `tests/hermes_cli/test_shared_metrics_install_run.py` | test-sleep | HX013×1 | 310 | T4 tests | 8 | diverged | tests/hermes_cli/test_shared_metrics_install_run.py |
| C-1955 | `tests/hermes_cli/test_shared_metrics_install_v4.py` | test-sleep | HX013×1 | 171 | T4 tests | 5 | identical | tests/hermes_cli/test_shared_metrics_install_v4.py |
| C-1956 | `tests/hermes_cli/test_shared_metrics_send_wiring.py` | test-sleep | HX013×1 | 194 | T4 tests | 8 | identical | tests/hermes_cli/test_shared_metrics_send_wiring.py |
| C-1957 | `tests/hermes_cli/test_shared_metrics_sender.py` | test-sleep | HX013×1 | 1003 | T4 tests | 11 | identical | tests/hermes_cli/test_shared_metrics_sender.py |
| C-1958 | `tests/hermes_cli/test_taskkill_identity_windows_live.py` | test-sleep | HX013×1 | 102 | T4 tests | 4 | identical | tests/hermes_cli/test_taskkill_identity_windows_live.py |
| C-1959 | `tests/hermes_cli/test_tui_rapid_enter_paste.py` | test-sleep | HX013×1 | 48 | T4 tests | 1 | identical | tests/hermes_cli/test_tui_rapid_enter_paste.py |
| C-1960 | `tests/hermes_cli/test_update_live_output.py` | test-sleep | HX013×1 | 50 | T4 tests | 1 | identical | tests/hermes_cli/test_update_live_output.py |
| C-1961 | `tests/hermes_cli/test_update_lock_protocol.py` | test-sleep | HX013×1 | 95 | T4 tests | 6 | diverged | tests/hermes_cli/test_update_lock_protocol.py |
| C-1962 | `tests/hermes_cli/test_update_pause_unmapped_identity.py` | test-sleep | HX013×1 | 57 | T4 tests | 1 | identical | tests/hermes_cli/test_update_pause_unmapped_identity.py |
| C-1963 | `tests/hermes_cli/test_update_posix_pause.py` | test-sleep | HX013×1 | 214 | T4 tests | 6 | identical | tests/hermes_cli/test_update_posix_pause.py |
| C-1964 | `tests/hermes_cli/test_venv_holder_windows_live.py` | test-sleep | HX013×1 | 54 | T4 tests | 12 | identical | tests/hermes_cli/test_venv_holder_windows_live.py |
| C-1965 | `tests/hermes_cli/test_web_server_console_ws.py` | test-sleep | HX013×1 | 77 | T4 tests | 11 | identical | tests/hermes_cli/test_web_server_console_ws.py |
| C-1966 | `tests/hermes_cli/test_web_server_pty_reconnect.py` | test-sleep | HX013×1 | 195 | T4 tests | 12 | identical | tests/hermes_cli/test_web_server_pty_reconnect.py |
| C-1967 | `tests/hermes_cli/test_web_server_status_topology_cache.py` | test-sleep | HX013×1 | 27 | T4 tests | 2 | identical | tests/hermes_cli/test_web_server_status_topology_cache.py |
| C-1968 | `tests/hermes_platform/test_resolver_app.py` | test-sleep | HX013×1 | 226 | T4 tests | 6 | identical | tests/hermes_platform/test_resolver_app.py |
| C-1969 | `tests/hermes_state/test_auto_archived_lineage_reactivation.py` | test-sleep | HX013×1 | 55 | T4 tests | 1 | identical | tests/hermes_state/test_auto_archived_lineage_reactivation.py |
| C-1970 | `tests/hermes_state/test_compression_watermark_commit.py` | test-sleep | HX013×1 | 176 | T4 tests | 1 | identical | tests/hermes_state/test_compression_watermark_commit.py |
| C-1971 | `tests/hermes_state/test_hermes_state_compression_locks.py` | test-sleep | HX013×1 | 77 | T4 tests | 2 | identical | tests/hermes_state/test_hermes_state_compression_locks.py |
| C-1972 | `tests/hermes_state/test_lease_pid_namespace.py` | test-sleep | HX013×1 | 52 | T4 tests | 6 | identical | tests/hermes_state/test_lease_pid_namespace.py |
| C-1973 | `tests/hermes_state/test_session_read_state.py` | test-sleep | HX013×1 | 48 | T4 tests | 1 | identical | tests/hermes_state/test_session_read_state.py |
| C-1974 | `tests/hermes_state/test_state_db_repair_non_destructive.py` | test-sleep | HX013×1 | 329 | T4 tests | 4 | identical | tests/hermes_state/test_state_db_repair_non_destructive.py |
| C-1975 | `tests/plugins/platforms/photon/test_fatal_notify_self_cancel.py` | test-sleep | HX013×1 | 96 | T4 tests | 3 | identical | tests/plugins/platforms/photon/test_fatal_notify_self_cancel.py |
| C-1976 | `tests/plugins/platforms/photon/test_overflow_recovery.py` | test-sleep | HX013×1 | 431 | T4 tests | 16 | identical | tests/plugins/platforms/photon/test_overflow_recovery.py |
| C-1977 | `tests/plugins/platforms/photon/test_presence_watchdog.py` | test-sleep | HX013×1 | 58 | T4 tests | 6 | identical | tests/plugins/platforms/photon/test_presence_watchdog.py |
| C-1978 | `tests/plugins/test_a2a_phase23.py` | test-sleep | HX013×1 | 405 | T4 tests | 12 | diverged | tests/plugins/test_a2a_phase23.py |
| C-1979 | `tests/plugins/test_a2a_plugin.py` | test-sleep | HX013×1 | 1478 | T4 tests | 36 | diverged | tests/plugins/test_a2a_plugin.py |
| C-1980 | `tests/plugins/test_achievements_plugin.py` | test-sleep | HX013×1 | 83 | T4 tests | 11 | identical | tests/plugins/test_achievements_plugin.py |
| C-1981 | `tests/plugins/test_google_meet_plugin.py` | test-sleep | HX013×1 | 346 | T4 tests | 13 | identical | tests/plugins/test_google_meet_plugin.py |
| C-1982 | `tests/pm/test_downloader_integrity.py` | test-sleep | HX013×1 | 161 | T4 tests | 3 | identical | tests/pm/test_downloader_integrity.py |
| C-1983 | `tests/scripts/desktop_update/test_desktop_update_macos_no_browser.py` | test-sleep | HX013×1 | 72 | T4 tests | 2 | identical | tests/scripts/desktop_update/test_desktop_update_macos_no_browser.py |
| C-1984 | `tests/scripts/desktop_update/test_desktop_update_posix_marker.py` | test-sleep | HX013×1 | 281 | T4 tests | 10 | identical | tests/scripts/desktop_update/test_desktop_update_posix_marker.py |
| C-1985 | `tests/scripts/desktop_update/test_desktop_update_shim_progress.py` | test-sleep | HX013×1 | 79 | T4 tests | 1 | identical | tests/scripts/desktop_update/test_desktop_update_shim_progress.py |
| C-1986 | `tests/scripts/desktop_update/test_desktop_update_windows_marker_ops.py` | test-sleep | HX013×1 | 317 | T4 tests | 8 | identical | tests/scripts/desktop_update/test_desktop_update_windows_marker_ops.py |
| C-1987 | `tests/scripts/desktop_update/test_desktop_update_windows_progress.py` | test-sleep | HX013×1 | 124 | T4 tests | 2 | identical | tests/scripts/desktop_update/test_desktop_update_windows_progress.py |
| C-1988 | `tests/scripts/desktop_update/test_desktop_update_windows_ui_delivery.py` | test-sleep | HX013×1 | 71 | T4 tests | 2 | identical | tests/scripts/desktop_update/test_desktop_update_windows_ui_delivery.py |
| C-1989 | `tests/test_mcp_serve.py` | test-sleep | HX013×1 | 420 | T4 tests | 17 | diverged | tests/test_mcp_serve.py |
| C-1990 | `tests/tools/test_approval_outcome_parity.py` | test-sleep | HX013×1 | 41 | T4 tests | 5 | identical | tests/tools/test_approval_outcome_parity.py |
| C-1991 | `tests/tools/test_bot_desktop_runtime.py` | test-sleep | HX013×1 | 332 | T4 tests | 11 | identical | tests/tools/test_bot_desktop_runtime.py |
| C-1992 | `tests/tools/test_bot_desktop_thumbnail.py` | test-sleep | HX013×1 | 26 | T4 tests | 3 | identical | tests/tools/test_bot_desktop_thumbnail.py |
| C-1993 | `tests/tools/test_browser_process_tree.py` | test-sleep | HX013×1 | 180 | T4 tests | 5 | identical | tests/tools/test_browser_process_tree.py |
| C-1994 | `tests/tools/test_code_kernel_remote.py` | test-sleep | HX013×1 | 299 | T4 tests | 12 | identical | tests/tools/test_code_kernel_remote.py |
| C-1995 | `tests/tools/test_cronjob_run_immediate.py` | test-sleep | HX013×1 | 304 | T4 tests | 20 | identical | tests/tools/test_cronjob_run_immediate.py |
| C-1996 | `tests/tools/test_daemon_pool.py` | test-sleep | HX013×1 | 53 | T4 tests | 15 | identical | tests/tools/test_daemon_pool.py |
| C-1997 | `tests/tools/test_delegate.py` | test-sleep | HX013×1 | 1649 | T4 tests | 47 | diverged | tests/tools/test_delegate.py |
| C-1998 | `tests/tools/test_delegate_heartbeat_suppression.py` | test-sleep | HX013×1 | 147 | T4 tests | 2 | fork-only | tests/tools/test_delegate_heartbeat_suppression.py |
| C-1999 | `tests/tools/test_delegate_liveness_timeout.py` | test-sleep | HX013×1 | 53 | T4 tests | 4 | identical | tests/tools/test_delegate_liveness_timeout.py |
| C-2000 | `tests/tools/test_delegate_nested_deadline_orphaning.py` | test-sleep | HX013×1 | 173 | T4 tests | 5 | fork-only | tests/tools/test_delegate_nested_deadline_orphaning.py |
| C-2001 | `tests/tools/test_execute_code_lifecycle_guard_deadline.py` | test-sleep | HX013×1 | 23 | T4 tests | 3 | identical | tests/tools/test_execute_code_lifecycle_guard_deadline.py |
| C-2002 | `tests/tools/test_file_read_guards.py` | test-sleep | HX013×1 | 526 | T4 tests | 22 | identical | tests/tools/test_file_read_guards.py |
| C-2003 | `tests/tools/test_interrupt.py` | test-sleep | HX013×1 | 263 | T4 tests | 17 | identical | tests/tools/test_interrupt.py |
| C-2004 | `tests/tools/test_kill_verify_tree_death.py` | test-sleep | HX013×1 | 119 | T4 tests | 6 | identical | tests/tools/test_kill_verify_tree_death.py |
| C-2005 | `tests/tools/test_mcp_dashboard_oauth.py` | test-sleep | HX013×1 | 220 | T4 tests | 15 | identical | tests/tools/test_mcp_dashboard_oauth.py |
| C-2006 | `tests/tools/test_mcp_elicitation.py` | test-sleep | HX013×1 | 129 | T4 tests | 17 | identical | tests/tools/test_mcp_elicitation.py |
| C-2007 | `tests/tools/test_mcp_oauth.py` | test-sleep | HX013×1 | 1065 | T4 tests | 35 | identical | tests/tools/test_mcp_oauth.py |
| C-2008 | `tests/tools/test_mcp_oauth_cold_load_expiry.py` | test-sleep | HX013×1 | 128 | T4 tests | 11 | identical | tests/tools/test_mcp_oauth_cold_load_expiry.py |
| C-2009 | `tests/tools/test_mcp_protocol_negotiation.py` | test-sleep | HX013×1 | 93 | T4 tests | 4 | identical | tests/tools/test_mcp_protocol_negotiation.py |
| C-2010 | `tests/tools/test_mcp_schema_cache_ttl.py` | test-sleep | HX013×1 | 39 | T4 tests | 1 | identical | tests/tools/test_mcp_schema_cache_ttl.py |
| C-2011 | `tests/tools/test_mcp_stdio_init_timeout.py` | test-sleep | HX013×1 | 37 | T4 tests | 7 | identical | tests/tools/test_mcp_stdio_init_timeout.py |
| C-2012 | `tests/tools/test_process_registry.py` | test-sleep | HX013×1 | 586 | T4 tests | 78 | diverged | tests/tools/test_process_registry.py |
| C-2013 | `tests/tools/test_process_registry_completion_races.py` | test-sleep | HX013×1 | 77 | T4 tests | 2 | identical | tests/tools/test_process_registry_completion_races.py |
| C-2014 | `tests/tools/test_process_registry_windows_live.py` | test-sleep | HX013×1 | 98 | T4 tests | 2 | identical | tests/tools/test_process_registry_windows_live.py |
| C-2015 | `tests/tools/test_process_wait_clarity.py` | test-sleep | HX013×1 | 180 | T4 tests | 4 | diverged | tests/tools/test_process_wait_clarity.py |
| C-2016 | `tests/tools/test_send_message_missing_platforms.py` | test-sleep | HX013×1 | 170 | T4 tests | 8 | identical | tests/tools/test_send_message_missing_platforms.py |
| C-2017 | `tests/tools/test_skill_view_dedup.py` | test-sleep | HX013×1 | 56 | T4 tests | 10 | diverged | tests/tools/test_skill_view_dedup.py |
| C-2018 | `tests/tools/test_skills_hub.py` | test-sleep | HX013×1 | 1953 | T4 tests | 52 | identical | tests/tools/test_skills_hub.py |
| C-2019 | `tests/tools/test_skills_hub_clawhub.py` | test-sleep | HX013×1 | 700 | T4 tests | 18 | identical | tests/tools/test_skills_hub_clawhub.py |
| C-2020 | `tests/tools/test_stt_idle_unload.py` | test-sleep | HX013×1 | 134 | T4 tests | 3 | identical | tests/tools/test_stt_idle_unload.py |
| C-2021 | `tests/tools/test_terminal_pre_guard_deadline.py` | test-sleep | HX013×1 | 50 | T4 tests | 3 | identical | tests/tools/test_terminal_pre_guard_deadline.py |
| C-2022 | `tests/tools/test_terminal_yield_to_background.py` | test-sleep | HX013×1 | 59 | T4 tests | 2 | identical | tests/tools/test_terminal_yield_to_background.py |
| C-2023 | `tests/tools/test_threaded_process_handle.py` | test-sleep | HX013×1 | 49 | T4 tests | 9 | identical | tests/tools/test_threaded_process_handle.py |
| C-2024 | `tests/tools/test_timeout_transport_drain.py` | test-sleep | HX013×1 | 46 | T4 tests | 1 | identical | tests/tools/test_timeout_transport_drain.py |
| C-2025 | `tests/tools/test_transcription_tools.py` | test-sleep | HX013×1 | 1428 | T4 tests | 61 | diverged | tests/tools/test_transcription_tools.py |
| C-2026 | `tests/tools/test_web_providers_ddgs.py` | test-sleep | HX013×1 | 46 | T4 tests | 12 | identical | tests/tools/test_web_providers_ddgs.py |
| C-2027 | `tests/tools/test_web_result_cache.py` | test-sleep | HX013×1 | 133 | T4 tests | 7 | identical | tests/tools/test_web_result_cache.py |
| C-2028 | `tests/tui_gateway/test_compute_host_late_compress_ack.py` | test-sleep | HX013×1 | 105 | T4 tests | 2 | identical | tests/tui_gateway/test_compute_host_late_compress_ack.py |
| C-2029 | `tests/tui_gateway/test_compute_host_phase1.py` | test-sleep | HX013×1 | 211 | T4 tests | 17 | identical | tests/tui_gateway/test_compute_host_phase1.py |
| C-2030 | `tests/tui_gateway/test_compute_host_turn_protocol.py` | test-sleep | HX013×1 | 62 | T4 tests | 6 | identical | tests/tui_gateway/test_compute_host_turn_protocol.py |
| C-2031 | `tests/tui_gateway/test_cross_process_orphan_ownership.py` | test-sleep | HX013×1 | 382 | T4 tests | 10 | identical | tests/tui_gateway/test_cross_process_orphan_ownership.py |
| C-2032 | `tests/tui_gateway/test_hosted_room_member_activity_hook.py` | test-sleep | HX013×1 | 92 | T4 tests | 2 | identical | tests/tui_gateway/test_hosted_room_member_activity_hook.py |
| C-2033 | `tests/tui_gateway/test_multi_client_fanout.py` | test-sleep | HX013×1 | 316 | T4 tests | 9 | diverged | tests/tui_gateway/test_multi_client_fanout.py |
| C-2034 | `tests/tui_gateway/test_reaper_flush_profile_scope.py` | test-sleep | HX013×1 | 58 | T4 tests | 3 | identical | tests/tui_gateway/test_reaper_flush_profile_scope.py |
| C-2035 | `tests/tui_gateway/test_tui_entry_mcp_owner.py` | test-sleep | HX013×1 | 29 | T4 tests | 2 | identical | tests/tui_gateway/test_tui_entry_mcp_owner.py |
| C-2036 | `tests/tui_gateway/test_wait_for_mcp_discovery.py` | test-sleep | HX013×1 | 53 | T4 tests | 8 | identical | tests/tui_gateway/test_wait_for_mcp_discovery.py |
| C-2037 | `tests/verify/test_environment_and_runner.py` | test-sleep | HX013×1 | 254 | T4 tests | 8 | identical | tests/verify/test_environment_and_runner.py |
