# Fork notes — adurham/hermes-agent

This is a personal fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
Code here is **not intended for upstream contribution.** See "Why a fork" below.

### Fork-only fix — 2026-07-22 (desktop: work profile deletion silently reverted after quitting and reopening the app)

Reported symptom: deleting the "work" profile from the desktop app's Manage
Profiles panel appeared to succeed, but the profile reappeared every time the
app was quit and relaunched — reproduced 3 times in a row.

Root cause, two independent bugs found across the backend and the sidebar UI:

1. **Zombie backend process survived delete.** `_profile_bound_backend_pids()`
   (`hermes_cli/profiles.py`) scans running processes for ones bound to a
   profile so delete can terminate them first. It required `argv[0]` to
   resolve to an executable literally named `hermes` (or contain a
   `hermes_cli.main`/`hermes-gateway`/`tui_gateway` marker). Electron's
   pool-backend spawn resolves the `hermes` console-script shim's path via
   `findOnPath('hermes')` and executes it through the interpreter directly —
   `python3 /path/to/hermes --profile work serve ...` — so the OS reports
   `argv[0]` as `python3`, not `hermes`; the joined-argv marker check also came
   up empty. The scanner never matched the running backend, so delete removed
   the profile's directory/config while its live backend process (which
   re-persists profile state as it runs) kept running untouched — surviving
   the delete and reappearing at next launch.
   **Fix:** added a `python[\d.]*w?(\.exe)?` interpreter-basename check; when
   `argv[0]` matches, additionally check `argv[1]` (the script path handed to
   the interpreter) for a `hermes`-prefixed basename. Verified live: the
   previously-immortal PID (running since before the fix, survived 3 delete
   attempts) is now correctly targeted and killed.
2. **Sidebar profile rail cached a stale profile list.** Even with the
   backend fix, `apps/desktop/src/app/chat/sidebar/profile-switcher.tsx`'s
   `ProfileRail` only called `refreshActiveProfile()` once, on mount — a
   profile deleted (or created/renamed) from another surface (the Manage
   Profiles panel, another window, the CLI) left the rail's cached `$profiles`
   atom stale until something unrelated happened to trigger a refetch, which
   is why opening Manage Profiles was previously the only thing that made a
   deleted profile's ghost square disappear.
   **Fix:** added a `window focus` + `document visibilitychange` listener
   that re-calls `refreshActiveProfile()`, matching the existing
   focus/visibilitychange refresh idiom already used elsewhere in the sidebar
   (`refreshProjects`/`refreshProjectTree` in `sidebar/index.tsx`).

Added `test_backend_scan_matches_shebang_exec_of_hermes_shim` to
`tests/hermes_cli/test_profiles.py` (interpreter-exec'd shim bound to the
target profile is matched; a different profile or a non-hermes script under
python3 is correctly skipped). `scripts/run_tests.sh
tests/hermes_cli/test_profiles.py` — 156/156 passing. `tsc --noEmit` and
`eslint` clean on `profile-switcher.tsx`.

Files: `hermes_cli/profiles.py`, `tests/hermes_cli/test_profiles.py`,
`apps/desktop/src/app/chat/sidebar/profile-switcher.tsx`.

### Fork-only fix — 2026-07-22 (desktop: duplicate "working" pulse indicators for collapsed sidebar session groups; stale indicators never cleared)

Reported symptom (screenshot): two horizontal pulsing "working" indicators
visible simultaneously where only one was expected, in the session sidebar.

Two related bugs, one about a stuck signal and one about how many places
render it:

1. **Stale `busy: true` state never cleared.** Live session status
   (working-dot, arc-border) is normally driven by streamed gateway events,
   but events emitted while Desktop was disconnected can't be replayed —
   `rehydrateLiveSessionStatuses()` (`apps/desktop/src/app/contrib/hooks/use-background-sync.ts`)
   already reconciled the reconnect direction (restoring liveness the
   snapshot reports) but never the reverse: a runtime the renderer still
   marked `busy: true` that the authoritative `session.active_list` snapshot
   stopped reporting (a missed terminal event — reconnect blip, an
   auto-compression rotation racing the poll) stayed `busy` forever.
   `sessionsToKeep()` force-keeps any `busy` row visible, so a stuck entry
   permanently rendered its pulse + arc-border with no turn actually running.
   **Fix:** the rehydrate sweep now also walks every runtime still marked
   `busy` and, if 3+ consecutive polls (~9.5s total, `MISSING_RUNTIME_GRACE_MS`)
   stop reporting it, force-clears `busy`/`awaitingResponse`/`needsInput`. The
   grace window avoids punishing a single flaky poll miss or a fresh
   optimistic send (busy is set locally before the backend runtime is
   registered — see `seedOptimistic` in `use-prompt-actions/submit.ts`).
2. **One indicator per hidden row, not per collapsed group.** A collapsed
   workspace/project group hides its child session rows entirely — including
   each row's own working-dot — with no substitute cue at the group level, so
   a user had no way to tell a hidden session was still running. Once (1)
   above is fixed, the remaining ambiguity was: any place a group can show a
   redundant indicator alongside its still-visible children.
   **Fix:** added `WorkspaceWorkingDot` (`chrome.tsx`, same pulse styling as a
   session row's own dot) and threaded a `workingSessionIdSet` down through
   `SidebarSessionsSection` → `EnteredProjectContent`/`RepoFlatSection` →
   `SidebarWorkspaceGroup` → `WorkspaceHeader`. The dot renders on a group's
   header **only while that group is collapsed** and only when a session
   inside it is working — expanded groups show nothing extra since each
   row's own dot/arc-border is already visible. Net result: exactly one
   pulsing cue per running session at all times — the row's own dot while
   expanded, the collapsed group's aggregate dot while collapsed — never both.

Verified: `tsc --noEmit` clean across the whole desktop app; `eslint` clean on
all seven touched files; 95 sidebar/session-state tests pass (70 in
`sidebar/` + `use-background-sync.test.ts`, 25 in `store/session-states.test.ts`
+ `store/session-watchdog.test.ts`), including 3 new cases for the
missing-runtime reconciliation sweep.

Files: `apps/desktop/src/app/contrib/hooks/use-background-sync.ts` (+ test),
`apps/desktop/src/app/chat/sidebar/chrome.tsx`,
`apps/desktop/src/app/chat/sidebar/projects/workspace-header.tsx`,
`apps/desktop/src/app/chat/sidebar/projects/workspace-group.tsx`,
`apps/desktop/src/app/chat/sidebar/projects/entered-content.tsx`,
`apps/desktop/src/app/chat/sidebar/sessions-section.tsx`.

### Fork-only fix — 2026-07-22 (desktop: queued composer message could be delivered into a different, currently-viewed session)

Reported symptom: user queued a composer message while viewing session A
(agent busy), switched to viewing session B before A's turn finished, and
the queued message landed in / was sent to session B instead of A.

Root cause: `useBackgroundQueueDrain`
(`apps/desktop/src/app/session/hooks/use-background-queue-drain.ts`) — the
hook that drains queued prompts for sessions not currently rendered by
ChatBar — resolved the target session's live runtime id via a **raw,
unvalidated** `runtimeIdByStoredSessionIdRef.current.get(storedSessionId)`.
That stored→runtime map can go stale: a pooled/idle-reaped profile backend
re-mints runtime ids (`pruneSecondaryGateways`), so an old mapping can end up
pointing at a runtime id that now belongs to a **different, currently-live
session**. This exact failure mode is already named, documented, and guarded
against elsewhere in the same codebase —
`use-session-state-cache.ts::getRuntimeIdForStoredSession` exists
specifically to reject a mapping whose target runtime's cached state no
longer claims the requested stored id (with its own regression test, "only
returns a runtime whose cached state owns the requested stored session") —
but `useBackgroundQueueDrain` wasn't using it, even though the validated
getter was already in scope one call up in `wiring.tsx`. Downstream,
`submit.ts`'s `useSubmitPrompt` honors an explicitly-passed `sessionId`
faithfully (seeds optimistic state, submits `prompt.submit` against that
exact runtime id), so a stale/recycled id handed to it by the drain wasn't
just a UI paint bug — it dispatched the queued text as a live turn against
whichever session actually held that runtime id.

**Fix:** `useBackgroundQueueDrain` now takes `getRuntimeIdForStoredSession`
(the validated getter) instead of the raw `runtimeIdByStoredSessionIdRef`
map, mirroring the latest-closure-ref pattern the hook already uses for
`submitText`. Call site (`wiring.tsx`) updated to pass the getter that was
already computed there. On a stale/cross-wired mapping the getter now
returns `null`, and the existing `submitText`/`session.resume` fallback path
(already exercised by the "resume then send" test) reattaches by stored id
instead of misrouting into whatever session currently owns the stale runtime
id.

Added a regression test to `use-background-queue-drain.test.tsx` — "passes
null (not a recycled runtime id) when the stored→runtime mapping is
cross-wired" — simulating the validated getter rejecting a stale mapping and
asserting the drain falls back to `sessionId: null` rather than the stale
id. All 6 tests in the file pass (5 pre-existing + 1 new); `tsc --noEmit`
clean. Diagnosis independently reviewed via `mcp__consult` before
implementing.

A second, related symptom was also reported in the same session: a clarify
(blocking Q&A) prompt raised by a background session didn't render/trigger
when a different session was in view. Investigated but **not the same code
path** — clarify state (`store/clarify.ts`) is keyed directly off the
runtime id carried by the gateway's `clarify.request` event
(`gateway-event.ts`), not through the stored→runtime map this fix touches.
Left open for separate investigation.

Files: `apps/desktop/src/app/session/hooks/use-background-queue-drain.ts`,
`apps/desktop/src/app/session/hooks/use-background-queue-drain.test.tsx`,
`apps/desktop/src/app/contrib/wiring.tsx`.

### Fork-only fix — 2026-07-22 (aux tasks stuck on stale provider-block "default" model after switching main; "Reset all to main" wrote nothing)

Reported symptom: switching the desktop Models page's main model to
Anthropic `claude-sonnet-5` did not change any auxiliary task's model (all
still showed `claude-haiku-4-5-20251001`); clicking "Reset all to main"
also left every task on `claude-haiku-4-5-20251001` instead of the newly
selected main model.

Root cause, two compounding bugs in the provider-first `auxiliary` schema
(`agent/auxiliary_client.py::_aux_flatten_provider_first`):

1. **The "default" key conflated two unrelated concepts.** A model-only
   provider block (e.g. `auxiliary.anthropic: {default: claude-haiku-4-5,
   provider: anthropic}`) used the SAME `default` key as a genuine
   cross-provider redirect block (e.g. `auxiliary.exo: {provider:
   ollama-cloud, default: gemma4:31b}`, which has no "main model" concept
   and must name *some* model to route to). For a model-only block, `default`
   was silently governing every unconfigured task FOREVER — including after
   switching main to a different model on the same provider — instead of
   deferring to `_resolve_auto()`'s Step 1 (which tracks the LIVE main
   model). A redundant same-provider `provider: anthropic` key inside the
   anthropic block made this worse: it also tripped the block's "has an
   explicit endpoint" check, routing the request through
   `resolve_provider_client`'s hardcoded-aux-model branch instead of the
   auto-detect chain that actually tracks main.
2. **"Reset all to main" never wrote anything.** The `__reset__` handler in
   `hermes_cli/web_server.py` (`POST /api/model/set`) only DELETES any
   top-level pin and the current-main block's per-task entry — it does not
   touch the block's `default` key (by design, to avoid clobbering
   hand-authored per-provider defaults meant for other providers). Combined
   with bug (1), deleting the task entries just fell straight back to the
   same stale `default` — so the button appeared to do nothing.

Fix: `_aux_flatten_provider_first` now distinguishes a genuine
cross-provider/endpoint redirect (`is_cross_provider_redirect`: block names
an explicit `base_url`, OR a `provider` that normalizes to something
DIFFERENT from the live main provider) from a same-provider model-only
block. Only the former may (a) consult the block's `default` model and (b)
emit an explicit non-`auto` provider override. A same-provider block —
redundant `provider:` key or not — is now always treated as model-only:
unconfigured tasks resolve to `provider="auto", model=None`, which
`_resolve_auto()` fills in with the CURRENT main provider + main model at
call time. Explicit per-task overrides (e.g. `anthropic.memory_extraction:
claude-sonnet-5`) are untouched — this only removes the provider-wide cheap
fallback tier for tasks with NO configured override. The exo block's
cross-provider `default` (its "assume from main aux config" rule) is
preserved exactly as before, since it has no main-model concept to defer to.

No changes to `POST /api/model/set`'s `__reset__` handler were needed — once
the resolver stopped treating a same-provider `default` as authoritative,
reset's existing delete-only behavior correctly falls through to live
main-model tracking.

Added regression tests: updated
`test_anthropic_main_models`/`test_unlisted_task_uses_block_default` in
`tests/agent/test_auxiliary_provider_first.py` to assert the new
main-tracking behavior (they previously asserted the removed cheap-fallback
behavior as the desired outcome), and added
`test_set_model_auxiliary_reset_then_resolve_tracks_main_not_stale_default`
in `tests/hermes_cli/test_web_server.py` — reset via the real endpoint, then
resolve via the real aux-task resolver and confirm every unconfigured task
returns `model=None` (defers to main), not the block's default.

### Fork-only fix — 2026-07-22 (desktop model picker hid Anthropic despite valid Claude Code credentials)

Desktop's chat model picker (`build_models_payload(explicit_only=True)` in
`hermes_cli/inventory.py`, wired through `tui_gateway/server.py`'s
`model.options` handler) was silently dropping the `anthropic` provider row
even though the CLI's `hermes model` picker showed it fine with the exact
same credentials on disk (valid Claude Code CLI OAuth tokens in Keychain /
`~/.claude/.credentials.json`, no `ANTHROPIC_API_KEY`/`ANTHROPIC_TOKEN` env
var, no `active_provider` set in `auth.json`).

Root cause: `list_authenticated_providers()` (shared substrate for both CLI
and desktop pickers) already special-cases anthropic — it treats valid
external Claude Code / Hermes-PKCE credentials as `has_creds=True` and emits
the row. But desktop's `explicit_only=True` path runs an ADDITIONAL filter,
`_filter_explicit_provider_rows()`, which re-checks every row against
`is_provider_explicitly_configured()`. That function deliberately excludes
`CLAUDE_CODE_OAUTH_TOKEN` / external credential files from counting as
"explicit" (upstream intent, PR #4210: stop aux tasks from silently burning
the user's Claude Code subscription tokens without an explicit Hermes-side
choice). The desktop filter reused that same strict gate for pure picker
*display*, so a working Anthropic session showed up in the CLI but not in
the desktop's model dropdown — the two surfaces disagreed even though
neither is wrong about the underlying credential.

**Fix:** narrow carve-out inside `_filter_explicit_provider_rows()` — when
`is_provider_explicitly_configured("anthropic")` is False, additionally check
for valid external Claude Code / Hermes-PKCE credentials (the exact same
check `list_authenticated_providers()` already performs via
`read_claude_code_credentials()` / `read_hermes_oauth_credentials()`) before
dropping the row. `is_provider_explicitly_configured()` itself is completely
untouched, so the PR #4210 aux-task gate (auxiliary tasks silently consuming
Claude subscription tokens) still works exactly as before — this only widens
what the desktop/dashboard model picker is willing to *display*. Verified
`explicit_only=True` is consumed by nothing except the desktop model-options
request path (`apps/desktop/src/lib/model-options.ts`), so there's no
downstream credential-consumption code depending on this list being narrow.

Added regression tests: `test_explicit_only_keeps_anthropic_row_when_claude_code_credentials_valid`
and `test_explicit_only_drops_anthropic_row_without_external_credentials` in
`tests/hermes_cli/test_inventory.py`.

### Fork-only fix — 2026-07-21 (desktop package.json version stuck at 0.17.0)

The desktop app's `package.json` version field was stuck at `0.17.0` while the
canonical `hermes_cli/__init__.py` was at `0.19.0` (8 releases of drift). The
runtime workaround `resolveHermesVersion()` in `electron/main.ts` reads from
`__init__.py`, so the About panel showed the right version — but the installer
DMG filename, `Info.plist CFBundleShortVersionString`, and `app.getVersion()`
all came from the stale `package.json`.

`scripts/release.py` (lines 2191–2204) already has code to bump the desktop
`package.json` in lockstep, but it only runs when `--bump` is passed and was
silently skipped for 8 releases.

**Fix:** `apps/desktop/scripts/sync-version.mjs` reads the canonical version
from `hermes_cli/__init__.py` and writes it into `package.json` before every
build, wired via the `prebuild` npm script. No manual bump needed, no
dependency on `release.py --bump` being run. Best-effort: failures log a
warning and never block the build.

**Commits:** `8c2557360` (bump), `8014756b2` (FORK.md), `[pending]` (sync script + prebuild)

### Fork-only feature — 2026-07-21 (provider-first aux-task Models-page writes were never actually provider-scoped)

Follow-up to the same-day "aux-task pin silently reverted on every save" fix
below. After that fix, the Models page's per-task "Change" control correctly
PERSISTED an assignment — but a deeper design gap remained: on a
provider-first `auxiliary` config, the write path (`POST /api/model/set`,
scope=auxiliary) and read path (`GET /api/model/auxiliary`) only ever knew
about the LEGACY top-level `auxiliary.<task>` pin shape, never the
provider-first per-provider blocks (`auxiliary.<provider_id>.<task>`). Net
effect: every aux-task reassignment from the desktop/web Models page created
a permanent, GLOBAL, cross-provider pin — e.g. assigning Vision to
`ollama-cloud/gemma4` while main=ollama-cloud would keep vision on gemma4
forever, even after switching main to Anthropic, silently shadowing the
perfectly good `auxiliary.anthropic.vision` block entry already sitting in
config. The read side had the mirror-image bug: it displayed raw top-level
dict state instead of resolving through the real runtime resolver, so a
genuine provider-block override (with no top-level pin) silently showed as
"auto" — the Models page was lying about what a task would actually run on.

**Fix — hybrid pin/block write rule** (validated via `mcp__consult` against a
naive "always write to whichever provider was selected" design, which would
have silently no-op'd any cross-provider assignment until main happened to
match later):
  * Selected provider == current active main provider → write into that
    provider's BLOCK (`auxiliary.<block>.<task> = model`). Takes effect
    immediately; naturally re-resolves to a DIFFERENT model later if the
    same task is reassigned while main is on a different provider — one
    override per (task, provider) pair, matching what the Models page rows
    visually imply on a provider-first setup. Clears any stale top-level pin
    for that task so it can't keep shadowing the block (explicit pins always
    outrank block resolution per the existing read contract).
  * Selected provider != current main, OR the assignment carries a
    `base_url`/custom endpoint (a bare block entry is a model string with no
    room for endpoint info) → falls back to the legacy top-level pin, which
    takes effect immediately regardless of active main. This is the ONLY
    shape that can express "run task X on provider Y always," so it remains
    a first-class, reachable write path — just no longer the ONLY path.
  * Reset ("Set to main" / "Reset all to main") on a provider-first config
    clears the top-level pin PLUS the task entry in the CURRENT MAIN's block
    ONLY — not every provider's block. Wiping every block would silently
    destroy hand-authored per-provider overrides meant for later (e.g.
    resetting Vision while main=ollama-cloud must not delete a deliberately
    configured `auxiliary.anthropic.vision` entry).
  * Read path (`GET /api/model/auxiliary`) now resolves each task through
    `agent.auxiliary_client._get_auxiliary_task_config` (the SAME flattener
    the runtime uses at call time) instead of a raw top-level dict lookup,
    and annotates each task with `source: "pin" | "block" | "auto"` so a
    future UI can distinguish "explicitly pinned" from "inherited from the
    active provider's block" from "no override at all" — the same task can
    legitimately show a different resolved model after main switches
    providers, and `source` is what lets a client render that without
    guessing. When the resolver returns the `provider: "auto"` sentinel
    (model-only blocks, e.g. the `anthropic` block) the response substitutes
    the real active main provider id so the UI never shows the literal
    string "auto" next to a concrete resolved model.
  * Legacy task-first configs are completely unaffected — every branch is
    gated behind the existing `_auxiliary_is_provider_first()` detector.

New shared helper `agent/auxiliary_client.py::_aux_block_key_for_provider`
factors out the provider-id → block-key normalization (exo-cluster aliasing,
`custom:` prefix stripping) so both the existing main-provider-keyed
`_aux_select_provider_block` and the new selected-provider write path share
one normalization rule instead of duplicating it.

Verified with 6 new tests in `tests/hermes_cli/test_web_server.py`
(same-provider block write, cross-provider pin write, reset scoped to main's
block only, read-path real-resolver resolution + `source` tagging) — all
pass. Ran the full `tests/hermes_cli/test_web_server.py` +
`tests/agent/test_auxiliary_provider_first.py` suite (350 passed, 136
skipped, 0 failures) and the broader aux/config/model-assignment surface
(577 passed, same 6 pre-existing unrelated failures as this morning's fix, 0
new failures).

Files: `agent/auxiliary_client.py`, `hermes_cli/web_server.py`,
`tests/hermes_cli/test_web_server.py`, `apps/desktop/src/types/hermes.ts`,
`web/src/lib/api.ts` (TS response-type parity for the new `source` field;
UI treatment of `source` is a follow-up, not done here).

### Fork-only fix — 2026-07-21 (aux-task pin silently reverted on every save)

`save_config()`'s `_strip_provider_first_aux_pollution` (added 2026-06-24 with
the provider-first `auxiliary` schema, entry below) unconditionally deleted
ANY top-level `auxiliary.<task>` key on a provider-first config, treating
every one as `DEFAULT_CONFIG`-deep-merge pollution. It never distinguished
that inert pollution (`{provider: auto, model: ''}`) from a genuine explicit
task pin — e.g. exactly what `POST /api/model/set` (scope=auxiliary) writes
when a user picks a model for a specific aux task via the desktop/web Models
page "Change" control. Net effect: reassigning ANY auxiliary task (Vision,
web_extract, compression, …) away from "auto" on Adam's provider-first config
silently reverted to auto on the very next config load/save cycle — reported
as "changing the vision model to gemma4 doesn't stick, instantly reverts back
to auto" and initially suspected as a desktop-app staleness issue, but
reproduced identically via the raw `save_config()`/`load_config()` round-trip
with no desktop app involved.

Fixed: `_strip_provider_first_aux_pollution` now only strips a task key when
it is inert, via a new local `_aux_task_pin_is_explicit()` mirroring
`agent.auxiliary_client._aux_task_pin_is_explicit` (concrete provider,
non-empty model, or a base_url — none of which the merge pollution ever
carries). This aligns the write-side stripper with the read-side contract
that `agent/auxiliary_client.py::_get_auxiliary_task_config` already
implements (its 2026-07-11 fix already honors an explicit top-level pin over
the provider block) — the two sides had drifted: reads honored a pin that
writes then silently deleted before the next read.

Verified via a temp-`HERMES_HOME` round-trip (write pin → `save_config` →
fresh `load_config` → `agent.auxiliary_client._get_auxiliary_task_config`)
that the pin now survives on disk and resolves correctly. Added
`test_strip_pollution_preserves_explicit_task_pin` to
`tests/agent/test_auxiliary_provider_first.py` (19/19 passing, up from 18).
Ran the broader aux/config/model-assignment test surface (~600 tests across
`tests/hermes_cli/`, `tests/cli/`, `tests/agent/test_auxiliary_provider_first.py`)
before and after the change — same 6 pre-existing failures both times (unrelated:
a stubbed-CLI missing `_apply_reasoning_for_new_model` in one test file, and an
unrelated base_url-persistence assertion), 0 new failures. Introduced in
`a4c788a9a` (2026-07-18, fork-only commit, never existed upstream) — no
upstream sync implication.

Files: `hermes_cli/config.py`, `tests/agent/test_auxiliary_provider_first.py`.

### Upstream sync — 2026-07-21 (v2026.7.20, 1,584 commits, 46 conflict files)

Merge-base was v2026.7.7.2; pulled 1,584 upstream commits on branch
`sync/upstream-2026-07-21` (tag `v2026.7.20`). 46 conflict files predicted by
`fork-merge-plan.py`, all resolved (parallelized across delegated subagents +
manual resolution of the highest-risk streaming/config/schema files).

**Notable resolutions:**

* `agent/anthropic_adapter.py` — kept fork's `thinking.display` omission
  (CC wire-shape parity) verbatim over upstream's `display="summarized"`
  re-add; updated `tests/agent/test_auxiliary_client.py` assertion to match.
* `agent/chat_completion_helpers.py` (5 dense blocks) — hand-merged
  `_call_anthropic()`: kept BOTH upstream's per-request-client lifecycle +
  single-writer fencing (#67142/#65991) AND the fork's SSE-ping observability,
  rate-limit header capture, message_start usage logging, and routing-header
  capture. Neither side's fix was a superset of the other — verified via
  `mcp__consult` before hand-merging. Live heartbeat display now fires BOTH
  the fork's rich diagnostic scrollback line and upstream's `_emit_wait_notice`
  live-spinner rewrite (previously two competing status writers; folded the
  recovery-ETA into the shared diagnostic suffix instead of a separate call).
* `agent/auxiliary_client.py` (12 blocks) — adopted upstream's ContextVar-based
  `_RUNTIME_MAIN_CONTEXT` / `set_runtime_main()` / `scoped_runtime_main()` /
  `reset_runtime_main()` over the fork's 2026-07-18 threading.local mechanism
  (`_rtl_get`/`_runtime_main_tls`) — strictly dominates it (isolates async
  tasks too) and other already-merged files (`turn_context.py`, `run_agent.py`,
  `gateway/run.py`) already call the ContextVar API. **`_runtime_main_tls` no
  longer exists** — any future patch referencing it should target
  `_RUNTIME_MAIN_CONTEXT`/`set_runtime_main` instead.
* `agent/turn_context.py` — collapsed a duplicate pre-restore
  `set_runtime_main()` call (fork bug: called once before
  `_restore_primary_runtime()`, once after — upstream's design calls it
  exactly once, after restoration settles the runtime).
* `hermes_state.py` — **blast-radius bug found post-merge, not in a
  conflicting hunk**: two `INSERT INTO messages` statements had 20 declared
  columns but only 19 `?` placeholders (`sqlite3.OperationalError: 19 values
  for 20 columns`), and the shared `_CONVERSATION_ROW_COLUMNS` SELECT
  constant was missing `anthropic_content_blocks` entirely (upstream added
  the constant with no awareness of the fork's column). All three fixed;
  `tests/test_hermes_state.py` went from 165 failures to 394/394 passing.
  **Lesson reinforced**: after an "additive keep-both" schema merge, grep
  every INSERT/SELECT touching the affected table for placeholder-count and
  column-list drift — the bug is rarely in the conflict hunk itself.
* `agent/auxiliary_client.py::_resolve_vision_provider_client_impl` — schema
  drift bug: `resolve_vision_provider_client(main_runtime=...)` accepted the
  param but never forwarded it to `_resolve_vision_provider_client_impl`,
  which didn't even declare it (classic "field added to one call site, not
  threaded to the next" — same bug genus as the 2026-07-15 delegate_task
  entry below). Fixed; also added a module-level vision-resolution-cache
  clear in test setup (`_clear_vision_resolution_cache()`) since 3 tests in
  `test_auxiliary_main_first.py` shared a memoization cache key and polluted
  each other's mocked results.
* `hermes_cli/config.py::_coerce_config_value` — merged fork's JSON/list-split
  coercion with upstream's string-typed-enum guard (`approvals.mode: "off"`
  must never become the YAML boolean `False`). Order matters: the string-type
  check must run BEFORE JSON parsing, or a string-typed key whose value
  starts with `[`/`{` gets silently JSON-parsed anyway.
