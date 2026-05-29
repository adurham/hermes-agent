# Fork notes — adurham/hermes-agent

This is a personal fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
Code here is **not intended for upstream contribution.** See "Why a fork" below.

## What's different from upstream

### Hard-fork boundaries (zero merge conflicts ever)

These files/directories don't exist upstream and never will. Upstream merges
will never touch them.

| Path | Purpose |
|---|---|
| `agent/fork/__init__.py` | Marker module for fork-only code |
| `agent/fork/skill_recall.py` | Skill-recall reminder — tracks loaded skills + nudges agent to re-check `skill_pitfalls()` before destructive ops |
| `agent/fork/memory_recall.py` | Memory-recall reminder — nudges agent to call `memory(action='recall', ...)` against the warm-tier store every N tool calls (or on explicit "remember"-style directives); auto mode runs the recall and injects the top hit. Config: `agent.memory.recall_reminder_*`. |
| `agent/fork/memory_session_pin.py` | Session-pin — keeps selected warm-tier facts visible in the system prompt for the rest of the current session (gone on restart). Exposes `memory(action='pin'/'unpin'/'pinned', fact_id=N)`. Config: `agent.memory.session_pin_max_count`/`max_chars`. |
| `agent/fork/rate_limit_tracker.py` | Rate-limit observability — one-shot INFO on first header capture, WARN on 90% bucket transitions with 80% hysteresis |
| `agent/fork/anthropic_recovery.py` | Refusal retry sanitization (strip credential-extraction shell patterns from historical context) + CC alias arg translation + `is_anthropic_refusal` detection predicate (T2.3) |
| `agent/fork/anthropic_messages.py` | The fork's ~540-line `convert_messages_to_anthropic` OpenAI→Anthropic converter (T2.2). Moved out of `anthropic_adapter.py` so upstream's converter refactors can't tangle with it. |
| `agent/fork/stream_recovery.py` | Cold-start stale-timeout computation (`effective_stale_timeout`) — the fork's grace window before the first stream event (T2.3). |
| `agent/fork/tool_search_lazy.py` | Client-side lazy MCP tool loading — name-only stubs inflated to full schemas on demand |
| `agent/fork/diagnostics.py` | Per-turn usage history + tools-signature hash + xAI 403 entitlement hint |
| `hermes_cli/fork_banner.py` | The fork's banner branding + git-state subsystem (carried/upstream-behind line, fork-aware agent name, HEAD-date label, fork-tree release URLs) (T2.5). Moved out of `banner.py`. |
| `FORK.md` | This file |
| `scripts/fork-merge-plan.py` | Pre-merge analyzer (see "Future upstream merges" below) |
| `scripts/setup-merge-drivers.sh` | One-time-per-clone registration of the uv.lock merge driver |

### Soft-fork edits (merge conflicts possible)

These are upstream files we've modified. Fork divergence vs `upstream/main`:

After the Tier-2 refactors (2026-05), several of these shrank: the biggest
inline blocks moved into hard-fork modules (see table above), leaving thin
forwarders. The conflict surface on these files is now mostly forwarder lines.

