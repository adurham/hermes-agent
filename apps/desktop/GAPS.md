# Desktop Gap Analysis (2026-07-25)

Reference notes from a feature-gap review of Hermes Desktop against two
comparison classes: AI coding tools (Cursor, Windsurf, VS Code + Copilot,
Cline) and general polish-caliber apps (Linear, Raycast, Arc). **Nothing here
is scheduled or approved for implementation** — this is a documented backlog
of ideas for future consideration, checked against the app's actual source
(`apps/desktop/src`) so it doesn't repeat things already built.

Re-verify against source before acting on any item — this file is a snapshot,
not a live source of truth. Update it in place if an item ships or a
conclusion turns out wrong.

---

## Part 1 — IDE-parity gaps (vs. Cursor/Windsurf/VS Code+Copilot)

Ranked by value, with explicit fit-vs-philosophy reasoning against this
project's stated anti-goal: Desktop is a **chat-first native agent app, not
an IDE** (see `apps/desktop/AGENTS.md`: "Desktop is its own native chat
surface... does not embed the TUI"; root `AGENTS.md` footprint ladder:
extend existing code → CLI+skill → service-gated tool → plugin → MCP server
→ new core tool, last resort).

### Do (ranked)

1. **@-mention file/symbol autocomplete in the composer.** Missing entirely
   today. Zero philosophy conflict — reuses the file tree that already
   powers the file browser, needs no new core tool. Table-stakes on every
   competing tool; highest ROI on this list.

2. **Turn-scoped checkpoints with one-click revert.** The highest-value
   *strategic* gap, not just a nice-to-have. ~90% of the plumbing already
   exists: session history, and the Review pane's stage/unstage/revert
   (`src/app/right-sidebar/review/`). Missing piece: a shadow-commit/stash
   tied to a specific chat turn, surfaced through the existing Review pane.
   This is the trust mechanism that lets a user hand the agent more
   autonomy over a repo. No new core tool, no toolset swap mid-conversation
   — doesn't threaten the prompt-caching invariant.

3. **Codebase/semantic search — shipped as a CLI+skill, not a panel.**
   Real value for agent grounding, but the correct shape per the footprint
   ladder is a tool whose results get cited in chat and optionally jump-open
   the existing single-file preview (`src/app/chat/right-rail/preview-file.tsx`)
   — *not* a dedicated Sourcegraph-style sidebar. A standalone search panel
   would start pulling Desktop toward IDE-navigation territory.

4. **Inline accept/reject on diff-bearing tool-call cards.** Attach actions
   to the transcript's existing structured tool-call summaries rather than
   building new editor chrome. Complements the Review pane; doesn't replace
   it. Moderate value, low cost.

5. **Test status as structured tool output only.** Agent runs tests,
   pass/fail summary renders as a tool-call card, reusing the terminal +
   transcript machinery already in place. No dedicated panel, no test
   selection UI.

### Explicit anti-goals — would be a mistake to add

- **Full LSP client** in the editor pane (autocomplete-while-typing,
  hover-docs, go-to-definition). This *is* the product core of
  Cursor/Windsurf — building it means competing on IDE mechanics that
  `apps/desktop/AGENTS.md` explicitly rules out. If diagnostics matter for
  agent grounding, expose them via a typecheck/lint tool card in chat, not a
  maintained per-language LSP client.
- **Multi-file/multi-tab code editor.** The current single-file preview
  (`src/components/chat/code-editor.tsx`) is correctly scoped as a
  spot-edit/preview surface. A tabbed workspace is the first domino toward
  rebuilding VS Code inside Electron, with no natural stopping point.
- **Devcontainer / remote-SSH / Codespaces-style environment management.**
  Actively competes with the Connections abstraction Desktop already owns
  (local managed backend / remote gateway / Hermes Cloud). A second,
  IDE-flavored remote-environment model for the same conceptual problem
  doubles the maintenance surface for no benefit.
- **Full debugger (DAP breakpoints/step/watch).** Maximum IDE coupling,
  minimum fit for a chat-first agent. The terminal + agent-runs-and-reports
  pattern already captures most of the real-world value.

**Meta-signal:** everything in the "do" list lands at the "extend existing
code" or "skill" rung of the footprint ladder — none require a new core tool
or a general extension mechanism. Anything that *would* require that (full
LSP, full debugger) is itself the tell that it belongs in "don't build."

---

## Part 2 — Quality-of-life / UI / UX gaps

Desktop is already unusually mature on QoL — most of the "usual suspects"
checklist for a Cursor/Linear/Raycast-caliber app is already covered. The
items below survived a source-level grep pass specifically to avoid
re-suggesting things that already exist.

### Already built (confirmed in source — do not re-propose)

