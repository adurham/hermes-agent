# Round-4 upstream-absorption audit (2026-10-13, tag v0.21.7) — record + disposition log

**Context.** De-fork program, round 4 ("spoon", post-v0.21.7 sync). Hypothesis under test (owner):
*"if we don't NEED it in the fork and it's not a feature we're using then we should de-fork it."*
Method: the 47 surviving fork bug-fix rows from round 3 were sliced into 4 read-only worklists
(`defork4/BRIEF4.md`, `defork4/worklist.jsonl`) and re-adjudicated against the merged tree AND
upstream **v0.21.7** by 4 read-only units on DSv4.1-Flash (`deleg_e23fcf52`; per-row evidence =
`file:line` on both sides, logged to `defork4/U{1..4}.log.jsonl` as they went), then re-verified by
the parent.

**Verdict: same as round 3 — the 777-commit delta absorbed NOTHING new.** Recorded counts across
the 47 rows: **44 KEEP-OK · 1 FIX-MISSING · 2 STALE no-ops (both already-retired rows; verified
no-action) · 0 retirement candidates.** Row 6's FIX-MISSING was the only actionable item.

## The one FIX-MISSING (row 6 — holographic/FTS test failures) — EXECUTED 2026-10-13

The v0.21.7 sync deleted the in-tree holographic provider (`plugins/memory/holographic/` is now a
catalog plugin, `itpartypattaya/hermes-plugin-holographic`, pinned at v0.6.1 in
`plugin-catalog/holographic.yaml`), but `tools/memory_warm.py` still imported it. Executed per the
owner ruling ("upstream moved it to a plugin, so we should follow suit"):

- `tools/memory_warm.py` resolves the provider through `plugins.memory.import_provider_module`
  (store + retrieval) — the same loader seam every host-side caller uses
  (`gateway/run_profile_reconcile.py`); proven on a synthetic `HERMES_HOME` with the catalog plugin
  installed.
- `tests/tools/test_memory_warm_holographic_migration.py` reworked to the seam +
  `pytest.skip(allow_module_level=True)` when the provider is absent + the catalog tokenizer's
  shape (`("plat 15800" ...)`, glued-form negative control kept); `test_memory_warm.py`
  module-skips likewise; `test_memory_auto_feedback.py` / `test_memory_extraction.py` skip only
  their warm-store-backed classes (isolated suite environments install no catalog plugins).
- The warm-tier prefetch recall hook (`prefetch()` → `get_warm_store()._after_recall`) was ported
  into the plugin and published to the owner's fork: `adurham/hermes-plugin-holographic`, branch
  `fork/warm-accounting` (0c58cda) + tag `v0.6.1-warm-tier.1` on the catalog-pinned v0.6.1 base.
  Install/catalog wiring to the fork lands with the live update.

## Owner rulings executed with this round

- **W1** (watcher held-release): upstream's new child-notice pre-gate had made the fork's
  hold-then-deliver contract inert at the agent-notify watcher — resolved IN-TREE (drain-site
  `_EVENT_HELD_AT_KEY` exemption across consumers, two phase helpers; fork 47/47 + upstream
  128/128 green).
- **W2** (vision fix, `0285cf60c`): re-ported onto upstream's refactored resolver
  (`vision_model = resolved_model or provider_vision_default or main_model` in
  `_vision_main_provider_client`).
- **O1** (approval bell): aligned to upstream's default-off (`approvals.bell_on_prompt` /
  `notify_on_prompt` default False).
