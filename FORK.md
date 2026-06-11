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
| `agent/fork/anthropic_native_web_search.py` | Provider-aware web search — on first-party Anthropic (Claude) swaps the client `web_search` tool for Anthropic's native server-side `web_search_20250305` tool so search runs inline; non-Claude endpoints keep the client tool. Config: `web.anthropic_native_search` (default on), `web.anthropic_native_search_max_uses`. |
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
| `agent/conversation_loop.py` | +330 / -7 | Per-turn callouts to fork modules (rate-limit capture, usage history, refusal handler). **T2.3**: refusal *detection* moved to `agent/fork/anthropic_recovery.is_anthropic_refusal`; the recovery ladder stays inline (control-flow-coupled). **2026-06**: the truncated tool-call recovery block was CONVERGED to upstream verbatim (upstream caught up with the same `_ephemeral_max_output_tokens` boost the fork added in cb293a90f) — that block is now byte-identical to upstream and won't conflict again. |
| `hermes_cli/banner.py` | (reduced by T2.5) | Branding + git-state subsystem moved to `hermes_cli/fork_banner.py`; banner.py keeps thin forwarders + the patchable git plumbing/caches. |
| `hermes_state.py` | (reduced by T2.1/T2.4) | Fork-only `api_calls` table → `FORK_SCHEMA_SQL`; fork column `anthropic_content_blocks` → `FORK_TABLE_COLUMNS` (reconciler-added). SCHEMA_SQL is now pure-upstream shape. |
| `run_agent.py` | +234 / -24 | 12 forwarder methods (now extracted to `ForkForwardersMixin`), `_classify_anthropic_stream_phase` top-level function, fork-state initialization. |
| `agent/agent_init.py` | +122 / -13 | Fork instance state initialization (delegated to `fork.<module>.init_state(agent)` where possible). |
| `agent/agent_runtime_helpers.py` | +119 / -29 | Scattered port additions during the 2026-05-19 upstream merge — mostly CC alias support in `repair_tool_call`, switch_model 1M-beta latch re-eval, swarm_run handling in `invoke_tool`. |
| `agent/tool_executor.py` | +111 / -29 | Skill-recall hook callsites (`_record_loaded_skill`, `_maybe_skill_recall_hint`) in both sequential + concurrent paths, plus hermes_load_tools and swarm_run dispatch. |
| `agent/system_prompt.py` | +17 / -23 | Date-only timestamp restored (upstream's prompt-cache fix), grok added to OPENAI_MODEL_EXECUTION_GUIDANCE gate. |
| `agent/turn_context.py` | (new upstream file, fork-patched) | Upstream (2026-06-08) extracted the per-turn prologue out of `conversation_loop.py` into this new `build_turn_context()` module. Carries 3 PORTED fork-only prologue steps: `memory_auto_feedback` session bind, `_last_user_message` capture (feeds `agent/fork/memory_recall.py`), `_recent_tool_args` reset. On conflict: keep those 3, the rest is upstream-shared; do NOT re-inline the prologue into `conversation_loop.py`. |

Plus 165 commits of fork-only history. See `git log upstream/main..main`.

### Fork-only fixes — 2026-06-02 (prompt-cache cost work)

Three changes from a cost-tracking investigation (polaris was cold-caching
~157K tokens/session, ~5x Claude Code). Root cause was a wiring bug, not a
config issue. **Not sent upstream** (user decision — "not my problem").

1. **`7b6cb3f98` — MCP tool-search deferral was dead code on the live path.**
   `agent/chat_completion_helpers.py::build_api_kwargs` (the anthropic_messages
   branch) never passed `tool_search_config=` or `cache_tools=` to the
   transport, so `agent/fork/tool_search_lazy.py`'s MCP-stub deferral and the
   native `tools[]` cache breakpoint were both inert. Every request shipped all
   MCP tool schemas in full (measured: 253 tools / ~399KB / ~100K tokens cold-
   cached on a 9-server install). Fix threads
   `tool_search_config=agent._build_tool_search_config()`, `session_id`,
   `cache_tools=agent._use_native_cache_layout`, `cache_ttl` through. Result:
   157K → 42K cold prompt. Test: `tests/run_agent/test_tool_search_config_wiring.py`.
   **Merge note:** this is a fork file already (streaming reliability edits). On
   conflict take ours; verify the anthropic branch still passes all four kwargs.

2. **`0eff5e9cc` — system-prompt stable|volatile cache split.** Anthropic caches
   the prefix cumulatively (tools → system → messages); the whole system prompt
   was one cached block, so the volatile tail (memory snapshot, USER profile,
   daily timestamp) cold-rewrote the byte-stable identity+tools head on any
   memory edit or date rollover. `agent/system_prompt.py::build_system_prompt`
   now inserts an internal `SYSTEM_VOLATILE_SENTINEL` at the boundary;
   `agent/prompt_caching.py::apply_anthropic_cache_control` splits the system
   block into `[{stable, cache_control}, {volatile}]` on the native layout
   (`_use_native_cache_layout`), keeping breakpoint count at 4. The sentinel is
   internal-only — always consumed by the split or stripped (at the injection
   point in `conversation_loop.py`) before send, so the model never sees it and
   sent bytes are unchanged. Falls back to a single block when no sentinel
   (empty volatile / pre-change stored prompts). Proven live: a warm session
   dropped from $0.27 → $0.066. Test:
   `tests/agent/test_system_prompt_cache_split.py` (8 tests).
   **Merge note:** `prompt_caching.py` becomes a soft-fork file (new helpers
   `split_system_for_cache` / `strip_volatile_sentinel` / `_apply_split_system_marker`).
   The `system_prompt.py` and `conversation_loop.py` edits are small; on
   conflict keep ours and re-verify the sentinel round-trips (strip == legacy
   flat join).

3. **Config (not code) — `prompt_caching.cache_ttl: 1h → 5m`** on both boxes, to
   match Claude Code's default (CC defaults to 5m; the 1h tier costs 2x on write
   vs 1.25x for 5m). The 1h tier only wins for 5–60-min idle gaps between turns;
   for sub-5-min or >1h gaps, 5m is cheaper. Hermes already SHIPS 5m as the
   default (`hermes_cli/config.py`); the 1h was a local override now removed.
   A dynamic-TTL adjustment system was discussed as future work.

