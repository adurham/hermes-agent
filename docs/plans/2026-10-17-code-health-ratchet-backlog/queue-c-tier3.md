# Queue C — tier 3 (tooling) blocking rule-family units

Part of [the code-health ratchet backlog](../2026-10-17-code-health-ratchet-backlog.md) (measured on main@6bb43667, after slice 1). Read that document's *How to take a unit* and *Rule-family playbooks* first; IDs are stable references for the Progress log.

| ID | path | family | rules × count | lines on main | tier | churn90 | file vs upstream | tests to run (`scripts/run_tests.sh …`) |
|---|---|---|---|---|---|---|---|---|
| C-1289 | `evals/postmortem/live_ab/cache_concurrency_probe.py` | correctness | F821×5 | 103, 104 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1290 | `optional-skills/security/godmode/scripts/auto_jailbreak.py` | correctness | F821×5 | 486, 546, 556, 593, 626 | T3 tooling | 10 | identical | (none — mechanical only) |
| C-1291 | `evals/postmortem/review_probes/goal_repaste_probe.py` | correctness | F811×1 | 19 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1292 | `trajectory_compressor.py` | async | ASYNC230×1, ASYNC240×2 | 620, 658, 671 | T3 tooling | 21 | identical | tests/agent/test_env_loader_secret_sources.py tests/hermes_cli/test_arcee_provider.py tests/test_trajectory_compressor.py tests/test_trajectory_compressor_async.py +1 |
| C-1293 | `evals/delivery_flood_wire.py` | async | ASYNC240×2 | 144, 192 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1294 | `evals/heartbeat_idle_wire.py` | async | ASYNC240×1 | 55 | T3 tooling | 7 | identical | tests/gateway/test_heartbeat_acceptance.py tests/gateway/test_heartbeat_execution_ownership.py tests/gateway/test_kanban_wake_acceptance.py |
| C-1295 | `evals/slack_stream_wire_contract.py` | async | ASYNC230×1 | 111 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1296 | `scripts/termux/build_wheels.py` | timeout | HX006×9 | 151, 173, 178, 190, 198, 215, 295, 320, 372 | T3 tooling | 10 | identical | tests/scripts/test_pm_build_consumers.py tests/scripts/test_termux_build_wheels.py |
| C-1297 | `scripts/releases/entrypoint.py` | timeout | HX006×8 | 38, 78, 160, 190, 295, 576, 617, 623 | T3 tooling | 29 | identical | tests/scripts/test_release_entrypoint.py |
| C-1298 | `scripts/release.py` | timeout | HX006×7 | 36, 50, 93, 112, 435, 449, 602 | T3 tooling | 87 | diverged | tests/scripts/test_release_changelog.py tests/scripts/test_release_channel_build.py tests/scripts/test_release_entrypoint.py tests/scripts/test_release_revision_anchor.py |
| C-1299 | `scripts/bundles/release_artifacts.py` | timeout | HX006×5 | 94, 107, 108, 123, 131 | T3 tooling | 24 | identical | tests/ci/test_channel_build_publication.py tests/ci/test_protected_canary_publication.py tests/scripts/test_appinstaller.py tests/scripts/test_macos_channel_artifact_record.py +1 |
| C-1300 | `scripts/termux/python_linkage.py` | timeout | HX006×4 | 23, 31, 34, 44 | T3 tooling | 3 | identical | tests/scripts/test_termux_python_linkage.py |
| C-1301 | `evals/fanout_resource_bench.py` | timeout | HX006×3 | 107, 108, 160 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1302 | `optional-skills/creative/kanban-video-orchestrator/scripts/monitor.py` | timeout | HX006×3 | 37, 46, 70 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1303 | `scripts/check-unspecced-sdk-mocks.py` | timeout | HX006×3 | 203, 216, 247 | T3 tooling | 2 | fork-only | (none — mechanical only) |
| C-1304 | `scripts/ci/check_public_surface.py` | timeout | HX006×3 | 39, 48, 163 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1305 | `scripts/ci/live_comment.py` | timeout | HX006×3 | 155, 170, 334 | T3 tooling | 14 | identical | (none — mechanical only) |
| C-1306 | `evals/browser_use/orchestrate.py` | timeout | HX006×2 | 56, 62 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1307 | `evals/codebase_navigability/static_metrics.py` | timeout | HX006×2 | 135, 136 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1308 | `evals/postmortem/forensics/rework.py` | timeout | HX006×2 | 24, 44 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1309 | `optional-skills/creative/pixel-art/scripts/pixel_art_video.py` | timeout | HX006×2 | 297, 309 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1310 | `optional-skills/security/unbroker/scripts/crypto.py` | timeout | HX006×2 | 91, 96 | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1311 | `scripts/bundles/desktop.py` | timeout | HX006×2 | 20, 24 | T3 tooling | 24 | identical | tests/scripts/test_bundle_npm.py tests/scripts/test_bundle_payload.py tests/scripts/test_commit_bundle_entrypoints.py tests/scripts/test_desktop_preparation.py |
| C-1312 | `scripts/bundles/native.py` | timeout | HX006×2 | 129, 172 | T3 tooling | 30 | identical | tests/pm/test_chromium.py tests/pm/test_dmgbuild_package.py tests/pm/test_pm_core.py tests/scripts/test_bundle_native.py +2 |
| C-1313 | `scripts/check-windows-footguns.py` | timeout | HX006×2 | 830, 844 | T3 tooling | 17 | identical | (none — mechanical only) |
| C-1314 | `scripts/check_profile_scope_patterns.py` | timeout | HX006×2 | 81, 111 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1315 | `scripts/ci/check_lazy_deps_imports.py` | timeout | HX006×2 | 50, 62 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1316 | `scripts/ci/lockfile_diff.py` | timeout | HX006×2 | 115, 126 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1317 | `scripts/releases/store.py` | timeout | HX006×2 | 94, 152 | T3 tooling | 6 | identical | tests/scripts/test_release_sequencer.py tests/scripts/test_release_store.py |
| C-1318 | `scripts/render-builds-table.py` | timeout | HX006×2 | 727, 749 | T3 tooling | 22 | identical | (none — mechanical only) |
| C-1319 | `scripts/sync-fork-branding.py` | timeout | HX006×2 | 54, 308 | T3 tooling | 1 | fork-only | (none — mechanical only) |
| C-1320 | `scripts/termux/stage_apt_repo.py` | timeout | HX006×2 | 101, 387 | T3 tooling | 7 | identical | tests/scripts/test_termux_deb_version.py |
| C-1321 | `evals/browser_use/orchestrate_cloud.py` | timeout | HX006×1 | 140 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1322 | `evals/cli_fallback_add_picker_error.py` | timeout | HX006×1 | 40 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1323 | `evals/codebase_navigability/runtime_bench.py` | timeout | HX006×1 | 25 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1324 | `evals/desktop_bug_campaign/history_live.py` | timeout | HX006×1 | 71 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1325 | `evals/desktop_bug_campaign/hooks_live.py` | timeout | HX006×1 | 138 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1326 | `evals/desktop_bug_campaign/leases_live.py` | timeout | HX006×1 | 145 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1327 | `evals/desktop_bug_campaign/linux_launcher_probe.py` | timeout | HX006×1 | 49 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1328 | `evals/desktop_bug_campaign/persistence_live.py` | timeout | HX006×1 | 79 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1329 | `evals/gateway_completion/delivery_lifecycle.py` | timeout | HX006×1 | 142 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1330 | `evals/gemini_type_array_probe.py` | timeout | HX006×1 | 103 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1331 | `evals/kanban_graph_identity.py` | timeout | HX006×1 | 23 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1332 | `evals/kanban_scope_probe.py` | timeout | HX006×1 | 14 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1333 | `evals/key_cmd_picker_live.py` | timeout | HX006×1 | 165 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1334 | `evals/native_compaction/ab_checkpoint_preflight.py` | timeout | HX006×1 | 248 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1335 | `evals/postmortem/live_ab/cache_prefix_live.py` | timeout | HX006×1 | 43 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1336 | `evals/postmortem/live_ab/cache_prefix_wire.py` | timeout | HX006×1 | 60 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1337 | `evals/postmortem/review_probes/cache_estimator_probe.py` | timeout | HX006×1 | 22 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1338 | `evals/postmortem/review_probes/deadline_probe.py` | timeout | HX006×1 | 31 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1339 | `evals/postmortem/review_probes/goal_scope_probe.py` | timeout | HX006×1 | 20 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1340 | `evals/prompt_footprint/property_guidance.py` | timeout | HX006×1 | 36 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1341 | `evals/session_search_schema/runner.py` | timeout | HX006×1 | 62 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1342 | `evals/token_accounting/ab_image_cost_calibration.py` | timeout | HX006×1 | 159 | T3 tooling | 5 | identical | (none — mechanical only) |
| C-1343 | `evals/token_accounting/replay_gates.py` | timeout | HX006×1 | 279 | T3 tooling | 5 | identical | (none — mechanical only) |
| C-1344 | `evals/token_accounting/worktree_prompt_prefix.py` | timeout | HX006×1 | 14 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1345 | `evals/webhook_auth/standard_webhooks_ab.py` | timeout | HX006×1 | 31 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1346 | `optional-skills/creative/ai-presenter-video/scripts/preflight.py` | timeout | HX006×1 | 15 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1347 | `scripts/audit-old-updater-imports.py` | timeout | HX006×1 | 729 | T3 tooling | 10 | identical | (none — mechanical only) |
| C-1348 | `scripts/audit_pr_attribution.py` | timeout | HX006×1 | 49 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1349 | `scripts/bundles/desktop_prepare.py` | timeout | HX006×1 | 25 | T3 tooling | 9 | identical | tests/scripts/test_channel_build_versions.py tests/scripts/test_commit_bundle_entrypoints.py tests/scripts/test_desktop_preparation.py tests/scripts/test_native_build.py |
| C-1350 | `scripts/bundles/desktop_toolchain.py` | timeout | HX006×1 | 80 | T3 tooling | 4 | identical | tests/scripts/test_desktop_toolchain.py tests/scripts/test_native_build.py |
| C-1351 | `scripts/bundles/payload.py` | timeout | HX006×1 | 41 | T3 tooling | 9 | identical | tests/pm/test_runtime_wheelhouse.py tests/scripts/test_bundle_payload.py tests/scripts/test_payload_digests.py |
| C-1352 | `scripts/bundles/stage.py` | timeout | HX006×1 | 53 | T3 tooling | 3 | identical | tests/scripts/test_agent_build.py |
| C-1353 | `scripts/check-case-collisions.py` | timeout | HX006×1 | 69 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1354 | `scripts/check_bash_shebangs.py` | timeout | HX006×1 | 19 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1355 | `scripts/check_no_tmp_literals.py` | timeout | HX006×1 | 172 | T3 tooling | 5 | diverged | (none — mechanical only) |
| C-1356 | `scripts/ci/python_packages.py` | timeout | HX006×1 | 33 | T3 tooling | 1 | identical | tests/scripts/test_pm_build_consumers.py tests/scripts/test_release_artifacts.py |
| C-1357 | `scripts/ci/timings_report.py` | timeout | HX006×1 | 74 | T3 tooling | 14 | identical | (none — mechanical only) |
| C-1358 | `scripts/contributor_audit.py` | timeout | HX006×1 | 103 | T3 tooling | 16 | identical | (none — mechanical only) |
| C-1359 | `scripts/generate_conformance_vectors.py` | timeout | HX006×1 | 194 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1360 | `scripts/profile-tui.py` | timeout | HX006×1 | 572 | T3 tooling | 15 | identical | (none — mechanical only) |
| C-1361 | `scripts/releases/distance.py` | timeout | HX006×1 | 16 | T3 tooling | 2 | identical | tests/scripts/test_install_stamp_distance.py |
| C-1362 | `scripts/releases/docker.py` | timeout | HX006×1 | 97 | T3 tooling | 10 | identical | tests/scripts/test_release_docker_cli.py tests/scripts/test_release_sequencer.py tests/scripts/test_release_tags.py |
| C-1363 | `scripts/releases/pick_tags.py` | timeout | HX006×1 | 12 | T3 tooling | 1 | identical | tests/scripts/test_release_tag_sampling.py |
| C-1364 | `scripts/releases/sequencer.py` | timeout | HX006×1 | 97 | T3 tooling | 22 | identical | tests/scripts/test_release_sequencer.py |
| C-1365 | `scripts/releases/stable.py` | timeout | HX006×1 | 437 | T3 tooling | 35 | diverged | tests/ci/test_desktop_release_tag_admission.py tests/ci/test_protected_canary_publication.py tests/ci/test_stable_release_graph.py tests/scripts/test_stable_admission.py +1 |
| C-1366 | `scripts/releases/versioning.py` | timeout | HX006×1 | 97 | T3 tooling | 8 | identical | tests/ci/test_channel_build_publication.py tests/ci/test_desktop_release_commit_admission.py tests/hermes_cli/test_release_channels.py tests/scripts/test_release_canary_git.py +3 |
| C-1367 | `scripts/termux/build.py` | timeout | HX006×1 | 63 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1368 | `scripts/termux/build_environment.py` | timeout | HX006×1 | 47 | T3 tooling | 2 | identical | tests/scripts/test_pm_build_consumers.py |
| C-1369 | `skills/productivity/google-workspace/scripts/google_api.py` | timeout | HX006×1 | 108 | T3 tooling | 13 | identical | (none — mechanical only) |
| C-1370 | `skills/productivity/google-workspace/scripts/gws_bridge.py` | timeout | HX006×1 | 113 | T3 tooling | 11 | identical | (none — mechanical only) |
| C-1371 | `skills/productivity/pdf/scripts/_raster.py` | timeout | HX006×1 | 69 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1372 | `evals/desktop_bug_campaign/leases_live.py` | structure | HX011×1 | 29 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1373 | `evals/gateway_failure_ownership/probe.py` | structure | HX011×1 | 312 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1374 | `optional-skills/blockchain/hyperliquid/scripts/hyperliquid_client.py` | structure | HX011×1 | 827 | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1375 | `optional-skills/creative/comfyui/scripts/run_workflow.py` | structure | HX011×1 | 295 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1376 | `optional-skills/creative/comfyui/scripts/ws_monitor.py` | structure | HX011×1 | 194 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1377 | `optional-skills/finance/stocks/scripts/stocks_client.py` | structure | HX011×1 | 728 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1378 | `optional-skills/health/fitness-nutrition/scripts/body_calc.py` | structure | HX011×1 | 178 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1379 | `optional-skills/productivity/memento-flashcards/scripts/memento_cards.py` | structure | HX011×1 | 167 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1380 | `optional-skills/research/pinecone-research/scripts/memory_manager.py` | structure | HX011×1 | 138 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1381 | `optional-skills/security/oss-forensics/scripts/evidence-store.py` | structure | HX011×1 | 258 | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1382 | `optional-skills/web-development/har-derived-api-client/scripts/har_capture.py` | structure | HX011×1 | 25 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1383 | `optional-skills/web-development/har-derived-api-client/scripts/har_capture_cdp.py` | structure | HX011×1 | 33 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1384 | `scripts/generate_icons.py` | structure | HX011×1 | 804 | T3 tooling | 28 | identical | (none — mechanical only) |
| C-1385 | `scripts/releases/r2.py` | structure | HX011×1 | 1040 | T3 tooling | 19 | identical | tests/ci/test_channel_build_publication.py tests/ci/test_tag_builds_summary.py tests/ci/test_termux_input_archive.py tests/scripts/test_archive_inputs.py +6 |
| C-1386 | `scripts/render-builds-table.py` | structure | HX011×1 | 279 | T3 tooling | 22 | identical | (none — mechanical only) |
| C-1387 | `scripts/termux/stage_apt_repo.py` | structure | HX011×1 | 92 | T3 tooling | 7 | identical | tests/scripts/test_termux_deb_version.py |
| C-1388 | `skills/productivity/docx/scripts/docx_create.py` | structure | HX011×1 | 97 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1389 | `skills/productivity/docx/scripts/docx_edit.py` | structure | HX011×1 | 219 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1390 | `skills/productivity/maps/scripts/maps_client.py` | structure | HX011×1 | 822 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1391 | `skills/productivity/pdf/scripts/pdf_create.py` | structure | HX011×1 | 58 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1392 | `skills/productivity/pdf/scripts/pdf_make_form.py` | structure | HX011×1 | 89 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1393 | `skills/productivity/powerpoint/scripts/pptx_edit.py` | structure | HX011×1 | 166 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1394 | `optional-skills/blockchain/evm/scripts/evm_client.py` | swallow | BLE001×25, S110×9 | 346, 349, 374, 377, 526, 535, 552, 564, 592, 628, 699, 744 … | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1395 | `optional-skills/creative/comfyui/scripts/run_workflow.py` | swallow | BLE001×16, S110×3 | 121, 124, 154, 181, 202, 227, 285, 335, 352, 359, 372, 419 … | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1396 | `scripts/iso-certify.py` | swallow | BLE001×13, S110×4 | 198, 201, 242, 261, 271, 343, 366, 372, 377, 389, 400, 407 … | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1397 | `optional-skills/research/domain-intel/scripts/domain_intel.py` | swallow | BLE001×9, S110×1 | 172, 223, 230, 242, 257, 265, 281, 309, 350 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1398 | `optional-skills/creative/comfyui/scripts/check_deps.py` | swallow | BLE001×7, S110×2 | 133, 139, 156, 162, 173, 226, 423 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1399 | `skills/productivity/google-workspace/scripts/setup.py` | swallow | BLE001×9 | 77, 105, 119, 138, 166, 198, 264, 355, 409 | T3 tooling | 12 | identical | (none — mechanical only) |
| C-1400 | `batch_runner.py` | swallow | BLE001×8 | 214, 297, 530, 571, 692, 829, 984, 1012 | T3 tooling | 27 | identical | tests/test_agent_close_lifecycle.py tests/test_batch_runner_checkpoint.py tests/test_batch_runner_discard_resume.py tests/test_batch_runner_durability.py +1 |
| C-1401 | `evals/core_tool_deferral/worker.py` | swallow | BLE001×6, S110×2 | 269, 299, 309, 329, 344, 377 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1402 | `evals/codebase_navigability/static_metrics.py` | swallow | BLE001×6, S110×1 | 28, 37, 52, 58, 143, 147 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1403 | `evals/session_search_schema/runner.py` | swallow | BLE001×5, S110×2 | 123, 130, 171, 231, 265 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1404 | `scripts/discord-voice-doctor.py` | swallow | BLE001×6, S110×1 | 83, 156, 214, 261, 275, 364 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1405 | `trajectory_compressor.py` | swallow | BLE001×7 | 52, 65, 302, 352, 469, 488, 609 | T3 tooling | 21 | identical | tests/agent/test_env_loader_secret_sources.py tests/hermes_cli/test_arcee_provider.py tests/test_trajectory_compressor.py tests/test_trajectory_compressor_async.py +1 |
| C-1406 | `evals/fanout_resource_bench.py` | swallow | BLE001×4, S110×2 | 51, 209, 213, 225 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1407 | `optional-skills/creative/comfyui/scripts/health_check.py` | swallow | BLE001×5, S110×1 | 53, 57, 65, 71, 130 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1408 | `optional-skills/productivity/telephony/scripts/telephony.py` | swallow | BLE001×5, S110×1 | 94, 102, 157, 236, 271 | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1409 | `scripts/ci/live_comment.py` | swallow | BLE001×6 | 397, 410, 420, 459, 580, 709 | T3 tooling | 14 | identical | (none — mechanical only) |
| C-1410 | `scripts/generate_icons.py` | swallow | BLE001×6 | 884, 903, 910, 929, 942, 986 | T3 tooling | 28 | identical | (none — mechanical only) |
| C-1411 | `scripts/probe_active_session_exclusivity.py` | swallow | BLE001×4, S110×2 | 99, 104, 107, 119 | T3 tooling | 2 | diverged | (none — mechanical only) |
| C-1412 | `evals/postmortem/live_ab/cache_concurrency_probe.py` | swallow | BLE001×5 | 86, 96, 128, 153, 201 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1413 | `evals/tool_search/tool_search_livetest2.py` | swallow | BLE001×3, S110×2 | 124, 138, 149 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1414 | `evals/tool_search/tool_search_livetest_ue.py` | swallow | BLE001×3, S110×2 | 204, 220, 228 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1415 | `evals/tool_search/tool_search_livetest_ue_disc.py` | swallow | BLE001×3, S110×2 | 163, 177, 185 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1416 | `evals/tool_search/tool_search_livetest_ue_hard.py` | swallow | BLE001×3, S110×2 | 243, 258, 266 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1417 | `optional-skills/creative/comfyui/scripts/fetch_logs.py` | swallow | BLE001×4, S110×1 | 36, 43, 55, 68 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1418 | `optional-skills/finance/stocks/scripts/stocks_client.py` | swallow | BLE001×3, S110×2 | 157, 169, 744 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1419 | `scripts/build_skills_index.py` | swallow | BLE001×5 | 71, 93, 131, 255, 319 | T3 tooling | 15 | identical | tests/scripts/test_build_skills_index_health.py tests/scripts/test_build_skills_index_resolve.py |
| C-1420 | `scripts/run_tests_parallel.py` | swallow | BLE001×4, S110×1 | 1092, 1095, 1120, 1522 | T3 tooling | 49 | diverged | (none — mechanical only) |
| C-1421 | `evals/browser_use/orchestrate.py` | swallow | BLE001×2, S110×2 | 49, 74 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1422 | `evals/browser_use/orchestrate_cloud.py` | swallow | BLE001×3, S110×1 | 55, 127, 174 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1423 | `evals/compaction/scripts/codex_arm.py` | swallow | BLE001×4 | 77, 89, 103, 136 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1424 | `evals/postmortem/live_ab/cache_prefix_wire.py` | swallow | BLE001×2, S110×2 | 33, 40 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1425 | `optional-skills/creative/comfyui/scripts/ws_monitor.py` | swallow | BLE001×3, S110×1 | 88, 185, 262 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1426 | `optional-skills/finance/dcf-model/scripts/validate_dcf.py` | swallow | BLE001×4 | 156, 185, 230, 280 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1427 | `optional-skills/research/osint-investigation/scripts/fetch_wikipedia.py` | swallow | BLE001×4 | 74, 105, 153, 191 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1428 | `optional-skills/security/godmode/scripts/auto_jailbreak.py` | swallow | BLE001×4 | 342, 374, 397, 736 | T3 tooling | 10 | identical | (none — mechanical only) |
| C-1429 | `optional-skills/web-development/har-derived-api-client/scripts/har_capture_cdp.py` | swallow | BLE001×2, S110×2 | 58, 117 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1430 | `scripts/hermes_token_check.py` | swallow | BLE001×3, S110×1 | 75, 175, 205 | T3 tooling | 3 | fork-only | (none — mechanical only) |
| C-1431 | `skills/productivity/pdf/scripts/pdf_stamp.py` | swallow | BLE001×2, S110×2 | 51, 80 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1432 | `evals/browser_use/single_run.py` | swallow | BLE001×2, S110×1 | 139, 154 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1433 | `evals/codebase_navigability/bench.py` | swallow | BLE001×2, S110×1 | 43, 141 | T3 tooling | 2 | identical | tests/evals/test_codebase_navigability.py |
| C-1434 | `evals/compaction/runner.py` | swallow | BLE001×3 | 244, 387, 439 | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1435 | `evals/openrouter_pkce_ab/harness.py` | swallow | BLE001×3 | 104, 123, 145 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1436 | `mini_swe_runner.py` | swallow | BLE001×3 | 183, 251, 321 | T3 tooling | 14 | identical | tests/test_mini_swe_runner.py |
| C-1437 | `optional-skills/creative/comfyui/scripts/hardware_check.py` | swallow | BLE001×3 | 266, 277, 281 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1438 | `optional-skills/security/unbroker/scripts/emailer.py` | swallow | BLE001×2, S110×1 | 247, 313 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1439 | `skills/productivity/pdf/scripts/pdf_fill_form.py` | swallow | BLE001×2, S110×1 | 23, 85 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1440 | `skills/productivity/pdf/scripts/pdf_read.py` | swallow | BLE001×2, S110×1 | 16, 88 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1441 | `evals/api_delegation_sync_probe.py` | swallow | BLE001×2 | 46, 54 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1442 | `evals/browser_use/benchmark_browser_eval.py` | swallow | BLE001×2 | 52, 133 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1443 | `evals/browser_use/report.py` | swallow | BLE001×1, S110×1 | 22 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1444 | `evals/codebase_navigability/runtime_bench.py` | swallow | BLE001×2 | 64, 140 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1445 | `evals/compaction/scripts/reconstruct_lineage.py` | swallow | BLE001×1, S110×1 | 76 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1446 | `evals/postmortem/forensics/goal_loop.py` | swallow | BLE001×1, S110×1 | 47 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1447 | `evals/tool_search/tool_search_livetest.py` | swallow | BLE001×2 | 320, 416 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1448 | `evals/vault_fill_live_e2e.py` | swallow | BLE001×1, S110×1 | 138 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1449 | `optional-skills/creative/comfyui/scripts/_common.py` | swallow | BLE001×1, S110×1 | 518 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1450 | `optional-skills/mcp/mcp-oauth-remote-gateway/scripts/diagnose-oauth-mcp.py` | swallow | BLE001×2 | 38, 130 | T3 tooling | 5 | identical | (none — mechanical only) |
| C-1451 | `optional-skills/research/darwinian-evolver/scripts/parrot_openrouter.py` | swallow | BLE001×2 | 53, 117 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1452 | `optional-skills/security/unbroker/scripts/registry.py` | swallow | BLE001×2 | 227, 272 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1453 | `optional-skills/web-development/har-derived-api-client/scripts/har_capture.py` | swallow | BLE001×1, S110×1 | 62 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1454 | `scripts/sample_and_compress.py` | swallow | BLE001×2 | 56, 110 | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1455 | `skills/productivity/pdf/scripts/pdf_create.py` | swallow | BLE001×1, S110×1 | 30 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1456 | `skills/productivity/pdf/scripts/pdf_form_layout.py` | swallow | BLE001×1, S110×1 | 142 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1457 | `skills/productivity/pdf/scripts/pdf_make_form.py` | swallow | BLE001×1, S110×1 | 40 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1458 | `skills/productivity/pdf/scripts/pdf_merge.py` | swallow | BLE001×1, S110×1 | 15 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1459 | `skills/productivity/pdf/scripts/pdf_meta.py` | swallow | BLE001×1, S110×1 | 28 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1460 | `skills/productivity/pdf/scripts/pdf_page_image.py` | swallow | BLE001×1, S110×1 | 43 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1461 | `skills/productivity/pdf/scripts/pdf_secure.py` | swallow | BLE001×1, S110×1 | 18 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1462 | `skills/productivity/pdf/scripts/pdf_split.py` | swallow | BLE001×1, S110×1 | 33 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1463 | `skills/productivity/pdf/scripts/pdf_watermark.py` | swallow | BLE001×1, S110×1 | 14 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1464 | `evals/api_delegation_http_probe.py` | swallow | BLE001×1 | 71 | T3 tooling | 2 | identical | tests/gateway/test_api_delegation_delivery_contract.py |
| C-1465 | `evals/auxiliary_resource_exhausted.py` | swallow | BLE001×1 | 95 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1466 | `evals/codebase_navigability/lookup_sim.py` | swallow | BLE001×1 | 40 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1467 | `evals/compaction/jev_arm.py` | swallow | BLE001×1 | 142 | T3 tooling | 4 | diverged | tests/evals/test_compaction_jev_arm.py |
| C-1468 | `evals/core_tool_deferral/orchestrator.py` | swallow | BLE001×1 | 50 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1469 | `evals/dashboard_auth/refresh_singleflight_live_e2e.py` | swallow | BLE001×1 | 69 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1470 | `evals/desktop_bug_campaign/history_live.py` | swallow | BLE001×1 | 47 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1471 | `evals/desktop_bug_campaign/hooks_live.py` | swallow | BLE001×1 | 90 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1472 | `evals/desktop_bug_campaign/leases_live.py` | swallow | BLE001×1 | 170 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1473 | `evals/desktop_mcp_oauth/backend_http_fixture.py` | swallow | BLE001×1 | 333 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1474 | `evals/plugin_isolation/audit_catalog.py` | swallow | BLE001×1 | 53 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1475 | `evals/postmortem/live_ab/auth_stampede.py` | swallow | BLE001×1 | 74 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1476 | `evals/postmortem/review_probes/context_cap_probe.py` | swallow | BLE001×1 | 77 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1477 | `evals/postmortem/review_probes/credential_identity_probe.py` | swallow | BLE001×1 | 85 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1478 | `evals/postmortem/review_probes/goal_repaste_probe.py` | swallow | BLE001×1 | 126 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1479 | `evals/provider_fallback/probe_104260.py` | swallow | BLE001×1 | 166 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1480 | `evals/provider_fallback/probe_104360.py` | swallow | BLE001×1 | 122 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1481 | `evals/provider_wire/issue_104359.py` | swallow | BLE001×1 | 159 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1482 | `evals/providers/reasoning_shapes.py` | swallow | BLE001×1 | 138 | T3 tooling | 2 | identical | tests/agent/test_reasoning_shape_boundaries.py |
| C-1483 | `evals/readtool/runner.py` | swallow | BLE001×1 | 121 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1484 | `evals/session_search_schema/report.py` | swallow | BLE001×1 | 26 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1485 | `evals/subagent_process_handoff/stress_handoff_live.py` | swallow | BLE001×1 | 190 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1486 | `evals/toolperf_abeval/ab_eval.py` | swallow | BLE001×1 | 275 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1487 | `optional-skills/blockchain/solana/scripts/solana_client.py` | swallow | BLE001×1 | 88 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1488 | `optional-skills/creative/comfyui/scripts/run_batch.py` | swallow | BLE001×1 | 202 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1489 | `optional-skills/creative/meme-generation/scripts/generate_meme.py` | swallow | BLE001×1 | 98 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1490 | `optional-skills/health/fitness-nutrition/scripts/nutrition_search.py` | swallow | BLE001×1 | 35 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1491 | `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` | swallow | BLE001×1 | 26 | T3 tooling | 17 | identical | (none — mechanical only) |
| C-1492 | `optional-skills/productivity/memento-flashcards/scripts/youtube_quiz.py` | swallow | BLE001×1 | 45 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1493 | `optional-skills/research/darwinian-evolver/templates/custom_problem_template.py` | swallow | BLE001×1 | 61 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1494 | `optional-skills/research/drug-discovery/scripts/chembl_target.py` | swallow | BLE001×1 | 17 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1495 | `optional-skills/research/drug-discovery/scripts/ro5_screen.py` | swallow | BLE001×1 | 18 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1496 | `optional-skills/research/osint-investigation/scripts/_http.py` | swallow | BLE001×1 | 57 | T3 tooling | 8 | diverged | (none — mechanical only) |
| C-1497 | `optional-skills/research/osint-investigation/scripts/fetch_courtlistener.py` | swallow | BLE001×1 | 74 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1498 | `optional-skills/research/osint-investigation/scripts/fetch_gdelt.py` | swallow | BLE001×1 | 84 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1499 | `optional-skills/research/osint-investigation/scripts/fetch_nyc_acris.py` | swallow | BLE001×1 | 118 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1500 | `optional-skills/research/osint-investigation/scripts/fetch_opencorporates.py` | swallow | BLE001×1 | 100 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1501 | `optional-skills/research/osint-investigation/scripts/fetch_senate_ld.py` | swallow | BLE001×1 | 60 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1502 | `optional-skills/research/osint-investigation/scripts/fetch_usaspending.py` | swallow | BLE001×1 | 101 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1503 | `optional-skills/research/osint-investigation/scripts/fetch_wayback.py` | swallow | BLE001×1 | 65 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1504 | `optional-skills/security/godmode/scripts/godmode_race.py` | swallow | BLE001×1 | 296 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1505 | `optional-skills/security/godmode/scripts/parseltongue.py` | swallow | BLE001×1 | 289 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1506 | `optional-skills/security/unbroker/scripts/pdd.py` | swallow | BLE001×1 | 378 | T3 tooling | 7 | identical | (none — mechanical only) |
| C-1507 | `optional-skills/software-development/ast-grep/scripts/ast_grep_helper.py` | swallow | BLE001×1 | 186 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1508 | `scripts/add_contributor.py` | swallow | BLE001×1 | 54 | T3 tooling | 9 | identical | (none — mechanical only) |
| C-1509 | `scripts/audit-old-updater-imports.py` | swallow | BLE001×1 | 397 | T3 tooling | 10 | identical | (none — mechanical only) |
| C-1510 | `scripts/bundles/dmgbuild_diagnostics.py` | swallow | BLE001×1 | 82 | T3 tooling | 2 | identical | tests/scripts/test_dmgbuild_diagnostics.py |
| C-1511 | `scripts/check_subprocess_stdin.py` | swallow | BLE001×1 | 238 | T3 tooling | 17 | identical | (none — mechanical only) |
| C-1512 | `scripts/desktop-update/serve-ui.py` | swallow | BLE001×1 | 48 | T3 tooling | 4 | identical | (none — mechanical only) |
| C-1513 | `scripts/docker_config_migrate.py` | swallow | BLE001×1 | 107 | T3 tooling | 12 | identical | (none — mechanical only) |
| C-1514 | `scripts/docker_rebootstrap_nous_session.py` | swallow | BLE001×1 | 221 | T3 tooling | 11 | identical | (none — mechanical only) |
| C-1515 | `scripts/generate_conformance_vectors.py` | swallow | BLE001×1 | 202 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1516 | `scripts/releases/r2.py` | swallow | BLE001×1 | 1096 | T3 tooling | 19 | identical | tests/ci/test_channel_build_publication.py tests/ci/test_tag_builds_summary.py tests/ci/test_termux_input_archive.py tests/scripts/test_archive_inputs.py +6 |
| C-1517 | `scripts/releases/r2_multipart.py` | swallow | BLE001×1 | 92 | T3 tooling | 1 | identical | (none — mechanical only) |
| C-1518 | `scripts/termux/stage_runtime_libs.py` | swallow | BLE001×1 | 225 | T3 tooling | 12 | identical | tests/pm/test_artifact_mirror.py tests/scripts/test_archive_inputs.py |
| C-1519 | `skills/media/youtube-content/scripts/fetch_transcript.py` | swallow | BLE001×1 | 92 | T3 tooling | 8 | identical | (none — mechanical only) |
| C-1520 | `skills/productivity/docx/scripts/docx_validate.py` | swallow | BLE001×1 | 138 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1521 | `skills/productivity/google-workspace/scripts/google_api.py` | swallow | BLE001×1 | 74 | T3 tooling | 13 | identical | (none — mechanical only) |
| C-1522 | `skills/productivity/maps/scripts/maps_client.py` | swallow | BLE001×1 | 288 | T3 tooling | 6 | identical | (none — mechanical only) |
| C-1523 | `skills/productivity/xlsx/scripts/csv_to_xlsx.py` | swallow | BLE001×1 | 102 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1524 | `skills/productivity/xlsx/scripts/xlsx_create.py` | swallow | BLE001×1 | 257 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1525 | `skills/productivity/xlsx/scripts/xlsx_edit.py` | swallow | BLE001×1 | 261 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1526 | `skills/productivity/xlsx/scripts/xlsx_read.py` | swallow | BLE001×1 | 158 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1527 | `skills/productivity/xlsx/scripts/xlsx_recalc.py` | swallow | BLE001×1 | 109 | T3 tooling | 3 | identical | (none — mechanical only) |
| C-1528 | `skills/productivity/xlsx/scripts/xlsx_restructure.py` | swallow | BLE001×1 | 335 | T3 tooling | 2 | identical | (none — mechanical only) |
| C-1529 | `skills/productivity/xlsx/scripts/xlsx_to_csv.py` | swallow | BLE001×1 | 67 | T3 tooling | 2 | identical | (none — mechanical only) |
