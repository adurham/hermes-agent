# Fork-specific inventory — adurham/hermes-agent

*Generated 2026-11-05 from all 347 entries in `FORK.md` at `3b8709639`, after auditing every entry
against the tree (the sweep record is at the top of `FORK.md`). Classification follows each entry's
own heading, so a handful of 'feature' entries are usability fixes rather than new capability.*

| class | count |
|---|---:|
| fork-specific features | 42 |
| fork-specific fixes | 201 |
| retired / superseded | 20 |
| sync + process records | 77 |
| upstream-PR attempts (policy closed 2026-11-05) | 6 |

## A. Fork-specific features

Capabilities that exist only in this fork. Newest first.

| date | area | feature |
|---|---|---|
| 2026-10-17 | — | full automatic memory management: `memory.auto_apply`; 3-week staged-write backlog drained |
| 2026-10-14 | — | exo generation_stats telemetry: the engine's per-response speculative-decode counters were dropped by the SDK and never persisted |
| 2026-10-14 | — | silence guard: text-starvation / convergence nudge + silent-long-turn warning |
| 2026-09-01 | — | per-call `model`/`provider` override on the `consult` tool to bypass a rate-limited or exhausted `auxiliary.consult` |
| 2026-09-01 | — | exempt pinned test tooling from the uv exclude-newer window |
| 2026-09-01 | — | Tavily web-search provider plugin |
| 2026-08-28 | — | a pinned ollama-cloud primary degrades through a pinned Anthropic secondary before finally tracking the live main model) |
| 2026-08-28 | — | a role-pinned dispatch degrades to a backup model+provider instead of failing loud) |
| 2026-08-23 | — | role-name synonyms resolve to their target's model/provider/effort/persona) |
| 2026-08-23 | — | `delegation.model_by_role` dict entry form) |
| 2026-08-23 | — | swarm board: distinct status for a PM/orchestrator row blocked on its own dispatched children |
| 2026-08-23 | — | `delegation.reasoning_effort_by_role`) |
| 2026-08-14 | — | (bogus .env deprecation warning; hideable status-bar title badge) |
| 2026-08-11 | — | machine-wide subagent visibility, so concurrent sessions stop stomping on each other's file edits |
| 2026-08-10 | — | background curator's skill_manage write refusals were invisible outside a live scrollback line |
| 2026-08-10 | — | TUI: persistent to-do list panel had no visible border |
| 2026-08-10 | — | CLI: border the live subagent/swarm tracking board; fix a narrow-terminal overflow bug in both boards |
| 2026-08-09 | — | (auxiliary-client stale-token cache misses; two-tier prompt-cache TTL) |
| 2026-07-31 | — | `hermes profile create --link`: share skills/plugins/memory between profiles via symlinks |
| 2026-07-25 | — | agent.pin_anthropic_token: opt-in override to make a static Anthropic setup-token win over a refreshable Claude Code credential |
| 2026-07-24 | — | desktop: terminal pane follows the active sidebar project |
| 2026-07-24 | — | desktop: pet voice via Miku RVC voice-conversion pipeline |
| 2026-07-24 | — | desktop: Vocaloid-themed pet phrasing for the Miku bubble + voice |
| 2026-07-24 | — | miku voice pipeline: warm daemon cuts latency 8x |
| 2026-07-24 | — | desktop: LLM-generated pet dialogue, opt-in, two beats only |
| 2026-07-24 | — | desktop: workspace tab's × was missing and its "close" would have ripped the app's anchor pane out of the tree |
| 2026-07-23 | — | desktop: sidebar click opens browser-like tabs instead of always replacing the middle pane |
| 2026-07-23 | — | desktop: visible × close button on every pane tab |
| 2026-07-22 | — | desktop: sidebar drag-to-reorder from anywhere on the session name, not just a dedicated grab icon |
| 2026-07-22 | — | (desktop: sidebar drag-to-reorder inside a project/branch view, plus a nested-DndContext bug that broke it) |
| 2026-07-22 | — | desktop: resume/reconnect into a running turn now restores the real "thinking" timer and mid-tool-call state |
| 2026-07-22 | — | pet interactions: click-to-pet, zone status bubble, idle fidget |
| 2026-07-21 | — | provider-first aux-task Models-page writes were never actually provider-scoped |
| 2026-07-21 | — | per-feature lazy-install denylist |
| 2026-07-21 | — | pet zone pane for the desktop app |
| 2026-07-18 | — | `trafilatura`: free no-API-key `web_extract` backend |
| 2026-07-07 | — | consult tool + periodic nudge |
| 2026-07-07 | — | delegate auto-route to model tier + persona |
| 2026-06-24 | — | provider-scoped aux fallback: `fallback_models` map |
| 2026-06-24 | — | provider-first `auxiliary` config schema |
| 2026-06-22 | — | opt-in deferral of core toolsets via tool_search |
| 2026-06-18 | — | exo-scoped auxiliary delegation |

## B. Fork-specific fixes

Bugs the fork found and fixed — its own, or upstream bugs it hit first. Grouped by month, newest first.

### 2026-11 — 4

- 2026-11-05 *(checkpoint)* — one unreadable path aborted the whole snapshot
- 2026-11-04 *(desktop status bar + gateway usage)* — a manually switched chat kept showing Nous's spent plan
- 2026-11-01 *(turn transcript)* — a Stopped turn was persisted as "your request was not processed"
- 2026-11-01 *(gateway + CLI client)* — the Steer button was missing again, on exactly the rows the goal lanes queue

### 2026-10 — 33

