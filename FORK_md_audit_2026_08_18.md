# FORK.md Full Behavioral Audit — 2026-08-18

Repo: `~/repos/hermes-agent`, HEAD `2214c67172` (post v2026.8.18 upstream sync
+ de-fork audit). Working tree clean, pushed to `origin/main`.

Full audit of all 176 dated FORK.md entries (lines 6–11282), split into 12
parallel line-range batches (~15 entries each) per the `fork-documentation-audit`
skill's methodology, followed by 4 direct spot-checks of the specific drift
claims before trusting them.

## Executive summary

| Verdict | Count | % |
|---|---|---|
| CONFIRMED (behavior matches doc exactly) | 158 | 90% |
| DRIFTED — self-documented (entry already says SUPERSEDED/RETIRED/OBSOLETE/CONVERGED) | 11 | 6% |
| DRIFTED — **undocumented** (real behavior change, no annotation in FORK.md) | 4 | 2% |
| UNVERIFIABLE (historical PR/process narrative, no standing code claim) | 3 | 2% |
| STALE / BROKEN (feature reverted, code missing, or genuinely non-functional) | 0 | 0% |

**Bottom line: zero broken or dead fork features found.** Every documented
fix and feature that's supposed to be live and working, is. The only
actionable findings are 4 places where FORK.md's prose has fallen behind a
real (and fine) code change — pure documentation debt, not functional risk.

## Undocumented drift — the 4 real findings (recommend fixing)

### 1. Pet-voice TTS `provider` param — entry's core claim is now false

**Entry:** 2026-07-24 "pet voice via Miku RVC voice-conversion pipeline"
(FORK.md ~line 2200s)

**Claim:** `text_to_speech_tool()`'s `provider_override` kwarg is "NOT
exposed on the model-facing tool schema... only the desktop's own REST
endpoint uses it."