* Tests requiring updates beyond their own conflict blocks:
  `tests/run_agent/test_streaming.py` had 2 non-conflicting mock call sites
  (`agent._anthropic_client.beta.messages.stream.side_effect/.call_count`)
  left over from before the merge — updated to the post-merge
  `.messages.stream` shape (no `.beta`) to match `_call_anthropic`'s
  resolved body.

**Verification (initial pass, ad-hoc file selection)**: full `tests/agent/` +
`tests/run_agent/` + `tests/test_hermes_state.py` targeted runs all green
except 7 tests confirmed pre-existing failures (reproduced identically on a
clean pre-merge `git worktree` at the old HEAD) — not merge regressions:
`TestAnthropicCredentialRefresh` (4 tests, `test_run_agent.py`),
`test_run_conversation_dict_returns_include_final_response`,
`test_tool_call_retry_budget_is_three_not_one`,
`test_stale_kill_increments_streak`.

**Follow-up sweep (2026-07-21, same day) — `scripts/run_tests.sh` isolation
catches what ad-hoc file selection missed:**

The initial verification pass above hand-picked files to re-test based on
what the conflict resolution touched. Running the canonical isolated-subprocess
runner (`scripts/run_tests.sh tests/agent/ tests/tools/ -j8` — one fresh
`pytest` process per file, no xdist collisions) instead surfaced 46 failures
across 18 files the ad-hoc selection never exercised. Every failure was
checked against a disposable `git worktree` (pristine upstream `v2026.7.20`,
or pre-merge fork HEAD `624340957`) before touching anything, specifically to
avoid conflating real merge regressions with pre-existing bugs this sync
happened to surface for the first time (new test files, or existing tests
against code paths nobody had run in isolation before).

**Real merge regressions found and fixed:**

* `agent/anthropic_adapter.py` — the Kimi-family adaptive-thinking guard in
  `build_anthropic_kwargs` was backwards. My earlier hand-merge of the
  streaming call path (see above) kept the fork's `_is_kimi_coding` exclusion
  verbatim without cross-referencing upstream commit `60811ced3` ("adaptive
  thinking for Kimi-family Anthropic endpoints", landed the same week),
  which removed that exclusion entirely — Kimi/Moonshot endpoints now
  support adaptive thinking like everyone else. Removed the stale guard;
  `_supports_adaptive_thinking()` already had the correct Kimi-family
  detection from the same upstream commit and needed no changes.
* `agent/fork/anthropic_messages.py` — ported upstream commit `ddd81e935`
  ("preserve thinking blocks on Kimi-family endpoints on replay") into the
  fork's separate `convert_messages_to_anthropic` (a documented hard-fork
  boundary — upstream's own equivalent function in `anthropic_adapter.py`
  is now just a forwarder into this file, so upstream's fix landed on a
  function the fork doesn't call). Live probing (per the upstream commit)
  showed Kimi For Coding (K3+) and Moonshot's Anthropic surface both issue
  AND validate their own thinking signatures — the fork's old contract
  (strip ALL signed thinking blocks for the whole Kimi family, keep only
  unsigned ones) silently discarded the model's prior chain-of-thought
  across multi-turn conversations. New contract: Kimi-family replays
  thinking blocks (signed or unsigned) completely unchanged; DeepSeek keeps
  the older strip-signed/preserve-unsigned contract (it genuinely can't
  validate Anthropic signatures, unlike Kimi).
* `tests/agent/test_set_runtime_main_custom_provider.py`,
  `tests/agent/test_auxiliary_client.py` — 2 stale references to the deleted
  `threading.local()` mechanism (`_rtl_get`/`_runtime_main_tls`, superseded
  by the ContextVar migration documented above) updated to
  `_runtime_main_value()` / a corrected docstring.
* `tests/agent/test_kimi_coding_anthropic_thinking.py` — 7 parametrized
  assertions expecting `thinking.display="summarized"` updated to match the
  fork's documented CC-wire-shape-parity decision (no `display` key present
  at all when `HERMES_THINKING_DISPLAY` is unset) — same class of test drift
  as the `test_auxiliary_client.py` fix from the initial pass.
* `toolsets.py`, `tools/delegate_tool.py` — split `swarm_run` out of the
  shared `"delegation"` toolset into its own `"swarm"` toolset (composed back
  in via `toolsets.py`'s `"includes"` mechanism, so top-level/non-delegated
  usage is unaffected). `DELEGATE_BLOCKED_TOOLS`/`_blocked_toolsets_for_role`
  only operate at whole-toolset granularity (`_strip_blocked_tools` disables
  a toolset only when ALL its tools are blocked) — `swarm_run` was added to
  the pre-existing `"delegation"` toolset when the fork's native swarm
  feature shipped, but never threaded into the blocking logic, so orchestrator
  subagents silently regained `swarm_run` alongside the intentionally-regranted
  `delegate_task` (recursive swarm delegation was never supposed to be
  allowed). Upstream's own unchanged
  `test_orchestrator_composite_regains_only_delegate_task` test caught this
  the first time the toolset actually held 2 tools instead of 1. Verified via
  `mcp__consult` that toolset-splitting (not a per-tool exclusion kwarg,
  which the resolution pipeline doesn't support) was the only fix shape that
  didn't require widening the test's own contract.
* `tests/agent/test_hot_tier_audit.py` — 2 call sites constructing
  `curator._ReviewRuntimeBinding` with 4 positional args instead of 5
  (missing `request_overrides`, added by the merge in an earlier pass).

**Pre-existing fork bugs found and fixed (NOT caused by this sync — confirmed
reproducing identically on pre-merge fork HEAD `624340957` via disposable
worktree before fixing):**

* `hermes_cli/config.py` — the auxiliary-schema migration step (task-first →
  provider-first, `current_ver < 31`) called `save_config()` directly instead
  of `_persist_migration()`, violating the documented single-choke-point
  write invariant that exists specifically to prevent the "lean config →
  full `DEFAULT_CONFIG` dump" regression (see `_persist_migration`'s own
  docstring). Existed since the migration step was added; a pre-existing
  test (`test_migrate_config_never_calls_save_config_directly`) had simply
  never been run against this code path before this session.
* `hermes_cli/config.py` (`_AUX_TASK_FIRST_KEYS`) + `agent/auxiliary_client.py`
  (`_BUILTIN_AUX_TASK_KEYS`) — the canonical task-first-vs-provider-first
  schema detector lists (documented as mirroring each other) were both stale,
  missing 6 task keys that exist in `DEFAULT_CONFIG.auxiliary`
  (`background_review`, `consult`, `goal_judge`, `memory_query_rewrite`,
  `moa_aggregator`, `moa_reference`). This made `_auxiliary_is_provider_first()`
  misdetect every unmodified default config as provider-first, which meant
  `save_config()`'s `_strip_provider_first_aux_pollution` choke point would
  silently **strip real user auxiliary task settings** (e.g. a configured
  vision provider/model, set via `hermes config` or the setup wizard) on
  every single write. This is a serious, silent data-loss bug independent of
  the sync — fixed because it was found, not because the merge caused it.
  Caught by `test_vision_picker_writes_provider_and_model` /
  `test_vision_picker_custom_endpoint`, both pre-existing tests.

**Verification (follow-up sweep)**: `scripts/run_tests.sh tests/agent/
tests/tools/ -j8` went from 46 failures/18 files to 0 new failures. Every
remaining failure (13–19, depending on which subset was run) was
independently confirmed pre-existing on pristine upstream `v2026.7.20` or
pre-merge fork HEAD via disposable `git worktree` — known flakes
(`test_concurrent_writes_never_tear_the_snapshot`, a shell-timing race),
pre-existing mock/fixture drift (`build_anthropic_client(model=...)` not
threaded into several test mocks, unrelated to this sync), and one live
network test (`test_unconfigured_search_emits_top_level_error` hits a real
search backend). A partial full-suite run (`tests/`, 13,888/~41,888 tests
collected before hitting the tool-call time budget) held at the same
19-failure baseline with no new regressions — the full ~42k-test suite
exceeds what's practical to run to completion in one session; see the
`hermes-agent-fork-development` skill's own documented pitfall about this.

**Post-merge cleanup (same day, 2026-07-21) — two minor fixes that would
otherwise be silently lost on the next sync:**

* `hermes_cli/config.py` — upstream changed the `approvals.mode` default from
  `"manual"` to `"smart"` in the v2026.7.20 release, but the line sat in a
  clean (non-conflicting) region of the file, so the merge carried the updated
  comment block ("smart (default)") while the actual `DEFAULT_CONFIG` value
  stayed `"manual"`. Fixed: `978121070`. **Merge note:** on next sync, check
  that `DEFAULT_CONFIG.approvals.mode` matches the upstream default — this is
  the kind of drift that lives in clean regions and never produces a conflict
  hunk to alert you.
* `tests/tools/test_approval.py` — pre-existing upstream test bug (reproduces
  identically on pristine v2026.7.20, not a merge regression):
  `test_nonrecursive_verification_artifact_cleanup_is_not_dangerous` hardcoded
  `"/tmp"` as both the mocked `gettempdir()` return and the operand path. On
  macOS, `tempfile.gettempdir()` returns `"/tmp"` but the OS resolves it to
  `/private/tmp` at the filesystem level; the production code's own
  `os.path.realpath(tempfile.gettempdir())` call already accounts for this
  correctly, but the test's hardcoded path never matched the realpath'd value
  it was compared against, so the exemption never fired and the test failed
  with a false "delete in root path" detection. Fixed: `45bc3c78e`. **Merge
  note:** this test file is not a soft-fork file — the fix is a one-line
  `tmp_path` change that will need re-verification if upstream rewrites the
  test.

### History squash — 2026-07-19

`main`'s 340 commits of fork-only history (vs the `upstream/main` merge-base)
were squashed into 6 commits, grouped by subsystem, with a verified
byte-identical tree before/after (`git diff <old-tip> <new-tip>` = empty).
Rationale: this file already documents every change; git history depth added
no information not already captured here, and 340 commits was getting
unwieldy. Force-pushed to `origin/main`.

The pre-squash history is preserved forever at tag
`backup/pre-squash-2026-07-19` (pushed to origin) — every short SHA cited
below still resolves there (`git show <sha>` after `git fetch --tags`), even
though `git log upstream/main..main` on current `main` now shows just 6
commits, not the commit counts quoted in a few places below (frozen at time
of writing, pre-squash). New commits on current `main` post-squash:

| SHA | Subsystem |
|---|---|
| `56c1c5417` | hard-fork modules |
| `69444061b` | core agent runtime |
| `a4c788a9a` | CLI, gateway, and run_agent |
| `be1f94f32` | tool subsystem |
| `715da117f` | plugins and scripts |
| `53f78e85b` | docs, locales, and project config |

Every pre-squash short SHA cited elsewhere in this file resolves to one of
the six squash commits above (confirmed via `git log --oneline --follow --
<path>` per file each old SHA touched):

| Old SHA | New commit |
|---|---|
| `7b6cb3f98` | `69444061b` core agent runtime |
| `0eff5e9cc` | `69444061b` core agent runtime |
| `79650d1de` | `56c1c5417` hard-fork modules |
| `efa0472954` | `69444061b` core agent runtime |
| `bc44a94f20` | `69444061b` core agent runtime |
| `8263a4c5c` | `69444061b` core agent runtime |
| `89ab0ca37` | `69444061b` core agent runtime |
| `8191519242` | `56c1c5417` hard-fork modules |
| `da796e6bd` | `69444061b` core agent runtime |
| `0285cf60c` | `69444061b` core agent runtime |
| `ecf9d12bb` | `a4c788a9a` CLI, gateway, and run_agent |
| `c5bb78547` | `56c1c5417` hard-fork modules |
| `1052432ea` | `a4c788a9a` CLI, gateway, and run_agent |
| `a026c8a74` | `a4c788a9a` CLI, gateway, and run_agent |
| `ab9c74ee4` | `69444061b` core agent runtime |
| `e6ffabb15` | `a4c788a9a` CLI, gateway, and run_agent |
| `f0adbbf8f` | `be1f94f32` tool subsystem |
| `ba0bc01d1` | `a4c788a9a` CLI, gateway, and run_agent |
| `e046afdd3` | `a4c788a9a` CLI, gateway, and run_agent |
| `fd2a35b16` | `a4c788a9a` CLI, gateway, and run_agent |
| `680b32655` | `a4c788a9a` CLI, gateway, and run_agent |
| `a730d5dc6` | `69444061b` core agent runtime |
| `2f882c9bf` | `a4c788a9a` CLI, gateway, and run_agent |
| `908ff9f25` | `a4c788a9a` CLI, gateway, and run_agent |
| `e80d8c73f` | `69444061b` core agent runtime |
| `61a1b8d6f` | `69444061b` core agent runtime |
| `b713432ab` | `be1f94f32` tool subsystem |
| `aeb00d7ae` | `be1f94f32` tool subsystem |
| `0f60943f7` | `be1f94f32` tool subsystem |
| `0f81be857` | `69444061b` core agent runtime |
| `20fb2e005` | `69444061b` core agent runtime |
| `ea0aef879` | `56c1c5417` hard-fork modules |
| `84cbae4e3` | `56c1c5417` hard-fork modules |
| `0a32275ff` | `69444061b` core agent runtime |

For full standalone-commit detail (isolated diff, original message) on any
of these, use the SHA directly against `backup/pre-squash-2026-07-19` —
e.g. `git show 7b6cb3f98`. That tag is fetched automatically with
`git fetch origin --tags`.

## What's different from upstream

### Hard-fork boundaries (zero merge conflicts ever)

These files/directories don't exist upstream and never will. Upstream merges
will never touch them.

| Path | Purpose |
|---|---|
| `agent/fork/__init__.py` | Marker module for fork-only code |
| `agent/fork/_mixin.py` | `ForkForwardersMixin` — `AIAgent` inherits it so fork-only methods (`_record_loaded_skill`, `_maybe_skill_recall_hint`, `_capture_rate_limits_from_headers`, etc.) appear on the agent while their real impls live in sibling fork modules. Keeps `run_agent.py` free of fork code → zero merge surface for these forwarders. |
| `agent/fork/skill_recall.py` | Skill-recall reminder — tracks loaded skills + nudges agent to re-check `skill_pitfalls()` before destructive ops |
| `agent/fork/memory_recall.py` | Memory-recall reminder — nudges agent to call `memory(action='recall', ...)` against the warm-tier store every N tool calls (or on explicit "remember"-style directives); auto mode runs the recall and injects the top hit. Config: `agent.memory.recall_reminder_*`. |
| `agent/fork/memory_session_pin.py` | Session-pin — keeps selected warm-tier facts visible in the system prompt for the rest of the current session (gone on restart). Exposes `memory(action='pin'/'unpin'/'pinned', fact_id=N)`. Config: `agent.memory.session_pin_max_count`/`max_chars`. |
| `agent/fork/rate_limit_tracker.py` | Rate-limit observability — one-shot INFO on first header capture, WARN on 90% bucket transitions with 80% hysteresis |
| `agent/fork/anthropic_recovery.py` | Refusal retry sanitization (strip credential-extraction shell patterns from historical context) + CC alias arg translation + `is_anthropic_refusal` detection predicate (T2.3) |
| `agent/fork/anthropic_messages.py` | The fork's ~540-line `convert_messages_to_anthropic` OpenAI→Anthropic converter (T2.2). Moved out of `anthropic_adapter.py` so upstream's converter refactors can't tangle with it. |
| `agent/fork/stream_recovery.py` | Cold-start stale-timeout computation (`effective_stale_timeout`) — the fork's grace window before the first stream event (T2.3). |
| `agent/fork/tool_search_lazy.py` | Client-side lazy MCP tool loading — name-only stubs inflated to full schemas on demand |
| `agent/fork/diagnostics.py` | Per-turn usage history + tools-signature hash + xAI 403 entitlement hint |
| `agent/fork/consult_nudge.py` | Second-opinion (consult tool) reminder — nudges the agent to call `consult(question, context)` for a review from a configurable reference model after N risky tool calls; reuses `skill_recall`'s risky-tool set. Config: `consult.nudge_interval`. |
| `agent/hot_tier_audit.py` | Hot-tier audit — heuristic stale-path detection + opt-in LLM keep/demote/stale/dead classification. On a real curator pass, reads `MEMORY.md`/`USER.md`; heuristic-only mode (default) flags/demotes entries whose extracted filesystem paths no longer exist on disk. `curator.consolidate: true` upgrades to an LLM classification pass (reuses the skill curator's aux-model binding) whose `demote` verdicts move to warm tier and `stale`/`dead` verdicts hard-delete only when `curator.prune_builtins` is also on; an LLM failure or a sanity-cap trip aborts with zero mutation rather than falling back to the heuristic. Opt-in via `curator.hot_tier_audit` (default off), `curator.hot_tier_audit_dry_run` (default on). See `docs/plans/2026-07-14-hot-tier-audit.md`. |
|| `agent/fork/anthropic_native_web_search.py` | Provider-aware web search — on first-party Anthropic (Claude) swaps the client `web_search` tool for Anthropic's native server-side `web_search_20250305` tool so search runs inline; non-Claude endpoints keep the client tool. Config: `web.anthropic_native_search` (default on), `web.anthropic_native_search_max_uses`. |
|| `agent/cc_aliases.py` | CC alias name mappings (Bash/Read/Edit/Write/Grep) for plan billing compatibility — maps Hermes built-in tool names to their Claude Code canonical equivalents so OAuth traffic counts as CC-API usage for billing. |
|| `agent/gemini_cloudcode_adapter.py` | Gemini → Cloud Code adapter for Gemini provider OAuth path. |
|| `agent/google_oauth.py` | Google OAuth credential handling for Gemini provider. |
|| `hermes_cli/fork_banner.py` | The fork's banner branding + git-state subsystem (carried/upstream-behind line, fork-aware agent name, HEAD-date label, fork-tree release URLs) (T2.5). Moved out of `banner.py`. |
|| `hermes_cli/delegation_stats.py` | Fork-only delegation statistics display (`/delegation` stats readout). |
|| `hermes_cli/keyboard_protocol.py` | Fork keyboard protocol for CLI interaction patterns. |
|| `hermes_cli/memory_confirm.py` | Memory confirmation dialogs (warm-tier memory verify-before-save). |
|| `hermes_cli/personas.py` | Fork-only persona management (`/persona` slash command). |
|| `hermes_cli/ruflo_agents.py` | Fork-only ruflo agent type catalog. |
|| `hermes_cli/submit.py` | Fork-only CLI submit flow for interactive proposal confirmation. |
|| `plugins/model-providers/exo/` | First-class exo provider profile (`custom:exo` provider type). |
|| `plugins/web/claude_code/` | Claude Code web backend for the Hermes web interface. |
|| `plugins/web/trafilatura/` | Free, no-API-key `web_extract` backend — direct `httpx` fetch (manual redirect-hop walk with per-hop SSRF/policy re-check) + the open-source `trafilatura` library for local content extraction. Closes the gap where non-Anthropic providers (exo, ollama-cloud) had a free search backend (brave-free/ddgs) but no free extract backend — every existing extract-capable provider (firecrawl/tavily/exa/parallel) needs a paid API key. |
|| `tools/bridges/` | Fork-only tool bridges (CC proxy MCP bridge). |
|| `tools/swarm_board.py` | Live SwarmBoard display for multi-agent task progress. |
|| `tools/swarm_tool.py` | Swarm orchestration tool — multi-agent parallel task execution with live board, cost tracking. |
|| `tools/hermes_load_tools.py` | Fork tool loading bridge — loads fork-only tools into agent runtime. |
|| `tools/memory_warm.py` | Warm-tier memory tool — search/recall/pin/unpin warm facts. |
|| `tools/memory_extraction/` | Memory extraction system (extractor, buffer, conflict, prompts). |
|| `tools/memory_auto_feedback/` | Memory auto-feedback module (audit and learning-ledger). |
| `tools/consult_tool.py` | Second-opinion tool — asks a configurable reference model (`auxiliary.consult`) for a review before a risky/uncertain decision; refusals/empty responses degrade gracefully to `unavailable: true` rather than erroring. Available to main agent + subagents (not in `DELEGATE_BLOCKED_TOOLS`). |
| `tools/delegation_router.py` | Cheap classifier that sorts a delegate_task goal (no explicit model/agent_type) into a capability tier (light/standard/deep) and optionally a ruflo persona, then maps tier→role→model through `delegation.model_by_role`. Fail-open everywhere. Config: `delegation.auto_route.*`, `auxiliary.delegation_router`. |
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
| `cli.py` | +2653 / -143 | Cancel-ladder keybindings, session-finalize, memory wiring, `/model --global` provider switch clears stale endpoint creds, per-model reasoning effort isolation. |
| `agent/anthropic_adapter.py` | +1784 / -93 | CC wire-shape parity: alias translation (Bash/Read/Edit/Write/Grep), `metadata.user_id` identity blob, billing header, SSE ping observer, `.beta.messages` namespace. Upstream v2026.7.1 absorbed OAuth creds, beta headers, 1M-context gate. The OAuth path is no longer fork-only. |
| `tools/delegate_tool.py` | +888 / -158 | Background-by-default delegation (adopted upstream's model), SwarmBoard, prompt-cache stagger, 1M-beta latch, cost/token rollup, `delegation.by_provider` provider-scoped config. |
| `agent/chat_completion_helpers.py` | +858 / -114 | Streaming reliability: SDK monkey-patch for SSE events, heartbeat ticks, stream-drop reconnect, cold-start detection. |
| `tools/swarm_tool.py` | +860 / -1 | Swarm orchestration: multi-agent parallel task execution with live board, cost tracking, prompt-cache management. |
| `tools/mcp_tool.py` | +743 / -98 | MCP tool registration (no `mcp_` prefix — exact server provenance map), parallel-safety fix, disk cache. |
| `agent/conversation_loop.py` | +640 / -14 | Per-turn callouts to fork modules, reasoning-channel budget-exhaustion detection, bare-XML tool-call recovery, 413 shrink-before-compress. |
| `agent/auxiliary_client.py` | +580 / -34 | Exo-scoped aux delegation, Anthropic aux 401/400 fixes, provider-matched aux model (sonnet-5), per-task fallback_model, provider-first aux config schema, 1M-beta baked-client fix, single-provider auto failover. |
| `tools/memory_tool.py` | +563 / -38 | Warm-tier memory (recall/pin/unpin), auto-feedback, session pin, skill-recall reminders. |
| `hermes_cli/config.py` | +513 / -14 | Config keys for fork features: `delegation.by_provider`, `web.by_provider`, `agent.reasoning_effort_by_model`, `auxiliary.<provider>` schema, `tools.tool_search.defer_*`, v31 migration, `get_missing_config_fields` guard. |
| `tools/swarm_board.py` | +467 / -1 | Live SwarmBoard display for multi-agent task progress. |
| `tools/memory_extraction/extractor.py` | +448 / -1 | Memory extraction with provider-first aux schema detection, per-task override support. |
| `agent/cc_aliases.py` | +306 / -1 | CC alias name mappings (Bash/Read/Edit/Write/Grep) for plan billing compatibility. |
| `hermes_state.py` | +257 / -7 | `FORK_SCHEMA_SQL` (`api_calls` table), `FORK_TABLE_COLUMNS` (`anthropic_content_blocks`), `SCHEMA_VERSION` 18. |
| `run_agent.py` | +230 / -17 | 12 forwarder methods (now `ForkForwardersMixin`), `_classify_anthropic_stream_phase`, fork-state initialization. |
| `tools/skills_tool.py` | +224 / -1 | Skill management with lazy listing support. |
| `agent/model_metadata.py` | +210 / -10 | Per-model reasoning effort, model metadata overrides, `claude-sonnet-5` context length. |
| `hermes_cli/main.py` | +194 / -20 | CLI entry point changes for fork features (model switch, session management). |
| `tools/hermes_load_tools.py` | +195 / -1 | Fork tool loading bridge. |
| `agent/image_routing.py` | +193 / -18 | Proactive image downscaling (4 MB ceiling), exo main detection via runtime base_url. |
| `tools/web_tools.py` | +187 / -28 | Multi-provider search failover chain (`web.search_chain`), native Anthropic search swap. |
| `agent/prompt_caching.py` | +167 / -18 | System prompt cache split (stable/volatile), `split_system_for_cache` / `strip_volatile_sentinel`. |
| `agent/usage_pricing.py` | +160 / -7 | Fork cost tracking (cache tiers, API-call level pricing), `claude-sonnet-5` pricing entry. |
| `agent/agent_init.py` | +146 / -7 | Fork instance state initialization (delegated to `fork.<module>.init_state`). |
| `agent/agent_runtime_helpers.py` | +141 / -23 | CC alias support in `repair_tool_call`, switch_model 1M-beta latch, swarm_run handling. |
| `agent/title_generator.py` | +133 / -41 | Title generation fixes, thinking block stripping. |
| `agent/tool_executor.py` | +129 / -12 | Skill-recall hooks, hermes_load_tools/swarm_run dispatch. |
| `hermes_cli/banner.py` | +117 / -107 | Thin forwarders to `fork_banner.py`; git-state plumbing, `_skin_branding`, `_resolve_repo_dir`. |
| `tools/tool_search.py` | +108 / -11 | Core toolset deferral (`defer_toolsets`/`defer_tools`/`keep_eager_tools`), explicit-intent activation. |
| `agent/insights.py` | +101 / -4 | Fork insights (account billing, usage stats). |
| `hermes_cli/models.py` | +95 / -1 | Provider-client cache fingerprint fix, bare `/model` config provider resolution, `claude-sonnet-5` in model catalog. |
| `agent/transports/anthropic.py` | +88 / -8 | Transport-level Anthropic wire format adjustments. |
| `tools/file_tools.py` | +70 / -5 | File tool CC alias slip-through guards. |
| `agent/account_usage.py` | +65 / -2 | Account usage tracking. |
| `tools/skill_manager_tool.py` | +60 / -1 | Skill manager tool fork additions. |
| `agent/error_classifier.py` | +59 / -1 | `FailoverReason.internal_code_error` — fail-fast on internal code bugs. |
| `agent/prompt_builder.py` | +59 / -1 | Prompt builder fork additions. |
| `agent/context_compressor.py` | +54 / -5 | `display_prompt_tokens()` — shows real provider tokens, not preflight estimate. |
| `agent/system_prompt.py` | +53 / -24 | Warm-memory status block, cache-breakpoint comments. Upstream absorbed date-only timestamp and grok guidance. |
| `agent/transports/chat_completions.py` | +50 / -2 | Chat completions transport adjustments. |
| `agent/credential_pool.py` | +37 / -19 | Keychain longlived token seeding, prunable source handling. |
| `agent/turn_context.py` | +29 / -1 | 3 ported fork-only prologue steps: memory_auto_feedback bind, `_last_user_message` capture, `_recent_tool_args` reset. |
| `agent/credential_sources.py` | +26 / -1 | `keychain_longlived` credential source. |
| `agent/conversation_compression.py` | +12 / -16 | Phase-2 auto-extraction hook (`memory_extraction.on_pre_compress`). `compress_context`'s docstring converged to upstream's fuller version 2026-07-21 (dropped the fork's trim-only divergence). |
| `agent/tool_guardrails.py` | +11 / -4 | `hard_stop_enabled` default `False→True` — tool-call loop guardrails now block/halt instead of just warning. See "Fork-only fix — 2026-07-07" below. |
| `plugins/model-providers/anthropic/__init__.py` | +2 / -2 | `default_aux_model` updated from haiku to sonnet-5. |
| `toolsets.py` | +25 / -7 | `"swarm"` toolset (`swarm_run`) split out of `"delegation"` (composed back in via `includes`) so delegation-blocking can independently gate it — see 2026-07-21 sync entry above. |

Was 314 commits of fork-only history (vs `upstream/main`, refreshed
2026-07-12 post v2026.7.7.2 sync) before the 2026-07-19 squash noted at the
top of this file; `git log upstream/main..main` on current `main` now shows
6 commits carrying the same net diff.

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
rest of this file — it leans on the fork's CC wire-shape parity path, which
is the fork-only surface that enables first-party-Anthropic detection).

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
   right after the CC alias block and before `_apply_tool_search`). This is
   the only edit to an upstream-shared file.
3. **`hermes_cli/config.py` (soft-fork).** Two `web:` keys —
   `anthropic_native_search` (default `True`) and
   `anthropic_native_search_max_uses` (default `5`).
4. **`tools/web_tools.py` (soft-fork, comment only).** Corrected the stale
   `check_web_api_key` docstring to point at the real swap site.

Tests: `tests/agent/test_anthropic_native_web_search.py` (27 — unit swap logic
+ first/third-party classification + integration through
`build_anthropic_kwargs` for native/oauth/third-party paths + wire-shape orphan
pairing for `web_search_tool_result` blocks, see follow-up below). Regression:
`test_anthropic_adapter` + `test_minimax_provider` +
`test_kimi_coding_anthropic_thinking` = 242 passed.

**Merge note:** the logic is isolated in `agent/fork/` (never conflicts). The
only upstream-shared touch is the 3-line forwarder in `build_anthropic_kwargs`
— on conflict keep ours (the `apply_native_web_search(...)` call must stay
between the CC alias block and `_apply_tool_search`). If upstream ever
implements native web search natively, converge per the "when upstream catches
up, take upstream" rule and drop this module + its forwarder + test. Until
then it stays — it depends on the fork-only first-party-Anthropic detection and CC wire-shape path and is not upstream-bound.


### Fork-only fix — 2026-06-10 (native web_search wire-shape orphan pairing)

Follow-up to the native-web-search swap above. Putting the native tool
definition on the wire surfaced a latent bug in the fork's
`convert_messages_to_anthropic` orphan-stripping pass — the swap itself was
fine, but the very next request after a successful web search 400'd:

    messages.N.content.M: unexpected `tool_use_id` found in
    `web_search_tool_result` blocks: srvtoolu_...
    Each `web_search_tool_result` block must have a corresponding
    `server_tool_use` block before it.

Root cause: `agent/fork/anthropic_messages.py`'s wire-shape orphan pass
(written for `tool_search_tool_*_tool_result` blocks) collected result IDs ONLY
from the `tool_search_tool_*` family. A `server_tool_use` paired with a
`web_search_tool_result` had its result ID missing from the set, so the use
looked orphaned and got dropped — stranding the result block as a same-message
orphan that Anthropic rejected on the next call. Pre-swap the path was dead
code (no native tool on the wire = no `web_search_tool_result` blocks to
mishandle); turning it on lit the bug.

Fix (`agent/fork/anthropic_messages.py`, hard-fork — zero conflict surface):
1. **Same-message web_search orphan pass** (new, runs before the existing
   tool_search pass). Anthropic's input validator requires
   `web_search_tool_result` to live in the same assistant message as its
   `server_tool_use`, immediately before it. Per assistant message, collect
   web_search uses and results and strip the symmetric difference — neither
   half survives without its partner. Identifies web_search `server_tool_use`
   blocks by `name == "web_search"`, so tool_search uses are untouched.
2. **Cross-message result-ID set extended** to include `web_search_tool_result`
   ids alongside `tool_search_tool_*_tool_result` ids. With (1) already
   reconciling web_search same-message pairing, the broader set just prevents
   a paired `server_tool_use` from being misclassified as orphaned by the
   pre-existing tool_search drop loop.

Tool_search behaviour is unchanged (its cross-message pairing still goes
through `_relocate_orphaned_tool_search_results` first, then the same drop
loop).

Tests: 5 new in `tests/agent/test_anthropic_native_web_search.py`
(`TestWebSearchWireShapeOrphanPairing`) — paired pair survives (direct
regression), orphan result stripped, orphan use stripped, split pair both
halves stripped, tool_search orphan-drop still works. Total in that file:
27 passed. Regression: full anthropic sweep
(`pytest tests/agent/ -k anthropic`) = 461 passed, 2 skipped.

**Merge note:** same as the parent native-web-search section — both the new
pass and the existing one live entirely in `agent/fork/anthropic_messages.py`,
which never conflicts. If a future sync converges this module back to
upstream's converter (extremely unlikely; the converter is the T2.2 hard-fork
boundary), the orphan-stripping passes must be ported across as a unit (drop
one without the other and you re-introduce this 400).