Verified along the way: the cost tracker (`agent/usage_pricing.py`) prices
cache tiers correctly (read 10% of input; write 1.25x@5m / 2x@1h; no double-
counting; TTL-aware) and its hardcoded rate snapshot matches Anthropic's live
pricing as of 2026-06-02.


### Fork-only fixes — 2026-06-02 (oversized-image 413 / false compaction)

A 35 MB phone photo (4284×5712) attached to a CLI session triggered an endless
"Compacting context — summarizing earlier conversation" loop at only ~10% of
the 1M window, then died with `Request payload too large (413). Cannot compress
further.` The window was a red herring; three real bugs stacked up. **Not sent
upstream** (personal fork; same "not my problem" stance as the cache work).

Root cause chain:
- Anthropic's hard limit on this path is the **32 MB HTTP request body**, not
  tokens. A 35 MB image inflates to ~47 MB base64 and 413s
  (`request_too_large`) on the FIRST call, regardless of how few conversation
  tokens exist (the second observed failure was "11 msgs / ~10K tokens" — still
  413, because the image *is* the payload).
- The 413 classifies as `FailoverReason.payload_too_large`, whose only recovery
  is "compress conversation history + retry." But `compression.protect_last_n`
  (20) shields the turn holding the image, so compressing 93→10 messages leaves
  the ~47 MB image untouched → retry 413s again → "cannot compress further."
- The recovery that *would* work — `try_shrink_image_parts_in_messages` — was
  gated solely on `FailoverReason.image_too_large` (Anthropic's 5 MB
  *per-image* 400), so it never fired for the 32 MB *body* 413.
- And even if it had fired, **Pillow was not installed in the runtime venv**, so
  every resize path (`vision_tools._resize_image_for_vision`) silently no-op'd
  and returned native-size bytes.

Fixes:

1. **Proactive ingestion ceiling — `agent/image_routing.py`.**
   `_file_to_data_url` previously embedded local images at native size by
   design (deferring all shrink to "the provider's first rejection"). It now
   estimates base64 size and, when over `_NATIVE_IMAGE_CEILING_BYTES` (4 MB —
   matches the reactive shrink target, slides under both Anthropic's 32 MB body
   and 5 MB per-image limits), downscales via `_resize_image_for_vision` before
   encoding. Images under the ceiling pass through verbatim (no quality tax on
   screenshots / normal uploads). Anthropic downscales to ~1568px server-side
   anyway, so the trimmed pixels were going to be discarded regardless.
   Verified live: the actual pump photo → 47 MB base64 became a 3.56 MB PNG.

2. **Reactive recovery reorder — `agent/conversation_loop.py`.** The
   `is_payload_too_large` (413) handler now attempts
   `_try_shrink_image_parts_in_messages` FIRST, and only falls through to
   history compression when there are no shrinkable image parts. Shares the
   single-shot `image_shrink_retry_attempted` flag with the existing
   `image_too_large` path, so a genuinely text-too-large 413 still reaches
   compression after one image attempt. This is the safety net for images that
   reach the wire oversized through paths that bypass `_file_to_data_url`.

3. **Pillow made a real (lazy) dependency — `tools/lazy_deps.py`,
   `tools/vision_tools.py`.** Added `image.resize → Pillow==12.2.0` to the
   `LAZY_DEPS` allowlist; `_resize_image_for_vision` now calls
   `lazy_deps.ensure("image.resize", prompt=False)` on first `ImportError`
   instead of silently giving up. Pillow stays out of core deps (text-only
   sessions never touch it) but auto-installs the first time an oversized image
   actually needs downscaling.

Tests: `tests/agent/test_image_routing.py::TestFileToDataUrlIngestionCeiling`
(4 new — pass-through under ceiling, missing-file None, oversized downscaled
under ceiling, Pillow-absent native fallback). Full image sweep green
(`test_vision_tools`, `test_image_routing`, `test_image_shrink_recovery`,
`test_image_rejection_fallback`, `test_vision_aware_preprocessing`,
`test_compressor_image_tokens`, `test_lazy_deps`).

**Merge note:** `image_routing.py` and `conversation_loop.py` are already
soft-fork files; on conflict keep ours and re-verify (a) `_file_to_data_url`
still resizes over `_NATIVE_IMAGE_CEILING_BYTES`, and (b) the 413 handler tries
image-shrink before `compression_attempts += 1`. `lazy_deps.py` /
`vision_tools.py` edits are additive — the `image.resize` key and the
`ensure(...)` fallback. **Activation:** running sessions must `/restart` to load
the patched `image_routing.py`; the module is read once at startup.


### Upstream sync — 2026-06-08 (771 commits, 17 conflicts)

Merge-base was 2026-06-02; pulled 771 upstream commits on branch
`sync/upstream-2026-06-08` (tag `pre-upstream-sync-2026-06-08`). 17 conflict
files, all resolved. New/changed fork surface this sync:

* **`agent/turn_context.py` is now a SOFT-FORK file.** Upstream extracted the
  entire per-turn prologue out of `conversation_loop.py` into this new module
  (`build_turn_context()`). Three fork-only prologue steps were PORTED into it:
  the `memory_auto_feedback` session bind, the `_last_user_message` capture (feeds
  `agent/fork/memory_recall.py`), and the `_recent_tool_args` reset. On conflict:
  keep these three; the rest is upstream-shared. `conversation_loop.py` now just
  calls `build_turn_context(...)` — do NOT re-inline the prologue.
* **`conversation_loop.py` retry flags → upstream's `TurnRetryState`.** Upstream
  consolidated the per-turn auth/retry single-shot flags into a `TurnRetryState`
  dataclass (`_retry`). Took upstream's consolidation; the fork's 413-image-shrink
  path was rewired from a bare local to `_retry.image_shrink_retry_attempted` so it
  shares upstream's single-shot flag (the FORK.md image-413 design intent). Two
  fork-only flags have no `TurnRetryState` home and stay bare locals:
  `_strip_cache_for_overload`, `_refusal_sanitize_attempted`.