- 2026-10-30 *(desktop)* — a busy app pinned the compositor with its window minimized
- 2026-10-30 *(gateway)* — a Desktop chat's queued row could be deleted, never sent
- 2026-10-30 *(desktop)* — a reading position was re-pinned to the bottom by the app's own writes
- 2026-10-30 *(gateway)* — a background completion reaches the RUNNING turn instead of the next one
- 2026-10-30 *(harness, documented, not fixed)* — a tool call cannot get the time it asks for
- 2026-10-30 *(desktop)* — an unreachable reading position stranded the reader at the top of the loaded rows
- 2026-10-29 *(desktop)* — a strip drag left the replay order of the tab strip stale
- 2026-10-29 *(desktop)* — tracing the lost reading position across tab switches (temporary probe)
- 2026-10-29 — GUI sessions kept every memory proposal in a buffer the prune deleted
- 2026-10-27 — A grandchild's process completion no longer wakes the human's chat
- 2026-10-26 — queue starvation, subagent completions, model truth, daemon leak, lease reclaim
- 2026-10-25 — the turn contract delivered declared work as a report: 28 deferred closes, 0 continued
- 2026-10-25 — every `web_extract` failed: a search-only shared `web.backend` was inherited as the FETCH backend
- 2026-10-25 — Promise gate rewritten around grammar, after measuring the miss rate
- 2026-10-24 — 
- 2026-10-18 — subagents died "Model used all output tokens on reasoning": an exo route's 8192 output cap followed the session onto ollama-cloud and into every delegated child
- 2026-10-17 — todo board toggle moved Ctrl+E → Ctrl+O: the fork had killed emacs end-of-line in the composer
- 2026-10-17 — false "Unknown toolsets: rea" warning on every CLI launch
- 2026-10-15 — (all 53 upstream workflows disabled; fork CI revival is one command)
- 2026-10-14 — guard-kill, raised shape: an engine kill the OpenAI SDK surfaced as APIError was re-streamed 3x, and the exhausted-attempt notice called it a connect failure
- 2026-10-11 — live todo board: collapsed by default, Ctrl+E expand/collapse
- 2026-10-10 — fallback↔primary flap: escalating anti-flap hold on primary-restore probes
- 2026-10-10 — solstice "No module named 'httpx'" update-log flood: checkpoint git calls paid a guaranteed-failing PM worker per call; client-side target-gap refusal + no provider scan in PM's runtime
- 2026-10-06 — badge glyphs de-VS16'd)
- 2026-10-04 — transient empty responses on the subscription route fell back to a weaker model within seconds; fallen-back children never returned; parents never saw it
- 2026-10-03 — dumb-retried and fell back instead of compressing)
- 2026-10-03 — soft-fork: `tools.tool_search.defer_toolsets` was honored by assembly but ignored by the bridge call-time/scope gates
- 2026-10-03 — scratch prune: the once-per-process flag was burned before the stamp check, so long-lived processes never pruned again
- 2026-10-03 — compaction trigger charged stale reasoning the ollama.com backend never counts
- 2026-10-03 — status-bar timers: minute/hour rollover, boundary-safe unit selection, dock widths
- 2026-10-02 — `cc_proxy_mcp.py` dedicated-login isolation restored: newest-entry resolution + `CC_PROXY_KEYCHAIN_SERVICE` pin
- 2026-10-01 — delegate_task: unknown `agent_type` silently ran on the parent model; per-role stats had stopped recording; persona prompts claimed stale models
- 2026-10-01 — Claude session-limit 429 burned the full context every turn

### 2026-09 — 30

- 2026-09-29 — delegate_task routing: the v2026.9.14 merge silently dropped per-provider delegation; 5 code commits + this entry
- 2026-09-29 — `trafilatura` post_setup: restore the merge-dropped installer + pin the declared-key contract
- 2026-09-29 — search_chain could not reach any keyless vendor; + ollama-cloud plugin base-URL fix
- 2026-09-26 — (`/model` switch warning; the fallback banner's label)
- 2026-09-25 — /26 (memory stack: 5 commits, one root-cause thread)
- 2026-09-25 — `hermes_subprocess_env`: opt-out for a nested-`claude`-as-inference-backend provider
- 2026-09-25 — `/reasoning` + `/effort` interactive picker: restore the merge-dropped UI wiring
- 2026-09-24 — desktop package.json version was 2 releases behind; the lock kept the pre-rename rcedit entry
- 2026-09-24 — CI coverage restored on the fork + hlxc as the out-of-CI runner
- 2026-09-24 — Test-infrastructure fixes found by actually running the suite on hlxc
- 2026-09-23 — (3-way test-failure comparison with failure-SIGNATURE matching)
- 2026-09-23 — Considered and deliberately NOT changed
- 2026-09-23 — consult died on the bare error instead of falling back to GLM 5.3)
- 2026-09-10 — `consult` failed at exactly 420.0s, repeatedly
- 2026-09-10 — usage anchor poisoned by folded Anthropic server-tool passes, causing premature/unnecessary Pre-API compression
- 2026-09-08 — MCP tool calls leaked a "coroutine was never awaited" warning every time
- 2026-09-08 — Dashboard "Recent Sessions" hid every LIVE session
- 2026-09-07 — CI: large GitHub-hosted runners not provisioned on this fork, stuck runs permanently held the concurrency group
- 2026-09-07 — Layer 2: reverse-tunnel clipboard bridge fixes Ctrl+V/Alt+V inside the hlxc tmux session
- 2026-09-07 — Ctrl+V/Alt+V image paste over SSH into headless Linux (kitten clipboard backend)
- 2026-09-06 — CLI clarify timeout shadowed by legacy default; batch/oneshot clarify never fire attention signals
- 2026-09-06 — Gateway restart → open desktop tabs 404 "session not found": stale-runtime recovery chain ported from upstream
- 2026-09-04 — delegate_task auto-routing: visible omission, `agent_type='auto'`, escalate-only tier check
- 2026-09-03 — CC identity refresh: stale 2.1.138 billing header broke fable-class consult (version gate)
- 2026-09-03 — Swarm-board per-agent slot → keyed active-board registry (structural fix)
- 2026-09-03 — Swarm-board single-slot race: heartbeat spam + frozen "0 tools" rows
- 2026-09-03 — Canonical exo serializer + golden byte contract (wire-payload serialization stability)
- 2026-09-02 — exo provider scoped reasoning_content pad omission (prefix-cache LCP fix)
- 2026-09-02 — Decode-only status-bar velocity + separate TTFT metric
- 2026-09-01 — Tavily kept keyed-only; removed from the default-on keyless ring

### 2026-08 — 43

