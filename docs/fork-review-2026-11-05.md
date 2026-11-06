# Fork divergence review — adurham/hermes-agent @ v0.23.0

*2026-11-05. Scope: every change the fork carries that upstream does not, measured against
upstream tag `v0.23.0` (2bd378ed1b8d), immediately after the v0.23.0 sync landed on `main`.*

## Bottom line

- **Nothing the fork adds has been absorbed by upstream.** Upstream v0.23.0 contains **zero**
  files matching any of the fork's signature features: `end_turn`, `consult`, `hot_tier`,
  `memory_extraction`, `cross_session`, `promise`, `usage_preflight`, `failover_state`,
  `warm_tier`, `silence`, `skill_recall`, `generation_stats`.
- **Nothing is dead.** All 64 fork-added Python modules have at least one non-test referrer, and 11
  of them are named in the live `~/.hermes/config.yaml` (memory extraction, auto-feedback,
  delegation personas/roles, cross-session messaging, consult, orphan sweep).
- The only "already removed" items are the **six upstream-first reversals made during this sync**
  (listed below) — those are the places where the fork's own implementation was retired in favour
  of upstream's.
- The real removable weight is **scaffolding, not features**: five desktop diagnostic scripts, a
  scroll harness, and three root-level artifacts (~30 KB, no runtime referrers).

## Method

1. `git diff --numstat v0.23.0..main` — the fork's whole delta.
2. Fork-added files (`--diff-filter=A`) classified by kind; each Python module's docstring read for
   purpose, then `grep -rl` for non-test referrers to separate live code from orphaned code.
3. Upstream v0.23.0 tree searched by concept for an equivalent implementation of each fork feature.
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

Each entry: what it is, the modules that carry it, evidence it is still live, verdict.

### 1. Warm-tier memory, extraction and audit — **KEEP**
`tools/memory_warm.py`, `tools/memory_extraction/{extractor,prompts,buffer,conflict,orphan_sweep}.py`,
`tools/memory_auto_feedback/audit.py`, `agent/hot_tier_audit.py`, `agent/fork/memory_recall.py`,
`agent/fork/memory_session_pin.py`, `hermes_cli/memory_confirm.py` — ~4,300 lines.
Live: `memory.auto_feedback: true`, `auxiliary.memory_extraction`, `orphan_sweep` in config; the
extractor is called from `run_agent.py`; `memory_warm` has 8 non-test referrers. Upstream has no
warm tier at all. Last touched 2026-10-17 → 2026-10-29.

### 2. Delegation personas, roles and routing — **KEEP**
`hermes_cli/personas.py` (thin runtime wrapper), `hermes_cli/persona_library.py` (the canonical
discovery, ported from the retired hermes-swarm), `hermes_cli/model_tiers.py`,
`hermes_cli/delegation_stats.py`, `tools/personas_sync.py`, `tools/delegation_router.py`.
`personas.py`/`persona_library.py` are not duplicates: the former is a documented wrapper that adds
config I/O over the latter. Live: `delegation.model_by_role`, `delegation.roles` (incl.
`consultant: max`), `delegate_tool` imports `get_role_model_map`/`lookup_agent` in 8 places.
Upstream's `hermes_cli/personality.py` and the desktop `delegation-route-*` settings are unrelated
(the desktop ones are UI for route drafts, not model-tier assignment).

### 3. Cross-session agent messaging — **KEEP**
`tools/cross_session_transport.py` (1,405 lines), `tools/cross_session_integration.py`,
`tools/cross_session_tool.py`, `tools/agent_messaging_{transport_a,contract,tools}.py`,
`gateway/agent_messaging_bridge.py`, `hermes_cli/agents_inbox.py`, `hermes_cli/subcommands/agents.py`.
Live: `cross_session` block in config; the inbound/outbound paths are exercised by the `list_agents`
and `send_agent_message` tools this session uses. Upstream: no cross-session transport.