| File | Adds / Dels | Why |
|---|---|---|
| `agent/anthropic_adapter.py` | +1922 / -59 | Claude Code OAuth mimicry (wire format, betas, metadata, 1M-context gate). This is the headline fork feature and is intentionally never going upstream. **T2.2**: the 540-line `convert_messages_to_anthropic` converter moved to `agent/fork/anthropic_messages.py` (thin forwarder remains). |
| `agent/chat_completion_helpers.py` | +780 / -124 | Streaming reliability: SDK monkey-patch hook for SSE events, heartbeat ticks, stream-drop reconnect, cold-start detection. **T2.3**: cold-start stale-timeout calc moved to `agent/fork/stream_recovery.py`. |
| `agent/conversation_loop.py` | +330 / -7 | Per-turn callouts to fork modules (rate-limit capture, usage history, refusal handler). **T2.3**: refusal *detection* moved to `agent/fork/anthropic_recovery.is_anthropic_refusal`; the recovery ladder stays inline (control-flow-coupled). |
| `hermes_cli/banner.py` | (reduced by T2.5) | Branding + git-state subsystem moved to `hermes_cli/fork_banner.py`; banner.py keeps thin forwarders + the patchable git plumbing/caches. |
| `hermes_state.py` | (reduced by T2.1/T2.4) | Fork-only `api_calls` table → `FORK_SCHEMA_SQL`; fork column `anthropic_content_blocks` → `FORK_TABLE_COLUMNS` (reconciler-added). SCHEMA_SQL is now pure-upstream shape. |
| `run_agent.py` | +234 / -24 | 12 forwarder methods (now extracted to `ForkForwardersMixin`), `_classify_anthropic_stream_phase` top-level function, fork-state initialization. |
| `agent/agent_init.py` | +122 / -13 | Fork instance state initialization (delegated to `fork.<module>.init_state(agent)` where possible). |
| `agent/agent_runtime_helpers.py` | +119 / -29 | Scattered port additions during the 2026-05-19 upstream merge — mostly CC alias support in `repair_tool_call`, switch_model 1M-beta latch re-eval, swarm_run handling in `invoke_tool`. |
| `agent/tool_executor.py` | +111 / -29 | Skill-recall hook callsites (`_record_loaded_skill`, `_maybe_skill_recall_hint`) in both sequential + concurrent paths, plus hermes_load_tools and swarm_run dispatch. |
| `agent/system_prompt.py` | +17 / -23 | Date-only timestamp restored (upstream's prompt-cache fix), grok added to OPENAI_MODEL_EXECUTION_GUIDANCE gate. |

Plus 165 commits of fork-only history. See `git log upstream/main..main`.

## Why a fork

Adam closed PR #25234 upstream in early 2026 — it included ~28K LOC of fork
divergence framed as a single bugfix, which was visible and embarrassing.
Lesson learned: anything that lives on this fork stays here, even when it
looks generally useful.

Specific things that **must never** be sent upstream:

* Claude Code OAuth mimicry (`anthropic_adapter.py`)
* `_decorate_xai_entitlement_error` (xAI billing hint UX)
* Anything in `agent/fork/`

If a fork feature later seems genuinely upstream-worthy, file a separate
clean PR built from upstream's tree, not a backport of fork code.

## Future upstream merges

**Cadence is the #1 conflict lever.** Conflict count scales with drift, measured:
a sync at ~715 commits behind produced 20 conflicts; the next sync at 134 produced
5. Merge little and often. A weekly cron (`~/.hermes/scripts/upstream_drift_check.sh`,
job "hermes-agent upstream drift digest") fetches upstream over HTTPS and pings when
drift/conflicts appear — but acting on it is manual.

Per merge:

```bash
git fetch upstream && git checkout -b sync/upstream-$(date +%F)
python scripts/fork-merge-plan.py    # predicts conflict files before you touch anything
git merge upstream/main
```

Work on a `sync/upstream-*` branch (never merge directly to `main`), resolve,
run tests, push the branch, review, then merge to `main`.

### One-time per clone

```bash
./scripts/setup-merge-drivers.sh   # registers the uv.lock "ours-then-regen" driver
```

After this, `uv.lock` conflicts auto-resolve (keep ours, run `uv lock` to reconcile
against the merged `pyproject.toml`). Without it, `uv.lock` conflicts every merge —
just take either side and run `uv lock`.

### Conflict guidance by file (refresh after each sync; line numbers drift)

* `agent/fork/*` + `hermes_cli/fork_banner.py` — **never conflicts.** This is the
  goal pattern: fork logic lives in its own modules, hooked into upstream files via
  thin forwarders. Proven: across two syncs, these had zero conflicts. The Tier-2
  refactors (2026-05) moved the worst inline offenders here — see below.
* `uv.lock` — handled by the merge driver (see above). No manual work.
* `hermes_state.py` — **mostly defused by Tier-2.** Remaining: `SCHEMA_VERSION` —
  both sides bump it, pick `max(both) + 1`. NOTE: `_reconcile_columns()` runs
  unconditionally on boot and ALTER-ADDs any column in `SCHEMA_SQL` OR
  `FORK_TABLE_COLUMNS` that's missing live, and tables use `CREATE TABLE IF NOT
  EXISTS` — so the version bump only gates *destructive* migrations.
  - **T2.1**: fork-only tables (`api_calls`) now live in `FORK_SCHEMA_SQL` (executed
    after `SCHEMA_SQL` at both call sites), NOT inline in `SCHEMA_SQL`. No more
    positional collision with upstream table additions.
  - **T2.4**: the fork column `anthropic_content_blocks` now lives in
    `FORK_TABLE_COLUMNS` (reconciler ALTER-ADDs it), NOT in the messages CREATE
    TABLE. SCHEMA_SQL's messages table is pure-upstream shape.
  - Residual (accepted): the `append_message` INSERT/VALUES/param + multi-session
    SELECT still carry `anthropic_content_blocks` interleaved with upstream columns.
    These are additive "keep-both" merges (overriding the whole method would be a
    bigger liability). Consumer reads BY NAME (`row["col"]`) so column order is safe.
* `agent/anthropic_adapter.py` — **converter defused by T2.2.** The ~540-line
  `convert_messages_to_anthropic` (vs upstream's ~63) now lives in
  `agent/fork/anthropic_messages.py`; the adapter has a 2-line forwarder. Upstream's
  extract-method refactors of its own converter can no longer tangle with it — on
  conflict, take-ours on the forwarder. The block/tool/content helpers stay in the
  adapter (some upstream-shared); the fork converter binds them via a lazy
  `from agent import anthropic_adapter` import (also breaks the circular dep).
  Still take "ours" for OAuth-path edits. Tool naming: the fork DELIBERATELY does
  NOT prepend `mcp_` to bare tool names (registers MCP tools as `mcp__server__tool`);
  upstream re-adds single-underscore prefixing every few syncs — take ours, drop
  upstream's prefix loop + its 2 outgoing-prefix tests.
* `agent/conversation_loop.py` + `agent/chat_completion_helpers.py` — **partially
  defused by T2.3.** Refusal detection is now `agent._is_anthropic_refusal()`
  (forwarder → `agent/fork/anthropic_recovery.is_anthropic_refusal`); the cold-start
  stale-timeout is `agent/fork/stream_recovery.effective_stale_timeout`. Residual
  (accepted, control-flow-coupled): the refusal-recovery LADDER (fallback → sanitize
  → giveup, with `continue`/`return`/loop-var resets) and the stale-kill counters
  stay inline — moving control flow out of a retry loop is riskier than the conflict
  it saves. On conflict: take "ours", verify loop vars (`retry_count`,
  `compression_attempts`, `primary_recovery_attempted`) still reset; keep BOTH
  recovery blocks (cache-strip-on-overload vs multimodal-tool-content).
* `agent/credential_pool.py` — `_seed_from_singletons` auth seeding. Keep the fork's
  keychain-longlived precedence; nest upstream's api-key-path pruning inside the
  fork's `if not longlived_token:` block. The pruning predicate uses
  `is_borrowed_credential_source()` — verify `keychain_longlived` stays kept-while-active.
* `agent/agent_runtime_helpers.py` (`switch_model`) — keep the fork's 1M-beta latch +
  `drop_context_1m_beta=` param; integrate into upstream's try/except-rollback +
  MiniMax-OAuth structure.
* `run_agent.py` — import unions (keep fork's `Set`/`Tuple`/`ForkForwardersMixin`).
  `_sync_external_memory_for_turn`: upstream threads a `messages=` kwarg into
  `sync_all` — keep that threading; the fork's separate `memory_extraction.on_turn_end`
  Phase-2 hook is independent, keep it too.
* `hermes_cli/banner.py` — upstream rewrites this file periodically. The fork's helper
  `_skin_branding` and `_resolve_agent_name` get DROPPED by auto-merge while their
  callers survive → latent runtime crash. After any banner merge, grep for
  `def _skin_branding` and `def _resolve_agent_name`; if missing, restore from the
  prior fork commit. The rich `get_git_banner_state` schema
  (`{local,origin,upstream,carried,upstream_behind}`) is fork-only — keep it, fold
  upstream's Docker build-SHA fallback into it.
* `cli.py` — additions near `kb = KeyBindings()` collide (fork's cancel-ladder vs
  upstream's keybindings). Keep BOTH blocks. Tool-count/status logic: keep fork's
  `disabled_toolsets` arg + upstream's defer logic.
* `.gitignore` / docstrings / comments — incidental collisions from edits near fork
  changes. Keep both / take either. Keep fork edits surgical (don't reformat upstream
  lines near your changes) to avoid these.

### After every merge — run the real blast radius, not just changed files

Tests catch defects auto-merge introduces in files that DIDN'T conflict (this bit us
twice: a dropped `_skin_branding`, a missing `messages=` thread). Minimum:

```bash
python -m pytest tests/agent/ tests/run_agent/ -o 'addopts=' -q --timeout=90
python -c "import cli, run_agent, hermes_state, hermes_cli.banner"   # boot smoke
```

Known pre-existing flake (NOT merge-caused): `auxiliary_client` provider/vision tests
(`test_vision_routing_31179.py`, `test_provider_parity.py::...openrouter_always_wins`,
`test_auxiliary_main_first.py`) fail only under full-suite ordering (global-state
pollution), pass in isolation. Deselect them when judging a merge.

## Tests

The fork adds these test files:

* `tests/test_skill_recall_reminder.py` (14 tests, fork-only feature)
* `tests/test_memory_recall_reminder.py` (20 tests, fork-only feature)
* `tests/test_memory_session_pin.py` (18 tests, fork-only feature)
* `tests/run_agent/test_rate_limit_observability.py` (6 tests, fork-only feature)
* `tests/run_agent/test_anthropic_stream_phase_classifier.py` (16 tests, exercises `_classify_anthropic_stream_phase`)
* `tests/run_agent/test_repair_tool_call_name.py` (CC alias coverage)

All other tests come from upstream.

## When to update this doc

* New fork feature lands → add to the "Hard-fork boundaries" table.
* Upstream merge changes the file-level divergence numbers significantly →
  update "Soft-fork edits" numbers.
* The "Why a fork" rules change → update them, but always document the reason.

Don't let this file go stale. If `git log --oneline | head -20` shows fork
commits but FORK.md doesn't reflect them, fix that.
