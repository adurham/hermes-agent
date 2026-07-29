# Fork notes — adurham/hermes-agent

This is a personal fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
Code here is **not intended for upstream contribution.** See "Why a fork" below.

## Merging upstream

1. `python scripts/fork-merge-plan.py --fetch` — predicts conflicts before you merge.
2. `git merge upstream/main`, resolve conflicts.
3. `python scripts/sync-fork-branding.py` — re-applies the adurham/hermes-agent
   repo-link rebrand. Upstream's own files always say `NousResearch/hermes-agent`;
   merging them back in reintroduces those links, including ones that are the
   installer/updater's *source of truth* for where to clone code from (this is
   what caused the 2026-07-25 incident where an install repair silently
   reverted to vanilla upstream, losing every fork commit from the live
   checkout). Run this **every time**, not just once. `--dry-run` to preview,
   `--verbose` to see every changed line. It's idempotent and reports anything
   it can't safely auto-resolve at the end — check that list by hand.
4. Run tests, push.

## Mandatory workflow for every fork-only change

Every fork-only fix/feature landed in this repo — no exceptions — must complete
all three of the following before the task is considered done:

1. **Document it in this file** — add a dated `### Fork-only fix/feature — YYYY-MM-DD (...)`
   entry (see the existing entries below for the expected level of detail:
   symptom, root cause, fix, files touched, verification).
2. **Commit it to git** — uncommitted changes are not durable. Multiple
   concurrent Hermes sessions can run against this same working tree
   (different terminals/windows/profiles), and any one of them doing a
   `git reset`/`checkout` can silently wipe an uncommitted edit sitting in
   another session's context. Commit as soon as the change is verified working
   — don't leave it staged/unstaged across turns.
3. **Push to `origin/main`** — unless the user has explicitly said they're
   working on a branch other than `main` for this task (a sync branch, a
   feature branch, etc.), in which case push there instead. An unpushed local
   commit is still vulnerable to a sibling session's `git reset --hard
   origin/main`, which a plain local commit does not protect against.

This sequence is not optional or "when convenient" — it is the definition of
done for a fork change. Skipping step 2 or 3 is how a fix gets silently
reverted and re-discovered as "still happening" days later.

### Fork-only fix — 2026-07-29 (desktop: pet roam loop never actually roams — dies after one animation frame)

**Reported:** after the pane-resize and jump-bob fixes shipped, user
reported the pet in Pet Zone "still not fixed" — no pacing, no falling,
no running, sitting completely still except for the (now-correct) idle
jump fidget. Confirmed via a live screenshot that Roam WAS enabled in
Settings. Diagnostic: user watched the sprite's `style="left/top"` in
DevTools for ~20s — it never moved on its own, "it only changes when
something in the loop forces itself forward, like the standing by/user
prompt breakout." That's the exact fingerprint of the bug below.

**Root cause, two compounding bugs:**

1. **The real one.** `usePetRoam`'s `schedule()` helper only calls
   `requestAnimationFrame` when its closure-local `raf` id is falsy —
   but nothing ever reset that id back to 0 after the browser "spent" it
   (fired the callback). So the rAF loop scheduled exactly **one frame**
   per effect mount, then went silently dead forever, regardless of how
   long roam/canRoam stayed true. The physics state machine (phase, walk
   target, dwell timer, fall/jump integrators) never got a second tick to
   progress on.
2. **The masking bug (this session's own earlier detour).** The effect's
   `enabled` prop was `roamEnabled && active && !overlayActive &&
   canRoam`, where `canRoam` flips false on every turn completion /
   clarify / error / celebrate beat. Each flip tore the whole effect
   down and remounted it (via the dependency array) — which
   incidentally re-armed bug #1's dead `raf` and let exactly one more
   frame slip through before dying again. That's why the pet only ever
   nudged at the instant of an activity transition: every "movement" was
   really just bug #1's one free frame per remount, triggered by bug #2's
   constant teardown/rebuild churn. Fixing #2 alone (as this session
   initially did, via a `canMove` ref-split — see below) would NOT have
   fixed the actual symptom on its own, since #1 would just make the pet
   freeze after its first frame in the fixed-and-no-longer-remounting
   effect instead. Both had to be fixed together.

**Fix:** `apps/desktop/src/components/pet/use-pet-roam.ts` — `raf = 0` is
now the first thing `step()` does, before any early return or scheduling
decision, so every code path's `schedule()` call actually queues the next
frame. Separately, `enabled` was split into a structural `enabled`
(roam opted in / pet loaded / not popped out — rare, still an effect
dependency, still legitimately tears down + rebuilds on its own flips)
and a new `canMove` (the old `canRoam` activity gate), read via a ref
(`canMoveRef`) inside the step loop instead of the dependency array —
toggling it now freezes physics in place and resumes the SAME state,
never resets it. `apps/desktop/src/components/pet/floating-pet.tsx`
updated to pass both props at the `usePetRoam` call site.

**Verification:** `npx tsc -p apps/desktop/tsconfig.json --noEmit`
clean. New test file
`apps/desktop/src/components/pet/use-pet-roam.test.tsx` (harness
component + fake-rAF driver, 3 tests): (1) proves the loop keeps
scheduling a new frame every tick indefinitely — this test FAILS against
the pre-fix code, confirmed by stashing the fix and re-running it; (2)
proves a `canMove` flip freezes/resumes the same closure instead of
resetting it; (3) proves an `enabled` flip still legitimately resets
everything, so the fix didn't just stop resetting anything at all. Full
`src/components/pet` + `src/store/pet.test.ts` suite: 5 files, 44 tests,
all passing. Files: `use-pet-roam.ts`, `floating-pet.tsx`,
`use-pet-roam.test.tsx` (new). Commit `e67330ecb`.

### Fork-only fix — 2026-07-28 (desktop: pet stationary jump pose looped in place after the first hop instead of bobbing)

**Reported:** after the previous Pet Zone resize fix, user noticed the
mascot in the zone "kinda just repeating the same jumping animation but
not actually changing height" — the leg/jump sprite frames kept cycling
but the pet stopped physically hopping after the first bob.

**Root cause:** `PetSprite`'s stationary jump bob (`.pet-jump-bob` in
styles.css — idle fidget / click-to-pet / turn-end celebrate; NOT the roam
loop's real ledge-to-ledge hop, which is physically impossible while
docked in Pet Zone mode since `snapshotContainerLedges()` always returns
exactly one ledge, so `chooseMove()` in `roam-behavior.ts` can never
select `'hop'` there) fired exactly ONE CSS animation per `jump`-state
transition, capped at `jumpDurationMs()`'s 900ms ceiling. But a
turn-completion celebrate (`flashPetActivity({ celebrate: true, ... },
2200)` in `gateway-event.ts`) deliberately HOLDS the `jump` pose for
2200ms — ~2x the sprite's own frame-loop `loopMs` so the leg animation
gets to loop twice before settling, per that call site's own comment. The
single bob landed and went flat for the remaining ~1.3s while the canvas
kept cycling jump frames underneath it, which reads exactly as "stuck
repeating the animation but not changing height."

**Fix:** `apps/desktop/src/components/pet/pet-sprite.tsx` — the bob effect
now re-triggers itself on an interval paced to `jumpDurationMs(loopMs)`
for as long as `$petState` (via the same `isJumping()` check the old code
used, still excluding the roam loop's own airborne case) stays `jump`,
instead of firing once per transition. `$petJumpBeat` (the repeat-request
nonce) now restarts the whole cadence from a fresh hop instead of just
firing one bob, so a second celebrate mid-hold doesn't leave a stale
interval ticking out of phase. Cleanup clears the interval alongside the
existing state/beat unsubscribes.

**Verification:** `npx tsc -p apps/desktop/tsconfig.json --noEmit` clean.
`npx vitest run src/components/pet src/store/pet.test.ts` — 4 files, 41
tests, all passing. File touched: `pet-sprite.tsx`. Commit `0b89c317e`.

### Fork-only fix — 2026-07-28 (desktop: Pet Zone dragged into the bottom dock made the whole bottom band unresizable)

**Reported:** user enabled Pet Zone and dragged it into the bottom dock next
to the Terminal/Logs tab group, landing side-by-side rather than stacked as
a third tab. Once there, dragging the sash between the main workspace and
the bottom band no longer resized it.

**Root cause:** the pane-shell layout tree resizer treats a resize boundary
as owned by "the FIXED zone(s) that touch that edge." Before this fix,
`edgeFixedZone` (`track-model.ts`) walked a cross-axis run (zones laid out
side-by-side, e.g. `row([terminal+logs, pet-zone])` sized along the
`column` axis) and returned only the FIRST fixed zone it found, using a
`for...return`. `tree-split.tsx`'s drag handler (`sideFor`) then wrote the
live height override to only that one zone's panes on every pointermove.
The sibling zone (whichever one wasn't first) kept its old, un-overridden
height — and because `fixedTrackSize`'s cross-axis branch sizes the whole
row to `cssMax()` of its children, that stale sibling silently reclamped
the entire band back to its old size on every render, making the seam feel
undraggable. The double-click "reset to default" handler had the identical
single-zone bug. A second, quieter bug: `sizingFor` (which the ANCESTOR
column split reads to apply a CSS `min-height`/`max-height` floor to the
band) only recognized a direct `group` child — once the band became a
`split` of two zones it stopped contributing any floor/ceiling to the
ancestor at all, independent of the drag path.

**Fix:** `apps/desktop/src/components/pane-shell/tree/renderer/track-model.ts`
— replaced `edgeFixedZone` (single `GroupNode | null`) with
`edgeFixedZones` (returns every fixed zone touching the edge across a
cross-axis run, not just the first) and added `edgeZonesClamp` (combines
several zones' declared min/max into one tightest-floor/tightest-ceiling
drag clamp). `apps/desktop/src/components/pane-shell/tree/renderer/tree-split.tsx`
— `sideFor` (live drag) and `resetBoundary` (double-click reset) both now
write/clear overrides across every zone `edgeFixedZones` returns instead of
assuming one; `sizingFor` now also recurses into a nested `split` child (not
just a bare `group`), combining its fixed sub-zones' clamps the same way, so
the ancestor split keeps enforcing a real CSS floor/ceiling on a band that
became a two-zone row instead of a single group.

**Verification:** `npx tsc -p apps/desktop/tsconfig.json --noEmit` clean
(zero errors, whole desktop package). `npx vitest run
src/components/pane-shell` — 5 files, 15 tests, all passing (no existing
behavior regressed, including the single-fixed-zone/no-op case which is now
just `edgeFixedZones` returning a 1-element array). Files touched:
`track-model.ts`, `tree-split.tsx`. Commit `4b474017b`.

### Fork-only fix — 2026-07-28 (5th recurrence of the spinner-timer corruption: root cause was on the OTHER side of the pty — xterm.js's own Unicode-11 table disagrees with the 2026-07-24 fix)

**Reported (again):** a live `process(action="wait", timeout=300)` spinner
line inside Hermes Desktop's own embedded terminal tab (title "polaris")
showed `wait proc_c043482f975c 300s (4m361s)` — a seconds remainder >= 60,
mathematically impossible from `elapsed // 60` / `elapsed % 60` arithmetic
(verified: current code can only ever produce `_s` in `[0, 59]`). Same bug
class as the 2026-07-06, 2026-07-19, and 2026-07-24 entries below — this is
the 4th documented occurrence, now root-caused for real.

**Investigation:** confirmed the running deployment already has the
2026-07-24 `display_cwidth()` fix (byte-identical file content; the
apparent "missing commit" from a `git merge-base --is-ancestor` check was a
false negative from `~/.hermes/hermes-agent`'s shallow clone, not a missing
fix — the deployed file was inspected directly and matches). Confirmed the
formatting arithmetic cannot overflow. That left one place left to check:
whether the terminal actually *displaying* these bytes agrees with
`display_cwidth()`'s glyph-width assumption.

**Root cause, this time on the other side of the boundary:** Hermes
Desktop's own embedded terminal pane (`apps/desktop/src/app/right-sidebar/
terminal/use-agent-terminal.ts`) renders via `xterm.js` +
`@xterm/addon-unicode11`, activated with `term.unicode.activeVersion =
'11'`. Extracted xterm.js's actual shipped `UnicodeV11.wcwidth` /
`getStringCellWidth` algorithm (verbatim from the npm package, not
reimplemented) and ran it standalone in Node against Hermes's own
registered tool emoji: xterm.js's Unicode-11 table reports width **1**
(not 2) for `⚙️` (GEAR+VS-16, the `process` tool's emoji used in the
screenshot's `wait proc_...` spinner line), and the same for `✍️`, `✉️`,
`⚠️`, `⌨️`, `◀️`, `🖼️`, `👁️`, `🖥️`, `🗣️`, `❤️` — the exact glyph shape the
2026-07-24 fix corrected Python's side to treat as 2 cells (matching
iTerm2/Kitty/Terminal.app/Windows Terminal). So after that fix, Python
*reserves* height assuming these sequences are 2 cells wide, but Hermes's
own xterm.js terminal pane only *advances the cursor* 1 cell for the same
bytes — landing the reserved wrap height exactly 1 row short and producing
the same "wrapped continuation overlaps the row below" corruption as
before, just with the mismatch moved to the other side of the pty. This
also explains why the 2026-07-19 forensic logging and earlier reports
never pinned it down cleanly: the corruption only manifests when a Hermes
CLI/TUI session runs *inside Hermes Desktop's own terminal tab*, not in an
external terminal emulator that (unlike xterm.js's Unicode-11 table) treats
VS-16 sequences as 2 cells consistently with the Python side.

**Why not patch xterm.js's Unicode table instead:** considered and
rejected (confirmed via a second-opinion review before committing). Two
reasons: (1) it would only fix Hermes's own embedded pane — cli.py also
runs in tmux, VS Code's integrated terminal (also xterm.js!), Hyper,
Windows Terminal, ssh sessions, etc., none of which this touches, so the
next differently-consensus terminal would just be fix #6; (2) overriding
`term.unicode.activeVersion`'s width table for the *whole pane* risks
misrendering every other program's output rendered in that same terminal,
not just Hermes's own spinner line — new mismatches, not fewer.

**Fix:** stop depending on VS-16 width consensus for Hermes's own tool
emoji at all — strip the trailing `\ufe0f` from every registered tool
emoji that had one, keeping the bare base codepoint. Every measured width
table (`get_cwidth`, `display_cwidth`, and xterm.js's own Unicode-11
table) already agrees unambiguously on the *bare* codepoint's width, so
there's no longer a boundary for the two sides of the pty to disagree
across. Changed 16 registrations across 11 files:
`tools/process_registry.py` (`process`), `tools/browser_tool.py`
(`browser_type`/`browser_press`/`browser_back`/`browser_get_images`/
`browser_vision`/`browser_console`, 6 entries), `tools/close_terminal_tool.py`
(`close_terminal`), `tools/read_terminal_tool.py` (`read_terminal`),
`tools/file_tools.py` (`write_file`), `tools/feishu_drive_tool.py` (2
comment-reply entries), `tools/skills_tool.py` (skills warning),
`tools/vision_tools.py` (`vision_analyze`), `tools/yuanbao_tools.py`
(`yb_send_dm`), `plugins/spotify/__init__.py` (`spotify_library`, found via
the new regression test below — missed by the initial `emoji=` grep since
plugin tool registration uses a tuple, not a keyword arg), and
`plugins/google_meet/__init__.py` (`meet_say`, same tuple-registration
pattern). Left `gateway/run.py` and `gateway/platforms/base.py`'s
`get_tool_emoji(..., default="⚙️")` call sites untouched — those are the
messaging-platform (Discord/Slack/Telegram/etc.) tool-progress renderer,
which has its own independent formatting path and isn't implicated in
cli.py's prompt_toolkit wrap-height math.

**Verification:** added
`TestNoRegisteredEmojiUsesVS16.test_no_registered_tool_emoji_contains_variation_selector_16`
to `tests/agent/test_display_cwidth_vs16.py` — scans the live tool
registry (`tools.registry.registry._tools`) and fails if any future change
reintroduces a VS-16 tool emoji (this test is what caught the
`spotify_library` and `meet_say` misses on the first pass, since those
live in plugin tuples rather than the `emoji=` keyword grep pattern used
for the initial sweep). Full `tests/agent/test_display_cwidth_vs16.py` +
`test_display_emoji.py` + `test_display.py` +
`test_kawaii_spinner_display_width.py` + `tests/cli/test_cli_status_bar.py`
+ `tests/tools/test_spotify_client.py` + `tests/plugins/
test_google_meet_plugin.py` + `tests/hermes_cli/test_spotify_auth.py`: 219
passed, 0 failed. Also independently re-ran the extracted xterm.js
Unicode-11 algorithm against every changed emoji post-fix and confirmed
all now measure width=1 on both `display_cwidth()` and xterm.js's table —
zero disagreement remains.

### Fork-only fix — 2026-07-28 (single-subagent status printed a new scrollback line every 30s instead of updating in place)

**Symptom (reported live in the CLI, on the work MacBook):** a `delegate_task()`
call with exactly ONE subagent showed a fresh scrollback line every ~30s for
heartbeat / "still waiting on provider" ticks instead of a single row updating
in place:

```
[subagent-0] Still waiting on provider — 30s elapsed ...
[subagent-0] Still waiting on provider — 60s elapsed ...
[subagent-0] Still waiting on provider — 90s elapsed ...
```

Looked exactly like a frozen/broken status line spamming duplicate text — it
never disappeared or got replaced.

**Root cause:** `SwarmBoard` (`tools/swarm_board.py`, fork-only, zero upstream
merge surface — added 2026-05-04) is the live multi-row display that redraws
one row per subagent in place every 250ms via a prompt_toolkit widget. Its
`maybe_start()` gate only activated for `n_children >= 2` (batches). A
single-child `delegate_task()` call always fell through to the `_NoopBoard`,
so its heartbeat/wait-notice text went through `agent._emit_status()` ->
`_vprint(force=True)` -> raw `print()` -> a brand-new scrollback line every
tick, forever.

**Fix (3 sites):**

- `tools/swarm_board.py`: `SwarmBoard.maybe_start()` now activates for
  `n_children >= 1` (was `>= 2`), still gated on a CLI host exposing the
  required widget hooks (`_swarm_board_show`/`_swarm_board_hide`/
  `_invalidate_app`) and the `HERMES_SWARM_BOARD=0` escape hatch.
- `tools/delegate_tool.py`: the `n_tasks == 1` fast path now wraps
  `_run_single_child` in the same `SwarmBoard.maybe_start()` context the
  `n_tasks > 1` batch path already used — registers the one row, routes
  child chatter into the row's note slot via `make_child_print_fn`, and
  stashes/clears `parent_agent._swarm_board` around the call. Row completion
  (`finish()` -> terminal status icon) was already correctly wired for
  single-child via `_build_child_progress_callback`'s existing
  `"subagent.complete"` handler (runs regardless of batch size) — no change
  needed there.
- `agent/chat_completion_helpers.py`: the ~30s heartbeat loop now checks
  `agent._swarm_board` (set only when this agent IS a delegated child inside
  an active board) and routes the "thinking +N chars" / "still waiting on
  provider" text into `board.note(sid, text)` instead of `_emit_status()`, so
  it updates the same row instead of printing a new line. `board.note()`
  failures fall back to the original `_emit_status()` text (logged at debug)
  rather than silently dropping the whole heartbeat tick.
  `_emit_wait_notice()` (a different UI surface — the parent's own live
  spinner text) is unconditional since it can't duplicate a board row.

**Test fix:** `tests/tools/test_async_delegation.py::
test_delegate_task_background_detaches_child_from_parent` patched
`dt._run_single_child` inside a `patch.object(...)` block that exited
(reverting the patch) before synchronizing with the background
daemon-executor worker thread — a pre-existing race that was "lucky" before
and became a consistent failure once SwarmBoard registration added latency
to the worker's pre-call setup. Fixed with a `threading.Event` the mock sets
on entry, asserted INSIDE the patch block. `tests/tools/test_swarm_board.py`:
renamed/updated `test_single_child_returns_noop` ->
`test_single_child_returns_real_board` to match the new gating.

**Known limitation (documented inline, pre-existing, not introduced by this
change):** `parent_agent._swarm_board` is a single-slot attribute (same
pattern the `n_tasks > 1` path already used) — not safe against two
concurrent `delegate_task()` calls on the same parent (e.g. overlapping
background dispatches). Worst case is a missed/misdirected row update, not a
crash; the CLI widget itself (`cli_ref._swarm_board`) is also single-slot so
only one board renders at a time regardless.

**Files touched:** `tools/swarm_board.py`, `tools/delegate_tool.py`,
`agent/chat_completion_helpers.py`, `tests/tools/test_async_delegation.py`,
`tests/tools/test_swarm_board.py`.

**Verification:** `scripts/run_tests_parallel.py` across `tests/agent/` +
`tests/tools/` filtered to delegation/swarm/async/heartbeat/stream_phase/ttfb
— all pass (repeated 3x for the fixed race test, no flakes). Full
`tests/agent/` + `tests/tools/` sweep shows the same 37 pre-existing failures
(macOS tmp-path/AF_UNIX length, unrelated to this change) on this branch and
on unmodified `main` — confirmed via a `git stash` control run.

Commit `d4fd4bfb2`, pushed to `origin/main`.

### Fork-only fix — 2026-07-26 (pre-existing test-suite failures: 6 real production bugs + ~50 stale-mock/assertion fixes)

**Symptom:** ~54 pre-existing pytest failures across `tests/run_agent/`,
`tests/cli/`, `tests/hermes_cli/`, `tests/tools/`, `tests/agent/`,
`tests/acp/`, `tests/tui_gateway/` — unrelated to the same-day CI-lockfile
fix above, these were failing on `main` before that fix even landed
(uncovered once CI could run at all). Root causes fell into two buckets:
genuine production bugs, and test doubles/assertions that had drifted out
of sync with legitimate upstream/fork changes (new kwargs, renamed fields,
new guard clauses, deliberately-changed defaults).

**Real production bugs found and fixed (not test-only):**
1. `agent/conversation_loop.py::run_conversation()` returned
   `final_response: None` on the refusal/internal-error dict-return paths
   instead of the actual message text.
2. `cli.py::_persist_global_model_switch` wrote `""` instead of `None` for
   a cleared base_url/api_mode, contradicting its own docstring and the
   sibling branch's behavior. Same function's `/fast` handler always
   persisted to config on every toggle; added a `--global`/`-g` flag so it's
   session-scoped by default (matches the test's documented intent).
3. `plugins/memory/holographic/retrieval.py::_sanitize_fts_query` only
   split the query on whitespace, so a hyphenated token like `"PLAT-15800"`
   collapsed into one glued token (`plat15800`) that could never match
   FTS5's own `unicode61` tokenizer output for the same string (which
   splits on `-` into `plat` + `15800`). Any hyphenated identifier in a
   recall query silently returned zero results. Fixed by replacing `-`
   with a space before the whitespace split.
4. `tools/memory_tool.py` — the hot-tier `read` action reused
   `_success_response()`, a helper whose docstring explicitly says it
   withholds the entries list (deliberately, to stop the model from
   re-issuing writes after every add/replace/remove). Reusing it for `read`
   meant `/memory read` returned `entry_count` but never the actual
   `entries` — defeating the action's entire purpose. Built a
   read-specific response inline instead of reusing the write-shaped
   helper.
5. `agent/auxiliary_client.py::resolve_vision_provider_client`'s
   memoization cache key was `(provider, model, base_url, api_key,
   async_mode)` — it omitted `main_runtime` entirely. Two callers with
   identical explicit args (the common case: most call sites pass all
   default args and rely on ambient/context runtime) but different
   `main_runtime` would silently share one cached `(provider, client,
   model)` tuple, leaking one session's vision endpoint/model into an
   unrelated session/thread. Caught via cross-test pollution
   (`test_explicit_vision_runtime_wins_over_stale_ambient_runtime` failing
   only when run after `test_concurrent_vision_probes_...`), but the same
   collision is reachable in production. Fixed by folding the same
   runtime-aware discriminator tuple `_client_cache_key` already uses for
   this exact reason into the vision cache key too.
6. `tools/lazy_deps.py` had `anthropic==0.87.0` pinned while
   `pyproject.toml` had already moved to `0.100.0` — pins had drifted out
   of sync (both versions are past the CVE-34450/34452 fix line, so this
   was a consistency bug, not a security one).
7. `hermes_cli/config.py::_AUX_TASK_FIRST_KEYS` was missing `pet_dialogue`,
   causing that auxiliary task's config block to be misdetected as
   "provider-first" and silently mis-saved. `hermes_cli/main.py`'s
   `_BUILTIN_SUBCOMMANDS` was missing `submit`, causing it to be excluded
   from top-level `--help` subcommand listings.

**Stale test doubles/assertions fixed (production behavior was correct,
tests hadn't caught up):** MagicMock auto-vivifying `.beta.messages.stream`
as truthy and silently bypassing tests that only patched `.messages.stream`
(the fork's OAuth beta-path preference introduced this); missing
`sticky_active`/`model=`/`provider_override=` kwargs on fakes after real
signatures grew; a retry-budget assertion (`< 3`) that predated a
deliberate bump to `< 4`; a default-aux-model assertion
(`claude-haiku-4-5-20251001`) that predated the deliberate upgrade to
`claude-sonnet-5` for compression quality + 1M-context beta eligibility;
this fork's MCP tool naming convention (`{server}_{tool}`, no `mcp__`
prefix — see `tools/mcp_tool.py::is_mcp_tool_parallel_safe`) vs. a test
still asserting the upstream `mcp__{server}__{tool}` shape; toolset
composite drift (`consult`, a fork-only "second opinion" tool, missing
from an expected-toolsets list); a background-review read-before-write
guard added after its test was written.

**Left unresolved (documented, not swept under the rug):**
`tests/tools/test_mcp_circuit_breaker.py::test_half_open_dead_session_recovers_after_reconnect`
and `test_half_open_probe_on_dead_session_requests_reconnect` — dead-session
recovery moved from the old `_signal_reconnect`-based mechanism these tests
exercise to a newer `_ensure_server_connected` lazy-spawn subsystem that
does a real async config-backed connection attempt. The tests' stub setup
(`_mcp_loop = None`, no real config entry, `_is_recycled_stdio=False`) is
incompatible with the new dual-path branching in
`tools/mcp_tool.py::_make_tool_handler` — patching just the config-presence
check makes the test hang (real connect attempt against a nonexistent stub
command) instead of pass. Needs either a graceful "reconnecting" error path
added to `_ensure_server_connected` itself, or a test rewrite with a
working fake transport — real follow-up work, not a mock patch.

**Verification:** `scripts/run_tests.sh` (the CI-matching per-file-isolated
runner) across all touched files: 34 files, 2063 tests passed, 0 failed, 8
workers. `ruff check .` and `scripts/check-windows-footguns.py --all` both
clean. Confirmed via `git stash` that 3 unrelated pre-existing failures in
`test_file_tools.py` / `test_execution_flag_detection.py` /
`test_web_tools_config.py` are environment-specific and fail identically
without any of this session's changes — left untouched, out of scope.

**Files touched (production):** `agent/auxiliary_client.py`,
`agent/conversation_loop.py`, `cli.py`, `hermes_cli/config.py`,
`hermes_cli/main.py`, `plugins/memory/holographic/retrieval.py`,
`plugins/memory/holographic/store.py`, `tools/lazy_deps.py`,
`tools/memory_tool.py`, `tools/memory_warm.py`. Plus ~25 test files with
mock/assertion updates (see diff for the full list).

**Follow-up same-day (2026-07-27): 3 more failures surfaced by real CI**
that hadn't reproduced in local runs (test-order and OS dependent):
1. `tests/plugins/memory/test_holographic_retrieval.py` had a parametrized
   case (`"context: length-probe"` → `{"context", "lengthprobe"}`) that
   encoded the exact pre-fix buggy behavior of the `_sanitize_fts_query`
   hyphen bug fixed above — CI runs this file in a slice/order where it
   wasn't shadowed by a coincidentally-passing local run. Updated the
   expectation to the correct `{"context", "length", "probe"}`.
2. `tests/tools/test_web_providers_claude_code.py::test_check_web_api_key_true_when_claude_code_configured`
   relied on ambient plugin-registration state (`agent.web_search_registry`
   only knows about a provider once some earlier-run test/import has called
   its `register(ctx)` hook) — passes when run after enough of the suite,
   fails when CI's parallel slicing puts it first. Fixed by having the test
   call `register_provider(ClaudeCodeWebProvider())` directly
   (`register_provider()`'s own docstring says repeated registration is
   explicitly safe — "makes hot-reload scenarios (tests, dev loops) behave
   predictably").
3. `tests/tools/test_mcp_circuit_breaker.py`'s two half-open-probe tests
   (`test_half_open_probe_on_dead_session_requests_reconnect`,
   `test_half_open_dead_session_recovers_after_reconnect`) — the real fix
   this time, not deferred. Confirmed via a second-opinion review that the
   two reconnect mechanisms (`_request_lazy_reconnect` for recycled stdio,
   `_ensure_server_connected` lazy-spawn for everything else) are
   legitimately different by design, not a product gap — the tests'
   `_is_recycled_stdio=False` stub setup means they were always meant to
   exercise the lazy-spawn path, they just predated that path's addition
   and asserted the other branch's (`_reconnect_event`/"reconnect" wording)
   contract instead. Rewrote both to mock `_connect_server` to fail fast
   (`ConnectionRefusedError`) instead of hanging on a real spawn attempt,
   and assert on `_ensure_server_connected`'s actual clean-failure contract
   ("failed to connect" + breaker bump). Also manually audited
   `_run_on_mcp_loop`'s timeout path (the thing that would matter if this
   *were* a production gap) and confirmed it does real bounded polling with
   `future.cancel()` on deadline — not a naive blocking wait — so no
   follow-up production fix was needed there.

Re-verified: `scripts/run_tests.sh` across all touched + previously-touched
files: 272 files, 4750 tests passed, 0 failed. Confirmed via `git stash`
that `test_web_tools_config.py`, `test_base_environment.py` also have
pre-existing failures unrelated to any of this session's changes (fail
identically without the diff) — left untouched, out of scope, same as the
`test_file_tools.py` / `test_execution_flag_detection.py` findings above.

### Fork-only fix — 2026-07-26 (CI Lint + uv.lock/CI Tests permanently red — relative exclude-newer + missing encoding=)

**Symptom:** GitHub Actions "CI" workflow failing on every push to `main` for
this fork — `Python lints / ruff enforcement (blocking)`,
`Python lints / Windows footguns (blocking)`, `Python tests / Run tests slice
N/8` (all 8 slices, at the `uv sync --locked` install step, before any test
ran), and `Check uv.lock / uv lock --check` all red.

**Root cause (uv.lock / tests):** `pyproject.toml`'s `[tool.uv]` had
`exclude-newer = "7 days"` — a *relative* duration. uv resolves relative
durations to an absolute cutoff timestamp at the moment `uv lock` (or `uv
sync --locked`, which re-resolves to verify) runs, so the effective cutoff
drifts forward every time CI runs, permanently out of sync with whatever
absolute timestamp got baked into the committed `uv.lock` the last time
someone ran `uv lock` locally. CI error was exactly this: `Resolving despite
existing lockfile due to change of exclude newer timestamp ... error: The
lockfile at uv.lock needs to be updated, but --locked was provided.` This is
a known upstream footgun — it was removed once (PR #21221, 2026-05-07) then
reintroduced hours later in the v0.13.0 release commit (498bfc7bc1), and has
apparently been reintroduced again since (present on `upstream/main` as of
2026-07-26).

**Root cause (ruff / Windows footguns):** 6 `open()` calls across
`scripts/hermes_token_check.py`, `scripts/hermes_usage_tracker.py`, and
`tools/bridges/cc_proxy_mcp.py` were missing the explicit `encoding=`
argument ruff's `PLW1514` rule requires (also a Windows footgun: default
text-mode encoding is platform-dependent — cp1252 on Windows vs UTF-8 on
macOS/Linux — so an unspecified-encoding `open()` can silently mis-decode
non-ASCII bytes on Windows even though it "works" everywhere the author
tested it). Separately, `hermes_cli/gateway.py:launchd_install()` called bare
`os.getuid()`, which doesn't exist on Windows and raises `AttributeError` at
call time if that code path is ever reached on a Windows install.

**Fix:**
- `pyproject.toml`: `exclude-newer = "7 days"` → `exclude-newer =
  "2026-07-19T00:00:00Z"` — a full RFC 3339 **UTC** timestamp, not just an
  absolute date. First attempt used a bare date (`"2026-07-19"`, no time/
  offset) which fixed the relative-duration drift but introduced a second,
  subtler footgun: uv resolves a bare date in the *local system timezone*
  of whatever machine runs `uv lock`/`uv sync`, not UTC — so `uv lock` on a
  CDT (UTC-5) laptop baked in a different absolute cutoff than `uv sync
  --locked` on GitHub Actions' UTC runners, reproducing the exact same
  "lockfile needs updating" failure with a different root cause. Caught
  when the first push's CI run still failed identically; confirmed the fix
  by checking `uv lock --check` under `TZ=UTC`, `TZ=America/Los_Angeles`,
  and `TZ=Asia/Tokyo` locally — all three now agree. Comment added at the
  site explaining both footguns and pointing at this history. Bump this
  timestamp by hand (always with a trailing `Z`) + run `uv lock` when you
  want a newer cooldown window.
- `uv lock` regenerated against the new absolute cutoff — also picked up a
  drifted `hermes-agent` self-version entry in `uv.lock` (0.18.2 vs
  `pyproject.toml`'s already-bumped 0.19.0) and a handful of genuine
  upstream dependency bumps (honcho-ai, lark-oapi, nemo-relay,
  slack-bolt/slack-sdk) that had accumulated since the lockfile was last
  regenerated.
- Added explicit `encoding="utf-8"` to all 6 flagged `open()`/`read_text()`
  calls; also converted a bare `open()`-without-`with` in
  `hermes_token_check.py` to a proper context manager while there (unrelated
  leaked-fd bug, same line).
- `hermes_cli/gateway.py`: `os.getuid() == 0` → `hasattr(os, "getuid") and
  os.getuid() == 0`. `launchd_install` is only ever called from an
  `is_macos()` guard today, so this can't currently fire on Windows, but the
  bare call still trips the static checker (and would raise immediately if a
  future call site loses that guard).

**Files touched:** `pyproject.toml`, `uv.lock`, `scripts/hermes_token_check.py`,
`scripts/hermes_usage_tracker.py`, `tools/bridges/cc_proxy_mcp.py`,
`hermes_cli/gateway.py`.

**Verification:** `PYTHONPATH= uvx ruff check .` → `All checks passed!`.
`PYTHONPATH= python3 scripts/check-windows-footguns.py --all` → `No Windows
footguns found (838 file(s) scanned)`. `PYTHONPATH= uv lock --check` → clean.
`PYTHONPATH= uv sync --locked --python 3.11 --extra all --extra dev` →
succeeds (matches the exact CI command). Ran the existing test suite
targeting the changed files (`tests/tools/test_cc_proxy_mcp.py` — 5/5 pass;
`tests/hermes_cli/test_gateway_service.py` — 183 pass / 6 skipped, including
several tests that already `monkeypatch.setattr(os, "getuid", ...)` and
exercise `launchd_install` directly, confirming the `hasattr` guard doesn't
change behavior on platforms where `getuid` exists). A local unsharded
`scripts/run_tests.sh` full-suite run surfaced pre-existing, unrelated
macOS-local-environment failures (a sensitive-system-path guard tripping on
pytest's `/private/var/folders` tmpdir, and a couple of test-order-dependent
failures that don't reproduce when run individually or via CI's actual
per-file-isolated slicing) — confirmed unrelated by reproducing the same
failures on a clean `git stash` of this change.

### Fork-only fix — 2026-07-26 (self-update relaunch fixup stripped mac entitlements + hardened runtime)

**Symptom:** a from-scratch local macOS build (`npm run dist:mac`/`pack`, or
`CSC_IDENTITY_AUTO_DISCOVERY=false ... -c.mac.identity='-'`) is ad-hoc signed
WITH entitlements (`electron/entitlements.mac.plist`: JIT, unsigned-executable-
memory, disable-library-validation, audio-input) and the hardened-runtime flag
(`mac.hardenedRuntime: true` in `package.json`'s `build` config) —
`codesign -dv` shows `flags=0x10002(adhoc,runtime)`. But every subsequent
in-app self-update (`hermes update` → `hermes desktop --build-only` →
electron-builder `--dir` rebuild → relaunch) silently regressed the packaged
app back to `flags=0x2(adhoc)` with **zero entitlements** — confirmed via
`codesign -d --entitlements -` on `/Applications/Hermes.app` immediately after
an observed self-update cycle.

**Root cause:** `hermes_cli/main.py`'s `_desktop_macos_relaunchable_fixup()`
runs after every self-update rebuild to keep the bundle relaunchable (an
ad-hoc signature has no stable Team ID, so a rebuilt bundle's new cdhash reads
as tampering to Gatekeeper/LaunchServices otherwise, producing "Hermes is
damaged and can't be opened"). Its re-sign command was a bare
`codesign --force --deep --sign -` — no `--entitlements`, no
`--options runtime` — so it clobbered whatever the original packaged build had
signed in, on every single update.

**Fix:** `_desktop_macos_relaunchable_fixup()` now re-signs with
`--options runtime --entitlements electron/entitlements.mac.plist` (same
inputs electron-builder used for the original packaged build), falling back to
the prior bare ad-hoc re-sign only if the entitlements file can't be found
(never worse than the prior behavior). No-op preserved for real signing
identities (`CSC_LINK`/`APPLE_SIGNING_IDENTITY`). Neither entitlement is
actually load-bearing without hardened runtime (mic access is TCC/Info.plist-
driven regardless of the sandbox entitlement; library validation is only
enforced under hardened runtime), so the prior behavior wasn't a functional
break for this app's shape today — but this closes the gap so a self-updated
build stays byte-for-byte equivalent, capability-wise, to a fresh local build
as the app's needs grow (e.g. any future feature that DOES require hardened
runtime + a specific entitlement).

**Files:** `hermes_cli/main.py` (`_desktop_macos_relaunchable_fixup`),
`tests/hermes_cli/test_gui_command.py` (3 new tests: entitlements+hardened-
runtime re-sign, fallback to bare ad-hoc when the entitlements file is
missing, no-op preserved for a real signing identity).

**Verification:** manually reproduced the regression against a live
`/Applications/Hermes.app` post-self-update (`codesign -dv` showed
`flags=0x2(adhoc)`, zero entitlements); confirmed the new re-sign command
(`codesign --force --deep --options runtime --entitlements ... --sign -`)
against a scratch copy of that same regressed bundle restores
`flags=0x10002(adhoc,runtime)` + all 4 entitlements, `codesign --verify --deep
--strict` exits 0, and the re-signed bundle launches cleanly (no Gatekeeper
"damaged" error). `tests/hermes_cli/test_gui_command.py` 65/65 passed (62
pre-existing + 3 new); `py_compile` clean.

### Fork-only fix — 2026-07-26 (relaunch fixup only ran on the "rebuild happened" branch, not the "already up to date" skip branch)

**Symptom:** the fix above landed, but a subsequent self-update cycle still
relaunched `/Applications/Hermes.app` with `flags=0x2(adhoc)` and zero
entitlements — the fixed code hadn't actually run yet.

**Root cause:** `cmd_gui()`'s call to `_desktop_macos_relaunchable_fixup()` was
nested inside the `if build_needed:` branch of the content-hash stamp check.
When the stamp matches (nothing under `apps/desktop/` or the lockfiles
changed since the last successful build — the common case for a self-update
that only pulled backend/Python changes), the entire rebuild, including the
fixup call, is skipped and `hermes desktop --build-only` just re-signs
nothing and relaunches whatever bundle is already on disk. A signing fix
shipped in `_desktop_macos_relaunchable_fixup()` itself therefore couldn't
reach an already-packaged bundle until some unrelated future change happened
to invalidate the content-hash stamp and force a real rebuild — which could
be arbitrarily long, or never, on a machine that keeps working from Python
code changes alone.

**Fix:** moved the `_desktop_macos_relaunchable_fixup()` call out from inside
the `if build_needed:` branch to right after the whole `if skip_build: ...
else: ...` block, gated only on `not source_mode and packaged_executable is
not None`. It now runs unconditionally on every `hermes desktop` invocation
that has (or produces) a packaged executable — whether that came from
`--skip-build`, the stamp-match skip, or a fresh rebuild. Re-signing an
already-correctly-signed bundle is a cheap, idempotent no-op, so this has no
functional cost on the common case where nothing was actually wrong.

**Immediate remediation:** re-signed the live, already-regressed
`/Applications/Hermes.app` in place by hand (`xattr -cr` + the same
`codesign --force --deep --options runtime --entitlements ... --sign -`
command the fixed code now runs) so the fix takes effect immediately rather
than waiting for a future rebuild trigger. Confirmed
`flags=0x10002(adhoc,runtime)` + all 4 entitlements restored,
`codesign --verify --deep --strict` exits 0.

**Files:** `hermes_cli/main.py` (`cmd_gui`'s desktop build flow),
`tests/hermes_cli/test_gui_command.py` (2 new tests proving the fixup is
called on both the stamp-match skip path and the `--skip-build` path — both
fail against the pre-fix code).

**Verification:** `tests/hermes_cli/test_gui_command.py` 67/67 passed (65
pre-existing + 2 new); full `tests/hermes_cli/` suite run — 19 pre-existing
unrelated failures (model-switch/TTS/kanban/service-manager tests) confirmed
identical with this change stashed out via `git stash`, i.e. not caused by
this diff; `py_compile` clean.

### Fork-only feature — 2026-07-26 (repoint all "get code/docs from here" links at the fork + scripts/sync-fork-branding.py)

**Symptom:** the installed runtime at `~/.hermes/hermes-agent` lost all 97
fork-only commits — its `origin` had silently ended up pointing at
`NousResearch/hermes-agent` instead of the fork, and its git history had
collapsed to the vanilla upstream tip. Recovered by repointing `origin` back
at the fork and re-running `hermes update` (which correctly detected the
divergence and reset to the fork tip).

**Root cause:** `scripts/install.sh` / `install.ps1`'s fresh-clone path (and
the desktop/Tauri bootstrap's `raw.githubusercontent.com` script downloader)
hardcode `NousResearch/hermes-agent` with zero fork-awareness. Any repair/
reinstall flow that wipes the checkout and re-runs the fresh-clone branch —
which is exactly what happened — silently reverts to vanilla upstream. Traced
via `~/.hermes/hermes-agent/.git`'s reflog (a literal `clone: from
https://github.com/NousResearch/hermes-agent.git` entry) and
`~/.hermes/logs/update.log` (a 00:42 run correctly said "Updating from
fork..."; the 13:00 run had no such message — origin was already wrong by
then).