* **`agent_runtime_helpers.py` + `tool_executor.py` dispatch → `_execute` closures.**
  Upstream moved tool dispatch to a uniform `_execute(next_args)` + middleware
  pattern (busy-input steering). Converted the fork's `session_search` / `memory` /
  `hermes_load_tools` / `swarm_run` branches to the closure form, preserving warm-
  tier memory args + merged session-search scroll params.
* **`AGENT_RUNTIME_POST_HOOK_TOOL_NAMES` frozenset gained `hermes_load_tools` +
  `swarm_run`.** Upstream shipped a new invariant test
  (`test_frozenset_matches_inline_dispatch_chain`) asserting every inline dispatch
  branch that emits its own post-hook is listed in this frozenset. The fork's two
  extra inline branches (fork-only in `tool_executor`) weren't in upstream's
  frozenset → added them, else `post_tool_call` double-fires. **Merge note:** if a
  future sync re-introduces this drift, add any fork-only inline dispatch branch to
  the frozenset.
* **`main.py` — `cmd_insights` relocated by upstream.** Upstream moved
  `cmd_insights` to a module-level def + a `build_insights_parser()` helper. Ported
  the fork's `account_billed` feature (authoritative billed figure via
  `fetch_anthropic_billing`) into the relocated def.
* **Converged to upstream ("when upstream catches up, take upstream"):**
  `hermes_cli/models.py` (gemini flash slugs — upstream now offers both
  preview+GA), `hermes_cli/doctor.py` (vendor-slug `custom:` predicate),
  `tools/vision_tools.py` (Pillow lazy-install — adopted upstream's `tool.vision`
  key + #40490 deadlock comment, removed the orphaned fork `image.resize` key from
  `tools/lazy_deps.py`).
* `SCHEMA_VERSION` 16 → 17 (max of fork-16 / upstream-15 + 1).

Verification: full `tests/agent/` + `tests/run_agent/` = 5805 passed. The one real
failure (frozenset drift, above) was fixed; the other 12 are the documented
ordering-pollution flakes (`test_subagent_stop_hook`, `test_vision_routing_31179`,
`test_provider_parity::...openrouter_always_wins`, `test_auxiliary_main_first`),
all green in isolation.


### Fork-only fix — 2026-06-08 (MCP parallel-safe prefix gate)

Post-sync cleanup of a stale fork test
(`test_mcp_tool.py::...test_registered_tool_provenance_prevents_prefix_collision`,
which asserted the upstream `mcp_` prefix the fork deliberately removed) surfaced
a REAL latent fork bug, not just a test-shape mismatch:

`tools/mcp_tool.py::is_mcp_tool_parallel_safe` still early-returned `False` on
`if not tool_name.startswith("mcp_")`. But the fork registers MCP tools WITHOUT
that prefix (`_build_tool_schema` → `{server}_{tool}`, e.g. `github_search`), so
**every fork MCP tool was wrongly classified non-parallel-safe** and silently
serialized — even on servers with `supports_parallel_tool_calls: true`. The
function's own docstring already prescribed the correct approach ("use exact
server provenance captured at registration, not prefix matching"); the gate was
leftover from before the prefix was dropped.

Fix: replaced the `startswith("mcp_")` gate with an empty-name guard, keying
purely on the `_mcp_tool_server_names` provenance map (a tool only has an entry
there if registered as MCP, so the map lookup already filters non-MCP tools).
Updated the stale test to assert the fork's actual `{server}_{tool}` name shape
with a docstring documenting the divergence. Consumer
(`agent/tool_dispatch_helpers.py::_is_mcp_tool_parallel_safe`) unchanged. Verified:
`tests/tools/test_mcp_tool.py` + `tests/agent/test_tool_dispatch_helpers.py` =
225 passed. **Merge note:** `mcp_tool.py` is otherwise upstream-shared; on a future
conflict here keep the provenance-only gate (no `mcp_` prefix check) — re-adding
upstream's prefix match would re-break fork MCP parallelism.


### Fork-only fix — 2026-06-10 (provider-aware web search: native on Claude)

The CLI kept emitting **"Web search isn't configured"** on a plain Anthropic
setup with no third-party search key. Root cause was a half-built capability,
not a config mistake. **Not sent upstream** (personal fork; same stance as the
rest of this file — and it leans on the Claude Code OAuth machinery, which is
the headline never-upstream feature).