- **Route-model stance**: adopted upstream's `reasoning_replay_route` carrier model as shipped
  (the fork's pre-merge strip-for-anthropic/unknown stance retired with the adoption; accepted).

## Row verdicts (47)

Aggregate/batch rows are the plain-numbered entries; the rest are individual fixes.

| Row | Verdict | Item | Note (condensed from the unit logs) |
|-----|---------|------|-------------------------------------|
| 1 | KEEP-OK | agent_type routing (batch) | Tag has no agent_type routing, persona/stats modules or delegation_router.py. |
| 1 | KEEP-OK | aux-adapter timeout | Tag's `_AnthropicCompletionsAdapter.create()` never reads `kwargs["timeout"]` (Codex adapter only). |
| 1 | KEEP-OK | spinner padding (+ Δ status-bar) | Need live at the tag: upstream's KawaiiSpinner pads with `len()` so wide glyphs under-pad. |
| 1 | KEEP-OK | status-bar timer / approval | Merged keeps fork timer (no zero-pad, hour rollover) + codex map `{"timeout"...}`. |
| 2 | KEEP-OK | sweep-restored call sites (batch) | Delegation role resolution, swarm board callers etc. restored + wired. |
| 2 | KEEP-OK | interrupt tuple guard | Tag `interrupt()` has no tuple/list→str coercion; fork guard present + load-bearing. |
| 2 | KEEP-OK | pytest-in-argv0 guard | Need exists at tag: `-p randomly` still applied as a profile by the unguarded preflight. |
| 2 | KEEP-OK | beta-only kwargs | Merged prefers `.beta.messages` and strips `_BETA_ONLY_` kwargs correctly. |
| 3 | KEEP-OK | Transport A registration (batch) | Fork-only transport; merged wires its registration; tag has zero occurrences. |
| 3 | KEEP-OK | curator blocked-writes status | Tag never surfaces refused skill_manage writes in curator status. |
| 3 | KEEP-OK | merge-regression test fixes | 2 of 3 sub-items already at parity; remainder kept. |
| 3 | STALE | exclude-newer pin (no-op) | Both trees carry the relative 14-day pin; already retired in round 2. |
| 4 | KEEP-OK | gateway messaging bridge (batch) | Tag has no bridge file/in-process transport; merged ships it. |
| 4 | KEEP-OK | cached-client token freshness | Tag cache-hit branch returns cached client with no token-freshness check. |
| 4 | KEEP-OK | `_clean_pass_durations` | Permanent timeout loop for a large file with standing failures still at tag. |
| 4 | KEEP-OK | tab/sidebar drag | Merged relocates the workspace pane via `moveTreePane`; fork behavior load-bearing. |
| 5 | KEEP-OK | Anthropic OAuth-org 403 keywords (batch) | Tag `_PAYMENT_KEYWORDS` lacks the OAuth-org 403 family. |
| 5 | KEEP-OK | redirect pending wipe | Tag `interrupt()` wipes `_pending_redirect` with no fold; fork folds it. |
| 5 | KEEP-OK | host-shaped macOS test failures | Sub-item by sub-item on both trees: still applicable; fork fixes kept. |
| 5 | KEEP-OK | in-project reorder / single DndContext | Merged applies manual lane order after the activity sort. |
| 6 | KEEP-OK | `/reasoning` picker (batch) | Tag's bare `/reasoning` prints state only; merged restores the picker. |
| 6 | **FIX-MISSING** | holographic/FTS test failures | The seam restore — **EXECUTED** (see above). |
| 6 | KEEP-OK | empty-response policy / fallback restore | All three halves absent upstream at the tag, present + wired in merged. |
| 6 | KEEP-OK | working-pulse indicators | Stale-busy half converged; remaining halves fork-load-bearing. |
| 7 | KEEP-OK | stale reasoning charge (batch) | Tag still charges stale reasoning on aggregator hosts. |
| 7 | KEEP-OK | emoji VS-16 width | Tag has no emoji+VS-16 correction anywhere. |
| 7 | KEEP-OK | retry wall-clock parsing | Weekly-limit errors name a local wall-clock reopen time; fork grammar still needed. |
| 8 | KEEP-OK | timer-overflow sites (batch) | All four overflow sites still overflow at the tag. |
| 8 | KEEP-OK | MCP shutdown traceback | Tag still calls `cancel()` outside the try; class remains. |
| 8 | STALE | banner disabled-toolsets filter (no-op) | Already removed in round 2 (FORK.md retirement note). |
| 8 | KEEP-OK | board border / overflow trim | Swarm half retired; remaining trim halves kept. |
| 9 | KEEP-OK | exo-cluster overflow/cache-size (batch) | Tag has neither pattern; exo-scoped fixes kept. |
| 9 | KEEP-OK | provider-first aux schema bug | Schema is fork-only; the bug it fixes cannot exist upstream. |
| 9 | KEEP-OK | oversized-image 413 (+ ceiling) | Pillow-as-core-dep half superseded; proactive downscale + 413 shrink kept. |
| 9 | KEEP-OK | clarify timeout / attention | Timeout-shadowing half converged; remainder kept. |
| 10 | KEEP-OK | fallback model version gate (batch) | Tag's fallback 2.1.74 < the 2.1.251 per-model gate that 400s fable. |
| 10 | KEEP-OK | MCP prefix gate | Tag gate early-returns False unless `mcp__`; would serialize everything else. |
| 10 | KEEP-OK | adaptive-thinking temperature guard | Collision created by the fork's own documented adaptive-thinking default. |
| 10 | KEEP-OK | `_AUX_TASK_SLOTS` | Tag's 12-key subset omits slots its own DEFAULT_CONFIG defines. |
| 11 | KEEP-OK | completion hold gate (batch) | Upstream absorbed owner_task_id plumbing but has no hold gate; fork's is load-bearing. |
| 11 | KEEP-OK | placeholder-key 401 | Tag accepts any explicit_api_key verbatim (foreign placeholder passes through). |
| 11 | KEEP-OK | native 1M context opt-in | Fork-native opt-in + per-model gate effective in merged; absent at tag. |
| 11 | KEEP-OK | relaunch fixup on skip branch | Merged's unconditional desktop fixup kept. |
| 12 | KEEP-OK | agent_type dispatch (batch) | Tag has no `agent_type` setting at all. |
| 12 | KEEP-OK | aux-model test fixes | Items 1+3 already retired; item 2 remains useful. |
| 12 | KEEP-OK | DSv4-local reliability sweep | Five sub-items verified on both sides; kept. |
| 12 | KEEP-OK | aux pin stripped on save | The pollution stripper the fix repairs is fork-only. |

## Provenance

- Brief + worklist: `~/.hermes/cache/scratch/sync-v0.21.7/defork4/BRIEF4.md`, `worklist.jsonl`.
- Unit logs (append-as-they-went JSONL): `defork4/U{1..4}.log.jsonl` (+ `U3.g5/g12`, `U*.rows`).
- Consolidated gate: `defork4/DEFORK4_GATE.md` (this file's source; rounds the rows into the
  restore list + rulings).
- Parent spot-verification: FORK.md 2026-10-13 entries (sync close-out + follow-ups) carry the
  executed outcomes; suite evidence in the sync ledger.
