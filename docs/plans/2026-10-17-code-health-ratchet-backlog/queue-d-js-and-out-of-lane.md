# Queue D — TypeScript units and out-of-lane Python (not for this series)

Part of [the code-health ratchet backlog](../2026-10-17-code-health-ratchet-backlog.md) (measured on main@6bb43667, after slice 1). Read that document's *How to take a unit* and *Rule-family playbooks* first; IDs are stable references for the Progress log.

| path::unit | metrics (value / target) |
|---|---|
| `apps/desktop/src/app/session/hooks/use-session-actions/index.ts::useSessionActions.resumeSession` | CC 216/20, FUNC_LINES 1223/300, NESTING 7/6 |
| `ui-tui/packages/hermes-ink/src/native-ts/yoga-layout/index.ts::layoutNode` | CC 216/20, FUNC_LINES 740/300 |
| `ui-tui/packages/hermes-ink/src/ink/render-node-to-output.ts::renderNodeToOutput` | CC 199/20, FUNC_LINES 1079/300, NESTING 9/6 |
| `ui-tui/src/app/createGatewayEventHandler.ts::createGatewayEventHandler.<anon>#2` | CC 167/20, FUNC_LINES 719/300 |
| `ui-tui/src/app/useInputHandlers.ts::useInputHandlers.<anon>#2` | CC 132/20, FUNC_LINES 363/300 |
| `ui-tui/src/components/textInput.tsx::TextInput.<anon>#7` | CC 109/20, FUNC_LINES 337/300 |
| `apps/desktop/src/app/chat/sidebar/index.tsx::ChatSidebar` | CC 107/20, FUNC_LINES 883/300 |
| `apps/desktop/src/app/starmap/render.ts::drawScene` | CC 106/20, FUNC_LINES 434/300 |
| `apps/desktop/src/app/session/hooks/use-prompt-actions/submit.ts::useSubmitPrompt.<anon>` | CC 105/20, FUNC_LINES 660/300 |
| `apps/desktop/src/components/assistant-ui/tool/fallback.tsx::ToolEntry` | CC 103/20, FUNC_LINES 350/300 |
| `ui-tui/src/components/appChrome.tsx::StatusRule` | CC 101/20, FUNC_LINES 346/300 |
| `web/src/pages/SystemPage.tsx::SystemPage` | CC 90/20, FUNC_LINES 939/300 |
| `apps/desktop/src/app/chat/composer/index.tsx::ChatBar.handleEditorKeyDown` | CC 87/20, FUNC_LINES 378/300 |
| `apps/desktop/src/plugins/hermes-bots/group-chat.ts::mergeRemoteGroupChatSnapshotIntoRooms` | CC 83/20 |
| `ui-tui/packages/hermes-ink/src/ink/ink.tsx::Ink.onRender` | CC 80/20, FUNC_LINES 538/300 |
| `apps/desktop/src/app/settings/model-settings.tsx::ModelSettings` | CC 79/20, FUNC_LINES 506/300 |
| `apps/desktop/src/lib/chat-messages/hydration.ts::toChatMessages.<anon>` | CC 79/20 |
| `apps/desktop/src/sdk/index.ts::host.openSession` | CC 75/20, FUNC_LINES 314/300 |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts::appendLiveSessionProjection` | CC 74/20 |
| `ui-tui/src/components/modelPicker.tsx::ModelPicker.<anon>#4` | CC 73/20 |
| `web/src/pages/SessionsPage.tsx::SessionsPage` | CC 73/20, FUNC_LINES 742/300 |
| `ui-tui/src/components/thinking.tsx::ToolTrail` | CC 71/20, FUNC_LINES 377/300 |
| `apps/desktop/src/app/chat/composer/index.tsx::ChatBar` | CC 70/20, FUNC_LINES 786/300 |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts::preserveLocalPendingTurnMessages` | CC 68/20 |
| `apps/desktop/src/components/pane-shell/tree/renderer/tree-group.tsx::TreeGroup` | CC 68/20, FUNC_LINES 412/300 |
| `ui-tui/src/components/markdown.tsx::MdImpl.nodes` | CC 68/20, FUNC_LINES 400/300 |
| `ui-tui/src/hooks/useVirtualHistory.ts::useVirtualHistory` | CC 64/20, FUNC_LINES 334/300 |
| `apps/desktop/src/app/chat/right-rail/preview-pane.tsx::PreviewPane` | CC 63/20, FUNC_LINES 312/300 |
| `ui-tui/src/theme.ts::fromSkin` | CC 63/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/index.ts::useMessageStream.completeAssistantMessage.completedState` | CC 62/20 |
| `tests-js/scripts/mock-server.ts::startMockServer.<anon>.server.<anon>#2` | CC 62/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/session-info.ts::handleSessionInfoEvent` | CC 61/20 |
| `ui-tui/src/components/agentsOverlay.tsx::AgentsOverlay.<anon>#9` | CC 60/20 |
| `apps/desktop/src/app/chat/right-rail/preview-file.tsx::LocalFilePreview` | CC 59/20 |
| `apps/desktop/src/lib/preview-act/act-in-page.ts::actInPageCore` | CC 59/20 |
| `ui-tui/packages/hermes-ink/src/ink/output.ts::Output.get` | CC 59/20 |
| `apps/desktop/src/plugins/hermes-bots/group-test-utils.ts::createGroupGateway.handle` | CC 56/20 |
| `ui-tui/packages/hermes-ink/src/ink/stringWidth.ts::isZeroWidth` | CC 55/20 |
| `apps/desktop/src/plugins/hermes-bots/data.ts::mergeMultiSourceRoster` | CC 54/20 |
| `apps/desktop/src/plugins/hermes-bots/group-chat.ts::mergeGroupChatSyncSnapshots` | CC 54/20 |
| `ui-tui/packages/hermes-ink/src/ink/parse-keypress.ts::parseKeypress` | CC 54/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/message-stream.ts::handleMessageStreamEvent` | CC 53/20 |
| `apps/desktop/electron/connection-registry.ts::normalizeRegistry` | CC 52/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/status.ts::handleStatusEvent` | CC 52/20 |
| `apps/desktop/src/app/settings/gateway-settings.tsx::GatewayConnectionSettings` | CC 52/20, FUNC_LINES 512/300 |
| `apps/desktop/src/plugins/hermes-bots/create-dialog.tsx::CreateAgentDialog` | CC 52/20, FUNC_LINES 379/300 |
| `web/src/pages/PluginsPage.tsx::PluginsPage` | CC 52/20, FUNC_LINES 359/300 |
| `ui-tui/packages/hermes-ink/src/ink/termio/sgr.ts::applySGR` | CC 51/20 |
| `ui-tui/packages/hermes-ink/src/ink/termio/tokenize.ts::tokenize` | CC 51/20 |
| `apps/desktop/src/app/contrib/wiring.tsx::ContribWiring` | CC 40/20, FUNC_LINES 761/300 |
| `ui-tui/packages/hermes-ink/src/ink/termio/parser.ts::parseCSI` | CC 49/20 |
| `ui-tui/packages/hermes-ink/src/ink/colorize.ts::colorize` | CC 48/20 |
| `apps/desktop/src/app/chat/sidebar/session-row.tsx::SidebarSessionRowImpl` | CC 47/20, FUNC_LINES 318/300 |
| `apps/desktop/src/app/settings/appearance-settings.tsx::AppearanceSettings` | CC 47/20, FUNC_LINES 565/300 |
| `apps/desktop/src/app/chat/composer/status-stack/session-control-goal.tsx::SessionControlGoalSection` | CC 45/20, FUNC_LINES 339/300 |
| `apps/desktop/src/lib/preview-act/watch-in-page.ts::watchInPage` | CC 45/20, FUNC_LINES 362/300 |
| `apps/desktop/src/plugins/kanban/drawer.tsx::TaskDrawer` | CC 45/20 |
| `ui-tui/src/components/journey.tsx::Journey.<anon>#3` | CC 45/20 |
| `web/src/pages/ChannelsPage.tsx::WhatsAppOnboardingPanel` | CC 45/20 |
| `apps/desktop/electron/main.ts::coerceDesktopConnectionConfig` | CC 44/20 |
| `apps/desktop/src/app/shell/model-catalog-menu.tsx::ModelFamilyRow` | CC 44/20 |
| `apps/desktop/src/components/desktop-install-overlay.tsx::DesktopInstallOverlay` | CC 44/20, FUNC_LINES 331/300 |
| `apps/desktop/src/components/onboarding/index.tsx::DesktopOnboardingOverlay` | CC 44/20 |
| `ui-tui/packages/hermes-ink/src/ink/render-border.ts::renderBorder` | CC 44/20 |
| `apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts::useDesktopIntegrations.<anon>#3` | CC 43/20 |
| `apps/desktop/src/app/settings/connections-registry.tsx::ConnectionsRegistrySection` | CC 42/20, FUNC_LINES 307/300 |
| `apps/desktop/src/components/assistant-ui/tool/fallback-model/index.ts::toolSubtitle` | CC 42/20 |
| `apps/desktop/src/plugins/hermes-bots/bot-row.tsx::BotRow` | CC 42/20 |
| `apps/desktop/src/store/quick-entry.ts::quickComposerReducer` | CC 42/20 |
| `apps/desktop/src/app/chat/sidebar/sessions-section.tsx::SidebarSessionsSection` | CC 41/20, FUNC_LINES 312/300 |
| `apps/desktop/src/components/assistant-ui/tool/fallback-model/index.ts::buildToolView` | CC 41/20 |
| `apps/desktop/src/lib/mcp-dashboard-oauth.ts::completeMcpDesktopOAuth` | CC 41/20 |
| `tests/e2e/core/terminal/_vt.py::Screen._csi` | CC 41/20 |
| `apps/desktop/src/app/session/hooks/use-session-actions/index.ts::useSessionActions.openNewSessionTile` | CC 40/20 |
| `ui-tui/packages/hermes-ink/src/ink/components/Text.tsx::Text` | CC 39/20 |
| `ui-tui/src/components/agentsOverlay.tsx::AgentsOverlay` | CC 39/20 |
| `apps/desktop/electron/oauth-partition.ts::resolveOauthPartition` | CC 38/20 |
| `apps/desktop/src/app/capabilities/plugins/plugins-tab.tsx::PackageRow` | CC 38/20 |
| `apps/desktop/src/app/chat/composer/status-stack/coding-row.tsx::CodingStatusRow` | CC 38/20 |
| `apps/desktop/src/app/session/hooks/use-prompt-actions/utils.ts::renderRpcResult` | CC 38/20 |
| `apps/desktop/src/app/shell/model-catalog-menu.tsx::ModelCatalogMenu` | CC 38/20 |
| `apps/desktop/src/plugins/hermes-bots/screen-pane.tsx::BotScreenPane` | CC 38/20 |
| `ui-tui/src/components/branding.tsx::SessionPanel` | CC 38/20 |
| `ui-tui/src/components/modelPicker.tsx::ModelPicker` | CC 38/20 |
| `apps/desktop/electron/main.ts::interceptSessionRequestForRemote` | CC 37/20 |
| `apps/desktop/src/app/chat/sidebar/projects/overview-row.tsx::ProjectOverviewRow` | CC 37/20 |
| `apps/desktop/src/app/session/hooks/use-route-resume.ts::useRouteResume.<anon>` | CC 37/20 |
| `apps/desktop/src/app/settings/config-field.tsx::ConfigField` | CC 37/20 |
| `apps/desktop/src/plugins/kanban/drawer.tsx::eventText` | CC 37/20 |
| `ui-tui/packages/hermes-ink/src/ink/styles.ts::applyFlexStyles` | CC 37/20 |
| `ui-tui/src/app/turnController.ts::TurnController.upsertSubagent.<anon>` | CC 37/20 |
| `web/src/pages/ProfilesPage.tsx::ProfilesPage` | CC 37/20, FUNC_LINES 505/300 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/tools.ts::handleToolEvent` | CC 36/20 |
| `apps/desktop/src/plugins/hermes-bots/group-chat-view.tsx::GroupChatWorkspace` | CC 36/20, FUNC_LINES 354/300 |
| `apps/desktop/src/store/profile-dot-state.ts::$profileDotStateByScope` | CC 36/20 |
| `ui-tui/src/components/messageLine.tsx::MessageLine` | CC 36/20 |
| `apps/desktop/src/app/chat/sidebar/projects/workspace-groups.ts::mergeRepoWorktreeGroups` | CC 35/20 |
| `apps/desktop/src/app/cron/index.tsx::scheduleOptionForExpr` | CC 35/20 |
| `apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts::useGatewayBoot.<anon>.attemptReconnect` | CC 35/20 |
| `apps/desktop/src/components/remote-setup/fields.tsx::RemoteSetupFields` | CC 35/20 |
| `apps/desktop/src/lib/voice-barge-in.ts::monitorSpeechDuringPlayback.<anon>.tick` | CC 35/20 |
| `ui-tui/packages/hermes-ink/src/ink/components/App.tsx::handleMouseEvent` | CC 35/20 |
| `ui-tui/packages/hermes-ink/src/ink/output.ts::writeLineToScreen` | CC 35/20, NESTING 7/6 |
| `web/src/pages/ChatPage.tsx::ChatPage` | CC 32/20, FUNC_LINES 513/300 |
| `apps/desktop/electron/main.ts::sanitizeDesktopConnectionConfig` | CC 34/20 |
| `apps/desktop/electron/remote-lifecycle.ts::connect` | CC 34/20 |
| `apps/desktop/src/plugins/hermes-bots/group-chat.ts::flushGroupChatServerSync` | CC 34/20 |
| `apps/desktop/src/store/connections.ts::selectConnection` | CC 34/20 |
| `ui-tui/packages/hermes-ink/src/ink/components/Box.tsx::Box` | CC 34/20 |
| `ui-tui/packages/hermes-ink/src/native-ts/yoga-layout/index.ts::resolveFlexibleLengths` | CC 34/20 |
| `ui-tui/src/components/activeSessionSwitcher.tsx::ActiveSessionSwitcher.<anon>#6` | CC 34/20 |
| `ui-tui/src/components/thinking.tsx::SubagentAccordion` | CC 34/20 |
| `web/src/lib/schedule.ts::parseSimpleCronExpression` | CC 34/20 |
| `apps/desktop/src/app/chat/composer/hooks/use-composer-trigger.ts::useComposerTrigger.replaceTriggerWithChip` | CC 33/20 |
| `apps/desktop/src/app/chat/composer/status-stack/index.tsx::ComposerStatusStack` | CC 33/20 |
| `apps/desktop/src/app/chat/index.tsx::ChatViewContent` | CC 33/20, FUNC_LINES 351/300 |
| `apps/desktop/src/app/settings/local-models-model-rows.tsx::CatalogModelRow` | CC 33/20 |
| `apps/desktop/src/app/settings/toolset-config-panel.tsx::ToolsetConfigPanel.<anon>#5` | CC 33/20 |
| `apps/desktop/src/lib/version-status.ts::resolveVersionStatus` | CC 33/20 |
| `apps/desktop/src/plugins/hermes-bots/plugin.tsx::register.<anon>#9` | CC 33/20 |
| `ui-tui/packages/hermes-ink/src/ink/log-update.ts::LogUpdate.render` | CC 33/20 |
| `ui-tui/packages/hermes-ink/src/ink/selection.ts::findPlainTextUrlAt` | CC 33/20 |
| `ui-tui/src/components/subscriptionOverlay.tsx::ConfirmScreen` | CC 33/20 |
| `ui-tui/src/hooks/useVirtualHistory.ts::useVirtualHistory.<anon>#5` | CC 33/20 |
| `web/src/pages/ProfileBuilderPage.tsx::ProfileBuilderPage` | CC 33/20, FUNC_LINES 403/300 |
| `web/src/pages/ProfilesPage.tsx::ProfilesPage.L` | CC 33/20 |
| `apps/desktop/src/app/chat/sidebar/profile-switcher.tsx::ProfileRail` | CC 32/20 |
| `apps/desktop/src/app/hooks/use-keybinds.ts::useKeybinds.<anon>#9.onKeyDown` | CC 32/20 |
| `apps/desktop/src/plugins/hermes-bots/data.ts::migrateBotMeta` | CC 32/20 |
| `apps/desktop/src/plugins/hermes-bots/group-rounds.ts::runGroupChatRounds` | CC 32/20 |
| `web/src/App.tsx::App` | CC 32/20, FUNC_LINES 365/300 |
| `apps/desktop/electron/connection-config.ts::normalizeSshConfig` | CC 31/20 |
| `apps/desktop/src/app/chat/hooks/use-composer-actions.ts::useComposerActions.attachDroppedItems` | CC 31/20 |
| `apps/desktop/src/app/cron/index.tsx::CronEditorDialog` | CC 31/20 |
| `apps/desktop/src/contrib/runtime-loader.ts::codeRanges` | CC 31/20 |
| `apps/desktop/src/lib/preview-act/act-in-page.ts::actInPageCore.survey` | CC 31/20 |
| `apps/desktop/src/plugins/hermes-bots/canonical-chat.ts::createCanonicalChat.<anon>#2` | CC 31/20 |
| `apps/desktop/src/plugins/hermes-bots/data.ts::resolveRosterMentions` | CC 31/20 |
| `apps/desktop/src/plugins/hermes-bots/group-turns.ts::harvestStrandedGroupReply` | CC 31/20 |
| `apps/desktop/src/plugins/kanban/board.tsx::CardFooter` | CC 31/20 |
| `apps/desktop/src/store/subagents.ts::toProgress` | CC 31/20 |
| `ui-tui/src/domain/messages.ts::toTranscriptMessages` | CC 31/20 |
| `ui-tui/src/lib/virtualHeights.ts::estimatedMsgHeight` | CC 31/20 |
| `apps/desktop/electron/main.ts::testDesktopConnectionConfig` | CC 30/20 |
| `apps/desktop/electron/updater/checkout.ts::createCheckoutStrategy.applyBody` | CC 30/20 |
| `apps/desktop/src/app/chat/right-rail/preview-act.ts::driveAction` | CC 30/20 |
| `apps/desktop/src/app/contrib/hooks/use-background-sync.ts::reconcileTileTranscripts` | CC 30/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/input-requests.ts::handleInputRequestEvent` | CC 30/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/lifecycle.ts::handleLifecycleEvent` | CC 30/20 |
| `apps/desktop/src/app/settings/config-settings.tsx::ConfigSettingsInner` | CC 30/20 |
| `apps/desktop/src/lib/keybinds/approval-keys.ts::handleApprovalKey` | CC 30/20 |
| `apps/desktop/src/plugins/hermes-bots/group-turns.ts::pollGroupMemberTurn` | CC 30/20 |
| `apps/desktop/src/store/session-control.ts::parseGoal` | CC 30/20 |
| `ui-tui/src/components/activeSessionSwitcher.tsx::ActiveSessionSwitcher.<anon>#3` | CC 30/20 |
| `ui-tui/src/sdk/apps/gridTest.tsx::reduce` | CC 30/20 |
| `web/src/pages/EnvPage.tsx::EnvVarRow` | CC 30/20 |
| `ui-tui/src/app/useMainApp.ts::useMainApp` | FUNC_LINES 444/300 |
| `apps/desktop/src/components/assistant-ui/thread/list.tsx::ThreadMessageListInner` | CC 23/20, FUNC_LINES 437/300 |
| `apps/desktop/electron/connection-registry.ts::resolvedConnectionId` | CC 29/20 |
| `apps/desktop/src/app/chat/composer/rich-editor.ts::normalizeComposerEditorDom` | CC 29/20 |
| `apps/desktop/src/app/chat/sidebar/projects/workspace-group.tsx::SidebarWorkspaceGroup` | CC 29/20 |
| `apps/desktop/src/app/pet-generate/pet-generate-content.tsx::PetGenerateContent` | CC 29/20 |
| `apps/desktop/src/app/session/hooks/use-session-actions/persisted-live-turn.ts::reconcilePersistedLiveTurn` | CC 29/20 |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts::reconcileResumeMessages.<anon>` | CC 29/20 |
| `apps/desktop/src/lib/inflight-turn-journal.ts::isSnapshot.<anon>` | CC 29/20 |
| `apps/desktop/src/plugins/hermes-bots/group-round-members.ts::runGroupRoundMember` | CC 29/20 |
| `apps/desktop/src/sdk/index.ts::host.deleteProfile` | CC 29/20 |
| `ui-tui/src/components/markdown.tsx::MdInline` | CC 29/20 |
| `web/src/components/MemoryPressureBanner.tsx::MemoryPressureBanner` | CC 29/20 |
| `web/src/pages/SkillsPage.tsx::SkillsPage` | CC 29/20, FUNC_LINES 317/300 |
| `apps/desktop/electron/bootstrap-runner.ts::runBootstrap` | CC 28/20 |
| `apps/desktop/electron/managed-ssh-update.ts::parseRemoteUpdateObservation` | CC 28/20 |
| `apps/desktop/electron/remote-lifecycle.ts::readLockfile` | CC 28/20 |
| `apps/desktop/src/app/chat/composer/hooks/use-voice-conversation.ts::useVoiceConversation.handleTurn` | CC 28/20 |
| `apps/desktop/src/app/chat/sidebar/profile-remote-override-dialog.tsx::ProfileRemoteOverrideDialog` | CC 28/20 |
| `apps/desktop/src/app/contrib/hooks/use-background-sync.ts::rehydrateLiveSessionStatuses` | CC 28/20 |
| `apps/desktop/src/app/right-sidebar/review/index.tsx::ReviewPane` | CC 28/20 |
| `apps/desktop/src/app/session-import/index.tsx::SessionImportView` | CC 28/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/index.ts::useGatewayEventHandler.<anon>#2` | CC 28/20 |
| `apps/desktop/src/lib/chat-messages/tool-parts.ts::findToolPartIndex` | CC 28/20 |
| `apps/desktop/src/lib/deeplink-routes.ts::resolveDeepLinkAction` | CC 28/20 |
| `apps/desktop/src/plugins/hermes-bots/group-rounds.ts::parseGroupChatMentions` | CC 28/20 |
| `apps/desktop/src/store/model-presets.ts::applyModelPreset.<anon>#2` | CC 28/20 |
| `ui-tui/packages/hermes-ink/src/ink/ink-focus-redraw.test.ts::TermModel.write` | CC 28/20 |
| `web/src/pages/PluginsPage.tsx::PluginRowCard` | CC 28/20 |
| `web/src/pages/PluginsPage.tsx::CatalogEntryCard` | CC 28/20 |
| `web/src/pages/SessionsPage.tsx::SessionRow` | CC 28/20 |
| `apps/desktop/electron/managed-ssh-update.ts::waitForManagedRemoteUpdate` | CC 27/20 |
| `apps/desktop/src/app/contrib/hooks/use-background-sync.ts::reconcileActiveTranscript` | CC 27/20 |
| `apps/desktop/src/app/session/hooks/use-preview-routing.ts::usePreviewRouting.handleDesktopGatewayEvent` | CC 27/20 |
| `apps/desktop/src/app/session/hooks/use-prompt-actions/slash.ts::useSlashCommand.<anon>.actionHandlers.browser` | CC 27/20 |
| `apps/desktop/src/app/session/hooks/use-session-actions/index.ts::useSessionActions.forkBranch` | CC 27/20 |
| `apps/desktop/src/app/session/hooks/use-session-actions/index.ts::useSessionActions.removeSession` | CC 27/20 |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts::chatMessagesEquivalent` | CC 27/20 |
| `apps/desktop/src/components/desktop-install-overlay.tsx::applyEvent` | CC 27/20 |
| `apps/desktop/src/plugins/hermes-bots/group-turns.ts::ensureGroupChatSession` | CC 27/20 |
| `apps/desktop/src/store/connections.ts::initializeConnectionsRegistry` | CC 27/20 |
| `ui-tui/packages/hermes-ink/src/ink/events/input-event.ts::parseKey` | CC 27/20 |
| `ui-tui/packages/hermes-ink/src/native-ts/yoga-layout/index.ts::computeFlexBasis` | CC 27/20 |
| `ui-tui/src/components/agentsOverlay.tsx::Detail` | CC 27/20 |
| `ui-tui/src/components/journey.tsx::Journey` | CC 27/20 |
| `web/src/pages/CronPage.tsx::CronPage.<anon>#21` | CC 27/20 |
| `apps/desktop/electron/connection-registry.ts::normalizeConnectionInput` | CC 26/20 |
| `apps/desktop/electron/linux-nvidia-egl-fallback.ts::decideNvidiaEglFallback` | CC 26/20 |
| `apps/desktop/electron/main.ts::sanitizeConnectionProfiles` | CC 26/20 |
| `apps/desktop/electron/profile-session-routing.ts::fetchSessionRowsInPages` | CC 26/20 |
| `apps/desktop/src/app/chat/sidebar/projects/workspace-groups.ts::overlayRepoLanes` | CC 26/20 |
| `apps/desktop/src/app/contrib/hooks/desktop-onboarding-metrics.ts::onboardingTransition` | CC 26/20 |
| `apps/desktop/src/app/session/hooks/use-session-list-actions.ts::useSessionListActions.refreshSessions` | CC 26/20 |
| `apps/desktop/src/app/settings/billing/use-billing-state.ts::deriveUsageRows` | CC 26/20 |
| `apps/desktop/src/app/settings/local-models-runtime-section.tsx::LocalModelsRuntimeSection` | CC 26/20 |
| `apps/desktop/src/components/boot-failure-overlay.tsx::BootFailureOverlay` | CC 26/20 |
| `apps/desktop/src/components/pane-shell/tree/renderer/tree-split.tsx::TreeSplit.<anon>#6` | CC 26/20 |
| `apps/desktop/src/lib/mcp-import.ts::fromClaudeAdd` | CC 26/20 |
| `apps/desktop/src/plugins/hermes-bots/group-chat-view.tsx::GroupChatWorkspace.renderEntry` | CC 26/20 |
| `apps/desktop/src/store/subagents.ts::reconcileSubagentSnapshot` | CC 26/20 |
| `apps/desktop/src/themes/skin.ts::skinToDesktopTheme` | CC 26/20 |
| `ui-tui/packages/hermes-ink/src/ink/screen.ts::setCellAt` | CC 26/20 |
| `ui-tui/src/app/slash/commands/session.ts::run#19.<anon>` | CC 26/20 |
| `ui-tui/src/components/skillsHub.tsx::SkillsHub.<anon>#2` | CC 26/20 |
| `apps/desktop/electron/main.ts::broadcastBootstrapEvent` | CC 25/20 |
| `apps/desktop/electron/main.ts::ensureRegistryBackend` | CC 25/20 |
| `apps/desktop/src/app/chat/right-rail/preview-file.tsx::LocalFilePreview.<anon>#2.load` | CC 25/20 |
| `apps/desktop/src/app/chat/sidebar/session-actions-menu.tsx::useSessionActions` | CC 25/20 |
| `apps/desktop/src/app/settings/billing/errors.ts::resolveRefusal` | CC 25/20 |
| `apps/desktop/src/app/updates-overlay.tsx::UpdatesOverlay` | CC 25/20 |
| `apps/desktop/src/components/assistant-ui/tool/fallback-model/index.ts::toolDetailText` | CC 25/20 |
| `apps/desktop/src/components/assistant-ui/tool/fallback-model/index.ts::toolCopyPayload` | CC 25/20 |
| `apps/desktop/src/lib/preview-act/watch-in-page.ts::watchInPage.prose` | CC 25/20 |
| `ui-tui/packages/hermes-ink/src/ink/log-update.ts::LogUpdate.render.<anon>#3` | CC 25/20 |
| `ui-tui/packages/hermes-ink/src/ink/parse-keypress.ts::keycodeToName` | CC 25/20 |
| `ui-tui/src/app/turnController.ts::TurnController.recordMessageComplete` | CC 25/20 |
| `ui-tui/src/components/skillsHub.tsx::SkillsHub` | CC 25/20 |
| `web/src/pages/McpPage.tsx::McpPage.<anon>#17` | CC 25/20 |
| `apps/desktop/src/app/settings/vault-settings.tsx::VaultSettings` | FUNC_LINES 367/300 |
| `web/src/pages/McpPage.tsx::McpPage` | FUNC_LINES 366/300 |
| `apps/desktop/electron/main.ts::resolveHermesBackend` | CC 24/20 |
| `apps/desktop/electron/main.ts::bootstrapSshConnectionInner` | CC 24/20 |
| `apps/desktop/electron/main.ts::<anon>#143` | CC 24/20 |
| `apps/desktop/electron/profile-session-routing.ts::fetchRemoteProfileSessions` | CC 24/20 |
| `apps/desktop/electron/renderer-heap-flags.ts::readDesktopLaunchConfig` | CC 24/20 |
| `apps/desktop/src/app/chat/hooks/use-composer-actions.ts::extractDroppedFiles` | CC 24/20 |
| `apps/desktop/src/app/chat/right-rail/preview-act.ts::actOnActivePreview` | CC 24/20 |
| `apps/desktop/src/app/context-menu/app-context-menu.tsx::domSections` | CC 24/20 |
| `apps/desktop/src/app/session/hooks/use-model-controls.ts::useModelControls.refreshCurrentModel` | CC 24/20 |
| `apps/desktop/src/app/settings/connections-registry.tsx::ConnectionsRegistrySection.save` | CC 24/20 |
| `apps/desktop/src/app/shell/hooks/use-statusbar-items.tsx::useStatusbarItems.coreLeftStatusbarItems` | CC 24/20 |
| `apps/desktop/src/components/onboarding/index.tsx::ApiKeyForm` | CC 24/20 |
| `apps/desktop/src/components/ui/copy-button.tsx::CopyButton` | CC 24/20 |
| `apps/desktop/src/lib/chat-messages/reconciliation.ts::localAssistantErrorIdsToPreserve` | CC 24/20 |
| `apps/desktop/src/plugins/hermes-bots/avatar.tsx::sampleFaceRing` | CC 24/20 |
| `apps/desktop/src/plugins/hermes-bots/group-chat-view.tsx::disbandGroupChat` | CC 24/20 |
| `apps/desktop/src/plugins/hermes-bots/roster-pane-content.tsx::renderRosterContent` | CC 24/20 |
| `apps/desktop/src/plugins/kanban/board.tsx::NewTaskDialog` | CC 24/20 |
| `apps/desktop/src/store/session-states.ts::openSessionTile` | CC 24/20 |
| `apps/desktop/src/store/updates.ts::runBackendUpdate` | CC 24/20 |
| `apps/desktop/src/store/updates.ts::runEverythingUpdate` | CC 24/20 |
| `apps/desktop/src/themes/vscode.ts::convertVscodeColorTheme` | CC 24/20 |
| `ui-tui/packages/hermes-ink/src/ink/optimizer.ts::optimize` | CC 24/20 |
| `ui-tui/packages/hermes-ink/src/ink/selection.ts::wordBoundsAt` | CC 24/20 |
| `ui-tui/packages/hermes-ink/src/ink/selection.ts::shiftSelection` | CC 24/20 |
| `ui-tui/src/components/appLayout.tsx::ComposerPane` | CC 24/20 |
| `web/src/pages/FilesPage.tsx::FilesPage` | CC 24/20 |
| `web/src/pages/PluginsPage.tsx::PluginsPage.<anon>#9` | CC 24/20 |
| `apps/desktop/electron/linux-gpu-fallback.ts::decideLinuxGpuLaunch` | CC 23/20 |
| `apps/desktop/electron/main.ts::resolveRemoteBackend` | CC 23/20 |
| `apps/desktop/electron/main.ts::enumerateRegistryAgentSources.<anon>.<anon>` | CC 23/20 |
| `apps/desktop/electron/managed-ssh-update.ts::runManagedSshUpdate` | CC 23/20 |
| `apps/desktop/src/app/chat/composer/attachments.tsx::AttachmentPill` | CC 23/20 |
| `apps/desktop/src/app/chat/index.tsx::ChatRuntimeBoundary` | CC 23/20 |
| `apps/desktop/src/app/chat/sidebar/index.tsx::ChatSidebar.<anon>#17` | CC 23/20 |
| `apps/desktop/src/app/command-center/index.tsx::CommandCenterView` | CC 23/20 |
| `apps/desktop/src/app/contrib/hooks/use-session-tile-delegate.ts::useSessionTileDelegate.<anon>.resumeTile` | CC 23/20 |
| `apps/desktop/src/app/session/hooks/use-message-stream/utils.ts::applySessionInfoStatePatch` | CC 23/20 |
| `apps/desktop/src/app/settings/screenshot-settings.tsx::ScreenshotSettings` | CC 23/20 |
| `apps/desktop/src/app/shell/hooks/use-statusbar-items.tsx::useStatusbarItems` | CC 23/20, FUNC_LINES 323/300 |
| `apps/desktop/src/components/assistant-ui/thread/assistant-message.tsx::ErrorRecoveryActions` | CC 23/20 |
| `apps/desktop/src/components/assistant-ui/thread/user-message.tsx::UserMessage` | CC 23/20 |
| `apps/desktop/src/components/assistant-ui/tool/fallback-model/index.ts::dynamicTitle` | CC 23/20 |
| `apps/desktop/src/components/pane-shell/tree/renderer/tree-split.tsx::TreeSplit.resetBoundary` | CC 23/20 |
| `apps/desktop/src/components/pet/floating-pet.tsx::FloatingPet.<anon>.pull` | CC 23/20 |
| `apps/desktop/src/debug/render-counter.ts::record` | CC 23/20 |
| `apps/desktop/src/lib/chat-messages/hydration.ts::codexMessageItemText` | CC 23/20 |
| `apps/desktop/src/lib/chat-messages/tool-parts.ts::upsertToolPart` | CC 23/20 |
| `apps/desktop/src/lib/error-surface.ts::parseErrorSurface` | CC 23/20 |
| `apps/desktop/src/lib/incremental-external-store-runtime.ts::IncrementalExternalStoreThreadRuntimeCore.__internal_setAdapter` | CC 23/20 |
| `apps/desktop/src/lib/markdown-preprocess.ts::normalizeFenceBlocks` | CC 23/20 |
| `apps/desktop/src/plugins/hermes-bots/create-dialog.tsx::CreateAgentDialog.ensureAgentCreated.flight` | CC 23/20 |
| `apps/desktop/src/plugins/hermes-bots/profile-ops.ts::mergeServerMeta` | CC 23/20 |
| `tests-js/assistant-ui-tap-compat.test.ts::caretSatisfies` | CC 23/20 |
| `tests/e2e/core/terminal/_vt.py::Screen._step` | CC 23/20 |
| `ui-tui/packages/hermes-ink/src/ink/render-node-to-output.ts::renderScrolledChildren` | CC 23/20 |
| `ui-tui/packages/hermes-ink/src/ink/renderer.ts::createRenderer.<anon>` | CC 23/20 |
| `ui-tui/scripts/visual/render.tsx::ansiToHtml` | CC 23/20 |
| `ui-tui/src/components/prompts.tsx::ClarifyPrompt.<anon>#3` | CC 23/20 |
| `ui-tui/src/lib/terminalSetup.ts::configureTerminalKeybindings` | CC 23/20 |
| `web/src/components/Markdown.tsx::parseBlocks` | CC 23/20 |
| `web/src/components/ModelPickerDialog.tsx::ModelPickerDialog.applySelection` | CC 23/20 |
| `web/src/pages/PluginsPage.tsx::MemoryProviderSetupHint` | CC 23/20 |
| `apps/desktop/electron/desktop-remote-route.ts::resolveDesktopRemoteRoute` | CC 22/20 |
| `apps/desktop/electron/main.ts::previewFileTarget` | CC 22/20 |
| `apps/desktop/electron/main.ts::drainManagedSshScope` | CC 22/20 |
| `apps/desktop/src/app/chat/composer/voice-menu.tsx::VoiceMenu` | CC 22/20 |
| `apps/desktop/src/app/chat/sidebar/session-row.tsx::SidebarSessionRowImpl.<anon>#7` | CC 22/20 |
| `apps/desktop/src/app/messaging/telegram-qr-setup.tsx::TelegramQrSetup` | CC 22/20 |
| `apps/desktop/src/app/settings/billing/use-charge-poller.ts::pollChargeSettlement` | CC 22/20 |
| `apps/desktop/src/components/assistant-ui/thread/user-edit-composer.tsx::UserEditComposer.handleKeyDown` | CC 22/20 |
| `apps/desktop/src/components/pane-shell/tree/renderer/tree-group.tsx::ZoneDropOverlay` | CC 22/20 |
| `apps/desktop/src/lib/preview-act/naming.ts::namingKit.stemOf` | CC 22/20 |
| `apps/desktop/src/lib/runtime-readiness.ts::interpretRuntimeReadiness` | CC 22/20 |
| `apps/desktop/src/lib/tour/engine.ts::runTourEngine` | CC 22/20 |
| `apps/desktop/src/plugins/hermes-bots/bot-row.tsx::GroupRow` | CC 22/20 |
| `apps/desktop/src/plugins/hermes-bots/group-chat.ts::groupSpeakerLabel` | CC 22/20 |
| `apps/desktop/src/plugins/hermes-bots/labels.ts::displayName` | CC 22/20 |
| `apps/desktop/src/store/onboarding.ts::saveOnboardingLocalEndpoint` | CC 22/20 |
| `apps/desktop/src/store/session-control.ts::parseLoop` | CC 22/20 |
| `ui-tui/packages/hermes-ink/src/ink/components/Button.tsx::Button` | CC 22/20 |
| `ui-tui/scripts/bench-streaming-md.tsx::fenceOpenAt` | CC 22/20 |
| `ui-tui/src/app/slash/commands/topup.ts::renderBillingError` | CC 22/20 |
| `ui-tui/src/lib/platform.ts::isVoiceToggleKey` | CC 22/20 |
| `web/src/pages/ModelsPage.tsx::ModelCard` | CC 22/20 |
| `web/src/pages/CronPage.tsx::CronPage` | CC 21/20, FUNC_LINES 326/300 |
| `web/src/pages/WebhooksPage.tsx::WebhooksPage` | FUNC_LINES 318/300 |
| `apps/desktop/electron/backend-health.ts::waitForHermesReady` | CC 21/20 |
| `apps/desktop/electron/connection-registry.ts::reuseMatchingPrimarySshBackend` | CC 21/20 |
| `apps/desktop/electron/main.ts::runHermesStart.connectionPromise` | CC 21/20 |
| `apps/desktop/electron/main.ts::createWindow#2` | CC 21/20 |
| `apps/desktop/electron/renderer-bundle.ts::manifestAssetRefs` | CC 21/20 |
| `apps/desktop/electron/ssh-connection.ts::validateSshTarget` | CC 21/20 |
| `apps/desktop/src/app/chat/composer/hooks/use-slash-completions.ts::useSlashCompletions.fetcher` | CC 21/20 |
| `apps/desktop/src/app/chat/composer/status-stack/status-row.tsx::StatusItemRow` | CC 21/20 |
| `apps/desktop/src/app/chat/sidebar/gateway-groups.tsx::GatewayProfileGroup` | CC 21/20 |
| `apps/desktop/src/app/session/hooks/use-prompt-actions/prompt-actions-harness.tsx::Harness` | CC 21/20 |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts::applyRuntimeInfo` | CC 21/20 |
| `apps/desktop/src/app/settings/billing/auto-reload-row.tsx::AutoReloadRow` | CC 21/20 |
| `apps/desktop/src/components/boot-failure-reauth.ts::sshFailureMessage` | CC 21/20 |
| `apps/desktop/src/components/pane-shell/tree/zones-engine.ts::zonesFromPoint` | CC 21/20 |
| `apps/desktop/src/components/remote-setup/use-remote-setup.ts::useRemoteSetup` | CC 21/20 |
| `apps/desktop/src/components/sync-status.ts::deriveSyncStatusSummary` | CC 21/20 |
| `apps/desktop/src/lib/chat-runtime.ts::toRuntimeMessage` | CC 21/20 |
| `apps/desktop/src/lib/voice-playback.ts::openClientDirectSpeechSession.pump` | CC 21/20 |
| `apps/desktop/src/plugins/hermes-bots/cron.tsx::RoutinesPane` | CC 21/20 |
| `apps/desktop/src/plugins/hermes-bots/screen-hero.tsx::ScreenHeroContent` | CC 21/20 |
| `apps/desktop/src/sdk/index.ts::waitForFocusedSessionHydration.<anon>.check` | CC 21/20 |
| `apps/desktop/src/store/goals.ts::nextGoalFromText` | CC 21/20 |
| `apps/desktop/src/store/projects.ts::createProject` | CC 21/20 |
| `ui-tui/src/components/markdown.tsx::renderTable` | CC 21/20 |
| `ui-tui/src/components/subscriptionOverlay.tsx::OverviewScreen` | CC 21/20 |
| `web/src/pages/McpPage.tsx::McpPage.<anon>#18` | CC 21/20 |
| `apps/desktop/src/app/command-palette/index.tsx::CommandPaletteBody` | FUNC_LINES 305/300 |
| `apps/desktop/electron/main.ts` | FILE_LINES 19750/2000 |
| `apps/desktop/src/app/session/hooks/use-session-actions.test.tsx` | FILE_LINES 7297/2000 |
| `apps/desktop/src/app/session/hooks/use-prompt-actions/index.test.tsx` | FILE_LINES 6451/2000 |
| `apps/desktop/src/i18n/fr.ts` | FILE_LINES 5705/2000 |
| `apps/desktop/src/i18n/de.ts` | FILE_LINES 5697/2000 |
| `apps/desktop/src/i18n/es.ts` | FILE_LINES 5691/2000 |
| `apps/desktop/src/i18n/en.ts` | FILE_LINES 5164/2000 |
| `apps/desktop/src/i18n/zh.ts` | FILE_LINES 4880/2000 |
| `apps/desktop/src/i18n/types.ts` | FILE_LINES 4370/2000 |
| `apps/desktop/src/i18n/ru.ts` | FILE_LINES 4030/2000 |
| `apps/desktop/src/i18n/ja.ts` | FILE_LINES 3907/2000 |
| `apps/desktop/src/app/session/hooks/use-session-actions/index.ts` | FILE_LINES 3451/2000 |
| `apps/desktop/src/store/session-states.ts` | FILE_LINES 3194/2000 |
| `apps/desktop/src/app/gateway/hooks/use-gateway-boot.test.tsx` | FILE_LINES 2866/2000 |
| `ui-tui/packages/hermes-ink/src/ink/ink.tsx` | FILE_LINES 2850/2000 |
| `web/src/lib/api.ts` | FILE_LINES 2823/2000 |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.ts` | FILE_LINES 2691/2000 |
| `apps/desktop/src/store/gateway.ts` | FILE_LINES 2624/2000 |
| `apps/desktop/src/app/session/hooks/use-session-actions/utils.test.ts` | FILE_LINES 2545/2000 |
| `ui-tui/src/__tests__/createGatewayEventHandler.test.ts` | FILE_LINES 2427/2000 |
| `ui-tui/packages/hermes-ink/src/native-ts/yoga-layout/index.ts` | FILE_LINES 2393/2000 |
| `apps/desktop/electron/connection-registry.test.ts` | FILE_LINES 2311/2000 |
| `apps/desktop/src/components/pane-shell/tree/store.ts` | FILE_LINES 2282/2000 |
| `apps/desktop/src/plugins/hermes-bots/i18n.ts` | FILE_LINES 2260/2000 |
| `apps/desktop/electron/remote-lifecycle.test.ts` | FILE_LINES 2246/2000 |
| `web/src/pages/SessionsPage.tsx` | FILE_LINES 2169/2000 |
| `apps/desktop/src/sdk/index.ts` | FILE_LINES 2097/2000 |
| `apps/desktop/src/app/chat/sidebar/index.tsx` | FILE_LINES 2084/2000 |
| `web/src/pages/ChatPage.tsx` | FILE_LINES 2073/2000 |
| `apps/desktop/e2e/update/seed.py` (timeout) | HX006×2 |
| `apps/desktop/electron/native/hud-modifier-x11.test.py` (structure) | HX011×1 |
| `apps/desktop/electron/pool-retirement-live-fixture/serve.py` (profile) | HX005×1 |
| `apps/desktop/scripts/perf/gateway_attach_bench.py` (swallow) | BLE001×1 |
| `tests/e2e/core/chaos/_gateway_fake_platform.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/cli_tmux/test_resize_scrollback.py` (test-sleep) | HX013×7 |
| `tests/e2e/core/compaction/_helpers.py` (structure) | HX011×1 |
| `tests/e2e/core/delivery/_cron_clock.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/delivery/_fake_platform.py` (correctness) | F841×1 |
| `tests/e2e/core/delivery/_fake_platform.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/mcp_plugins/mcp_fixture_server.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/parity/_gateway_child.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/parity/fixture_mcp_server.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/providers/test_oauth_anthropic_refresh.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/providers/test_oauth_shared_pool.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/sqlite/_helpers.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/sqlite/_roles.py` (test-sleep) | HX013×2 |
| `tests/e2e/core/sqlite/test_compaction_contention.py` (test-sleep) | HX013×2 |
| `tests/e2e/core/sqlite/test_torture_chamber.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/terminal/_pty.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/terminal/_vt.py` (structure) | HX011×4 |
| `tests/e2e/core/terminal/test_multiclient_session_model.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/tui_pty/_helpers.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/tui_pty/test_modals_resize.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/tui_pty/test_resize_ledger.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/upgrade/git/test_hostile_pause.py` (test-sleep) | HX013×4 |
| `tests/e2e/core/upgrade/handoff/_ns_agent.py` (structure) | HX011×1 |
| `tests/e2e/core/upgrade/handoff/_ns_agent.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/upgrade/handoff/_scenario.py` (test-sleep) | HX013×2 |
| `tests/e2e/core/upgrade/network/_netedge.py` (correctness) | F821×3 |
| `tests/e2e/core/upgrade/pm/test_interpreter_hygiene.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/windows_update/test_interpreter.py` (test-sleep) | HX013×1 |
| `tests/e2e/core/windows_update/test_pause_durable.py` (test-sleep) | HX013×2 |
| `tests/e2e/core/windows_update/test_update_lock_tree.py` (test-sleep) | HX013×1 |
| `tests/e2e/test_discord_adapter.py` (test-sleep) | HX013×1 |
| `tests/e2e/test_platform_commands.py` (test-sleep) | HX013×3 |