- 2026-08-31 — container-key collapse hid the real task_id)
- 2026-08-30 — role='orchestrator' with no agent_type= silently inherited the dispatching session's own model, no warning
- 2026-08-30 — stale model strings silently accepted at top-level dispatch + hardcoded tier literals that drift stale every generation
- 2026-08-30 — a subagent could bypass role resolution entirely via a bare model= on its own nested dispatch
- 2026-08-30 — unanswerable after the fact)
- 2026-08-30 — still-running subagent's background-process completion notifications bubbling into the top-level user-facing chat
- 2026-08-28 — dashboard `_AUX_TASK_SLOTS` was stale: 11/23 built-in aux tasks, not all of them
- 2026-08-28 — (reported "compression never fires for subagent sessions" was a false alarm from misread telemetry; added the missing positive-signal counter)
- 2026-08-25 — no opt-out for the adaptive/medium reasoning-effort default (flagged , pre-existing since d394e56ec6)
- 2026-08-25 — top-level `model`/`agent_type` silently dropped for every `delegate_task` BATCH fan-out
- 2026-08-23 — nested delegation: orchestrator subagent's blocking `delegate_task` killed at 420s by the generic tool deadline, orphaning its own grandchildren for ~7h
- 2026-08-23 — swarm board: nested orchestrator subagents rendered as a flat, unbounded row list with no hierarchy
- 2026-08-23 — status bar context counter permanently stuck at "0/1M" for the whole session
- 2026-08-22 — Anthropic-native auxiliary calls silently dropped the per-task `timeout`, letting a hung connection stall CLI exit for 11+ minutes past the configured budget
- 2026-08-22 — `consult` tool: calling model writes a literal "[truncated]"/"(see above)" placeholder into `question`/`context` instead of real content, wasting a full reference-model call
- 2026-08-22 — typing a message with an attached image while the agent was busy could crash the interrupt path: `TypeError: sequence item 0: expected str instance, tuple found`
- 2026-08-20 — CLI's "Redirected current turn" confirmation lied when a redirect degraded to a queued steer
- 2026-08-20 — batch recheck of all 8 open PRs (Step 0o)
- 2026-08-20 — #72164 also found genuinely stale on the same check-in ( follow-up)
- 2026-08-19 — `hermes config set delegation.model_by_role.<role>` spuriously warned "not a recognized config key" for every role, including existing ones
- 2026-08-19 — pre-existing, not from the mcp 2.0 port)
- 2026-08-19 — second file the migration missed)
- 2026-08-19 — `cc_proxy_mcp.py` bridge broke on mcp 2.0 SDK, missed by the 2.x migration
- 2026-08-19 — a confirmed /steer got silently dropped by a hard Ctrl+C/Esc stop
- 2026-08-18 — v2026.8.18 merge reintroduced `agent-browser`/`@streamdown/math` as root npm deps, breaking `npm ci` on every `hermes update`
- 2026-08-16 — test suite spoke real audio through the developer's speakers
- 2026-08-14 — startup typeahead corrupting the session: `^[[99;5u`/`[[200~` composer garbage, per-second cursor-jump overwrite, pastes as ctrl codes
- 2026-08-14 — double-bracket bracketed-paste garble surviving to the input line
- 2026-08-14 — bogus "Unknown toolsets: a2a" warning on every CLI launch
- 2026-08-14 — crash on every launch: `NameError: disabled_toolsets`
- 2026-08-10 — spawn-time Transport A session registration dropped the parent's `cli` ref, breaking a subagent's first `send_to_parent` before the next idle tick
- 2026-08-10 — the ACP half of the same registration gap, companion to `9f6a4da9`)
- 2026-08-10 — Transport A never registered the top-level session, so every subagent `send_to_parent` fell through to Transport B's approval gate
- 2026-08-10 — the gateway half of the same registration gap, companion to `9f6a4da9` and the ACP fix)
- 2026-08-10 — TUI todo/task board: no border, ambiguous `[x]`/`[>]` markers, naive truncation buried active work
- 2026-08-09 — four independent CI job failures on main
- 2026-08-09 — a live "Initializing agent..." misdiagnosis led to root-causing OAuth-credential-churn forcing a full agent rebuild on every token refresh
- 2026-08-09 — (Ollama Cloud always reports 0 cache tokens: confirmed upstream reporting gap, NOT a Hermes bug, no code change)
- 2026-08-09 — status bar Δ+ context-growth segment gated behind an arbitrary 2K floor, hid on most turns
- 2026-08-09 — active-turn redirect: a hard stop silently destroyed an already-accepted correction
- 2026-08-08 — CLI: agent force-reinitialized on every turn, not just on real config changes
- 2026-08-08 — `hermes config unset` silently dropped the trailing Security/Fallback Model comment blocks
- 2026-08-02 — #72087 and #72153 both hit real upstream refactors

### 2026-07 — 79

