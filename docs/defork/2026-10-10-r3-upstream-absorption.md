# Round-3 upstream-absorption audit (2026-10-10) — record + disposition log (2026-10-11)

**Context.** De-fork program, round 3. Hypothesis under test (owner): *"the fork-specific
bug fixes should be mostly fixed upstream by now."* Method: 4 read-only audit units checked
all surviving fork bug-fix rows against upstream **v0.21.6** — the code at the exact sites
AND upstream history across the ~9,180-commit sync window; evidence = `file:line` + commit
refs. Auditors: 4 subagents (`deleg_49df12a6`; live transcripts in
`~/.hermes/cache/delegation/live/deleg_49df12a6/task-{0..3}.log`).

**Verdict: DISPROVEN.** Recorded counts across the surviving rows: **39 STILL-FORKONLY ·
19 PARTIAL · 2 UPSTREAMED** (both "upstreamed" = stale rows already retired in round 2: the
welcome-banner filter and the exclude-newer pin). **Net retireable by absorption: 0.**

Why: most fork fixes either
(a) protect **fork-only machinery** upstream never had a reason to touch (provider-first aux
schema, Transport A/B, per-role delegation, Δ status bar, exo handling, adaptive-thinking
default, native 1M opt-in), or
(b) fix **upstream code still broken at v0.21.6** — verified at the exact upstream lines.