**Fix:**
1. Repointed every "where do I get code/docs from" reference at
   `adurham/hermes-agent` across ~184 files: `scripts/install.sh`/`install.ps1`/
   `install.cmd`, the desktop/Tauri bootstrap downloaders, `hermes_cli/main.py`'s
   ZIP-fallback + curl-recovery message, `model_catalog.py`, `package.json`,
   `docusaurus.config.ts`, READMEs (4 languages), CONTRIBUTING, issue/PR
   templates, Nix package metadata, User-Agent/Referer self-identification
   strings, and the ~190 files under `website/docs/` + the zh-Hans i18n tree
   (embedded docs-site links resolved to real `github.com/.../blob/main/...`
   file paths, not guessed).
2. Deliberately left untouched: fork-vs-upstream *detection* constants
   (`OFFICIAL_REPO_URL`, `KNOWN_UPSTREAM_URLS`, `_CANONICAL_REPO`,
   `update-remote.ts`'s equivalents — repointing these would make the tool
   think its own fork IS upstream and disable the exact protection that
   would've prevented this incident), live Nous services (portal, inference
   API, Discord), legal attribution, historical issue/PR/security-advisory
   citations, Docker Hub/Homebrew/Releases pointers (fork publishes none of
   these yet), and `tools/skills_hub.py`'s `OFFICIAL_REPO` (skill-provenance
   attribution, not a source pointer).
3. Fixed a related bug found in passing: the in-app Docs page (`web/src/pages/DocsPage.tsx`)
   iframed the docs link — harmless while it pointed at the hosted Docusaurus
   site, but a GitHub tree URL refuses to be framed, so it would've rendered
   permanently blank. Replaced the iframe with an "Open Documentation" button.
4. Added `scripts/sync-fork-branding.py` to make this repeatable: every file
   upstream still calls `NousResearch/hermes-agent`, so merging upstream back
   in reintroduces exactly these links, source-of-truth ones included. Wired
   into `scripts/fork-merge-plan.py`'s printed merge recipe and documented
   under "Merging upstream" above — run it after every upstream merge, not
   just once. Idempotent; reports anything it can't safely auto-resolve
   instead of guessing.

**Verification:** all fork-detection invariants confirmed unchanged via
grep; `tests/test_install_*.py` (66), `apps/desktop/electron/update-remote.test.ts`
(6), `tests/hermes_cli/test_model_catalog.py` + `test_web_server.py` +
`tests/acp/test_registry_manifest.py` + `tests/tools/test_skills_hub.py`
(591 combined) all pass; `tsc --noEmit` and `py_compile` clean across every
touched file. One pre-existing, unrelated TTS test failure and one
pre-existing fork_banner test-ordering flake both reproduce identically on
unmodified `main` (confirmed via `git stash`) — not caused by this change.
`scripts/sync-fork-branding.py --dry-run` run twice in a row produces zero
changes the second time (idempotency confirmed).

### Fork-only fix — 2026-07-25 (npm audit: 19 high-severity vulns → 0, react-router v7→v8 + minimatch/brace-expansion overrides)

**Symptom:** `npm install` at repo root reported 19 high-severity
vulnerabilities; `npm run pack` in `apps/desktop` (the packaged-build path)
surfaced this on every fresh install.

**Root cause (two independent vuln classes):**
1. 17/19 were devDependency-only, all transitively pinned to old
   `minimatch`/`brace-expansion` (`GHSA-mh99-v99m-4gvg` — DoS via
   `expand()` producing unbounded-length results, uncatchable OOM crash)
   via the `electron-builder` build toolchain (`@electron/asar`,
   `@electron/universal`, `app-builder-lib`, `dmg-builder`, `ejs`, `jake`,
   `filelist`, `electron-winstaller`, etc.) and the `eslint`/
   `eslint-plugin-react` lint toolchain. These only ever process our own
   source tree paths at build/lint time — no attacker-controlled input
   reaches them — but electron-builder's real fix is v27, still
   alpha-only on npm (no stable release), so bumping the top-level package
   wasn't an option yet.
2. 2/19 were `react-router`/`react-router-dom`, a real *production*
   dependency shipped in the built app: `GHSA-qwww-vcr4-c8h2`, an RSC-mode
   CSRF bypass. Confirmed via grep across all ~65 import sites in
   `apps/desktop` and `web` that neither app uses any RSC/`unstable_`
   APIs (plain SPA routing — `HashRouter`/`BrowserRouter`/`MemoryRouter`,
   `useNavigate`, `Routes`/`Route`, no `RouterProvider`/data-router mode)
   — the exploit path is unreachable as shipped — but since it's a real
   prod dep on an EOL-track major, did the actual v8 migration rather than
   accept the risk.

**Fix:**
- Root `package.json`: added `minimatch: "^10.2.5"` and
  `brace-expansion: "^5.0.8"` to the existing `overrides` block, forcing
  the whole dependency tree (electron-builder's toolchain + eslint's
  toolchain) onto patched transitive versions without waiting on an
  electron-builder major bump. Verified with a clean
  `rm -rf node_modules && npm install` that the override actually takes
  (npm doesn't always re-resolve overrides against an existing
  `node_modules`/lockfile in place) — killed 17/19 vulns immediately.
- `apps/desktop/package.json` + `web/package.json`: `react-router-dom`
  `^7.17.0` → `react-router` `^8.3.0`. Rewrote all 41 files across both
  apps that imported from `react-router-dom` to import from
  `react-router` instead (mechanical text swap — every API in use,
  `BrowserRouter`/`HashRouter`/`MemoryRouter`/`Link`/`Navigate`/`Route`/
  `Routes`/`useLocation`/`useNavigate`/`useParams`/`useSearchParams`, is
  exported directly from the `react-router` v8 package root; neither app
  uses `RouterProvider`/`react-router/dom`, so no deeper migration was
  needed). Confirmed prerequisites already satisfied: Node 26 (v8 needs
  ≥22.22), React 19.2.8 (v8 needs ≥19.2.7), Vite 8.1.5 (v8 needs ≥7).

**Verification:** clean reinstall → `npm audit` reports 0 vulnerabilities.
`apps/desktop` and `web` both `tsc --noEmit` clean. `apps/desktop` `eslint`
runs clean against the new minimatch/eslint-plugin-react chain (2
pre-existing unrelated import-order errors, untouched by this change).
Full `apps/desktop` `npm run pack` (build → electron-builder → app
bundle) succeeds end-to-end, `Hermes.app` produced. `web` production
build (`tsc -b && vite build`) succeeds. Desktop vitest suite: A/B tested
via `git stash`/`stash pop` against unmodified `main` — identical 191
passed / 19 failed on both baseline and with this change (the 19 are a
pre-existing `window.localStorage.clear()` jsdom environment issue,
unrelated to routing). All 52 tests across the 5 files that actually
exercise `MemoryRouter`/router hooks (`approval-mode-menu.test.tsx`,
`model-settings.test.tsx`, `toolset-config-panel.test.tsx`,
`messaging/index.test.tsx`, `skills/index.test.tsx`) pass individually.

**Files:** `package.json` (+2 overrides), `apps/desktop/package.json`,
`web/package.json` (react-router-dom → react-router), 41 `.ts`/`.tsx`
files across `apps/desktop/src` and `web/src` (import path only, no
logic changes), `package-lock.json`.