- 2026-07-29 — dies after one animation frame)
- 2026-07-28 — desktop: pet stationary jump pose looped in place after the first hop instead of bobbing
- 2026-07-28 — desktop: Pet Zone dragged into the bottom dock made the whole bottom band unresizable
- 2026-07-28 — xterm.js's own Unicode-11 table disagrees with the 2026-07-24 fix)
- 2026-07-28 — single-subagent status printed a new scrollback line every 30s instead of updating in place
- 2026-07-28 — desktop: duplicate session tabs from bypassed dedup guard
- 2026-07-27 — Real maintainer/community response ( through 2026-07-30)
- 2026-07-27 — test-order-dependent failure: `test_profile_global_fallback_normalizes_in_memory_without_writing` leaked host Keychain/env state
- 2026-07-27 — macOS-only failure: `test_media_files_routed_by_type` compared against an unresolved /tmp symlink path
- 2026-07-27 — test-order/environment-dependent failure: `test_seed_supervise_skeleton_*` setgid bit silently stripped depending on pytest's ambient basetemp group ownership
- 2026-07-27 — 2 real intra-file test-order leaks in test_tui_gateway_server.py, surfaced by pytest-randomly's own CI run
- 2026-07-27 — missing encoding= on 4 open()/.read_text() calls)
- 2026-07-27 — a 6th real intra-file test-order leak, plus a subtle monkeypatch.delenv footgun: it snapshots the CURRENT value, not the pre-test value
- 2026-07-27 — disabled by default, still available on demand)
- 2026-07-26 — pre-existing test-suite failures: 6 real production bugs + ~50 stale-mock/assertion fixes
- 2026-07-26 — relative exclude-newer + missing encoding=)
- 2026-07-26 — self-update relaunch fixup stripped mac entitlements + hardened runtime
- 2026-07-26 — relaunch fixup only ran on the "rebuild happened" branch, not the "already up to date" skip branch
- 2026-07-26 — pet: uncapped rAF loops kept running at full rate in the background, chewing battery
- 2026-07-26 — regression from the same-day pet visibility-gate fix: sprite froze on one frame
- 2026-07-26 — second regression on the same day: document.hidden itself is unreliable in this app, not just hasFocus()
- 2026-07-24 — Miku pet voice: leading-edge trim clipped word onsets, made speech unintelligible
- 2026-07-24 — sluggish/draggy cadence + always-instant speech)
- 2026-07-24 — desktop: project-terminal follow duplicated tabs on repeated switches
- 2026-07-24 — desktop: narrow pet voice to "needs you" + "turn done", not running chatter
- 2026-07-24 — idol/performer voice, not studio-engineer)
- 2026-07-24 — pet_dialogue prompt taught nothing about spoken delivery
- 2026-07-24 — desktop: tab drag lit up the layout-edit dashed zone overlay
- 2026-07-24 — desktop: session/tab drag-to-reorder didn't work at all, or silently reverted
- 2026-07-24 — thinking-signature retry recovery no longer forces a user-facing ⚠️ warning
- 2026-07-24 — profile clone: `--clone`/`--clone-config` never copied installed pets
- 2026-07-24 — invisible token/cost doubling from native `web_search_20250305` server-tool passes; sibling fail-closed evidence-scrubber in `_sanitize_replay_block`
- 2026-07-24 — desktop: replaced deprecated `rcedit` dep with `resedit`
- 2026-07-24 — `get_cwidth()` blind to emoji+VS-16)
- 2026-07-23 — spurious "Event loop is closed" traceback on /exit
- 2026-07-23 — desktop: malformed CSS comment tripped a build-time lightningcss warning
- 2026-07-23 — desktop: running tool call buried mid-group, tool window too short
- 2026-07-23 — pet: jump hop looked abrupt/teleport-y
- 2026-07-23 — desktop: queued prompt + attachments could deliver into a different, currently-active session
- 2026-07-23 — pet: jump still didn't lift off the ground
- 2026-07-23 — pet: jump bob too high + didn't replay on repeat clicks
- 2026-07-23 — pet: running animation looked janky on Retina
- 2026-07-23 — pet: kept "running" against a wall it had already stopped at
- 2026-07-22 — desktop: Terminal-deck layout opened to the logs tab instead of terminal
- 2026-07-22 — desktop: terminal glyphs render as tofu boxes, missing Nerd Font fallback
- 2026-07-22 — desktop: clicking back into a still-running session showed a blank transcript
- 2026-07-22 — desktop: work profile deletion silently reverted after quitting and reopening the app
- 2026-07-22 — desktop: duplicate "working" pulse indicators for collapsed sidebar session groups; stale indicators never cleared
- 2026-07-22 — desktop: queued composer message could be delivered into a different, currently-viewed session
- 2026-07-22 — aux tasks stuck on stale provider-block "default" model after switching main; "Reset all to main" wrote nothing
- 2026-07-22 — desktop model picker hid Anthropic despite valid Claude Code credentials
- 2026-07-22 — pet zone: roam/drag used viewport coords, pet vanished
- 2026-07-22 — pet: idle fidget leaked into the status bubble; zone bubble clipped
- 2026-07-22 — pet: roam froze entirely during real agent activity
- 2026-07-22 — pet zone: resizing the pane didn't adjust the walk
- 2026-07-21 — desktop package.json version stuck at 0.17.0
- 2026-07-21 — aux-task pin silently reverted on every save
- 2026-07-21 — beta-only kwargs crash on SDK < 0.100
- 2026-07-21 — doctor Tool Availability ignores agent.disabled_toolsets
- 2026-07-19 — History squash
- 2026-07-19 — `agent/display.py`: CLI todo tool showed a bare count, never the actual checklist
- 2026-07-19 — (`cli.py`: unreproduced spinner-timer anomaly — forensic logging added, not yet root-caused)
- 2026-07-18 — spinner redraw leaves stale digits + phantom "Δ+NNNK new" context-delta balloon
- 2026-07-18 — `agent/auxiliary_client.py`: runtime-main override was a process-global data race, not thread-local
- 2026-07-15 — response box: short complete sentences invisibly buffered before a tool call
- 2026-07-14 — Claude Code Keychain write-back on OAuth refresh
- 2026-07-14 — Bearer clients no longer leak env ANTHROPIC_API_KEY as x-api-key
- 2026-07-14 — content-filter trigger patterns weren't scrubbed from tool results, only from compaction/refusal-retry
- 2026-07-14 — exit watchdog swallows cost report / resume hint
- 2026-07-14 — memory-confirm cost not counted, no exit progress indicator
- 2026-07-14 — background skill-curator's own LLM cost was also uncounted
- 2026-07-14 — end-of-session ordering: curator, then memory-confirm, then cost summary
- 2026-07-12 — suppress thinking-progress overlay when reasoning is streaming
- 2026-07-11 — auxiliary: honor explicit top-level task pins in provider-first schema
- 2026-07-10 — consult: reject degenerate reference-model answers
- 2026-07-07 — tool-call loop guardrails now block by default
- 2026-07-07 — clarify/approval panel rendering + /usage + estimator + spinner
- 2026-07-07 — (deterministic suite, no behavior change)
- 2026-07-06 — status-bar timer + approval timeout semantics

### 2026-06 — 10

- 2026-06-22 — Anthropic aux 400: thinking + temperature collision
- 2026-06-22 — cheap Haiku for trivial aux on Anthropic-main)
- 2026-06-22 — Anthropic aux 400: Haiku request carries Sonnet-only context-1m beta
- 2026-06-22 — auto aux + cheap pin gets main-model fallback on single-provider setups
- 2026-06-21 — Anthropic aux 401 fix + provider-matched sonnet-4-6
- 2026-06-10 — provider-aware web search: native on Claude
- 2026-06-10 — native web_search wire-shape orphan pairing
- 2026-06-08 — MCP parallel-safe prefix gate
- 2026-06-02 — prompt-cache cost work
- 2026-06-02 — oversized-image 413 / false compaction