### 4. Turn-close discipline — **KEEP**
`agent/end_turn.py`, `agent/turn_final_response_contract.py`, `agent/promise_gate.py` — 1,113 lines.
The newest fork work (2026-10-25 → 11-04). Wired into `turn_final_response.py`, `mcp_tool_agent.py`
and `config_defaults.py`. Upstream has no equivalent.

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
`hermes_cli/fork_banner.py` (fork branding + git state; upstream's `banner.py` calls into it),
`hermes_cli/portable_glyphs.py`, `hermes_cli/power_qos.py` (touched today — the E-core/power work),
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
`scripts/sync-fork-branding.py` (post-merge branding sync), `scripts/hermes_token_check.py`
(manual auth diagnostic, only referenced by its own test).

### 11. Desktop additions — **KEEP**
146 files: pet voice, plan gauge, power animations, tab scroll memory, session pane visibility,
perf capture. All covered by the `ui` project's 13,240 passing tests.

## Already removed / retired in favour of upstream (this sync)

These are the fork implementations that **no longer exist** because the v0.23.0 merge adopted
upstream's version instead. Each has a revert recipe in `FORK.md`.

1. **Desktop stream throttle** — the fork's 2026-10-30 deletion (`7c61589695e`) of the subsystem was
   reversed; upstream's v0.23.0 model is in place (and the five
   `streamThrottle.register(win)` call sites restored). Tripwire: pack draw with an occluded window.
2. **Auxiliary task slot lists** — `web_extract` and `skills_hub` dropped from the fork's three
   lists; upstream retired both tools.
3. **Compression feasibility** — upstream's route-aware `lazy_compression_feasibility_check` is in
   place, with the fork's `lifecycle.complete(force_terminal=True)` folded in behind it.
4. **Checkpoint staging** — upstream's unstage + oversize-preflight ordering; the fork's unreadable
   path retry folded into `_add_all()` behind `_stage_all_args`.
5. **`.github/workflows/tests.yml`** — upstream's three-slice split, plus the fork-repo `if:` guard.
6. **CLI class hierarchy** — upstream's `CLIBacktrackMixin` leads the `HermesCLI` MRO; the fork's
   notifier reaper kept.

## Removal candidates (small, mechanical)

| item | why | size |
|---|---|---:|
| `apps/desktop/scripts/diag-scroll-{bottom-return,deep,tabs,yank}.mjs`, `diag-transcript-row-height.mjs` | one-off probes from the Oct 29–30 scroll investigation; zero referrers outside the git index | ~5 files |
| `apps/desktop/scroll-harness.html`, `apps/desktop/src/dev/scroll-harness.tsx` | the harness those probes drove; not referenced by the app, the build or any test | 2 files |
| `.merge-v0.22.5.log` (repo root) | a merge log accidentally committed | 19 KB |
| `FORK_md_audit_2026_08_18.md` (repo root) | one-off audit of FORK.md; superseded by the file itself | 9.9 KB |
| `apps/desktop/GAPS.md` | historical gap list, not linked from anything | 9.7 KB |
| `scripts/hermes_token_check.py` | manual diagnostic, referenced only by its own test | 247 lines |

Nothing in this table is loaded at runtime. Deleting them shrinks the fork without touching a
feature; the desktop harness is the only one with any chance of being wanted again (it was the tool
that found the scroll-position bugs).

## What this means for the next sync

The de-fork lever is not deletion — upstream has not caught up with any of the fork's features, so
removing them would remove capability, not divergence. The lever is the reverse direction:
**upstreaming the fork's generic fixes** so the next merge has fewer conflicts. The candidates that
are clearly generic (nothing fork-specific in them) are:

- the feishu SDK-name drift guard (`tests/gateway/test_feishu_sdk_name_binding.py`) — it exists
  because upstream's own `globals().update(...)` import block silently desynchronises;
- the prompt-surface fixture re-render (fork-only fixtures, but the failure mode is upstream's);
- the `_apply_claude_code_identity` / `_oauth_wire_namer` extraction into
  `agent/anthropic_adapter_{kwargs,tool_blocks}.py` — pure decomposition, no behaviour change.

Everything else in the fork is a deliberate behavioural divergence and should stay.