> **Provenance.** Durable recovery of the 2026-10-10 audit record: the original
> `R3_CONSOLIDATED.md` + `defork2/reports/R3-U*.jsonl` were written under
> `~/.hermes/cache/scratch/sync-v0.21.6/` and deleted by the 24-hour scratch prune on
> 2026-10-11 09:08. This file carries the consolidated text and the four unit tables
> (recovered from the de-fork session `20261009_204052_656f04`); raw JSONL aggregate was
> 60 rows including duplicates (dedupe note: "48 unique rows — 39 still-fork-only,
> 19 partial, 2 upstreamed"). 15 PARTIAL rows are individually documented below.

## Unit verdicts (as delivered by the four auditors)

### U1 — 12 rows: 9 STILL-FORKONLY · 3 PARTIAL

| Row | Verdict | Why |
|---|---|---|
| 1 | STILL-FORKONLY | T `tools/delegate_tool.py` never mentions `agent_type`/`model_by_role`; persona+stats modules absent. Fork code 62 non-test lines (`73b8e64ae04`). |
| 2 | PARTIAL | Umbrella merge-regression sweep (~29 commits, ~2,259 non-test lines). Already match T exactly: `config_home.py`, `environments/local.py`, `gateway/config.py`, scoped `API_SERVER_KEY` read, IPv6 local-endpoint guard, interrupt image-tuple handling. Three generic bugs still broken in T; delegation/state/Transport-A items sit on fork-only code. |
| 3 | STILL-FORKONLY | T has no Transport A (`register_session_participant`/`send_to_parent`: 0 hits); ACP session code unregistered (`388aa20150e`). |
| 4 | STILL-FORKONLY | T has no `gateway/agent_messaging_bridge.py`, no in-process transport (`36bd139d83f`, `9e28864e45a`). |
| 5 | STILL-FORKONLY | T `auxiliary_client.py:3241` `_PAYMENT_KEYWORDS` + `_FALLBACK_REASONS` (`:7559`) don't match the OAuth-org 403; T only recognises it on the main path (`agent_runtime_helpers.py:922`). Fork fix `b82307f9f81`, 16 lines. |
| 6 | STILL-FORKONLY | T has no `/reasoning` picker (bare `/reasoning` prints state; `cli_commands_mixin.py:2267`). Fork fix `1aabe7e2f0a`. |
| 7 | STILL-FORKONLY | T `message_sanitization.py:684-696` no host exclusion; "deepseek" name-match still charges ollama-cloud routes; manual `/compress` estimate not route-aware (`conversation_compression_manual.py:58`). Fork fix `bdb0a668aa0`. |
| 8 | STILL-FORKONLY | T timers still overflow (`turn_summary.py:147`, `runtime_footer.py:65`); 59.5s → "60s" (`usage_pricing.py:745`); process dock bare seconds (`cli_process_dock.py:84`). Fork fix `9b92d6f02fb`. |
| 9 | STILL-FORKONLY | T overflow patterns + context-limit parser (`model_metadata.py:1238`): 0 hits for the exo "cache this instance was configured for" message. Fork fix `4dbeb8c625f`, 17 lines. |
| 10 | PARTIAL | Billing-header half gone from both trees. T `anthropic_adapter.py:233` still falls back to "2.1.74"; fork uses "2.1.259" (`d80b374384a`). Remaining fork change = one constant. |
| 11 | PARTIAL | Upstream `5a4dbdec278` covers `owner_task_id` plumbing (T `process_registry.py:544`+). T has no completion hold gate; gateway completion event (`run_notifications.py:1988-2002`) doesn't carry `owner_task_id`. ~6 fork lines remain. |
| 12 | STILL-FORKONLY | T `delegate_tool.py:226` ignores `role`; warned-about `agent_type` setting doesn't exist upstream (`2bd65953302`). |

**Row-2 upstream bugs still live in T (worth filing):** `mcp_tool_server_run.py:30-38` calls
`t.cancel()` outside the `try` (interpreter-shutdown crash class); `mcp_tool_discovery.py`
treats an empty cached tool list as a cache hit (server never reconnects); `hermes_cli/config.py:3553`
writes user config without commented reference sections (`config set`/`unset` delete them).
**Optional cleanup:** fork `_combine_interrupt_parts` ≈ T inline handling (#110737) — untested.

### U2 — 12 rows: 8 STILL-FORKONLY · 4 PARTIAL

| Row | Verdict | Why |
|---|---|---|
| 1 | STILL-FORKONLY | T Anthropic aux adapter (`auxiliary_client.py:1717-1795`) never reads `kwargs['timeout']`; Codex adapter does. Fork fix ~16 lines (`282da894174`) — PR candidate. |
| 2 | STILL-FORKONLY | T still passes a tuple into `interrupt()`; crash site fork-only; guard (`b1057057ca6`) protects fork's redirect-folding. |
| 3 | STILL-FORKONLY | T has no `blocked_writes`/refusal summary in curator status; `e3d2e57c9ec` makes refusals rarer, doesn't surface them. ~193 lines (`12feabce13b`). |
| 4 | STILL-FORKONLY | T `_get_cached_client` cache-hit path (`:6015-6050`) has no token freshness check (`d29317e4eea`); two-tier TTL half already retired. |
| 5 | STILL-FORKONLY | T `interrupt_control.py:195` still wipes `_pending_redirect`; T's own `test_steer.py:155-180` expects it (`dfa47d935e8`). |
| 6 | PARTIAL | Bugs 1, 2a, 2b, 6 fixed upstream or no longer apply; bug 3 (hyphenated FTS terms) still broken upstream at `retrieval.py:197`; bugs 4, 5, 7 fork-only features. |
| 7 | STILL-FORKONLY | T has no emoji+VS-16 width correction; `cli_status_bar_mixin.py:434` plain `get_cwidth` (`5068de72b56`+). |
| 8 | PARTIAL | T fixed the traceback differently (drain tasks before close `cc21c4f78ff`; reap orphans #74139). Fork delta left: `t.cancel()` inside the `try` (`mcp_tool_server_run.py:34`). Retiring defensible. |
| 9 | STILL-FORKONLY | Bug is in the fork-only provider-first aux schema (upstream has none; `d0d2e1bc029`). |
| 10 | STILL-FORKONLY | T prefix gate (`mcp_tool_discovery.py:723`) would serialize all fork unprefixed MCP tools — fork gate needed for its naming. |
| 11 | PARTIAL | Part 5 fixed upstream (`127a179ae5e`); part 1 (placeholder key reaching `_try_anthropic`, `:3051-3055`) still happens upstream; parts 2-4 = fork's aux-model choice, not a bug. |
| 12 | PARTIAL | Items 1+3 superseded upstream (`ef1b06d8536`), fork already deleted them; item 2 tests the fork-only aux model (rides with row 11). |

**Possible PRs:** row 1 (aux timeout), row 6 bug 3 (hyphen FTS), row 11 part 1 (placeholder 401), row 8 (one-line cancel move).

### U3 — 12 rows: 7 STILL-FORKONLY · 4 PARTIAL · 1 UPSTREAMED (stale)

| # | Verdict | Why |
|---|---|---|
| 1 | STILL-FORKONLY | T spinner still pads with `len()` (`display.py:931-932`); Δ segment doesn't exist upstream. |
| 2 | STILL-FORKONLY | T "pytest in argv0" check (`main.py:452`) covers only the invalid-name error; `-p randomly` still applied as profile (`:501`, `:647`); session-isolation fixture missing upstream. |
| 3 | PARTIAL | `surfaces.test.tsx` matches upstream byte-for-byte (pet zone retired); delegate default-10 test gone upstream; only MCP naming test differs (fork `<server>_<tool>` names). |
| 4 | STILL-FORKONLY | T `_clean_pass_durations` (`run_tests_parallel.py:493-510`) still drops every failed file; path-remap half retired in round 2. |
| 5 | PARTIAL | Upstream fixed `pgrep -c` test (`92686159d13`) + voice-mode socket test (`67475a62c6f` + Linux marker); fork `short_socket_dir` fixture now redundant. Pins for `is_supported_host`/`_IS_LINUX`, desktop-swap predicate, plugin_validate prefix match, Ollama fixture, web_tools home redirect still fork-only. |
| 6 | STILL-FORKONLY | T `deterministic_empty` (`empty_response_guard.py:207-226`) has no transient policy; no `empty_fallback_restore.py`, no carrier filter, no child fallback visibility. Merge-conflict hotspot (~1.3K lines). |
| 7 | STILL-FORKONLY | T `RETRY_DELAY_PATTERNS` (`retry_utils.py:96-101`) parses only "resets in N"; no wall-clock ("resets 12:30pm (America/Chicago)"). |
| 8 | UPSTREAMED (stale) | Fork banner filter already removed in round 2 (`ee0ae7e4761`); fork matches upstream. Nothing to retire. |
| 9 | PARTIAL | Pillow as core dep upstreamed (before window); T still attaches images at full size (`image_routing.py:394-397`); 413 handler doesn't shrink first (`turn_overflow.py:226`). |
| 10 | STILL-FORKONLY | T has no thinking guard on temperature re-attach (`auxiliary_client.py:1754-1759`); T avoids the 400 mostly by not defaulting thinking on — fork's adaptive-thinking default (`anthropic_adapter.py:1703`) creates the collision. |
| 11 | STILL-FORKONLY | T never sends `context-1m` to native Anthropic (only Azure: `anthropic_adapter.py:215-219`, `anthropic_endpoints.py:137`); bug exists only via fork's 1M opt-in. |
| 12 | PARTIAL | sms + google_meet sub-fixes match upstream; `tool_search_lazy` retired; rest (diagnostics imports, `internal_code_error`, exo detection, extractor, `display_prompt_tokens`) has no upstream equivalent. |

**Retire candidates:** none whole; pieces: `short_socket_dir` fixture (row 5), default-10 test (row 3); rows 10+11 removable only with the fork's feature decisions.

### U4 — 12 rows: 10 live (wholly/partly) · 1 UPSTREAMED · 1 retired-stale

| Row | Verdict | Why |
|---|---|---|
| 1 status-bar timer / approval | STILL-FORKONLY | T `cli_status_bar_mixin.py:574` still zero-pads minutes; T codex map `:833-835` sends `decline` on timeout; approval half converged; vision half (`0285cf60c`) lost in BOTH trees (T `auxiliary_client.py:5614`). |
| 2 beta-only kwargs | STILL-FORKONLY | T `chat_completion_helpers.py:3456` calls plain `.messages.stream`; upstream never sends these kwargs → no crash; retirable only with the fork's Claude-Code wire-format kwargs. |
| 3 exclude-newer pin | UPSTREAMED (already retired) | fork + T both "14 days"; uv records `exclude-newer-span="P14D"` (T `uv.lock:14`); retired in `ee0ae7e4761`. |
| 4 tab/sidebar drag | PARTIAL | Bug 2 upstream (`9c28771cf31`, `03b759db869`); bug 1 still broken in T (`session-states.ts:2392` early-returns for main session; `session-drag.ts:226,262` hardcode tile pane id); fork lane `preserveOrder` arg is dead. |
| 5 in-project reorder / single DndContext | STILL-FORKONLY | T `workspace-groups.ts:386` still sorts lanes by activity; no `SortableGroup`/lane-order atom. |
| 6 working-pulse indicators | PARTIAL | stale-busy half upstream (`42a30d13dbc`; `use-background-sync.ts` byte-identical); collapsed-group aggregate dot fork-only. |
| 7 pet-zone coordinates | STILL-FORKONLY (already retired) | upstream has no pet zone; round 2 (`ee0ae7e4761`) removed fork code (`zoneOrigin`: 0 hits). |
| 8 board border / overflow trim | PARTIAL | swarm half superseded by upstream's subagent dock (clips rows to width; T `cli_subagent_monitor.py:15-25`); todo-board trim live again (`6b1d58ea92c`) — T has no todo board, so trim still needed (now covered by upstream PR #136664). |
| 9 clarify timeout / attention | PARTIAL | timeout fix converged; T batch clarify rings bell + terminal notification (`_ring_bell`, `cli_modal_mixin.py:1024`) but off by default (`bell_on_prompt=False`); fork keeps always-on bell + native macOS banner. |
| 10 `_AUX_TASK_SLOTS` | STILL-FORKONLY | T `web_server_config.py:551` lists 12 slots, omits 7 tasks T's own defaults define (`goal_judge`, `monitor`, `moa_*`). Real upstream gap. |
| 11 relaunch fixup on skip branch | STILL-FORKONLY | T `main_desktop.py:1703-1730`: skip + up-to-date branches never re-sign; fixup only at `:1380` (staged promotion). |
| 12 aux pin stripped on save | STILL-FORKONLY | code it fixes doesn't exist upstream (no provider-first aux schema). |

## PARTIAL rows — re-evaluation ("why they don't make sense to upstream")

The PARTIAL rows decompose into three kinds of residue: **(1) already-absorbed halves** —
nothing to file; **(2) fork-coupled halves** — protect fork-only machinery, never upstreamable
as-is; **(3) genuinely upstreamable halves** — extracted to the filing queue (below).

| Row | Absorbed by upstream | Remains | Disposition |
|---|---|---|---|
| U1-2 merge-regression sweep | config_home, environments/local, gateway/config, scoped API_SERVER_KEY, IPv6 guard, interrupt image-tuple match T exactly | 3 generic upstream bugs + fork-only delegation/state/Transport-A items | 3 bugs queued (see below) |
| U1-10 CC identity refresh | billing-header half gone both trees | "2.1.259" vs T "2.1.74" fallback constant | in-fork only |
| U1-11 subagent notifications | `owner_task_id` plumbing upstream (`5a4dbdec278`) | completion hold gate + gateway event carrying `owner_task_id` (~6 lines) | in-fork (hold gate is fork-only) |
| U2-6 test-suite failures (6 bugs + stale mocks) | bugs 1, 2a, 2b, 6 fixed/obsolete upstream | bug 3 = hyphen FTS (still upstream, then); bugs 4,5,7 fork-only | bug 3 → filed #136706 → **closed** (path left core; see below) |
| U2-8 'Event loop is closed' on /exit | T fixed traceback differently (drain + reap) | 1-line `t.cancel()` inside try | queued → **filed #136704** |
| U2-11 Anthropic aux 401 | part 5 fixed (`127a179ae5e`) | part 1 placeholder-key 401 | declined (competing PRs 92125/110841) |
| U2-12 fork-only test fixes | items 1,3 superseded (`ef1b06d8536`), already deleted | item 2 = fork aux-model test | in-fork |
| U3-3 post-sync merge regressions | surfaces.test.tsx matches upstream; default-10 test gone | MCP naming test (fork `<server>_<tool>`) | in-fork (fork naming) |
| U3-5 host-shaped macOS test failures | pgrep + socket tests fixed (`92686159d13`, `67475a62c6f`) | pins/fixtures fork-only | `short_socket_dir` = cleanup candidate |
| U3-9 oversized-image 413 | Pillow-as-core upstreamed earlier | 413 shrink-first + false-compaction handling | in-fork |
| U3-12 DSv4-local reliability sweep | sms/google_meet match upstream; tool_search_lazy retired | diagnostics imports, internal_code_error, exo detection, extractor, display_prompt_tokens | in-fork (exo/DSv4 stack) |
| U4-4 tab/sidebar drag | bug 2 upstream (`9c28771cf31`, `03b759db869`) | bug 1 still broken upstream (`session-states.ts:2392`; `session-drag.ts:226,262`) | dead `preserveOrder` arg = cleanup candidate |
| U4-6 working-pulse | stale-busy half upstream (`42a30d13dbc`) | collapsed-group aggregate dot fork-only | in-fork |
| U4-8 board border/overflow | swarm half superseded by upstream dock | todo-board trim (fork's board; now PR #136664) | in-fork |
| U4-9 clarify timeout | timeout fix converged; T has default-off bell+notif | fork's always-on bell + macOS banner | owner decision |

## Section A — small in-fork cleanups still available

1. `short_socket_dir` fixture — redundant (U3-5).
2. Delegate "default-10" test piece — upstream no longer has it (U3-3).
3. Dead `preserveOrder` arg on `renderRows` — verify lane-reorder (U4-5) doesn't use it first (U4-4).
4. `mcp_tool_server_run.py:34` `t.cancel()` inside `try` — align or retire (U2-8 → also covered by #136704).
5. U1-2 sub-items already matching T (config_home, environments/local, gateway/config, scoped API_SERVER_KEY, IPv6 guard, interrupt image-tuple) — confirm + drop test residue.
6. U1-11: ~6 fork lines remain (completion hold gate + `owner_task_id` on gateway completion event).

## Section B — upstream-filing queue: disposition (2026-10-11)

1. mcp `t.cancel()` outside try → **FILED #136704** (draft; clean vs current main; 108 tests green).
2. mcp empty cached tool list → **declined** (existing issue #88661 + competing #119092).
3. `config set/unset` comment loss → **dropped** (upstream fixed independently).
4. aux native-Anthropic timeout dropped → **FILED #136705** (draft; 242 tests green).
5. hyphenated-FTS terms → filed **#136706** → **CLOSED same day** (path left core, see below).
6. placeholder-key 401 → **declined** (competing PRs 92125/110841).
7. `_AUX_TASK_SLOTS` missing tasks → **not filed**: competing open PR **#125435** (dskwe; covers the same list + CLI picker + stale-pin iteration). Branch `upstream-fix-aux-slots` kept on origin (web labels + narrower MoA stance could inform a later revision).

## Draft-batch refresh (the 11 Oct 3-4 drafts) — 2026-10-11

All 11 checked against current `main` (97a9ae8ee5). **5 were CONFLICTING → rebased, retested,
force-pushed (signed)**; 6 mergeable as-is:

| PR | branch | old → new sha | retest |
|---|---|---|---|
| #132900 | pr/estimator-interleaved | 4491b942dc → bcd887033b | 123 tests green |
| #132442 | pr/compaction-aggregator-echo | 283476e432 → c8d035507c | 4 files green — re-applied the estimator-facing re-export exclusion onto upstream's new route model |
| #132439 | pr/timer-hour-rollover | 1dcfb1c383 → 3e3ebbe463 | 51 tests green |
| #132249 | pr/update-shadowed-install | daf5bc8d01 → 158c1affab | PR tests green; test_update_autostash.py 8 fails = pre-existing Mac home-IO-guard trap (identical on pure upstream/main) |
| #132229 | pr/retry-wallclock-reset | 8ae7ab9f64 → 128d5dcc83 | 36 tests green |

**Undraft attempt (all 12 drafts): account-gated.** Both mutations denied:
`CreatePullRequest` (non-draft creation) and `markPullRequestReadyForReview` (flipping a
draft) — `"adurham does not have permission to mark the pull request … ready for review"`
(tested on #132439, #132900, #136664; creation tested on #136704). The account can only
*create* drafts and *close* PRs; a maintainer must flip them ready.

## Upstream moved: holographic left core (fork-planning item)

Upstream `cca83848ec` ("chore(memory): holographic leaves core for its community catalog
plugin (re-land #136424)"; earlier 9e827685f2 + revert 435b7bd3a7) removes
`plugins/memory/holographic/` from the tree. `plugin-catalog/holographic.yaml` now points at
`https://github.com/itpartypattaya/hermes-plugin-holographic` (v0.6.1, sha `46b6f908`;
same provider name, config `plugins.hermes-memory-store` and `memory_store.db` — "`hermes
update` moves existing users onto it"; `requires_hermes >=0.21.5`).

**Fork impact:** the fork runs the in-tree holographic provider (fact_store) — the next
upstream sync will delete it. Decision needed: adopt the catalog plugin (upstream's
migration path) or preserve the in-tree copy. The hyphen-FTS fix (#136706) would need
re-homing to the community repo if still present there (unverified; touching that repo
needs owner go).

## Owner decisions still open (from the audit)

1. Vision fix (U4-1): lost from BOTH trees — re-apply or leave lost.
2. Clarify bell (U4-9): fork's always-on bell + macOS banner vs upstream's default-off bell.
3. U3-10 (adaptive-thinking default) / U3-11 (native 1M opt-in): fixes removable only if the fork drops the features.
4. U1-2 caveat: fork's interrupt re-queue may drop attached images (upstream keeps them) — untested.