### undated — 2

-  — Production fixes (not test-only)
-  — all fixable, all fixed

## C. Retired / superseded

Every fork-only file that has been deliberately removed, with the date and the reason. Source of truth:
the "Retired boundaries" table in `FORK.md`. Nothing here should be re-carried on a sync.

| path | retired | why |
|---|---|---|
| `agent/fork/anthropic_server_tool_passes.py` | 2026-09-25 | Slice C — Anthropic server-tool cluster removed (owner-approved) |
| `agent/fork/anthropic_native_web_search.py` | 2026-09-25 | Slice C — native web search moved to upstream |
| `agent/fork/anthropic_messages.py` | 2026-09-14 | vendored converter superseded by `agent/anthropic_message_convert.py` |
| `agent/fork/tool_search_lazy.py` | 2026-10-09 | covered by upstream `tools/tool_search.py` |
| `agent/cc_aliases.py` | 2026-09-25 | Slice B — CC alias wire shape removed |
| `agent/gemini_cloudcode_adapter.py` | 2026-09-23 | account-ban risk + non-functional |
| `agent/google_oauth.py` | 2026-09-23 | same decision as the Gemini adapter |
| `agent/promise_gate.py` | 2026-11-05 | inert under `turn_contract`; superseded by `agent/end_turn.py` |
| `hermes_cli/keyboard_protocol.py` | 2026-08-26 | upstream `pt_input_extras.py` supersedes the mapping table |
| `hermes_cli/ruflo_agents.py` | 2026-10-09 | de-fork round 2 — the ruflo shim was deleted; personas carry the catalog |
| `hermes_cli/submit.py` | 2026-09-22 | superseded by upstream `hermes peer run` |
| `hermes_cli/onboarding_migrations.py` | — | never existed in this tree; listed by mistake |
| `plugins/web/claude_code/` | 2026-09-25 | Slice A — CC web backend removed |
| `plugins/web/trafilatura/` | 2026-10-09 | de-fork round 2 — free extract backend retired |
| `tools/swarm_board.py` | 2026-09-23 | superseded by upstream's `cli_subagent_monitor` dock |
| `tools/hermes_load_tools.py` | 2026-10-09 | retired with `tool_search_lazy` |
| `scripts/setup-merge-drivers.sh` | 2026-10-09 | stub for an inert merge driver |
| `apps/desktop/scripts/diag-*.mjs` | 2026-11-05 | investigation scaffolding, zero referrers |
| `apps/desktop/scroll-harness.html + src/dev/scroll-harness.tsx` | 2026-11-05 | harness those probes drove |
| `scripts/hermes_token_check.py` | 2026-11-05 | manual diagnostic, referenced only by its own test |

## D. Sync and process records

The merge history and the audits that produced the lists above.