**Reality (verified directly, not just subagent-reported):** `tools/tts_tool.py`'s
`TTS_SCHEMA` (line 4451) has a `provider` property in its model-facing
`parameters.properties` block, with a full description of accepted values
(edge/openai/elevenlabs/minimax/xai/mistral/gemini/neutts/kittentts/piper +
custom command providers). This was added later (subagent identified commit
`462b3cf994` "feat(tts): add optional provider parameter to text_to_speech
tool"). The underlying desktop-REST-endpoint mechanism the entry describes
still works fine — the entry's narrower "not exposed to the model" claim is
just no longer true.

**Risk:** None functionally. Purely a case of a future reader trusting a
stale claim about tool-schema surface area.

**Fix:** one-line annotation on the 07-24 entry noting the later widening.

### 2. TUI todo board — dynamic row cap + marker glyph changed, undocumented

**Entry:** 2026-08-10 "TUI: persistent to-do list moved out of transcript,
anchored to status line" (and its bordering sibling entry)

**Claim:** fixed 12-row hard cap, `⬜` pending marker.

**Reality (directly verified):** `cli.py:22118` — `_TODO_BOARD_MAX_ROWS = 30`
with a `term_rows // 2` dynamic formula (`_TODO_BOARD_MIN_ROWS` to
`_TODO_BOARD_MAX_ROWS` clamp), not a fixed 12. Subagent also reports the
pending marker is now plain `[ ]`, not `⬜` (I did not independently
re-confirm the marker glyph — the row-cap change I did verify directly).

**Risk:** None — this is a legitimate later improvement (adapts to terminal
height instead of a hardcoded number), just never got its own FORK.md
follow-up entry.

**Fix:** brief addendum to the 08-10 entry, or a short new dated entry
documenting the 30-row dynamic-cap follow-up.

### 3. Vite chunk-size ceiling fix — silently superseded

**Entry:** 2026-07-23 (desktop chunk-size warning bump 25000→32000,
`codeSplitting:false` kept)

**Reality:** live `vite.config.ts` has `chunkSizeWarningLimit: 25000` (reset
back down) and `codeSplitting:false` replaced by an `advancedChunks`
configuration — a later commit (`6fb5d2d89c`, "split heavy lazy-only libs
out of the renderer entry chunk") took a structurally different approach
(actually splitting the bundle) instead of just raising the warning
ceiling. Unlike sibling entries in the same date range that self-annotate
supersession, this one doesn't.

**Risk:** None — the later commit is a strictly better fix (addresses the
cause, not just the symptom). Just undocumented.

**Fix:** annotate with a "superseded by 6fb5d2d89c" note, matching the
convention already used elsewhere in the file.

### 4. `swarm_run`/`tools/swarm_tool.py` — dead file still listed as live in FORK.md's own reference tables

**Entry:** hard-fork file table (~line 5625) + soft-fork diff table (~line
5650) + several older dated entries (2026-07-21 toolset-split entry, etc.)

**Reality (directly verified):** `tools/swarm_tool.py` does not exist on
disk. It was deliberately retired via commit `99c8f2c9c4` "remove(swarm):
retire dead swarm_run tool and hermes-swarm dependency". Confirmed zero
live references to `swarm_run` remain in `agent/agent_runtime_helpers.py`,
`agent/tool_executor.py`, or `toolsets.py` — this is NOT dead code sitting
in the codebase, it's cleanly gone. `tools/swarm_board.py` (the live
progress *widget*, kept per your explicit decision this session) is a
different, still-live file — don't confuse the two.

**Risk:** None to running code. Risk is purely to a future sync/de-fork
session trusting FORK.md's own hard-fork table and wasting time
investigating a file that isn't there.

**Fix:** remove the `tools/swarm_tool.py` rows from both reference tables,
and add a one-line note to the surviving mentions in older dated entries
(07-21 toolset split, etc.) that the tool itself was later retired
(keeping `swarm_board.py`, the display layer, and `swarm` toolset gating
logic, which are unaffected).

## Self-documented drift (11 entries) — correctly annotated, no action needed

These entries already say SUPERSEDED / RETIRED / OBSOLETE / CONVERGED /
DE-FORKED / "Read path RETIRED" in their own text, and live-code
verification confirms the superseding mechanism is actually in place. Not
re-listed individually here since the file's own text already tells the
true story correctly:

- 2026-07-06 status-bar timer + approval timeout (superseded 2026-08-04)
- 2026-07-07 consult tool + periodic nudge (de-forked, upstream PR #82103)
- 2026-07-14 legacy `fallback_model`/`fallback_models` read paths (retired 2026-08-04)
- 2026-07-18 `auxiliary_client.py` runtime-main race (superseded by ContextVar rework, 2026-07-21)
- 2026-07-22 terminal-deck `front` param (superseded 2026-08-04)
- 2026-07-23 tool-group-scroll pending-row reorder (marked OBSOLETE 2026-08-04)
- 2026-07-23 "Event loop closed" on /exit (partially upstreamed 2026-08-04)
- 2026-07-23 sidebar click → browser tabs (re-expressed 2026-08-04)
- 2026-07-24 workspace tab × / closer (superseded 2026-08-04)
- 2026-07-26/07-27 pet rAF visibility-gate saga, 3 entries (superseded by `renderer-loop-pause` controller, 2026-08-04)
- 2026-06-02 sentinel-based system-prompt cache split (retired 2026-08-04, `strip_volatile_sentinel` survivor kept for legacy session-DB reads)

## Unverifiable (3 entries) — historical narrative, no standing claim

- 2026-08-14 De-fork audit entry itself (audit-only, no code claim)
- 2026-08-02 "Second rebase round" (#72087/#72153 — describes a past PR-merge event)
- 2026-07-12 Upstream sync v2026.7.7.2 (historical merge-log summary)

These describe things that happened, not standing behavioral guarantees —
correctly out of scope for a "does the code still do X" audit.

## Full CONFIRMED list

158 entries spanning 2026-06-02 through 2026-08-18 were independently
verified (each subagent traced the actual function/class/config-key/test
file the entry claims, not just checked file existence) with zero
discrepancies. Categories covered: CLI cancel-ladder/session-finalize
behavior, Anthropic wire-shape parity (CC alias translation, beta kwargs
guard, thinking-signature handling), auxiliary task provider-first config
schema, warm-tier memory (recall/pin/session-pin/hot-tier audit),
delegation (SwarmBoard, cross-session visibility, cwd-collision warnings,
auto-route classifier), desktop pet animation/voice pipeline (roam, jump,
zone pane, RVC voice), desktop pane/tab/sidebar drag-and-reorder, CI/test
determinism fixes (18+ entries), macOS credential/Keychain handling, and
6 separate upstream-sync merge-log entries. Full per-entry table available
in the 12 live subagent transcripts under
`~/.hermes/cache/delegation/live/deleg_0a2418c8/task-{0..11}.log` if a
future session needs the raw per-entry evidence.

## Methodology notes

- 12 parallel subagents, each auditing a ~900-1400-line FORK.md slice
  (~15 dated entries), using the `fork-behavior-audit`/`fork-documentation-audit`
  skill's 5-point verification path (code presence → signature match →
  call-site tracing → guard-condition check → test coverage), not grep-only.
- All subagents were explicitly warned off any stale sibling clone and
  told the correct absolute path (`~/repos/hermes-agent`) — one subagent's
  task context had a wrong path baked in from an earlier planning step
  (`~/repos/claude-code`) and self-corrected to the right path before
  auditing, confirmed in its report.
- 4 of the most notable claims (2 undocumented-drift, 2 self-documented)
  were independently re-verified by direct `grep`/`Read` rather than
  taken on the subagent's word, per this skill's own documented pitfall
  about batch-audit reliability. All 4 confirmed accurate.
- Total audit wall-clock: ~2 minutes (parallel dispatch), ~118s max
  single-task duration.

## Recommendation

None of the 4 undocumented-drift findings represent functional risk —
every one is "the code got better/changed shape, the doc didn't follow."
Recommend a single follow-up commit: 4 small FORK.md text edits (no code
changes) annotating each drift the same way the file's own 11
self-documented examples already do. I have not made any edits — this
file is the evidence trail; the actual FORK.md patch is a explicit go/no-go
for you.