### Fork-only feature — 2026-06-18 (exo-scoped auxiliary delegation)

The local exo cluster runs DeepSeek-V4-Flash as the big chat model and
Qwen3.6-35B-A3B-8bit as a smaller sidekick. The desired routing: when the main
session is on exo/DeepSeek-V4-Flash, all auxiliary tasks (vision, compression,
memory_extraction, session_search, title_generation, curator, mcp, approval,
kanban_decomposer, profile_describer, triage_specifier, tts_audio_tags,
web_extract, models, skills_hub) should offload to Qwen3.6 on the same cluster,
freeing the big model for main reasoning. When the main session is NOT on exo
(Claude, OpenRouter, Ollama, etc.), aux tasks should follow whatever main
provider is active — the exo cluster must not get pulled into non-exo sessions.

The existing `auxiliary.<task>.provider` config override is unconditional: set
it to `exo` and every session routes its side tasks to the cluster, even when
the user switched main to Claude. So this needed a code change, not just config.

Fix (`agent/auxiliary_client.py`, soft-fork — single shared file): added an
exo-scoping guard inside `_resolve_task_provider_model`. New helper
`_aux_override_targets_exo(provider, base_url, cfg)` returns True when an
auxiliary override targets the exo cluster (by provider name `exo` /
`custom:exo`, or by a `base_url` matching `providers.exo.base_url`). When the
override targets exo AND the active main provider is itself exo (checked via
the existing `agent.image_routing._provider_is_exo`), the override is honored.
When the override targets exo but the main provider is NOT exo, the override is
dropped and the resolver falls through to `"auto"` (which follows the main
provider via Step-1 of `_resolve_auto`). This mirrors the exo-only delegate
scoping already used for vision in `agent/image_routing.py::decide_image_input_mode`.

The guard is purely additive — it only fires when the user has configured an
exo-targeted aux override. Users who never set `auxiliary.*.provider: exo` see
zero behavior change. Non-exo aux overrides (e.g. `provider: openrouter`) are
unaffected and pass through as before.

Config companion (not part of this diff — lives in `~/.hermes/config.yaml`):
`model.provider: exo`, `model.default: mlx-community/DeepSeek-V4-Flash`, and
every `auxiliary.<task>` block set to `provider: exo`, `model:
mlx-community/Qwen3.6-35B-A3B-8bit`, `base_url:
http://192.168.86.201:52415/v1`, `api_key: not-needed`.

Tests: 2 new in `tests/agent/test_auxiliary_main_first.py`
(`TestExoScopedAuxDelegation`):
- `test_exo_main_honors_exo_aux_override`: main=exo + exo aux config → override
  honored, returns the exo endpoint + Qwen model (does not fall through to
  `"auto"`).
- `test_non_exo_main_drops_exo_aux_override`: main=anthropic + exo aux config →
  override dropped, returns `("auto", None, None, None, None)` so aux follows
  the main provider.
Full file: 11 passed, 6 skipped. Broader sweep (auxiliary_client +
auxiliary_main_first + image_routing + vision_routing_31179 +
set_runtime_main_custom_provider): 329 passed, 9 skipped. The one failure
(`test_openrouter_main_vision_uses_main_model`) is the documented pre-existing
global-state-pollution flake — reproduced on clean `main` with this patch
stashed.

**Merge note:** the guard lives inside `_resolve_task_provider_model`, a
shared upstream function. If a future sync rewrites that function, the
`_aux_override_targets_exo` helper + the `if _aux_override_targets_exo(...)`
block must be ported across as a unit. The helper itself is fork-only (new,
self-contained); the only upstream surface it touches is the
`_resolve_task_provider_model` body. Config-driven: the feature is inert
without `auxiliary.<task>.provider: exo` in `config.yaml`, so upstream users
who never set it see no change.


### Fork-only fix — 2026-06-21 (Anthropic aux 401 fix + provider-matched sonnet-4-6)

Two related issues surfaced when the user hot-swapped from an exo main session
to `anthropic/claude-opus-4-8` mid-session: `/compress` 401'd immediately, and
even when the credentials would have resolved correctly, every aux task used the
main Opus model rather than a dedicated, cost-efficient aux model.

**Part 1 — Anthropic auxiliary 401 (credential leak fix)**

Root cause: `set_runtime_main()` records the live main credentials verbatim.
When the main was previously exo (`api_key: not-needed`), `_RUNTIME_MAIN_API_KEY
= "not-needed"`. After hot-swapping to Anthropic, `_resolve_auto` Step-1
threaded this stale `explicit_api_key="not-needed"` through to
`_try_anthropic()`. Inside `_try_anthropic`, the line
`token = explicit_api_key or resolve_anthropic_token()` returned `"not-needed"`
(truthy), which was then sent as the Anthropic Bearer token → guaranteed 401.

Fix (`agent/auxiliary_client.py::_try_anthropic`): at the top of the function,
sanitize `explicit_api_key` — if it does not start with `"sk-ant-"` it is a
foreign-provider placeholder and is silently discarded so the function falls
through to `resolve_anthropic_token()` (which reads the real OAuth credential
from `~/.claude/.credentials.json`). The guard is a single `if` that fires only
when an invalid key would otherwise have been used. Upstream users who never
configure an exo `api_key: not-needed` see zero behavior change; the only
callers that pass a non-"sk-ant-" value are exactly the broken exo paths this
fixes.

**Part 2 — Provider-matched auxiliary model: main=anthropic → sonnet-4-6**