Root cause:
- Hermes' `web_search` is a **client tool** (`tools/web_tools.py`) that the
  agent calls and Hermes dispatches to a configured backend (firecrawl / exa /
  parallel / tavily / searxng / brave-free / ddgs / xai).
- On first-party Anthropic, `check_web_api_key()` reports the tool *available*
  purely because Anthropic creds are present (`ANTHROPIC_API_KEY` / Claude Code
  OAuth, web_tools.py ~1209), so the tool is registered and offered to the
  model. But at dispatch `_get_search_backend()` falls back to the `firecrawl`
  default with no key → `web_search_tool` returns "No web search provider
  configured." Every time.
- Anthropic exposes a **native server-side** web search tool
  (`web_search_20250305`): the model searches inline, Anthropic runs it, and
  results stream back as `server_tool_use` / `web_search_tool_result` blocks.
  The adapter already had the code to STORE + reconcile those result blocks
  (`anthropic_adapter.py` ~2230-2540 + `agent/fork/anthropic_messages.py`) —
  but **nothing ever put the native tool definition on the request wire.** A
  stale comment in `web_tools.py::check_web_api_key` even claimed
  `convert_tools_to_anthropic()` "decides whether to send the native form";
  it never did. Half-built, never reachable.

Fix — provider-aware priority (Claude → native, everything else → client):
1. **`agent/fork/anthropic_native_web_search.py` (hard-fork, new).**
   `apply_native_web_search(anthropic_tools, base_url)` finds the client
   `web_search` entry in the converted tools array and replaces it in place
   with the native server-tool param dict
   (`{"type": "web_search_20250305", "name": "web_search", "max_uses": N}`),
   but ONLY when `is_first_party_anthropic(base_url)` (delegates to
   `anthropic_adapter._is_third_party_anthropic_endpoint` — any non
   `*anthropic.com*` host is third-party). Idempotent, order-preserving,
   cache_control-preserving, never raises (degrades to the client tool on any
   error). Scoped to first-party Anthropic only — Bedrock/Vertex Claude
   classify as third-party here and keep the client tool until explicitly
   opted in. `web_extract` deliberately left on the client path (its native
   analog is a separate `web_fetch` server tool).
2. **`agent/anthropic_adapter.py::build_anthropic_kwargs` (soft-fork, ~3 lines).**
   A thin forwarder calls `apply_native_web_search(anthropic_tools, base_url)`
   right after the OAuth cc-aliasing and before `_apply_tool_search`. This is
   the only edit to an upstream-shared file.
3. **`hermes_cli/config.py` (soft-fork).** Two `web:` keys —
   `anthropic_native_search` (default `True`) and
   `anthropic_native_search_max_uses` (default `5`).
4. **`tools/web_tools.py` (soft-fork, comment only).** Corrected the stale
   `check_web_api_key` docstring to point at the real swap site.

Tests: `tests/agent/test_anthropic_native_web_search.py` (22 — unit swap logic
+ first/third-party classification + integration through
`build_anthropic_kwargs` for native/oauth/third-party paths). Regression:
`test_anthropic_adapter` + `test_minimax_provider` +
`test_kimi_coding_anthropic_thinking` = 242 passed.

**Merge note:** the logic is isolated in `agent/fork/` (never conflicts). The
only upstream-shared touch is the 3-line forwarder in `build_anthropic_kwargs`
— on conflict keep ours (the `apply_native_web_search(...)` call must stay
between the OAuth cc-alias block and `_apply_tool_search`). If upstream ever
implements native web search natively, converge per the "when upstream catches
up, take upstream" rule and drop this module + its forwarder + test. Until
then it stays — it depends on the fork-only first-party-Anthropic / OAuth
surface and is not upstream-bound.


### Upstream sync — 2026-06-10 (187 commits, 12 conflicts)

Merge-base was 2026-06-08; pulled 187 upstream commits on branch
`sync/upstream-2026-06-10` (tag `pre-upstream-sync-2026-06-10`). Drift now 0.
12 conflict files, all resolved. Notable points this sync:

* **The native-web-search fork feature (2026-06-10) merged with ZERO conflicts.**
  `agent/fork/anthropic_native_web_search.py` + its test were untouched by the
  merge (the `agent/fork/` isolation pattern working as designed); the only
  shared touch — the 3-line forwarder in `build_anthropic_kwargs` — survived
  intact because the actual `anthropic_adapter.py` conflict was elsewhere (the
  `_ANTHROPIC_OUTPUT_LIMITS` dict). All 22 web-search tests green post-merge.
  This was the live proof that the fork-safe design holds across a real sync.
* **Conflict resolutions (keep-both unless noted):**
  - `anthropic_adapter.py` — `_ANTHROPIC_OUTPUT_LIMITS`: kept fork's CC-mimicry
    comment + upstream's new `claude-fable` entry.
  - `hermes_cli/config.py` — skills dict: kept fork's `lazy_listing` +
    upstream's new `write_approval`.
  - `agent_runtime_helpers.py` — `AGENT_RUNTIME_POST_HOOK_TOOL_NAMES` frozenset:
    kept fork's `hermes_load_tools`/`swarm_run` + upstream's new `read_terminal`
    (the recurring frozenset-drift from the 2026-06-08 sync — same fix). Plus
    the `_execute`-closure dispatch chain: kept both branches.
  - `tool_executor.py` — same dispatch-chain keep-both (fork `hermes_load_tools`
    + upstream `read_terminal`).
  - `conversation_loop.py` — thinking-sig recovery: kept fork's
    `anthropic_content_blocks` pop + upstream's `_api_stripped` counter.
  - `chat_completion_helpers.py` — streaming reliability (fork file): kept fork's
    cold-start tracking vars + upstream's `_stream_stale_timeout` socket-read
    floor.
  - `error_classifier.py`, `cli.py`, `memory_tool.py`, `test_error_classifier.py`,
    `test_usage_pricing.py` — keep-both (independent fns/tests colliding).
  - `cli.py` session-finalize: **converged to upstream** — upstream extracted the
    fork's inline `invoke_hook("on_session_finalize")` into
    `_notify_session_finalize`; took upstream's, kept the fork-only Phase-2
    memory-extraction block beside it.
  - `uv.lock` — regenerated via `uv lock` after pyproject merge.
* **Real regression caught by the post-merge sweep (NOT a conflict file):**
  `agent/title_generator.py` auto-merged into a Frankenstein — upstream's slim
  function body wrapped with a fork-era `show_auxiliary_errors` config gate that
  did `from agent.config import read_config`. **`agent.config` exists in neither
  fork nor upstream**, so the import raised, got swallowed by the bare `except`,
  and `failure_callback` silently never fired (`test_title_generator` red).
  Fix: dropped the dead config gate, call `failure_callback` directly (matches
  upstream's shape). This is the canonical "run the real blast radius" catch —
  the file never conflicted, so only the test suite surfaced it.
* **Pre-existing failures (NOT merge-caused), confirmed by re-running at the
  pre-sync tag + in isolation:** `test_credential_pool` (3 — env-dependent on the
  machine's real keychain `keychain_longlived` Anthropic cred), plus the
  documented global-state-pollution flakes `test_vision_routing_31179`,
  `test_provider_parity::...openrouter_always_wins`, `test_auxiliary_main_first`,
  `test_display_todo_progress::test_default_skin_prefix` — all green in isolation.

Verification: `tests/agent/` + `tests/run_agent/` = 5900 passed, 10 failed (all
the pre-existing flakes above), 38 skipped. Boot smoke clean.


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

**When upstream catches up, take upstream.** If a conflict is on a fork patch that
upstream has since implemented natively (same feature, possibly different shape),
resolve it by adopting upstream's version verbatim, not by re-applying the fork's.
This shrinks the divergence permanently — that hunk stops conflicting on every
future merge. Confirm it's the SAME feature first (same observable behavior + tests
still green), then drop any fork-only test infrastructure / helpers the convergence
orphans. Done 2026-06 for: the truncated tool-call recovery block in
`conversation_loop.py` (now byte-identical to upstream), and the
`conversation_compression.py` estimator call (dropped the `_ra()` test-patch
indirection). Distinguish from genuine fork FEATURES with no upstream equivalent
(Claude Code OAuth, MCP disk-cache, claude-code web backend, memory/skill-recall) —
those stay.

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
