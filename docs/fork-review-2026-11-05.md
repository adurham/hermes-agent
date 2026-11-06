# Fork divergence review — adurham/hermes-agent @ v0.23.0

*2026-11-05, revised after the owner's corrections. Scope: every change the fork carries that
upstream does not, measured against upstream tag `v0.23.0` (2bd378ed1b8d) and upstream `main`
(ca4360692, 162 commits past the tag).*

## Bottom line

- **Upstream has absorbed none of the fork's features.** Neither `v0.23.0` nor upstream `main` has an
  equivalent for the turn-close contract, consult, hot/warm memory extraction, agent-to-agent
  cross-session messaging, delegation personas/routing, usage preflight, failover identity or the
  behavioural guards.
- **Two removals landed on the strength of this review:** `agent/promise_gate.py` (inert under the
  turn-close contract) and 27 files of investigation scaffolding (~3,503 lines).
- **One recommendation was already implemented by the fork:** the warm-tier tool surface is already
  withdrawn at the schema level whenever the holographic provider is registered — the model sees
  `tier: ["hot"]` and a description pointing at `fact_store`.
- **Upstreaming is off the table** (owner, 2026-11-05: "we can't keep more than a few PRs open at a
  time and they really have to catch the devs attentions to even get CI to run"). Fork divergence is
  permanent; the de-fork goal is to keep the fork small and locally tested, not to send patches up.

## Method

1. `git diff --numstat v0.23.0..main` — the fork's whole delta.
2. Fork-added files (`--diff-filter=A`) classified by kind; each Python module's docstring read for
   purpose, then `grep -rl` for non-test referrers to separate live code from orphaned code.
3. Upstream `v0.23.0` **and** upstream `main` searched by concept for an equivalent implementation
   of each fork feature.
4. Each fork module's last commit date and commit count, to find features that are wired but have
   not been touched in months.
5. Live config (`~/.hermes/config.yaml`) checked for the feature's user-facing knobs.

## Size of the divergence

| area | files | added | removed |
|---|---:|---:|---:|
| `tools/` | 67 | 14,571 | 320 |
| `agent/` | 98 | 13,038 | 559 |
| `apps/desktop` | 146 | 8,641 | 877 |
| `hermes_cli/` | 103 | 8,588 | 431 |
| repo root | 21 | 3,458 | 239 |
| `plugins/` | 43 | 1,502 | 385 |
| `scripts/` | 18 | 1,423 | 23 |
| `gateway/` | 27 | 1,253 | 92 |
| other | 68 | 1,943 | 246 |
| **source total** | **591** | **54,417** | **3,172** |
| incl. tests/docs/website | 1,292 | 165,968 | 4,768 |

107 files exist only in the fork (64 of them Python modules, the rest TypeScript, scripts and
plugin config).

## What is still needed — by feature

Each entry: what it is, what carries it, the evidence it is still live, verdict.

### 1. Warm-tier memory and extraction — **KEEP the pipeline; the surface is already retired**
`tools/memory_extraction/{extractor,prompts,buffer,conflict,orphan_sweep}.py`,
`tools/memory_auto_feedback/audit.py`, `agent/hot_tier_audit.py`,
`agent/fork/{memory_recall,memory_session_pin}.py`, `hermes_cli/memory_confirm.py`.
The warm **tool surface** is already withdrawn when a provider is registered:
`_withdraw_warm_tool_surface()` narrows the schema to `tier: ["hot"]` and drops the warm-only
parameters, and `_get_warm_store_or_error()` refuses warm actions at runtime ("Warm-tier actions are
disabled because memory.provider is set to …"). That is deliberate: both surfaces open the same
`memory_store.db`, so the model would be given two names for one fact.
What remains live is the **write** path — extraction still applies proposals through
`conflict.apply_verdict()`, and the holographic plugin has no equivalent (its store exposes
`add_fact`/`add_fact_status`, no duplicate/refinement/contradiction classifier). Keep that layer;
only the surface was ever redundant.

### 2. Turn-close contract — **KEEP end_turn; promise_gate retired**
`agent/end_turn.py`, `agent/turn_final_response_contract.py`.
`promise_gate` was the predecessor, not an equal: `turn_final_response.py` ran it only when the
contract was off (`_promise_kind = None if _contract else gate_kind(...)`), and `turn_contract: true`
in every profile, so it never fired. The code carries the 2026-10-25 consult verdict that a
50 %-precision prose nudger must not run beside the structural rule. **Deleted 2026-11-05** (332
lines + its test); `end_turn.py`'s docstring now records it as the retired third generation.
end_turn costs one tool call per turn and exists because DSv4.1-flash announces work without doing
it — keep while that model is the main one.

### 3. Delegation personas, roles and routing — **KEEP, and not upstream's "personas"**
`hermes_cli/personas.py` (thin runtime wrapper), `hermes_cli/persona_library.py` (the canonical
discovery, ported from the retired hermes-swarm), `hermes_cli/model_tiers.py`,
`hermes_cli/delegation_stats.py`, `tools/personas_sync.py`, `tools/delegation_router.py`.
Live: `delegation.model_by_role`, `delegation.roles` (incl. `consultant: max`), 8 import sites in the
delegate tools.
Upstream's persona is a **different feature**: `hermes_cli/personality.py` is prompt-overlay
personalities (`display.personality`, `agent.personalities`). Upstream has no
`model_by_role`/`persona_file`/`personas/` anywhere, and upstream's `delegate_task` `role` argument
is the orchestrator/leaf depth role, not a model-tier assignment.

### 4. Cross-session agent messaging — **KEEP**
`tools/cross_session_transport.py` (1,405 lines), `tools/cross_session_integration.py`,
`tools/cross_session_tool.py`, `tools/agent_messaging_{transport_a,contract,tools}.py`,
`gateway/agent_messaging_bridge.py`, `hermes_cli/agents_inbox.py`, `hermes_cli/subcommands/agents.py`.
Live: `cross_session` in config; exercised by `list_agents` / `send_agent_message`.
Upstream has **no messaging transport**: searching `v0.23.0` and `main` for
`send_agent_message`/`list_agents`/`agent_messaging`/`inbox`/`mailbox` returns only ACP protocol
messages and a `crossSession` flag on *spawn-tree listing*
(`SpawnTreeListParams.cross_session?: boolean`). The fork's `cross_session_registry` in `state.db` is
also what the owner's own `session-awareness` plugin reads.

### 5. Provider adapters and failover — **KEEP**
`agent/anthropic_adapter_kwargs.py`, `agent/anthropic_adapter_tool_blocks.py` (extracted phases of
`anthropic_adapter` — upstream still keeps them inline), `agent/fork/anthropic_recovery.py`,
`agent/exo_canonical_serializer.py`, `plugins/model-providers/exo/`, `agent/failover_state.py`,
`agent/usage_preflight.py`, `agent/empty_fallback_restore.py`,
`hermes_cli/config_{aux_provider_first,defaults_aux}.py`, `agent/fork/rate_limit_tracker.py`.
`agent/fork/rate_limit_tracker.py` is an **extension** of upstream's `agent/rate_limit_tracker.py`
(it imports `RateLimitState`, `parse_rate_limit_headers`, `format_rate_limit_compact`) — not a
shadow copy; keep both.

### 6. Consult (second opinion) — **KEEP**
`tools/consult_tool.py`, `agent/fork/consult_nudge.py`. Live: `auxiliary.consult` and the `consult`
entry in the tools list; used heavily in this session.

### 7. CLI chrome and host niceties — **KEEP**
`hermes_cli/fork_banner.py` (fork branding + git state; upstream's `banner.py` forwards to
`fork_banner.skin_branding`), `hermes_cli/portable_glyphs.py`, `hermes_cli/power_qos.py`,
`hermes_cli/mcp_gateway.py`, `hermes_cli/subcommands/agents.py`.

### 8. Behavioural guards — **KEEP, but the four oldest are worth a look**
`agent/fork/{silence_guard,skill_recall,stream_recovery,generation_stats,diagnostics}.py`,
`tools/content_filter_scrub.py`. All wired (`agent_init`, `turn_context`, `turn_finalizer`,
`chat_completion_helpers`, `turn_usage`). `consult_nudge`, `skill_recall`, `stream_recovery` and
`content_filter_scrub` have **one commit each, last touched 2026-07-18** — five months old, still
imported. They cost ~400 lines together; keep unless the models they were written for are gone.

### 9. Platform adapter details — **KEEP**
`plugins/platforms/feishu/feishu_audio.py` (the `duration` field opus uploads need),
`plugins/platforms/slack/adapter_rich_text.py` (Slack `rich_text` → readable lines).

### 10. Fork maintenance scripts — **KEEP**
`scripts/autostash_cleanup.py` (called by `hermes_cli/update_cmd_common.py`),
`scripts/check-unspecced-sdk-mocks.py` (`.github/workflows/lint.yml`),
`scripts/sync-fork-branding.py` (post-merge branding sync).

### 11. Desktop additions — **KEEP**
146 files: pet voice, plan gauge, power animations, tab scroll memory, session pane visibility,
perf capture. All covered by the `ui` project's 13,240 passing tests.

## Already removed

### Retired by this review (2026-11-05)

| item | why |
|---|---|
| `agent/promise_gate.py` + `tests/test_promise_gate.py` | inert under `turn_contract: true`; superseded by `end_turn` (332 lines) |
| 16 × `apps/desktop/scripts/diag-*.mjs`, `scroll-harness.html`, `src/dev/scroll-harness.tsx` | investigation scaffolding, zero referrers outside the git index |
| `FORK_md_audit_2026_08_18.md`, `apps/desktop/GAPS.md` | superseded artifacts |
| `scripts/hermes_token_check.py` + its test | manual diagnostic, referenced only by its own test |
| `.merge-v0.22.5.log` (repo root) | merge log committed by accident |

### Retired in favour of upstream during the v0.23.0 sync

1. **Desktop stream throttle** — the fork's 2026-10-30 deletion (`7c61589695e`) was reversed;
   upstream's model is in place, with all five `streamThrottle.register(win)` sites restored.
   Tripwire: pack draw with the window occluded.
2. **Auxiliary task slot lists** — `web_extract` and `skills_hub` dropped from the fork's three
   lists; upstream retired both tools.
3. **Compression feasibility** — upstream's route-aware `lazy_compression_feasibility_check`, with
   the fork's `lifecycle.complete(force_terminal=True)` folded in behind it.
4. **Checkpoint staging** — upstream's unstage + oversize-preflight ordering; the fork's unreadable
   path retry folded into `_add_all()` behind `_stage_all_args`.
5. **`.github/workflows/tests.yml`** — upstream's three-slice split, plus the fork-repo `if:` guard.
6. **CLI class hierarchy** — upstream's `CLIBacktrackMixin` leads the `HermesCLI` MRO; the fork's
   notifier reaper kept.

## What this means for the next sync

Upstreaming is no longer a lever. The remaining options, in order of value:

1. **Keep the divergence tested locally.** The expensive part of a sync is not the merge, it is
   proving the merged tree: this round needed a 1,911-file per-file pass plus a 17-directory sweep,
   and both were dominated by two environment traps (`commit.gpgsign` + `env -i`; the `/tmp` tmpfs
   quota), now documented in the `hermes-agent-fork-sync` skill.
2. **Shrink the divergence where it is accidental, not behavioural.** The fork's extracted phases
   (`anthropic_adapter_{kwargs,tool_blocks}.py`) are pure decomposition; if upstream ever refactors
   the same function, deleting the fork's split is free.
3. **Watch the four July guards.** They were written for models that may no longer be in use; each is
   small, but they are the likeliest "nobody remembers why this is here" set.
