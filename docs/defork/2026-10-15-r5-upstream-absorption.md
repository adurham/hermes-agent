# Round-5 upstream-absorption audit (2026-10-15, tag v0.22.2) — record + disposition log

**Context.** De-fork program, round 5 (post-v0.22.2 sync). Standing owner bar: *"if we don't NEED it in
the fork and it's not a feature we're using then we should de-fork it."* Method (per the round-N+1
convention: re-verify, don't rediscover): the round-4 keep-set was rebuilt as a 52-row worklist —
round 4's 47 surviving fix rows **plus 5 new rows** (this sync's merge-defect restores and the newest
fork work: exo `generation_stats`, guard-kill raised-shape terminalization, silence guard, the
`_route_model_for_banner` restore, the memory hot-target re-default). Sliced into 4 read-only units
(`U{1..4}.jsonl`), re-adjudicated against the merged tree AND pristine upstream **v0.22.2**
(`d923a5eb487`) by 4 read-only units on DSv4.1-Flash (`deleg_c9f01b0d`, ~250s), then parent-checked.

**Verdict (52/52 rows):** **45 STILL-FORKONLY · 5 PARTIAL · 2 STALE · 0 UPSTREAMED · 0 retireable.**
Second consecutive zero-absorption round — the 2983-commit v0.22.2 delta absorbed none of the kept
fixes (as expected: they guard fork-only machinery upstream never had, or repair upstream code that is
STILL broken at the tag). **`survived_merge = yes` on every row whose files this sync touched** — no
silent merge losses on the keep set (rows 18, 22, 23, 28, 40, 48, 49, 50, 51, 52 explicitly re-verified
present AND wired).

## STALE (2 — no action, retirement bookkeeping only)

- **12** exclude-newer pin — fully retired this sync: both trees now carry upstream's
  `exclude-newer = "3 days"` with no exemption table and no fork-side reference.
- **30** banner disabled-toolsets filter — already retired in round 2; absent in both trees.

## PARTIAL (5 — fork remainder kept, upstream convergence noted)

- **11** merge-regression test fixes — 2 of 3 sub-items at parity at the tag; remainder is fork
  wire-shape assertions (kept). Round-4 unit logs were pruned; re-derived from FORK.md's v0.21.7
  merge-defect list.
- **19** host-shaped macOS test failures — upstream filed part of the family (#128793/#128794) at some
  point; remainder fork-only (kept).
- **20** in-project reorder — manual-order-after-activity-sort converged upstream ("Bug 2 SUPERSEDED");
  single-shared-`DndContext` lane reorder still fork-only.
- **24** working-pulse indicators — stale-busy half cleared upstream (desktop
  `use-background-sync.ts`); collapsed-group `WorkspaceWorkingDot` still fork-only.
- **44** agent_type dispatch — **the round's one real absorption:** v0.22.2 now ships
  `_credential_overrides` (`tools/delegate_tool.py`) + the lifecycle API (`agent/subagent_lifecycle.py`)
  consuming it. No conflict: the sync threads `routing_cfg` into the shared bundle for both surfaces
  (see the sync entry). The fork's routing stack (auto-route classifier, agent_type role-map,
  `delegation_router.py`) has zero occurrences at the tag — stays fork-only.

## Filing queue (17 — owner-gated; NEVER filed unasked; account-level PR filing was blocked 2026-10-10)

Fixes that repair UPSTREAM-OWNED code still broken at v0.22.2. Evidence per row in `U*.jsonl`; parent
spot-checked 36/29/15 live. Already-known artifacts: the fork's draft PR **#132439** covers 3 of row
28's sites; a community draft **#132442** exists for row 25.

| Row | Item | One-line evidence (tag side) |
|-----|------|------------------------------|
| 2 | aux-adapter timeout | `_AnthropicCompletionsAdapter.create()` never reads `kwargs["timeout"]` → SDK 900s default. |
| 3 | spinner padding | `KawaiiSpinner` pads with `len()` → wide glyphs under-erase; status-bar width fn uncorrected. |
| 6 | interrupt tuple guard | `interrupt()` has no tuple/list→str coercion while the tag's own CLI queues `(text, images)`. |
| 7 | pytest-in-argv0 guard | `-p randomly` is profile-shaped in `_apply_profile_override` → `sys.exit(1)` under pytest. |
| 14 | cached-client token freshness | `_get_cached_client` cache-hit returns stale-token aux client. |
| 15 | test duration cache | `run_tests_parallel.py` excludes ALL failures from the duration cache → permanent timeout loop. |
| 17 | OAuth-org 403 keywords | `_PAYMENT_KEYWORDS` lacks the Anthropic OAuth-org 403 family. |
| 18 | pending-redirect fold | `interrupt()` wipes `_pending_redirect` with no fold. |
| 19 | test fixes (partial) | `tests/hermes_cli/test_plugin_validate.py` still ships the broken exact-match assertion. |
| 25 | stale-reasoning estimate | No aggregator-host exclusion; stale reasoning charged on re-export routes (see #132442). |
| 26 | VS-16 width | No `display_cwidth` VS-16 correction → timer/status corruption. |
| 27 | wall-clock retry parsing | Only relative "resets in N" grammar; no wall-clock reopen parsing. |
| 28 | timer overflow | 4 unbounded sites (`turn_summary`, `runtime_footer`, `update_cmd_drain_report`, spinner). |
| 29 | MCP shutdown | `t.cancel()` before the `try` → "Event loop is closed" at shutdown GC. |
| 32 | exo KV-cache phrase | No exo KV-cache rejection pattern in `error_classifier`/`model_metadata`. |
| 36 | fallback UA gate | `_CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"` < the ≥2.1.251 gate that 400s fable-class. |
| 39 | `_AUX_TASK_SLOTS` | Tag's 12-key subset omits 7 keys its own `DEFAULT_CONFIG["auxiliary"]` defines. |

## Not filing (fork-specific or by-design upstream)

Rows 1/5/8/9/10/13/33/34/35/37/38/41/42/43/45/46/47/31 — fork-only features with no upstream bug
behind them (provider-first aux schema is fork-only by construction; upstream's `mcp__` gate is
correct for upstream's registration scheme; row 38's collision exists only because of the fork's own
adaptive-thinking default; row 4's codex `"timeout":"cancel"` half is upstream's documented
intentional `decline` stance). Row 45's round-4 sub-item enumeration was unavailable (defork4 logs
pruned); observed residue is fork aux-model test wiring only.

## Provenance

- Worklist: `~/.hermes/cache/scratch/v022-sync/defork5/worklist.jsonl` (52 rows).
- Unit logs (append-as-they-went JSONL): `…/defork5/U{1..4}.jsonl` — **scratch is prunable; THIS file
  is the durable copy** of the verdicts and the filing queue.
- Delegation: `deleg_c9f01b0d` (4 read-only units, DSv4.1-Flash); parent spot-checks of rows 36/29/15
  recorded above; suite evidence in the sync close-out entry.