**Merge note:** the `overrides` block already existed pre-fork
(upstream); this only appends two keys, low conflict risk on merge. The
`react-router-dom` → `react-router` import rewrite touches files that
exist upstream too — expect merge conflicts on any upstream PR that also
touches routing imports in these files; resolve by keeping the
`react-router` import path (upstream will eventually need this same v8
migration once react-router-dom's compat shim ages out further).

### Fork-only feature — 2026-07-25 (agent.pin_anthropic_token: opt-in override to make a static Anthropic setup-token win over a refreshable Claude Code credential)

**Motivation:** user wants Hermes pinned to a dedicated long-lived
`claude setup-token` (valid ~1 year) on each machine, independent of the
Anthropic account the user is interactively logged into via `claude` for
normal Claude Code CLI work. On the LXC gateway (headless, no interactive
login) this already worked with zero changes — nothing competed with the
static token. On macOS this did not work, and multiple attempts to force it
via credential-file/Keychain isolation caused real breakage (see the warm
memory note "Claude Code (CLI) multi-account/setup-token isolation
gotchas", fact 1083, for the full incident writeup) before we accepted the
platform constraint and added a proper config option instead.

**Root cause / platform constraint:** `resolve_anthropic_token()` in
`agent/anthropic_adapter.py` has always deliberately preferred a refreshable
Claude Code credential (`~/.claude/.credentials.json` or, on macOS, the
`"Claude Code-credentials"` Keychain entry) over a static persisted
`ANTHROPIC_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN`, via
`_prefer_refreshable_claude_code_token()`. This is intentional and was
previously protected by two tests
(`test_prefers_refreshable_claude_code_credentials_over_static_anthropic_token`,
`test_static_env_oauth_token_does_not_block_refreshable_claude_creds` — see
warm memory fact 266, "parked change... permanently discarded") specifically
so a stale static token can never silently block auto-refresh for a user who
didn't opt into a static token. That default is correct and stays correct.

The actual blocker for this user's use case: on macOS, Claude Code CLI
stores OAuth credentials in Keychain **only** — `CLAUDE_CONFIG_DIR` isolates
config/skills/sessions but does not redirect credential storage the way it
does on Linux/Windows (confirmed against Anthropic's own docs). So a normal
interactive `claude` login and Hermes's setup-token read from the exact same
shared Keychain slot on macOS, and the always-prefer-refreshable default
meant the interactive login always won, no matter what token was persisted
in `.env`. There is no clean OS-level way to keep the two credentials
separate on macOS; every attempt to force separation via Keychain surgery
(deleting/rewriting the shared entry) broke the user's daily-driver login
twice during diagnosis, because non-interactive `security add-generic-password`
writes don't carry the ACL that gives the `claude` binary read access — only
a real interactive `/login` from `claude` itself writes a usable entry.

**Fix:** added an explicit opt-in config key, `agent.pin_anthropic_token`
(default `false`, preserving all existing behavior and both protective
tests unchanged). When set `true`, `resolve_anthropic_token()` skips the
`_prefer_refreshable_claude_code_token()` call for both `ANTHROPIC_TOKEN`
and `CLAUDE_CODE_OAUTH_TOKEN` sources and returns the static token directly,
regardless of what refreshable credential Claude Code holds. This is a
per-machine opt-in via `hermes config set agent.pin_anthropic_token true` —
it does not change resolution order for anyone who hasn't explicitly set it.

**Files touched:**
- `agent/anthropic_adapter.py` — new `_pin_static_anthropic_token()` helper
  (lazy `hermes_cli.config.load_config()` read, same pattern as
  `_system_prompt_mode_compact()`); `resolve_anthropic_token()` now checks
  it before calling `_prefer_refreshable_claude_code_token()` for both env
  sources.
- `hermes_cli/config.py` — `DEFAULT_CONFIG["agent"]["pin_anthropic_token"]
  = False`.
- `tests/agent/test_anthropic_adapter.py` — 2 new tests:
  `test_pin_anthropic_token_config_makes_static_token_win` (flag true →
  static token wins over a live refreshable credential) and
  `test_pin_anthropic_token_false_preserves_default_behavior` (flag
  explicitly false → identical to flag absent, refreshable credential still
  wins).

**Verification:**
1. `scripts/run_tests.sh tests/agent/test_anthropic_adapter.py` — 208/208
   passed, including both pre-existing protective tests unchanged.
2. Live end-to-end on this Mac: with the flag unset, ran
   `resolve_anthropic_token()` directly against the real `~/.hermes/.env`
   and real Keychain — resolved to the personal-login token (SHA256 hash
   compared, not the raw value). Ran `hermes config set
   agent.pin_anthropic_token true`, re-ran the same resolver call — resolved
   to the setup-token instead (hash match confirmed). Flipped back
   confirmed default restored.

**Deployment note:** this flag only solves the "which credential does
Hermes pick" half of the problem. Getting a dedicated setup-token onto a
machine still requires `claude setup-token` run interactively (prints the
token to stdout, does not reliably self-persist — see fact 1083) and saving
it into `.env` via `hermes model` or manual edit. No Keychain surgery is
required or recommended anymore — leave the interactive `claude` login
alone entirely; this flag makes Hermes ignore it instead of needing it gone.

### Fork-only fix — 2026-07-24 (Miku pet voice: leading-edge trim clipped word onsets, made speech unintelligible)

**Symptom (user report, immediately after the previous cadence-fix entry
shipped):** "I couldn't understand what she said" — the cadence fix landed
but broke intelligibility.

**Root cause:** the trailing-silence trim added in the prior entry also
trimmed the LEADING edge, with a hard cut at the exact `-30dB` silencedetect
boundary and (unlike the trailing edge) zero lead-in pad. Verified with
`faster-whisper` word-level timestamps that a line like "on it!" has real
speech starting at `t=0.000`, but the trim cut in at `t=0.168s` — silence-
detect was flagging the word's own quiet initial phoneme as "silence" under
the same threshold that correctly flagged real dead air, and the hard cut
sliced directly into the word onset. Every `"..."`-split segment (introduced
by the same prior fix) got this treatment, and multi-beat lines got it
twice, so it fired far more often than the single-clip pre-cadence-fix
pipeline ever hit a leading-edge risk at all.

**Fix:** `_trim_to_speech` (in `~/.hermes/pets/voices/miku/miku_voice.py`,
machine-local pipeline state, not this repo) no longer touches the leading
edge at all — only the trailing silence is trimmed, which was ~0.8-1.1s (the
actual dead-air problem) vs. the leading gap's ~0.15-0.2s (tiny, and risky
to cut given amplitude-based detection can't distinguish "real silence"
from "a word's quiet onset").

**Verification methodology (can't hear the audio directly, so verified
objectively via `faster-whisper` local transcription instead of guessing):**
1. Regenerated a battery of real pet lines (both default + Miku-flavored
   pools) through OLD (pre-cadence-fix) vs. buggy-NEW (leading+trailing
   trim) vs. fixed-NEW (trailing-only trim) pipelines, transcribed all
   three with `faster-whisper` (`language="en"` forced — auto-detect
   sometimes mis-picked Japanese/Chinese on short exclamations).
2. Found two lines ("thinking…", "on it!") that transcribed as EMPTY or
   near-empty under the buggy-NEW pipeline — a hard signal something was
   badly wrong, not just quality variance.
3. Confirmed via word-timestamps the leading clip was landing inside real
   speech, not silence.
4. After the leading-edge-untouched fix, durations normalized (no more
   near-empty transcripts) and the two previously-empty lines transcribed
   correctly again.
5. Isolated one line that STILL sounds garbled ("here we go~" → "Kyo-yo")
   by running it through the RAW, completely untrimmed, unsplit RVC
   conversion — it garbles identically with zero trim/splice logic
   involved, proving that specific badness is a pre-existing RVC voice-
   conversion artifact unrelated to either fix in this or the prior entry.
   Not fixed here — flagged as a separate, deeper RVC-quality limitation if
   the user wants to chase it further (candidates: index_rate tuning,
   trying the alternate candidate model from the original pitch-tuning
   A/B, or accepting some invented/Miku-fandom terms just don't convert
   cleanly through this checkpoint).

**Files:** `~/.hermes/pets/voices/miku/miku_voice.py` only — no repo files
changed, hence no commit for this entry (see the mandatory-workflow note
above: FORK.md still records it here since this pipeline's tuning history is
explicitly cross-referenced from the script's own comments).

### Fork-only fix — 2026-07-24 (desktop: Miku pet voice — sluggish/draggy cadence + always-instant speech)

**Symptom (user report):** the Miku pet voice felt "slow" — both overall
response time before she started speaking, and the cadence/rhythm of the
speech itself felt "kinda both" rushed and draggy at once.

**Two independent root causes found and fixed, one in each half of the
pipeline:**

**1. Cadence (audio itself) — fixed in `~/.hermes/pets/voices/miku/miku_voice.py`,
machine-local pipeline state, not this repo.** Measured directly: edge-tts
bakes in ~0.8-1.1s of fixed leading/trailing silence per clip (on a 2-word
line like "all set!" that's over half the total clip), and renders a literal
`"..."` (used in lines like "Yay!... done!") as an internal pause of ~0.82s —
almost as long as the words on either side of it. Confirmed on the RAW
pre-RVC edge-tts output (not an RVC-introduced artifact) via
`ffmpeg silencedetect`. Fix: `_synthesize_source_audio` now splits a line on
`"..."`/`"…"` into separate edge-tts calls (fired concurrently via
`asyncio.gather`, not sequentially, so a 2-beat line only pays one network
round trip), trims each segment's fixed leading/trailing silence to its real
speech boundary via `ffmpeg atrim` (guided by `silencedetect`, with a small
tail pad so a trailing consonant isn't clipped, and a safe copy-through
fallback on any detection failure or degenerate all-silence clip), then
re-splices with a short controlled 0.18s gap (`_splice_with_gaps`, an ffmpeg
`apad`+`concat` filter graph) instead of trusting edge-tts's own pause/tail.
Measured result: "Yay!... done!" duration dropped from 2.28s → 0.86s (62%
reduction), gap shrank from 0.82s → ~0.25s, and a plain no-ellipsis line's
duration also dropped (removing the fixed dead air alone helps every line,
not just multi-beat ones).

**2. Perceived latency (time-to-first-sound) — fixed in
`apps/desktop/src/components/pet/pet-bubble.tsx` (this repo).** The optional
LLM-flavored dialogue feature (`auxiliary.pet_dialogue.enabled`) was
SEQUENTIAL: `speakAnnouncedBeat` awaited `fetchPetDialogue()` (a real Haiku
round-trip, measured ~2.3s live against the actual auxiliary client) before
speech started at all — so on top of the ~3-5s TTS pipeline itself (worse
when the RVC daemon had idled out), the user was also waiting out an LLM
call before anything played. Confirmed the model was already the cheap
choice (`claude-haiku-4-5` via `auxiliary.anthropic.default`, no
`pet_dialogue`-specific override in the user's config) — the ~2.3s is
Haiku's real latency, not a wrong-model bug.

User explicitly chose (after ruling out simpler options — disable
pet_dialogue entirely, or accept the wait) an architecturally faster shape:
speech must be instant, EVERY time, with the LLM-generated line still used
whenever available — explicitly accepting that a "completed" beat's spoken
line may occasionally describe the PREVIOUS finished task rather than the
current one on rapid back-to-back completions (the "waiting" beat carries no
task-specific context, so it has no such risk).

**Design (validated via `consult` against claude-fable-5 before building —
confirmed the stale-while-revalidate/double-buffering shape was correct, and
surfaced one real bug in the first draft):** a per-beat (`completed` |
`waiting`) single-slot cache. `speakAnnouncedBeat` now speaks WHATEVER'S
CACHED right now (falling back to the static pool if nothing's cached yet)
before doing anything else, then fires `fetchPetDialogue()` in the
background — its result is stored into the cache for the NEXT occurrence of
that beat type, never spoken for the current call. Fable caught a genuine
correctness gap in the first draft: network resolution order doesn't match
launch order, so on rapid beats an OLDER, slower in-flight fetch could
resolve AFTER a newer one and silently clobber the cache with a stale
result, breaking the intended "one beat behind" bound into "unboundedly
stale." Fixed with a monotonic per-beat sequence number
(`beatLineSeq`/`beatLineCommittedSeq`): each fetch captures the sequence in
effect at launch and only commits to the cache if no fetch with an
equal-or-higher sequence has already committed (compare-and-swap by
sequence, not last-write-wins).

**Verification:** desktop `tsc --noEmit` clean; `eslint` clean; 5 new tests
in `pet-bubble.test.ts` covering instant-speak-with-empty-cache,
speak-prior-beat's-cached-line-not-current-in-flight-one,
never-speak-the-LLM-result-for-the-SAME-call, the out-of-order-resolution
regression Fable's review caught (an older slow fetch resolving after a
newer fast one must not overwrite the fresher cached line), and
cache-untouched-on-rejection — all 5 pass; full desktop suite unaffected —
210 files / 1758 tests passing. Cadence fix verified directly against the
live pipeline (not just unit-level): restarted the daemon to pick up the
new module, ran cold + warm calls through the real `speak.sh` entry point,
confirmed consistent 0.86s output duration across repeated warm calls (was
2.28s, and varied run-to-run before the fix) and clean daemon logs
throughout.

**Files:** `apps/desktop/src/components/pet/pet-bubble.tsx` (per-beat cache +
sequence-guarded background refresh in `speakAnnouncedBeat`, new
`_resetBeatLineCacheForTests` test hook), `apps/desktop/src/components/pet/
pet-bubble.test.ts` (new, 5 tests). `~/.hermes/pets/voices/miku/miku_voice.py`
(cadence fix — machine-local pipeline state per the
`custom-tts-voice-pipelines` skill's guidance, not tracked in this repo).

**Merge note:** fork-only desktop app, no upstream equivalent — no conflict
risk.

### Fork-only fix — 2026-07-24 (desktop: project-terminal follow duplicated tabs on repeated switches)

**Symptom (user report):** with the "terminal pane follows the active
sidebar project" feature (same-day entry below), switching `exo` →
`hermes-agent` → `exo` left 3 terminal tabs instead of the expected 2 — a
duplicate tab spawned every time you returned to a project.

**Root cause:** two gaps in `ensureProjectTerminal`'s matching, both stemming
from the same blind spot — it only ever recognized a tab as "this project's"
by an exact `projectId` stamp:
1. The pane's very first tab (created by `ensureTerminal()` the first time
   the user ever opened the terminal pane, before entering any project) is
   plain and unbound — no `projectId`. Re-entering the project that tab
   happens to sit in couldn't recognize it as already covering that project,
   so it spawned a second, redundant bound tab right next to it.
2. `ensureTerminal()` itself had no project awareness at all, so it always
   produced that first unbound tab even when a project was already the
   active sidebar scope at pane-open time.

**Fix:**
- `ensureProjectTerminal` now has a 3-rung lookup: (1) the remembered/bound
  tab for this `projectId`, (2) failing that, *adopt* an existing unbound
  user tab whose `cwd` (or live `restoreCwd`, so a shell that's since `cd`'d
  into the project also counts) sits at-or-under the project root — stamping
  it with `projectId` in place rather than creating a new one, (3) only then
  create a fresh tab. Path containment reuses the same `underPath` semantics
  as `store/projects.ts`'s `projectIdForCwd` (duplicated locally — importing
  from `projects.ts` here would cycle, since `projects.ts` already imports
  `ensureProjectTerminal`).
- `ensureTerminal()` now takes an optional `{ id, cwd }` for the
  currently-scoped project; when passed, the pane's first-ever tab is created
  already bound via `ensureProjectTerminal` instead of blind, closing the gap
  at the source instead of only patching it on the next switch.
  `PersistentTerminal` (the pane-mount effect) now reads `$projectScope` +
  `projectRootCwd` and passes it through.

**Verification:** desktop `tsc --noEmit` clean; `eslint` clean (two
auto-fixed import-order/blank-line nits); 7 new tests covering exact-tab
reuse, blind-tab adoption (including via `restoreCwd`), non-adoption of
tabs outside the project root, tab count staying flat across repeated
switches, and `ensureTerminal`'s new project-bound-first-tab path — full
`terminals.test.ts` + `projects.test.ts` pass (39/39 in the touched files);
full desktop suite unaffected — 209 files / 1753 tests passing (Node 26
`window.localStorage`/jsdom collision worked around with
`NODE_OPTIONS=--no-experimental-webstorage`, same pre-existing issue noted in
the original feature entry, reproduces on unmodified `main` too).

**Files:** `apps/desktop/src/app/right-sidebar/terminal/terminals.ts`
(`ensureProjectTerminal` adopt-rung + `underPath`, `ensureTerminal(project?)`),
`apps/desktop/src/app/right-sidebar/terminal/persistent.tsx` (passes the
active project scope into `ensureTerminal`), `apps/desktop/src/app/
right-sidebar/terminal/terminals.test.ts` (7 new tests).

**Merge note:** fork-only desktop app, no upstream equivalent — no conflict
risk.

### Fork-only feature — 2026-07-24 (desktop: terminal pane follows the active sidebar project)

**Request:** user wants the desktop's embedded terminal pane to switch
alongside the sidebar's project scope — switching from e.g. `hermes-agent` to
`exo` should switch the terminal to a shell already sitting at that project's
root, not leave the pane on whatever cwd it happened to be in.

**Design choice (per-project tab, confirmed with user over the 3 alternatives
— reuse-one-tab / per-project-tab / new-tabs-only):** each project keeps its
own terminal tab. Switching into a project reuses its existing tab (or spawns
one at the project root if it doesn't have one yet) instead of `cd`-ing a
single shared shell, so a live process in one project's terminal is never
disturbed by switching to another project and back.

**Why not just repurpose the existing cwd-snapshot-only terminal model:**
`terminals.ts` documents on purpose that terminal tabs live outside
session/project state (`cwd` is captured once at creation, switching
*sessions* never moves a terminal) — that's deliberate insulation so an
in-flight shell command survives session switches. Project scope is a
different, coarser axis than session, so this adds a *new*, opt-in binding
(`TerminalEntry.projectId`) rather than touching that existing invariant.

**Implementation:**
- `TerminalEntry` gains an optional `projectId`, persisted alongside the
  existing fields (`sanitizePersistedTerminal` / `persistTerminals`).
- New `ensureProjectTerminal(projectId, cwd)` in `terminals.ts`: reuses the
  project's last-active tab if one exists (tracked via a runtime-only
  `lastActiveTerminalByProject` map, updated on every `$activeTerminalId`
  change), else creates a fresh tab pinned to that project via
  `createTerminal(cwd, projectId)`. Store-only — the PTY spawns lazily when
  the pane mounts (`PersistentTerminal`'s existing latch), so calling this
  while the pane is closed is free.
- `projects.ts`'s `enterProject(id)` — the single call site the sidebar uses
  when the user clicks into a project — now also calls
  `ensureProjectTerminal(id, projectRootCwd(id))`, gated on
  `$terminalTakeover` (only follows if the user has opened the terminal pane
  at least once, so entering a project never silently spawns a PTY nobody
  asked to see). `projectRootCwd(id)` is `resolveNewSessionCwd`'s existing
  root-path lookup, extracted to a standalone exported helper so both call
  sites share one definition of "that project's root."

**Verification:** desktop `tsc --noEmit` clean; `eslint` clean (one
auto-fixed blank-line warning); 4 new `ensureProjectTerminal` tests
(create-and-focus, dedupe-on-repeat, per-project isolation/switching,
resume-last-active-tab-over-extra-tabs) plus the existing 7
`terminals.test.ts` + all `projects.test.ts` tests pass (28/28 in the touched
files); full desktop suite unaffected — 209 files / 1746 tests passing (run
with `NODE_OPTIONS=--no-experimental-webstorage` to route around a pre-
existing, unrelated Node 26 jsdom `window.localStorage` collision that also
reproduces on unmodified `main`).

**Files:** `apps/desktop/src/app/right-sidebar/terminal/terminals.ts` (new
`projectId` field + `ensureProjectTerminal`), `apps/desktop/src/store/
projects.ts` (`projectRootCwd` extraction + `enterProject` hook),
`apps/desktop/src/app/right-sidebar/terminal/terminals.test.ts` (4 new
tests).

**Merge note:** fork-only desktop app, no upstream equivalent — no conflict
risk.

### Fork-only feature — 2026-07-24 (desktop: pet voice via Miku RVC voice-conversion pipeline)

**Request:** user has the Hatsune Miku petdex mascot active in Hermes Desktop
and asked to wire its status-bubble lines (PetBubble's "working…"/"thinking…"
text) up to actual Miku-voiced audio, based on her real voicebank rather than
a from-scratch TTS voice.

**Approach — command-type TTS provider, zero core-tool footprint:**
Hermes already supports `tts.providers.<name>: type: command`, a shell-
template mechanism (`tools/tts_tool.py::_generate_command_tts`) for wiring any
local CLI into the TTS dispatch chain. Built the whole pipeline as one such
provider instead of adding a new core tool or touching the model-facing
`text_to_speech` tool schema:

```
edge-tts (free "Ana" voice, synthesizes raw text)
  -> ffmpeg (resample to 44.1kHz mono WAV)
  -> mlx-rvc (RVC voice-conversion, Apple Silicon MLX, no CUDA/GPU needed)
       using a community Hatsune Miku RVC model trained directly on official
       VOCALOID V4X demo-track samples (not fan covers)
  -> ffmpeg (encode to caller's requested format: mp3/ogg/wav)
```

Wrapper script + Python driver live outside the repo, at
`~/.hermes/pets/voices/miku/` (`speak.sh`, `rvc_convert_fixed.py`) — these are
user machine-local assets (a downloaded third-party model file + a small glue
script), not something that belongs in the Hermes tree. `tts.providers.miku`
in `config.yaml` points at `speak.sh`.

**Real upstream bug found and worked around, not patched in-place:**
`mlx-rvc` 0.1.0 (`pip install git+https://github.com/lextoumbourou/mlx-rvc`)
has a genuine bug in `RVCPipeline.convert()`: ContentVec emits phone features
at 50fps but F0 extraction runs at 100fps, and upstream RVC's reference
implementation repeats each phone frame twice (`F.interpolate(...,
scale_factor=2)`) before synthesis so the two timelines match. mlx-rvc's
`pipeline.py` skips that step and just truncates `min(len(phone), len(f0))` —
since `len(f0) ≈ 2×len(phone)`, this silently drops ~half of every phone
frame's information and outputs audio at half the correct duration (heard as
2x-speed garbled speech; confirmed via `ffprobe` duration comparison: 5.57s
input -> 2.78s broken output -> 5.56s fixed output). Fix: a standalone
`rvc_convert_fixed.py` that duplicates `RVCPipeline.convert()` but inserts
`np.repeat(phone, 2, axis=1)` before the F0 alignment step — NOT a monkeypatch
of the installed pip package, so a future `mlx-rvc` reinstall/upgrade can't
silently reintroduce or double-apply the fix. `speak.sh` calls this script
instead of the `mlx-rvc convert` CLI directly, with an explicit comment
pointing back at this root cause.

**Pitch tuned by objective measurement, not vibes:** pulled an official
Miku V4X reference sample (Wikimedia Commons, freely licensed, "Kimigayo"
EVEC demo) and used `librosa.pyin` to measure its median F0 (392Hz / G4).
Measured our pipeline's output at several pitch-shift settings and models,
then had the user A/B the two candidates that measured closest to the
reference. Settled on the vocaloid-sample-trained model (`aple/HatsuneMikuRVC`
family member trained on 36min of audio sampled directly from VOCALOID demo
tracks, not fan covers) at +2 semitones (measured median ~364Hz) over an
alternative "on par with original voicebank" model at +4 (~405Hz) — the lower
measured-closer candidate lost the user's ear test, confirming pitch alone
doesn't fully capture timbre fit.

**Core-repo changes (all additive, no existing behavior changed):**
- `tools/tts_tool.py`: `text_to_speech_tool()` gained an internal-only
  `provider_override` kwarg that bypasses `tts.provider` for one call. NOT
  exposed on the model-facing tool schema (the `registry.register()` call
  site for the agent tool is untouched) — only the desktop's own REST
  endpoint uses it, so the main "read replies aloud" TTS behavior for
  everyone else is completely unaffected.
- `hermes_cli/web_server.py`: `/api/audio/speak`'s `TTSSpeakRequest` gained an
  optional `provider` field, threaded into `text_to_speech_tool(...,
  provider_override=payload.provider)`.
- `hermes_cli/config.py`: `display.pet.voice_enabled` (bool, default False —
  opt-in like `voice.auto_tts`) and `display.pet.voice_provider` (string,
  default "") added to `DEFAULT_CONFIG`. No `_config_version` bump — new keys
  under an existing section, handled by the deep-merge.
- Desktop (`apps/desktop/src/`): new `store/pet-voice.ts` (mirrors
  `voice-prefs.ts`'s pattern exactly — atoms seeded from config, optimistic
  read-modify-write persistence). `PetBubble` speaks its line via the
  existing `playSpeechText()`/`voice-playback.ts` pipeline once per mood
  TRANSITION (not on every 2.6s line-rotation tick) when the toggle is on.
  `voice-playback.ts`/`hermes.ts`'s `speakText()` gained an optional
  `provider` passthrough. `VoicePlaybackSource` gained a `'pet'` variant.
  New Settings toggle in `pet-settings.tsx` alongside Roam/Zone, i18n strings
  added to en/zh/zh-hant/ja + `i18n/types.ts`.

**Verification:** `text_to_speech_tool(text, provider_override='miku')`
exercised end-to-end against the REAL `~/.hermes/config.yaml` (not a mock),
confirmed correct command-provider resolution + audio output. Targeted
pytest (`test_tts_command_providers.py` 52/52, `test_tts_registry.py`,
`test_tts_media_routing.py`, `test_tts_picker.py`,
`test_plugins_tts_registration.py` — 64 more, all green). Desktop `tsc
--noEmit` clean (both configs), `eslint` clean on every touched file.

**Files:** `tools/tts_tool.py`, `hermes_cli/web_server.py`,
`hermes_cli/config.py`, `apps/desktop/src/store/pet-voice.ts` (new),
`apps/desktop/src/components/pet/pet-bubble.tsx`,
`apps/desktop/src/app/settings/pet-settings.tsx`,
`apps/desktop/src/lib/voice-playback.ts`,
`apps/desktop/src/store/voice-playback.ts`, `apps/desktop/src/hermes.ts`,
`apps/desktop/src/types/hermes.ts`,
`apps/desktop/src/app/session/hooks/use-hermes-config.ts`,
`apps/desktop/src/i18n/{en,zh,zh-hant,ja,types}.ts`.

**Not in the repo (user machine-local, by design):** `~/.hermes/pets/voices/
miku/speak.sh` + `rvc_convert_fixed.py` (the actual voice pipeline glue —
references a locally-downloaded third-party RVC model file, so it can't be
portable repo content), `~/.hermes/pets/voices/miku_vocaloid/MikuAI.pth` +
its FAISS index (the downloaded community RVC model itself, per its OpenRAIL-
style license — user attribution: credit to the model's uploader per its
source page). `tts.providers.miku` + `display.pet.voice_*` values are
user-specific `config.yaml` entries, not repo defaults.

**Merge note:** additive changes to existing files, no upstream conflict
expected. The new `store/pet-voice.ts` file has no upstream equivalent.

### Fork-only refinement — 2026-07-24 (desktop: narrow pet voice to "needs you" + "turn done", not running chatter)

**Follow-up to the same-day pet-voice entry above.** User clarified after
trying it: don't narrate everything the pet's status bubble shows — only
speak up when the agent needs the user, or when a prompt finishes running.

**Problem with the first cut:** `PetBubble`'s original voice wiring spoke
*every* bubble line on every mood transition, including the continuous
`run`/`review` rotation ("working…", "thinking…", "crunching…", etc.) — so a
long tool-heavy turn meant constant narration, not the two specific beats the
user actually wanted.

**Real bug caught before it shipped:** my first fix attempt keyed the
"turn finished" announcement off `state === 'jump'` (the sprite's
celebrate/jump pose). That pose is NOT unique to a finished turn — clicking/
petting the mascot (`vibe-hearts.tsx`'s `burstVibeHearts` → `flashPetActivity
({ celebrate: true })`) fires the exact same pose. Shipping that as-is would
have announced "all done!" every time the user pets their own mascot, not
just on real completions. Also found `justCompleted`/`'wave'` is dead code —
grepped for `justCompleted: true` across the desktop tree and nothing sets it
outside tests, so a `state === 'wave'` branch would never fire.

**Fix:**
- Added a dedicated `$petTurnCompletedBeat` nonce in `store/pet.ts`, bumped
  ONLY by the gateway's real turn-completion handler
  (`use-message-stream/gateway-event.ts`, right next to the existing
  `flashPetActivity({ celebrate: true, ... })` call) — never by
  `burstVibeHearts`/manual petting, which still drives the shared
  celebrate/jump sprite pose but no longer implies "spoken as done."
- `PetBubble` now has two independent, narrowly-scoped voice effects instead
  of one broad one: (1) speaks a "needs you" line once per transition INTO
  `specKey === 'waiting'` (a real approval/clarify/sudo prompt or plain
  end-of-turn idle blocking on the user); (2) speaks a "turn finished" line
  keyed off `$petTurnCompletedBeat` changing, skipping the initial mount value
  so opening the app never announces a stale completion from a prior turn.
  `run`/`review` bubble text rotation is now 100% silent — the visual bubble
  is unchanged, only the voice gating changed.
- Config default description (`hermes_cli/config.py`) and the Settings
  toggle copy (`pet-settings.tsx` via en/zh/zh-hant/ja i18n) updated to state
  the narrower behavior so the toggle's own description isn't misleading.

**Verification:** desktop `tsc --noEmit` clean (both configs), `eslint`
clean on every touched file, `pet.test.ts` 13/13 unaffected,
`gateway-events.test.ts` 6/6 unaffected. No new automated test added for the
voice-announcement gating itself (it's a thin React-effect wrapper around
already-tested state; the meaningful logic — dead-code elimination of
`justCompleted`/`wave`, and the celebrate-vs-completion disambiguation — was
verified by grep + code reading, not by new assertions).

**Files:** `apps/desktop/src/components/pet/pet-bubble.tsx`,
`apps/desktop/src/store/pet.ts` (new `$petTurnCompletedBeat` +
`triggerPetTurnCompleted`), `apps/desktop/src/app/session/hooks/
use-message-stream/gateway-event.ts` (fires the new trigger alongside the
existing celebrate flash), `hermes_cli/config.py`,
`apps/desktop/src/i18n/{en,zh,zh-hant,ja}.ts`.

**Merge note:** additive changes to existing files, no upstream conflict
expected.

### Fork-only feature — 2026-07-24 (desktop: Vocaloid-themed pet phrasing for the Miku bubble + voice)

**Follow-up to the two pet-voice entries above.** User asked to make both the
status-bubble TEXT and the spoken voice lines sound more "Miku-like" instead
of generic status words ("working…", "thinking…").

**Approach:** `PetBubble` previously had one hardcoded `SPECS` line set (plus
`COMPLETION_LINES` for the spoken-only "turn done" beat) shared by every pet.
Split into a default set (`DEFAULT_SPECS`/`DEFAULT_COMPLETION_LINES` —
unchanged generic phrasing, used by any pet) and a Vocaloid-flavored set
(`MIKU_SPECS`/`MIKU_COMPLETION_LINES`), selected by the ACTIVE pet's slug
(`$petInfo.slug`, matched case-insensitively against `hatsune-miku`/`miku`/
`hatsunemiku`) via new `specsForSlug()`/`completionLinesForSlug()` helpers.
Both the bubble text (`run`/`review`/`failed`/`waiting` states) and the
spoken lines read from the same flavored lookup, so picking the Miku pet
changes both surfaces from one selection — no separate "voice phrasing"
config needed.

Miku phrasing draws on real Vocaloid/fan-culture terms rather than invented
character traits: "producer" (プロデューサー / "P") is the actual term her
community uses for whoever's directing her, and "39"/"san-kyuu" is a
long-established "thank you" pun (most visibly in her "Miku's Day", 3/9). Any
other installed pet keeps the exact original generic phrasing — this is
additive, not a replacement of the default line sets.

**Verification:** desktop `tsc --noEmit` clean, `eslint` clean,
`pet.test.ts` 13/13 + `gateway-events.test.ts` 6/6 unaffected (this feature
touches only local component state/lookups, no store contract changed).

**Files:** `apps/desktop/src/components/pet/pet-bubble.tsx` (full rewrite of
the SPECS/COMPLETION_LINES section into slug-keyed default + Miku sets, plus
reading `$petInfo.slug`).

**Merge note:** additive change to an existing file, no upstream conflict
expected.

### Fork-only refinement — 2026-07-24 (desktop: Miku phrasing pass 2 — idol/performer voice, not studio-engineer)

**Follow-up to the Vocaloid-phrasing entry above.** User said the phrasing
"doesn't quite feel right" without specifying what. Rather than guess-and-
check against the user, ran the line sets past a second model (Claude Fable,
via `consult`) for a persona/authenticity read.

**Diagnosis (from consult):** the first pass framed Miku as a studio
engineer narrating her own signal chain ("recording…", "mixing it in…",
"vocal sync in progress…") rather than a performer/idol — the actual
persona. It also stapled "producer" onto RUN-state lines where she's
narrating her own action, which doesn't logically track: direct address
("producer") only makes sense where she's actually speaking TO the user
(WAITING/completion), not describing what she herself is doing. The
consistent trailing "…" on every line (including active-work RUN) read as
hesitant rather than her actual upbeat/confident energy. Two lines were
flagged as already working and used as the calibration bar for the rewrite:
"san-kyuu for waiting!" (genuine fandom pun, kept as completion anchor) and
"ah, glitchy" (self-aware nod to being software, on-brand, kept in FAILED).

**Fix:** rewrote `MIKU_SPECS.run` and `.review` to drop "producer" entirely
(reserved for `waiting`/completion, where she's genuinely addressing the
user) and reframe as an idol prepping/mid-performance rather than an
engineer at a mixing desk ("here we go~", "let's roll!", "cueing up…",
"warming up…" replacing "recording…"/"mixing it in…"/"vocal sync in
progress…"). Trimmed FAILED's "missed a note"/"that flopped" (generic) in
favor of "system hiccup"/"oof, rewind" (keeps the self-aware software wink).
Reordered `MIKU_COMPLETION_LINES` to lead with "san-kyuu for waiting!" and
added "yay, done!" for more idol-cheer energy. WAITING kept unchanged — it
was already correct (genuine direct address to "producer").

**Verification:** desktop `tsc --noEmit` clean, `eslint` clean, `pet.test.ts`
13/13 + `gateway-events.test.ts` 6/6 unaffected (data-only change, no logic
touched).

**Files:** `apps/desktop/src/components/pet/pet-bubble.tsx` (`MIKU_SPECS`
run/review/failed line arrays, `MIKU_COMPLETION_LINES` reorder + addition,
updated doc comment explaining the producer-address restriction).

**Merge note:** additive/data-only change to an existing file, no upstream
conflict expected.

### Fork-only feature — 2026-07-24 (miku voice pipeline: warm daemon cuts latency 8x)

**Reported:** the Miku voice pipeline "worked" but "took longer than I
thought it would and was a bit short/hard to understand." Investigated:
two genuinely separate problems bundled into one complaint.

**Latency root cause:** `speak.sh` spawned a fresh Python process per pet
voice line, which cold-loaded the RVC model (~0.3s), ContentVec (~1s), and
lazy-loaded RMVPE (~1s) from scratch EVERY call — ~2.4s of pure model-load
overhead before any conversion even started, on top of ~0.5-0.7s of Python
startup + edge-tts network round-trip. Measured breakdown confirmed model
loading, not the actual conversion (~0.29s once warm), dominated the cold
path 8-to-1.

**Fix:** built a warm background daemon
(`~/.hermes/pets/voices/miku/daemon.py`) that loads the RVC pipeline once
and keeps it resident, talking newline-delimited JSON over a Unix socket.
`speak_client.py` tries the socket first (~0.3-0.8s once warm, dominated by
the edge-tts network call, not RVC); on any failure (daemon never started,
idled out) it falls back to synthesizing that ONE request cold in-process
(same latency as before this daemon existed — never a regression) and
spawns the daemon detached for next time. The daemon self-exits after 5
minutes of no requests (`MIKU_VOICE_IDLE_TIMEOUT`, default 300s) so it
doesn't sit at ~1.9-2.2GB RSS indefinitely on a machine not actively using
the feature — verified the watchdog actually fires and cleans up its
socket/lock files via a real 5s-timeout run, not just code review.
`speak.sh` now just delegates to `speak_client.py`; the old inline
`rvc_convert_fixed.py` one-shot script was split into `miku_voice.py`
(shared synth logic, importable by both the daemon and the cold-fallback
path) so there's exactly one copy of the RVC-bug workaround.

**Expressiveness, separately:** "yay, done!" sounded flat because RVC only
carries through whatever expression the SOURCE Edge-TTS recording had, and
Edge-TTS's baseline reading of a short exclamation is flat by default. A/B
iterated with the user through several rounds (edge-tts `rate`/`pitch`
prosody knobs at three intensity levels, then punctuation/pacing variants
to land a "YAY!" *(pause)* "done" rhythm) and landed on `+20%` rate /
`+30Hz` pitch (source-TTS prosody, distinct from the existing `+2` semitone
RVC pitch shift) plus rewriting the Miku completion line to `"Yay!...
done!"` so the punctuation itself carries the pause. Locked in as
`EDGE_RATE`/`EDGE_PITCH` constants in `miku_voice.py`.

**Verification:** confirmed cold-path latency parity (no regression) and
warm-path speedup with real `time`/timestamped runs, not estimates — cold
~3.2-3.9s (matches pre-daemon baseline), warm ~0.77-1.6s (includes ~0.3-1s
of test-harness Python subprocess overhead the real desktop call site
doesn't pay). Verified through the actual `text_to_speech_tool(...,
provider_override='miku')` path against the real `~/.hermes/config.yaml`,
not a mock. Idle-timeout self-shutdown verified via a live 5s-timeout run
(watched the daemon actually exit + clean up its socket/lock without a
manual kill).

**Files (all outside the repo, user machine-local — see the original
pet-voice entry's rationale):** `~/.hermes/pets/voices/miku/miku_voice.py`
(new — shared synth logic), `daemon.py` (new), `speak_client.py` (new),
`speak.sh` (rewritten to delegate), `rvc_convert_fixed.py` (removed,
folded into `miku_voice.py`).

**Merge note:** no repo files touched — this entry documents machine-local
tooling for completeness, matching the original pet-voice entry's pattern.

### Fork-only feature — 2026-07-24 (desktop: LLM-generated pet dialogue, opt-in, two beats only)

**Follow-up idea from the user** while discussing the voice feature: "maybe
we should have some of the text or responses here come from a cheap LLM?"

**Scoping decision:** the continuous run/review bubble rotation (fires every
2.6s while a turn is in flight) stays 100% local/static — an LLM call on
that cadence would be real, avoidable cost + latency for decorative UI, and
AGENTS.md's cost-consciousness applies. LLM generation is wired ONLY into
the two ANNOUNCED beats (turn finished, needs user), which already fire at
most once per turn — the same scope discipline as the earlier voice-gating
refinement.

**Approach — new `auxiliary.pet_dialogue` task, following the existing
pattern used by `approval`/`title_generation`/`profile_describer`/etc.:**
`hermes_cli/config.py` gained `auxiliary.pet_dialogue` (off by default,
`enabled: false`; `timeout: 8`, `reasoning_effort: "none"` — this is a
one-liner, not a reasoning task; `max_context_chars: 400` caps the
"what just happened" context server-side). `hermes_cli/web_server.py` gained
`POST /api/pet/dialogue`: reads the task's `enabled` flag (clean 404 when
off — the desktop's failure handling treats this identically to a
timeout/network error), builds a persona + beat-specific prompt (Miku
persona reserves "producer" address for genuine direct-address beats, same
restriction as the earlier phrasing-pass entry), and calls
`agent.auxiliary_client.call_llm(task="pet_dialogue", ...)` — the same
central resolution chain (auto → main provider → OpenRouter → Nous Portal →
...) every other auxiliary task uses, so it picks up the user's main
provider/model with zero extra config when `provider: auto`.

**Desktop wiring:** `apps/desktop/src/hermes.ts` gained `fetchPetDialogue()`
(6s client timeout — decorative UI, not worth a long hang). `pet-bubble.tsx`
gained a shared `speakAnnouncedBeat()` helper: tries the LLM line first,
falls back to a random pick from the existing static pool on ANY rejection
(disabled/404, timeout, network error, empty response) — the static pool
from the earlier phrasing-pass entries is now a fallback, not deleted.
`store/pet.ts`'s `$petTurnCompletedBeat` changed shape from a bare
`atom<number>` nonce to `{ seq, context }`, threading the assistant's final
reply text (already computed at the gateway-event completion call site for
the message store — no new extraction needed) through as "what just
happened" context for the completion beat. The `waiting` beat intentionally
passes empty context — the line is about the STATE (it's your turn), not
which specific prompt is blocking, and extracting prompt text from three
different prompt-store shapes (clarify/approval/sudo/secret) wasn't worth
the complexity for that beat.

**Verification:** exercised `POST /api/pet/dialogue` through the REAL
FastAPI `TestClient` (not a mock) against the actual auth middleware and
the actual `call_llm` resolution chain — confirmed both beats produce
genuinely context-aware, in-character lines (e.g. "Login bug squashed,
producer~ all green!" for a login-fix context), confirmed the disabled path
returns a clean 404, confirmed re-enabling round-trips correctly. Desktop
`tsc --noEmit` clean (both configs), `eslint` clean on every touched file,
`pet.test.ts` 13/13 + `gateway-events.test.ts` 6/6 unaffected. Ran the
broader desktop suite and confirmed a batch of unrelated failures
(composer/panes/onboarding/projects/sidebar, all `localStorage.clear`
errors) are pre-existing on a clean `main` with zero changes applied —
verified via `git stash` + re-run, not assumed.

**Files:** `hermes_cli/config.py` (new `auxiliary.pet_dialogue` task),
`hermes_cli/web_server.py` (new `POST /api/pet/dialogue` +
`PetDialogueRequest`), `apps/desktop/src/hermes.ts` (new
`fetchPetDialogue()` + `PetDialogueResponse`),
`apps/desktop/src/store/pet.ts` (`$petTurnCompletedBeat` shape change +
`context` param on `triggerPetTurnCompleted`),
`apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts`
(threads `finalText` through), `apps/desktop/src/components/pet/pet-bubble.tsx`
(new `speakAnnouncedBeat()` helper, both voice effects rewired to use it).

**Merge note:** additive changes to existing files (new config task, new
endpoint, new client function) plus one shape change to an in-tree-only
atom (`$petTurnCompletedBeat`) with exactly one producer and one consumer,
both updated in the same commit. No upstream conflict expected.

### Fork-only fix — 2026-07-24 (pet_dialogue prompt taught nothing about spoken delivery)

**Reported:** user asked "This LLM integration knows to output text for
cadence and expressiveness and such yeah?" — it did not. The prompt from
the entry above only told the model length (2-6 words) and tone
(cheerful/direct); it had zero knowledge that this line gets spoken through
TTS -> RVC, or of the punctuation-for-pacing trick ("Yay!... done!" — the
ellipsis IS the pause) hand-tuned into the static fallback pool a few
entries up. A generated line could easily come out as a flat, comma-laden
sentence that reads fine as text but sounds monotone once synthesized —
exactly the gap the earlier voice-tuning work was trying to close.

**Fix:** added an explicit `delivery_rules` block to the system prompt in
`POST /api/pet/dialogue` (`hermes_cli/web_server.py`): use "!" for a punchy
beat and "..." for a short pause between two beats, mirroring the shape of
"Yay!... done!" directly (told the model to copy that exact example's
shape); prefer short/simple/high-energy words (TTS renders those more
clearly than long ones); avoid commas/semicolons/subordinate clauses, which
read flat once spoken.

**Verification:** re-ran `POST /api/pet/dialogue` through the real FastAPI
`TestClient` against the actual `call_llm` chain for three cases (two
completion contexts, one waiting context) — confirmed the burst/pause
punctuation pattern now shows up consistently rather than only in the
one-shot example the prompt happened to mention. Synthesized all three
through the real Miku voice pipeline (`text_to_speech_tool(...,
provider_override='miku')`) and had the user confirm by ear that the
delivery now matches the hand-tuned static lines' energy, not a flatter
LLM default.

**Files:** `hermes_cli/web_server.py` (`delivery_rules` addition to the
`pet_dialogue` system prompt).

**Merge note:** small additive change to an existing endpoint, no upstream
conflict expected.

### Fork-only fix — 2026-07-24 (desktop: tab drag lit up the layout-edit dashed zone overlay)

**Reported:** after fixing the tab-reorder no-op (see the entry above), the
user confirmed reordering works, but asked to stop the "dotted UI that
indicates entire panes can be moved around" from appearing while dragging a
session tab — they only want that surface reachable via the titlebar's
layout-editor button (⌘⇧\\ / ⌘K stay fine as-is).

**Root cause:** a session tab's own drag (`session-tile.tsx`'s `tabDrag` +
`controller.tsx`'s `workspaceTabDrag`, both routed through the shared
`startSessionDrag` resolver in `app/chat/session-drag.ts`) always ran the
SAME resolver as a sidebar-row drag — full stack/split/composer-link
targeting. `onEngage` unconditionally set `$treeDragging.set(SESSION_TILE_DRAG)`,
which is exactly the sentinel `ZoneDropOverlay` (`tree-group.tsx`) keys off
to light the dashed drop-target sheet over every zone. So dragging a TAB
even a few pixels — not just past its strip — always entered the full
zone-targeting drop language and painted the layout-editor-style dashed
overlay, which is not what a browser-tab reorder gesture should ever show.
This is a different bug from the earlier reorder-no-op fix: that one made
the drag DO something; this one is about what it visually enters into.

A plain (non-session) pane tab already has the right shape for this —
`startPaneDrag`/`drag-session.ts`'s `reorder` context confines an in-strip
drag to `mode: 'reorder'` and only escalates to zone mode on tear-off past
`TEAR_OFF_SLACK_PX`. Session tabs never had an equivalent: `startSessionDrag`
had no `reorder` concept at all, so there was no way for a tab drag to stay
strip-confined.

**Fix:** added an optional `reorder: { groupId, strip }` param to
`startSessionDrag`, populated only for TAB drags (session tiles + the
workspace tab), never for sidebar-row drags. When set, the whole drag is
confined to its own strip for its entire lifetime — `onEngage` skips the
zone/strip/composer snapshots and NEVER sets `$treeDragging`, `resolveMove`
unconditionally resolves an insertion slot via `slotBefore` (mirroring
`startPaneDrag`'s in-strip branch, no tear-off escalation), and `onCommit`
calls `reorderTreePane` directly instead of `openSessionTile`. Per the
user's explicit choice, this is NOT "hide the overlay while still allowing
the drop" — dragging a session tab now literally cannot leave its strip or
enter zone-move mode at all; docking/splitting/linking a session elsewhere
remains a SIDEBAR ROW drag's job (unchanged, still lights the full overlay).
Threaded the new `reorder` param through the `PaneChrome.tabDrag` /
`PaneMirror.tabDrag` type signatures (`track-model.ts`, `pane-mirror.ts`)
and their two call sites (`session-tile.tsx`'s tile tabDrag,
`controller.tsx`'s `workspaceTabDrag`) so both the workspace tab and every
session tile tab pick up the confinement; exported `stripSlots`/`StripSlot`
from `drag-session.ts` so `session-drag.ts` could reuse the same strip-slot
geometry helper `startPaneDrag` uses internally.

Files: `apps/desktop/src/app/chat/session-drag.ts` (`reorder` mode),
`apps/desktop/src/app/chat/session-tile.tsx` + `apps/desktop/src/app/contrib/controller.tsx`
(pass `reorder` through), `apps/desktop/src/app/chat/pane-mirror.ts` +
`apps/desktop/src/components/pane-shell/tree/renderer/track-model.ts`
(`tabDrag` signature), `apps/desktop/src/components/pane-shell/tree/renderer/drag-session.ts`
(export `stripSlots`/`StripSlot`), `apps/desktop/src/components/pane-shell/tree/renderer/tree-group.tsx`
(pass strip/groupId into `chrome.tabDrag`).

Verified: `tsc -p . --noEmit && tsc -p tsconfig.electron.json --noEmit`
clean; `eslint` clean on every touched file (one pre-existing unrelated
import-order lint error in `controller.tsx`, reproduced identically on a
clean `git stash`, left untouched); full `vitest run` — 2107 passed / 104
failed, byte-identical failure count/file set to a clean `main` run before
this change (same pre-existing `window.localStorage` jsdom setup issue);
real `vite build` production build succeeds.

### Fork-only fix — 2026-07-24 (desktop: session/tab drag-to-reorder didn't work at all, or silently reverted)

**Reported:** two related bugs in the desktop app — (1) can't drag to
rearrange the browser-style tabs at the top of a chat/session (the workspace
tab + session tile strip), and (2) session rows in the sidebar Recents/Pinned
list drag fine but snap back to their old position instead of staying where
dropped.

**Bug 1 — tab drag no-ops for the loaded MAIN session.** `startSessionDrag`
(`app/chat/session-drag.ts`) is the shared resolver for both sidebar-row
drags and tab drags (`session-tile.tsx`'s `tabDrag` + `controller.tsx`'s
`workspaceTabDrag`, both wired through `pane-mirror.ts`/`tree-group.tsx`).
Its commit path always called `openSessionTile(payload.id, ...)`, and
`openSessionTile` (`store/session-states.ts`) early-returns a no-op whenever
`payload.id === $selectedStoredSessionId.get()` — i.e. whenever the dragged
tab IS the loaded main session (the most common tab to drag: it's always
present, always first). Every reorder/split/stack drag of the main
workspace tab silently did nothing. A second, compounding bug: the strip
divider math (`slotBefore`) and the post-commit `revealTreePane` call both
hardcoded the tile-prefixed pane id `session-tile:${payload.id}`, which is
correct for a session TILE but wrong for the main session (which lives at
pane id `'workspace'`, no prefix) — so even fixing the first bug alone would
have left the main tab's own-slot exclusion and reveal targeting a
non-existent pane.

**Fix:** `openSessionTile` now resolves the anchor's group and calls
`moveTreePane('workspace', ...)` instead of no-op'ing when the dragged
session is the loaded main session — mirroring the tile-move path one branch
up. `session-drag.ts` derives `ownPaneId` (`'workspace'` when dragging the
loaded main session, `session-tile:<id>` otherwise) once per drag and uses
it for both the strip's own-slot exclusion (`slotBefore`) and the
post-commit `revealTreePane` call, replacing the two now-wrong hardcoded
`session-tile:` references.

**Bug 2 — sidebar drag order gets discarded on the next render.**
`flattenSessionsWithBranches` (`lib/session-branch-tree.ts`), used to nest
branch/fork sessions under their parent for BOTH the flat Recents/Pinned
list (`sessions-section.tsx`) and each project/worktree lane
(`workspace-group.tsx` → `renderRows`), unconditionally re-sorts top-level
(non-nested) rows by `groupRecency` (freshest-branch-in-cluster wins) on
every call — including immediately after a drop, when the caller's `sessions`
prop is *already* the exact order the drag just produced
(`agentOrderManual`-gated `orderByIds(...)` upstream in `sidebar/index.tsx`,
or a lane's `laneSessionOrder` in `workspace-group.tsx`). The re-sort
silently discarded that manual order on the very next render, so a
successful drag+drop reverted a frame later. A second, independent bug in
the same code path: the flat (non-virtualized) `ReorderableList` instances
passed `ids={sessions.map(s => s.id)}` (pre-flatten order) as dnd-kit's
`SortableContext` items while rendering `displayEntries` (post-flatten,
branch-nested) children — any divergence between the two orders corrupts
dnd-kit's from/to index math independently of the recency bug.

**Fix:** `flattenSessionsWithBranches` takes an optional `{ preserveOrder }`
— when true, the top-level `.sort(...)` is skipped entirely (branch nesting
via the existing recursive `emit()` is untouched either way). Threaded a new
`manualOrder` prop through `SidebarSessionsSection` (`true` for Pinned,
`agentOrderManual` for Recents) and a `preserveOrder` argument through
`renderRows` (`Boolean(laneSessionOrder?.length)` from
`SidebarWorkspaceGroup`) so every call site that already holds an
authoritative manual order tells the flattener to keep it. Also fixed the
`ReorderableList` `ids` mismatch in both the flat and virtualized branches to
derive from `displayEntries` instead of the raw `sessions` prop, so dnd-kit's
index space always matches rendered DOM order.

Files: `apps/desktop/src/store/session-states.ts` (`openSessionTile`),
`apps/desktop/src/app/chat/session-drag.ts` (`ownPaneId`),
`apps/desktop/src/lib/session-branch-tree.ts` (`preserveOrder`),
`apps/desktop/src/app/chat/sidebar/sessions-section.tsx` (`manualOrder` prop
+ `ids` fix), `apps/desktop/src/app/chat/sidebar/index.tsx` (wiring),
`apps/desktop/src/app/chat/sidebar/projects/workspace-group.tsx` +
`entered-content.tsx` (`renderRows` signature).

Verified: `tsc -p . --noEmit && tsc -p tsconfig.electron.json --noEmit`
clean; `eslint` clean on every touched file; added 3 new regression tests to
`lib/session-branch-tree.test.ts` (default recency sort still applies,
`preserveOrder` keeps input order, branch nesting still works under
`preserveOrder`) — 6/6 pass; full `vitest run` — 2107 passed / 104 failed,
identical failure count and file set reproduced on a clean `git stash` of
this change (pre-existing `window.localStorage` jsdom setup issue, unrelated
to this fix); real `vite build` production build succeeds.

### Fork-only fix — 2026-07-24 (thinking-signature retry recovery no longer forces a user-facing ⚠️ warning)

**Reported:** user kept seeing "⚠️  Thinking block signature invalid, stripped
reasoning_details from api_messages for retry..." repeatedly during normal
desktop-app use and asked for it to be found and fixed.

**Investigation:** traced the recovery path in
`agent/conversation_loop.py` (`FailoverReason.thinking_signature`, classified
in `agent/error_classifier.py` from an Anthropic 400 containing "thinking" +
"signature"/"cannot be modified"/"must remain as they were"). Anthropic signs
extended-thinking blocks against the exact content of the turn they came
from; any upstream mutation of that turn (interrupting the response mid-flight,
the desktop backend restarting mid-turn, orphaned tool_use stripping,
message-role merging) invalidates the signature and the next replay 400s.
The recovery is a one-shot, self-healing strip of `reasoning_details` /
`anthropic_content_blocks` from `api_messages` — the **wire-payload copy
only**, never the canonical DB-persisted `messages` list (this distinction
was deliberately fixed upstream in commit `9f95f72b98` after an earlier
version popped the field from `messages` directly and permanently corrupted
stored sessions). Correlated the desktop.log occurrences against
`⚡ Interrupted during API call.` / backend-restart (`HERMES_BACKEND_READY`)
lines — every occurrence in the user's logs immediately follows one of those,
consistent with the known "interrupt/restart lands between thinking+tool_use
capture and persist" gap that the pre-emptive `_thinking_signature_invalidated`
guard (`agent/anthropic_adapter.py` / `agent/fork/anthropic_messages.py`)
already targets but can't fully close for genuinely mid-flight kills. Checked
`agent/context_compressor.py` for a second, unflagged mutation path (compression
stripping/merging thinking blocks without setting the invalidation flag) —
compression only prunes tool_calls/tool_results, and the final orphan-check in
`convert_messages_to_anthropic` runs over the fully assembled list regardless
of source, so that path is already covered.

**Verdict:** working-as-designed, no data loss — but the recovery was printed
via `agent._vprint(..., force=True)` (always shown, even in quiet/streaming
modes) as a scary `⚠️` despite requiring zero user action. Per user request,
downgraded it: dropped the forced `_vprint` call entirely and changed the
paired `logger.warning` to `logger.debug` (still fully traceable in
`agent.log` at debug level, just not surfaced to the live UI/terminal as an
alarm). The strip/retry mechanics are unchanged.

Files: `agent/conversation_loop.py` (~line 3454-3479).
Verified: `tests/run_agent/test_thinking_sig_recovery_persistence.py` (4/4
pass, asserts the strip still targets `api_messages` only and leaves
canonical `messages` untouched — untouched by this change since only the
print/log calls moved); `tests/agent/test_turn_retry_state.py` +
`tests/run_agent/` full pass modulo 5 pre-existing failures confirmed
unrelated (streaming/Bedrock credential-refresh mocks, reproduced identically
on a clean `git stash` of this change).

### Fork-only fix — 2026-07-24 (profile clone: `--clone`/`--clone-config` never copied installed pets)

**Reported:** user cloned a new profile (`exo`) from `default` with
`--clone`, and the resulting profile lost its petdex mascot — the sprite
just didn't render even though `config.yaml`'s `display.pet.slug` was
correctly copied over as `hatsune-miku`, `enabled: true`.

**Root cause:** `create_profile()`'s `clone_config` path (`hermes profile
create --clone`) copies `_CLONE_CONFIG_FILES` (`config.yaml`, `.env`,
`SOUL.md`) and the source's `skills/` tree, but never touched the source's
`pets/` directory. Pet identity is split across two places: `config.yaml`
stores which pet slug is *active* (`display.pet.slug`), but the actual
downloaded asset — `pets/<slug>/pet.json` + `spritesheet.webp` — lives
under the profile root, sibling to `skills/`, not inside `config.yaml`.
Copying the config half without the asset half left the clone pointed at a
pet slug it had never downloaded, so pet rendering silently fell back to
"no pet" with no error surfaced anywhere (this is the same class of gap
`test_clone_config_copies_source_skills` already guards for skills — pets
just weren't in the mirrored list). `--clone-all` was unaffected since it
does a full `shutil.copytree` of the entire source profile including
`pets/`.

**Fix:** added a `pets/` copytree step to the `clone_config` branch in
`hermes_cli/profiles.py::create_profile()`, immediately after the existing
skills copytree and gated the same way (`if source_pets.is_dir(): ...`, a
no-op when the source profile has no pets installed).

**Verification:** `scripts/run_tests.sh tests/hermes_cli/test_profiles.py`
— 158/158 passing (156 existing + 2 new:
`test_clone_config_copies_installed_pets`,
`test_clone_config_missing_pets_dir_skipped`). Manually reproduced against
the user's real `exo` profile (`hermes -p exo pets install hatsune-miku
--select` restored the missing asset as an immediate workaround) before
landing the root-cause fix.

**Files touched:** `hermes_cli/profiles.py` (clone_config pets copytree),
`tests/hermes_cli/test_profiles.py` (2 new tests).

**Merge note:** fork-only file, no upstream equivalent for the pets
feature — no conflict risk.

### Fork-only fix — 2026-07-24 (invisible token/cost doubling from native `web_search_20250305` server-tool passes; sibling fail-closed evidence-scrubber in `_sanitize_replay_block`)

**Reported:** a live session's status bar showed a "720K new" token jump on a
single turn (from ~670K to ~1.39M), which looked like — and was initially
suspected to be — a session-corrupting bug (compaction fired immediately
after, treated as an emergency). Full forensic trail: session
`20260723_211736_99ee22`, API call #26, 2026-07-24.

**Root cause:** `agent/fork/anthropic_native_web_search.py` (an existing
fork-only feature, not new) unconditionally swaps the client-side
`web_search` tool for Anthropic's native server-side `web_search_20250305`
tool on every first-party-Anthropic API call. When the model actually invokes
that native tool mid-turn, Anthropic runs a **second internal inference
pass** over the same (by-then-warm) prompt prefix — pass 1 reads
`cache_read=R` tokens and writes `cache_creation=C` new ones; pass 2 (after
the search result returns) reads the now-warm `R+C` prefix with zero new
creation. Anthropic's cumulative `usage` object in the final
`get_final_message()` sums both passes: `cache_read = R + (R+C) = 2R+C`.
Verified exactly against the real numbers: `1,385,062 == 2×691,645 + 1,772`,
an *exact* integer match, not approximate. This is genuine Anthropic billing
for a real second pass the model chose to trigger — not a Hermes accounting
bug — but it was **completely invisible** in Hermes's own logs: the per-call
line (`conversation_loop.py`, "API call #N: in=X") showed only the summed
total with no indication a server-side tool had run, making a real,
explicable cost multiplier look like an unexplained context-tracking
malfunction.

Ruled out before landing on the real cause (each with hard evidence, not
just plausibility): cross-session/cross-thread contamination (a second
session was concurrently in-flight on the same model, but
`691,645 + 444,988 = 1,136,633 != 1,385,062` — arithmetic rules out simple
additive contamination); Hermes's own stream-retry loop (no
`_emit_stream_drop`/`_log_stream_retry`/"Reconnected after a dropped
stream" log lines anywhere in the window); MoA reference-usage folding (no
MoA preset configured for this session); an Anthropic SDK-level
accumulation bug (`anthropic/lib/streaming/_messages.py`
`accumulate_event()` uses plain assignment, not `+=`, for usage fields —
confirmed by reading the installed SDK source, v0.87.0); and the
already-known `tool_search` server-tool re-billing mechanism from a prior
2026-05-13 incident (case 00271597) — that one is gated behind
`tool_search.mode: server_side` (non-default, warned-on-opt-in), and this
session used the safe `client_side` mode, so it's a different (if
structurally similar) mechanism.

**Compounding discovery — a second, independent bug that hid the evidence:**
When first checking whether a server-side web search had actually occurred,
querying `~/.hermes/state.db`'s `messages.anthropic_content_blocks` for the
resulting assistant message showed only 4 clean blocks (`text`, `thinking`,
`text`, `tool_use`) — no `server_tool_use` or `web_search_tool_result` block
— which looked like it *ruled out* a server-side search. It didn't.
`agent/anthropic_adapter.py::_sanitize_replay_block()` (the function that
prepares a stored response block for replay as request input) was a strict
**fail-closed** whitelist: `text` / `thinking` / `redacted_thinking` /
`tool_use` / `image` only, silently `return None`-ing (dropping) anything
else — including `server_tool_use` and `web_search_tool_result` — before
persistence. Its sibling function, `_sanitize_block_for_anthropic_input()`
(used for the *same class* of problem, sanitizing tool_result inner blocks),
already has the **correct, fail-open** contract: unrecognized block types
pass through unchanged rather than being dropped, specifically so a future
SDK-added block type "doesn't silently get stripped before this map is
updated" (its own docstring). The asymmetry between the two sibling
functions was itself the bug: `_sanitize_replay_block` erased the only
on-disk evidence that a server-side tool call had happened, making the
real cost-multiplier bug above look unfalsifiable by DB inspection alone.

**Fix:**
1. `agent/usage_pricing.py` — `CanonicalUsage` gained
   `server_tool_web_search_requests` / `server_tool_web_fetch_requests`
   fields (+ a `server_tool_requests` convenience property and `__add__`
   support), populated in `normalize_usage()` from Anthropic's own
   authoritative `response.usage.server_tool_use.{web_search,web_fetch}_requests`
   counter — no re-derivation from content blocks needed.
2. `agent/conversation_loop.py` — the per-call `logger.info("API call #%d: ...")`
   line now appends `server_tool_passes=N (web_search=X web_fetch=Y — each is
   an extra Anthropic-side inference pass folded into this usage figure)`
   whenever a call actually invoked a native server tool, so a doubled/
   inflated call is immediately self-explanatory instead of looking like an
   inexplicable spike.
3. `agent/anthropic_adapter.py::_sanitize_replay_block()` — flipped from
   fail-closed to fail-open: the four hand-reconstructed types (`text`,
   `thinking`, `redacted_thinking`, `tool_use`, `image` — these need custom
   logic, e.g. tool-id sanitizing, dropping empty `redacted_thinking`) are
   unchanged, but anything else now delegates to
   `_sanitize_block_for_anthropic_input()` (the already-correct, SDK-derived,
   fail-open sibling) instead of being dropped. This can never be *more*
   lossy than the old behavior — only strictly less so — and closes the
   blind spot for `server_tool_use` / `web_search_tool_result` /
   `tool_search_tool_*` blocks today, and any future SDK server-tool block
   type Hermes hasn't special-cased yet.

**Deliberately NOT changed (left for a human/policy decision, not a pure bug
fix):** `agent/context_compressor.py::update_from_response()` still reads
the (correctly-reported, but still large) `prompt_tokens` figure and will
still trigger auto-compaction on a genuinely large server-tool-inflated
turn. Whether compaction should discount server-tool-caused inflation is a
policy call (the next turn's real prompt may legitimately still be huge if
another search fires) — the fix here is about *attribution*, not about
suppressing/discounting real, billed token growth.

**Also confirmed NOT the same live bug, but same *bug class*, lower
priority (found via a full-codebase scrub after the fix above, not yet
acted on):**
- `_apply_tool_search`'s `server_side` mode (`agent/anthropic_adapter.py`,
  `_apply_tool_search`) has the identical "server-tool re-bills the full
  prompt per iteration" mechanism (already documented in that function's own
  docstring, referencing the 2026-05-13 case). Dormant: `client_side` is the
  default and `cli.py` warns on opt-in to `server_side`.
- MoA advisor-fanout usage (`conversation_loop.py`'s
  `_moa_client.consume_reference_usage()` fold) sums N advisor passes into
  one log line with no "N passes" breakdown — same *attribution* gap as the
  fixed bug, different mechanism (client-side fan-out, not a server-side
  tool), not fixed this round.
- `pause_turn` retry recovery and stream-abandon retries *discard* the
  rejected attempt's usage entirely (opposite direction: under-reporting,
  not over-reporting) — same "attribution missing from logs" root class, not
  fixed this round.
- No native `web_fetch` / `code_execution` / `bash_20250124` /
  `computer_use`-style server tool is currently wired in (zero hits for
  those type strings outside `computer_use`, which is deliberately kept on
  the generic OpenAI-compatible client-side schema, per its own module
  docstring and `tests/tools/test_computer_use.py`). If a `web_fetch` twin
  of `anthropic_native_web_search.py` is ever added (its own docstring at
  line ~42-44 already anticipates this), it will need the same
  `server_tool_web_fetch_requests` attribution wired through — the plumbing
  added in this fix already supports it (the field exists;
  `normalize_usage()` already reads `web_fetch_requests` from the SDK's
  `ServerToolUsage` model), it just needs a real fetch to occur to exercise
  it.

**Files touched:** `agent/usage_pricing.py`, `agent/conversation_loop.py`,
`agent/anthropic_adapter.py`, `tests/agent/test_anthropic_output_field_leak.py`.

**Verification:** `ast.parse()` clean on all three modified `agent/` files.
Full targeted test sweep: `tests/agent/test_usage_pricing.py`,
`tests/agent/test_context_engine_host_contract.py`,
`tests/run_agent/test_moa_loop_mode.py` (82 tests, all constructing
`CanonicalUsage` directly) — all pass, confirming the new dataclass fields
+ extended `__add__` don't break existing consumers. Manual construction of
a synthetic Anthropic-shaped `usage` object (matching the real captured
numbers: `cache_read_input_tokens=1385062`, `cache_creation_input_tokens=1772`,
`server_tool_use.web_search_requests=2`) through `normalize_usage()`
confirmed `server_tool_requests=2`, correct `prompt_tokens`, and correct
summing through `__add__`. `tests/agent/test_anthropic_output_field_leak.py`
— replaced the one existing test that asserted the *old, buggy* fail-closed
behavior (`test_unknown_type_dropped`, asserting
`_sanitize_replay_block({"type": "server_tool_use", ...}) is None`) with
three new regression tests: `server_tool_use` survives sanitization with
known fields intact and unknown fields stripped;
`web_search_tool_result` survives identically; and a genuinely novel/future
block type passes through completely unchanged. All 9 tests in that file
pass. Broader regression check: `pytest tests/agent/ -k anthropic` — 603
passed, 2 failed, 2 skipped; the 2 failures
(`test_auxiliary_anthropic_pool_fallback_regression.py`, a mock-signature
mismatch unrelated to any change here) were confirmed **pre-existing** by
stashing this change and re-running against clean `main` — identical
failure, so not introduced by this fix.

### Fork-only fix — 2026-07-24 (desktop: replaced deprecated `rcedit` dep with `resedit`)

**Reported:** `hermes desktop` printed an npm deprecation warning on every
launch (`rcedit@5.0.2: Package no longer supported`) during the workspace
dependency install step.

**Root cause:** `apps/desktop/scripts/set-exe-identity.mjs` (the afterPack
hook that stamps the Hermes icon + version-info strings onto the packed
Windows `Hermes.exe`, since `build.win.signAndEditExecutable=false` disables
electron-builder's own resource-editing step) depended directly on
`rcedit@^5.0.2`, whose npm listing is marked "no longer supported."

**Fix:** rewrote `set-exe-identity.mjs` to call `resedit` (pure-JS PE
resource editor, actively maintained, MIT) directly instead of shelling out
to `rcedit`'s bundled native `rcedit(-x64).exe` under Wine. `resedit` was
already present in the tree as a transitive dependency of `electron-builder`
→ `app-builder-lib` (which uses it internally for the identical job — see
`node_modules/app-builder-lib/out/util/resEdit.js`), so this is a like-for-
like swap with **zero new dependency footprint**. Updated
`apps/desktop/package.json` (`rcedit` → `resedit@^1.7.2`), ran `npm install`
to regenerate the root lockfile (rcedit fully removed from
`package-lock.json` and `node_modules`), and updated stale `rcedit`
references in comments (`after-pack.mjs`, `scripts/install.ps1`).

**Files touched:** `apps/desktop/scripts/set-exe-identity.mjs` (rewritten),
`apps/desktop/scripts/after-pack.mjs` (comment only), `scripts/install.ps1`
(comment only), `apps/desktop/package.json`, `package-lock.json`.

**Verification:** downloaded the real Windows Electron 40.10.2 build
(`electron-v40.10.2-win32-x64.zip`) to get an actual PE32+ exe, ran the
rewritten script against it end-to-end on macOS
(`node scripts/set-exe-identity.mjs <exe>`) — exited 0, then re-parsed the
stamped exe with `resedit` to confirm `ProductName`/`FileDescription`/
`CompanyName`/`LegalCopyright` all landed correctly and the icon resource
entries changed (5 → 7, reflecting Hermes's `.ico` frame count), i.e. the
icon was actually replaced, not just the version strings. Also confirmed
`npm run typecheck` and `npx eslint scripts/set-exe-identity.mjs
scripts/after-pack.mjs` pass clean, and `npm install` no longer prints the
rcedit deprecation warning.

### Fork-only fix — 2026-07-24 (root cause found: recurring "garbled/duplicate digit" spinner-timer corruption — `get_cwidth()` blind to emoji+VS-16)

**Reported (again):** the live tool-call status line showed a corrupted
elapsed duration for a `process(action="wait", timeout=280)` call —
`wait proc_e0efad4683 280s (4m170s)` instead of `(4m17s)`. This is the
same bug class documented as unreproduced/unresolved in the 2026-07-19
entry below ("unreproduced spinner-timer anomaly — forensic logging
added, not yet root-caused"), and is at least the third time a
variant of this symptom has been reported after two prior "fixes"
(2026-07-06 status-bar timer, 2026-07-18 `KawaiiSpinner` redraw
padding) — both of which correctly diagnosed *len() vs get_cwidth()*
mismatches but left the underlying bug able to resurface.

**Root cause, finally isolated:** `prompt_toolkit.utils.get_cwidth()`
itself undercounts a specific glyph shape by 1 cell — an emoji base
codepoint followed by VARIATION SELECTOR-16 (U+FE0F). U+FE0F is Unicode
category `Mn` (nonspacing mark), so wcwidth-family width tables (which
`get_cwidth` mirrors) assign it width 0. But VS-16's entire purpose
(UTR#51) is to force emoji (wide, 2-cell) presentation, and virtually
every terminal Hermes runs in (iTerm2, Kitty, WezTerm, Terminal.app,
Windows Terminal) honors that and renders 2 cells. Confirmed live:
`get_cwidth("⚙️")` (process tool's registered emoji, U+2699 GEAR +
U+FE0F) returns `1`, not `2`. Eight other registered tool emoji are the
identical shape: `⌨️`/`◀️`/`🖼️`/`👁️`/`🖥️` (browser tools),
`✍️` (write_file), `✉️` (feishu), `⚠️` (skills warning).

That 1-cell undercount feeds directly into `_spinner_widget_height()`'s
wrap-height math (`ceil(_status_bar_display_width(spinner_line) /
terminal_width)`) — the reserved prompt_toolkit `Window` height comes out
exactly 1 row short whenever the undercounted spinner line's true width
lands right at a wrap boundary. The wrapped continuation then overlaps
whatever renders on the row below instead of getting its own row —
producing the visually-concatenated "duplicate/garbled digit" corruption
(a stale "0" from the row below bleeding into the new duration string,
read as "170s" instead of "17s"). This also explains the "impossible"
finding in the 2026-07-19 forensic entry: the duration-formatting
arithmetic (`elapsed // 60`, `elapsed % 60`) was never broken — the
corruption is a real terminal-side row overlap, not a computed value.

Crucially, this is a glyph-level blind spot *inside* `get_cwidth` itself
— both prior fixes (2026-07-06, 2026-07-18) correctly replaced `len()`
with `get_cwidth()` for the *aggregate-string-vs-len()* mismatch, but
both still called `get_cwidth` directly, so this bug survived untouched
through two "fixes" of the same symptom family. That's why it kept
coming back.

**Fix:** new shared helper `agent.display.display_cwidth()` — wraps
`get_cwidth()` per-codepoint and adds the missing 1-cell width whenever
it encounters a bare `\ufe0f`, leaving every other glyph byte-identical
to plain `get_cwidth()`. Wired into every call site that previously
called `get_cwidth` directly for display-width purposes, so the fix
applies uniformly instead of as another one-off patch:
- `agent/display.py`: `KawaiiSpinner._display_width()` now delegates to
  `display_cwidth()`.
- `cli.py`: `_status_bar_display_width()`, `_trim_status_bar_text()`,
  `_panel_cwidth()`, and module-level `_estimate_tui_input_height()` all
  now delegate to `display_cwidth()` instead of calling
  `prompt_toolkit.utils.get_cwidth()` directly.

Verified numerically before/after: `get_cwidth("⚙️")` → `1`;
`display_cwidth("⚙️")` → `2`. All 9 affected VS-16 tool emoji now
measure correctly; all previously-correct emoji (astral wide emoji with
no VS-16, e.g. `🌐`/`💻`/`📖`) are unaffected — `display_cwidth ==
get_cwidth` for those, confirmed by test.

**Tests:** new `tests/agent/test_display_cwidth_vs16.py` (8 tests) —
direct measurement of all 9 affected VS-16 tool emoji, confirmation that
non-VS-16 emoji and plain ASCII are unaffected, an end-to-end status-line
string assertion (`display_cwidth(line) == get_cwidth(line) + 1` for a
line containing exactly one VS-16 tool emoji), and confirmation that
`KawaiiSpinner._display_width` delegates to the shared helper. Full
`tests/agent/test_display_cwidth_vs16.py` + `tests/cli/test_cli_status_bar.py`
+ `tests/agent/test_kawaii_spinner_display_width.py` +
`tests/agent/test_display.py` + `tests/agent/test_display_tool_failure.py`
+ `tests/agent/test_display_todo_progress.py`: 197 passed (8 new), 0
failed. Broader `tests/cli/` + `tests/agent/` run surfaced 14 pre-existing
failures across 8 unrelated files (service_tier config, exit-summary
resume hint, credential pool auth-type, auxiliary runtime cache key) —
confirmed identical on a clean unmodified clone of current `main` via a
separate checkout, unrelated to this change. Zero new failures.

**Still open:** the 2026-07-19 forensic warning latch
(`_spinner_elapsed_anomaly_logged` in `cli.py`) is left in place — it
guards against a *different* failure mode (elapsed exceeding session age,
an actual arithmetic/state bug) and never fired for this incident, so it
remains a valid tripwire for a genuinely different anomaly if one occurs.

Files: `agent/display.py` (+ new `tests/agent/test_display_cwidth_vs16.py`),
`cli.py`.

### Fork-only fix — 2026-07-23 (spurious "Event loop is closed" traceback on /exit)

**Symptom:** on `/exit`, after the "(cleaning up — press Ctrl+C to quit
immediately)" message, the CLI process occasionally printed a Python
"Exception ignored in: <coroutine object MCPServerTask.run at ...>" /
`RuntimeError: Event loop is closed` traceback pointing at
`_wait_for_reconnect_or_shutdown` in `tools/mcp_tool.py`, during interpreter
teardown right before the process actually exited.

**Root cause — two compounding bugs in `tools/mcp_tool.py`:**

1. **Orphaned parked task.** `_connect_server()` calls `MCPServerTask.start()`,
   which awaits `self._ready` and then re-raises `self._error` on failure —
   but by design `run()` doesn't exit on every error path: when the initial
   connection fails `_MAX_INITIAL_CONNECT_RETRIES` times (or hits certain
   OAuth/URL-validation failures), `run()` sets `_error`/`_ready` and then
   *parks* in `_wait_for_reconnect_or_shutdown(timeout=_PARKED_RETRY_INTERVAL)`
   to self-probe every 5 minutes, rather than returning. `_connect_server`
   propagated the exception without ever returning the `MCPServerTask`
   instance to its caller, so nothing held a reference to it — the parked
   task ran forever, invisible to `shutdown_mcp_servers()` (never inserted
   into `_servers`), a live orphan on the MCP background loop.
2. **Unguarded `cancel()` in cleanup `finally` blocks.** Three near-identical
   lifecycle-wait helpers (`_wait_for_lifecycle_event`,
   `_wait_for_reconnect_or_shutdown`, `_wait_for_lazy_reconnect`) each had a
   `finally:` block that called `task.cancel()` *outside* its guarding
   `try/except`. `Task.cancel()` schedules internally via
   `loop.call_soon()`, which raises `RuntimeError('Event loop is closed')`
   once the owning loop has actually been closed. At `/exit`,
   `_stop_mcp_loop()` calls `loop.close()` on the MCP background loop
   regardless of whether the orphaned parked task (bug 1) is still alive on
   it. When the orphan's coroutine was later garbage-collected, Python threw
   `GeneratorExit` into it to run `close()`, resuming this `finally:` block
   — whose unguarded `cancel()` then raised past the `try/except` below it,
   surfacing as the "Exception ignored in" trace.

**Fix:**
- `tools/mcp_tool.py`: `_connect_server()` now wraps `await server.start(config)`
  in `try/except Exception` (the pre-existing `CancelledError` re-raise path
  from #59349 is preserved separately) and calls `await server.shutdown()`
  before re-raising, reaping the orphaned task instead of abandoning it.
- Extracted the duplicated cancel-and-await cleanup from all three lifecycle
  wait helpers into one shared `_cancel_lifecycle_wait_tasks()` function,
  with `task.cancel()` correctly inside the `try` alongside the `await` so
  `RuntimeError('Event loop is closed')` is caught by the existing
  `except (asyncio.CancelledError, Exception)`.

**Tests:** `tests/tools/test_mcp_tool.py` — added
`TestConnectServerOrphanReaping` (2 tests: a `start()` failure via
`server._error` now leaves `server._task` fully done and
`_shutdown_event` set rather than orphaned; the pre-existing
`CancelledError` path from #59349 still reaps cleanly) and
`TestLifecycleWaitFinallySurvivesClosedLoop` (2 tests against the new
shared helper directly, using a duck-typed fake task since real
`asyncio.Task` is immutable in CPython and can't have `.cancel` patched:
confirms `cancel()` raising `RuntimeError('Event loop is closed')` on both
tasks doesn't escape, and confirms normal cancellation still awaits the
task out to `cancelled()`). Full `tests/tools/test_mcp_tool.py` suite:
218 passed (214 pre-existing + 4 new — `test_register_wakes_stale_cached_server`
in the sibling `test_mcp_register_wakes_stale.py` fails identically on
unmodified `main` when run in the same session, a pre-existing order-dependent
flake unrelated to this change, confirmed via `git stash`).

### Fork-only feature — 2026-07-23 (desktop: sidebar click opens browser-like tabs instead of always replacing the middle pane)

**Ask:** user wanted the middle chat pane's tab strip to behave like a
browser — multiple sessions visible as tabs at once — instead of always
showing a single tab.

**What already existed:** the tab strip (`components/pane-shell/tree/renderer/tree-group.tsx`)
already fully supports multiple stacked tabs, and there's already a "session
tile" mechanism (`store/session-states.ts`'s `openSessionTile(id, 'center')`)
that docks a session as an additional closeable tab beside the main
`workspace` tab. It was already wired to ⌘/⌃-click, middle-click (open in new
tab), and ⇧⌘-click (pop into its own OS window) on sidebar session rows. The
gap: a **plain click** (`onResumeSession` in `app/contrib/wiring.tsx`) always
called `navigate(sessionRoute(id))`, which loads the session into the
`workspace` tab **in place** — so serially clicking through sidebar sessions
only ever showed one tab, no matter how many sessions you'd visited.

**Fix (browser-tab semantics for a plain click):**
1. If the session is already showing (an open tile, or the loaded main
   session) — jump to its tab (`focusOpenSession`, unchanged).
2. Otherwise, if the workspace is currently *empty* (a fresh "New session"
   draft, or a full-page route with no session loaded) — load straight into
   it, same as before. The very first session you open never needs a tile.
3. Otherwise — call `openSessionTile(id, 'center')` instead of navigating,
   so the session opens as a new tab **beside** whatever's already showing,
   rather than replacing it.

This reuses the existing tile/tab machinery verbatim (no new tab-model
concepts, no change to the uncloseable-workspace invariant) — it only changes
what a plain sidebar click decides to do with it. Considered and rejected: a
"first click replaces, 2+ tabs open new" hybrid — it degenerates to always-new
after the first couple of clicks in normal use, so it just adds a confusing
mode for no real benefit over the simpler always-open-or-focus rule above.

**Verification:** `tsc --noEmit` clean; `apps/desktop` production build
succeeds; `store/session-states.test.ts` (6/6, unchanged) passes. Full
`vitest run src/store` has pre-existing unrelated failures on this machine
(`window.localStorage` unavailable under this Node/vitest config) — confirmed
identical failure count on `main` before this change via `git stash`.

**Follow-up same day:** user reported the new tab opened but didn't come to
the foreground. Root cause: `insertAtGroup` in
`components/pane-shell/tree/model.ts` takes an `activate` flag that defaults
`true` for a real drop/gesture but is explicitly `false` for *silent*
adoption (`adoptContributedPanes` in `store.ts` calls it with
`activate=false` so a background pane — e.g. logs stacking into an existing
terminal zone — can't steal an already-focused tab). A brand-new session tile
pane is adopted through that same silent path the instant it's registered,
so `openSessionTile()` alone opens the tab behind whatever's already showing.
The existing drag-to-open-tile flow (`app/chat/session-drag.ts`) already
compensates for this by calling `revealTreePane()` right after
`openSessionTile()` — added the same call to `onResumeSession`'s new-tab
branch (front the tab a sidebar click just opened, since a click is an
explicit user gesture, exactly like the drag case).

**Files:** `apps/desktop/src/app/contrib/wiring.tsx` (`onResumeSession`
branch + `$workspaceIsPage`/`openSessionTile`/`revealTreePane` imports).

**Verification (follow-up):** `tsc --noEmit` clean; production build
succeeds; `store/session-states.test.ts` (6/6, unchanged) passes.

**Merge note:** touches an upstream file (`wiring.tsx`) — small, isolated
diff inside one callback; low conflict risk but worth a careful look on
next upstream sync.

### Fork-only feature — 2026-07-23 (desktop: visible × close button on every pane tab)

**Follow-up to the browser-tab feature above:** once sidebar clicks could
open multiple session tabs, the user noticed there was no visible way to
close one — `PaneTab`'s design had deliberately shipped without a hover-X
("too easy to hit on small tabs" — the close gesture was middle-click or
⌘-click only, discovered via the code comment, not obvious from the UI).
Asked to add one back.

**Design:** a single reserved trailing slot in every `PaneTab` (`components/ui/pane-tab.tsx`)
that holds the dirty-dot and the close × as two absolutely-positioned layers
in the *same* footprint, cross-fading between them — VS Code's dirty-dot/close
swap. The slot is a REAL flex child (`shrink-0`), so the label truncates
around it instead of an indicator floating over live tab text.
- **Active tab:** × always visible (an open tab's close affordance
  shouldn't depend on a hover state that isn't currently true).
- **Inactive tab:** × hover/focus-reveal only, so a dense tab strip doesn't
  turn into a field of × buttons at rest — matches the app's existing
  hover-reveal pattern (e.g. the zone header's minimize chevron in
  `tree-group.tsx`).
- Click handler calls `preventDefault`/`stopPropagation` before `onClose()`
  so it can never also fire the tab's own activate/drag pointerdown handler
  (same pattern the existing ⌘-click-to-close branch already used).
- Existing gestures (middle-click, ⌘-click, right-click → Close/Close
  others/Close to the right) are untouched — the button is one more way in,
  not a replacement.
- New `closeLabel?: string` prop for a per-tab accessible name (e.g. "Close
  My Session Title"); falls back to a generic `t.common.close`. Wired at both
  real call sites (`tree-group.tsx`'s horizontal + vertical-rail tab strips
  via a new `zones.closeTab(label)` i18n key, mirrored into en/ja/zh/zh-hant
  — other shipped locales deep-merge onto `en` per `defineLocale`, so a
  missing key falls back automatically; `right-rail/preview.tsx`'s file tabs
  via the existing `preview.closeTab(label)` key).

**Verification:** `tsc --noEmit` clean; `eslint` clean (0 warnings after
`--fix` on 2 blank-line-before-statement nits in the new tests); production
build succeeds with no new warnings. `pane-tab.test.tsx` grew from 6 to 10
tests (renders/hides the button correctly, click closes without activating,
its pointerdown doesn't leak into the tab-strip drag handler, `closeLabel`
resolves the accessible name with a generic fallback) — all pass.
`preview-pane.test.tsx` (2/2) and `session-states.test.ts` (6/6) unchanged.
Confirmed pre-existing unrelated `window.localStorage` failures in
`bind-order-front.test.ts` / `focus-tab-hijack.test.ts` / `reactive-unhide.test.ts`
are identical on `main` before this change (`git stash` A/B).

**Files:** `apps/desktop/src/components/ui/pane-tab.tsx` (close button +
`closeLabel` prop), `apps/desktop/src/components/ui/pane-tab.test.tsx` (4 new
tests), `apps/desktop/src/components/pane-shell/tree/renderer/tree-group.tsx`
(`closeLabel` at both `PaneTab` call sites), `apps/desktop/src/app/chat/right-rail/preview.tsx`
(`closeLabel` at its `PaneTab` call site), `apps/desktop/src/i18n/{types,en,ja,zh,zh-hant}.ts`
(new `zones.closeTab` key).

**Merge note:** touches upstream files (`pane-tab.tsx`, `tree-group.tsx`,
`preview.tsx`, the i18n locale files) — moderate diff size (new prop +
button markup) but additive/non-breaking; existing close gestures and tests
are unchanged. Worth a careful look on next upstream sync in case upstream
also touches `pane-tab.tsx`.

### Fork-only feature — 2026-07-24 (desktop: workspace tab's × was missing and its "close" would have ripped the app's anchor pane out of the tree)

**Follow-up to the two features above.** After adding the × close button,
the user reported it showed on the 2nd (session-tile) tab but not the 1st —
"needs to be on all tabs. and yes that means you can close all tabs."

**Root cause, once traced:** the middle zone always has exactly ONE
`workspace` pane (`placement:'main'`, `uncloseable:true` in the contribution
registry — the app's structural anchor: dock target for splits, drag-payload
carrier, assumed live everywhere `findGroupOfPane(tree, 'workspace')` is
called) plus zero or more closeable `session-tile:<id>` panes. `uncloseable`
was overloaded to drive THREE things at once: (1) the tab's × visibility,
(2) whether the hosting zone can ever minimize (must not — collapsing MAIN
strands the app), (3) whether a lone pane forces its tab strip to show at
all. Every close-button/close-menu site keyed directly off the raw flag, so
workspace could never show a ×. Worse: `closeTreePane('workspace')` had no
registered closer for it, so blindly flipping the flag would have fallen
through to the generic dismiss-from-tree path and actually removed the
anchor pane from the layout — silently breaking every downstream assumption
that `workspace` always resolves.

**Design (validated via `consult` before writing code — B was the right
call over "just reset to a blank draft"):** real browser-tab close
semantics. Workspace can't structurally leave the tree, so "closing" it
means promoting an adjacent tab into it — load that session into main
(`resumeSession`), then drop the now-redundant tile (`closeSessionTile`) —
exactly what closing a browser tab does: the strip shrinks by one, focus
lands on a neighbor. Only when no sibling tab remains does it fall back to
resetting to a blank draft (`startFreshSessionDraft` — same as ⌘N).

- `components/pane-shell/tree/store.ts`: new `isPaneCloseable(paneId)` —
  true when a closer is registered for the pane (mirrors `closeTreePane`'s
  own resolution order) OR the pane isn't `uncloseable` at all. Every
  close-button/close-menu/⌘W site now reads through this instead of the raw
  flag: `closeWorkspaceTab` (⌘W), `closeableTreeSiblings` /
  `treeTabCloseTargets` (right-click Close others/right/all + their
  enablement counts), `closeAllTreeTabs`. The zone-never-minimize guard and
  `forceLoneHeaderForPanes` (lone-header.ts) deliberately still read the RAW
  `uncloseable` flag, untouched — a fresh single-session/no-tiles user still
  gets the clean no-tab-strip default (covered by the existing
  `lone-header.test.ts` case, unmodified and still green).
- `components/pane-shell/tree/renderer/tree-group.tsx`: both tab-render
  sites (horizontal strip + vertical-rail minimized form) and the zone
  menu's `closable()` now compute from `isPaneCloseable` instead of the raw
  flag.
- `app/contrib/wiring.tsx`: registers the actual promote-or-reset closer via
  `registerPaneCloser('workspace', …)` — the same inversion-of-control
  pattern `sessions`/`files`' side-collapse closers already use. Picks the
  adjacent pane the same way a real tab close does (`removePane` in
  model.ts: previous tab, falling back to the next one when workspace was
  first).

**Verification:** `tsc --noEmit` clean; `eslint` clean; production build
succeeds. New `components/pane-shell/tree/workspace-closer.test.ts` (4
tests: not-closeable-until-registered / closer-fires-and-pane-survives for
`closeTreePane` / ⌘W no-ops-then-fires / "Close all" count includes
workspace once registered) — all pass. `lone-header.test.ts` (4/4,
unmodified) still confirms the single-session/no-tiles default is
unaffected. `pane-tab.test.tsx` (10/10), `preview-pane.test.tsx` (2/2),
`session-states.test.ts` (6/6) unchanged. Confirmed the pre-existing
unrelated `window.localStorage` failures in `bind-order-front.test.ts` /
`focus-tab-hijack.test.ts` / `reactive-unhide.test.ts` are identical to
`main`.

**Files:** `apps/desktop/src/components/pane-shell/tree/store.ts`
(`isPaneCloseable` + every close-resolution site), `apps/desktop/src/components/pane-shell/tree/renderer/tree-group.tsx`
(both tab-render sites + `closable()`), `apps/desktop/src/app/contrib/wiring.tsx`
(the registered workspace closer), `apps/desktop/src/components/pane-shell/tree/workspace-closer.test.ts`
(new, 4 tests).

**Correction — same day.** User reported ⌘W stopped working entirely and
the lone open tab still had no × to click. Root cause: `isPaneCloseable`
was wired into every close-*resolution* site, but `forceLoneHeaderForPanes`
(the switch that decides whether the tab STRIP even renders when only one
pane is shown) was deliberately left reading the raw `uncloseable` flag —
that was the right call for the zone-never-minimize guard, but wrong here.
With `shown = ['workspace']` alone, `uncloseable:true` meant
`forceLoneHeader` stayed `false`, so `headerHidden` defaulted to `true` —
the header (and its only ×) never rendered at all. ⌘W's resolution chain
(`closeWorkspaceTab` → `closeTreePane` → registered closer → `resumeSession`)
was actually intact and unaffected by that; a `consult` second-opinion
pass confirmed the missing header fully explains "can't close the only
tab" with no other plausible cause in the traced code, and flagged the
unhandled-rejection risk in the promote path as the ⌘W-specific concern
even though the wiring itself was sound.

Fix: `forceLoneHeaderForPanes` now forces the header for ANY
`placement:'main'` pane, full stop — dropped the `!chrome.uncloseable`
condition since workspace always has a registered closer once one exists
and is therefore always effectively closeable. Also hardened the promote
path's `resumeSession(...).then(...)` into `.catch(...).finally(...)` so a
failed/rejected resume (stale id, mid-swap gateway) logs instead of
silently eating the whole ⌘W/× press, and the redundant tile still drops
either way. Updated `lone-header.test.ts`'s two workspace cases to assert
`true` (header always forced) instead of `false`; `workspace-closer.test.ts`
unaffected. `tsc`/`eslint`/build/tests all re-verified clean. No
`computer_use` used for this pass — traced statically + one `consult` call
per explicit user instruction not to drive the GUI for this task.

**Merge note:** touches upstream files (`store.ts`, `tree-group.tsx`,
`wiring.tsx`) — the riskiest of the three tab-strip changes so far, since it
changes what Close actually DOES for the app's one structurally-special
pane. Worth a very careful look on next upstream sync; if upstream reworks
the tile/workspace model, re-verify `isPaneCloseable`'s resolution order
still matches `closeTreePane`'s.

### Fork-only fix — 2026-07-23 (desktop: malformed CSS comment tripped a build-time lightningcss warning)

**Symptom:** `npm run build` in `apps/desktop` prints `Found 1 warning while optimizing generated CSS: Unexpected token Delim('*')`, pointing at `.btn-arc`'s `text-*` comment text.

**Root cause:** `src/styles.css`'s comment above `.btn-arc` read "Unlayered so it beats Tailwind's bg-\*/text-\* variant utilities." — the `bg-*/` substring contains a literal `*/`, which is the CSS block-comment close token. That closed the comment two words early; `text-* variant utilities. */ .btn-arc {` was then parsed as real (if harmless — `.btn-arc {` still matched correctly) CSS, and lightningcss choked on the leftover `text-*` token before the real comment-close.

**Fix:** reworded the comment to avoid `*/ ` appearing mid-sentence (split `bg-*/text-*` into "bg- and text- variant utilities") and put `.btn-arc {` on its own line for clarity. No selector/rule/behavior change — `.btn-arc`'s actual CSS block was already being parsed correctly; only the comment truncation and the resulting bogus warning are fixed.

**Verification:** `npm run build` in `apps/desktop` — no CSS warnings (previously: 1).

**Files:** `apps/desktop/src/styles.css` (comment reword only).

### Fork-only chore — 2026-07-23 (desktop: bumped Vite chunk-size warning ceiling for the intentional single-bundle build)

**Symptom:** `npm run build` in `apps/desktop` prints a `[plugin builtin:vite-reporter]` warning that "Some chunks are larger than 25000 kB after minification," suggesting dynamic `import()` / `codeSplitting` / raising `chunkSizeWarningLimit`.

**Investigation (before touching anything):** the desktop renderer is deliberately built as a single JS chunk — `apps/desktop/vite.config.ts` sets `rolldownOptions.output.codeSplitting: false` — because Shiki's full language bundle emits ~694 per-language dynamic-import chunks, and `electron-builder` OOMs scanning that many files during packaging (see `0175be3aa7`, the commit that first raised this same warning's ceiling to 25000). The bundle has simply grown past that ceiling: it's now ~28.2 MB (was ~22 MB when the ceiling was set).

Before proposing a `manualChunks`/`codeSplitting` rework as a "real fix," checked git history and found this exact angle was already investigated 4 days prior: `b6ae910d8c` (#67720, `bench(desktop): trustworthy cold-start measurement (code-splitting is not the lever)`, 2026-07-19). That commit built a real production bundle, measured actual cold-start composition with CDP, and found the entire bundle-eval cost is only ~0.27s of a ~1.5s cold boot — not a meaningful lever — and confirmed that re-enabling `codeSplitting` (required for `manualChunks`) reintroduces the exact electron-builder OOM the single-bundle design avoids, since `codeSplitting` is a global switch in rolldown (verified in `node_modules/rolldown/dist/shared/define-config-*.d.mts`: "If `manualChunks` and `codeSplitting` are both specified, `manualChunks` option will be ignored"). That same commit's suggested follow-up levers — deferred non-critical mount and V8 code cache — were also already addressed: `e702a45b5` (#67857, same day) wrapped the boot-hidden panes (files/preview/review/logs) in `<IdleMount>` (`requestIdleCallback`-gated mount), and V8's on-disk bytecode cache is an automatic Chromium mechanism the perf harness (`scripts/perf/lib/launch.mjs::coldStartSamples`) already accounts for via its `warm`/`--cold-fresh` split (fresh-profile cache miss costs ~+400ms, already measured, nothing to build).

Ran the bench myself on today's HEAD (`npm run perf -- cold-start --spawn --prod --runs 3`) to confirm no regression: spawn→CDP 720ms, spawn→driver 1087ms, DOM interactive 416ms, DOM content loaded 660ms — all within the harness's gated tolerance vs. the 2026-07-19 baseline (606/984/324/574ms; the delta reads as normal machine-load noise, not a code regression, since nothing on this cold-start path was touched).

**Conclusion:** no code-splitting/manualChunks work to do here — it was correctly rejected on the numbers 4 days ago, and both of that investigation's follow-up recommendations are already shipped. The warning itself is purely cosmetic (same conclusion as `0175be3aa7`); the fix is the same one applied then: bump the ceiling to reflect today's real, expected size.

**Fix:** `apps/desktop/vite.config.ts` — `chunkSizeWarningLimit: 25000` → `32000`, updated comment to reflect the current ~28MB size and to record the 2026-07-19 re-investigation so a future pass doesn't redo it. `codeSplitting: false` untouched.

**Verification:** `npm run build` in `apps/desktop` — clean, no chunk-size warning (28,223 kB bundle under the new 32,000 kB ceiling).

**Files:** `apps/desktop/vite.config.ts` (comment + `chunkSizeWarningLimit` only).

### Fork-only fix — 2026-07-23 (desktop: running tool call buried mid-group, tool window too short)

**Symptom:** in the inline tool-call list under a chat reply, once a
back-to-back run collapses into the bounded auto-scrolling window
(`ToolGroupSlot` / `.tool-group-scroll`, triggered at
`TOOL_GROUP_SCROLL_THRESHOLD = 3` rows), a still-*running* call that isn't
the last one in the model's call order stayed wherever it was issued —
buried under already-finished rows below it — instead of floating to the
bottom where the auto-scroll-to-bottom/top-fade behavior could surface it.
Separately, the window itself was only `6.75rem` tall (~2-3 rows), cramped
even when nothing was misplaced.

**Root cause:** `ToolGroupSlot` renders each tool call as a standalone row
in strict DOM/call order — deliberately, per the existing comment, so a
run reshaping mid-stream (narration/reasoning interleaved into many tiny
ranges while streaming vs. one big settled range once done) never remounts
a row. That stability is correct and worth keeping, but it means visual
order was tied 1:1 to DOM order with no way for a pending row to visually
sort last.

**Fix:** two independent CSS-only changes, no DOM/row-identity changes:
1. `ToolEntry`'s wrapper `div` (`fallback.tsx`) now carries a
   `data-tool-pending` attribute whenever `isPending` is true (mirrors the
   existing `data-tool-open`/`data-tool-row` pattern).
2. `styles.css`: `.tool-group-scroll [data-tool-row][data-tool-pending] {
   order: 1; }` — the group's inner content div is already a CSS grid
   (`grid ... gap-(--tool-row-gap)`), so `order` repaints pending row(s) to
   the end without touching the DOM. Finished rows keep flow order (all
   `order: 0`), multiple concurrently-pending rows keep their relative call
   order among themselves — only "pending vs. not" is reordered.
3. `--tool-group-scroll-max-h` raised from `6.75rem` to `13.5rem` (~2x) so
   the bounded window shows enough rows that the sorted-to-bottom pending
   call has room to actually be visible, not just technically last.

**Files:** `apps/desktop/src/components/assistant-ui/tool/fallback.tsx`
(added `data-tool-pending` attr), `apps/desktop/src/styles.css` (`order`
rule + taller `--tool-group-scroll-max-h`).

**Verification:** `npx tsc -p . --noEmit` clean, `npx eslint
src/components/assistant-ui/tool/fallback.tsx` clean, existing
`fallback.test.ts` + `fallback-model.test.ts` (30 tests) pass unchanged.

**Merge note:** small, isolated diff in two files with no upstream
equivalent behavior change (upstream doesn't have this variable
reorder) — should apply cleanly on future syncs; re-check
`--tool-group-scroll-max-h`'s value if upstream retunes it independently.

### Fork-only fix — 2026-07-23 (desktop: review pane never showed by default — hidden behind an undiscoverable ⌘G)

**Symptom:** the review pane (git working-tree diff / uncommitted-changes
list) never appeared in the app, even on layouts (e.g. Default) whose zone
tree places it directly beside the files pane. The layout editor's dimmed
preview correctly showed a "review" zone above/beside "files", but the live
app never rendered it.

**Root cause:** `$reviewOpen` in `apps/desktop/src/store/review.ts` is a
`persistentAtom` gating the review pane's visibility (`bindPaneVisibility`
in `controller.tsx` collapses the zone to nothing while `$reviewOpen` is
false). It defaulted to `false` and is only flipped true by `openReview()`,
called from the ⌘G shortcut / toggle button — a shortcut with no visible
affordance anywhere in the UI. A user who never happened to discover ⌘G
would never see the pane, even though its zone is present in every stock
layout preset that includes it.

**Fix:** default `$reviewOpen` to `true`. The pane is still gated on
`$hasWorkspace` (hidden for a detached/no-cwd chat) and remains a normal
`persistentAtom`, so an explicit ⌘G close is still remembered across
reloads — only the *first-run* default changed. Updated two stale comments
in `controller.tsx` that described the pane as "hidden until ⌘G".

**Files:** `apps/desktop/src/store/review.ts` (`$reviewOpen` default),
`apps/desktop/src/app/contrib/controller.tsx` (comments only).

**Verification:** `npx vitest run src/store/review.test.ts` — 35/35 pass
(tests explicitly set `$reviewOpen` per-case, unaffected by the default
change).

### Fork-only fix — 2026-07-22 (desktop: Terminal-deck layout opened to the logs tab instead of terminal)

**Symptom:** opening the desktop app while on the "Terminal deck" layout
preset (or any layout where `terminal` and `logs` share one tabbed zone)
showed the **logs** tab fronted, even though the user wanted/expected
**terminal** to be active.

**Root cause:** `terminal` and `logs` are both "tool panes" bound via
`bindPaneCollapse(paneId, $open, close, open)` in
`apps/desktop/src/app/contrib/controller.tsx`. Each pane's visibility is its
own independently persisted boolean store (`$terminalTakeover`,
`$logsOpen`), and they can land in the SAME tabbed zone (e.g. after using
the Quad preset, which explicitly stacks them together). `bindPaneCollapse`
ran `setPaneCollapsed(paneId, !$open.get())` unconditionally at MOUNT time
for every tool pane to reconcile the pane's store against the tree — and
`setPaneCollapsed`'s shared-zone branch always called `revealTreePane(paneId)`
when the pane's store was "open," which always sets that pane as the zone's
active tab. `terminal` is bound before `logs` in `controller.tsx`'s
module-level code, so on every single app boot, `logs`'s mount-time sync ran
LAST and unconditionally re-fronted logs over terminal whenever `$logsOpen`
was persisted true from earlier in the session history — regardless of what
the persisted layout tree's own `active` field said was actually last shown,
and regardless of which preset the user had selected. This is a classic
last-write-wins race decided by source-order of `bindPaneCollapse` calls, not
by user intent.

**Fix:** `revealTreePane(paneId, front = true)` and
`setPaneCollapsed(paneId, collapsed, front = true)` gained a `front`
parameter that gates ONLY the "make this the active tab" step — un-dismiss,
un-collapse-side, un-hide, and un-minimize all still run regardless, since
those are legitimate even during a boot-time reconciliation.
`bindPaneCollapse`'s initial mount-time sync call now passes `front: false`,
so it no longer steals the active tab away from whatever the persisted tree
already recorded; only a genuine user gesture — the live `$open.listen`
toggle callback, `restoreTreePane` (rail/chevron/tab click), a preview/review
pane landing, applying a preset — still fronts with the default `front: true`.

**Files:** `apps/desktop/src/components/pane-shell/tree/store.ts`
(`revealTreePane`, `setPaneCollapsed`), `apps/desktop/src/app/contrib/controller.tsx`
(`bindPaneCollapse`'s initial sync call).

**Tests:** new regression test
`apps/desktop/src/components/pane-shell/tree/bind-order-front.test.ts`
mirrors `bindPaneCollapse`'s exact boot sequence (terminal bound first, logs
bound last, both persisted-open) against a terminal-deck-shaped tree whose
`active` starts as `terminal`; asserts `terminal` stays active after both
binds run. Verified the test fails (`active` becomes `'logs'`) against the
pre-fix code and passes after the fix. Full desktop `ui` vitest project:
208 files / 1719 tests passed, no regressions.

**Merge note:** touches only fork-owned `store.ts`/`controller.tsx` logic
already present upstream — a straightforward 3-way merge on future syncs
(no new files, no restructuring).

### Fork-only feature — 2026-07-22 (desktop: sidebar drag-to-reorder from anywhere on the session name, not just a dedicated grab icon)

Follow-up to the sidebar drag-to-reorder entry directly below: the reorder
handle worked (see that entry for the nested-DndContext bug it fixed), but
the drag surface was a tiny dedicated dot/grabber icon — the user asked for
drag-to-reorder to also work when clicking and dragging from the session's
name/label, not just that small icon.

`SidebarSessionRow` (`session-row.tsx`) got `dragHandleProps` (dnd-kit's
`{...attributes, ...listeners}` from `useSortable`) as a single bundle,
applied only to the small `SidebarRowGrab` dot wrapper via
`data-reorder-handle` — everything else in the row, including the label,
fell through to the row's own separate pointer-drag system
(`startSessionDrag`, used for dragging a session into a pane/tab/split),
which `.closest('[data-reorder-handle]')`-excludes from firing wherever that
attribute is present.

**Fix:** added `splitDragHandleProps()` (`session-row-state.ts`) to split
dnd-kit's combined `dragHandleProps` into its POINTER activator
(`onPointerDown`) and everything else (the KEYBOARD activator's `onKeyDown`
plus `attributes` — role/tabIndex/aria-*). The row now wraps
dot+handoff-badge+label in a `display: contents` span (so it doesn't disturb
`SidebarRowBody`'s flex/gap layout) carrying only the pointer half +
`data-reorder-handle` — dragging now starts from anywhere across that wider
cluster, including the label, while a plain click still bubbles to the
button's `onClick` untouched (dnd-kit's existing 6px movement threshold is
what decides "drag" vs "click", not this wrapper). The keyboard half stays on
the small dot alone (unchanged `SidebarRowGrab`, a real focusable element),
because `display: contents` strips an element from the accessibility tree —
it can never be dnd-kit's `KeyboardSensor` activator, which needs a real,
focusable `role="button"` node to Tab onto. Without this split, Tab + Space +
Arrow reordering would have silently stopped working for session rows.

Verified: `tsc --noEmit` clean (both tsconfig targets), `eslint` clean, a
real `vite build` succeeds, and 72 sidebar unit tests pass (2 new, covering
`splitDragHandleProps`'s pointer/keyboard split and its `undefined` input
case for non-reorderable rows).

Files: `apps/desktop/src/app/chat/sidebar/session-row-state.ts` (+ test),
`apps/desktop/src/app/chat/sidebar/session-row.tsx`.

### Fork-only feature/fix — 2026-07-22 (desktop: sidebar drag-to-reorder inside a project/branch view, plus a nested-DndContext bug that broke it)

Reported: user wanted to hand-reorder sessions in the sidebar; noted the
existing project/branch (grouped) view visibly reshuffled lanes on its own
("occasionally jump active ones to the top... random and kind of jarring").
After a first implementation pass, the user rebuilt the desktop app and
reported dragging a session did nothing — the panel/pane-split gesture fired
instead.

**Root cause 1 (the original ask):** the flat Recents/Pinned sidebar lists
already supported drag-to-reorder (`dnd-kit`), deliberately sorted by creation
time — never activity — to avoid exactly this jitter. Inside an entered
project, though, sessions were flat, non-draggable, and grouped into
branch/worktree "lanes." Those lanes were re-sorted by `sortWorktreeGroups` —
an *activity*-based sort — on every project-tree refresh (turn completion,
window focus, etc.) inside `mergeRepoWorktreeGroups`. A pre-existing, dead
persisted-order atom (`$sidebarWorkspaceOrderIds`) was applied *before* that
merge step but got silently discarded by the merge's own trailing sort every
time, so no manual lane order could ever stick — this was the actual source
of the "jumps to top" jitter.

**Fix 1:** `mergeRepoWorktreeGroups` now accepts an optional manual lane order
and applies it *after* the default activity sort (instead of always ending on
it). Repurposed the existing `$sidebarWorkspaceOrderIds` atom rather than
adding a new one. Added drag handles to lane headers (`WorkspaceHeader`) and a
new per-lane session order map (`$sidebarLaneSessionOrderIds`, keyed by lane
id, with a prune helper for stale lanes) so sessions inside a lane can also be
manually reordered, independent of the lane's own order.

**Root cause 2 (why dragging did nothing after rebuild):** two compounding
issues.
1. A concurrent `hermes update` autostashed the entire uncommitted diff during
   the user's rebuild — the tested build contained none of the sidebar
   changes at all. (Same incident class as the two entries below this one;
   the "commit immediately" mandatory-workflow rule above exists because of
   this exact failure mode.)
2. Independently, the implementation itself had a real bug: it nested a
   `DndContext` for session-level dragging *inside* another `DndContext` for
   lane-level dragging (`ReorderableList` used twice, once per level).
   `dnd-kit` does not support nesting `DndContext` providers — the outer
   context's sensors capture pointer events first, so the inner list's drag
   handles silently stop reordering and the gesture falls through to
   whatever's listening outside (here, the sidebar row's own
   `startSessionDrag` pane-split/tile-open gesture). This would have broken
   dragging even without the autostash.

**Fix 2:** rebuilt `RepoFlatSection` (`entered-content.tsx`) around dnd-kit's
documented "multiple containers" pattern — **one shared `DndContext`** per
repo subtree, with sibling/nested `SortableContext`s for lanes and each
lane's sessions, and a single `onDragEnd` dispatcher that branches on a
tagged `data.type` (`'lane'` vs `{type:'session', laneId}`) to resolve which
array to reorder. A session only ever reorders within its own lane — dropping
it on a different lane or a lane header is a no-op, never an implicit
cross-lane move. Added a `SortableGroup` primitive (bare `SortableContext`,
no `DndContext` of its own) to `reorderable-list.tsx` for this nesting case,
with the pitfall documented directly in that file's header comment so it
isn't reintroduced.

Verified: `tsc --noEmit` clean (both `tsconfig.json` and
`tsconfig.electron.json`), `eslint` clean, 76 targeted unit tests pass
(15 new: manual-order vs default-order behavior in `mergeRepoWorktreeGroups`,
`mergeReorderedSubset` subset-isolation, lane-session-order
persistence/pruning), and a real `vite build` succeeds (no new errors/warnings
beyond the pre-existing chunk-size notice).

Files: `apps/desktop/src/app/chat/sidebar/index.tsx`,
`apps/desktop/src/app/chat/sidebar/order.ts` (+ test),
`apps/desktop/src/app/chat/sidebar/reorderable-list.tsx`,
`apps/desktop/src/app/chat/sidebar/sessions-section.tsx`,
`apps/desktop/src/app/chat/sidebar/projects/entered-content.tsx`,
`apps/desktop/src/app/chat/sidebar/projects/workspace-group.tsx`,
`apps/desktop/src/app/chat/sidebar/projects/workspace-groups.ts` (+ test),
`apps/desktop/src/app/chat/sidebar/projects/workspace-header.tsx`,
`apps/desktop/src/store/layout.ts`, new
`apps/desktop/src/store/layout-lane-order.test.ts`.

### Fork-only feature — 2026-07-22 (desktop: resume/reconnect into a running turn now restores the real "thinking" timer and mid-tool-call state)

Discovered uncommitted and undocumented in the working tree (swept up by the
same `hermes update` autostash as the sidebar drag-reorder work above — see
that entry and the "Mandatory workflow" note for why this keeps happening).
Traced, tested, and committed here rather than discarded, since it's
complete, passing, and unrelated in cause to the sidebar bug.

Two compounding gaps in resuming/reconnecting into an already-running
session:
1. `setSessionStartedAt(Date.now())` was called unconditionally on every
   session switch/resume in `use-session-actions/index.ts`, so the
   statusbar's session-age timer reset to ~0 on every tab switch instead of
   continuing from the session's real creation time.
2. The backend's inflight-turn projection (`_inflight_snapshot` in
   `tui_gateway/server.py`) never carried the turn's real start time or
   whether a tool call was currently open. A resume/reconnect landing
   mid-turn re-stamped "now" as the turn start (resetting the live "thinking"
   timer) and, if the turn was mid-tool-call, showed a bare "thinking" bubble
   with no indication a tool was running until it completed.

**Fix:** backend — `_on_tool_start`/`_on_tool_complete` now track a session's
currently-open tool call in a new `open_tool_calls` dict (seeded alongside
the existing `tool_started_at` at every session-init site);
`_inflight_snapshot` includes the inflight turn's real `started_at` and, via
new `_open_tool_call_snapshot`, the open tool call's name/args/id/start time.
Frontend — `SessionResumeResponse.inflight` gained `started_at` and an
optional `tool` field (`types/hermes.ts`); `use-session-actions` prefers the
session's stored `started_at` over `Date.now()` for the age timer, and
prefers the backend's inflight `started_at` over the locally-tracked value
for the turn timer; `appendLiveSessionProjection`
(`use-session-actions/utils.ts`) now also projects the open tool call as a
pending `tool-call` message part via a new `pendingToolCallPart()` helper in
`chat-messages.ts`, so mid-tool-call resumes render the tool's pending row
instead of a bare bubble.

Also included in this batch: a small, unrelated UI affordance — the composer
status stack's "todo" group gained a `resizeId` (`composer-status-todo`) so
`StatusSection` now supports an optional drag-to-resize height handle
(persisted through the existing pane-height-override store), rather than
always sizing to content.

Verified: `tests/test_tui_gateway_server.py -k "inflight_snapshot or
open_tool_call"` (5/5 pass); `tsc --noEmit` clean; desktop vitest —
`use-session-actions.test.tsx` (31/31), `use-session-actions/utils.test.ts`
(27/27), `chat-messages.test.ts` (34/34) all pass. No test file exists yet for
`status-section.tsx`'s new resize behavior — untested by an automated test,
manual verification only.

Files: `tui_gateway/server.py` (+ `tests/test_tui_gateway_server.py`),
`apps/desktop/src/types/hermes.ts`,
`apps/desktop/src/app/session/hooks/use-session-actions/index.ts`,
`apps/desktop/src/app/session/hooks/use-session-actions/utils.ts` (+ test),
`apps/desktop/src/lib/chat-messages.ts`,
`apps/desktop/src/components/chat/status-section.tsx`,
`apps/desktop/src/app/chat/composer/status-stack/index.tsx`.

### Fork-only chore — 2026-07-22 (replace two deprecated/unmaintained transitive npm deps with local shims)

Discovered uncommitted alongside the feature above (same autostash sweep).
`npm install`/lockfile were already regenerated and internally consistent, so
this was finished, not in-progress, work — committed rather than discarded.

Two deprecated packages showed up as install-time warnings from deep
transitive chains with no way to bump the direct dependency to fix them:
- `rimraf@2.6.3`, pulled in only by `temp@0.9.4` (used by
  `electron-winstaller` for Windows Squirrel/NSIS temp-dir cleanup), itself
  pulling in `glob@7` + `inflight`.
- `boolean@3.2.0`, pulled in by `electron`'s `@electron/get` -> `global-agent`
  chain and by `roarr`.

**Fix:** added `local-packages/rimraf-shim/` and `local-packages/boolean-shim/`
— minimal same-API reimplementations (rimraf's callable
`rimraf(path, opts, cb)` / `rimraf.sync` backed by Node's built-in
`fs.rm`/`fs.rmSync`; boolean's `boolean(value)`/`isBooleanable(value)`
truthy-string parsing reimplemented directly, no dependency) — then aliased
both via `package.json`'s `overrides` to `file:local-packages/<name>-shim`,
plus an `electron-builder.app-builder-lib.@electron/asar.glob` override
bumping a separately-flagged transitive `glob` to `^13.0.0`. Regenerated
`package-lock.json` (`npm install`) so the alias is a real, resolved
dependency edge, not just a manifest declaration; `npm ls rimraf boolean`
confirms both resolve to the shims through their real consumers
(`electron-winstaller` -> `temp` -> `rimraf`; `electron` -> `@electron/get` ->
`global-agent`/`roarr` -> `boolean`).

Files: `package.json`, `package-lock.json`, new
`local-packages/rimraf-shim/{index.js,package.json}`, new
`local-packages/boolean-shim/{index.js,package.json}`.

### Fork-only fix — 2026-07-22 (desktop: terminal glyphs render as tofu boxes, missing Nerd Font fallback)

Reported symptom: the embedded xterm.js terminal pane rendered shell-prompt
icons (powerline separators, starship/oh-my-posh glyphs) as tofu/box
placeholders, while the same prompt rendered correctly in kitty on the same
machine.

Root cause: both xterm.js `Terminal` instantiations (the interactive PTY
terminal in `use-terminal-session.ts` and the read-only agent-output terminal
in `use-agent-terminal.ts`) hardcoded a `fontFamily` stack — `'JetBrains
Mono', 'Cascadia Code', 'SF Mono', Menlo, Consolas, monospace` — with no Nerd
Font entry. None of those fonts ship the PUA glyph ranges Nerd Font-aware
prompts assume. kitty renders correctly because its OS-level font-fallback
path picks up an installed Nerd Font (Hack Nerd Font Mono / FiraCode Nerd Font
Mono, both present under `~/Library/Fonts`) automatically when a glyph is
missing from the configured font; Chromium's xterm.js canvas/WebGL renderer
does not do the same automatic PUA fallback.

**Fix:** added `'Hack Nerd Font Mono', 'FiraCode Nerd Font Mono', 'Symbols
Nerd Font Mono'` to the `fontFamily` fallback chain (after `'JetBrains Mono'`,
before the generic system fallbacks) in both terminal instantiation sites.

**Note (2026-07-22, same incident class as the session below):** this fix was
applied once, left uncommitted, and silently wiped by a concurrent Hermes
session's git activity on the same shared working tree before it could be
verified end-to-end. Root-caused via `git fsck --unreachable` (found the
original edit stranded in an orphaned stash-merge commit created by another
session's `hermes update` autostash) and re-applied. Committed immediately
this time — see the "Mandatory workflow" section above, added as a direct
result of this and the sibling incident below happening within the same hour.

Commit: `edf9f4052`.

Files: `apps/desktop/src/app/right-sidebar/terminal/use-terminal-session.ts`,
`apps/desktop/src/app/right-sidebar/terminal/use-agent-terminal.ts`.

### Fork-only fix — 2026-07-22 (desktop: clicking back into a still-running session showed a blank transcript)

Reported symptom: user clicked back into a session that was actively
running/thinking (backend logs, pet-zone indicator, and the sidebar dot all
confirmed it was still working), but the transcript pane rendered completely
blank instead of the in-progress conversation.

Root cause: `syncSessionStateToView()`
(`apps/desktop/src/app/session/hooks/use-session-state-cache.ts`) already had
logic to force a synchronous flush for "critical transitions" (turn finished,
needs input) specifically to avoid a documented sibling bug — the code's own
comment names it: "Electron throttles `requestAnimationFrame` to ~0 while the
window is backgrounded, occluded, or unfocused, so an RAF-deferred flush can be
stranded in `pendingViewStateRef` indefinitely — that's the 'new chat stuck on
Thinking until I refocus' bug." That synchronous-flush gate never covered a
third case: switching onto a session that is ALREADY busy (clicking back into
a running session, resuming a warm-cached busy session). That first paint was
RAF-batched identically to a routine steady-state heartbeat; if the RAF tick
landed on a throttled frame, the transcript stayed blank indefinitely with
nothing else around to force a flush until the turn eventually finished.

**Fix:** added an `isSessionSwitch` check (`sessionId !==
viewSessionIdRef.current`, i.e. this session's transcript isn't the one
currently painted into `$messages`) to the existing critical-transition gate,
so the first paint after a session switch always flushes synchronously exactly
like a terminal transition. Repeat heartbeats to a session already on screen
are unaffected and still get the RAF coalescing that avoids scroll-position
jank.

Verified: `use-session-state-cache.test.tsx` (10/10) and
`use-session-actions.test.tsx` (31/31, covers the warm-cache resume paths that
call this function) both pass; `tsc --noEmit` clean on the modified file.

**Note (2026-07-22, later same day):** this fix was initially applied and
verified but left uncommitted across a turn boundary, and a concurrent Hermes
session's `git reset` on the same working tree silently wiped it — rediscovered
via "this is still happening." Re-applied, re-verified, and this time committed
+ pushed immediately. This incident is what prompted the "Mandatory workflow
for every fork-only change" section at the top of this file.

Commit: `356e55e15`.

Files: `apps/desktop/src/app/session/hooks/use-session-state-cache.ts`.

### Fork-only fix — 2026-07-22 (background skill/memory review racing a live turn: doubled prompt-token accounting + a Ctrl+C-proof lockup)

Reported symptom (live, on an actual multi-hour exo-cluster debugging
session): a single ordinary prompt showed the session's token usage jump by
over 500K tokens in one turn (Δ+570K new, 100% of the 1M context window),
and the session became completely unresponsive — required a hard Ctrl+C from
outside the app; a normal in-app interrupt never recovered it.

Root cause, confirmed against the session's own `agent.log` and `state.db`
`api_calls` rows: `_spawn_background_review()`
(`agent/turn_finalizer.py:595`) forks a **second, complete `AIAgent`** in a
daemon thread after roughly every 10 tool-turns to self-review and update
memory/skills. That fork deliberately shares the **live agent's own
`session_id`** (`agent/background_review.py`, for prompt-cache warmth) and
runs a full independent `run_conversation()` (up to 16 iterations) — but
nothing stopped the user's **next real turn** from starting while that fork
was still mid-conversation. Log evidence showed exactly that: a review fired
at 11:57:56, and the user's very next message at 12:01:06 started a live turn
whose API calls (sequence #90–#96) interleaved in real wall-clock time with
the review fork's own independent call sequence (#1–#14) — both streaming
against Anthropic concurrently under the identical `session_id`. The live
turn's own call immediately afterward logged almost exactly **2.01x** its own
prior call's prompt-token count (560,966 → 1,129,121), and premature context
compression fired off that inflated number. Separately, the review fork —
being a fully independent `AIAgent` — was never added to `_active_children`
(the list `AIAgent.interrupt()` actually walks for real subagent
delegation, `tools/delegate_tool.py`), so a live-turn Ctrl+C had **no
propagation path to it at all**, explaining why the lockup didn't clear on
interrupt.

**Fix**, three files:
1. `agent/agent_init.py` — added `_background_review_agent` /
   `_background_review_lock` tracking state to every `AIAgent` (mirrors the
   existing `_active_children` pattern).
2. `agent/background_review.py` — the review fork now registers itself on
   the parent's `_active_children` right after construction (reusing the
   exact same list/lock `interrupt()` already fans out to for subagents, so
   Ctrl+C now reaches it), and unregisters on every exit path (success,
   the tool-whitelist `finally`, and the outer exception safety-net). All
   registration is defensive (`getattr`/try-except) so an `AIAgent` built
   without going through `agent_init.py`'s setup — test stubs, an older/
   foreign construction path — degrades to "no cross-turn cancellation"
   instead of aborting the whole review.
3. `agent/conversation_loop.py` — at the very start of every
   `run_conversation()` turn, if a prior background review is still
   in-flight (`agent._background_review_agent` is set), it is now
   proactively cancelled via `review_agent.interrupt(...)` before the live
   turn proceeds — fire-and-forget, non-blocking, so it adds no latency.
   This restores the feature's own documented intent
   ("runs AFTER the response is delivered so it never competes with the
   user's task for model attention") which the original code stated but
   never actually enforced against a review that outlives its triggering
   turn.

Verified: `ruff check` clean on all three files; all 60 existing
`background_review`-related tests pass (`tests/run_agent/test_background_review*.py`,
`tests/test_background_review_list_shapes.py`,
`tests/test_background_review_session_isolation.py`); all interrupt-
propagation tests pass (`test_interrupt_propagation.py`,
`test_concurrent_interrupt.py`, `test_real_interrupt_subagent.py`,
`test_cascading_interrupt_6600.py`, `tools/test_interrupt.py`); ran the full
`tests/run_agent/` suite (2162 passed) and confirmed via `git stash` that the
11 remaining failures there are pre-existing on a clean `main` (unrelated
Anthropic-SDK/mock drift in this sandbox), not caused by this change.

Files: `agent/agent_init.py`, `agent/background_review.py`,
`agent/conversation_loop.py`.

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

## Upstream contribution candidates (audit, 2026-07-25; re-sorted 2026-07-26 per external review)

A full read-through of every dated entry in this file, sorted into what's a
legitimate candidate to send upstream as a clean PR vs. what's tied to this
fork's specific environment/preferences and should stay put. Nothing here is
scheduled — this is a documented shortlist for later triage. Per the "Why a
fork" rule above, any of these would be filed as a **separate clean PR built
from upstream's tree**, never a backport of fork code as-is.

**CAVEAT (added 2026-07-26 after an external second-opinion review):** the
original 2026-07-25 version of this audit was written by the same
agent/context that produced the PR #25234 incident it's trying to avoid
repeating — "I described it as clean in my own docs" is not independent
verification, it's exactly the failure mode that got #25234 closed. Every
bucket assignment below is a hypothesis, not a fact, until each item clears
this checklist:

1. **Does upstream have this feature/code path at all?** Ask this *before*
   asking whether the bug is generic. A correctness fix to fork-only
   infrastructure (CC-mimicry OAuth, exo routing, the memory/skill review
   subsystem if it's fork-shaped) is not an upstream bug — it's a non-sequitur
   patch with nothing to land on.
2. **Diff against the actual upstream file**, not against this audit's prose
   description. `git diff upstream/main...HEAD -- <file>` and confirm the
   whole file — not just the target function — hasn't structurally diverged
   in ways that make the fix non-portable.
3. **Grep the isolated diff and its immediate context for fork-only
   symbols** (CC alias names, `anthropic_adapter`, `agent/fork/`, `miku`,
   exo-routing identifiers, `_decorate_xai_entitlement_error`). If any of
   these appear even a few lines outside the intended hunk, stop and extract
   further before filing.
4. **Apply-test the isolated patch against a clean upstream clone**, not
   this fork. If it doesn't apply cleanly, or applies but references
   undefined names, the item is contaminated regardless of how it reads here.

Re-sorted based on that checklist reasoning (not yet independently diffed —
see per-item status):

### Bucket A — legitimate candidates for a clean upstream PR

Only items where the underlying code path plausibly exists in upstream
independent of fork-only infrastructure. Still needs the diff/grep/apply-test
verification above before filing — "plausible" is not "confirmed."

* **MCP orphaned-task / "Event loop is closed" traceback on `/exit`**
  (2026-07-23) — **VERIFIED 2026-07-26, PR-ready.** Full checklist run: (1)
  confirmed upstream has the identical code path (`_MAX_INITIAL_CONNECT_RETRIES`,
  the park-instead-of-return behavior in `run()`, the unguarded
  `await server.start(config)` in `_connect_server()`); (2) the fork's original
  commit (`7b29c89a20`) does NOT apply to upstream — `tools/mcp_tool.py` has
  diverged by ~1740 of ~2500 lines from unrelated fork features layered in
  around the same line numbers (confirmed via `git worktree` + `git apply
  --check` against a real `upstream/main` checkout, which failed); (3)
  hand-isolated just the real fix (the `_connect_server()` try/except) plus 3
  new regression tests, re-diffed and grepped for fork-only symbols (CC alias
  names, `agent/fork/`, `miku`, exo-routing, `sanitize_mcp_name_component`
  vs. upstream's `mcp_prefixed_tool_name`) — zero leaked into the isolated
  diff; (4) applied cleanly to a fresh `upstream-pr/mcp-connect-server-orphan-reap`
  branch (tracking `upstream/main`) and ran the full
  `tests/tools/test_mcp_tool.py` suite there: 219 passed (216 pre-existing +
  3 new). Also caught FORK.md's own description overstating the bug: the
  original "unguarded `cancel()` in a `finally` block" claim (bug 2 of 2) is
  **factually wrong** — diffing the fork's pre-fix file showed all three
  lifecycle-wait helpers already had `cancel()` correctly inside
  `try/except` before the "fix" commit; that half of the commit was a pure
  refactor (deduplicating three near-identical blocks into
  `_cancel_lifecycle_wait_tasks()`), not a bug fix. Only the orphaned-task
  reaping in `_connect_server()` (bug 1) was real. Patch saved at
  `.upstream-candidates/mcp-orphaned-task-fix.diff`; branch ready to push
  and open as a PR.

  **SUBMITTED 2026-07-26 as upstream PR #72054.** Before submitting, got two
  independent second opinions (a Claude subagent + external reference-model
  consult) on whether to submit given upstream issue #60197 already had ~10
  associated PRs. Both initially recommended deferring to the most complete-
  looking one (#62026, "retain parked startup tasks for clean shutdown" —
  a fuller fix via registry-adoption that also addresses revival + task-
  accumulation, not just the traceback). Went to actually test that
  recommendation (git apply --check against upstream/main) rather than take
  it on faith: found `#62026`'s diff has drifted in a way deeper than a
  mechanical rebase — 6/8 hunks apply cleanly (line drift only), but the 2
  touching `_register_discovered_tools_if_needed()` conflict with `main`'s
  own independently-landed fix for the same symptom (`106d1822e`,
  2026-07-19, one week after #62026 went stale), which uses a different
  ownership-tracking mechanism (`_servers.get(name) is self` vs. #62026's
  `_connect_server_claim` contextvar). Reconciling two overlapping-but-
  different design approaches is the PR author's/maintainers' call, not a
  third party's — so did NOT force a rebase of someone else's branch.
  Confirmed `_connect_server()`'s orphan leak itself (this fork's fix's
  actual target) is untouched by `106d1822e` or anything else on `main` —
  the fix and #62026 don't overlap in code, only in the same tracking
  issue. Submitted #72054 scoped explicitly to just the leak (not revival),
  named #62026 directly in the PR body with the `106d1822e` divergence
  finding, and left an informational comment on #62026
  (issuecomment-5084156368) so the reconciliation context isn't lost.
  Confirmed 5 of the other ~10 PRs in the cluster (#60104, #69420, #64114,
  #61466, #71846) propose a fix for a "cancel() outside try" symptom that
  doesn't reproduce from that cause on current `main` — #72054 doesn't
  touch that code path, so it isn't a duplicate of those either.

General bug fixes (CLI/display/desktop) — audit each individually before
filing, do NOT batch-file as one PR. Several of these plausibly touch shared
render/display code that may also serve fork-only overlays (CC identity
blob, Miku pet bubble) even when the fix itself is generic:

* `get_cwidth()` blind to emoji+VS-16 → `display_cwidth()` helper
  (2026-07-24) — **CHECKED 2026-07-26, DO NOT FILE.** The reported symptom
  (spinner-timer digit corruption from tool emoji like `⚙️`) cannot reproduce
  on upstream at all: diffed `agent/display.py` against `upstream/main` and
  found upstream's `KawaiiSpinner` redraw/pad math uses plain `len(line)`,
  not `get_cwidth()` — it never had the VS-16 blind spot to begin with.
  `KawaiiSpinner._display_width`, `_panel_cwidth`, and `_panel_ljust` (3 of
  the 4 sites the fork's fix touches) don't exist upstream at all; they're
  artifacts of an earlier fork-only spinner refactor (`0a32275ff0`) that
  introduced `get_cwidth` usage in the first place. Only `cli.py`'s
  `_status_bar_display_width`/`_trim_status_bar_text`/
  `_estimate_tui_input_height` genuinely exist upstream with the same
  `get_cwidth` VS-16 gap — but traced their actual call sites and confirmed
  none of them ever measure a tool-progress emoji (those print via a
  separate `_cprint()` path the cwidth functions don't touch);
  `_estimate_tui_input_height` only measures user-*typed* input text, so the
  gap there is real but triggers only if a user literally types a VS-16
  emoji into their own prompt — a much narrower edge case with no actual
  observed upstream bug report behind it. Per external review: submitting
  this would lead with a bug narrative that's fork-fabricated (doesn't
  reproduce as described) even though a real, much smaller correctness gap
  exists underneath — exactly the "solution looking for a problem" pattern
  to route around. Would need an actual reproduced upstream artifact (a
  real height-miscalculation, not just the raw `get_cwidth("⚙️ hello") == 7
  vs true 8` arithmetic, confirmed via a quick script) before this is worth
  a PR, scoped to just the `cli.py` input-height gap with no reference to
  the spinner bug. Not pursued further — low value/evidence relative to
  other candidates.
* Clarify/approval panel wide-glyph padding bug, `ljust()` vs. terminal cell
  width (2026-07-07). **CHECKED 2026-07-26, DO NOT FILE.** Unlike the
  previous two candidates, this one's diff genuinely applies cleanly to
  upstream (0 conflicts, 39/39 tests pass, zero fork-only symbols) — the
  bug and the fix are both fully portable. Did NOT file anyway: found the
  exact matching upstream issue (#20621, open) and an **open PR with a real
  maintainer review** (#20750, from `teknium1`) that already covers a
  strict superset of this fix (5 dialog types incl. model picker, vs. this
  fix's 3; module-level reusable helpers vs. per-closure duplication) and
  has been given a concrete, specific spec by the maintainer for what's
  still missing: (a) its wrapping helper breaks on unspaced CJK sentences
  (`text.split()`-based, doesn't advance past a single unbroken "word"),
  and (b) it misses the `/new`/`/clear` confirmation panel, which has the
  identical bug at a different code location. Checked: this fix's own
  `_wrap_panel_text` (untouched by the original fork commit) has the exact
  same latent flaw — still plain `textwrap.wrap()`, no display-width
  awareness — so submitting it wouldn't even be a strictly-more-correct
  narrower alternative, just a strictly smaller one. A closed earlier PR
  (#20641, same narrow 3-function scope as this fix) self-closed after a
  week of no review, not because it was wrong. Per external review: a
  maintainer engaged with a concrete stated spec is a much stronger signal
  than mere staleness — submitting a fix that satisfies neither of their
  named gaps would read as not having read their review, not as a genuine
  alternative. Not filed. If revisited: the right contribution is fixing
  #20750's two flagged gaps (CJK word-boundary wrapping + `/new`/`/clear`
  panel) directly on that PR or as an explicit "supersedes/builds on
  #20750" submission crediting the original author — not a narrower
  competing patch.
* `/usage` NameError — `cache_read_tokens`/`cache_write_tokens` referenced
  but never defined (2026-07-07). **CHECKED 2026-07-26, DO NOT FILE.**
  Confirmed by direct comparison against the fork's own pre-fix commit
  (`a026c8a74~1`): the crashing `CanonicalUsage(...)`/`estimate_usage_cost()`
  cost-estimation block inside `_show_usage()` is entirely fork-only —
  upstream's current `_show_usage()` has no such call at all, just prints
  raw token counts. The exact function this fix patches doesn't have the
  bug upstream because the surrounding cost-estimation feature it lives in
  doesn't exist there. Not a portable fix; not filed.
* Reasoning-block token estimator quadruple-counting `anthropic_content_blocks`
  thinking text (2026-07-07) — **SUBMITTED 2026-07-26 as upstream PR #72087.**
  Verification found more than expected: `anthropic_content_blocks` IS a real
  upstream concept (confirmed: `_convert_assistant_message` in
  `agent/anthropic_adapter.py` reads it, no underscore, for interleaved-
  thinking replay), but upstream's `_estimate_message_tokens_without_images()`
  checks for `_anthropic_content_blocks` (WITH a leading underscore) — a
  field name that's never actually written anywhere in that context, so the
  exclusion never fires. Also found the fork's own commit message overstated
  its baseline ("mirroring the existing dedup logic already applied to
  `content`") — that content-dedup was itself added by an *earlier, separate,
  fork-only* commit (`7eee5efd3e`, 2026-05-13) that never went upstream
  either; confirmed via `git log -S` that upstream never had any form of this
  dedup. So the portable fix needed to cover MORE ground than the fork's
  isolated diff assumed (both the content-dedup AND the reasoning-field
  dedup, not just the latter layered on an existing former). Reproduced the
  bug live against a real upstream worktree before writing anything: a
  message with one thinking block duplicated across all 5 fields estimated
  at ~4x the correct token count (4053 vs ~1000). Confirmed
  `_estimate_message_tokens_without_images` (unlike its sibling
  `_estimate_message_chars`, which is dead code — zero call sites upstream)
  is the live path, called from `context_compressor.py`/`conversation_loop.py`/
  `turn_context.py`/`context_breakdown.py` via `estimate_messages_tokens_rough()`
  — i.e. this feeds real compaction-trigger decisions, not just a cosmetic
  number. Got a second-opinion consult on how to handle the larger-than-
  expected scope; wrote and tested the combined fix against a clean
  `upstream/main` worktree: 136/136 passed in the estimator's own test file
  (132 pre-existing + 4 new), 302/302 passed across every real caller
  (context_compressor + 5 variants, context_breakdown, turn_context + 1
  variant). Zero fork-only symbols in the isolated diff. Searched
  issues/PRs first (per CONTRIBUTING.md) — genuinely nothing existing,
  unlike the two prior candidates. Patch saved at
  `.upstream-candidates/reasoning-estimator-dedup-fix.diff`.
* Dangling `toolsets` NameError in `delegate_task` after upstream removed the
  model-facing arg (2026-07-07). **CHECKED 2026-07-26, ALREADY FIXED
  UPSTREAM.** Both spots the fork's fix targeted (`tools/delegate_tool.py`
  task_list construction + the per-task `_build_child_agent` call) already
  read `toolsets=None` on current `upstream/main` — matching what the fix
  would have changed them to. Ran `tests/tools/test_delegate.py` against a
  clean `upstream/main` worktree: 159/159 passed (the fork's original commit
  message cited 43 failures on the pre-fix code, which no longer reproduce).
  Upstream must have landed its own equivalent fix independently sometime
  after 2026-07-07. Nothing to file — `implemented_on_main`.
* tool_search sticky activation — flapping mid-conversation corrupts
  tool-call history via `_strip_unknown_tool_blocks` (2026-07-07).
  **CHECKED 2026-07-26, DO NOT FILE.** The fork's own commit message
  (`908ff9f252`) self-disqualifies this: "Fork-only change (tagged # FORK
  throughout) — diverges from upstream's intentionally-stateless-per-call
  design, which exists to avoid a past regression." This is the "intentional
  design, not a gap" pattern — upstream's stateless-per-call tool activation
  was a deliberate prior fix for a different regression (stale session-keyed
  catalog), not an oversight. Not filed; no further verification needed
  given the fix's own author already identified it as intentionally
  diverging from upstream's design.
* Desktop: session/tab drag-to-reorder completely broken, plus a nested
  `DndContext` bug (2026-07-22). **CHECKED 2026-07-26, NEEDS
  HAND-RECONCILIATION.** Confirmed the underlying files DO exist upstream
  (not fork-only) and the feature itself is genuinely shared. But
  `git apply --check` on the isolated fix rejects 4 of 12 touched files —
  real semantic drift (changed import shapes, a new `dndSensors` prop
  threading through multiple components), not just line-offset noise, per
  a `git apply --reject` inspection. Would need real hand-porting onto the
  current file shape, not a quick patch. Deprioritized in favor of items
  that applied cleanly; revisit if time allows.
* Desktop: workspace tab had no close button, and naively adding one would
  have removed the app's structurally-required anchor pane (2026-07-23/24).
  **CHECKED 2026-07-26, NEEDS HAND-RECONCILIATION.** Isolated fix rejects
  on `apps/desktop/src/app/contrib/wiring.tsx` — real conflict, not
  line-drift. Deprioritized alongside the drag-to-reorder item above.
* Desktop: clicking into a still-running session showed a blank transcript —
  an RAF-throttle bug when the window is backgrounded (2026-07-22).
  **SUBMITTED 2026-07-26 as upstream PR #72151.** Applied cleanly to a
  fresh `upstream/main` worktree, 0 conflicts. Wrote 2 new regression
  tests (one stubs `requestAnimationFrame` to never fire, confirming the
  first paint after a session switch still flushes synchronously; the
  other confirms a same-session repeat heartbeat still gets RAF-coalesced)
  — the first fails against the pre-fix code, confirmed via a scripted
  revert. Full `src/app/session` suite: 320 passed, 0 failed. No existing
  issue found.
* Desktop: a queued composer message could be delivered into the wrong,
  currently-viewed session (2026-07-22). **CHECKED 2026-07-26, NEEDS
  HAND-RECONCILIATION.** Isolated fix rejects on
  `apps/desktop/src/app/session/hooks/use-prompt-actions/submit.ts` — real
  conflict, not line-drift. Deprioritized alongside the two drag/close
  items above; all three would need genuine hand-porting effort against
  the desktop app's fast-churning session/composer code, not just a
  rebase.
* Desktop: profile deletion silently reverted after relaunch — zombie
  backend process detection missed `python3 <hermes-shim>` argv shapes
  (2026-07-22). **SUBMITTED 2026-07-26 as upstream PR #72152.** Search-
  first turned up real complexity: closed issue #52279 (same headline
  symptom) was already fixed via 2 merged PRs (#57329 respawn-routing +
  rail-refresh on a DIFFERENT component; #49435 recreation guard in
  `ensure_hermes_home()`). Got two rounds of external consult on whether
  this fix still adds anything real. Verified directly (not assumed): (1)
  `ProfileRail` subscribes reactively to the same `$profiles` atom
  `refreshProfiles()` already updates on same-window delete, so that path
  is NOT the gap this fix closes — the actual remaining gap is
  cross-window/cross-process staleness, confirmed by reading the atom's
  definition (no IPC sync); (2) a zombie backend surviving the argv[0]
  detection gap still holds a bound uvicorn port even with the recreation
  guard in place — confirmed by reading `hermes_cli/web_server.py`, not
  inert as initially worried. Both pieces are genuinely real, distinct
  from what's already merged. Python: 156/156 passed.
* Desktop model picker hid Anthropic despite valid Claude Code credentials
  (2026-07-22). **SUBMITTED 2026-07-26 as upstream PR #72155.**
  **Caution paid off as a genuine check, not just process**: confirmed the
  fix's premise directly by reading `list_authenticated_providers()` in
  `hermes_cli/model_switch.py` — it already has the exact same
  `read_hermes_oauth_credentials()`/`read_claude_code_credentials()`
  fallback pattern this fix extends into the desktop-only filter, citing
  the same #4210 rationale. Confirms this is extending an
  already-established upstream pattern, not inventing new CC-credential
  plumbing. Python: 46/46 in the target test file, 117/117 across
  adjacent credential-detection test files.
* Desktop: terminal glyphs render as tofu boxes — missing Nerd Font fallback
  in the xterm.js `fontFamily` chain (2026-07-22). **SUBMITTED 2026-07-26
  as upstream PR #72153.** Trivial, low-risk font-stack string extension;
  applied cleanly, 0 conflicts. No dedicated unit test to extend (a
  font-family constant isn't meaningfully unit-testable); ran the broader
  `src/app/right-sidebar/terminal` suite instead — 25/25 passed, 0 new
  failures.
* `resedit` replacing deprecated `rcedit`, and the `rimraf`/`boolean`
  local-shim replacements (2026-07-22/24). **CHECKED 2026-07-26, SKIPPED —
  can't verify.** Confirmed `rcedit` is still present unchanged in
  upstream's `apps/desktop/package.json` (not independently fixed), and
  the isolated diff applies cleanly (0 conflicts). But this is Windows-only
  build tooling (PE resource stamping on the built `.exe`) — the original
  fork commit's own verification was "against a real Windows Electron
  40.10.2 PE exe," which isn't reproducible on this macOS machine. Diffing
  clean isn't sufficient verification for a claim like "the icon/version
  stamping still works" when the actual behavior can't be executed and
  observed. Skipped rather than submit an unverified Windows-only build
  change — user confirmed this call directly.
* Exit-summary/cleanup ordering + cost-accounting fixes: exit watchdog
  swallowing the cost report, memory-confirm cost not counted, background
  curator cost not counted (2026-07-14, three related entries).
  **SPLIT 2026-07-26.** The "watchdog swallows cost report" piece is real,
  portable, shared code — **SUBMITTED as upstream PR #72164**, split out
  from the other two entirely fork-only pieces:
  - Verified `_run_cleanup()`-before-`_print_exit_summary()` ordering and
    the 30s watchdog default both genuinely exist unchanged in upstream's
    current `cli.py`, alongside the exact same "`hermes --tui` alive ~47
    min at 4% CPU" signal-arming fix (`c66891db08`) that IS already
    merged — so only the ordering/timeout piece was still missing, not
    the whole commit chain.
  - The other two entries (memory-confirm cost tracking, curator cost
    tracking) are genuinely fork-only: `hermes_cli/memory_confirm.py` and
    `tools/memory_extraction/extractor.py` (the files they touch) don't
    exist in upstream at all — different memory-review architecture.
    Confirmed "cost accounting" here is specifically the fork's
    Phase-2-memory-confirm/curator cost ledgers, not upstream's general
    cost-tracking — not filed.
  - Extracted a shared `_finish_interactive_exit()` helper as part of the
    fix (both call sites had to stop duplicating the ordering) — this also
    made the invariant unit-testable and replaced the original fix's
    source-text-regex test (a banned antipattern per this file's own
    testing rules) with a real behavioral one. That extraction caught a
    genuine second bug the original fork commit missed: a
    separately-hardcoded 30s default in
    `_arm_exit_watchdog_on_shutdown_signal` that would have left the
    signal-armed backstop's "2x headroom" computed from a stale base.
  - No existing issue found. 10/10 in the rewritten + updated test files,
    51/51 across adjacent exit/cleanup tests, 0 regressions.

### Post-submission Fable review pass (2026-07-26)

Ran all 7 filed PRs (#72054, #72087, #72151, #72152, #72153, #72155,
#72164) back through external review with the real diffs, not summaries.
Two came back with real, actionable findings; both were fixed and pushed
as follow-up commits to the same PRs (not new PRs) before this note:

- **#72054 (MCP orphan reap):** `except Exception: pass` around the
  cleanup `shutdown()` call silently swallowed any failure in the reap
  itself, with zero trace. Fixed: added a `logger.debug` line, matching
  existing precedent elsewhere in the file. Also flagged: none of the 3
  original tests exercised the REAL `shutdown()`/
  `_wait_for_reconnect_or_shutdown()` machinery (all faked `shutdown()`
  itself), so a hypothetical regression to polling/sleeping in the park
  loop wouldn't have been caught. Verified directly by reading the real
  implementation first — confirmed it's `asyncio.wait()` on real events,
  not a blind sleep, so the theoretical stall risk doesn't apply — but
  added a 4th test exercising the real machinery end-to-end anyway
  (bounded by a tight `asyncio.wait_for(..., timeout=2.0)` so a future
  regression to polling would fail the test, not just run slow). 220/220
  passed after the fix (up from 219).
- **#72152 (profile deletion hygiene):** the new argv[1] script-name
  check reused the pre-existing loose `startswith("hermes")` pattern from
  the exe_name check above it, but argv[1] can be ANY user-invoked python
  script path when argv[0] is a bare interpreter (unlike a
  directly-resolved executable name, where a false match is rare) — a
  user's own script named e.g. `hermes-notes.py` would be misidentified
  as the console-script shim and become killable by profile delete.
  Fixed: match against the actual known entry points
  (`pyproject.toml [project.scripts]`: `hermes`, `hermes-agent`,
  `hermes-acp`) instead of a bare prefix. Added a regression test that
  fails against the pre-fix loose match (confirmed via scripted revert)
  plus one confirming the other 2 real entry points still match. 158/158
  passed after the fix (up from 156).

Two more findings were raised but resolved as **non-issues after checking
the real code** (not accepted at face value, not dismissed either):

- **#72087 (reasoning-estimator):** flagged risk that `content` could
  diverge from `anthropic_content_blocks` after compression mutates a
  message (found one real mutation site,
  `context_compressor.py`'s orphaned-tool-call stripping, that rewrites
  `content` without touching `anthropic_content_blocks`). Verified this
  is architecturally safe: `anthropic_content_blocks` — not `content` —
  is what `_convert_assistant_message`'s replay path actually sends to
  the API whenever blocks are present, regardless of what `content` gets
  rewritten to, so counting the blocks is still correct for token
  estimation. Also flagged a missing image-strip on the blocks branch;
  verified `anthropic_content_blocks` can structurally never contain
  image-type blocks (it's populated only from the model's own
  `thinking`/`redacted_thinking`/`tool_use` response blocks, per
  `agent/transports/anthropic.py` — Anthropic's API never returns
  `image` type in assistant-turn content). No code change needed.
- **#72151 (RAF-throttle blank transcript):** flagged a possible
  ordering bug (does `viewSessionIdRef` update before or after the new
  `isSessionSwitch` check reads it?) and a scroll-jank risk on warm-cached
  session resume. Verified by reading `flushPendingViewState()` directly:
  the ref update (`viewSessionIdRef.current = pending.sessionId`) runs
  synchronously inside the same critical-transition flush the check
  gates, not deferred into an RAF callback — ordering is correct. The
  scroll-jank concern is more architectural/speculative (no scroll-restore
  mechanism found anywhere in this hook or its callers to race against);
  noted but not acted on absent a concrete repro.

### Bucket B — needs de-forking first, or unverified/likely-contaminated (do NOT file as-is)

Moved here from the original Bucket A after re-review: FORK.md's own
description already admits these are tied to fork-only infrastructure, or
the infrastructure they depend on doesn't obviously exist upstream in the
same shape. "Light de-forking" is the same euphemism that hid 28K LOC in
PR #25234 — treat every item here as needing full extraction work, not a
quick rename, before it's upstream-shaped.

* **Claude Code Keychain write-back on OAuth refresh** (2026-07-14) — this is
  a bugfix *inside* the CC-mimicry OAuth/keychain system, which is on this
  file's own "must never be sent upstream" list (`anthropic_adapter.py` CC
  alias translation, metadata identity blob, billing header, SSE observer).
  Upstream has no Hermes-mimics-Claude-Code keychain integration for this bug
  to exist in — there is likely nothing to patch upstream. Do not file unless
  you first confirm upstream independently has an equivalent OAuth-refresh
  keychain-write code path with the same bug (unlikely).
* **Bearer clients leak `ANTHROPIC_API_KEY` as `x-api-key`** (2026-07-14) —
  only genuinely portable if upstream's `build_anthropic_client` has the same
  bearer-token/API-key dual-auth branching. If that branching exists *because*
  the fork added OAuth-bearer support for CC mimicry, this "fix" has nothing
  to attach to upstream. Diff `build_anthropic_client` against
  `upstream/main` before trusting this is a general SDK bug.
* **`_sanitize_replay_block` fail-closed → fail-open** (2026-07-24) — FORK.md's
  own description says this is "part of the invisible token/cost doubling
  entry, tied to the fork's native-search feature" while separately claiming
  it "stands on its own." That contradiction is the tell. Verify (a) whether
  upstream's Anthropic adapter has `server_tool_use`/`web_search_tool_result`
  replay logic at all, or whether native search is itself fork-only, and
  (b) whether the isolated fail-open diff still references fork-only
  cost-accounting/native-search state before assuming it's clean.
* **Background skill/memory review racing a live turn** (2026-07-22) — likely
  touches the same memory/skill-review subsystem already listed below as
  "Hot-tier memory audit — needs de-forking first." If that subsystem is
  fork infrastructure with no upstream equivalent, this concurrency bug has
  no target upstream. Verify whether upstream has any background-review-vs-
  live-turn concurrency mechanism at all before filing.
* `agent.pin_anthropic_token` (2026-07-25) — solves a real macOS
  Keychain-sharing problem (interactive `claude` login and a dedicated
  setup-token read from the same Keychain slot), but the motivating scenario
  is niche (a dedicated long-lived setup-token separate from daily-driver
  login). Could genuinely help others with the same dual-credential setup;
  would need generalized framing in the config docs, not fork-specific
  wording, before filing.
* Pet-bubble's stale-while-revalidate + monotonic-sequence-guard cache
  pattern (2026-07-24, "sluggish/draggy cadence" entry) — the caching
  *pattern* (speak-cached-now, fetch-in-background, sequence-guarded commit
  so an older slow fetch can't clobber a newer one) is a reusable UI
  technique independent of the Miku voice feature it was built for. Would
  need extraction into a generic helper before it's upstream-shaped.
* **`consult` tool** (second-opinion from a reference model, 2026-07-07) —
  general-purpose mechanism, but explicitly flagged by external review as
  needing "light de-forking" — do not batch this with genuine bugfix PRs.
  Extract fully (config keys, no exo-specific defaults) before filing as its
  own PR.
* **Opt-in toolset deferral via `tool_search`** (`defer_toolsets`/
  `defer_tools`/`keep_eager_tools`, 2026-06-22) — pure config, plausibly
  generalizable, but same rule: verify no exo-specific default sneaks through
  before filing, and file it standalone.
* **`trafilatura` free `web_extract` backend** (2026-07-18) — closes a real
  gap for users without a paid extract-capable provider key. Standalone
  candidate once verified it doesn't import/depend on fork-only routing.
* **Hot-tier memory audit** (stale-path detection + optional LLM
  classification, 2026-07-14, three entries) — generalizable in principle,
  but this is the same memory subsystem the background-review race item
  above may depend on; verify upstream's memory architecture is similar
  enough for this to be a portable feature, not a fork-only rewrite.
* **Delegate auto-route to model tier + persona** (2026-07-07) — useful
  default-routing behavior, but verify it doesn't hardcode or default toward
  the fork's exo/Anthropic two-provider routing preference before filing.

### Bucket C — personal/fork-only, do not upstream

* Everything in the Miku/Vocaloid/RVC voice pipeline (voice synthesis
  pipeline, phrasing/persona passes, warm-daemon latency work) — cosmetic
  character choice specific to this user's pet mascot pick.
* exo-cluster-specific auxiliary routing: exo-scoped delegation, the
  provider-first `auxiliary` config schema, the `fallback_models` map — built
  around this fork's specific two-provider (exo/Anthropic) routing
  preference, not a general-purpose design upstream would want verbatim.
* MCP no-`mcp_`-prefix naming convention — a deliberate, permanent fork
  divergence from upstream's `mcp__` normalization (see "Conflict guidance by
  file" below), not a bug to fix.
* Anything in `agent/fork/` and anything listed under "must never be sent
  upstream" above — the PR #25234 lesson applies without exception.

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

### Fork-only fix — 2026-07-22 (pet: roam froze entirely during real agent activity)

**Symptom:** the pet would only wander while the agent was fully idle;
the moment a turn started (talking, running a tool, thinking) it froze
in place mid-frame for the whole duration of the activity, only resuming
its walk once the agent went idle again.

**Root cause:** `usePetRoam`'s `enabled` flag was gated on `$petAtRest`
(`derivePetState(...) === 'idle'`) — the narrowest possible reading, true
only at plain idle. Any activity at all (`run`/`review`/`waiting`/etc.)
flipped it false, which `usePetRoam`'s cleanup immediately resolves to
`$petMotion.set(null)` — killing the stride instantly rather than letting
it finish naturally. This was overly conservative: ordinary work
(`run`/`review`) already renders with the SAME running-leg sprite rows
the walk animation itself uses, so a stride mid-tool-call is visually
indistinguishable from a stride at idle — freezing for it bought nothing
and just made the pet look stuck/broken during the exact moments
(thinking, running tools) where continued pacing reads as most alive.

**Fix:** added `$petCanRoam` to `store/pet.ts` —
`state === 'idle' || state === 'run' || state === 'review'` — and wired
`usePetRoam`'s `enabled` to it instead of `$petAtRest`. `$petAtRest`
itself is unchanged and still used (correctly) by the idle-fidget effect,
which SHOULD stay idle-only — a fidget wave/jump mid-tool-call would be
wrong, unlike ordinary pacing. The wander now only pauses for the states
that render a DISTINCT, stationary pose meant to grab attention —
`failed`, `waiting`, `wave`, `jump` — where `usePetRoam` disabling
correctly zeroes `$petRoamDir`, which is what lets `PetSprite`'s
`rowOverride` stand down and the real pose show through
(`roamWalkRow(0, ...)` returns no row override).

**Files:** `apps/desktop/src/store/pet.ts` (new `$petCanRoam`),
`apps/desktop/src/components/pet/floating-pet.tsx` (roam-enable gate).

**Merge note:** fork-only files, no upstream equivalent — no conflict
risk.

### Fork-only fix — 2026-07-22 (pet zone: resizing the pane didn't adjust the walk)

**Symptom:** live-dragging the pet-zone layout track to shrink/grow the
pane had no visible effect on the wander — the pet kept strolling to
targets sized for the OLD dimensions, sometimes walking right past the
new clipped edge after a shrink.

**Root cause:** `snapshotContainerLedges()` (the zone's ledge geometry)
only gets re-measured at the START of a decision beat in
`use-pet-roam.ts`'s `planNext()` — i.e. when a pause ends or a walk
arrives. A live pane resize is invisible to the loop for however long
the CURRENT beat has left to run: a pause can dwell up to `PAUSE_DWELL`'s
~13s ceiling, and an in-flight walk keeps striding toward a target
already computed against stale bounds. The separate React-level reclamp
effect that keeps the STATIONARY pet on-screen had the same class of gap:
it only listened for `window.resize`, but the zone pane is a layout-tree
grid track the user drags — a purely in-app CSS/layout event that never
fires `window.resize` at all, so a standing-still pet could stay clamped
to stale bounds indefinitely.

**Fix:** `use-pet-roam.ts` now attaches a `ResizeObserver` directly to
`zoneContainer` when zone mode is active. On any resize it (1) clamps the
pet's current x into the fresh bounds immediately, (2) if the loop is in
a controllable phase (`pause` or `walk`) forces an immediate `planNext()`
re-plan so the next stroll/loaf targets the real, current span rather
than finishing out a stale one; in-flight `fall`/`jump` transitions are
left alone (re-targeting mid-arc looks glitchy, and they finish fast /
the next pause re-measures anyway). `floating-pet.tsx`'s separate
stationary-pet reclamp effect gained the same `ResizeObserver` on
`zoneContainer` alongside its existing `window.resize` listener, so a
standing pet reclamps to a shrunk zone the instant you finish dragging
the track, not on some unrelated later window resize.

**Files:** `apps/desktop/src/components/pet/use-pet-roam.ts`,
`apps/desktop/src/components/pet/floating-pet.tsx`.

**Merge note:** fork-only files, no upstream equivalent — no conflict
risk.

**Merge note:** purely additive — one new function, one filter line in the
existing Tool Availability loop, three new tests. No upstream equivalent
(upstream's doctor doesn't have this section at all in the same form), so
should apply cleanly on future syncs.

### Fork-only fix — 2026-07-23 (pet: jump hop looked abrupt/teleport-y)

**Symptom:** user reported the desktop pet's roam animation is "very
abrupt," particularly the jump between ledges (walking traced back to
the same root once investigated — see below).

**Investigation:** decoded the active pet's (`hatsune-miku`)
`spritesheet.webp` directly (1536x1872, 8 cols x 9 rows) with
`agent.pet.render.state_frame_counts` / a manual per-cell alpha scan.
Frame budget was NOT the problem: `running-left`/`running-right` are
fully populated (8/8 real columns), `jumping` has 5/8 real frames —
neither is starved. The bug was a timing mismatch between the visual
frame cadence and the physical motion:

- `PetSprite` (`pet-sprite.tsx`) steps a state's real frame count evenly
  across `loopMs` (`stepMs = loopMs / realFrameCount`) — for jump's 5
  frames at the default `loopMs=1100`, that's ~220ms/frame, so the pose
  needs ~1100ms to read as an actual spring.
- `use-pet-roam.ts`'s hop arc used a flat `JUMP_DUR_MS = 460` completely
  decoupled from `loopMs`/frame count. The position tween (physics)
  finished in 460ms while only ~2 of 5 jump frames had time to display,
  and landing immediately calls `beginPause()` → `signal(null, 0)`,
  cutting the pose back to idle — reads as a teleport/flash, not a hop.
  This affects every installed pet, not just Miku (the constant never
  scaled with `loopMs` since the roam feature was introduced).

Walking's abruptness is a separate, smaller issue not fixed here: `PetSprite`
resets `frame = 0` the instant the active row changes with no
blend/settle frame — a design/UX polish item, not a config or missing-asset
bug.

**Fix:** added `jumpDurationMs(loopMs)` to `roam-behavior.ts` — the hop
duration is now `loopMs * 0.75`, clamped to `[260ms, 900ms]`, so the jump
pose gets enough wall-clock time to play its real frames before the loop
lands and cuts back to idle. `use-pet-roam.ts` computes this once per
roam-effect setup (alongside the existing `walkSpeedPxS` pacing) and uses
it in place of the old flat constant.

**Files:** `apps/desktop/src/components/pet/roam-behavior.ts` (new
`jumpDurationMs` + 3 tests in `roam-behavior.test.ts`),
`apps/desktop/src/components/pet/use-pet-roam.ts` (removed
`JUMP_DUR_MS`, wired `jumpDurationMs(loopMs)` in its place).

**Merge note:** fork-only files, no upstream equivalent — no conflict risk.

### Fork-only fix — 2026-07-23 (desktop: queued prompt + attachments could deliver into a different, currently-active session)

**Symptom (user report):** typed a message and pasted screenshots in one
session (hermes-agent project), pressed Enter (queued because the session
was busy), then browsed other sessions to check their progress. The queued
message and its images were later sent into a *different*, unrelated
session (homelab project) instead of the one they were queued in.

**Root cause:** `use-prompt-actions/submit.ts` seeded the submit pipeline's
target runtime id with:

```ts
let sessionId: null | string = options?.sessionId ?? activeSessionIdRef.current
```

Both queue-drain call sites (the foreground composer's
`use-composer-queue.ts` and the offscreen `use-background-queue-drain.ts`)
always pass `sessionId` explicitly for the session being drained — and per
its own doc comment, that field exists specifically so *"a
backgrounded/source session cannot be replaced by the current foreground
session between enqueue and drain."* When the validated
`getRuntimeIdForStoredSession` getter (added in `072395dad`, 2026-07-21, for
the sibling "background queue drain could deliver a queued message into a
different, currently-live session" bug) correctly rejects a stale/reaped
runtime mapping, it returns `sessionId: null` explicitly — meaning "resolve
this by stored id instead." `??` cannot distinguish an explicit `null` from
"the key was never passed," so it silently fell through to
`activeSessionIdRef.current` — whichever session the user currently has
foregrounded. `submit.ts` already had the *correct* handling 30 lines down
(`!sessionId && targetStoredSessionId` resumes the specific target stored
session by id) — it was just unreachable for these calls because `sessionId`
never landed on `null`.

This is the same bug *class* as `072395dad`, but on the submit-pipeline side
of the seam rather than the id-resolution side: that fix made the *getter*
correctly say "I don't know," but nothing downstream distinguished "I don't
know" from "no opinion, use the default."

**Fix:** `submit.ts` now checks whether `sessionId` was an explicit key on
`options` (`'sessionId' in options`) rather than using `??`. When explicitly
provided (any queue drain), a `null` value is honored as "unresolved — fall
through to the stored-session resume path," never coerced to the active
session. Only a plain foreground submit (no `sessionId` key on `options` at
all) still defaults to `activeSessionIdRef.current`.

Added a regression test,
`never substitutes the foreground active session when a queue drain
explicitly passes sessionId: null`, in `use-prompt-actions/index.test.tsx`
(`usePromptActions sleep/wake session recovery` describe block) — verified
it fails against the pre-fix code (asserts `session.resume` against the
background stored id fires before `prompt.submit`, and that the foreground's
`activeSessionIdRef` is untouched).

**Files:** `apps/desktop/src/app/session/hooks/use-prompt-actions/submit.ts`,
`apps/desktop/src/app/session/hooks/use-prompt-actions/index.test.tsx` (new
test).

**Merge note:** small, local change to fork-diverged logic already patched
once before (`072395dad`) — low conflict risk, but if upstream touches this
`sessionId` seeding line, re-verify the explicit-null vs. absent-key
distinction survives the merge.

### Fork-only fix — 2026-07-23 (pet: jump still didn't lift off the ground)

**Symptom:** after the previous jump-timing fix, the user reported the jump
animation still isn't visibly lifting the pet off the ground (screenshot:
Miku sitting still in the desktop app's Pet Zone).

**Root cause:** the previous fix (`jumpDurationMs`) only paces the ROAM
LOOP's ledge-to-ledge hop — a real `top`/`left` DOM move driven by
`use-pet-roam.ts`'s platformer state machine. But the `jump` pose is also
entered as a purely STATIONARY reaction with no roam physics behind it at
all: idle fidget (`floating-pet.tsx`'s fidget effect writes `jump` straight
onto `$petMotion`), click-to-pet (`burstVibeHearts` → `flashPetActivity({
celebrate: true })`), and turn-end celebrate all just swap `PetSprite`'s
canvas frames on a FIXED canvas position — there was never any vertical
motion for that path, only a frame-row change. Worse: the Pet Zone (what the
screenshot shows) has exactly one ledge (`snapshotContainerLedges` returns a
single floor), so `chooseMove`'s `canHop` is always false there — the roam
hop can never fire in the zone, meaning every jump the user sees in that
surface is the stationary case. Confirmed via the same spritesheet decode as
before: `jumping`'s 5 real frames were fine: the bug was 0px of vertical
travel, not missing/short frames.

**Fix:**
- `store/pet.ts`: added `$petRoamAirborne` — true only while the roam loop
  is actually mid-hop/fall (set in `use-pet-roam.ts`'s `beginVertical`,
  cleared in every settle/pause/drag-yield/cleanup path). This is the signal
  that distinguishes "roam is already moving me" from "the pose alone says
  jump."
- `roam-behavior.ts`: added `jumpBobHeightPx(petH)` — hop height as a
  fraction (0.28) of the pet's on-screen height, clamped `[10px, 36px]`, so
  the bob scales with `display.pet.scale` instead of a flat guess.
- `styles.css`: new `.pet-jump-bob` class + `@keyframes pet-jump-bob` — a
  CSS `translateY` bob (0 → up → slight settle → 0) driven by
  `--pet-jump-height`/`--pet-jump-ms` custom properties, following the
  existing `pet-egg-wobble` convention; respects
  `prefers-reduced-motion: reduce`.
- `pet-sprite.tsx`: wrapped the canvas in a `<div ref={wrapRef}>`, and added
  a `$petState.listen` effect that re-triggers `.pet-jump-bob` (with a
  reflow-forcing class remove/re-add so back-to-back jump beats restart the
  animation) whenever the pose transitions INTO `jump` while
  `$petRoamAirborne` is false — i.e. exactly the stationary-reaction case.
  While the roam loop IS airborne, this is a no-op, so the two vertical
  motions (DOM-level ledge hop vs. CSS-level stationary bob) never fight.
  Shared by all three `PetSprite` consumers (in-window `FloatingPet`, the
  pop-out overlay, and the generate-flow hatch preview) with zero
  prop/API changes — the pop-out overlay in particular never had ANY
  vertical jump motion before this, since it has no roam loop at all.

**Verification:** `jumpBobHeightPx`/`jumpDurationMs` unit tests (17 total in
`roam-behavior.test.ts`), full desktop `tsc --noEmit` clean, `eslint` clean
on all touched files, full pet test suite green (31/31). The 104
pre-existing `localStorage.clear` failures in unrelated files (session
preview routing, pane-shell tree tests) reproduce identically on unmodified
`main` — confirmed none are pet-related.

**Files:** `apps/desktop/src/store/pet.ts` (new `$petRoamAirborne`),
`apps/desktop/src/components/pet/use-pet-roam.ts` (sets/clears it),
`apps/desktop/src/components/pet/roam-behavior.ts` (new
`jumpBobHeightPx` + 3 tests), `apps/desktop/src/components/pet/pet-sprite.tsx`
(bob-trigger effect + wrapper div), `apps/desktop/src/styles.css` (new
`.pet-jump-bob` keyframes).

**Merge note:** fork-only files, no upstream equivalent — no conflict risk.

### Fork-only fix — 2026-07-23 (pet: jump bob too high + didn't replay on repeat clicks)

**Symptom (user report, follow-up to the previous fix):** the new jump bob
was jumping "a little too high," and clicking the pet repeatedly in quick
succession didn't replay the bob on the later clicks.

**Root cause 1 (too high):** `jumpBobHeightPx`'s fraction/ceiling
(`0.28`/`36px`) was tuned before seeing it live at the user's configured
`display.pet.scale: 0.5` — 28% of a ~104px-tall pet's body height reads as
an oversized pogo, not a light hop.

**Root cause 2 (doesn't replay on repeat clicks):** the bob-trigger effect in
`pet-sprite.tsx` only fired on a `$petState` *transition into* `'jump'`. But
`$petState` is a `computed` nanostores atom, and `computed`'s internal
`$computed.set(value)` — like the base `atom.set()` — only notifies
listeners when the VALUE actually changes (`node_modules/nanostores/atom/
index.js`: `if (oldValue !== newValue) { ...notify... }`). Clicking the pet
again (`burstVibeHearts` → `flashPetActivity({celebrate: true})`) while
still inside the first click's 1.6s decay window updates the underlying
`$petActivity` object, but `derivePetState()` still resolves to the same
string `'jump'` — so `$petState.set('jump')` on an already-`'jump'` value is
a silent no-op, no listener fires, and the bob effect (keyed on a `!==
'jump'` → `'jump'` transition) never replays. Same root cause class as
`STATE_ALIASES`/priority races documented elsewhere in this file: derived
state collapsing distinct *events* into one *value* loses the "did something
happen again" signal.

**Fix:**
- `roam-behavior.ts`: `JUMP_BOB_HEIGHT_FRACTION` 0.28→0.15, floor 10→6px,
  ceiling 36→24px. Updated the 2 boundary tests in `roam-behavior.test.ts`
  and the CSS fallback default in `styles.css` (22px→14px) to match.
- `store/pet.ts`: added `$petJumpBeat` — a monotonic nonce atom bumped by
  `flashPetActivity` on every `celebrate: true` call, independent of whether
  `$petState`'s VALUE actually changes. This is the general fix for "replay a
  one-shot effect on every event, not just the first value change" — the
  right primitive when a derived atom's value-equality check would otherwise
  swallow a repeat.
- `pet-sprite.tsx`: the bob effect now listens to BOTH `$petState` (catches
  the first transition into `jump`, including the roam-airborne guard) AND
  `$petJumpBeat` (catches every repeat celebrate while already in the `jump`
  pose). Both paths funnel through one `playBob()` helper so the reflow-
  forcing restart logic isn't duplicated.
- `pet-overlay-app.tsx`: the pop-out overlay's `'vibe'` reaction handler
  (received over IPC from the main renderer, since the overlay has no local
  `flashPetActivity` call of its own) now also calls `triggerPetJumpBeat()`
  so repeat click-to-pet reactions replay there too.
- Added 2 new `pet.test.ts` cases asserting `$petJumpBeat` bumps on every
  celebrate call (even while already celebrating) and does NOT bump for
  non-celebrate beats (error/justCompleted), so this contract can't silently
  regress.

**Verification:** full desktop `tsc --noEmit` clean, `eslint` clean on all
touched files, pet test suite 33/33 (10 in `pet.test.ts`, up from 8; 17 in
`roam-behavior.test.ts`), full desktop suite unchanged at 235 files / 2093
tests passing (the pre-existing 104 `localStorage.clear` failures in
unrelated files reproduce identically on unmodified `main`).

**Files:** `apps/desktop/src/components/pet/roam-behavior.ts` (bob height
constants), `apps/desktop/src/components/pet/roam-behavior.test.ts` (updated
boundary tests), `apps/desktop/src/styles.css` (fallback default),
`apps/desktop/src/store/pet.ts` (new `$petJumpBeat` +
`triggerPetJumpBeat`), `apps/desktop/src/store/pet.test.ts` (2 new tests),
`apps/desktop/src/components/pet/pet-sprite.tsx` (dual-listener bob trigger),
`apps/desktop/src/app/pet-overlay/pet-overlay-app.tsx` (bumps the beat on a
mirrored vibe reaction).

**Merge note:** fork-only files, no upstream equivalent — no conflict risk.

### Fork-only fix — 2026-07-23 (pet: running animation looked janky on Retina)

**Symptom:** user reported the running animation still looks "a bit jank"
and asked whether it's a missing-sprite-frames problem.

**Investigation, ruled out first:** decoded the active pet's spritesheet
directly and diffed adjacent frame pairs pixel-by-pixel. The roaming
directional walk rows (`running-left`/`running-right`) have 8 full real
frames each — not starved. The stationary in-place `running` row (shown
during ordinary busy/tool-running when the pet ISN'T roaming) genuinely only
has 6 real frames of 8 columns — a real asset limit for that one row, but not
the dominant complaint since roaming is the richer, more commonly-seen path
and was reported as janky too.

**Root cause (verified against the user's actual hardware — a Retina
3024×1964 display):** `pet-sprite.tsx`'s canvas had no HiDPI-aware backing
store. Every OTHER pixel-art canvas in this same directory
(`pixel-egg-sprite.tsx`, `pet-star-shower.tsx`) already sizes its canvas to
`Math.min(devicePixelRatio, 3) * cssSize` — but the actual animated pet
sprite renderer, the highest-traffic canvas of the three, was missing this
fix entirely and had no `imageRendering: 'pixelated'` CSS either (also
present on `pet-thumb.tsx`'s `<img>` and the egg sprite, absent here). On a
2x-density display, drawing into a backing store sized to CSS pixels forces
the browser to upscale the whole canvas element 2x to fill its box — a
SECOND resampling pass stacked on top of the `drawImage` scale-down already
happening from the 192×208 source frame. That second pass's sub-pixel
rounding shifts slightly differently frame to frame, reading as a subtle
shimmer/wobble that's most visible on a fast, edge-heavy cycle like running
(short ~183ms/frame cadence, limb edges crossing many pixels each frame).

**A second hypothesis investigated and DISPROVEN before touching code:**
suspected the RAF step loop's `lastStep = now` (rather than `lastStep +=
stepMs`) discards overshoot from a late tick and could read as an uneven
cadence. Wrote a numeric simulation (`execute_code`, jittered RAF ticks +
occasional main-thread hitches) comparing the existing `if`-based step
against a `while`-based catch-up variant before writing any product code.
Result: the EXISTING code never skips a frame index across 2000+ simulated
ticks (frame sequence stayed perfectly 0→1→2→…→0 with zero non-sequential
jumps); the catch-up variant I was about to ship introduced real skips
(13 in one run — e.g. frame 4→1) after a big stall, which would look WORSE,
not better. Reverted that change before committing anything — logged here
so a future me doesn't re-propose the same broken "fix." The lesson: a
timing loop that looks superficially correct because it references
`performance.now()` is not proof of behavior; simulate the actual frame
sequence before changing a working animation loop.

**Fix:**
- `pet-sprite.tsx`: canvas backing store now `Math.round(drawW/drawH *
  min(devicePixelRatio, 3))` instead of the plain CSS-pixel `drawW`/`drawH`,
  matching the sibling canvases. `drawImage`'s destination rect now targets
  the backing-store dimensions (`backingW`/`backingH`) instead of the CSS
  ones. Dropped the JSX `width`/`height` canvas attributes (which fight an
  imperatively-set `.width`/`.height`) in favor of imperative-only sizing,
  matching `pixel-egg-sprite.tsx`'s exact pattern. Added `imageRendering:
  'pixelated'` to the canvas's CSS, matching every other pixel-art surface
  in the pet component tree.
- Verified the pop-out overlay's per-pixel alpha click-through hit-test
  (`pet-overlay-app.tsx`) is unaffected: it already computes
  `target.width / rect.width` as a RATIO before scaling a click coordinate
  into canvas space, so it's dpr-correct automatically — previously that
  ratio was always exactly 1 (no HiDPI backing store), and is now correctly
  the real device pixel ratio. No hit-test regression; if anything it's more
  mathematically sound now.

**Verification:** full desktop `tsc --noEmit` clean, `eslint` clean, pet
test suite 33/33 unchanged, full desktop suite unchanged at 235 files / 2093
tests passing (same pre-existing unrelated `localStorage.clear` failures).

**Files:** `apps/desktop/src/components/pet/pet-sprite.tsx` (DPR-aware
canvas backing store + `imageRendering: 'pixelated'`).

**Merge note:** fork-only file, no upstream equivalent — no conflict risk.

### Fork-only fix — 2026-07-23 (pet: kept "running" against a wall it had already stopped at)

**Symptom:** user reported the roaming pet keeps trying to run into walls —
it hits the end of its walkable path and stops moving, but the running-leg
animation keeps playing as if it were still walking.

**Root cause:** `use-pet-roam.ts`'s `beginPause()` correctly stops the pet
(`$petMotion.set(null)`) the instant it settles at a wall or picks a rest
beat. But `$petState`'s derivation (`store/pet.ts`) intentionally lets a busy
pose (`toolRunning`/`busy` → `run`) win over the roam pose — by design, so a
stride mid-tool-call still reads as alive rather than the roam loop's pause
freezing the pet's legs mid-work. The bug: `$petMotion === null` means BOTH
"roam just settled at a wall" AND "roam was never enabled at all" — the busy
check can't tell those apart, so if the agent was STILL busy the instant the
pet hit a wall, the running pose kept forcing itself onto a pet that had
already, correctly, stopped moving. Reads exactly as "the pet keeps trying to
run into the wall."

**Fix:** added `$petRoamPaused` — a boolean, false by default (so a
roam-disabled pet, the pop-out overlay, and the generate-flow preview, none
of which mount `usePetRoam`, are completely unaffected), set true by
`beginPause()` (settled/loafing) and the drag-yield branch, cleared by
`beginVertical()` (starting a hop) and `planNext()`'s walk-start branch, and
reset in the effect's disable/cleanup path. `$petState`'s computed callback
now takes an extra branch: a `base === 'run'` derived from busy/tool activity
still shows `idle` when `motion === null && roamPaused` — i.e. exactly the
"stopped at a wall while still busy" case — but is otherwise untouched
(waiting/review/failed keep their existing priority; a roam-disabled pet's
busy pose never changes since its `$petRoamPaused` never leaves `false`).

Added 3 new `pet.test.ts` cases: the exact busy+wall scenario resolving to
idle then back to running once roam picks a new stroll target, a check that
a roam-disabled pet's `run` pose is completely unaffected, and a check that
`waiting`/other higher-priority states aren't masked by the new branch.

**Verification:** full desktop `tsc --noEmit` clean, `eslint` clean, pet test
suite 36/36 (13 in `pet.test.ts`, up from 10), full desktop suite unchanged
at 235 files / 2096 tests passing (same pre-existing unrelated
`localStorage.clear` failures).

**Files:** `apps/desktop/src/store/pet.ts` (new `$petRoamPaused` +
`$petState` branch), `apps/desktop/src/store/pet.test.ts` (3 new tests),
`apps/desktop/src/components/pet/use-pet-roam.ts` (sets/clears the new
signal across every phase transition).

**Merge note:** fork-only files, no upstream equivalent — no conflict risk.

### Fork-only fix — 2026-07-26 (pet: uncapped rAF loops kept running at full rate in the background, chewing battery)

**Symptom:** user reported Hermes Desktop draining battery noticeably.
`powermetrics --samplers tasks` showed the renderer process pegged at
37–53% of a core, sustained, with Safari frontmost and Hermes fully
occluded/backgrounded — the single hottest process on the machine, ahead of
WindowServer and kernel_task.

**Root cause:** `electron/main.ts` deliberately disables Chromium's normal
renderer-backgrounding throttle app-wide (`disable-renderer-backgrounding`,
`disable-backgrounding-occluded-windows`, `disable-background-timer-throttling`)
so a streaming chat reply doesn't stall when the window loses focus (Chromium
normally pauses `requestAnimationFrame` for a hidden/occluded renderer — see
the `ad09bf387` upstream fix on the same theme). That's correct for the
transcript stream, which now flushes off a timer, not rAF. But it means every
*other* rAF loop in the renderer runs at a full, uncapped 60Hz forever,
whether the window is visible or not, with no clamp to fall back on. Two pet
loops were the biggest offenders: `pet-sprite.tsx`'s canvas render loop (ticks
every frame even though the sprite itself only steps ~5Hz) and
`use-pet-roam.ts`'s physics step loop (runs continuously whenever roam is on).
`app/starmap/star-map.tsx` had already solved this exact problem for its own
loop with a `document.hidden` / `document.hasFocus()` gate — the pet loops
just never got the same treatment.

**Fix:** ported star-map's pause/resume pattern to both pet loops. Each adds
an `isPaused()` check (`document.hidden` or `!document.hasFocus()`), a
`schedule()` wrapper that only calls `requestAnimationFrame` when not paused,
and a `visibilitychange`/`blur`/`focus` listener that cancels the in-flight
frame on hide and kicks a fresh one on show. `pet-sprite.tsx` also resets
`drawnFrame` on resume so the next frame always repaints instead of relying on
a stale "no change" skip; `use-pet-roam.ts` resets `last` on resume so the
`MAX_DT_S` clamp absorbs the paused gap instead of teleporting the pet. Purely
cosmetic loops — no visible behavior change while the window is actually in
front of the user, since `document.hasFocus()` is true in that case
regardless of the process-level throttle switches.

Left `display.pet.enabled` on for the user in the meantime — this fix removes
the need to keep it off. (User also set it to `false` as an immediate,
no-rebuild mitigation while this fix was in flight; safe to re-enable once
this build is running.)

**Verification:** full desktop `tsc --noEmit` clean (no new errors in either
file), `eslint` clean on both files, pet test suite 28/28 unchanged, full
desktop suite unchanged at 256 files / 2227 tests passing (2 pre-existing
skips, unrelated).

**Files:** `apps/desktop/src/components/pet/pet-sprite.tsx` (visibility-gated
render loop), `apps/desktop/src/components/pet/use-pet-roam.ts`
(visibility-gated step loop).

**Merge note:** fork-only files, no upstream equivalent — no conflict risk.

### Fork-only fix — 2026-07-26 (regression from the same-day pet visibility-gate fix: sprite froze on one frame)

**Symptom:** immediately after rebuilding with the visibility-gate fix
above, the pet got stuck looping the same single animation frame instead of
animating normally — reported right after the rebuild+relaunch.

**Root cause:** the gate above paused the loop on `document.hidden ||
!document.hasFocus()`, copied directly from `star-map.tsx`'s pattern. That's
correct for star-map (a foreground interactive panel where "not focused"
genuinely means covered/backgrounded), but wrong for `PetSprite`, which has
two consumers with different windowing:

1. The in-window floating mascot (`floating-pet.tsx`) — a background
   companion meant to keep animating while glanced at, even when Hermes
   isn't the OS-focused window. `document.hasFocus()` goes false the instant
   any other app is clicked into, even though the pet is still fully visible
   on screen — so the loop paused and stayed paused on whatever frame it
   happened to be on for as long as focus stayed elsewhere.
2. The popped-out overlay (`pet-overlay-app.tsx`) — its own `BrowserWindow`
   created with `focusable: false` and shown via `showInactive()`
   specifically so it never takes OS focus (see `spawnPetOverlayWindow` in
   `electron/main.ts`). `document.hasFocus()` is *permanently* false in that
   window by design, so its sprite render loop froze on frame 1 the instant
   it mounted and never moved again.

Both read as "stuck looping the same animation" — exactly the reported
symptom.

**Fix:** drop the `hasFocus()` half of the gate; pause purely on
`document.hidden` (true Page Visibility — minimized, switched to another
space, or occluded enough that Chromium flips it), which still eliminates
the original battery drain (a genuinely backgrounded/occluded window) without
punishing "visible but not OS-focused." Also removed the now-pointless
`window.addEventListener('blur'/'focus', ...)` listeners from both files,
since the gate no longer reacts to focus changes — only `visibilitychange`
matters now. Added an explicit comment in both files explaining why the pet
deliberately differs from star-map's stricter gate, so a future port of one
pattern to the other doesn't reintroduce this.

**Verification:** `tsc --noEmit` clean, `eslint` clean on both files, pet
test suite 28/28, full desktop suite 256 files / 2227 tests passing (2
pre-existing skips, unrelated).

**Files:** `apps/desktop/src/components/pet/pet-sprite.tsx`,
`apps/desktop/src/components/pet/use-pet-roam.ts` (both: gate narrowed to
`document.hidden` only, blur/focus listeners removed).

**Merge note:** fork-only files, no upstream equivalent — no conflict risk.

### Fork-only fix — 2026-07-26 (second regression on the same day: document.hidden itself is unreliable in this app, not just hasFocus())

**Symptom:** after rebuilding with the `document.hidden`-only gate above, the
pet was still stuck on a single frame from the moment the app launched, and
the roam physics loop never ran at all (the pet didn't drop to the pet zone's
floor on mount, as it should).

**Root cause (confirmed via a second-opinion review before touching code
again, since this was the second broken attempt at the same fix):**
`electron/main.ts` deliberately disables Chromium's own occlusion tracking
app-wide — `disable-renderer-backgrounding`, `disable-backgrounding-occluded-
windows`, `disable-background-timer-throttling` — so a streaming chat reply
doesn't stall on refocus. `document.hidden`/`visibilitychange` are partially
driven by that same occlusion-tracking machinery. Disabling it doesn't just
stop *occlusion* from flipping `document.hidden` (which was the intent) — it
can leave Page Visibility stuck at whatever it was initialized to. Both
windows are created with `show: false` and shown later via `win.show()` /
`win.showInactive()` once ready; `document.hidden` starts `true` at that
initial `show: false` mount, and with occlusion tracking off, the
show→visible transition doesn't reliably propagate back to Blink to flip it
to `false`. Both loops mount already paused and never receive a
`visibilitychange` event to unpause them — permanently frozen from launch,
matching the exact symptom (stuck sprite, roam physics never starts).

This app's own architecture doctrine (`apps/desktop/AGENTS.md`, "Decide state
by authority") says exactly this: Electron is authoritative for machine/
runtime facts, the renderer's copy is a cache of that truth. `document.hidden`
is a renderer-side derivation that this app has specifically broken by
opting out of the machinery that keeps it honest — it was never a safe
signal to use here for anything, not just the `hasFocus()` half.

**Fix:** stopped using `document.hidden` entirely for this gate. Added a new
main-process → renderer IPC channel, `hermes:window-visibility-changed`,
pushed by `wireCommonWindowHandlers()` (shared by the main window, secondary
session windows, and the pet overlay window) on native `show`/`hide`/
`minimize`/`restore` events — `win.isVisible() && !win.isMinimized()` is a
real OS window-manager fact, untouched by the Chromium command-line switches.
Exposed via `preload.ts`'s `onWindowVisibilityChanged` (same pattern as the
existing `onPowerResume`) and typed in `global.d.ts`. Added
`use-window-visibility.ts`: a small `subscribeWindowVisibility()` helper the
two pet loops now call instead of touching `document` directly. Both loops'
local `paused`/`hidden` state now **defaults to `false` (running)** rather
than probing an unreliable signal at mount — if `onWindowVisibilityChanged`
is unavailable (tests, a non-Electron context) or the first IPC message
hasn't landed yet, the loop runs rather than risking a permanent freeze; a
real main-process push corrects it moments later if the window is actually
hidden.

Also fixes a self-inflicted mid-edit slip caught before commit: an early
patch attempt on `preload.ts` accidentally deleted the
`ipcRenderer.on('hermes:boot-progress', listener)` line inside the existing
`onBootProgress` handler while inserting the new channel above it. Caught by
re-reading the file after the edit (not by tsc/eslint — it was still valid
JS, just dead) and restored before running any verification.

**Verification:** `tsc --noEmit` clean across the whole desktop app, `eslint`
clean on every touched file (`pet-sprite.tsx`, `use-pet-roam.ts`,
`use-window-visibility.ts`, `global.d.ts`, `electron/main.ts`,
`electron/preload.ts`), pet test suite 28/28, full desktop suite 256 files /
2227 tests passing (2 pre-existing skips, unrelated) — including
`main-window-lifecycle.test.ts` and the other `electron/*.test.ts` window
lifecycle coverage.

**Files:** `apps/desktop/electron/main.ts` (visibility push in
`wireCommonWindowHandlers`), `apps/desktop/electron/preload.ts`
(`onWindowVisibilityChanged` bridge), `apps/desktop/src/global.d.ts` (typing),
`apps/desktop/src/components/pet/use-window-visibility.ts` (new — the
subscription helper), `apps/desktop/src/components/pet/pet-sprite.tsx`,
`apps/desktop/src/components/pet/use-pet-roam.ts` (both switched from
`document.hidden` to the IPC signal, default-unpaused at mount).

**Merge note:** fork-only files, no upstream equivalent — no conflict risk.


### Fork-only fix — 2026-07-27 (test-order-dependent failure: `test_profile_global_fallback_normalizes_in_memory_without_writing` leaked host Keychain/env state)

**Symptom:** `pytest-randomly` (newly added dev dependency to catch exactly
this class of bug) surfaced
`tests/agent/test_credential_pool_oat_authtype.py::test_profile_global_fallback_normalizes_in_memory_without_writing`
failing with `assert not (profile_home / "auth.json").exists()` — but only
under certain random orderings, and it passed reliably run in isolation.

**Root cause (test isolation bug, not production):** the test monkeypatches
`Path.home()` and `HERMES_HOME` to point at fresh tmp dirs, but never mocks
`agent.anthropic_adapter.read_claude_code_credentials()` the way its sibling
test `test_load_heals_legacy_row_and_exposes_it_to_resolver` does. `load_pool()`
calls `_seed_from_singletons("anthropic")`, which (when `CLAUDE_CODE_OAUTH_TOKEN`
is unset and no explicit API-key path is signaled) tries to auto-discover a
real Claude Code OAuth credential — from `~/.claude/.credentials.json` (safe,
since `Path.home()` is mocked) **and from the macOS Keychain** (`security
find-generic-password -s "Claude Code-credentials"`), which is an OS-level
lookup unaffected by the `Path.home()` monkeypatch. On this dev machine a
real Keychain entry exists, so `load_pool()` seeds a genuine `claude_code`
OAuth entry into the pool, `changed=True`, and it writes `auth.json` into
`profile_home` — exactly what the test asserts must not happen. This surfaced
"order-dependent" under `pytest-randomly` because sibling tests also
`monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", ...)`, and depending on run
order that env var was sometimes already set (short-circuiting into the
even-more-real `env:CLAUDE_CODE_OAUTH_TOKEN` seed path) or absent (falling
through to the Keychain lookup) — both non-hermetic paths depending on
ambient host state, not on genuine cross-test state leakage in
`agent/credential_pool.py` itself (verified no unreset module-level caches
there).

**Fix:** in the test, explicitly `monkeypatch.delenv` the anthropic env vars
(`ANTHROPIC_API_KEY`, `ANTHROPIC_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`) and
`monkeypatch.setattr("agent.anthropic_adapter.read_claude_code_credentials",
lambda: None)`, mirroring the existing hermetic pattern already used one test
above it in the same file. No production code changed.

**Verification:** `pytest tests/agent/test_credential_pool_oat_authtype.py -p
randomly -q` passing 7/7 across 5 different random seeds (previously failed
under seed `3464663842` and reproducibly with `-p no:randomly` too once
`CLAUDE_CODE_OAUTH_TOKEN`/Keychain state was present); full
`tests/agent/ -k credential_pool` suite (123 tests) still green; `ruff check`
clean.

**Files:** `tests/agent/test_credential_pool_oat_authtype.py` (test fixture
isolation only).

**Merge note:** test-only file, low conflict risk with upstream; if upstream
carries the same test it likely has the identical gap (this is an
environment-dependent bug class, not fork-specific behavior) and the same fix
should apply cleanly.


### Fork-only fix — 2026-07-27 (macOS-only failure: `test_media_files_routed_by_type` compared against an unresolved /tmp symlink path)

**Symptom:** `pytest-randomly` flagged
`tests/gateway/test_background_command.py::TestRunBackgroundTask::test_media_files_routed_by_type`
as failing under one random ordering — but investigation showed it's not
actually order-dependent; it fails deterministically on macOS in isolation
too, regardless of run order. The random-seed run just happened to be the
first time this session ran the file in a way that surfaced it.

**Root cause (test-comparison artifact, not a production bug):**
`gateway/platforms/base.py`'s `validate_media_delivery_path()` intentionally
calls `Path.resolve(strict=True)` on every candidate path *before* its
containment/denylist security checks — symlinks must be resolved before a
containment check runs, not after, or a symlink could be used to escape the
allowed-roots check. On macOS, `/tmp` and `/var/folders/...` are both
symlinks into `/private/...`, so the path `_run_background_task` hands back
after validation is the fully-resolved `/private/...` form. The test built
its expected value from the raw `tempfile.mkdtemp()` return value (the
unresolved `/tmp/...` or `/var/folders/...` form) and asserted equality
against that — a real mismatch, but in the test's comparison, not in the
security-motivated resolution behavior it was accidentally exercising.

**Fix:** resolve the tmpdir once, up front in the test
(`os.path.realpath(tempfile.mkdtemp(...))`), so the expected path already
matches the form validate_media_delivery_path() will hand back — no change
to `gateway/platforms/base.py`'s security logic. Documented inline at the
call site (see comment above the `_tmpdir` assignment).

**Verification:** `pytest tests/gateway/test_background_command.py -p
randomly -q` passing 22/22 across 5+ different random seeds and in
isolation; `pytest tests/gateway/ -k background -q` (full sibling suite)
still green; `ruff check` clean.

**Files:** `tests/gateway/test_background_command.py` (test fixture only —
no production code changed).

**Merge note:** test-only file, low conflict risk with upstream.


### Fork-only fix — 2026-07-27 (test-order/environment-dependent failure: `test_seed_supervise_skeleton_*` setgid bit silently stripped depending on pytest's ambient basetemp group ownership)

**Symptom:** `pytest-randomly` (via a full-suite run, not the file in
isolation) surfaced
`tests/hermes_cli/test_service_manager.py::test_seed_supervise_skeleton_creates_expected_layout`
and `test_seed_supervise_skeleton_handles_log_subservice` failing with
`assert stat.S_IMODE(...) == 0o3730` → `assert 984 == 2008` (984 = `0o1730`,
missing the setgid bit `0o2000`). The whole file passes 66/66 in isolation.

**Root cause (environment-dependent, not a leaking test/production bug):**
`hermes_cli/service_manager.py`'s `_mkdir_owned()` sets mode
(`0o3730` — setgid + sticky) via `path.chmod(mode)`. On macOS/BSD, a newly
created directory inherits its GROUP from its *parent* directory (not from
the creating process's egid, unlike SysV semantics) — and POSIX kernel
semantics silently strip the setgid bit on chmod whenever the acting
process is not a member of the directory's group (only root or a group
member may set setgid for that group; no exception is raised, chmod just
"succeeds" with a different resulting mode). Whether this bites depends
entirely on which group owns the ancestor of pytest's `tmp_path` basetemp
for a given run: the common case (`/private/var/folders/.../T/pytest-of-
<user>/...`) is group `staff`, which the test user belongs to, so setgid
sticks — but a full-suite run can end up with pytest's basetemp instead
under `/tmp/pytest-of-<user>/...`, whose ancestor `/tmp` is group `wheel`,
which the test user does NOT belong to, silently stripping setgid.
Confirmed directly: `TMPDIR=/tmp uv run pytest ...` (forcing the bad
ancestor) reproduces the failure deterministically; the same command
without `TMPDIR` set passes. Not a bug in `_mkdir_owned` itself (which
already handles the analogous `os.chown` `PermissionError` case
explicitly and correctly) — the test's own `svc_dir` fixture directory
just inherited whatever ambient group pytest happened to pick.

**Fix:** in both tests, `os.chown(svc_dir, -1, os.getegid())` immediately
after creating `svc_dir` — pins the test directory's group to one the
test process is *always* a member of (its own effective gid), so the
setgid bit sticks regardless of which ancestor group pytest's basetemp
happens to land under that run. No production code changed.

**Verification:** `pytest tests/hermes_cli/test_service_manager.py -q`
66/66 passing; reproduced the original failure with `TMPDIR=/tmp uv run
pytest ...` (forcing the wheel-owned ancestor) on the pre-fix test file,
confirmed the fixed version passes under the same forced condition;
`ruff check` clean.

**Files:** `tests/hermes_cli/test_service_manager.py` (test fixture only —
no production code changed).

**Merge note:** test-only file, low conflict risk with upstream; this is a
genuinely environment-dependent (basetemp ancestor group ownership) bug
class, not fork-specific behavior.


### Fork-only fix — 2026-07-27 (previous commit's own CI run caught 2 real bugs in the mock-audit work itself)

**Symptom:** the push containing the pytest-randomly/spec'd-mock/lint-
guardrail work above (commit `ff06cd7d0`) went green in every LOCAL
verification this session ran, but real CI (`30282912663`) failed on 3
jobs: the new `Unspecced SDK-client mocks` lint job itself, and 2 Python
test slices.

**Root cause 1 (lint job — checker design bug):**
`scripts/check-unspecced-sdk-mocks.py --diff <ref>` flagged
`tests/run_agent/test_streaming.py:1732` — a bare `mock_client =
MagicMock()` for an OpenAI-shaped client (`.chat.completions.create`,
`base_url="https://openrouter.ai/..."`), unrelated to the Anthropic-only
migration in this same file, untouched since an April 2026 commit
(confirmed via `git blame`). The checker's `--diff` mode determined which
FILES changed but then scanned the WHOLE file for matches, so any
pre-existing unspecced mock anywhere in a touched file blocked the PR —
exactly the "retrofix on touch" behavior the checker's own docstring says
it deliberately avoids. Fixed by teaching `scan_file()` an `only_lines`
parameter and adding `get_added_lines()`, which parses `git diff
--unified=0`'s hunk headers to restrict matches to lines actually
added/changed by the diff being checked, not merely lines living inside a
changed file.

**Root cause 2 (2 test slices — real dependency bug in the new fixture):**
`tests/conftest.py`'s `spec_anthropic_client`/`spec_async_anthropic_client`
fixtures did a bare top-level `import anthropic` inside
`_build_spec_anthropic_client()`. `anthropic` is a lazy-installed optional
extra (see `pyproject.toml`'s `[all]` comment: deliberately removed from
the always-installed set on 2026-05-12 specifically so a quarantined PyPI
release of it can't break every fresh install or CI slice) — it is
genuinely not guaranteed present. `ModuleNotFoundError: No module named
'anthropic'` at fixture-setup time errored 3 tests in
`tests/agent/test_auxiliary_client.py`'s `TestAnthropicOAuthFlag` class in
whichever CI slice didn't have it installed. Fixed by swapping the bare
import for `pytest.importorskip("anthropic")`, matching the project's
existing convention for this exact situation (see
`tests/hermes_cli/test_timeouts.py`'s own `pytest.importorskip("anthropic")`
call) — cleanly skips affected tests instead of erroring when the optional
SDK isn't installed.

**Verification:** `check-unspecced-sdk-mocks.py --diff ff06cd7d0~1` now
returns 0 matches on the exact commit that failed CI (previously 1);
confirmed `pytest.importorskip` raises a clean `Skipped` (not an error)
when `anthropic` import is blocked; `tests/agent/test_auxiliary_client.py`
+ `tests/run_agent/test_streaming.py` + `tests/conftest.py` +
`tests/hermes_cli/test_timeouts.py` together: 390 tests passed, 0 failed
via `scripts/run_tests.sh`. `ruff check .` and
`check-windows-footguns.py --all` clean. The 2 other CI test-slice
failures on the same run (`test_dashboard_auth_gate.py`) were confirmed
pre-existing and unrelated (fails identically on unmodified `main`,
established earlier this session).

**Files:** `scripts/check-unspecced-sdk-mocks.py`, `tests/conftest.py`.

**Merge note:** `scripts/check-unspecced-sdk-mocks.py` is fork-only
tooling with no upstream equivalent, no conflict risk. `tests/conftest.py`
is a shared file but this touches only the fixtures added in the prior
entry above — low conflict risk.


### Fork-only fix — 2026-07-27 (2 real intra-file test-order leaks in test_tui_gateway_server.py, surfaced by pytest-randomly's own CI run)

**Symptom:** the second CI recovery push above (`61da986b1`) still failed
1 Python test slice: `tests/test_tui_gateway_server.py::test_config_set_approval_mode_persists_three_way_value_and_emits_live_status`
— `assert emitted[0][0:2] == ("session.info", "sid")` got `("session.info",
"abx")` instead. Passed cleanly in isolation. Delegated to a subagent for
root-cause investigation (matches the exact "passes solo, fails in a full
randomized run" signature pytest-randomly exists to catch).

**Root cause 1 (real intra-file leak — module-level dict, not env/ambient):**
`server._sessions["abx"]` (seeded by
`test_image_attach_bytes_writes_to_gateway_dir`) was never cleaned up on
that test's exit path — most sibling tests in this file seed
`server._sessions[<id>]` directly and clean up via their own
`try/finally` + `.pop()`/`.clear()`, but this one didn't. A leftover
`"abx"` entry becomes the FIRST value iterated/emitted by whichever later
test runs next under a given random seed, corrupting that test's
assertion about its own session id. Fixed at the module level with a new
autouse `_isolate_server_sessions(monkeypatch)` fixture — a systemic
safety net (snapshot/restore `server._sessions` around every test) rather
than auditing all ~183 individual `server._sessions[...]` seed sites in
this 11k-line file. Uses `monkeypatch.setattr(server, "_sessions",
dict(server._sessions))` specifically (NOT a bare
`try/finally: server._sessions.clear(); server._sessions.update(...)`) —
the bare version raced
`test_session_delete_fails_closed_when_active_snapshot_raises`'s OWN
`monkeypatch.setattr(server, "_sessions", _ExplodingDict())` (a test
double lacking `.clear()`), because pytest tears fixtures down LIFO
relative to setup and there's no way to force "run after a fixture I
don't depend on" without riding its actual mechanism — `monkeypatch`
instances are cached per test call, so putting both patches on the same
instance's single undo stack resolves the ordering correctly regardless
of declaration order.

**Root cause 2 (real bug, unrelated to the leak above, ALSO surfaced by
random-seed reruns — this repo's own dev-shell ambient environment AND a
genuine hermes_cli/main.py argv-parsing gap):**

- 2a: this dev machine's shell exports `HERMES_DESKTOP=1` (running inside
  the Hermes desktop app). `_resolve_session_platform()` in
  `tui_gateway/server.py` reads that env var to distinguish "desktop"
  from "tui", so any test not explicitly isolating it inherited the
  ambient "desktop" value instead of the expected default "tui". Fixed
  with a second autouse fixture, `_isolate_desktop_env`, that strips
  `HERMES_DESKTOP`/`HERMES_DESKTOP_TERMINAL` for every test in this
  module (individual tests that need the desktop branch still set these
  explicitly via monkeypatch).
- 2b: `hermes_cli/main.py`'s `_apply_profile_override()` runs at MODULE
  IMPORT TIME, scanning the real process `sys.argv` for `-p`/`--profile`
  — but "this module got imported" and "the hermes CLI was invoked" are
  different events colliding here. `pytest -p randomly` (real pytest
  plugin-activation syntax; matters now that pytest-randomly is a dev
  dependency) gets misread as `hermes -p randomly` the instant any test
  file importing `hermes_cli.main` runs inside that invocation, crashing
  with `sys.exit(1)` ("Profile 'randomly' does not exist"). An existing
  regex guard already special-cased `no:xdist`-shaped values (colon =
  clearly invalid profile name) but a plain-word plugin name like
  `randomly` is shaped exactly like a real profile and slips through.
  Two guard attempts were WRONG and reverted before landing on the right
  one (see the extensive comment on `_looks_like_hermes_invocation` in
  the code): checking `PYTEST_VERSION` broke
  `tests/hermes_cli/test_apply_profile_override.py`, which calls
  `_apply_profile_override()` DIRECTLY with a test-controlled
  `monkeypatch.setattr(sys, "argv", ["hermes", ...])` and legitimately
  wants the override logic to run even under pytest; checking only
  `sys.argv[0]`'s basename against `{hermes, hermes-agent, hermes-acp}`
  broke the equally-real, documented `python -m hermes_cli.main`
  invocation style (used by `hermes_cli/relaunch.py`'s own fallback,
  `gateway.py`'s `--replace` re-exec argv builder, and a systemd
  `ExecStart` doc example) — CPython rewrites `argv[0]` to the resolved
  module file path under `-m`, not the script name. Final fix:
  `_looks_like_hermes_invocation()` checks BOTH the console-script
  basename AND the `.../hermes_cli/main.py` file-path shape, covering
  real console-script invocations, real `-m`/direct-script invocations,
  and the test file's own test-constructed `["hermes", ...]` argv, while
  correctly rejecting pytest's own real process argv (never any of those
  shapes).

**Verification:** `tests/test_tui_gateway_server.py` — 385/385 passing
across 11 of 12 random-seed reruns in a row (the 1 remaining failure,
`test_notification_poller_live_loop_requeues_foreign_completion_for_owner`,
passed cleanly in isolation and on 4 immediate reruns — a separate,
pre-existing async-timing flake unrelated to this fix, not chased
further). `tests/hermes_cli/test_apply_profile_override.py` (12/12),
`test_startup_plugin_gating.py`, `test_gateway_command_line_matcher.py`
(both also exercise `_apply_profile_override`/argv parsing) all still
passing — confirmed the `-m hermes_cli.main` guard fix didn't regress
any of them. Full CI-matching `scripts/run_tests.sh` across all 4 files:
460 tests passed, 0 failed. `ruff check .` clean.

**Files:** `tests/test_tui_gateway_server.py` (2 new autouse fixtures +
try/finally cleanup on the one leaking test), `hermes_cli/main.py` (the
argv-identity guard + its helper function).

**Merge note:** `tests/test_tui_gateway_server.py` is test-only, low
conflict risk. `hermes_cli/main.py`'s `_apply_profile_override()` is
fork-specific profile-selection logic (upstream doesn't have Hermes'
multi-profile system) — low conflict risk, but re-verify against
upstream's own `-p`/`--profile` handling (if any) on next merge.


### Fork-only fix — 2026-07-27 (blocking CI lint failures from a concurrent session's commit — missing encoding= on 4 open()/.read_text() calls)

**Symptom:** a concurrent Hermes session's commit
(`38d36c0357`, "fix(tools): strip orphan tool-call TAIL leaking into final
content") added `scripts/hermes_hard_eval.py` — despite the file's own
docstring saying "NOTE: deliberately untracked — do not commit" — with 4
`open()`/`.read_text()` calls missing the explicit `encoding=` argument
required by this repo's blocking `ruff` (`unspecified-encoding`, PLW1514)
and Windows-footgun checks. Landed on `main` via `git pull --ff-only`
during this session's own CI-recovery work, turning the SAME blocking
lint jobs I'd just fixed (in the two entries above) red again on the next
push, for an unrelated reason.

**Fix:** added `encoding="utf-8"` to all 4 call sites in
`scripts/hermes_hard_eval.py` (2× `Path.read_text()`, 1× `open(..., "a")`,
1× more `Path.read_text()` in a different function) — no logic changes,
matches the identical fix pattern already documented in this file's
2026-07-26 "CI Lint + uv.lock/CI Tests permanently red" entry.

**Verification:** `ruff check .` and
`scripts/check-windows-footguns.py --all` both clean (840 files scanned,
0 footguns).

**Files:** `scripts/hermes_hard_eval.py`.

**Merge note:** the file's own docstring says it's meant to stay
untracked/uncommitted — flagging for the user rather than untracking it
myself, since that's a workflow decision outside a lint-fix's scope.


### Fork-only fix — 2026-07-27 (a 6th real intra-file test-order leak, plus a subtle monkeypatch.delenv footgun: it snapshots the CURRENT value, not the pre-test value)

**Symptom:** the CI run for the previous "2 real intra-file leaks" push
still failed 2 unrelated Python test slices, this time on
`tests/gateway/test_matrix_message_length.py::TestMatrixMaxMessageLength::test_default_limit_is_16000`
— `assert adapter.max_message_length == 16000` got `12000`. Passed cleanly
in isolation; reproduced deterministically with `--randomly-seed=7`.

**Root cause 1 (real leak):** `_apply_yaml_config()` in
`plugins/platforms/matrix/adapter.py` sets `MATRIX_MAX_MESSAGE_LENGTH`
directly via `os.environ[...] = ...` (by design — its docstring says
"everything flows through env"), and `test_apply_yaml_config_sets_env`
exercises exactly this real behavior with value `12000`. Left in place,
that value is the third-priority fallback `_resolve_max_message_length()`
reads (`extra` dict → env var → plugin registry), so any later test in
the file that doesn't set its own explicit value inherits it —
`test_default_limit_is_16000` calls `_make_adapter()` with no override at
all, so it should always see the true default 16000.

**Root cause 2 (why my FIRST fix attempt didn't work — a genuine
monkeypatch semantics footgun worth remembering):** my initial fix added
a second `monkeypatch.delenv("MATRIX_MAX_MESSAGE_LENGTH", raising=False)`
at the end of the test, expecting it to clean up. It didn't —
`monkeypatch.delenv`/`setenv` record an UNDO entry that restores whatever
value was present *at the moment that specific call runs*, not "the
value before this test started." Sequence: call 1 (test start) found the
var absent → no undo entry pushed. `_apply_yaml_config()` then set it to
`"12000"` via a RAW (untracked) assignment. Call 2 (my attempted cleanup)
found `"12000"` present → snapshotted `"12000"` as the thing to restore,
then deleted it. On test teardown, monkeypatch's undo stack faithfully
restored... `"12000"` — the value my own "cleanup" call had just seen,
not the original absent state. Confirmed by adding temporary debug
tracing showing `os.getenv(...)` correctly returned `None` immediately
after the second `delenv()` call inside the test, but the very next
test's `_make_adapter()` call saw `"12000"` again once monkeypatch's
fixture teardown ran.

**Fix:** replaced the second `monkeypatch.delenv()` call with a raw,
untracked `os.environ.pop("MATRIX_MAX_MESSAGE_LENGTH", None)`. This
doesn't touch monkeypatch's undo stack at all, so teardown restores only
the TRUE original state recorded by the first (pre-test) call.

**Also checked (NOT touched — pre-existing, unrelated):** the same CI run
additionally failed `test_windows_subprocess_no_window_flags.py`,
`test_hindsight_provider.py` (3 tests), `test_docker_network_config.py`,
`test_honcho_plugin/test_session.py` (2 tests), and the already-documented
`test_dashboard_auth_gate.py`. Verified each in isolation:
`test_windows_subprocess...`/`test_hindsight_provider.py` pass cleanly
alone and pass when run together with the fixed matrix file (confirming
they were incidental to running ~200 files in one un-isolated CI slice
process, not caused by the matrix leak or anything else this session
touched). `test_docker_network_config.py::test_reuse_keeps_airgapped_container_when_lockdown_requested`
fails in TRUE isolation too — `/usr/bin/docker` doesn't exist on this
machine — a pre-existing environment gap (same category as
`test_dashboard_auth_gate.py`), not a test-order bug; left untouched, out
of scope for this fix.

**Verification:** `tests/gateway/test_matrix_message_length.py` — 9/9
passing across 10 consecutive `-p randomly` reruns plus the exact
originally-failing `--randomly-seed=7`. `tests/gateway/ -k matrix` (full
sibling suite) still green. `ruff check .` and
`check-windows-footguns.py --all` clean.

**Files:** `tests/gateway/test_matrix_message_length.py`.

**Merge note:** test-only file, low conflict risk with upstream.


### Fork-only fix — 2026-07-27 (pytest-randomly default-on turned CI into unbounded whack-a-mole — disabled by default, still available on demand)

**Symptom:** after landing 6 real intra-file leak fixes today (all found
via `pytest-randomly`, each documented in its own entry above), CI kept
turning red on a NEW, unrelated, previously-never-seen test on every
subsequent push — `test_slack_mention.py`, `test_deepinfra_provider.py`,
etc. — each passing cleanly in isolation. Consulted a second opinion:
`pytest-randomly`'s seed differs on every invocation unless explicitly
pinned, and CI's test slices bundle ~200 files into ONE shared pytest
process each (see `scripts/run_tests_parallel.py`'s own docstring on why
per-FILE isolation exists but per-slice does not) — so a large,
still-unaudited population of pre-existing intra-file leaks scattered
across the whole suite was surfacing one new random failure per push,
with no way to converge on green without auditing the entire suite in
one sitting. Today's blocking-gate addition of `pytest-randomly` had
turned net-positive bug-finding into an unbounded CI-blocking chase.

**Fix:** added `-p no:randomly` to `pyproject.toml`'s `[tool.pytest.
ini_options] addopts`, restoring pytest's normal deterministic
(file-declaration) order for every default invocation — local and CI —
while leaving `pytest-randomly` fully installed. An explicit `-p randomly`
on the command line (exactly how this whole session's leak-hunting was
done) still overrides the default and re-enables it, confirmed directly.
This converts "blocking on an unknown-size pre-existing backlog" into
"available on demand for a deliberate, bounded audit session" — the
correct scope for a single day's fix-forward work, not an indefinite
whole-suite remediation.

**Verification:** confirmed default `pytest tests/gateway/
test_matrix_message_length.py -v` shows no `--randomly-seed=` line
(deterministic); confirmed `-p randomly` still activates it
(`Using --randomly-seed=...` reappears). `tests/gateway/
test_matrix_message_length.py` + `tests/hermes_cli/
test_apply_profile_override.py` + `tests/test_tui_gateway_server.py`
together: 406 passed, 1 skipped, deterministic order. `ruff check .` and
`check-windows-footguns.py --all` clean.

**Follow-up (not done today, tracked here):** a bounded, deliberate
leak-audit session — pin a fixed `--randomly-seed`, run the full suite
once, fix everything that seed surfaces, repeat for a few more fixed
seeds to build confidence — is the right way to actually shrink the
remaining backlog, rather than reactive one-at-a-time CI-push chasing.

**Files:** `pyproject.toml`.

**Merge note:** config-only change, no logic touched; trivial to
re-apply or drop on next upstream merge depending on whether upstream
also adopts pytest-randomly.

### Fork-only fix — 2026-07-28 (desktop: duplicate session tabs from bypassed dedup guard)

**Symptom:** the same session could show up as two tabs in one window's tab
strip — a tile tab plus a separate workspace tab both bound to the identical
stored session id.

**Root cause:** `focusOpenSession()` (in `apps/desktop/src/store/session-states.ts`)
is the guard that fronts an already-open tab instead of loading a duplicate,
but it was only wired into the sidebar-row click handler
(`onResumeSession` in `contrib/wiring.tsx`). Every OTHER way to jump to a
session — the Ctrl+Tab session switcher, ^N session-slot hotkeys, the command
palette (direct-id paste and the sessions list), artifacts' "open chat",
native-notification click, cron/command-center "go to session", and the
cold-start remembered-session restore — called `navigate(sessionRoute(id))`
directly, skipping the guard entirely. The workspace pane
(`ChatRoutesSurface`) renders whatever `$selectedStoredSessionId` points to
independent of `$sessionTiles`, so navigating straight to a session id loaded
it into the workspace tab even while a tile for that same session already sat
in the tab strip.

**Fix:** added `goToSession(navigate, storedSessionId, opts?)` in
`store/session-states.ts` as the single canonical "jump to this session" entry
point — it calls `focusOpenSession` first (fronts the existing tab, no-op)
and only falls through to `navigate(sessionRoute(id), opts)` when the session
has no open tab anywhere. Rewired every bypassing call site to go through it:
- `app/session-switcher.tsx` (Ctrl+Tab pick)
- `app/hooks/use-keybinds.ts` (^N slot hotkeys, renamed the local shadowing
  helper to `gotoSlotSession` to avoid colliding with the imported name)
- `app/command-palette/index.tsx` (direct session-id paste + sessions list;
  added a `goSession` wrapper alongside the existing generic `go`)
- `app/artifacts/index.tsx` (both "open chat" call sites)
- `app/contrib/hooks/use-desktop-integrations.ts` (native-notification click +
  cold-start remembered-session restore)
- `app/contrib/wiring.tsx` (CommandCenterView + CronView "open session")

Left untouched: the sidebar's own `onResumeSession` (already correctly
guarded, with richer "open beside" tile semantics `goToSession` doesn't need
to replicate) and the `navigate(sessionRoute(...))` calls inside
`use-session-actions/index.ts` for session creation, branch creation, and
compression-id rotation — those always target a brand-new id or the current
selection, which by construction can never already have an open tile.

**Verification:** added 4 regression tests to
`store/session-states.test.ts` covering `goToSession` directly (fronts an
open tile without navigating, fronts main without navigating, falls through
to `navigate` for a session with no open tab, forwards `{replace: true}` on
the fallback path). Full desktop suite: 256 test files / 2231 tests pass
(was 2227 before the 4 new tests), `tsc --noEmit` clean, `eslint` clean on
every touched file.

**Files:** `apps/desktop/src/store/session-states.ts`,
`apps/desktop/src/store/session-states.test.ts`,
`apps/desktop/src/app/session-switcher.tsx`,
`apps/desktop/src/app/hooks/use-keybinds.ts`,
`apps/desktop/src/app/command-palette/index.tsx`,
`apps/desktop/src/app/artifacts/index.tsx`,
`apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts`,
`apps/desktop/src/app/contrib/wiring.tsx`.

**Merge note:** pure fork-local UI dedup fix, no upstream file identity
concerns; trivial to re-apply on the next upstream merge if these files
conflict.