- 2026-11-05 — v0.23.0 merged
- 2026-11-05 — v0.23.0 landed on `main`, and the ledger behind it
- 2026-11-05 — FORK.md audited against the tree
- 2026-11-05 — Fork divergence review
- 2026-11-03 — v0.22.6 and v0.22.7 merged
- 2026-10-30 — a restore that never landed left the reader mid-flight
- 2026-10-30 — an empty model reply was recorded as a completed review, and the fallback was dead config
- 2026-10-26 — Fable review dispositions on the seven fixes above
- 2026-10-25 — Fable review of the continuation gate: ownership as the discriminator, idle re-closes as refusals
- 2026-10-24 — (v0.22.2 → v0.22.5, 3,291 commits, 4,690 changed files, 65 conflict files)
- 2026-10-24 — (v0.22.5): the merge defects the suites caught, and the residuals
- 2026-10-17 — /18 (rule-family cleanup: F821/F811, BLE001/S110, ASYNC/HX, RUF006, LIX001, correctness)
- 2026-10-15 — (MacBook + hermes-gw-01 to v0.22.2 @ 0988db4a)
- 2026-10-15 — (v0.21.7 → v0.22.2, 2983 commits, 1274 changed files, 15 conflict files)
- 2026-10-15 — (v0.22.2): suite triaged clean, six merge defects fixed, round-5 audit recorded
- 2026-10-15 — (post-v0.22.2 review): Fable review dispositions, fork-symbol sweep, live-update runbook
- 2026-10-13 — (post-v0.21.7 sync): holographic seam restore, bell default aligned, vision fix re-ported
- 2026-10-13 — (v0.21.7): route-model alignment, upstream's new gates, ruff-strict sweep, health lane classified
- 2026-10-13 — (v0.21.6 → v0.21.7, 777 commits, 70 conflict files)
- 2026-10-10 — Personal MacBook updated to the synced head + pm-runtime bootstrap fix
- 2026-10-09 — (v2026.9.24 → v0.21.6, 9180 commits, 172 conflict files)
- 2026-10-09 — (52 merge-caused test files fixed in 3 commits; 7 items parked at owner decisions)
- 2026-09-29 — (Slice A/C dead-config follow-through: `anthropic_native_search` + `claude-code` web backends)
- 2026-09-26 — (FORK.md corrected against the post-Slice-B tree; no code change)
- 2026-09-24 — (v2026.9.14 → v2026.9.21, 5173 commits, 141 conflict files)
- 2026-09-24 — guard added)
- 2026-09-24 — current inventory (verified )
- 2026-09-23 — (three trailing commits that landed after the sweep's own docs commit; documented here retroactively)
- 2026-09-23 — origin/main drift absorbed into the sync branch
- 2026-09-23 — (~30 fork call sites silently orphaned by the v2026.9.14 decomposition)
- 2026-09-21 — (v2026.8.31 → v2026.9.14, 7136 commits, 182 conflict files)
- 2026-09-07 — (explicit user decision)
- 2026-09-01 — 2 pre-existing since pre-sync)
- 2026-08-31 — (v2026.8.19 → v2026.8.31, 2287 commits, 87 conflict files)
- 2026-08-30 — delegation personas moved from ~/.hermes-only runtime files into tracked fork source, with manifest-based bundled-vs-customized sync
- 2026-08-25 — (v2026.8.18 → v2026.8.19, 804 commits, 40 conflict files)
- 2026-08-25 — async batch completion header reported the batch-default model, silently misattributing every per-task model pin
- 2026-08-23 — (removed dead `_get_child_max_runtime()` / `delegation.child_max_runtime_seconds`)
- 2026-08-21 — added auto-decline countdown)
- 2026-08-20 — session-end memory: raised entry cap + existing-fact cleanup pass
- 2026-08-18 — (v2026.8.13 → v2026.8.18, 1396 commits, 44 conflict files + 2-commit second-round reconciliation)
- 2026-08-18 — 3 real merge regressions caught by a post-sync second-opinion review, none surfaced by the original test scope
- 2026-08-13 — (v2026.8.3 → v2026.8.13, 1620 commits)
- 2026-08-11 — closed two real gaps in machine-wide subagent visibility, found by a second external review pass
- 2026-08-10 — two review-flagged gaps in today's delegation/Transport A work: no session-ownership check on `/stop <id>`; gateway unregister could silently no-op or unregister the wrong id
- 2026-08-10 — TUI: persistent to-do list moved out of transcript, anchored to status line
- 2026-08-08 — drop spent 2026-07-19 spinner-timer forensic latch
- 2026-08-08 — bump stale `exclude-newer` uv cutoff, re-verify the absolute-UTC mechanism is still correct
- 2026-08-04 — (v2026.8.3 / v0.20.0 "Herald", 4,032 commits, ~200 conflict files)
- 2026-07-27 — previous commit's own CI run caught 2 real bugs in the mock-audit work itself
- 2026-07-26 — repoint all "get code/docs from here" links at the fork + scripts/sync-fork-branding.py
- 2026-07-26 — Post-submission Fable review pass ()
- 2026-07-25 — npm audit: 19 high-severity vulns → 0, react-router v7→v8 + minimatch/brace-expansion overrides
- 2026-07-23 — desktop: bumped Vite chunk-size warning ceiling for the intentional single-bundle build
- 2026-07-23 — hidden behind an undiscoverable ⌘G)
- 2026-07-22 — replace two deprecated/unmaintained transitive npm deps with local shims
- 2026-07-22 — background skill/memory review racing a live turn: doubled prompt-token accounting + a Ctrl+C-proof lockup
- 2026-07-21 — (v2026.7.20, 1,584 commits, 46 conflict files)
- 2026-07-14 — hot-tier audit, dry-run MVP
- 2026-07-14 — hot-tier audit live mutation
- 2026-07-14 — hot-tier audit LLM classification
- 2026-07-14 — Ctrl+C to skip the exit-cleanup wait
- 2026-07-12 — (v2026.7.7.2, 405 commits, 18 conflicts)
- 2026-07-07 — tool_search sticky activation + anthropic replay name sync
- 2026-07-04 — (v2026.7.1, 1,760 commits, 32 conflicts)
- 2026-07-01 — DSv4-local reliability sweep + aux/status-bar bugs
- 2026-06-22 — (1193 commits, 24 conflicts)
- 2026-06-10 — (187 commits, 12 conflicts)
- 2026-06-08 — (771 commits, 17 conflicts)
- — — Conflict guidance by file (refresh after each sync; line numbers drift)
- — — Hard-fork boundaries (zero merge conflicts ever)
- — — Vendor-Identifying Strings Must Not Land Without Approval
- — — Soft-fork edits (merge conflicts possible)
- — — personal/fork-only, do not upstream
- — — One-time per clone
- — — run the real blast radius, not just changed files
- — — upstream-designed changes the fork's tests hadn't followed

## E. Upstream-PR attempts (historical)

Recorded for history only — upstreaming is closed as of 2026-11-05.