Desired routing (user's words): "When I'm on an Anthropic model, ALL aux items
go to claude-sonnet-4-6, UNLESS specifically stated otherwise."

Previously, `_resolve_auto` Step-1 forwarded the main Opus model as the aux
model for every side task (compression, title generation, session search, etc.),
which both wastes quota and uses a 200K-context model for tasks that fit in 8K.
The `_API_KEY_PROVIDER_AUX_MODELS_FALLBACK["anthropic"]` haiku fallback was also
never reachable in the common case because Step-1 always won with the Opus model.

Fix: three co-ordinated changes:

1. New constant `_ANTHROPIC_DEFAULT_AUX_MODEL = "claude-sonnet-4-6"` in
   `agent/auxiliary_client.py` — the single place to update if the preferred aux
   model ever changes.

2. `_API_KEY_PROVIDER_AUX_MODELS_FALLBACK["anthropic"]` updated to reference
   the constant (was `"claude-haiku-4-5-20251001"`). This covers the explicit
   `auxiliary.<task>.provider: anthropic` (no model override) path.

3. `plugins/model-providers/anthropic/__init__.py` — `default_aux_model`
   updated from `"claude-haiku-4-5-20251001"` to `"claude-sonnet-4-6"`. The
   `ProviderProfile.default_aux_model` takes priority over the fallback dict in
   `_get_aux_model_for_provider`, so this is the load-bearing change for all
   `resolve_provider_client("anthropic")` callers.

4. `_resolve_auto` Step-1: when `resolved_provider == "anthropic"`, the model
   forwarded to `resolve_provider_client` is now `_ANTHROPIC_DEFAULT_AUX_MODEL`
   instead of `main_model`. Per-task explicit overrides (`auxiliary.<task>.model`
   in `config.yaml`) still win: they propagate as the `model=` kwarg of the outer
   `resolve_provider_client("auto", model=per_task_model)` call, which overrides
   the `resolved` model returned by `_resolve_auto`.

5. **Choke-point fix in `resolve_provider_client` (follow-up — same date):**
   The Step-1 substitution was undermined by a universal model-resolution fallback
   near the top of `resolve_provider_client`:
   ```python
   if not model:
       model = _get_aux_model_for_provider(provider) or _read_main_model() or model
   ```
   For `provider="auto"` the provider-catalog lookup returns `""`, so `model`
   becomes `_read_main_model()` = `"claude-opus-4-8"`. Later, the auto branch
   computes `final_model = model or resolved` — and since `model` is now the
   truthy `"claude-opus-4-8"`, it overwrote the `"claude-sonnet-4-6"` that
   `_resolve_auto` returned.

   Fix: capture `caller_model = model` immediately before the `if not model:`
   fallback. In the auto branch, use `final_model = caller_model or resolved`
   instead of `model or resolved`. Now:
   - Caller-supplied model (explicit `model=` arg, incl. per-task overrides) →
     `caller_model` is non-empty → wins, per-task overrides intact.
   - No model supplied (typical aux call) → `caller_model` is None → `resolved`
     from `_resolve_auto` wins → sonnet-4-6 for anthropic-main sessions.
   - Non-anthropic auto sessions → `_resolve_auto` returns their main model as
     `resolved`; `caller_model` is None → behavior unchanged.

**1M-context beta on compression (automatic — no extra wiring needed):**
`build_anthropic_client` gates `context-1m-2025-08-07` via
`_model_supports_1m_context`. `claude-sonnet-4-6` is in that allowlist and
`_base_url_needs_context_1m_beta(None)` (native Anthropic) returns True, so the
1M beta is automatically included in every aux Anthropic client built with
sonnet-4-6 — no changes to `anthropic_adapter.py` required.

These changes affect ONLY anthropic-main sessions. Exo, OpenRouter, Ollama, and
all other providers are byte-for-byte unchanged (the substitution in Step-1 is
guarded on `resolved_provider == "anthropic"`).

Tests: 6 new in `tests/agent/test_auxiliary_main_first.py`
(`TestAnthropicAuxModel`):
- `test_anthropic_main_aux_ignores_foreign_placeholder_key`: `"not-needed"`
  passed as explicit_api_key → sanitized to None → real OAuth token used, NOT
  the placeholder.
- `test_anthropic_main_aux_uses_sonnet_not_opus`: main=anthropic/opus → Step-1
  forwards sonnet-4-6, not opus, to `resolve_provider_client`.
- `test_anthropic_per_task_model_override_wins`: `auxiliary.compression.model`
  explicit override → that model is returned by `_resolve_task_provider_model`,
  not sonnet-4-6.
- `test_non_anthropic_main_unaffected`: main=exo → main model forwarded
  unchanged, no sonnet substitution.
- `test_anthropic_aux_client_carries_1m_context_beta`: sonnet-4-6 is in
  `_model_supports_1m_context` allowlist → 1M beta present in beta list.
- `test_compression_task_anthropic_main_resolves_sonnet_e2e` **(canonical
  regression — choke-point fix)**: `set_runtime_main(anthropic, opus-4-8,
  not-needed)` → `_resolve_task_provider_model('compression')` →
  `resolve_provider_client(prov, model)` — exercising the REAL path with no
  mock on `resolve_provider_client` itself. Asserts resolved model ==
  sonnet-4-6, `build_anthropic_client` called with the real OAuth token (not
  "not-needed"), and `_model_supports_1m_context(resolved)` is True.  This
  test would have caught the Step-1 bypass before the choke-point fix landed.

4 existing tests updated to match new behavior (model assertions haiku →
sonnet-4-6; explicit api key tests updated to use `sk-ant-*`-prefixed keys
reflecting that real Anthropic credentials always start with that prefix).

Full file: 17 passed, 6 skipped. Broader sweep (auxiliary or aux or anthropic or
vision or routing): 905 passed, 14 skipped. The one failure
(`test_openrouter_main_vision_uses_main_model`) is the documented pre-existing
global-state-pollution flake — passes in isolation.

**Merge note:** the changes touch three files:
- `agent/auxiliary_client.py` — three surgical edits: (1) the `_try_anthropic`
  sanitization guard; (2) the `_resolve_auto` Step-1 substitution; (3) the
  `caller_model = model` capture + `final_model = caller_model or resolved` in
  the auto branch of `resolve_provider_client`. On conflict: keep all three. The
  `caller_model` capture must appear immediately before the `if not model:` auto-
  fill block. The `final_model` line must use `caller_model`, not `model`.
- `plugins/model-providers/anthropic/__init__.py` — `default_aux_model` change.
  On conflict: always use the constant `_ANTHROPIC_DEFAULT_AUX_MODEL` value
  (`"claude-sonnet-4-6"` as of this writing); do not revert to haiku.
- `tests/agent/test_auxiliary_client.py` — 4 existing test fixes (model
  assertions + api key format). On conflict: keep `== "claude-sonnet-4-6"` in
  both model assertions and keep `sk-ant-*`-prefixed keys in the explicit-key
  tests.


### Fork-only fix — 2026-06-22 (Anthropic aux 400: thinking + temperature collision)

**Symptom:** `⚠ Auxiliary title generation failed: HTTP 400: temperature may
only be set to 1 when thinking is enabled or in adaptive mode.` Hit on
anthropic-main sessions once auxiliary tasks started resolving to
`claude-sonnet-4-6` (the 2026-06-21 provider-matched change above). Title gen,
and any aux task passing a non-1 temperature, 400'd.

**Root cause:** A two-layer collision in the native `AnthropicAuxiliaryClient`
path (`agent/auxiliary_client.py`, the `build_anthropic_kwargs` call site
~line 1036):
1. `build_anthropic_kwargs` defaults `reasoning_config=None` → **adaptive
   thinking enabled** on every 4.6+ Claude model (the line-~3489 "mirror Claude
   Code 2.1.119 wire shape" default). This was designed for the main
   conversational session, but the aux client passed `reasoning_config=None`,
   so it leaked onto one-shot utility calls too.
2. The aux client then re-attached the caller's `temperature` (title gen sends
   `0.3`), gated only on `_forbids_sampling_params(model)` — which is **False**
   for the 4.6 family. So `thinking={type:"adaptive"}` and `temperature=0.3`
   went out together. Anthropic rejects temperature≠1 under thinking → 400.

The bug was dormant while aux ran on haiku (haiku doesn't support thinking, so
no thinking block was added). It surfaced the moment aux moved to sonnet-4-6.
`call_llm` has no `reasoning_config` parameter at all, confirming no auxiliary
caller ever intends thinking — these are deterministic utility completions.

**Fix (single choke point, the one aux `build_anthropic_kwargs` call site):**
1. Pass `reasoning_config={"enabled": False}` instead of `None`, explicitly
   disabling thinking on the aux path. Restores the historical thinking-less
   behavior these tasks always had under haiku; faster + cheaper for utility
   work; honors the caller's temperature.
2. Belt-and-suspenders: the temperature re-attach is now also gated on
   `"thinking" not in anthropic_kwargs`, so if thinking is ever (re)enabled for
   an aux call, temperature is left at the server default rather than 400-ing.

**Verification:** `build_anthropic_kwargs` with `reasoning_config={"enabled":
False}` produces no `thinking` key and honors `temperature=0.3` for sonnet-4-6
/ haiku (and correctly strips it for opus-4-8 via `_forbids_sampling_params`).
Live end-to-end: `generate_title(...)` on an anthropic-main session (exo
`not-needed` placeholder, hot-swapped to anthropic) returns a clean title with
no 400. Test sweep (auxiliary or aux or anthropic or title or vision): 849
passed, 14 skipped; the lone failure is the documented pre-existing
`test_openrouter_main_vision_uses_main_model` global-state flake (passes in
isolation).

**Merge note:** single-file change in `agent/auxiliary_client.py` at the
`AnthropicAuxiliaryClient` `build_anthropic_kwargs` call site. On conflict: keep
`reasoning_config={"enabled": False}` (NOT `None`) and the `"thinking" not in
anthropic_kwargs` guard on the temperature re-attach.


### Fork-only fix — 2026-06-22 (per-task `fallback_model` — cheap Haiku for trivial aux on Anthropic-main)

**Motivation:** Cost control. With aux tasks now resolving to `claude-sonnet-4-6`
on Anthropic-main sessions (2026-06-21 change), every side task — including
trivial ones like title generation and TTS-tag rewriting — burned Sonnet-tier
quota ($3/$15 per MTok). Haiku 4.5 ($1/$5) is 3x cheaper and more than adequate
for low-stakes utility work. The user works in Opus + 1M context on Anthropic;
only `compression` genuinely needs the 1M window, and a handful of tasks
(`vision`, `curator`, `memory_extraction`, `approval`, `session_search`) want
Sonnet-tier quality. The rest can drop to Haiku.

**Constraint (hard requirement):** Main provider is sacred — `main=exo` keeps ALL
aux on the local exo cluster (Qwen, free); `main=anthropic` keeps all aux on
Anthropic. Aux follows main, never crosses providers. The cost lever must only
change *which Anthropic model* a task uses when an Anthropic session follows
main — it must not pull Anthropic into exo sessions or vice-versa.

**Why config alone couldn't do it:** Every aux task in the user's config is
pinned to exo (`provider: exo` + cluster `base_url`). The existing exo-scoped
delegation guard (`_resolve_task_provider_model`, ~line 4856) drops that pin when
`main != exo` AND wipes the model field, so on Anthropic-main the task fell
through to the single global `_ANTHROPIC_DEFAULT_AUX_MODEL` (Sonnet). The shared
`model` field can't encode both "Qwen on exo" and "Haiku on Anthropic" — and
`provider: auto` / `provider: anthropic` shapes either break exo-main (cluster
asked for a Claude model it can't serve) or force Anthropic into exo sessions.
Verified empirically across all four shape×main combinations before coding.

**Fix (`agent/auxiliary_client.py`, in `_resolve_task_provider_model`):** Read an
optional per-task `auxiliary.<task>.fallback_model`. When the exo pin is dropped
because `main != exo`, set `cfg_model = cfg_fallback_model` instead of clearing
it to `None`. So:
- `main=exo`  → exo pin honored → Qwen (unchanged, free, local).
- `main=anthropic`, task HAS `fallback_model` → that model (Haiku) on the
  main-following `auto` provider, with real OAuth creds.
- `main=anthropic`, task has NO `fallback_model` → model cleared → provider
  default (Sonnet) applies. Unchanged behavior for quality-critical tasks.

**Config applied** (`fallback_model: claude-haiku-4-5-20251001` on 8 tasks):
`title_generation`, `tts_audio_tags`, `profile_describer`, `triage_specifier`,
`kanban_decomposer`, `skills_hub`, `mcp`, `web_extract`. Left on Sonnet (no
fallback): `compression` (needs 1M), `vision`, `curator`, `memory_extraction`,
`approval`, `session_search`.

**Verification:** End-to-end against the real on-disk config (no mocks), both
mains. Anthropic-main: 8 cheap tasks → Haiku (oauth=True), 6 quality tasks →
Sonnet (oauth=True, 1M-ctx where applicable). Exo-main: all 14 tasks → exo
cluster (Qwen). 3 new regression tests in
`tests/agent/test_auxiliary_main_first.py` (exo-main keeps Qwen; anthropic-main
with fallback → Haiku; anthropic-main without fallback → model cleared → Sonnet).
Aux sweep green except the two documented pre-existing global-state ordering
flakes (`test_openrouter_main_vision_uses_main_model`,
`test_kimi_coding_skipped_falls_through_to_openrouter`), both pass in isolation
and fail identically on clean `main` with changes stashed.

**Merge note:** single-file core change in `agent/auxiliary_client.py` inside the
exo-scoped delegation guard. On conflict: keep the `cfg_fallback_model` read and
`cfg_model = cfg_fallback_model` (NOT `cfg_model = None`) in the pin-drop branch.
`fallback_model` is purely additive config — absent = old behavior.


### Fork-only feature — 2026-06-24 (provider-scoped aux fallback: `fallback_models` map)

**Motivation:** The 2026-06-22 `fallback_model` scalar fixed cost on
Anthropic-main but is structurally under-designed: it is a single model string
with no provider dimension. The pin-drop branch fires whenever `main != exo`
(not specifically `main == anthropic`), so the scalar silently assumes the
non-exo provider is always Anthropic. If `main` were ever OpenRouter / Ollama /
any third provider, that bare `claude-*` scalar would be handed to the wrong
provider and break or mis-resolve. The user correctly flagged that the config
"should be provider-scoped."

**Fix (`agent/auxiliary_client.py`, `_resolve_task_provider_model`):** Added an
optional per-task `auxiliary.<task>.fallback_models` map of `{provider: model}`
keyed by the active *main* provider id. On exo-pin-drop the aux model is chosen:
1. provider-scoped `fallback_models[<main_provider>]` (case-insensitive key match)
2. legacy `fallback_model` scalar (backward compat)
3. cleared → provider-default aux model (e.g. Sonnet on Anthropic).
A non-dict `fallback_models` is ignored (falls through to scalar). Fully
backward compatible: absent map ⇒ identical to the 2026-06-22 scalar behavior.
The exo→Qwen side stays expressed by the existing pin (`provider`/`model`/
`base_url`); the map is only consulted once that pin is dropped, so an `exo:`
key would be dead — only non-exo mains (e.g. `anthropic:`) belong in the map.

**Config applied** (both Macs — corp + personal — `fallback_models.anthropic`
set, redundant `fallback_model` scalar nulled): SONNET (`vision`, `compression`,
`memory_extraction`, `curator`); HAIKU (`web_extract`, `skills_hub`, `approval`,
`mcp`, `title_generation`, `tts_audio_tags`, `triage_specifier`,
`kanban_decomposer`, `profile_describer`, `session_search`). Note this also
moved `approval`+`session_search` from Sonnet→Haiku vs the 2026-06-22 list, per
the user's "everything except vision+compression(+memory_extraction+curator) to
Haiku" decision.

**Verification:** 7 new tests in `TestProviderScopedFallbackModels`
(`tests/agent/test_auxiliary_main_first.py`): per-provider selection,
scoped-wins-over-scalar, scalar-fallback-when-provider-absent, clear-when-no-
match-no-scalar, exo-main-ignores-map, case-insensitive key, malformed-map.
Live resolver verified on both Macs (main=anthropic → the split above). Aux
sweep: 251 passed; the one `test_openrouter_main_vision_uses_main_model` failure
is the documented pre-existing cross-file global-state flake (fails identically
on clean `main` with changes stashed; passes in isolation).

**Merge note:** additive change in the same pin-drop branch as the 2026-06-22
scalar. On conflict: keep both the `cfg_fallback_models` dict read and the
scoped→scalar→clear resolution order. The scalar path is preserved underneath,
so this strictly supersets the prior entry.


### Fork-only feature — 2026-06-24 (provider-first `auxiliary` config schema)

**Supersedes** the `fallback_models` map entry above as the *config shape* (the
resolver mechanics it relies on — exo pin-drop, 1M-beta model matching — are
unchanged). Both schemas are read; this is the preferred authoring shape.

**Motivation:** The task-first schema (`auxiliary.<task>.{provider,model,
fallback_models}`) buried the provider dimension inside each task and forced the
exo-scoping gymnastics (pin to exo, drop pin when `main != exo`, dig through
`fallback_models`). The user wanted the inverse grouping so each provider's aux
routing is visible in one place: "when we are in the exo provider we have CLEAR
distinction of what the aux tasks point to."

**Schema (provider-first):**
```
auxiliary:
  defaults:                      # per-task, provider-INDEPENDENT settings
    vision: {timeout: 120, download_timeout: 30}
    curator: {timeout: 600}
  exo:                           # provider block — keys are task→model
    provider: custom:exo
    base_url: http://…/v1
    api_key: not-needed
    api_mode: chat_completions
    default: <qwen>              # model for any task not listed in this block
    compression: <deepseek>      # per-task override
  anthropic:
    default: claude-haiku-4-5    # cheap default for unlisted tasks
    vision: claude-sonnet-4-6    # heavier tasks bumped up
    compression: claude-sonnet-4-6
    curator: claude-sonnet-4-6
    memory_extraction: claude-sonnet-4-6
```
Resolution for (task T, active main provider P): model = `auxiliary.P.T` →
`auxiliary.P.default` → provider catalog default; connection = block-level
`base_url/api_key/api_mode/provider` (model-only blocks like `anthropic` emit
`provider=auto` so the main-provider auto path + family-matched aux model +
baked betas behave exactly as before); per-task settings = `auxiliary.defaults.T`.

**Implementation (`agent/auxiliary_client.py`):** one choke-point. The schema is
detected and flattened in `_get_auxiliary_task_config` — the single function all
accessors (`_resolve_task_provider_model`, `_get_task_timeout`,
`_get_task_extra_body`, the gateway env-bridge) already funnel through.
`_aux_flatten_provider_first` emits the SAME flat `{provider,model,base_url,…}`
dict the task-first path produced, so the entire downstream resolver (incl. the
exo-scoping guard and 1M-beta matching) is untouched. New helpers:
`_aux_schema_is_provider_first` (pollution-robust detector — see merge note),
`_aux_select_provider_block`, `_aux_flatten_provider_first`,
`_BUILTIN_AUX_TASK_KEYS`.

**Migration (`hermes_cli/config.py`):** `convert_auxiliary_to_provider_first()`
collapses task-first → provider-first (most-common model per provider becomes
the block `default`, minority tasks get explicit entries, `fallback_models[p]`
→ provider `p`'s block, legacy `fallback_model` scalar → `anthropic` block,
per-task settings → `defaults`). Wired as config **v30 → v31** migration step.
Idempotent. `get_missing_config_fields()` skips the `auxiliary` subtree when the
user's config is provider-first, else the task-first DEFAULT_CONFIG re-injects
all 15 `auxiliary.<task>` blocks as `{provider:auto,model:''}` pollution on every
migrate.

**DEFAULT_CONFIG stays task-first** (both copies: `hermes_cli/config.py` +
`cli.py`) — deliberately, to avoid perturbing fresh installs / upstream shape
and minimize merge surface. Provider-first is purely a *user-config* shape the
reader understands.

**Verification:** 12 tests in `tests/agent/test_auxiliary_provider_first.py`
(detector incl. pollution-survival, anthropic/exo resolution, defaults-timeout
preservation, block-default fallback, unit flatten, converter collapse +
idempotency). **Behavior-preserving proof:** Adam's real config resolved for all
15 tasks × {anthropic-main, exo-main} BEFORE conversion == AFTER (30/30 exact,
provider+model+base_url+api_mode). Live config migrated on the corp Mac (v31),
re-migrate is a clean no-op (aux keys = `[anthropic, defaults, exo]`, no
re-pollution). Broad sweep: 602 passed across aux/config/curator/vision/kanban
suites; the lone `test_openrouter_main_vision_uses_main_model` flake is the
documented pre-existing cross-file global-state issue (mocks
`_resolve_task_provider_model`, so upstream of this change; passes in isolation
+ on clean `main`).

**Merge note:** core changes in `agent/auxiliary_client.py` (one rewritten
function + 4 new helpers, all additive below `_DEFAULT_AUX_TIMEOUT`),
`hermes_cli/config.py` (converter + v31 step + `get_missing_config_fields`
guard + version bump to 31), `gateway/run.py` (env-bridge routed through the
flattener). On conflict: the flattener is the load-bearing piece — keep
`_get_auxiliary_task_config` dispatching on `_aux_schema_is_provider_first`. The
detector MUST treat task-key presence as a non-signal (the DEFAULT_CONFIG merge
always injects them); positive markers are a `defaults` key or a known
provider-id key only.


### Fork-only fix — 2026-06-22 (Anthropic aux 400: Haiku request carries Sonnet-only context-1m beta)

**Symptom:** With the `fallback_model`→Haiku change live, the first aux task to
fire (title generation) failed with `HTTP 400: The long context beta is not yet
available for this subscription`. Reproduced on every one of the 8 cheap Haiku
tasks — they share one resolution path, title-gen just fires first.

**Root cause:** The Anthropic SDK client bakes its `anthropic-beta` headers into
`default_headers` *at construction*, based on the model it's told it will serve
(`build_anthropic_client(..., model=...)` → `_model_supports_1m_context`). The
aux `auto` path built the client for the WRONG model:

1. `_resolve_task_provider_model` returns `provider='auto', model='claude-haiku-…'`.
2. `resolve_provider_client('auto', haiku)` → `_resolve_auto` Step 1, which
   built the Anthropic client for `step1_model = _ANTHROPIC_DEFAULT_AUX_MODEL`
   (`claude-sonnet-4-6`). Sonnet IS in the 1M allowlist, so the client baked
   `context-1m-2025-08-07` into its headers.
3. Back in `resolve_provider_client`, `final_model = caller_model` (Haiku) — so
   the request went out as Haiku against a client carrying the Sonnet-only 1M
   beta. Haiku has no 1M tier → 400.

The per-task model never reached the client builder; the model-gate
(`_model_supports_1m_context`) was correct but was being fed Sonnet, not Haiku.

**Fix (`agent/auxiliary_client.py`, three threaded params):**
- `_resolve_auto(..., preferred_model=None)` — Step 1 uses
  `preferred_model or _ANTHROPIC_DEFAULT_AUX_MODEL` for the Anthropic branch
  instead of always the Sonnet default.
- `_try_anthropic(..., model_override=None)` — builds the client for
  `model_override or _get_aux_model_for_provider('anthropic')`.
- `resolve_provider_client`'s `anthropic` branch passes the requested `model`
  into `_try_anthropic(model_override=model)`; the `auto` branch passes the
  caller's `caller_model` as `preferred_model`.

Net: the client is always built for the model that actually serves the request,
so the baked betas match. Haiku → no context-1m (400 gone); Sonnet/quality
tasks → context-1m preserved (unchanged).

**Verification:** Live Haiku title-gen call against real OAuth creds returns
generated text with no 400. Baked-beta audit across all 14 aux tasks vs the real
config: 8 Haiku → context-1m absent, 6 Sonnet → context-1m present, 0
mismatches. Exo-main unchanged (all tasks still resolve to the cluster). New
regression test `test_haiku_fallback_client_does_not_carry_1m_beta_e2e` exercises
the real `resolve_provider_client` + real `build_anthropic_client` path; verified
RED on pre-fix code (client built for Sonnet) and GREEN after. Full aux suite
green except the two documented pre-existing global-state ordering flakes.

**Merge note:** additive params only (`preferred_model`, `model_override`, both
default `None`). On conflict, keep all three threading points: `_resolve_auto`
signature + its anthropic `step1_model` branch, `_try_anthropic` signature + its
`model = model_override or …` line, and the two call sites in
`resolve_provider_client`. With all three None, behavior is identical to before.


### Fork-only fix — 2026-06-22 (auto aux + cheap pin gets main-model fallback on single-provider setups)

**Symptom:** On an Anthropic-only setup (Max subscription, no third-party aux
keys), once cheap per-task models are pinned (`fallback_model`→Haiku for
title_generation / skills_hub / mcp / web_extract / vision), a rate-limit / 402 /
connection error on the cheap aux model made the task **fail outright** — there
was no second model to catch it.

**Root cause:** In `call_llm`'s capacity-error failover, the `is_auto` branch
called ONLY `_try_payment_fallback`, which walks the third-party provider chain
(`openrouter → nous → local/custom → api-key`). The in-code comment claimed
"Step 1 IS the main agent model, so users on `auto` already get main-model
fallback" — but that equivalence only holds when the task has NO per-task model
pin. With a cheap `fallback_model` pin, the *initial* attempt uses Haiku and the
failover walks only the (empty, for this user) third-party chain — the main
provider is never re-tried. The `_try_main_agent_model_fallback` safety net
existed but was wired ONLY into the explicit-provider `else` branch, not `auto`.

**Fix (`agent/auxiliary_client.py`, one branch):** in the `is_auto` path, after
`_try_payment_fallback` returns nothing, also call
`_try_main_agent_model_fallback` — guarded by
`(final_model or "") != (_read_main_model() or "")` so a task that already
resolves to the main model doesn't pointlessly retry the same model against the
same rate-limited backend. Net: a rate-limited Haiku aux call now falls back to
the *current* main model (e.g. Opus) on the same provider/creds and completes,
instead of failing. Multi-provider `auto` users are unaffected (the third-party
chain still runs first); tasks whose model == main model are unaffected (guard
skips the redundant retry).

**Why "stay within Anthropic / use the current main model" falls out for free:**
`_try_main_agent_model_fallback` resolves `_read_main_provider()` +
`_read_main_model()` live at failover time, so the fallback is always whatever
main model is selected in the moment (Anthropic→Anthropic). No separate config.

**Verification:** RED/GREEN confirmed — the new
`test_auto_task_with_cheap_pin_falls_back_to_main_model` FAILS on pre-fix code
("all fallbacks exhausted", raises) and PASSES after; the guard test
`test_auto_task_no_cheap_pin_skips_redundant_main_fallback` asserts the
same-model case skips the redundant fallback. Live unmocked
`_try_main_agent_model_fallback('auto', …)` resolves `claude-opus-4-8` /
`main-agent(anthropic)`. Full aux suite green except the one long-documented
vision global-state ordering flake (`test_openrouter_main_vision_uses_main_model`
— fails identically on clean pre-change code, passes in isolation).

**Merge note:** single additive branch in `call_llm`'s `is_auto` failover. On
conflict, keep the two added lines (the `if fb_client is None and (final_model
or "") != (_read_main_model() or ""):` guard + the
`_try_main_agent_model_fallback` call) inside the `if is_auto:` block. Behavior
for multi-provider auto users and same-model tasks is unchanged.


### Fork-only feature — 2026-06-22 (opt-in deferral of core toolsets via tool_search)

**Problem.** The progressive-disclosure tool-search system (`tools/tool_search.py`)
only ever deferred MCP + non-core plugin tools: `is_deferrable_tool_name` hard-
refused to defer anything listed in `toolsets._HERMES_CORE_TOOLS`. That core list
includes the entire `browser` (10 tools), `homeassistant` (4), `cronjob`,
`swarm_run`, `text_to_speech`, and `vision_analyze` surfaces — ~21KB / ~5.3K
tokens of schema shipped on **every** request even in sessions that never touch
them. There was no config lever to lazy-load them; the only alternative was
disabling the toolset entirely (static, not dynamic).

Note this is a DIFFERENT system from `agent/fork/tool_search_lazy.py` /
`_apply_tool_search` (the `tool_search.additional_deferred` path). That one only
shrinks the Anthropic wire payload at request-build time and is invisible to
`agent.tools`, so it moves neither `hermes prompt-size` nor the CLI context
read-out, and its stubs route through `hermes_load_tools` which isn't always in
the visible list. The system patched here (`get_tool_definitions` →
`assemble_tool_defs`) physically removes deferred tools from `agent.tools` and
replaces them with the `tool_search`/`tool_describe`/`tool_call` bridge — so the
saving shows up in both the prompt-size report and the live context counter, and
recovery goes through the bridge that's already in the visible list.

**Change.** Added three optional config keys under `tools.tool_search`
(all default empty → upstream behavior byte-for-byte unchanged):

* `defer_toolsets` — registry toolset names (e.g. `browser`, `homeassistant`,
  `tts`, `vision`, `cronjob`) whose tools defer even though they're core.
* `defer_tools` — individual tool names to force-defer (e.g. `swarm_run` without
  deferring the rest of its `delegation` toolset).
* `keep_eager_tools` — individual names that must NEVER defer, overriding the
  above (e.g. keep `delegate_task` eager while deferring its sibling `swarm_run`).

Precedence in `is_deferrable_tool_name(name, config)` (highest first): bridge
tools → keep_eager_tools → defer_tools → defer_toolsets → upstream base rule.
`classify_tools` loads the config once and threads it through. `should_activate`
gained an explicit-intent branch: a non-empty defer list activates tool search
regardless of the `auto` threshold (but `enabled: off` still wins as a global
kill switch).

**Files.** `tools/tool_search.py` (soft-fork: dataclass fields +
`_str_frozenset` helper + `is_deferrable_tool_name`/`classify_tools`/
`should_activate`), `hermes_cli/config.py` (3 default keys under
`tools.tool_search`). Tests: `tests/tools/test_tool_search.py`
(`TestForkDeferToolsets`, `TestForkActivationIntent`, plus config-parse cases).

**Result.** With `defer_toolsets: [browser, homeassistant, tts, vision, cronjob]`,
`defer_tools: [swarm_run]`, `keep_eager_tools: [delegate_task]`:
`hermes prompt-size` drops from 39 tools / 70.5KB to 21 tools / 49.4KB
(~21KB / ~5.3K tokens off every turn that doesn't use those tools). Deferred
tools remain fully reachable via the bridge (`tool_search` → `tool_describe` →
`tool_call`), verified end-to-end. One-time cache-break + bridge round-trip the
first time a deferred tool is used in a session.

**Merge note.** `tools/tool_search.py` becomes a soft-fork file. On conflict,
keep the FORK precedence block in `is_deferrable_tool_name`, the `config` param
on `classify_tools`, the explicit-intent branch in `should_activate`, and the
three dataclass fields. The base-rule tail must stay last so upstream's
core-protection still applies to everything not explicitly opted in.


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


### Upstream sync — 2026-06-22 (1193 commits, 24 conflicts)

Merge-base was 2026-06-10; pulled 1193 upstream commits on branch
`sync/upstream-2026-06-22` (tag `pre-upstream-sync-2026-06-22` at ada09d3b2).
Largest sync to date. 24 conflict files, all resolved. The `uv.lock` merge
driver (`uvlock-ours`) was registered on this clone first
(`./scripts/setup-merge-drivers.sh`). Notable points:

* **HEADLINE — the two-billing-mechanisms collision (`anthropic_adapter.py`
  `build_anthropic_kwargs`).** The fork's CC-alias mimicry (renames the 5
  builtins `terminal`→`Bash`, `read_file`→`Read`, `patch`→`Edit`,
  `write_file`→`Write`, `search_files`→`Grep` via `cc_aliases.HERMES_TO_CC` +
  the `x-anthropic-billing-header` block) and upstream's GH-25255 `mcp__`
  normalization (everything→`mcp__`, with a `normalize_response` reverse-map in
  `transports/anthropic.py`) BOTH rewrite the same OAuth builtin tool names for
  the same plan-billing goal — incompatibly. A tool can be `Bash` OR
  `mcp__terminal`, not both, and applying `mcp__` first silently DEFEATS the CC
  mimicry. **Resolved to keep BOTH signals** (user decision: "port correctly /
  lose nothing"): `_to_oauth_wire_name` carries a skip-set of CC-aliased builtins
  + their CC-canonical targets (`HERMES_TO_CC` keys|values) + `web_search`, which
  pass through untouched so the later `replace_with_cc_canonical` step owns them;
  `mcp__` normalization applies ONLY to genuine MCP/other tools (`slack_*`,
  `mcp_*`, `session_search`, …). **MERGE-NOTE for future syncs: keep the skip-set.
  `web_search` MUST stay in it** — `apply_native_web_search` matches the literal
  name to swap in Anthropic's native server-side tool; prefixing it first breaks
  native search. Updated the two fork adapter tests + the two upstream
  `mcp_prefix_strip` tests (their `read_file`/`terminal` examples are CC-aliased
  here, swapped to `session_search`).
* **`hermes_state.py` shared-helper fork-column loss.** Upstream extracted message
  insertion into a NEW shared `_insert_message_rows()` (used by `replace_messages`
  + `archive_and_compact`) that omitted the fork's `anthropic_content_blocks`
  column. Threaded the column THROUGH the helper so all paths preserve thinking-
  signature blocks. SCHEMA_VERSION → 18 (max(fork 17, upstream 16)+1). Migration
  ladder keep-both (fork v13 api_calls + upstream v16 delegate-tag).
* **`auxiliary_client.py` — adopted upstream's `create_anthropic_message()`
  helper** (SSE-only-gateway stream aggregation) over the fork's
  `.beta.messages.create()`. The `anthropic-beta` HEADER rides in
  `default_headers` from `build_anthropic_client` regardless of namespace — BUT
  the fork's CC-mimicry also attaches beta-ONLY *body* kwargs
  (`context_management`, `output_config`, `thinking` in the CC 2.1.x shape) that
  ONLY `client.beta.messages.*` accepts. Plain `.messages.create()` 400s/TypeErrors
  with `unexpected keyword argument 'context_management'`. **Follow-up fix
  9440019ff:** `create_anthropic_message` now prefers the `.beta.messages`
  namespace when present (falls back to `.messages` for non-Anthropic-SDK clients
  / mocks), so it keeps upstream's stream aggregation AND accepts the fork body
  fields. MERGE-NOTE: keep that `getattr(client, "beta", ...).messages or
  client.messages` selection on conflict. Also: kept the
  fork's `_try_main_agent_model_fallback` safety net for single-provider auto
  setups, layered after upstream's new `_try_configured_fallback_chain` +
  `_try_main_fallback_chain` (upstream's chains SKIP the main provider, so the
  fork net is still needed for Anthropic-only users with a cheap pin); threaded
  both `preferred_model` (fork) and `task` (upstream) into `_resolve_auto`.
* **`agent/fork/anthropic_messages.py` — ported upstream's #19798 security fix.**
  The verbatim `anthropic_content_blocks` replay carried the LIVE (un-redacted)
  tool_use input; re-source each tool_use's `input` from the already-redacted
  `tool_calls` map (keyed by id) so secrets can't leak back on the fast path.
* **`tools/delegate_tool.py` — background-model port (10 hunks).** Adopted
  upstream's background-by-default delegation (`_execute_and_aggregate()` wrapper,
  `subagent.text` events, `child_timeout` default None, `background` param
  deprecated/ignored) and ported the fork's synchronous SwarmBoard stack (live
  board, prompt-cache stagger, 1M-beta latch, detailed cost/token rollup) INTO it.
  Guard added: `if child_timeout is not None and _idle_secs > child_timeout` (None
  is now the default). Cost-rollup block must sit at function-body indent (after
  the if/else), not inside the batch `else:` — else the single-task path skips it.
* **keep-both / converged elsewhere:** `system_prompt.py` (fork sentinel cache-
  split + upstream truncation-warning drain + new `_resolve_platform_hint`);
  `credential_pool.py` (took upstream's `_is_prunable` superset; kept fork
  keychain-longlived branch); `context_compressor.py` (took upstream prose
  wholesale — fork never customized it; credential-paraphrase instruction lives
  elsewhere, verified intact); `hermes_cli/models.py` (kept fork `google-gemini-cli`
  branding + upstream's NEW `google-antigravity` provider); `hermes_constants.py`
  (kept fork "max" reasoning-effort + upstream's home-helper functions);
  `cli.py`/`hermes_cli/config.py` (independent-function keep-both); the memory
  dispatch in `agent_runtime_helpers.py`/`tool_executor.py` (fork warm-tier
  `raw_target` + upstream batch `operations`); `agent_init.py` import + state
  init keep-both.
* **Post-merge triage:** full `tests/agent/` showed 87 failed vs the ~11
  documented-flake baseline. Baselined at the pre-sync tag (worktree run: 11
  failed / 4250 passed) vs post-merge (4604 passed) — the jump tracked upstream's
  +354 new tests amplifying a pre-existing in-memory model-catalog cache ordering
  weakness (the hermetic conftest isolates disk/HERMES_HOME but not in-process
  module globals). Per-file isolated runs: all passed 100% except ONE genuine
  failure (auxiliary_client `.beta` mock), which was fixed. The pollution values
  are real model context lengths (256000/1000000), not garbage — the tell.

Soft-fork divergence vs `upstream/main` after this sync (refreshed line counts):
`anthropic_adapter.py` +1783/-680, `chat_completion_helpers.py` +797/-213,
`conversation_loop.py` +466/-355, `auxiliary_client.py` +291/-220,
`credential_pool.py` +124/-94, `hermes_state.py` +372/-553, `run_agent.py`
+254/-243, `system_prompt.py` +52/-150, `tool_executor.py` +172/-82,
`agent_runtime_helpers.py` +202/-249, `tools/delegate_tool.py` +977/-559,
`tools/memory_tool.py` +548/-285. Was 244 commits of fork-only history at
the time; see the 2026-07-19 history-squash note at the top of this file
for how commit history is organized on current `main`.


### Fork-only fixes — 2026-07-01 (DSv4-local reliability sweep + aux/status-bar bugs)

A run of bugs that made local DSv4 (exo) sessions feel broken, plus a
systematic audit that turned up siblings. All root-cause fixes, no mitigations.

* **`agent/fork/diagnostics.py` missing imports (`79650d1de`)** — the module
  used `logging` (except handler in `record_usage_history`) and `hashlib`
  (`tools_signature`) but imported only `json` + `datetime`.
  `record_usage_history()` runs every completed turn → `_tools_signature()`
  → `hashlib` NameError → the except handler then hit `logging` NameError,
  which escaped and killed the whole API turn. Misclassified retryable, so it
  burned 3 retries then killed the session with 0 tool calls — presenting as
  flaky DSv4/exo behavior when model + server were fine. Verified with a real
  `hermes chat` on exo: before = died turn 1, Messages:1, 0 tools; after =
  Messages:18, 8 tool calls, finish_reason=stop. This was invisible to raw
  endpoint probes / ollama-cloud comparison because those never exercise the
  fork's response-handling path — only driving an actual `hermes` session
  reproduced it.

* **7 more undefined-name NameErrors from a pyflakes+AST audit (`efa0472954`)**
  — same bug class in executable (non-annotation) code:
  `conversation_loop.py` `_strip_cache_control()` was called on the
  overloaded-retry path but never defined (lost in a refactor port; restored
  from orig commit `bc44a94f20`); `chat_completion_helpers.py` called
  `cleanup_vm`/`cleanup_browser`/`_classify_anthropic_stream_phase` bare
  instead of via the `_ra()` lazy run_agent accessor; `auxiliary_client.py`
  `build_anthropic_client(model=final_model_str)` referenced a nonexistent var
  (should be `final_model`); missing `import re` in `agent/fork/tool_search_lazy.py`
  and `plugins/platforms/sms/adapter.py`; `plugins/google_meet/cli.py` nested
  closure referenced except-var `e` after the handler scope cleared it.
  Type-only undefined names in lazy annotations (future-annotations / quoted /
  TYPE_CHECKING) were left as-is. Audit technique: `pyflakes` + AST triage
  (SAFE = annotation/TYPE_CHECKING context; DANGEROUS = runtime statement).

* **`agent/error_classifier.py` fail-fast on internal code bugs (`8263a4c5c`)**
  — a Python builtin exception from a bug in our own API-call path (NameError,
  ImportError, …) had no status/body/message pattern, so it fell through to
  `FailoverReason.unknown` (retryable=True). The retry loop re-ran the identical
  broken code, reproduced the identical exception, and burned every retry —
  which is exactly what masked the diagnostics NameError above. Added
  `FailoverReason.internal_code_error` + `_INTERNAL_CODE_ERROR_TYPES` frozenset
  (NameError/UnboundLocalError/ImportError/ModuleNotFoundError/
  NotImplementedError/SyntaxError/IndentationError), matched by exact type name
  AND isinstance, checked AFTER the transport heuristic (so OSError/
  ConnectionError/TimeoutError stay retryable). Deliberately EXCLUDES
  AttributeError/TypeError/KeyError/IndexError/ValueError (can arise from a
  malformed provider response a retry may fix). `conversation_loop.py` aborts
  internal errors immediately with an accurate message + full traceback
  (`exc_info`), no wasted fallback, returns the standard failed-result dict.
  Tests: `TestInternalCodeError` in `tests/agent/test_error_classifier.py`.

* **`agent/auxiliary_client.py` + `agent/image_routing.py` — exo main detected by
  runtime base_url (`89ab0ca37`)** — launching on exo via `--provider exo`
  normalizes `agent.provider` to bare `custom`; the live endpoint is recorded in
  the runtime-main state, but `config.model.base_url` still holds the saved
  default (e.g. Anthropic). `_provider_is_exo("custom")` compared against that
  STALE config base_url, never matched, so the aux resolver failed to select the
  exo provider block and every aux task (memory_extraction, curator,
  title_generation, …) crossed over to another provider's model pointed AT the
  exo endpoint — e.g. `claude-haiku-4-5` → `http://<exo>/v1` → 404, silently
  killing memory extraction + curator on every exo session. Added
  `get_runtime_main_base_url()` accessor; `_provider_is_exo` now prefers the
  LIVE runtime base_url for a bare-`custom` runtime. Verified: aux tasks resolve
  to `custom:exo` (Qwen3.6 / DSv4). NOTE: this also fixed **vision** — it routes
  to `custom:exo` Qwen3.6-35B-A3B-8bit (which IS vision-capable; exo reports the
  `vision` capability) with no separate vision model needed.

* **`tools/memory_extraction/extractor.py` — don't force the stale default model
  under provider-first aux schema (`8191519242`)** — `_get_extraction_config`
  always read `auxiliary.memory_extraction.model` (the legacy task-first key)
  and fell back to `_DEFAULT_MODEL="claude-haiku-4-5"` when absent. Under the
  provider-first schema that key never exists, so the extractor passed an
  explicit `model="claude-haiku-4-5"` to `call_llm` — OVERRIDING the
  provider-first resolution and sending an Anthropic model name to the exo
  endpoint → 404 on every extraction. Fix: detect provider-first via
  `_aux_schema_is_provider_first` and return `model=None`/`provider=None` so
  `call_llm(task="memory_extraction")` resolves correctly (exo → Qwen3.6);
  per-task settings still come from `auxiliary.defaults.memory_extraction`.
  Verified end-to-end: extraction resolves to `custom:exo (Qwen3.6-35B-A3B-8bit)`,
  zero 404s, real proposals extracted and buffered. (Companion exo change: JIT
  enabled so Qwen auto-loads for aux — see the exo repo.)

* **`cli.py` + `agent/context_compressor.py` — status bar shows real provider
  tokens, not the preflight estimate (`da796e6bd`)** — the context counter
  (X/1M) and the Δ segment spiked mid-turn then snapped back to a smaller number
  on the SAME prompt (e.g. `528K / Δ+57.5K new` → `475K / Δ+4.62K new`). Not a
  real balloon: the bar read `compressor.last_prompt_tokens`, which
  `turn_context.py` ratchets UP to the rough char/4 preflight estimate
  (`estimate_request_tokens_rough` over messages+system+tools) so preflight
  compression can fire before send. That estimate overcounts schema-heavy /
  heavily-cached requests (real usage: input=2, cache_read≈485K/turn), so the
  displayed size jumped to the estimate then `update_from_response` overwrote it
  with the true provider count. Added `ContextCompressor.display_prompt_tokens()`
  returning `last_real_prompt_tokens` (written ONLY from real API usage, clamped
  0 for the post-compression transitional turn); pointed all three display sites
  (status bar, Δ baseline, `/usage` summary) at it; parked
  `last_real_prompt_tokens=-1` at compression. Preflight compression logic
  unchanged. Tests: `TestDisplayPromptTokens` in `test_context_compressor.py`;
  committed the previously-uncommitted context-delta status-bar tests too.


## Why a fork

Adam closed PR #25234 upstream in early 2026 — it included ~28K LOC of fork
divergence framed as a single bugfix, which was visible and embarrassing.
Lesson learned: anything that lives on this fork stays here, even when it
looks generally useful.

Specific things that **must never** be sent upstream:

* Claude Code wire-shape parity (`anthropic_adapter.py` — CC alias translation, metadata identity blob, billing header, SSE observer, `.beta.messages` targeting)
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
(Claude Code wire-shape parity, MCP disk-cache, claude-code web backend, memory/skill-recall) —
those stay.

**Target the latest RELEASE TAG, not `upstream/main`** (user preference, locked
2026-06-22). NousResearch `main` carries unreleased bleeding-edge commits; sync
to the highest published `v2026.*` release tag instead. Only merge
`upstream/main` directly if the user explicitly asks for latest-main.

Per merge:

```bash
git fetch upstream --tags && git checkout -b sync/upstream-$(date +%F)
SYNC_TARGET=$(git tag -l 'v2026.*' --sort=-version:refname | head -1)
echo "Syncing to $SYNC_TARGET"       # confirm with the user before merging
python scripts/fork-merge-plan.py    # predicts conflict files before you touch anything
git merge "$SYNC_TARGET"             # release tag, NOT upstream/main
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
  Still take "ours" for CC wire-shape edits (alias translation, metadata blob, billing header, SSE observer). Tool naming: the fork DELIBERATELY does
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

Plus fork additions to shared upstream test files:

* `tests/agent/test_auxiliary_main_first.py` — `TestExoScopedAuxDelegation` (2 tests, 2026-06-18 exo-scoped aux delegation guard).

All other tests come from upstream.

## When to update this doc

* New fork feature lands → add to the "Hard-fork boundaries" table.
* Upstream merge changes the file-level divergence numbers significantly →
  update "Soft-fork edits" numbers.
* Fork feature converged away (upstream now has equivalent) → remove from
  hard-fork table, update soft-fork entry, add dated entry below.
* The "Why a fork" rules change → update them, but always document the reason.

Don't let this file go stale. If `git log --oneline | head -20` shows fork
commits but FORK.md doesn't reflect them, fix that.

### Upstream sync — 2026-07-04 (v2026.7.1, 1,760 commits, 32 conflicts)

Merge-base was v2026.6.19; pulled 1,760 upstream commits on branch
`sync/v2026.7.1` (tag `v2026.7.1`). 32 conflict files, all resolved.

**New fork features this sync:**

* `plugins/model-providers/exo/` — first-class exo provider profile (was
  falling through to generic `custom` provider).
* `agent/web_search_registry.py` — `_read_web_config_key()` checks
  `web.by_provider.<current_provider>` before falling back to top-level
  `web.*_backend` keys, so search/extract backends auto-switch based on the
  active main provider.

**Fork features preserved (no upstream equivalent):**

* Provider-scoped delegation (`delegation.by_provider`)
* Provider-scoped web routing (`web.by_provider`)
* Exo provider profile + exo-scoped auxiliary delegation
* Native Anthropic web search swap (`agent/fork/anthropic_native_web_search.py`)
* Anthropic aux 401 fix (foreign-placeholder-key guard)
* System prompt cache split (stable/volatile)
* Image ingestion ceiling (proactive resize)
* MCP parallel-safety (no `mcp_` prefix check)
* CC alias arg slip-through guards (`file_tools`)
* Keychain longlived token seeding
* Skill recall / memory recall reminders
* Bare XML tool-call recovery
* Tool search deferral (lazy MCP loading)
* Per-model reasoning effort isolation
* SSE monkey-patch + heartbeat ticks (streaming)
* Cold-start stale-timeout grace window (`agent/fork/stream_recovery.py`)

**Upstream features adopted (additive, not replacements):**

* MoA aggregator cost model
* api_mode-aware client replacement
* `conversation_history_after_compression` helper
* MIME transcoding for unsupported image formats
* `strip_think_blocks` in title generation
* `make_tool_result_message` + `_flush_session_db_after_tool_progress`
* `_last_known_cwd` tests (#26211)
* Format compatibility tests (AVIF/TIFF/BMP/SVG transcode)
* `_sync_anthropic_entry_from_credentials_file` tests
* Petdex mascot animation
* `_config_version: 32`


### Fork-only fixes — 2026-07-06 (status-bar timer + approval timeout semantics)

1. **`0285cf60c` — status-bar timer no longer zero-pads minutes.**
   `cli.py` status-bar timer formatted `{_m:02d}m` so 2m19s rendered as
   `02m19s`. Changed to `{_m}m` (seconds keep `:02d` for width stability:
   `2m05s`). Same commit also fixed vision auto-detect in
   `agent/auxiliary_client.py` — `_resolve_vision_provider_client_impl`
   was falling back to `main_model` (DSv4-Flash, text-only) for the
   vision-support check, ignoring the configured
   `auxiliary.ollama-cloud.vision` model (gemma4:31b). The check failed
   and fell through to the aggregator chain (OpenRouter/Nous) which had
   no keys. Now `resolved_model` (from config) takes priority, so the
   configured vision model is used for the support check.

2. **`ecf9d12bb` — approval timeout no longer reported to model as "user
   denied."** `tools/approval.py` + `acp_adapter/permissions.py` +
   `agent/transports/codex_app_server_session.py` + `cli.py`. An approval
   prompt that times out was reported to the model as "BLOCKED: User
   denied this command / the user has explicitly rejected it." That is
   false — the user never answered (AFK, prompt unseen) — and it poisons
   the rest of the conversation: the model then refuses legitimate later
   re-requests for the same action because it believes the user already
   said no. Timeouts still fail closed (command NEVER runs), but the
   model-facing message now says what happened: "NOT RUN: ... timed out
   with no response — this is NOT a denial", keeps the #24912 contract
   ("Silence is not consent: do not run this or any equivalent command
   without approval"), and explicitly permits re-requesting approval
   later. `prompt_dangerous_approval()` returns a new `'timeout'` value
   on expiry (distinct from `'denied'`). Localized across all 15
   `locales/*.yaml`. Tests: `tests/acp/test_permissions.py`,
   `tests/tools/test_approval.py`.


### Fork-only fix — 2026-07-07 (tool-call loop guardrails now block by default)

The guardrail system in `agent/tool_guardrails.py` already had all the logic
to detect and block tool-call loops — tracking exact-failure counts, same-tool
failure streaks, and idempotent no-progress repetition — but
`hard_stop_enabled` defaulted to `False`, so it only ever appended warning
text to tool results. The model could (and did) ignore the warning and keep
retrying the same failing call.

**Change:** `hard_stop_enabled` default `False → True` (one line).

**Effect:**
- **5 identical failed calls** → `before_call()` returns `action="block"`,
  synthetic error injected, tool never executes
- **8 same-tool failures** (even with different args) → `after_call()` returns
  `action="halt"`, turn stops
- **5 identical idempotent results** (read_file, search_files, etc.) →
  `before_call()` returns `action="block"`, synthetic error injected

**Opt-out:** `tool_loop_guardrails.hard_stop_enabled: false` in config.yaml
restores the old warn-only behavior.

**Merge note:** this is a single-line default change in an upstream-shared
file. If a future sync reverts it to `False`, the fork's intent is to keep
`True` — the guardrails are useless without enforcement. The docstring and
4 tests were also updated to match the new default.

Verification: 347 fork-specific tests pass (8 skipped — pre-existing macOS
`/tmp` vs `/private/tmp` symlink issue).


### Fork-only feature — 2026-07-07 (consult tool + periodic nudge)

**`c5bb78547` — `tools/consult_tool.py` + `agent/fork/consult_nudge.py`.**

New `consult(question, context)` tool lets the agent (main or delegated
subagent) get a second opinion from a configurable reference model
(`auxiliary.consult` in config.yaml) before a risky or uncertain
decision. Routes through the shared `agent.auxiliary_client.call_llm`
plumbing. Refusals, empty responses, and content-filter stops from the
reference model degrade gracefully to `{"unavailable": true, "reason":
"..."}` instead of raising — expensive frontier models used as
reviewers (e.g. Fable-class) refuse often enough that this has to be a
first-class outcome, not an error path.

- Registered in the `"consult"` toolset, added to `_HERMES_CORE_TOOLS`
  (available to main agent by default), NOT added to
  `DELEGATE_BLOCKED_TOOLS` so subagents inherit it.
- `agent/fork/consult_nudge.py` — periodic reminder that nudges the
  agent to call `consult(...)` after N risky tool calls (reuses
  `skill_recall`'s risky-tool set). Config: `consult.nudge_interval`.

Tests: `tests/tools/test_consult_tool.py`, `tests/test_consult_nudge_reminder.py`.


### Fork-only fixes — 2026-07-07 (clarify/approval panel rendering + /usage + estimator + spinner)

1. **`1052432ea` — clarify/approval/sudo panels garbled on wide-glyph
   content.** `cli.py`. The modal panel renderers (clarify, approval,
   sudo, secret) padded row content with `str.ljust()`, which counts
   Python codepoints, not terminal display cells. Wide glyphs (emoji,
   CJK, box-drawing) render as 2 terminal cells but are 1 Python
   character, so any row containing one under-padded relative to the
   panel's border width (computed independently via
   `_panel_box_width`). The row's right border landed one or more
   columns short of the top/bottom border rules, visually shifting/
   clipping that row relative to its neighbors. Most common trigger:
   LLM-emitted emoji in clarify choices (✅ Yes / ❌ No), or a CJK
   question forwarded from a non-English user. Tests:
   `tests/cli/test_cli_approval_ui.py`, `tests/cli/test_panel_cwidth_padding.py`.

2. **`a026c8a74` — NameError in /usage cost reporting.** `cli.py`.
   `_show_usage()` referenced `cache_read_tokens` and
   `cache_write_tokens` when building the `CanonicalUsage` for cost
   estimation, but never defined them — only `input_tokens` /
   `output_tokens` / `reasoning_tokens` were pulled from the agent. Every
   `/usage` call and session-end exit-summary cost line crashed with
   `NameError`. Pull both from the agent's
   `session_cache_read_tokens` / `session_cache_write_tokens` counters,
   matching the existing pattern for the other token buckets.

3. **`ab9c74ee4` — estimator quadruple-counted Anthropic thinking
   blocks.** `agent/model_metadata.py`. When an assistant message
   carries `anthropic_content_blocks` (the interleaved-thinking replay
   channel), the `reasoning` / `reasoning_content` / `reasoning_details`
   fields are pure duplicates of the same thinking text already inside
   `anthropic_content_blocks` — the API replay path
   (`_convert_assistant_message` in `agent/anthropic_adapter.py`) reads
   `anthropic_content_blocks` alone for these turns and never touches
   the other three. Both rough-estimate char counters
   (`_estimate_message_chars` and
   `_count_message_chars_with_image_token_credit`) were walking all
   four copies, so every thinking block was counted ~4x. With
   interleaved thinking + high reasoning effort this inflated the
   preflight compression estimate far past the real provider-reported
   `prompt_tokens`, firing compaction the status bar gave no indication
   was imminent.

4. **`e6ffabb15` — spinner elapsed timer not fixed-width past 60s.**
   `cli.py`. `_render_spinner_text()`'s ≥60s branch formatted
   `"{m}m{s:02d}s"` with no padding, so single-digit minute counts (e.g.
   `1m05s`, 5 chars) were one character shorter than every other value
   in that branch and shorter than the <60s branch's fixed
   `"{elapsed:5.1f}s"` (6 chars). The comment claimed fixed width to
   avoid status-line wrap jitter while scrolling/repainting, but the
   single-digit-minute case (the first ~9 minutes of every long-running
   tool call) violated it. `rjust(6)` closes the gap without
   reintroducing the zero-padded-minutes look the comment explicitly
   rejected.

5. **`f0adbbf8f` — dangling `toolsets` references after upstream removal.**
   `tools/delegate_tool.py`. Upstream (`ba0bc01d1`) removed the
   model-facing `toolsets` arg from `delegate_task()` — subagents always
   inherit the parent's toolsets, not have them chosen by the model.
   That merge left two stale references to the now-undefined local
   `toolsets` name, both crashing the single-task delegate_task path
   with `NameError: name 'toolsets' is not defined`: the task-list
   construction (single-goal path) still built `{"toolsets": toolsets,
   ...}` and the per-task `_build_child_agent` call still passed
   `toolsets=t.get("toolsets") or toolsets`. Both now pass
   `toolsets=None`, matching upstream's fix and `_build_child_agent`'s
   documented behavior (`None` → pure parent inheritance).


### Fork-only test fixes — 2026-07-07 (deterministic suite, no behavior change)

1. **`e046afdd3` — isolate status-bar tests from operator's local skin
   config.** `tests/cli/test_cli_status_bar.py`. `cli.py` runs
   `init_skin_from_config(CLI_CONFIG)` at import time, which reads the
   real `~/.hermes/config.yaml` on whatever machine runs pytest and sets
   the module-level `_active_skin` singleton in
   `hermes_cli/skin_engine.py`. Any operator with a non-default
   `display.skin` (this machine has a custom skin overriding
   `status_glyph`) had two status-bar tests fail purely because of
   their local environment. Autouse fixture pins the `"default"` skin
   for the duration of the test file and restores whatever was active
   afterward. Also updates `test_show_usage_omits_cost_reporting`,
   which encoded upstream's `fd2a35b16` removal of all `/usage` cost
   reporting — this fork deliberately diverges from that commit
   (`680b32655` and follow-ups keep a `display.show_cost` opt-in and
   per-session cost lines in `_show_usage()`).

2. **`a730d5dc6` — stop hardcoding stale Anthropic model literal.**
   `tests/agent/test_auxiliary_client.py`. Two tests asserted
   `model == "claude-sonnet-4-6"` but the fork's aux-model default
   (`_ANTHROPIC_DEFAULT_AUX_MODEL`) has since moved to
   `"claude-sonnet-5"`. Import and assert against the constant instead
   of a frozen literal.

3. **`2f882c9bf` — flaky race in modal-paint repaint assertion.**
   `tests/cli/test_cli_approval_ui.py`. `TestModalPaintNow._drive()`
   asserted `app.invalidate()` had been called immediately after the
   modal state dict appeared, but the background callback thread sets
   state several statements before actually calling `_paint_now()` —
   `_fire_attention_signals()` runs in between and does real
   synchronous work (stdout write/flush, and on darwin a real
   `subprocess.Popen` for `osascript`). The assertion could win that race
   against any of the three modal types (approval/clarify/sudo),
   reproduced failing nondeterministically across repeated runs. Poll
   for the actual paint within the existing 2s deadline instead of
   asserting on the first state-dict sighting.


### Fork-only fixes — 2026-07-07 (tool_search sticky activation + anthropic replay name sync)

1. **`908ff9f25` — make progressive-disclosure activation sticky per
   conversation.** `tools/tool_search.py` + `model_tools.py` +
   `agent/agent_init.py` + `acp_adapter/server.py` + `tools/mcp_tool.py`.
   `tools/tool_search.py` recomputed the activate/deactivate decision
   for the tool_search/tool_describe/tool_call bridge fresh on every
   API call by walking the live, global tool registry singleton. When
   the deferrable-token total shifts across the threshold
   mid-conversation (MCP reconnect, a subagent loading tools, etc.),
   activation can flip from on to off between turns of the SAME
   conversation. When it flips off, the bridge tool names vanish from
   the wire tools array, and Anthropic rejects prior-turn `tool_use`
   blocks referencing them — `_strip_unknown_tool_blocks` then rewrites
   those blocks into inert text breadcrumbs, corrupting tool-call
   history even when the model successfully used the tool moments
   earlier. Confirmed live via `agent.log`: the same session
   accumulated 4 → 18 → 34 rewritten blocks over ~10 minutes as
   tool_search flapped on and off. Added a one-way sticky latch: once
   bridge tools are ever shown to an agent, they stay shown for the rest
   of that conversation. Tests: `tests/tools/test_tool_search.py`,
   `tests/agent/test_anthropic_adapter.py`.

2. **`e80d8c73f` — keep replayed tool_use name in sync with resolved
   dispatch name.** `agent/transports/anthropic.py`.
   `normalize_response()` captured OAuth-wire `tool_use` blocks twice:
   once into `tool_calls` (name correctly reversed from `mcp__<name>`
   back to the registry name) and once into `ordered_blocks`, the
   verbatim replay copy persisted as
   `provider_data["anthropic_content_blocks"]` whenever a turn
   interleaves signed thinking with tool_use (e.g. every clarify call).
   The reversal was never mirrored onto `ordered_blocks`, so the
   replayed history kept the raw `mcp__<name>` wire name forever. On the
   next turn, `_strip_unknown_tool_blocks()` compared that stale wire
   name against the live (bare) tool set, found no match, and rewrote
   the historical `tool_use`/`tool_result` pair into a lossy
   400-char-truncated "tool no longer available" breadcrumb — silently
   mangling the user's real answer and corrupting the model's view of
   its own prior turn. Tests: `tests/agent/test_anthropic_mcp_prefix_strip.py`.

3. **`61a1b8d6f` — resolve mcp__-prefixed bridge tool names in replay
   history.** `agent/transports/anthropic.py`. `e80d8c73f` synced the
   resolved dispatch name onto the `ordered_blocks` replay copy, but
   the resolution itself only checked `tools/registry.py` — which never
   contains `tool_search`/`tool_describe`/`tool_call`. Those three are
   dynamically synthesized bridge tools (`tools/tool_search.py`)
   dispatched by a name-check in `agent/tool_executor.py`, not
   registered `ToolRegistry` entries, so the registry lookup (and its
   bare/single-underscore fallbacks) always missed for them and `name`
   fell through unresolved. Reproduced live 2026-07-07 22:53-23:15
   (session `20260707_225321_554b40`), AFTER `e80d8c73f` had already
   landed: `agent.log` showed "rewrote N tool_use/result block(s) for
   tools no longer available: ['mcp__tool_call', 'mcp__tool_search']"
   climbing 1→20 over ~20 minutes in one ongoing conversation. The fix
   extends the resolver to recognize the three bridge tool names. Tests:
   `tests/agent/test_anthropic_mcp_prefix_strip.py`.


### Fork-only features — 2026-07-07 (delegate auto-route to model tier + persona)

1. **`b713432ab` — auto-route delegated tasks to the right model tier.**
   `tools/delegation_router.py` + `tools/delegate_tool.py` +
   `agent/auxiliary_client.py` + `hermes_cli/config.py`. When a
   `delegate_task` task states NEITHER an explicit model NOR an
   `agent_type`, a cheap classifier (`auxiliary.delegation_router`) sorts
   it into a capability tier (light/standard/deep), which maps
   tier→role→model through the existing `delegation.model_by_role` map.
   Lets a cheap main chat model fan work out onto the right-sized model
   automatically instead of every child silently inheriting the (cheap)
   parent model. Precedence: per-task `model` > `agent_type` role-map >
   auto-route > `delegation.model` > parent's model. Fail-open
   everywhere (classifier down, timeout, bad output, unmapped role,
   non-Anthropic provider) → task falls through to today's behavior,
   never worse than status quo. Every routing decision is surfaced in
   the result metadata + the per-subagent completion line so a routing
   choice is always visible, never a silent substitution. Tests:
   `tests/test_delegation_router.py`.

2. **`aeb00d7ae` — auto-route can also pick a ruflo persona
   (agent_type).** Extends the tier-only auto-router: the same single
   classifier call may now also pick a persona when it's a clearly
   better fit than the generic tier role, restricted to personas that
   already resolve to a model via `delegation.model_by_role`. A
   confident pick feeds into the same `task_agent_type` variable an
   explicit caller-supplied `agent_type` uses, so it gets both existing
   effects for free (persona-prompt injection + per-role model
   resolution) with no duplicated logic. Hallucinated/unknown names are
   validated against the real catalog and dropped. New
   `delegation.auto_route.classify_persona` config gate (default `True`)
   disables persona picks while keeping tier/model routing. Fail-open
   and precedence rules unchanged. 19/19 router tests pass, 266
   passed / 0 failed across the broader delegate-related suite.


### Fork-only fix — 2026-07-10 (consult: reject degenerate reference-model answers)

**`0f60943f7` — `tools/consult_tool.py`.** A local aux model answered a
consult with the consult request itself wrapped in raw DSML tool-call
markup; the orchestrator then paraphrased its own words as the
reference model's opinion and the user acted on a fabricated
consultation. Detect both failure shapes and return
`unavailable: true` with an explicit do-not-paraphrase reason instead:

- DSML sentinel with tool-call structure, or a leading chat-template
  control token.
- >70%-contiguous echo of the submitted question+context.

Regression tests include the observed garbage verbatim. Tests:
`tests/tools/test_consult_degenerate_guard.py`.


### Fork-only fix — 2026-07-11 (auxiliary: honor explicit top-level task pins in provider-first schema)

**`0f81be857` — `agent/auxiliary_client.py`.** An `auxiliary.<task>`
block carrying explicit routing (concrete provider, model, or
`base_url`) is a TASK PIN, not a provider block. Previously in a
provider-first config it was never selected
(`_aux_select_provider_block` only matches main-provider ids), so the
pin was dead config and the task silently resolved to the main
provider's block default. Observed: `auxiliary.consult {provider:
anthropic, model: claude-fable-5}` ignored — consult answered by
exo/Qwen3.6 on exo-main sessions and ollama gemma4 on ollama-main
(`agent.log` 2026-07-09), i.e. a local aux model impersonating the
configured Fable 5 reference. The inert `{provider: auto, model: ''}`
deep-merge pollution is explicitly NOT a pin (test covers this). Pin
routing replaces block routing wholesale (routing keys + model dropped
before merge) so a block `base_url` can't leak under the pin's provider
and trigger the downstream `base_url`→custom coercion. Tests:
`tests/agent/test_auxiliary_provider_first.py`.


### Fork-only fix — 2026-07-12 (suppress thinking-progress overlay when reasoning is streaming)

**Uncommitted — `agent/chat_completion_helpers.py` (lines ~3487-3510).**

The "🧠 Thinking — N chars (+M in last 30s)" heartbeat pulse fired every
30s (`_HEARTBEAT_INTERVAL`) precisely when `_thinking_delta_chars > 0`
— i.e. while reasoning text is actively streaming to the display via
`agent._fire_reasoning_delta()` → `agent.reasoning_callback` →
`_stream_reasoning_delta`. The streamed reasoning IS the progress
signal; the overlay landed on top of the text the user was reading,
breaking the flow of the output.

Fix: gated the progress pulse on `agent.reasoning_callback is None`.
When reasoning is visible (CLI with `show_reasoning: true`, or any
driver with a live reasoning box), the callback is set — overlay
suppressed, reasoning text flows uninterrupted. When reasoning is NOT
shown (gateway with reasoning off, batch, quiet), the callback is
`None` and the pulse stays as the only progress signal. The "⏳ Still
waiting on provider" stall path is untouched — a zero-char delta still
emits it, which is a genuine signal regardless of display mode.

**Merge note:** single conditional wrapper around an existing
`_emit_status` call in an already-soft-fork file. On conflict keep ours
and re-verify the `agent.reasoning_callback is None` guard is intact.


### Upstream sync — 2026-07-12 (v2026.7.7.2, 405 commits, 18 conflicts)

Merge-base was `v2026.7.1` (2026-07-04); pulled 405 upstream commits on
branch `sync/v2026.7.7.2` (tag `v2026.7.7.2`). 18 conflict files, all
resolved. Safety tag: `pre-upstream-sync-2026-07-12`.

**Conflict resolution summary (18 files, 84 blocks):**

- `agent/auxiliary_client.py` — kept fork's `caller_model` capture
  (needed for provider-matched substitution) + adopted upstream's
  `provider != "auto"` guard (prevents stale-model/fallback-provider
  pairing).
- `agent/conversation_compression.py` — adopted upstream's new
  `_compress_context_via_codex_app_server` (additive, Codex thread
  compaction).
- `agent/image_routing.py` — kept fork's exo-scoped vision delegation
  (fork-only: non-exo aux backends don't reroute vision-capable models).
- `agent/prompt_caching.py` — kept fork's system-split helpers
  (`_system_text`/`_strip_system_sentinel`/`_apply_split_system_marker`)
  + adopted upstream's `_can_carry_marker` carrier check (skips empty
  messages that would waste cache breakpoints).
- `agent/transports/chat_completions.py` — kept fork's custom-provider
  reasoning handling (exo `enable_thinking`, Nous tags, Ollama
  `num_ctx`, Qwen portal `vl_high_resolution_images`).
- `agent/web_search_registry.py` — kept fork's `_read_web_config_key`
  (`web.by_provider` routing) + adopted upstream's
  `_disabled_web_plugin_for` helper (diagnoses disabled-plugin case).
- `gateway/run.py` — kept fork's per-model reasoning effort map +
  adopted upstream's `or ""` removal fix (YAML `false` no longer
  coerced to `""`, silently re-enabling thinking).
- `hermes_cli/config.py` — kept fork's `interrupt_key`/`bell_on_prompt`/
  `notify_on_prompt` + adopted upstream's `busy_steer_ack_enabled`/
  `deny` rules + v32→v33 delegation concurrency migration
  (`max_async_children` folded into `max_concurrent_children`).
- `hermes_state.py` — `SCHEMA_VERSION` 18→19 (upstream bumped). Kept
  fork's v13 migration (api_calls CASCADE recreate). Merged
  `anthropic_content_blocks` (fork) + `active` (upstream) into the
  messages INSERT column list (18 columns, 18 placeholders).
- `tools/approval.py` — adopted upstream's converged timeout/deny
  handling (unified `outcome` field + `deny_reason` relay). The fork's
  separate `choice == "timeout"` branch was a divergent reimplementation
  of the same feature; upstream's version is the superset.
- `tools/delegate_tool.py` — kept fork's SwarmBoard pre-register +
  adopted upstream's `DaemonThreadPoolExecutor` (replaces
  `ThreadPoolExecutor` so abandoned workers don't block interpreter
  exit on parent interrupt).
- `tools/file_tools.py` — adopted upstream's container-path handling
  via `_expand_tilde` (supersedes fork's `RuntimeError` guard —
  `_expand_tilde` uses `os.path.expanduser` internally, which already
  handles the `HOME`-unset case safely).
- `tools/mcp_tool.py` — kept fork's no-`mcp_`-prefix naming convention
  (`sanitize_mcp_name_component`, fork-only MCP parallel-safety fix) +
  adopted upstream's `_is_recycled_stdio()` check for the check fn.
  Added `_is_cache_shell` slot/flag to `MCPServerTask` to distinguish
  disk-cached shells (True) from parked servers (False) — this unifies
  the fork's cache-shell invariant (check=True for cache shells) with
  upstream's parked-server handling (check=False after failed
  reconnect). Added recycled-stdio reconnect path to
  `_resolve_live_server` and the tool handler (was only in upstream's
  `_get_connected_server_for_call`, which the fork doesn't use).
- `tools/memory_tool.py` — kept fork's warm-tier dispatch + adopted
  upstream's `target: null` clarification (strict providers fill
  optional schema fields with JSON null).
- `tools/web_tools.py` — kept fork's search-chain failover
  (`_get_search_chain`/`_run_search_chain`) + adopted upstream's
  `_LEGACY_WEB_BACKENDS`/`_registered_web_provider`/`_disabled_web_plugin_for`
  diagnostics. `check_web_api_key` returns the configured backend's
  availability directly (early return) when a backend is explicitly
  configured, preventing the Anthropic-native fallback from masking a
  misconfigured backend.
- `tests/agent/test_image_routing.py` — kept fork's exo-scoped tests.
- `tests/tools/test_mcp_dynamic_discovery.py` — kept fork's MCP naming
  (`my_srv_my_tool`, no `mcp__` prefix).
- `tests/tools/test_mcp_tool.py` — kept fork's MCP naming. Updated
  cache-shell emulations to set `_is_cache_shell = True` (the
  check fn now uses this flag rather than the permissive
  `server is not None`).

**Fork features preserved (no upstream equivalent):**

- Provider-scoped delegation (`delegation.by_provider`)
- Provider-scoped web routing (`web.by_provider`)
- Exo provider profile + exo-scoped auxiliary delegation
- Exo-scoped vision delegation (image_routing)
- Native Anthropic web search swap
- System prompt cache split (stable/volatile)
- Image ingestion ceiling (proactive resize)
- MCP parallel-safety (no `mcp_` prefix) + cache-shell invariant
- CC alias arg slip-through guards
- Skill recall / memory recall reminders
- Per-model reasoning effort isolation
- SSE monkey-patch + heartbeat ticks (streaming)
- Cold-start stale-timeout grace window
- Search-chain failover (`web.search_chain`)
- Thinking-progress overlay suppression (reasoning-callback gate)
- Consult tool + degenerate-answer guard
- Delegation auto-router (model tier + persona)
- Tool_search sticky activation

**Converged to upstream (when upstream catches up, take upstream):**

- `tools/file_tools.py` — fork's `RuntimeError` guard for
  `Path.expanduser()` superseded by upstream's `_expand_tilde()`
  (uses `os.path.expanduser` internally, same safe fallback).
- `tools/approval.py` — fork's separate `choice == "timeout"` branch
  superseded by upstream's unified timeout/deny handling with `outcome`
  field + `deny_reason` relay.

**Post-merge test fixes:**

- `tests/tools/test_approval.py` — updated timeout-message assertion to
  match the converged "BLOCKED: Command timed out" format (same
  fail-closed + no-consent invariant, different prefix).
- `tests/tools/test_mcp_tool.py` — set `_is_cache_shell = True` on mock
  servers that emulate the cache-shell state.

**Verification:** 520/520 MCP + approval + dynamic-discovery tests pass
(1 skipped — pre-existing macOS `/tmp` vs `/private/tmp` symlink issue
in `test_edit_approval`). All 18 conflict files syntax-OK.


### Fork-only fix — 2026-07-14 (Claude Code Keychain write-back on OAuth refresh)

**`20fb2e005` — `agent/anthropic_adapter.py` + `tests/conftest.py`.**

**Symptom:** on macOS with Hermes in Claude-Code-credentials mode (both
`ANTHROPIC_TOKEN` and `ANTHROPIC_API_KEY` empty), Hermes intermittently
401'd ("invalid x-api-key") on tokens that had not expired, AND Claude
Code itself demanded `/login` at every launch. Both symptoms, one cause.

**Root cause:** Anthropic's OAuth refresh rotates the refresh token
(single-use). Claude Code >=2.1.114 on macOS reads/writes its credential
in the Keychain ("Claude Code-credentials"); Hermes's refresh path
(`_write_claude_code_credentials`) wrote the rotated credential only to
`~/.claude/.credentials.json`. Every Hermes refresh therefore stranded
the Keychain's refresh token. Claude Code's next launch retried the
stranded token, Anthropic's reuse detection revoked the **whole token
family**, and both consumers died at once. Each `/login` seeded a new
family; `read_claude_code_credentials()` prefers the fresher store, so
Hermes adopted it and broke it again ~1h later. Permanent loop.

**Fix:** new `_sync_claude_code_credentials_to_keychain()`, called at the
end of `_write_claude_code_credentials`. Mirrors the refreshed
`claudeAiOauth` payload into the Keychain entry so both consumers stay on
one shared token family.

- **Update-only, never creates the entry.** On hosts where the Keychain
  entry was deliberately deleted so the JSON file is the single source
  (headless/SSH-only machines — e.g. the macbook-m4 setup), this stays a
  no-op.
- Payload travels to `security -i` over **stdin**, not argv, so the token
  never appears in `ps`.
- Merges into the existing Keychain JSON (scopes/extra fields preserved
  when the refresh response omits them).
- All failures degrade to `logger.debug` — credential refresh never
  breaks because the Keychain write did.

**Test-suite guard (learned the hard way):** the first test run after the
patch clobbered the real Keychain entry with fixture data —
`test_anthropic_adapter.py` calls `_write_claude_code_credentials()`
directly with a tmp `Path.home()`, but the Keychain sync targets the real
Keychain regardless of home. Added autouse fixture `_keychain_write_guard`
in `tests/conftest.py` that no-ops the sync for every test. Any future
test that wants to exercise the sync must monkeypatch it back in
explicitly.

**Merge note:** upstream absorbed the OAuth credential read path in
v2026.7.1 but still writes refreshes file-only. If a future sync rewrites
`_write_claude_code_credentials`, re-attach the
`_sync_claude_code_credentials_to_keychain()` call at its tail — without
it the /login-every-launch loop returns on any macOS host running both
Hermes and Claude Code.

**Verification:** forced a live refresh through `_refresh_oauth_token` —
Keychain and file converge on identical access+refresh tokens, scopes
preserved; live API call authenticates (429 rate-limit, not 401). 309
adapter/keychain/oauth-flow/credential-pool tests pass (2 pre-existing
`test_credential_pool.py` disk-merge failures also fail on `main`);
Keychain verified untouched after the run.


### Fork-only fix — 2026-07-14 (Bearer clients no longer leak env ANTHROPIC_API_KEY as x-api-key)

**`agent/anthropic_adapter.py` — `build_anthropic_client` + the Entra ID
bearer-hook builder.**

**Symptom:** hermes 401'd ("invalid x-api-key") on Anthropic even though
`resolve_anthropic_token()` resolved a perfectly valid Claude Code OAuth
credential, `~/.hermes/.env` was clean, and the same token worked via
curl. Error banner said "Auth method: Bearer (OAuth/setup-token)" — which
was true, and misleading.

**Root cause:** the OAuth/bearer branches set `kwargs["auth_token"]` and
leave `api_key` unset. The Anthropic SDK constructor then auto-reads
`ANTHROPIC_API_KEY` from `os.environ` and sends it as an `x-api-key`
header **alongside** `Authorization: Bearer`. The server evaluates the
x-api-key header and rejects the whole request. Trigger was a long-dead
OAuth token exported as `ANTHROPIC_API_KEY` in the kitty terminal
process's environment — inherited by every tab, invisible to `.env`
resolution, and shadowing nothing until the SDK picked it up. Reproduced
directly: valid Bearer + stale x-api-key → 401 "invalid x-api-key";
Bearer alone → 200.

**Fix:** after constructing the client on a bearer path (auth_token set,
api_key not passed), null out `client.api_key` so the SDK cannot attach
x-api-key. Applied to both `build_anthropic_client` and the Entra ID
bearer-hook builder (whose Authorization is rewritten per-request by an
httpx hook, but which had the same silent x-api-key leak).

**Merge note:** if upstream rewrites the client builders, re-apply the
`client.api_key = None` guard on every path that authenticates via
`auth_token`. The SDK's env auto-read is constructor behavior and cannot
be suppressed by passing `api_key=None` (None triggers the env read).

**Verification:** with `ANTHROPIC_API_KEY=<dead token>` poisoned into the
environment, `build_anthropic_client(resolve_anthropic_token())` now has
`api_key=None` and a live `messages.create` succeeds. 218
adapter/keychain/oauth-flow tests pass.


### Fork-only fix — 2026-07-14 (content-filter trigger patterns weren't scrubbed from tool results, only from compaction/refusal-retry)

**`tools/content_filter_scrub.py` (new) + `agent/fork/anthropic_recovery.py` +
`tools/tool_result_storage.py`.**

**Symptom:** session `20260714_081201_7539dd` hit a real Anthropic
`stop_reason="refusal"` (`agent.log`: `finish_reason=content_filter`,
confirmed genuine via the 1:1 mapping at `agent/transports/anthropic.py:361`,
not a hermes misclassification) on an otherwise ordinary conversation — no
shell commands, no sensitive topic in the live turn. It then would not clear:
every subsequent turn re-refused (`Repaired 1 message-alternation violations
before request` logged on each one — a refusal leaves an empty assistant turn
that breaks role alternation), surviving both a manual model switch
(sonnet-5 → opus-4-8) and a `/compact` (38 → 25 messages).

**Root cause:** the refusal fired immediately after several `session_search`
tool calls returned huge raw excerpts of old session files (up to 194K chars
inline, before truncation — old sessions run 1–14 MB). The existing
credential-extraction/pg_dump/S3/SQLConnectionString/upload_stream sanitizer
(`sanitize_messages_for_refusal_retry`, originally in
`agent/fork/anthropic_recovery.py`) only ran in two places: inside the
`/compact` summarizer's paraphrase step, and on an explicit refusal-retry that
never actually fired this session (no fallback provider configured, so the
recovery chain had nothing to try — no "Refusal sanitize retry" line in the
log). **Raw tool output was never scrubbed.** If an old session surfaced by
`session_search` contains one of the known trigger patterns verbatim, it
poisons live context with zero protection, and — since the poisoning is now
baked into message *content*, not just the compaction summary — no amount of
model-switching or re-compacting removes it.

**Fix:**
1. **`tools/content_filter_scrub.py` (new).** Moved the `TRIGGER_PATTERNS`
   regex list out of `anthropic_recovery.py` into one shared module —
   `scrub_trigger_patterns(text)` (plain string) and `scrub_message_content
   (content)` (handles both string and multi-part list content). Single
   source of truth; both call sites below import from here instead of
   maintaining copies that drift.
2. **`agent/fork/anthropic_recovery.py`** — `sanitize_messages_for_refusal_retry`
   now delegates to `scrub_message_content` instead of a local copy. Same
   behavior (most recent user message left untouched), zero duplication.
3. **`tools/tool_result_storage.py::maybe_persist_tool_result` — the actual
   fix.** This is the universal Layer-2 choke point every non-multimodal tool
   result passes through (`agent/tool_executor.py:918`, unconditionally,
   before the result reaches context) — not a `session_search`-specific
   patch. Added `scrub_trigger_patterns(content)` at the very top, before the
   size-threshold check, so it fires regardless of tool name or size — this
   also covers `read_file` (pinned `threshold=inf`, previously untouched by
   any scrub path) and any other tool (`grep`, `bash`, etc.) that might
   surface the same patterns from a local file or command output, not just
   old session transcripts.

**Merge note:** `tools/content_filter_scrub.py` is a new hard-fork file
(never conflicts). `anthropic_recovery.py` is already fork-only. The one
upstream-adjacent-risk file is `tools/tool_result_storage.py` — on conflict,
keep the `scrub_trigger_patterns(content)` call at the top of
`maybe_persist_tool_result`, before `effective_threshold` is computed, so it
runs unconditionally rather than only on the persist-to-disk branch.

**Verification:** 66 new/updated tests pass —
`tests/tools/test_content_filter_scrub.py` (new, 13 tests: pattern-level +
message-content-shape coverage) and additions to
`tests/tools/test_tool_result_storage.py` (3 new: below-threshold scrub,
tool-agnostic scrub incl. `read_file`'s inf threshold, scrub-before-persist-
to-disk) alongside the pre-existing 52/52 in that file. Manual sanity check:
`sanitize_messages_for_refusal_retry` still scrubs historical messages via
the shared module and still leaves the active user turn untouched.


### Fork-only feature — 2026-07-14 (hot-tier audit, dry-run MVP)

New `agent/hot_tier_audit.py` (`ea0aef879`). Addresses a real gap noticed in
usage: hot-tier `MEMORY.md`/`USER.md` only get manually reviewed when a write
is rejected for exceeding the char cap — nothing periodically re-checks
existing entries for staleness (a dead file path sitting unnoticed for
months, etc.).

**What it does (this pass — deliberately narrow):**
- On a real (non-dry) curator run, if `curator.hot_tier_audit: true`, reads
  `MEMORY.md`/`USER.md` entries (same `ENTRY_DELIMITER` split
  `tools/memory_tool.py` already uses) and regex-extracts path-shaped tokens
  (`~/...`, `/Users/...`) from each entry.
- Flags an entry as a stale-path candidate if any extracted path fails
  `Path.exists()` after `expanduser()`.
- `run_hot_tier_audit(dry_run=True)` (the default —
  `curator.hot_tier_audit_dry_run` defaults on) only produces a summary dict
  (`entries_checked`, `stale_path_candidates`, ...) folded into the existing
  curator run-report/`on_summary` callback. No file mutation, no warm-tier
  writes.
- Hooked into `maybe_run_curator()` after the existing skill-curation pass,
  wrapped in try/except so an audit failure can never break the existing
  curator flow.

Design doc: `docs/plans/2026-07-14-hot-tier-audit.md` (full design, including
the still-deferred LLM-classification step — this landed pass implements
the heuristic-only stale-path subset, both dry-run and live mutation).

**Config:**
```yaml
curator:
  hot_tier_audit: true         # default false
  hot_tier_audit_dry_run: true # default true — set false for live mutation
```

Tests: `tests/agent/test_hot_tier_audit.py` (13 tests — config defaults,
stale/non-stale path classification, dry-run non-mutation guarantee,
curator-hook wiring).


### Fork-only feature — 2026-07-14 (hot-tier audit live mutation)

`agent/hot_tier_audit.py` (`84cbae4e3`) implements `dry_run=False`,
replacing the `NotImplementedError` placeholder from the dry-run MVP
above. Still heuristic-only — no LLM-based classification; this pass only
automates what `classify_entries()`'s stale-path check already flags.

**What it does:**
- Snapshot-first: calls new `agent.curator_backup.snapshot_memory(reason=...)`
  (mirrors `snapshot_skills()`'s tar.gz + `manifest.json` pattern, targets
  `~/.hermes/memories/` instead of `~/.hermes/skills/`, respects the same
  `curator.backup.enabled`/`curator.backup.keep` config and prunes old
  snapshots the same way). If the snapshot fails or returns `None`, live
  mutation aborts with `RuntimeError` — never mutates without a backup.
- Every entry flagged `is_stale_path_candidate=True` is demoted to the warm
  tier via `tools.memory_warm.get_warm_store().add(content=..., category=
  "demoted-stale-path", tags="hot-tier-audit,auto-demoted")`, then removed
  from its source hot-tier file (`MEMORY.md` or `USER.md` — provenance
  tracked per-file, so a stale entry in `USER.md` never gets removed from
  `MEMORY.md`). Warm-tier write happens before the hot-tier removal (a
  failure there loses nothing from hot tier).
- Non-stale entries are left untouched, in original order. A hot-tier file
  is only rewritten when its content actually changed — zero stale
  candidates in a file means no rewrite (avoids reformatting untouched
  files, still takes the snapshot for predictability).
- Summary dict gains `demoted_count` and `snapshot_path` alongside the
  existing `entries_checked`/`stale_path_candidates` keys.

**Config:** unchanged from the dry-run MVP above — flip
`curator.hot_tier_audit_dry_run: false` to enable live mutation once dry-run
reports are trusted (staged rollout per the design doc).

Tests: `tests/agent/test_hot_tier_audit.py` grew to 18 (added 6 live-mode
tests: snapshot-before-mutate ordering, snapshot-failure abort, stale-entry
demotion + hot-tier removal, non-stale entries left alone, no-op when zero
stale candidates, and cross-file provenance for MEMORY.md vs USER.md).


### Fork-only feature — 2026-07-14 (hot-tier audit LLM classification)

`agent/hot_tier_audit.py` implements design doc §2.1 step 2 — the
keep/demote/stale/dead LLM classification pass deferred by both passes
above. `run_hot_tier_audit()` gains a `consolidate` parameter (defaults to
`agent.curator.get_consolidate()` — the same flag the skill curator's own
LLM pass is gated behind, not a new one).

**What it does:**
- `consolidate=False` (default): behavior is byte-for-byte unchanged from
  the heuristic-only live-mutation pass above. No LLM call is ever made.
- `consolidate=True`: every hot-tier entry (not just heuristic-flagged
  ones) is sent in one prompt to `_llm_classify_entries()`, which calls
  `agent.auxiliary_client.call_llm()` directly — a single structured-output
  classification call, not a forked tool-using `AIAgent` (classification
  needs no tools). Reuses `agent.curator._resolve_review_runtime()` for
  provider/model/credential resolution so there's one aux-model binding
  path, not two. The system prompt explicitly instructs the model to
  treat in-entry text as data to classify, never as instructions to obey
  (memory entries are user-authored but semi-untrusted input to this
  pass).
- The LLM's response must be a fenced `\`\`\`json` array, one object per
  entry (`{"id", "classification", "reason"}`), covering every id exactly
  once with a label in `{keep, demote, stale, dead}`. Any deviation
  (malformed JSON, non-list, invalid label, duplicate/missing/out-of-range
  id) fails the WHOLE parse — `_parse_llm_classification()` returns `None`
  rather than accepting a partially-trustworthy response.
- Live mode: `demote` → warm tier (identical write path to the heuristic
  pass's demotion). `stale`/`dead` → hard-deleted (removed from the
  hot-tier file, no warm-tier write) **only** when
  `agent.curator.get_prune_builtins()` is also `True` — reusing that flag
  per the design doc rather than adding a new one; otherwise left in place
  and merely flagged in the report. `keep` → always untouched.
- Sanity cap: if the LLM classifies more than `max(3, 50% of entries)` as
  demote/stale/dead in one pass, live mode aborts with `RuntimeError` and
  zero mutation — guards against a degenerate or adversarial
  classification wiping most of the hot tier in one run.
- Failure handling is asymmetric by design: if the LLM call fails, its
  response fails validation, or the sanity cap trips, live mode raises
  `RuntimeError` with **zero mutation**. It never silently falls back to
  the more aggressive heuristic-only demote-everything-flagged path — a
  caller who opted into the smarter LLM-informed pass and hit a failure
  there must see that failure, not get downgraded quietly to a blunter
  live mutation they didn't ask for on this call.
- Dry-run + `consolidate=True` runs the LLM pass and reports what it WOULD
  do (verdict + reason per entry) with zero mutation, same "preview
  first" posture as the skill curator's own dry-run.
- Snapshot ordering unchanged: `snapshot_memory()` still runs before the
  LLM call and before any file touch, in both live sub-paths.
- `maybe_run_curator()` now resolves `consolidate` once and passes it
  explicitly into `run_hot_tier_audit()`, so the hot-tier pass and the
  skill-curation LLM pass it runs alongside always agree on
  heuristic-only vs LLM-classification mode for a given curator cycle.

**Report file — deliberate deviation from the design doc:** §2.1 step 5
asks for a "## Hot-tier audit" section appended to the same
`REPORT.md`/`run.json` the skill curator writes
(`agent.curator._write_run_report`). That curator report is written
asynchronously from a background daemon thread, while
`run_hot_tier_audit()` runs synchronously right after
`run_curator_review()` *returns* — before that thread finishes — so
appending to the same file would race the skill curator's own write.
Instead `agent/hot_tier_audit.py` writes its own sibling report
(`run.json` + `REPORT.md`, listing per-entry classification + reason) to
`$HERMES_HOME/logs/curator/hot_tier_audit/<timestamp>/`, under the same
parent logs directory. Flagged here for visibility since it diverges from
the plan doc's stated preference.

**Config:** unchanged — `curator.consolidate: true` (already the skill
curator's own consolidation gate) turns on the LLM classification step for
the hot-tier pass too; `curator.prune_builtins` (already the skill
curator's built-in-pruning gate) additionally gates hard-delete of
stale/dead entries.

Tests: `tests/agent/test_hot_tier_audit.py` grew to 35 — 17 new tests
covering: consolidate=False never invokes the LLM path; dry-run +
consolidate=True previews without mutating; LLM overriding a heuristic
false-positive; demote → warm-tier write; dead → hard-delete gated on
`prune_builtins` true/false; LLM failure aborts with zero mutation (no
heuristic fallback); sanity-cap trip aborts with zero mutation; the
`call_llm` plumbing (provider/model binding, prompt content, response
parsing) end-to-end with a mocked `call_llm`; and `_parse_llm_classification`
validation (valid response, missing ids, invalid label, duplicate ids,
malformed JSON, non-list body). `maybe_run_curator` hook tests updated for
the new `consolidate` kwarg plus a new test asserting `consolidate=True`
propagates through to the hot-tier audit call.

### Fork-only fix — 2026-07-14 (exit watchdog swallows cost report / resume hint)

**Problem:** On interactive-mode exit (`/exit` or Ctrl+D), `cli.py`'s
`run()` finally-block called `_run_cleanup()` then
`self._print_exit_summary()` — cleanup first, summary second.
`_run_cleanup()` includes the fork-only Phase 2 memory-confirm step
(`hermes_cli/memory_confirm.py::confirm_and_commit()` →
`tools.memory_extraction.extractor.on_session_end()`), which fires an LLM
call with its own timeout (`auxiliary.memory_extraction.timeout`, default
30s), plus `shutdown_mcp_servers()` (up to 15s). Both run inside
`_run_cleanup()`, which is guarded by `_arm_exit_watchdog()` — a daemon
thread that force-exits the process via `os._exit(0)` after
`HERMES_EXIT_WATCHDOG_S` seconds (was 30s) if cleanup hasn't returned.
Worst case (45s: 15s MCP + 30s memory-extraction) comfortably exceeded
the 30s watchdog budget, so the watchdog would fire mid-`_run_cleanup()`
and `os._exit(0)` the process before `_print_exit_summary()` — printed
*after* cleanup in source order — ever ran. User-visible symptom:
`agent.log` shows `"Memory: reviewing proposals from this session..."`
printed with nothing after it, then `"Exit watchdog fired after 30s —
forcing process exit"` — no cost report, no `--resume <session_id>` hint,
no error shown to the user.

**Fix:**
- Reordered both interactive-exit call sites (the stdin-unavailable
  early-return path near the top of `run()`, and the main exit path in
  `run()`'s finally block) to call `self._print_exit_summary()` **before**
  `_run_cleanup()`. The single-query path (`hermes chat -q`) was already
  correctly ordered and untouched.
- Bumped `_arm_exit_watchdog()`'s default from 30s to 60s
  (`HERMES_EXIT_WATCHDOG_S` env var override unchanged) so the
  15s-MCP + 30s-memory-extraction worst case has real headroom instead of
  being right at the guillotine line.
- Both changes are complementary, not redundant: the reorder guarantees
  the cost report/resume hint print even if a *future* slow step exceeds
  any watchdog budget; the timeout bump reduces how often cleanup gets
  cut off at all (letting memory review actually finish instead of being
  routinely truncated).

**Tests:** `tests/cli/test_exit_summary_before_cleanup_ordering.py` (new) —
statically asserts every bare `_run_cleanup()` call statement in `cli.py`
is preceded by a `self._print_exit_summary()` call within the same local
block; verified it fails against the pre-fix ordering by reverting locally
and re-running before committing the actual fix. Existing
`tests/cli/test_cli_shutdown_memory_messages.py`,
`test_session_boundary_hooks.py`, `test_single_query_session_finalize.py`,
`test_cli_active_agent_ref_wiring.py`, `test_tui_terminal_reset_on_exit.py`
all still pass (32 tests). `test_exit_summary_resume_hint.py`'s 5 failures
are pre-existing (a `sys.argv[0]` → `__main__.py` resolution quirk under
this test runner, unrelated to this change) — confirmed via `git stash`
against unmodified `main`.

### Fork-only feature — 2026-07-14 (Ctrl+C to skip the exit-cleanup wait)

**Follow-up to the watchdog fix above.** Bumping the exit watchdog to 60s
(from 30s) fixed the summary-swallowing bug, but it also means a user who
exits with `/exit` now potentially sits through up to 45s of legitimate
cleanup (memory-confirm LLM call + MCP teardown) with only two options:
wait it out, or `kill -9` the process from another terminal. Neither is
great — the second loses the graceful teardown (session persistence,
memory commit) the wait exists to let finish.

**What it does:** `_install_cleanup_skip_handler()` installs a temporary
SIGINT handler for the duration of `_run_cleanup()` (renamed body:
`_run_cleanup_body()`) that calls `os._exit(0)` directly on a Ctrl+C press,
rather than raising `KeyboardInterrupt` — cleanup steps are wrapped in
bare `except Exception` blocks that would otherwise swallow the interrupt
and keep running anyway, defeating the point. A one-line hint
(`(cleaning up — press Ctrl+C to quit immediately)`) prints alongside the
existing "Shutting down…" message so the option is visible, not hidden.
The previous SIGINT handler is restored via the caller's `finally` block
regardless of how cleanup exits (normal return, raise, or the existing
`except BaseException` around MCP shutdown), so a signal after cleanup
completes behaves normally again.

Safe to install unconditionally: by the time `_run_cleanup()` runs,
`app.run()` has already returned, so prompt_toolkit's own TUI-level Ctrl+C
binding (see the Windows SIGINT-absorb handler earlier in the file) is
no longer live — this is a later phase of shutdown, not a competing
handler for the same keypress. Skipped entirely under
`PYTEST_CURRENT_TEST` (mirrors `_arm_exit_watchdog`'s own guard) and
degrades to a no-op restore if `signal.signal()` fails (off-main-thread
call, or a platform without the expected SIGINT semantics) — the 60s
watchdog remains the backstop either way.

Net effect: three ways to end up exited — cleanup finishes on its own
(the common case, now fast-summary-first per the fix above), the user
Ctrl+C's for an instant exit, or the 60s watchdog catches a genuinely
wedged process. The cost report / resume hint is unaffected either way
since it already prints before cleanup starts.

**Tests:** `tests/cli/test_exit_cleanup_skip_handler.py` (new, 7 tests) —
pytest no-op guard, install/restore round-trip, handler calls `os._exit`
directly (not raise), graceful degradation when `signal.signal()` raises
(non-main-thread), `_run_cleanup` installs-then-restores around the split
`_run_cleanup_body()` including on a raising body, and the
`notify_session_finalize` kwarg still threads through the split
correctly (this was the actual regression risk introduced by the split —
verified explicitly, not just assumed). Full existing `tests/cli/` exit/
cleanup suite (31 pre-existing tests across 6 files) re-run clean
alongside the new file (38 total).

### Fork-only fix — 2026-07-14 (memory-confirm cost not counted, no exit progress indicator)

**Problem (two related complaints, same root cause: the memory-confirm
step's real work was invisible to two different things).**

1. The Phase 2 memory-confirm step (`hermes_cli/memory_confirm.py::
   confirm_and_commit` → `tools.memory_extraction.extractor.
   on_session_end` → `_call_extraction_llm`) makes a real LLM call against
   whatever `auxiliary.memory_extraction.*` is configured (default
   `claude-haiku-4-5`). That call has a real dollar cost, but nothing
   folded it into `agent.session_estimated_cost_usd` — the printed
   "Cost: $X.XX (estimated)" line in the exit summary only ever reflected
   the main conversation loop's spend, silently under-counting the true
   cost of ending the session. Compounding this, the confirm step ran
   inline inside `_run_cleanup_body`, which is called AFTER
   `_print_exit_summary()` reads that total in source order at every
   interactive-exit call site — so even if the cost had been tracked
   somewhere, the summary printed before it existed.
2. Separately, the confirm step printed a single static
   `"Memory: reviewing proposals from this session..."` banner and then
   blocked silently on the LLM call (which can legitimately take several
   seconds, longer with several proposals needing conflict classification
   via `conflict.classify` — one LLM call per ambiguous entry). No
   spinner, no heartbeat — the terminal looked hung with zero visible
   indication anything was happening, a regression from earlier behavior
   where at least a "thinking" indicator was visible.

**Fix:**
- `tools/memory_extraction/extractor.py`: added a small module-level cost
  ledger (`_accumulated_cost_usd`, lock-guarded since per-turn extraction
  runs on a background thread). `_call_extraction_llm` now mirrors
  `call_llm`'s own provider/model resolution (via
  `agent.auxiliary_client._resolve_task_provider_model`, read-only, purely
  for pricing) and calls `agent.usage_pricing.{normalize_usage,
  estimate_usage_cost}` on the response, recording the result. New
  `get_and_reset_extraction_cost_usd()` drains (reads + zeroes) the ledger
  — the CLI exit path uses this so a later drain never double-counts a
  cost already folded into the session total. Cost accounting is
  best-effort: any pricing/resolution failure is caught and logged at
  debug level, never affects the actual extraction call or its return
  value.
- `hermes_cli/memory_confirm.py`: `confirm_and_commit`'s
  `extractor.on_session_end` call and `_classify_proposals`'s per-entry
  `conflict.classify` loop are now each wrapped in a `KawaiiSpinner`
  (reusing the same spinner class `agent/display.py` already uses
  elsewhere in the CLI) so the terminal shows live progress instead of a
  static banner during the LLM call(s). The session-end spinner is
  stopped as the FIRST action inside `_confirm_callback` (called
  synchronously mid-`on_session_end`) rather than after the call returns,
  so it never animates concurrently with `_interactive_review`'s own
  classify spinner or printed proposal list. Both spinners degrade
  silently to no-progress-indicator on construction/drive failure —
  never block the actual LLM call.
- `cli.py`: extracted the memory-confirm invocation out of
  `_run_cleanup_body` into a standalone, idempotent
  `_run_memory_confirm_before_exit()` (guarded by a new
  `_memory_confirm_attempted` module flag). All three exit call sites
  (the stdin-unavailable early-return path, the main `run()`
  finally-block exit, and the single-query `-q` path — the last of these
  was previously fine on ordering but still missing the cost fold-in) now
  call this function explicitly BEFORE `self._print_exit_summary()` /
  `cli._print_exit_summary()`, and it folds the drained extraction cost
  into `agent.session_estimated_cost_usd` right there. `_run_cleanup_body`
  still calls the same function (now a no-op on the common path thanks to
  the guard) as a safety net for any exit route that doesn't call it
  explicitly first.

**Tests:**
- `tests/tools/test_memory_extraction.py` — new `TestExtractionCostLedger`
  class (4 tests): ledger starts/drains at 0.0, a real
  `_call_extraction_llm` invocation (mocked at the `call_llm` transport
  boundary, not at `_call_extraction_llm` itself like every other test in
  the file) records nonzero cost against a priced model, the ledger
  resets on read so a second immediate drain returns 0.0, and a
  provider-resolution exception during cost accounting doesn't propagate
  or block the extraction call's actual return value.
- `tests/cli/test_memory_confirm_before_exit.py` (new file, 6 tests) —
  `_run_memory_confirm_before_exit` folds a nonzero drained cost into
  `session_estimated_cost_usd`, a zero drain leaves the total unchanged,
  the idempotent guard prevents `confirm_and_commit` from running twice
  in one process, a missing `_active_agent_ref` is a no-op, a raising
  `confirm_and_commit` doesn't crash exit, and a raising
  `get_and_reset_extraction_cost_usd` doesn't crash exit or undo an
  already-successful `confirm_and_commit` call.
- `tests/cli/test_exit_summary_before_cleanup_ordering.py` — extended with
  a second source-level test asserting every `_print_exit_summary()` call
  site is preceded (within the same local block) by a
  `_run_memory_confirm_before_exit()` call, pinning the new ordering
  requirement the same way the existing test pins the
  `_run_cleanup()`-after-summary ordering.
- Full regression sweep: `tests/cli/`, `tests/tools/test_memory_
  extraction.py`, and `tests/hermes_cli/test_memory_confirm.py` re-run
  together (1069+ tests collected in `tests/cli/` alone) — zero new
  failures introduced. The 8 failures present both before and after this
  change (5 in `test_exit_summary_resume_hint.py`, already documented
  above as a pre-existing `sys.argv[0]` → `__main__.py` test-runner
  quirk; 3 more in `test_cli_approval_ui.py`, `test_cli_context_warning.py`,
  and `test_resume_quiet_stderr.py`) were confirmed pre-existing via
  `git stash` against unmodified `main` before this fix.

### Fork-only follow-up — 2026-07-14 (background skill-curator's own LLM cost was also uncounted)

**Problem:** the memory-confirm cost fix above only covers ONE of two
background LLM-calling subsystems that fire around CLI exit. The other —
`agent/curator.py`'s skill curator (`maybe_run_curator`, kicked off in a
daemon thread from `show_banner()` at CLI/session startup, not at exit)
— spawns a forked `AIAgent` (`run_curator_review`'s `_llm_pass` →
`_run_llm_review`) that reviews/prunes/consolidates agent-created skills.
That fork accumulates real cost on its own `session_estimated_cost_usd`,
but nothing ever surfaced it anywhere — not in the curator's own state
file (`hermes curator status`), not in the CLI's exit-summary cost
report. User-visible symptom: exiting mid-curation showed a
`⚡ skill_man github-pr-review-and-merge` line (the curator's forked
agent actively mid-tool-call) printed AFTER the "Cost: $X.XX (estimated)"
line — visible proof of in-flight spend the total never counted.

Architecturally different from the memory-extraction fix: the curator's
review pass is unbounded and can legitimately run for minutes (its own
docstring: "50-100 API calls against hundreds of candidate skills"),
started well before exit and running fully async in a daemon thread —
so unlike the bounded (~30s) memory-extraction call, exit must NEVER
block waiting for it.

**Fix:**
- `agent/curator.py`: added a small module-level cost ledger
  (`_accumulated_curator_cost_usd`, lock-guarded — the review runs on a
  daemon thread) plus `_active_curator_thread` (tracks the daemon thread
  object so liveness can be checked without blocking). `_run_llm_review`
  now captures `review_agent.session_estimated_cost_usd` in its `finally`
  block (before `.close()`) and records it via the new
  `_record_curator_cost_usd`. Two new public functions:
  `get_and_reset_curator_cost_usd()` (drain-and-reset, same pattern as
  the memory-extraction ledger) and `is_curator_running()` (True while
  the tracked thread is alive).
- `cli.py`: new `_fold_curator_cost_before_exit()` (idempotent, guarded
  by `_curator_fold_attempted`), called at all three exit call sites
  alongside `_run_memory_confirm_before_exit()` (same ordering
  requirement — before `_print_exit_summary()`). Non-blocking by
  construction: drains the ledger and folds a nonzero result into
  `session_estimated_cost_usd`; when the ledger is empty AND
  `is_curator_running()` is True, prints a one-line dim note
  ("background skill curator still running — its cost isn't included
  above; check `hermes curator status` after it finishes") so the
  printed total isn't silently incomplete without any indication. When
  the ledger is empty and curator isn't running (curator never fired,
  or already folded), it's a silent no-op.

**Tests:**
- `tests/agent/test_curator.py` — 6 new tests: `_run_llm_review` records
  nonzero fork cost into the ledger (draining resets it to 0.0 on a
  second read), a zero-cost fork doesn't pollute `result_meta` with a
  `cost_usd` key, a fork stub with NO `session_estimated_cost_usd`
  attribute at all doesn't break the review pass (cost tracking is
  advisory), and `is_curator_running()` correctly reflects thread
  liveness (false with no thread, true while a stub thread is alive,
  false again after it exits).
- `tests/cli/test_curator_cost_before_exit.py` (new file, 7 tests) —
  folds a nonzero drained cost into `session_estimated_cost_usd`, a zero
  drain with curator not running leaves the total unchanged and prints
  nothing, a zero drain WITH curator running prints the "still running"
  note without touching the cost total, the idempotent guard prevents a
  second drain, a missing `_active_agent_ref` is a no-op, and a raising
  `get_and_reset_curator_cost_usd` doesn't crash exit.
- `tests/cli/test_exit_summary_before_cleanup_ordering.py` — extended
  with a third source-level test pinning
  `_fold_curator_cost_before_exit()` before every exit-summary call site,
  same pattern as the memory-confirm ordering test. Fixed a latent bug in
  this file's own helper while adding it: the original `_find_all`-based
  substring search for `_run_memory_confirm_before_exit()` / the new
  `_fold_curator_cost_before_exit()` matched docstring MENTIONS of the
  call (e.g. `_run_memory_confirm_before_exit`'s own docstring explains
  its relationship to `_print_exit_summary()` in prose), not just actual
  call statements — happened to not matter for the memory-confirm test
  (the def-line's proximity papered over it) but caused a real false
  failure for the curator test. Replaced with `_bare_call_positions()`,
  which only counts lines that are ONLY the call statement (mirroring
  the bare-`_run_cleanup()` filter the very first test in this file
  already used) — applied retroactively to the memory-confirm test too
  for consistency.
- Full regression sweep: `tests/agent/test_curator.py` +
  `test_hot_tier_audit.py` (105 passed, 1 skipped) and the full
  `tests/cli/` suite with the 8 already-documented pre-existing failures
  explicitly deselected (1069 passed, 44 skipped, 8 deselected, exit 0)
  — zero new failures introduced.

### Fork-only follow-up — 2026-07-14 (end-of-session ordering: curator, then memory-confirm, then cost summary)

**User request:** the fast, non-blocking curator cost check should run
BEFORE the (potentially slower, interactive) memory-confirm UI, not
after — so a user about to sit through the confirm UI's countdown/review
prompt already knows the curator's status, and the two calls read in the
"natural" chronological sense (curator kicked off first, at startup;
memory extraction is what actually happened during the just-ended
conversation; then the summary of both).

**Fix:** swapped the call order at all four `_fold_curator_cost_before_
exit()` / `_run_memory_confirm_before_exit()` pairings in `cli.py` (the
three explicit exit call sites plus `_run_cleanup_body`'s safety-net
invocation) — curator now called first, memory-confirm second, in every
case. Pure reordering; neither function's own behavior changed.

**Tests:** `tests/cli/test_exit_summary_before_cleanup_ordering.py` — new
`test_curator_fold_precedes_memory_confirm_at_every_exit_site` pins the
new relative order (curator fold call site count must equal memory-
confirm call site count — they're always paired — and each memory-confirm
call must be preceded by a curator-fold call within the same local
block). Complements (doesn't replace) the two existing tests that each
independently pin "precedes the exit summary" for the two functions.

Full regression sweep re-run: targeted files 242 passed / 1 skipped
(exit 0); full `tests/cli/` suite with the same 8 pre-existing failures
deselected — 1070 passed / 44 skipped / 8 deselected (exit 0). Zero new
failures.

### Fork-only fix — 2026-07-15 (response box: short complete sentences invisibly buffered before a tool call)

**Symptom:** in the interactive CLI, the streamed response box could look
frozen mid-sentence with no closing border while the model was actually
still working — e.g. the model finishes a short sentence ("Let me first
enumerate outgoing messages and their subjects...") then goes on to
generate a tool call's arguments with no further visible text. From the
user's side this reads as a cut-off/stuck display.

**Root cause:** `_emit_stream_text()`'s partial-line force-flush (`cli.py`,
TTFT-perception fix from earlier this month) only paints buffered text once
it hits a full terminal line's worth of new characters, or a newline
arrives. A short-but-complete sentence well under that width just sits in
`_stream_buf` until something else eventually flushes it — the next
visible-text delta, the tool call actually firing (`_on_tool_gen_start` →
`_flush_stream()`), or end of turn. No indicator distinguishes "still
generating" from "done, waiting."

**Fix:** added a sentence-boundary early flush right after the existing
wrap-width loop in `_emit_stream_text()` — once the buffer holds at least
`max(24, wrap_w // 3)` characters and ends with `. `, `! `, `? `, or `: `,
flush immediately rather than waiting for wrap-width or a newline. Mirrors
the natural-boundary approach `_flush_reasoning_preview()` already uses for
the dim reasoning box. Short fragments with no sentence-ending punctuation
still stay buffered (unchanged behavior) — this only closes the gap for
complete-but-short chunks.

**Tests:** `tests/cli/test_stream_partial_line_flush.py` — two new cases,
`test_completed_sentence_flushes_before_wrap_width` (a short complete
sentence must flush immediately, buffer left empty) and
`test_short_incomplete_fragment_still_buffered` (guards against
over-eager flushing — a fragment with no sentence-ending punctuation must
still wait).

Verification: targeted file 8 passed (exit 0). Full `tests/cli/` suite —
1074 passed, 6 failed (exit 1); all 6 failures confirmed pre-existing via
an isolated `git worktree` checkout of `HEAD` before this change existed
(`test_exit_summary_resume_hint.py` ×5, `test_cli_context_warning.py` ×1) —
zero new failures from this fix. Two additional tests
(`test_cli_approval_ui.py`, `test_resume_quiet_stderr.py`) intermittently
flagged under the parallel runner / a bare full-file pytest run but passed
cleanly 3/3 and 1/1 in isolation — shared-state/parallel-worker flakiness,
not real regressions.

### Fork-only fixes — 2026-07-18 (spinner redraw leaves stale digits + phantom "Δ+NNNK new" context-delta balloon)

Two independent status/timer display bugs reported by the user in the same
session ("timers show duplicate numbers or too many digits" and "Δ+115K new
when I didn't add 115K of context in one go").

1. **`0a32275ff` — `agent/display.py`: `KawaiiSpinner` redraw padding used
   `len()` instead of terminal cell width.** The base-CLI tool-call spinner
   (`🌑 pondering (2.0s)`-style lines) tracks `self.last_line_len` to know
   how many trailing spaces to blank out on each `\r`-redraw. It computed
   that via Python's `len()`, which undercounts wide glyphs — the moon-phase
   spinner frames, kawaii-face frames (`(｡◕‿◕｡)`), and wing decorations all
   render as more terminal columns than `len()` reports (confirmed live:
   `len("🌑 ...") ` reports 1 column short per emoji). When a wide-glyph
   frame was followed by a narrower one, the pad computed from the
   undercounted `last_line_len` was too small, leaving stale trailing
   character(s) from the previous frame un-erased on screen — the visible
   symptom being leftover digits from the prior elapsed-time readout
   bleeding into the new one (e.g. a phantom trailing `0` surviving from a
   wider previous frame, making `1s` misread as `01s`/duplicated digits).
   This exact class of bug (`len()` vs true display width) was already
   fixed once for the CLI status bar itself
   (`HermesCLI._status_bar_display_width`, uses `get_cwidth`) but never
   applied to this older, separate spinner — hence "fixed in a few places
   but not everywhere."

   Fix: added `KawaiiSpinner._display_width()` using the same
   `prompt_toolkit.utils.get_cwidth()` mechanism as the CLI status bar, and
   pointed the `\r`-redraw pad calculation + `last_line_len` capture at it
   instead of `len()`. `print_above()`/`stop()`'s blank-line clearing derive
   from `last_line_len` too, so they're fixed as a byproduct — no separate
   change needed there.

   Reproduced numerically before/after: a wide-glyph previous frame
   followed by a shorter plain-ASCII frame left exactly 1 character
   un-erased under the old `len()`-based math; 0 characters left over with
   the `get_cwidth()`-based fix.

   Tests: new `tests/agent/test_kawaii_spinner_display_width.py` (6 cases)
   — direct `_display_width()` unit checks (ascii/emoji/kawaii-face/empty),
   a padding-math reproduction of the exact under-erase scenario, and a
   full `_animate()` integration test driving the real redraw loop and
   asserting `last_line_len` tracks cell width, not `len()`.

2. **`0a32275ff` — `cli.py`: per-turn context-delta segment (`Δ+NNK new`)
   treated a `0` baseline as a real baseline, reporting the ENTIRE context
   as this turn's growth.** `ContextCompressor.display_prompt_tokens()` returns `0`
   in two distinct "no real data yet" cases: a genuinely fresh session, and
   the turn immediately following a context compression (where
   `last_real_prompt_tokens` is parked at `-1` as an "awaiting real usage"
   sentinel and the method clamps any non-positive value to `0`). The
   turn-start capture stored that `0` directly into
   `self._turn_start_context_tokens` — and `0` is not `None`, so the later
   `base is not None` guard treated it as a legitimate baseline. The delta
   math then computed `context_tokens - 0 == context_tokens`: the user's
   whole accumulated context reported as if it were all added in a single
   turn (observed: "Δ+115K new" on a session where nothing close to 115K
   was actually added that turn).

   Fix: both the capture site (turn-start handler, ~cli.py:14690) and the
   consumption site (`_get_status_bar_snapshot`, ~cli.py:5044) now require
   the baseline to be a positive int, not merely non-`None`, before
   computing/showing a delta. A genuine prompt is never actually 0 tokens
   (system prompt + tool schemas alone are non-zero), so this loses no real
   baseline — it only suppresses the segment on the one/two turns where no
   honest "previous state" exists to diff against (consistent with a
   second-opinion review of the fix before applying it).

   Tests: added `test_zero_baseline_does_not_report_full_context_as_delta`
   to the existing `TestContextDeltaSegment` class in
   `tests/cli/test_cli_status_bar.py`, asserting the segment is fully
   suppressed (not shown as a false balloon) when the baseline is `0`.

Verification: `tests/agent/test_kawaii_spinner_display_width.py` (6/6
passed), `tests/cli/test_cli_status_bar.py` (51/51 passed, was 50). Full
`tests/cli/` + relevant `tests/agent/` display suites: 1193 passed, 44
skipped, 8 failed — all 8 failures reproduced identically against
unmodified `main` via `git stash` (the same known `test_exit_summary_resume_hint.py`
×5 / `test_cli_context_warning.py` ×1 pre-existing issues, plus 2 tests
that only fail under full-suite ordering but pass 2/2 in isolation both
before and after this change). Zero new failures.

### Fork-only feature — 2026-07-18 (`trafilatura`: free no-API-key `web_extract` backend)

**Problem:** the user's exo/ollama-cloud provider blocks had
`web.extract_backend: ddgs` configured, but `DDGSWebSearchProvider.
supports_extract()` is `False` — DuckDuckGo's `ddgs` package (like
brave-free and searxng) is search-only. `web_extract` calls returned
`"ddgs is a search-only backend and cannot extract URL content."` There was
no free, no-API-key, no-self-hosted extract backend at all — every
extract-capable provider in the registry (firecrawl/tavily/exa/parallel)
needs a paid key or a self-hosted service, and `claude-code` only works on
first-party Anthropic. Search-side was already fine (`web.search_chain:
[brave-free, ddgs]` correctly fails over).

**Fix:** new plugin `plugins/web/trafilatura/` (`TrafilaturaWebExtractProvider`,
extract-only — `supports_search()` is `False`). Fetches each URL directly via
`httpx.AsyncClient` and runs the open-source `trafilatura` library locally
for boilerplate/nav/ad-stripped markdown extraction + metadata (title,
author, description). No API key, no account, no self-hosted service.

Security-sensitive detail: redirects are walked manually
(`follow_redirects=False`, capped at 5 hops) rather than letting httpx
auto-follow, so `tools.url_safety.is_safe_url()` and
`tools.website_policy.check_website_access()` re-run on *every* hop before
it's requested — letting httpx auto-follow would fetch an attacker-controlled
redirect target (e.g. a 302 to a private/internal address) before any SSRF
check ever saw it. Also enforces a response body size cap (10MB) and a
content-type check (skips non-HTML responses rather than feeding binary/JSON
through trafilatura).

Wired into `hermes tools`' post-setup pip-install flow
(`hermes_cli/tools_config.py`, mirrors the existing `ddgs` post_setup
branch) and the picker auto-discovers it via the existing plugin-registry
mechanism (`_plugin_web_search_providers()`, no picker changes needed).

User's `~/.hermes/config.yaml` updated via `hermes config set` (not a direct
file edit — config.yaml write-protection blocked that): `web.extract_backend`,
`web.by_provider.exo.extract_backend`, and
`web.by_provider.ollama-cloud.extract_backend` all set to `trafilatura`. The
`anthropic` provider block's `claude-code` extract backend is untouched.

**Tests:** updated `tests/plugins/web/test_web_search_provider_plugins.py`'s
change-detector provider-count/capability-flag tests to include
`trafilatura` (extract=True, search=False). Fixed a latent bug (present on
clean `main` too, exposed by adding a second no-credential provider) in
`tests/tools/test_web_tools_config.py::test_no_keys_returns_false` — the
test only mocked the legacy `_ddgs_package_importable()` probe, not the
registry's own `DDGSWebSearchProvider.is_available()` (which
`get_active_search_provider()` calls directly), so `check_web_api_key()`
returned `True` in any dev env with `ddgs` actually pip-installed; now both
`DDGSWebSearchProvider.is_available` and
`TrafilaturaWebExtractProvider.is_available` are patched `False`.

Verification: real end-to-end extraction against
`docs.python.org/3/tutorial/introduction.html` through the actual
`web_extract_tool` dispatcher (18,592-char clean markdown, correct title,
truncation footer applied). SSRF guard confirmed blocking a private IP
(`127.0.0.1`) at the provider level. `tests/tools/test_web_tools*.py` +
`tests/tools/test_web_providers*.py` + `tests/plugins/web/` + `tests/
hermes_cli/test_plugins.py`: 418 passed, 1 failed — the 1 failure
(`test_unconfigured_search_emits_top_level_error`) reproduced identically
against unmodified `main` via `git stash` (a live `BRAVE_SEARCH_API_KEY` in
the dev `.env` leaks into that "unconfigured" test case; same root-cause
class as the bug fixed above, pre-existing, out of scope for this change).
Zero new failures.

### Fork-only fix — 2026-07-18 (`agent/auxiliary_client.py`: runtime-main override was a process-global data race, not thread-local)

**SUPERSEDED 2026-07-21** — the `threading.local()` mechanism this entry
describes (`_runtime_main_tls` / `_rtl_get` / `_rtl_set`) no longer exists.
The v2026.7.20 sync adopted upstream's `_RUNTIME_MAIN_CONTEXT` (a
`contextvars.ContextVar`) + `set_runtime_main()` / `scoped_runtime_main()` /
`reset_runtime_main()`, which independently solves the same cross-thread
clobbering bug this entry root-caused, and additionally isolates concurrent
async tasks on the same thread (which `threading.local()` cannot). See the
2026-07-21 sync entry above for what changed. Kept below for the historical
root-cause narrative (still accurate) and the reproduction technique (still
useful) — just mentally substitute ContextVar API calls for the
`_rtl_get`/`_rtl_set`/`_runtime_main_tls` names below.

**Symptom:** on an all-Anthropic session (main model `claude-sonnet-5`, no
ollama config error anywhere), the user hit `⚠ Auxiliary title generation
failed: HTTP 404: model: gemma4:31b`. `gemma4:31b` is the user's
`auxiliary.background_review` model (an `ollama-cloud`-routed
self-improvement fork), not anything configured for `title_generation` —
which should have resolved to `claude-haiku-4-5-20251001` via the
`auxiliary.anthropic` block. Config was correct; the model name was wrong
at request time.

**Root cause:** `_RUNTIME_MAIN_{PROVIDER,MODEL,BASE_URL,API_KEY,API_MODE}`
were bare module-level globals, written by `set_runtime_main()` at the top
of each turn and read by `_read_main_provider()` / `_read_main_model()` /
`_resolve_auto()` to determine "what the live main runtime is right now."
The comment above them claimed `"Process-local override ... Single-threaded
per turn — no lock needed."` That was false the moment background AIAgent
forks existed: `_spawn_background_review()` (the `bg-review` daemon thread)
and `maybe_auto_title()` (the `auto-title` daemon thread) each construct
their own `AIAgent` and run a full turn **concurrently** with the main
conversation thread — and each calls `set_runtime_main()` for **its own**
provider/model at turn start. With bare globals, whichever thread wrote
last won for every thread's reads, process-wide. A lock would not have
fixed this — the problem isn't "two threads racing to safely mutate shared
state," it's "the state itself needed to be per-thread, not shared." A
lock around a genuinely shared mutable would have just serialized the
clobbering instead of preventing it.

Concretely: the user's `auxiliary.background_review` config routes that
fork to `ollama-cloud` / `gemma4:31b`. Its daemon thread calls
`set_runtime_main("ollama-cloud", "gemma4:31b", ...)`. If the main
session's `title_generation` call (fired from `maybe_auto_title`'s own
daemon thread after the first exchange) resolved its task config in that
window, `_read_main_provider()` / `_read_main_model()` returned the
bg-review thread's values instead of the main thread's own
`anthropic`/`claude-sonnet-5` — sending a `gemma4:31b`-named request to the
Anthropic endpoint. 404.

Reproduced directly with a two-thread harness (one thread simulating the
main session's `set_runtime_main("anthropic", "claude-sonnet-5", ...)`,
the other simulating bg-review's `set_runtime_main("ollama-cloud",
"gemma4:31b", ...)`, both racing with a small `sleep()` between write and
read) — confirmed each thread saw the OTHER thread's values under the old
bare-global code, and confirmed the exact `title_generation` /
`background_review` resolution pair (`anthropic`/`claude-haiku-...` vs
`ollama-cloud`/`gemma4:31b`) came back correctly isolated after the fix.

**Fix:** converted the five globals to a single `threading.local()`
(`_runtime_main_tls`), with `_rtl_get(attr)` / `_rtl_set(**kwargs)` helper
wrappers. `set_runtime_main()` / `clear_runtime_main()` /
`get_runtime_main_base_url()` and the two inline read sites inside
`_resolve_auto()` and the vision custom-endpoint fallback in
`resolve_vision_provider_client` now go through the thread-local accessors
instead of bare globals. No caller-side changes needed —
`agent/turn_context.py`'s `build_turn_context()` and
`agent/background_review.py`'s review-fork setup already call
`set_runtime_main()` themselves, once per thread, at their own turn start;
they just needed the storage underneath to stop being shared.

Updated 3 existing unit tests that patched the old bare globals directly
(`monkeypatch.setattr(aux, "_RUNTIME_MAIN_BASE_URL", ...)`) to instead go
through the public `set_runtime_main()`/`clear_runtime_main()` API or patch
`aux._runtime_main_tls` attributes directly — `tests/agent/
test_set_runtime_main_custom_provider.py`, `tests/agent/
test_auxiliary_client.py::test_runtime_override_key_is_used`, `tests/agent/
test_auxiliary_main_first.py::TestResolveVisionCustomProvider` (all 3
cases).

Verification: `tests/agent/test_auxiliary_provider_first.py` + `tests/
agent/test_auxiliary_client.py` + `tests/agent/test_turn_context.py` +
`tests/agent/test_set_runtime_main_custom_provider.py` + `tests/tools/
test_browser_console.py` + `tests/tools/test_vision_native_fast_path.py`:
395 passed, 4 skipped (3 pre-existing `TestResolveVisionCustomProvider`
failures — a stale vision-resolution-cache test-isolation bug unrelated to
this change — reproduced identically against unmodified `main` via `git
stash`, excluded from this count). Plus `tests/agent/
test_title_generator.py` and 12 other `test_auxiliary_client_*` /
`test_auxiliary_*` suites: 167 passed, 3 skipped. Plus the full
`background_review` suite (`tests/run_agent/test_background_review*.py`,
`tests/test_background_review_*.py`): 57 passed. Zero new failures.

### Fork-only fix — 2026-07-19 (`agent/display.py`: CLI todo tool showed a bare count, never the actual checklist)

**Symptom:** user called `todo` with 7 items mid-session; the CLI printed
only `┊ 📋 plan      7 task(s)  0.0s` with no way to see what the 7 tasks
actually were. User: "we have to-do set/7 tasks but there is no UI element
showing what they are, that's not good."

**Root cause:** `get_cute_tool_message()`'s `"todo"` branch (the CLI's
quiet-mode tool-completion renderer) only ever parsed `summary.total` /
`summary.completed` out of the tool result to build a one-line count. It
never read the `todos` array the result also carries. This was a CLI-only
gap — the desktop app's `ComposerStatusStack`
(`apps/desktop/src/app/chat/composer/status-stack/index.tsx`) already
renders a full per-item checklist group (`defaultCollapsed={group.type !==
'todo'}`, i.e. expanded by default), and the TUI gateway
(`tui_gateway/server.py`) already forwards the complete `todos` array to
its frontend as structured `payload["todos"]`. Only the terminal path in
`agent/display.py` dropped the item list on the floor.

**Fix:** the `"todo"` branch now also extracts `data["todos"]` (the full
current item list, always present in `todo_tool()`'s return value — see
`tools/todo_tool.py`) and, when non-empty, renders each item as an indented
status line below the existing header, e.g.:

```
┊ 📋 plan      2/7 task(s)  0.0s
      [x] Wire EXO_PP_SPEC_FINISH_LOG through start_cluster.sh
      [x] Clear stall-dump directories on both nodes
      [>] Reboot both Mac Studios (TB link wedge)
      [ ] Verify TB link (en3) back up on macstudio-m4-1
      [ ] Relaunch exo cluster
      [ ] Repro the stall condition
      [ ] Capture finish-decision diagnostic log
```

Markers (`[x]`/`[>]`/`[ ]`/`[~]`) match `TodoStore.format_for_injection`'s
post-compression re-injection format, so the terminal view and what the
model sees after a compaction event look the same. Capped at 30 items
shown with a `+N more` tail line; per-item content still goes through the
existing `_trunc()` helper (respects the global `_tool_preview_max_len`
config). Falls back to the original header-only line whenever the result
doesn't carry a `todos` array — fully backward compatible.

Verification: added `TestTodoChecklistBody` (8 new tests) to `tests/agent/
test_display_todo_progress.py` covering read/create/merge-update paths,
per-item truncation, the 30-item cap, malformed non-dict items, and the
no-checklist-body fallback. `tests/agent/test_display_todo_progress.py` +
`tests/agent/test_display.py` + `tests/agent/test_display_tool_failure.py`
+ `tests/hermes_cli/test_skin_engine.py`: 145 passed. All 24 pre-existing
todo-progress tests pass unchanged (they don't pass item data in their
fake results, so `current_items` stays empty and the header-only path is
byte-identical to before). Zero new failures.

### Fork-only instrumentation — 2026-07-19 (`cli.py`: unreproduced spinner-timer anomaly — forensic logging added, not yet root-caused)

**Symptom reported, NOT yet reproduced or root-caused:** a screenshot showed
the live CLI status line for an in-flight `process(action="wait",
timeout=280)` call reading `wait proc_55cca0f2ceb 280s (17081s)` — i.e. the
live elapsed timer (`17081s` ≈ 4.7 hours) exceeded the *entire session's*
own runtime (`49m` shown in the same screenshot's status bar). That is
mathematically impossible for a genuine `time.monotonic() - t0` delta if
`t0` (`_tool_start_time`) was set at that same tool call's own start.

**Investigation (extensive, inconclusive):**
- Confirmed current on-disk `_render_spinner_text()` (`cli.py` ~5278)
  cannot literally print a bare `"17081s"` once elapsed passes 60s — the
  `>=60s` branch always renders `"{m}m{s:02d}s"` (e.g. `"284m41s"`). So
  whatever produced the screenshot's string is either older code, or a
  path not yet identified.
- Audited every write site of `_tool_start_time` (`cli.py` — the `tool.
  started` handler at ~13210 sets it to `time.monotonic()` in lockstep
  with `_spinner_text`; three other sites clear it to `0.0` on tool
  completion / mode switch / exit). No site was found that could set it to
  a value that stale.
- Confirmed the MCP-wire tool name (`mcp__process` in the screenshot) is
  normalized back to bare `process` in `agent/transports/anthropic.py`
  (`strip_tool_prefix`) before reaching the display code, so the earlier
  analysis of `agent/display.py`'s bare-`"process"` branch applies
  correctly — `mcp__process` never reaches display code as a distinct name.
- Checked commit `069acf8e8` (2026-07-16, "bound PID/host-liveness probes
  so process(wait) can't hang past its timeout") — a prior, structurally
  similar incident (`process(wait, timeout=300)` displayed ~38,000s
  elapsed). That fix is present and unmodified in current `HEAD`
  (`9a8c49d1`); confirmed via `git merge-base --is-ancestor`. Doesn't
  explain this one — the earlier bug was a probe hang inflating the
  *real* elapsed via a stuck polling loop, not a display artifact.
- Live-inspected both running `hermes` processes at the time of
  investigation via `py-spy dump --pid <pid>` (requires `sudo` on macOS,
  run manually and pasted back) — both were idle (no thread blocked in
  `wait()` or any probe), so the anomaly wasn't caught mid-occurrence.
  Both processes had launched (21:09 and 21:46 that day) well after every
  relevant commit, ruling out "stale process running old code."
- Ruled out the `polaris-bootstrap` wrapper (`~/repos/polaris-bootstrap`)
  as a separate codebase — it's a thin auth-injection shim (`polaris.
  launcher.main()`) that execs straight into `hermes-agent`'s own `.venv/
  bin/hermes`; "Polaris" branding is purely a skin
  (`~/.hermes/skins/tanium-dark.yaml`'s `branding.response_label`), not a
  different code path.

**Action taken (this commit):** added forensic instrumentation rather than
a blind fix, since the mechanism is unknown. `_render_spinner_text()` now
compares `elapsed` against `session_age` (`datetime.now() - self.
session_start`) and logs a `logger.warning(...)` — once per tool call via
a new `_spinner_elapsed_anomaly_logged` latch, re-armed on every `tool.
started` event — whenever `elapsed > session_age + 5.0s`. That condition
is a hard invariant violation (a single tool call cannot outlive the
session that spawned it), so it should never fire on correct code; if it
does, the log line captures `elapsed`, `session_age`, raw `t0` (monotonic),
current `time.monotonic()`, the spinner text, and the calling thread name —
enough to actually diagnose the next occurrence instead of relying on
catching a live `py-spy` dump before it clears.

Verification: added 3 new tests to `tests/cli/test_cli_status_bar.py`
(`test_spinner_elapsed_anomaly_logs_when_exceeding_session_age`,
`test_spinner_elapsed_anomaly_does_not_log_for_normal_elapsed`,
`test_spinner_elapsed_anomaly_logs_only_once_per_tool_call`). Full
`tests/cli/test_cli_status_bar.py` + `test_tool_progress_scrollback.py` +
`test_slash_confirm_windows.py` + `test_reasoning_command.py` +
`test_cli_approval_ui.py`: 166 passed, 1 pre-existing skip. Zero new
failures.

**Still open:** the underlying mechanism is unknown. If this warning fires
in the wild, capture the full log line (not just the on-screen elapsed
string) and reopen — that's the missing piece every prior investigation
attempt lacked.

### Fork-only fix — 2026-07-21 (beta-only kwargs crash on SDK < 0.100)

**Symptom:** `Messages.stream() got an unexpected keyword argument
'context_management'` — a `TypeError` on the very first API call when using
the Anthropic provider with an SDK version older than 0.100.0.

**Root cause:** The fork's Claude-Code-mimicry path in
`build_anthropic_kwargs` attaches typed body kwargs (`context_management`,
`output_config`, `speed`, `betas`) that only exist on the
`client.beta.messages.*` namespace (Anthropic SDK 0.100+). The
`create_anthropic_message` function already tried `.beta.messages` first and
fell back to `.messages` when the client had no `.beta` namespace — but the
fallback path passed the beta-only kwargs straight through, and
`.messages.create()/.stream()` rejects them with `TypeError`.

The betas themselves already ride in `default_headers` from
`build_anthropic_client`, so the server-side behavior (thinking-block
lifecycle, fast mode, etc.) is preserved even without the typed body kwargs
— only the typed kwarg form was missing.

**Fix:** Two sites needed the same guard:

1. `create_anthropic_message` in `anthropic_adapter.py` (auxiliary client
   path) — already tried `.beta.messages` first but passed beta-only kwargs
   through on the `.messages` fallback.
2. `_call_anthropic` in `chat_completion_helpers.py` (main agent streaming
   path) — called `request_client.messages.stream()` directly on the plain
   `.messages` namespace, never attempting `.beta.messages`.

Both now detect whether the client has `.beta.messages` and, when it does,
route through `.beta.messages` which accepts the typed kwargs. When it
doesn't (older SDK, mocks, non-Anthropic-SDK clients), they strip the four
beta-only kwargs before dispatching to `.messages.*`. The betas still ride
in `default_headers` from `build_anthropic_client`, so server-side behavior
(thinking-block lifecycle, fast mode) is preserved.

`_BETA_ONLY_KWARGS` is a module-level constant in `anthropic_adapter.py`
so both paths reference the same set.

**Merge note:** this is a fork-only fix — upstream doesn't send
`context_management` or `speed` as typed kwargs, so it never hits this error.
On conflict, keep our version of `create_anthropic_message` (the entire
function is a fork divergence) and the `_call_anthropic` block in
`chat_completion_helpers.py`. The `_BETA_ONLY_KWARGS` constant and the
`.beta.messages` routing guards are additive and won't conflict with upstream
changes to either function body.

### Fork-only feature — 2026-07-21 (per-feature lazy-install denylist)

**Motivation:** `security.allow_lazy_installs: false` is all-or-nothing — it
blocks every lazy backend (TTS, memory providers, search providers, every
messaging platform). On a managed/work device we wanted to permanently
prevent `python-telegram-bot` and `discord.py` from being reinstalled
(neither `TELEGRAM_BOT_TOKEN` nor `DISCORD_BOT_TOKEN` is set there, so the
lazy-install path never fires today, but a future config change or manual
`ensure()` call could bring them back) without disabling lazy installs for
every other backend on that machine.

**Change:** added `security.blocked_features: []` (default empty list) to
the config schema in `hermes_cli/config.py`. In `tools/lazy_deps.py`,
`_is_feature_blocked(feature)` checks a `LAZY_DEPS` key (e.g.
`"platform.telegram"`) against that list; `ensure()` raises
`FeatureUnavailable` immediately if blocked, before even checking whether
the packages are missing or validating specs. `refresh_active_features()`'s
skip/fail classifier also recognizes the blocked-features message so
`hermes update` reports it as `skipped: ...` rather than `failed: ...`.

Fails open (not blocked) if config is unreadable, matching
`_allow_lazy_installs()`'s existing fail-open behavior — a corrupt config
should never lock a user out of their own backends.

Set via `hermes config set security.blocked_features
'["platform.telegram", "platform.discord"]'` (direct edits to
`~/.hermes/config.yaml` are agent-blocked as security-sensitive; the CLI
path is required).

**Merge note:** purely additive — a new function (`_is_feature_blocked`),
one new check at the top of `ensure()`, one new schema key, one new
substring in the skip/fail classifier. No upstream equivalent exists (no
per-feature denylist concept upstream), so this should apply cleanly on
future syncs unless upstream restructures `ensure()`'s control flow.

### Fork-only fix — 2026-07-21 (doctor Tool Availability ignores agent.disabled_toolsets)

**Symptom:** `hermes doctor`'s "Tool Availability" section warned about
`discord`, `discord_admin`, `homeassistant`, `spotify`, `yuanbao` /
`hermes-yuanbao`, `video_gen`, `image_gen`, `x_search`, `tts`,
`computer_use`, and `browser-cdp` even after all of them were added to
`agent.disabled_toolsets` (see the `blocked_features` entry above for the
`platform.telegram`/`platform.discord` half of this cleanup — this entry
covers the rest of a corp-machine toolset audit).

**Root cause:** this doctor section calls `model_tools.check_tool_availability()`,
a raw dependency/capability probe (`can this toolset's deps import`, `is its
required env var set`) that has no awareness of `agent.disabled_toolsets` at
all — that config key is only consulted later, at `get_tool_definitions()`
time, when the live agent actually assembles its tool list. A toolset the
user explicitly turned off still gets probed and still reports itself
"unavailable" every single doctor run, which reads as an unresolved problem
when it's actually working-as-configured.

**Fix:** added `_disabled_toolset_names()` in `hermes_cli/doctor.py`, reading
`agent.disabled_toolsets` from config (fails open to an empty set on any
config read error, so a corrupt config surfaces MORE warnings, never fewer
— never silently hide a real problem). The Tool Availability loop now
filters `unavailable` through this set before printing, right after the
existing `_apply_doctor_tool_availability_overrides()` call (which handles
the unrelated honcho/kanban runtime-gate cases). Toolsets already showing
`✓` (available) are untouched — this only suppresses the noisy ⚠ rows for
toolsets the user deliberately disabled.

**Verification:** added `TestDoctorDisabledToolsetNames` (3 tests: reads
disabled list correctly, empty when unset, fails open on config error) to
`tests/hermes_cli/test_doctor.py`. Full `test_doctor.py` +
`test_doctor_command_install.py`: 87 passed, no regressions.

**Config change (not code):** also set `agent.disabled_toolsets` on this
machine to add `hermes-yuanbao`, `computer_use`, `tts`, and `browser-cdp`
to the pre-existing list (`discord`, `discord_admin`, `messaging`,
`feishu_doc`, `feishu_drive`, `yuanbao`, `homeassistant`, `moa`, `spotify`,
`video`, `video_gen`, `image_gen`, `x_search`) — a corp-machine tool-surface
audit, not a code change. Verified via `get_tool_definitions()` directly
that `browser_cdp`/`browser_dialog` tool names are actually stripped from
the assembled tool list (that toolset has no row in `hermes tools list`
since it's a sub-toolset of `browser`, so the CLI listing alone doesn't
confirm it — checked the resolved tool names instead).

### Fork-only feature — 2026-07-21 (pet zone pane for the desktop app)

**Motivation:** the floating pet roams the entire window by default, which
can be distracting. A dedicated layout pane confines the pet to a specific
area of the window while preserving all its behavior (roam, loaf, hop,
drag, pop-out overlay).

**Change:** added a `pet-zone` pane to the desktop app's contribution-driven
layout system. The pane is registered in `controller.tsx` with `placement:
'bottom'` and added to the default layout tree (bottom-right, stacked with
terminal). A new `PetZoneSurface` component renders a `data-slot="pet-zone"`
container that hosts the `FloatingPet` inside it.

When the pet zone is enabled (Settings → Pet → "Pet Zone" toggle), the
pet renders inside the pane with `position: absolute` and its roam physics
constrained to the pane's bounding rect via a new `snapshotContainerLedges()`
function in `roam-geometry.ts`. The drag clamp, facing direction, and z-index
all respect the zone bounds. When disabled, the pet falls back to full-window
`position: fixed` behavior as before.

The zone is collapsible (collapses to a rail when off) so the pane stays
mounted and the pet keeps its position. Persisted per-device via localStorage
(like the roam toggle), not per-profile.

**Files:** `apps/desktop/src/store/pet.ts` (new `$petZoneEnabled` atom),
`apps/desktop/src/app/contrib/types.ts` (added `petZone` to `WiringApi`),
`apps/desktop/src/app/contrib/controller.tsx` (pane registration + layout +
visibility binding), `apps/desktop/src/app/contrib/surfaces.tsx`
(`PetZoneSurface`), `apps/desktop/src/app/contrib/wiring.tsx` (conditional
rendering), `apps/desktop/src/components/pet/floating-pet.tsx` (zone-aware
clamp/facing/position), `apps/desktop/src/components/pet/roam-geometry.ts`
(`snapshotContainerLedges`), `apps/desktop/src/components/pet/use-pet-roam.ts`
(zone-aware ledge selection), `apps/desktop/src/app/settings/pet-settings.tsx`
(toggle), `apps/desktop/src/i18n/*.ts` (strings).

**Merge note:** purely additive — new pane registration, new surface
component, new store atom, new geometry helper. No upstream equivalent
exists. The `WiringApi` interface gained a new field (`petZone`) which
will conflict if upstream adds a 5th surface of their own; resolve by
keeping both. The `FloatingPet` component signature changed from
`export function FloatingPet()` to accepting an optional `zoneContainer`
prop — any upstream call site that instantiates `<FloatingPet />` without
the prop is unaffected (it's optional, defaults to full-window mode).

### Fork-only fix — 2026-07-22 (pet zone: roam/drag used viewport coords, pet vanished)

**Symptom:** with the pet zone enabled, the pet either never appeared or
disappeared the moment you dragged it — flung outside the (clipped)
`overflow: hidden` zone container.

**Root cause:** two separate bugs stacked. (1) The default layout tree's
right-column vertical `split()` had 3 children (rail row, terminal,
pet-zone) but only 2 declared weights (`[1.6, 1]`) — the pet-zone group
was silently dropped from the tree and never rendered at all. (2) Once
visible, `getBoundingClientRect()` always returns viewport coordinates,
but a zoned pet is `position: absolute`, so its `style.left/top` are
container-local. The roam loop and drag handlers seeded/tracked directly
from the rect without converting, so e.g. a viewport position of
`(1400, 800)` got written as a *local* offset inside a ~300px pane —
instantly outside the clipped zone.

**Fix:** `controller.tsx`'s split weights corrected to `[1.6, 0.6, 0.4]`
(3 children, 3 weights). `use-pet-roam.ts` gained a `zoneOrigin()` helper
that subtracts the zone container's viewport offset when seeding the
physics loop and when tracking during a drag-yield; zone ledges
(`snapshotContainerLedges`) now take priority over the route-overlay
ledge (which is viewport-space math and would corrupt zone coordinates
whenever a route overlay — settings, profiles, etc. — is open).
`floating-pet.tsx`'s drag handlers, Alt+wheel zoom anchor, and the
initial mount position (`useState` initializer runs before the ref is
attached, so it can't read the zone rect on first render — defaults to
`{x:0,y:0}` when a `zoneContainer` prop is present) all convert pointer
`clientX/Y` through the same origin before clamping. Zone-local positions
are never written to the full-window `POSITION_KEY` — that key is in the
wrong coordinate space and would teleport the full-window pet to a
corner when the zone is toggled off.

**Files:** `apps/desktop/src/app/contrib/controller.tsx`,
`apps/desktop/src/components/pet/use-pet-roam.ts`,
`apps/desktop/src/components/pet/floating-pet.tsx`.

**Merge note:** fork-only file, no upstream equivalent — no conflict risk.

### Fork-only feature — 2026-07-22 (pet interactions: click-to-pet, zone status bubble, idle fidget)

**Motivation:** the pet's animation set is fixed at 7 states baked into
the spritesheet taxonomy (`agent/pet/constants.py` — `idle/wave/run/
failed/review/jump/waiting`), each tied to real agent activity via
`derive_pet_state()`. Adding new animations needs new spritesheet art
(backend generation work); this pass instead adds new *triggers* that
reuse the existing rows/particle systems for occasions beyond agent
activity.

**Changes (`apps/desktop/src/components/pet/floating-pet.tsx`):**

1. **Click-to-pet.** A plain click (pointerdown→pointerup with < 4px of
   travel, not the existing shift-click pop-out) now fires
   `burstVibeHearts()` — the same heart-particle + celebrate/wave beat
   the composer's affection detector triggers on a `reaction` event, just
   without needing an agent turn to say something nice first. Drag
   tracking gained a `moved` flag (mirrors the pop-out overlay's own
   `CLICK_SLOP_PX` click/drag disambiguation) so a real drag never
   accidentally fires the reaction on release.
2. **Zone status bubble.** `PetBubble` (the "working…"/"thinking…"/"your
   turn" text bubble, driven by `$petState`/`$petActivity`) now renders
   above the pet when `zoneContainer` is set — i.e. only inside the
   dedicated pet zone pane. The full-window pet still skips it per the
   original design note ("the app itself is the surface"), but that
   reasoning doesn't hold inside a small dedicated box where a glanceable
   status line costs nothing.
3. **Idle fidget.** A new effect watches `$petState` (not `$petAtRest`,
   which ignores the roam pose — gating on it would fire the fidget
   mid-stride while the pet is walking) and, on an exponential dwell
   (`dwellMs`/`DwellRange`, reused from `roam-behavior.ts`'s existing
   `PAUSE_DWELL` mechanism, mean 50s / floor 20s / ceiling 150s), fires a
   wave-or-jump beat if the pet is still idle when the timer lands. Reads
   as an occasional "still here" glance during long idle stretches
   instead of a frozen sprite; re-arms itself indefinitely while the
   component is mounted and active.

**Merge note:** purely additive to a fork-only file — new imports
(`burstVibeHearts`, `flashPetActivity`, `$petState`, `PetBubble`,
`dwellMs`/`DwellRange`), new constants, extended `dragRef` shape, no
changes to exported signatures beyond what the pet-zone work already
added. No upstream equivalent exists (upstream doesn't have a pet zone,
and its `FloatingPet` predates the zone-aware coordinate work above) —
no conflict risk on sync.

### Fork-only fix — 2026-07-22 (pet: idle fidget leaked into the status bubble; zone bubble clipped)

**Symptom:** with the pet zone on and roam enabled, the status bubble
showed "making moves…" (a `run`-state phrase) while the pet was simply
strolling around at idle — no agent activity in flight. Separately, the
bubble text got visually cut off near the zone's top/side edges.

**Root cause 1 (bubble showing at idle):** `$petState` — the single atom
`PetSprite` reads for the animation row — deliberately layers the roam
loop's own `run`/`jump` wander pose on top of real agent activity
(`$petState = idle-base ?? roam-motion`) so a wandering pet *looks* alive.
That's correct for the SPRITE, but the previous commit's `PetBubble`
change (and this session's short-lived idle-fidget prototype) both read
that same merged atom for status TEXT — so a roaming-but-idle pet was
indistinguishable from a genuinely busy one, and `PetBubble`'s `run` spec
list (`"making moves…"`, `"on it…"`, etc.) rendered for a walk that wasn't
work.

**Fix 1:** added `$petRealState` to `store/pet.ts` —
`computed([$petActivity, $busy], deriveLivePetState)`, i.e. `$petState`
minus the `$petMotion` merge. `PetBubble` now reads `$petRealState`
instead of `$petState`; `PetSprite` is untouched (still correctly reads
`$petState` — the sprite SHOULD show the roam pose). The idle-fidget
effect was rewritten to write `$petMotion` directly (the same silent pose
channel roam uses) rather than `flashPetActivity`/`$petActivity` — a
decorative fidget is structurally incapable of reaching `PetBubble` now,
not just accidentally avoiding it. Gated off whenever `roamEnabled` is
true (the wander loop already provides continuous life by writing that
same atom; a second writer would fight it) and skips firing if something
else already holds `$petMotion` (never interrupt a real pose).

**Root cause 2 (bubble clipping):** the zone container has
`overflow: hidden` (required so a roaming pet is clipped to its pane),
but the bubble was unconditionally positioned `bottom: 100%` + horizontally
centered — no headroom/edge awareness. A pet near the zone's top edge (from
roaming there, or a drag) pushed the bubble above the clipped boundary;
a pet near the zone's left/right edge overhung the bubble past the
clipped side.

**Fix 2:** `floating-pet.tsx` gained `BUBBLE_CLEARANCE_PX` (flips the
bubble below the sprite when `position.y` is too close to the zone's top)
and `bubbleHorizontalStyle()` (pins the bubble to the pet's near edge
instead of centering when the pet sits in the outer third of a narrow
zone, so the bubble can't overhang the clipped side). Both are pure
functions of the pet's already-tracked local position — no new
measurement/RAF work.

**Files:** `apps/desktop/src/store/pet.ts` (new `$petRealState`),
`apps/desktop/src/components/pet/pet-bubble.tsx` (reads `$petRealState`),
`apps/desktop/src/components/pet/floating-pet.tsx` (idle fidget rewrite +
bubble edge-awareness).

**Merge note:** fork-only files, no upstream equivalent — no conflict
risk.

**Merge note:** purely additive — one new function, one filter line in the
existing Tool Availability loop, three new tests. No upstream equivalent
(upstream's doctor doesn't have this section at all in the same form), so
should apply cleanly on future syncs.