- Session tiles + route tiles (split view of any session or full page
  beside the main thread, persisted) — `src/store/session-states.ts`,
  `src/store/route-tiles.ts`
- Per-session color tags with project-color fallback, survives
  auto-compression's session-id rotation — `src/store/session-color.ts`
- Session pinning, drag-to-reorder sessions/projects, project grouping —
  `src/app/chat/sidebar/reorderable-list.tsx`, `sidebar/projects/`
- Mac-style Ctrl+Tab session switcher (quick-tap vs. hold-to-reveal HUD,
  220ms threshold) — `src/store/session-switcher.ts`
- Composer: per-session input history ring (derived live, not duplicated),
  draft-snapshot restore, persisted prompt queue, drag/paste attachments —
  `src/store/composer-input-history.ts`, `composer-queue.ts`
- Window translucency slider (native opacity, macOS/Windows) — `translucency.ts`
- Text zoom, main-process-owned, always in sync with Ctrl/Cmd +/-/0 —
  `src/store/zoom.ts`
- Secondary standalone chat windows (query-string flag, hides global
  sidebar/onboarding) — `src/store/windows.ts`
- Native OS notifications, 5 independently toggleable kinds (approval,
  input, turnDone, turnError, backgroundDone) — `native-notifications.ts`
- Haptics toggle, embed-consent privacy gate (per-embed/per-service) —
  `haptics.ts`, `embed-consent.ts`
- Full theme system incl. custom user themes AND VS Code theme JSON import
  — `src/themes/vscode.ts`
- 4-locale i18n with enforced parity (en, ja, zh, zh-hant)
- Pet/companion mascot with its own activity-mirroring state machine
- Command palette + separate Command Center overlay with maintenance page
- Sidebar-collapse persistence across reload (has a dedicated regression
  test) — `sidebar-collapse-persistence.test.ts`
- Rigorous design system: single `Button` primitive, mandatory `<Tip>`
  tooltips on icon-only buttons (lint-enforced, no native `title=`), keybind
  hints that live-read the rebindable keymap (`TipKeybindLabel`),
  token-only colors, reduced-motion respect
- **Multi-monitor window-state restore done right** — validates saved
  bounds against live `screen.getAllDisplays()` work areas, caps size to the
  current display so a window saved on an ultrawide doesn't spawn
  off-screen on a laptop — `electron/window-state.ts`
- Status bar already surfaces context/token usage —
  `src/app/shell/hooks/use-statusbar-items.tsx`, `ContextUsagePanel`
- Message-level edit-and-resubmit and regenerate already exist —
  `src/components/assistant-ui/thread/user-edit-composer.tsx`,
  `ActionBarPrimitive.Reload` in `assistant-message.tsx`
- Completion sound picker, 14 variants — `src/lib/completion-sound.ts`,
  `src/store/completion-sound.ts`
- Deep-link highlighting from the command palette (`?param=id` →
  scroll-into-view + flash) — `src/app/settings/use-deep-link-highlight.ts`

### Genuinely missing (grep-confirmed gaps, ranked)

1. **Global full-text search across session/message content.** The command
   palette does navigation, raw session-ID paste, and settings deep-links —
   but there's no "find that thing I said last Tuesday" content search
   across all sessions/projects. Confirmed absent (no FTS/full-text search
   wiring found beyond the backend's own `session_search` used internally
   by the agent, not exposed as a user-facing search surface). This is the
   single biggest remaining gap for a Linear-caliber power user.
2. **Conversation branching / fork-from-message.** No `branchId`/`forkFrom`
   anywhere in the codebase. Edit-and-resubmit exists but always mutates the
   same linear thread; there's no way to preserve the original branch and
   explore an alternate path as a sibling.
3. **System tray / menu-bar quick-access mode.** No `Tray(` usage anywhere
   in `electron/`. No menu-bar icon, no quick-capture-from-tray, no
   minimize-to-tray background mode.
4. **Persistent notification history/inbox.** The 5 native-notification
   kinds are real and independently toggleable, but there is no in-app log
   of what fired while the user was away — the in-app toast feed
   (`store/notifications.ts`) is ephemeral only, no persisted history view.
5. **Accessibility/screen-reader coverage.** Notably inconsistent with how
   rigorous the rest of the design system is (enforced tooltips, keybind
   hints, reduced-motion respect elsewhere) — sparse ARIA usage in the
   shell, no dedicated a11y test suite found.

---

## How to use this file

- Before starting any item, re-grep the relevant area — this snapshot is
  from 2026-07-25 and the app moves fast.
- Pick items from Part 1 or Part 2 independently; they don't depend on each
  other.
- If an item ships, delete its entry (or move it to "Already built") in the
  same change that implements it — a stale gap list is worse than no list.