- 2026-10-11 — (2 filed · 1 closed same-day · drafts refreshed · undraft + non-draft gated)
- 2026-10-10 — (round-3 queue: 4 branches ready, filing BLOCKED at the account level)
- 2026-10-04 — (resurrection batch #2: exit-summary ordering #132899, estimator dedup #132900; both DRAFT)
- 2026-10-04 — (#132889, carrier filter in the thinking-prefill gate; DRAFT filed)
- 2026-10-02 — /03 (upstream sync-readiness audit + 5 upstream PRs filed from the fork)
- — — legitimate candidates for a clean upstream PR

## F. Which fixes are still needed — Fable review + reachability check

Consulted Fable on the nine clusters, then checked every claim it made that could be settled from
the tree. Fable's framing is the important part: **"present and passing" proves a fix still applies,
not that its code path is reachable.** Reachability under the actual config (deepseek-v4.1-flash main,
all auxiliaries on ollama-cloud/nous except `consult` and `vision`-fallback on Claude, holographic
memory provider) is the real test for dead weight.

| cluster | n | verdict | basis |
|---|---:|---|---|
| Desktop app | 36 | **KEEP** | fork-only, heavily used, lowest sync cost; guard with an event-contract test, not per-fix tests |
| Models / providers | 32 | **KEEP, trim the dormant Anthropic-aux corner** | aux is ollama-cloud everywhere except `consult` (Claude) and `vision` fallback (Claude), so the Haiku-`context-1m` and single-provider cheap-pin fixes guard a path the config never takes. The Anthropic adapter itself is live for Claude roles — do not delete the adapter fixes |
| Gateway / turns | 16 | **KEEP + drift guards** | highest churn surface; every one is silent loss of user input if dropped. Checked for promise-gate dependency: none — `promise_gate` was imported only by `turn_final_response.py`, so yesterday's deletion does not orphan any of them |
| CLI / TUI | 17 | **CONSOLIDATE** | the `x-api-key` env leak and the process-global runtime-main race are must-keep and touch upstream files → pin with tests instead of hand-reapplying |
| Tools / skills | 16 | **CONSOLIDATE after checking upstream** | upstream has grown its own bot-chain/loop-guard code; if it covers the fork's guardrail, take upstream's and keep only the regression test |
| Memory | 9 | **KEEP — Fable's "likely drop" is wrong on reachability** | the staged confirm is `write_approval.MEMORY` (hot-tier `MEMORY.md` writes) and runs on every memory edit; the holographic provider only withdraws the *warm tool surface*. The warm-only part of the cluster is dormant, the hot-tier ordering/cost part is live |
| Sessions / subagents | 5 | **KEEP** | lane workers are subagents; `send_to_parent` registration failure is silent result loss |
| Git / checkpoint | 5 | **CONSOLIDATE** | merge-defect restores become one assertion each; the 2026-11-05 unreadable-path fix stays (verify it logs the skipped path) |
| Other | 62 | **RE-TRIAGE** | not a cluster: test-order leaks → shared fixtures; merge restores → drift guards; approval-timeout and cost accounting stay (kanban workers and spend numbers depend on them) |

**Cleaned up / reclassified 2026-11-05:**

- **removed** — the single-provider "cheap pin falls back to the main model" block in `call_llm`'s `is_auto` branch (13 lines) and its test (42 lines). No aux task in this config can reach it: every one has a configured fallback chain.
- **already gone** — the native-Claude web-search swap (retired in Slice C, 2026-09-25); only a comment remains.
- **kept, reclassified as live** — the Anthropic aux 401 placeholder-key guard (protects the exo→Anthropic switch; `providers.exo` is configured) and the Haiku `context-1m` fix (upstream owns the mechanism; the fork's latch is set by `turn_recovery` on a real 400 for ordinary Claude calls).

**Never drop without a replacement test:** the three gateway/turn fixes (hard stop destroying an
accepted correction, queued row deleted before send, completion landing on the running turn), the
`x-api-key` leak fix, `send_to_parent` registration, and the memory end-of-session ordering — each
fails silently rather than loudly.

**Correction to Fable's evidence read:** the 18 `tests/providers` failures are not "dead weight from
the fork" — they fail identically on the pre-merge baseline tree, i.e. they are upstream tests that
are already red, not fork fixes whose tests rotted.

## G. Fable's recommendations — executed

Checked each verdict against the tree rather than assuming the work was needed. Most of what Fable
asked for already exists; where it did, the evidence is named so nobody re-does it.

| recommendation | status | evidence |
|---|---|---|
| Desktop: guard the gateway→desktop event contract | **already in place** | `apps/desktop/src/api/canonical-producer-contract.test.ts` |
| CLI: pin the `x-api-key` leak with a test | **already in place** | `tests/agent/test_nous_portal_anthropic_wire.py:222` asserts `"x-api-key" not in headers` on the Bearer path |
| Git/checkpoint: the unreadable-path fix must log what it skipped | **already in place** | `tools/checkpoint_manager.py:519` — `logger.warning("Checkpoint of %s skipped %d unreadable path(s): %s", …)` |
| Models: provider-matrix test scoped to configured providers | **already in place** | `hermes_cli/doctor_config.py::_validate_auxiliary_config`, run against the live config: 20 tasks resolve, 0 issues (all ollama-cloud except `consult`→claude-subscription) |
| Tools/skills: check upstream for a loop guardrail first | **checked — different mechanism** | upstream's `gateway/bot_loop_guard.py` is a bot-to-bot hop cap (`bot_loop_guard.max_hops`), not a tool-call loop guard; the fork's stays |
| Gateway/turns: audit the 16 fixes against the promise_gate deletion | **audited — no dependency** | `promise_gate` was imported only by `agent/turn_final_response.py`; none of the turn/queue fixes touch it |
| Sessions/subagents: keep, they are lane-worker load-bearing | **kept, covered** | `tests/tools/test_agent_messaging_contract.py`, `test_agent_messaging_send_dispatch.py`, `test_delegate.py` |
| Models: drop the dormant Anthropic-aux subset | **cleaned up 2026-11-05** | The single-provider main-model fallback is gone (13-line FORK block + its 42-line test). The other three were NOT dormant on inspection: the placeholder-key guard protects the exo→Anthropic switch (exo is configured), and the `_oauth_1m_beta_disabled` latch is set by `turn_recovery` for ordinary Claude calls. The web-search swap was already deleted in Slice C. All four entries in `FORK.md` now say which it is |
| Memory: likely drop | **rejected on reachability** | the staged confirm is `write_approval.MEMORY` (hot-tier writes) and runs on every memory edit |
| Other: re-triage the 62 | **rule adopted, not a sweep** | merge-defect restores get a drift-guard test only the **second** time the same restore is needed; first-time restores stay as-is (a guard per restore would cost more than the restore) |

## H. Reduction review (2nd consult) — ranked cuts, verified

Goal set by the owner: stop carrying what does not earn its keep. Carrying cost = fork-added/removed
lines inside files that ALSO exist upstream (the ones that conflict each sync). Measured: **446
upstream-owned files, 32,735 changed lines**; the top 16 are 11,131 of them.

The Claude consult route failed (`HERMES_MODEL_ADMISSION_CONSUMED`), so the review ran on the
documented fallback (`glm-5.3-flash` on ollama-cloud). Its ranked list was then checked against the
tree — three of its top claims do not survive:

| reviewer claim | verification | verdict |
|---|---|---|
| Cut `tools/browser_use_cli.py` (805 lines) — "likely leftover harness" | the delta is harness-daemon OWNERSHIP machinery: `_reap_dead_child`, home-key daemon registry, `stop_harness_daemons` | **wrong** — real machinery; seam candidate, not a delete |
| `cli.py`'s 88 "delegation" hits are per-role wiring (≈1,400 lines) | the hits are the word "delegate" in docstrings; the real added defs are CLI feature glue (`_run_memory_confirm_before_exit`, `_fold_curator_cost_before_exit`, `_wrap_stream_line`, `_persist_global_model_switch`, `_handle_delegation_command`) | **wrong** — no per-role duplication to collapse |
| `agent/hot_tier_audit.py` is "internal diagnostics, archive-able" | the live config sets `curator.hot_tier_audit: true` and `hot_tier_audit_dry_run: false`, i.e. it mutates memory every curator pass | **wrong** — cutting it changes memory behaviour silently |
| Role routing could collapse to ≈400 lines (from 2,840) | `tools/delegate_tool.py` is genuinely +967/−12 of routing: `_resolve_role_credentials`, `_load_role_maps`, `_auto_route_batch`, `_persona_exists`, `_resolve_task_routes`, advisories | **partly right** — the seam is real, the 2,400-line saving is not |

What actually stands, in order of value:

1. **`tools/delegate_tool.py` (967 lines) — move the routing behind a seam.** The logic is fork-only but
   lives in an upstream file. A `delegate_tool` that imports a fork router module would keep ~1 import
   instead of 967 lines. Risk: a role silently resolving to the default model — mitigate with a startup
   assertion that every configured role resolves to model+persona, plus a before/after dump of resolved
   role configs (the config is static, so the parity check is cheap and decisive).
2. **Provider/exo/failover branches (≈3,400 lines across `anthropic_adapter`, `auxiliary_client`,
   `chat_completion_helpers`, `rate_limit_tracker`) — seam, do not delete.** Move behind fork subclasses
   registered by the fork loader, one-for-one, no behaviour change in the first pass. The exo→Anthropic
   placeholder guard stays (it protects a configured provider switch).
3. **Memory hooks in `tools/memory_tool.py` + `agent/curator.py` (≈1,477 lines) — last, and only with a
   measurement.** This is the one place where a wrong cut fails silently as memory-quality decay, and the
   holographic provider has no dedupe/refinement/contradiction fallback. Instrument the warm/extraction
   entry points with hit counters over real workdays before touching it.
4. **`hermes_cli/subcommands/peer.py` + `plugins/platforms/a2a/adapter.py` (≈616 lines)** — cheapest
   speculative surface, but only after a grep of config/scripts/cron/Electron for invocations. Failure
   mode is loud (an invocation error).

The long tail is the real pool: 21,604 lines across 430 files. The sweep for it is mechanical — fork-only
modules with zero incoming import edges and no config reference, quarantined a week before deletion.

## H1. The Fable review (route recovered) and what it changes

The first two long consult payloads died with `400 HERMES_MODEL_ADMISSION_CONSUMED` *inside* a broken
stream (`status 200, capture incomplete, relay failure IncompleteRead, native retries denied: 1`). That
is not a refusal and not quota — a one-sentence consult to the same route answered normally, and a
compacted ~1.5k-char brief also answered, while the ~7k-char briefs failed twice. It is the relay's own
retry being refused because the stream that consumed the admission never delivered.

Fable's verdict reframes the whole exercise, and it is right: **fork-only modules cost zero conflict
lines** (the 2,500-line cross-session stack is free at sync time), so the only number that matters is
*fork lines inside upstream-owned files × how often upstream rewrites that file*. Almost nothing should
be deleted; the lever is **moving code behind a seam** so the upstream file keeps a call site instead of
an implementation.

Ranked by lines evicted × churn (saving = fork lines out of the upstream file):

| # | file | mechanism | saving | failure if still needed |
|---|---|---|---|---|
| 1 | `cli.py` (1,861) | `fork_hooks.py` extension points (commands, startup, turn-end, desktop bridge, persona) | ~1,750 | **silent** — assert every declared hook fired at startup |
| 2 | `tools/memory_tool.py` (998) | register a fork tool under the same name; revert upstream's file | ~998 | loud (tool unresolved) |
| 3 | `agent/anthropic_adapter.py` (1,313) | fork adapter subclass in the provider table | ~1,250 | loud (unknown provider raises) |
| 4 | `agent/chat_completion_helpers.py` (684) | one `coerce_tool_calls(provider, response)` dispatch | ~650 | **silent and nasty** — deepseek stops calling tools; needs a canned-prompt smoke test |
| 5 | `agent/auxiliary_client.py` (1,040) | one `resolve_aux_model(task_kind)` dispatch | ~1,000 | silent — aux failures are swallowed; needs a startup probe |
| 6 | `tools/delegate_tool.py` (967) | `ROLES: dict[str, RoleSpec]` + `resolve_role()` (below) | ~940 | loud if the seam is closed |
| 7 | `tools/browser_use_cli.py` (805) | fork lifecycle module (`start/stop/attach/health`) | ~780 | loud, but low churn |
| 8 | `agent/curator.py` (479) | move last, once the hook pattern exists | ~470 | silent (audit stops, curation continues) |

**Delete candidates: none blind.** The bar Fable set is: config flag off **and** grep shows zero call
sites **and** a one-week run with the module stubbed to raise. Given three of the previous reviewer's
top three claims were false, that bar is correct.

**The `delegate_tool` seam, precisely:** eight roles is a table, not a program.
`_resolve_role_credentials` + `_load_role_maps` collapse to a declarative `ROLES: dict[str, RoleSpec]`
(~60 lines, provider/model/base_url env/key env/explicit fallback); `_resolve_task_routes` becomes a
~40-line lookup; `_auto_route_batch` stays as the only real logic (~100 lines) because it is the single
place silent misrouting can originate. The boundary:

    resolve_role(name: str) -> ResolvedRoute      # total and closed

It returns a fully bound `(provider, model_id, base_url, credential)` or raises `UnknownRole` /
`UnboundCredential`. **No fallback policy inside it** — fallback is an explicit `with_fallback(route,
alt_role)` the caller chooses and logs. Forbidden inside the seam, because each one is how a role
silently resolves to the wrong model: defaulting unknown roles to the main model, fuzzy/prefix role-name
matching, and credential chains that fall through to another provider's key. `_auto_route_batch` may pick
a wrong *role name*, but it cannot invent a model, because every route passes through `resolve_role`.
Locked by one test: enumerate all eight roles, assert the exact model string, fail CI on drift. Upstream's
file keeps one line: `route = fork_roles.resolve_role(role)`.

**First cut: `tools/delegate_tool.py`.** Not the biggest saving (`cli.py` is), but it is self-contained,
has a decisive oracle (8 roles → 8 exact assertions), converts a silent failure class into a loud one,
and removes the role-map duplication with the personas module — which proves the seam pattern before it
is applied to `cli.py`, where the touch points are many and the failure mode is silent.

