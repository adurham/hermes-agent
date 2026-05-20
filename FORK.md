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
| `agent/fork/rate_limit_tracker.py` | Rate-limit observability — one-shot INFO on first header capture, WARN on 90% bucket transitions with 80% hysteresis |
| `agent/fork/anthropic_recovery.py` | Refusal retry sanitization (strip credential-extraction shell patterns from historical context) + CC alias arg translation |
| `agent/fork/tool_search_lazy.py` | Client-side lazy MCP tool loading — name-only stubs inflated to full schemas on demand |
| `agent/fork/diagnostics.py` | Per-turn usage history + tools-signature hash + xAI 403 entitlement hint |
| `FORK.md` | This file |
| `scripts/fork-merge-plan.py` | Pre-merge analyzer (see "Future upstream merges" below) |

### Soft-fork edits (merge conflicts possible)

These are upstream files we've modified. Fork divergence vs `upstream/main`:

| File | Adds / Dels | Why |
|---|---|---|
| `agent/anthropic_adapter.py` | +1922 / -59 | Claude Code OAuth mimicry (wire format, betas, metadata, 1M-context gate). This is the headline fork feature and is intentionally never going upstream. |
| `agent/chat_completion_helpers.py` | +780 / -124 | Streaming reliability: SDK monkey-patch hook for SSE events, heartbeat ticks, stream-drop reconnect, cold-start detection. Currently a wholesale-replaced `interruptible_streaming_api_call`. |
| `agent/conversation_loop.py` | +330 / -7 | Per-turn callouts to fork modules (rate-limit capture, usage history, refusal handler), plus the refusal handler itself which is wedged in mid-control-flow. |
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

Weekly cadence — `git fetch upstream && git merge upstream/main` into `main`.
Before merging, run:

```bash
python scripts/fork-merge-plan.py
```

Predicts which files will conflict + where, based on overlap between upstream's
new commits and fork's known divergent regions. Lets you see merge friction
before you touch anything.

When conflicts do happen:

* `agent/fork/*` — never conflicts (upstream doesn't know these exist).
* `run_agent.py` — divergence is concentrated in the `ForkForwardersMixin`
  (which is in `agent/fork/_mixin.py`) plus 4-line forwarders that import
  from `agent/fork/`. These either auto-merge or resolve trivially as
  "take ours".
* `agent/anthropic_adapter.py` — biggest pain. Take "ours" for anything that
  touches the OAuth path; review carefully for anything that touches the
  base-url normalization, beta-header gating, or client construction (where
  upstream + fork both work).
* `agent/conversation_loop.py` — the refusal handler at line ~1273-1340 is
  the most likely conflict source. It's a 67-line block fork-only. Take
  "ours" and verify the surrounding loop variables (`retry_count`,
  `compression_attempts`, `primary_recovery_attempted`) still get reset
  correctly on the refusal-recovery paths.

## Tests

The fork adds these test files:

* `tests/test_skill_recall_reminder.py` (14 tests, fork-only feature)
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
