import { atom, computed } from 'nanostores'

import { readKey, writeKey } from '@/lib/storage'
import { $currentCwd } from '@/store/session'

import { setTerminalTakeover } from '../store'

import { seedAgentTerminalCommand } from './agent-terminal-stream'

/** One in-app terminal tab. `id` is the renderer-side handle (distinct from the
 *  PTY session id the main process mints); each instance owns its own shell. */
export interface TerminalEntry {
  id: string
  /** Display label. `auto` adopts the resolved shell name until the user renames. */
  title: string
  auto: boolean
  /** Working directory, snapshotted once at creation. A plain terminal lives
   *  outside session/project state — the only thing it inherits is this
   *  initial cwd. A `projectId`-bound tab (see `ensureProjectTerminal`) is the
   *  one exception: switching to that project re-selects this tab. Switching
   *  *sessions* never moves or recreates a terminal either way. */
  cwd: string
  /** Last observed working directory of the live shell (tracked via the PTY
   *  cwd probe / OSC 7). Used to reopen the tab where the user last `cd`'d
   *  rather than the original launch dir. User tabs only. */
  restoreCwd?: string
  /** Serialized xterm scrollback from the last session, replayed on relaunch so
   *  the tab reopens with its recent history (VS Code parity). Processes are NOT
   *  revived — a fresh shell starts beneath the restored buffer. Captured live
   *  for user tabs only; agent mirrors stay runtime-only. */
  reviveBuffer?: string
  /** `user` = interactive PTY shell. `agent` = read-only mirror of an agent
   *  background process (`terminal(background=true)`), keyed by `procId`. */
  kind: 'user' | 'agent'
  procId?: string
  /** The sidebar project ($projectScope id) this tab is bound to, if it was
   *  created via `ensureProjectTerminal` (opening/switching to a scoped
   *  project). Unlike `cwd`, this ties the tab to the project going forward:
   *  entering that project again re-selects this same tab instead of the
   *  cwd-only match `createTerminal` gives every other tab. User tabs only. */
  projectId?: string
}

interface PersistedTerminalEntry {
  auto: boolean
  cwd: string
  id: string
  projectId?: string
  restoreCwd?: string
  reviveBuffer?: string
  title: string
}

interface PersistedTerminalState {
  activeTerminalId: null | string
  terminals: PersistedTerminalEntry[]
}

const TERMINALS_STORAGE_KEY = 'hermes.desktop.terminals.v1'

// Cap a single tab's replayed history so the persisted layout can't blow the
// localStorage quota. Roughly mirrors VS Code's persistentSessionScrollback
// default (100 lines) once the serialized escape codes are counted in.
const MAX_REVIVE_BUFFER_CHARS = 48_000

function sanitizePersistedTerminal(value: unknown): PersistedTerminalEntry | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const record = value as Record<string, unknown>
  const id = typeof record.id === 'string' ? record.id.trim() : ''
  const title = typeof record.title === 'string' ? record.title.trim() : ''
  const cwd = typeof record.cwd === 'string' ? record.cwd : ''
  const projectId = typeof record.projectId === 'string' && record.projectId ? record.projectId : undefined
  const restoreCwd = typeof record.restoreCwd === 'string' && record.restoreCwd ? record.restoreCwd : undefined
  const reviveBuffer = typeof record.reviveBuffer === 'string' ? record.reviveBuffer : undefined

  if (!id) {
    return null
  }

  return {
    auto: typeof record.auto === 'boolean' ? record.auto : true,
    cwd,
    id,
    ...(projectId ? { projectId } : {}),
    ...(restoreCwd ? { restoreCwd } : {}),
    ...(reviveBuffer ? { reviveBuffer } : {}),
    title: title || 'Terminal'
  }
}

function loadPersistedTerminals(): PersistedTerminalState {
  const fallback: PersistedTerminalState = { activeTerminalId: null, terminals: [] }
  const raw = readKey(TERMINALS_STORAGE_KEY)

  if (!raw) {
    return fallback
  }

  try {
    const parsed = JSON.parse(raw) as unknown

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return fallback
    }

    const record = parsed as Record<string, unknown>

    const terminals = Array.isArray(record.terminals)
      ? record.terminals.map(sanitizePersistedTerminal).filter((term): term is PersistedTerminalEntry => Boolean(term))
      : []

    const active =
      typeof record.activeTerminalId === 'string' && terminals.some(term => term.id === record.activeTerminalId)
        ? record.activeTerminalId
        : (terminals[0]?.id ?? null)

    return { activeTerminalId: active, terminals }
  } catch {
    return fallback
  }
}

// Persist synchronously on every change (the app-wide convention — see panes.ts
// / layout.ts). Capturing history this way means a snapshot is already on disk
// well before the renderer tears down, so app quit needs no unload hook.
function persistTerminals(list: readonly TerminalEntry[], activeTerminalId: null | string) {
  const terminals = list
    .filter(term => term.kind === 'user')
    .map(term => ({
      auto: term.auto,
      cwd: term.cwd,
      id: term.id,
      ...(term.projectId ? { projectId: term.projectId } : {}),
      ...(term.restoreCwd ? { restoreCwd: term.restoreCwd } : {}),
      ...(term.reviveBuffer ? { reviveBuffer: term.reviveBuffer } : {}),
      title: term.title
    }))

  if (!terminals.length) {
    writeKey(TERMINALS_STORAGE_KEY, null)

    return
  }

  const active = terminals.some(term => term.id === activeTerminalId) ? activeTerminalId : (terminals[0]?.id ?? null)
  writeKey(TERMINALS_STORAGE_KEY, JSON.stringify({ activeTerminalId: active, terminals }))
}

const restored = loadPersistedTerminals()

export const $terminals = atom<readonly TerminalEntry[]>(
  restored.terminals.map(term => ({ ...term, kind: 'user' as const }))
)
export const $activeTerminalId = atom<string | null>(restored.activeTerminalId)

$terminals.subscribe(list => persistTerminals(list, $activeTerminalId.get()))
$activeTerminalId.subscribe(active => persistTerminals($terminals.get(), active))

export const $activeTerminal = computed(
  [$terminals, $activeTerminalId],
  (list, id) => list.find(term => term.id === id) ?? null
)

const newId = () =>
  globalThis.crypto?.randomUUID?.() ?? `term-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`

/** Append a fresh terminal and focus it. Captures the current cwd once (its only
 *  tie to session/project state); pass an explicit cwd to override. Pass
 *  `projectId` to bind the tab to a sidebar project (see `ensureProjectTerminal`).
 *  Returns the id. */
export function createTerminal(cwd: string = $currentCwd.get(), projectId?: string): string {
  const id = newId()
  $terminals.set([
    ...$terminals.get(),
    { id, title: 'Terminal', auto: true, cwd, kind: 'user', ...(projectId ? { projectId } : {}) }
  ])
  $activeTerminalId.set(id)

  return id
}

// The most-recently-active tab per bound project, so re-entering a project
// resumes wherever the user left it there rather than always the first match.
// Runtime-only (not persisted) — a fresh app launch falls back to "first tab
// bound to this project" via ensureProjectTerminal below.
const lastActiveTerminalByProject = new Map<string, string>()

$activeTerminalId.subscribe(id => {
  const term = id ? $terminals.get().find(t => t.id === id) : undefined

  if (term?.kind === 'user' && term.projectId) {
    lastActiveTerminalByProject.set(term.projectId, term.id)
  }
})

// True when `child` is `parent` itself or a path nested under it. Mirrors the
// identically-named helper in store/projects.ts — duplicated rather than
// imported, since projects.ts imports ensureProjectTerminal from this module
// and importing back would form a cycle.
const underPath = (parent: string, child: string): boolean =>
  child === parent || child.startsWith(parent.endsWith('/') ? parent : `${parent}/`)

/** Switch to the terminal tab bound to `projectId` (from a previous call here),
 *  adopt an existing *unbound* tab already sitting inside `cwd` (the project
 *  root), or create a fresh one anchored there if neither exists. The adopt
 *  step matters: the pane's very first tab (opened via `ensureTerminal` before
 *  any project was ever entered, or spawned via the plain "+" control) has no
 *  `projectId` — without adopting it, entering that project would spawn a
 *  redundant duplicate right next to a tab that already covers it. Called when
 *  the sidebar project scope changes, so each project keeps — and returns to —
 *  its own terminal tab instead of a single shell drifting between working
 *  directories. Store-only: the underlying PTY spawns lazily when the pane
 *  actually mounts (see PersistentTerminal), so this is cheap even while the
 *  terminal pane is closed. Returns the tab id. */
export function ensureProjectTerminal(projectId: string, cwd: string): string {
  const list = $terminals.get()
  const remembered = lastActiveTerminalByProject.get(projectId)

  const bound =
    (remembered && list.find(term => term.id === remembered && term.kind === 'user')) ||
    list.find(term => term.kind === 'user' && term.projectId === projectId)

  if (bound) {
    $activeTerminalId.set(bound.id)

    return bound.id
  }

  const root = cwd.trim()

  const adoptable =
    root &&
    list.find(
      term => term.kind === 'user' && !term.projectId && underPath(root, (term.restoreCwd || term.cwd).trim())
    )

  if (adoptable) {
    $terminals.set(list.map(term => (term.id === adoptable.id ? { ...term, projectId } : term)))
    $activeTerminalId.set(adoptable.id)

    return adoptable.id
  }

  return createTerminal(cwd, projectId)
}

// Procs we've already surfaced a tab for — so closing an agent tab doesn't
// resurrect it on the next poll while the process is still running.
const surfacedProcs = new Set<string>()

const findByProc = (procId: string) => $terminals.get().find(term => term.procId === procId)

/** Auto-surface an agent background process as a read-only tab — once. Returns
 *  the tab id, or null if it was already surfaced and the user has since closed it. */
export function ensureAgentTerminal(procId: string, title: string): string | null {
  const existing = findByProc(procId)

  if (existing) {
    return existing.id
  }

  if (surfacedProcs.has(procId)) {
    return null
  }

  surfacedProcs.add(procId)
  const id = newId()
  $terminals.set([...$terminals.get(), { id, title: title || 'agent', auto: false, cwd: '', kind: 'agent', procId }])

  return id
}

/** Open + focus an agent process's tab (the status-stack link), recreating it if
 *  the user had closed it. Opens the pane. */
export function openAgentTerminal(procId: string, title: string): void {
  surfacedProcs.add(procId)
  seedAgentTerminalCommand(procId, title)
  let id = findByProc(procId)?.id

  if (!id) {
    id = newId()
    $terminals.set([...$terminals.get(), { id, title: title || 'agent', auto: false, cwd: '', kind: 'agent', procId }])
  }

  $activeTerminalId.set(id)
  setTerminalTakeover(true)
}

/** Guarantee at least one tab exists when the pane opens for the very first
 *  time. If a status-stack click already opened an agent tab, don't create a
 *  second, unrelated user shell just because the pane became visible. Pass
 *  `project` (a currently-scoped sidebar project's id + root cwd) so that
 *  first tab comes up already bound to it — otherwise a user who launches the
 *  app already inside a project and opens the terminal pane for the first
 *  time would get a blind, unbound tab that a later project switch has to
 *  detect and adopt instead of just starting out right. */
export function ensureTerminal(project?: { id: string; cwd: string }): void {
  if ($terminals.get().length === 0) {
    if (project?.cwd) {
      ensureProjectTerminal(project.id, project.cwd)
    } else {
      createTerminal()
    }
  }
}

export function selectTerminal(id: string): void {
  if ($terminals.get().some(term => term.id === id)) {
    $activeTerminalId.set(id)
  }
}

/** Move the active tab by `direction` (+1 next / -1 prev), wrapping around. */
export function cycleTerminal(direction: 1 | -1): void {
  const list = $terminals.get()

  if (list.length < 2) {
    return
  }

  const current = Math.max(
    0,
    list.findIndex(term => term.id === $activeTerminalId.get())
  )

  $activeTerminalId.set(list[(current + direction + list.length) % list.length].id)
}

/** Drop a terminal. Focus slides to the neighbor that fills its slot; closing
 *  the last one closes the whole pane. */
export function closeTerminal(id: string): void {
  const list = $terminals.get()
  const index = list.findIndex(term => term.id === id)

  if (index < 0) {
    return
  }

  const next = list.filter(term => term.id !== id)
  $terminals.set(next)

  if ($activeTerminalId.get() === id) {
    $activeTerminalId.set((next[index] ?? next[index - 1])?.id ?? null)
  }

  if (!next.length) {
    setTerminalTakeover(false)
  }
}

/** Close the read-only agent tab mirroring a background process. The agent
 *  drives this via the desktop-gated `close_terminal` tool → `terminal.close`.
 *  The process is NOT killed — only the view is dropped; `surfacedProcs` keeps
 *  it from auto-resurfacing, and the status-stack row can reopen it on demand.
 *  No-op when no such tab exists. */
export function closeAgentTerminalByProc(procId: string): boolean {
  const term = $terminals.get().find(t => t.kind === 'agent' && t.procId === procId)

  if (!term) {
    return false
  }

  closeTerminal(term.id)

  return true
}

export function closeActiveTerminal(): void {
  const id = $activeTerminalId.get()

  if (id) {
    closeTerminal(id)
  }
}

export function closeAllTerminals(): void {
  if ($terminals.get().length === 0) {
    return
  }

  $terminals.set([])
  $activeTerminalId.set(null)
  setTerminalTakeover(false)
}

export function closeOtherTerminals(id: string): void {
  const keep = $terminals.get().find(term => term.id === id)

  if (keep) {
    $terminals.set([keep])
    $activeTerminalId.set(keep.id)
  }
}

/** Record the latest serialized scrollback for a tab so it can be replayed on
 *  the next launch. Oversized buffers are tail-trimmed to stay under the storage
 *  budget; only user tabs ever carry one. */
export function updateTerminalReviveBuffer(id: string, reviveBuffer: string): void {
  const capped =
    reviveBuffer.length > MAX_REVIVE_BUFFER_CHARS ? reviveBuffer.slice(-MAX_REVIVE_BUFFER_CHARS) : reviveBuffer

  $terminals.set(
    $terminals.get().map(term => (term.id === id && term.kind === 'user' ? { ...term, reviveBuffer: capped } : term))
  )
}

/** Record the shell's latest working directory for a tab so the next launch can
 *  restart the PTY there instead of the original launch dir. User tabs only;
 *  no-ops when the value is empty or unchanged to avoid redundant persistence. */
export function updateTerminalRestoreCwd(id: string, restoreCwd: string): void {
  const next = restoreCwd.trim()

  if (!next) {
    return
  }

  $terminals.set(
    $terminals.get().map(term => {
      if (term.id !== id || term.kind !== 'user' || term.restoreCwd === next) {
        return term
      }

      return { ...term, restoreCwd: next }
    })
  )
}

export function renameTerminal(id: string, title: string): void {
  const trimmed = title.trim()

  $terminals.set(
    $terminals.get().map(term => (term.id === id ? { ...term, title: trimmed || term.title, auto: false } : term))
  )
}

/** A live terminal reports its resolved shell; adopt it as the label only while
 *  the user hasn't named the tab themselves. */
export function reportTerminalShell(id: string, shell: string): void {
  const name = shell.trim()

  if (!name) {
    return
  }

  $terminals.set($terminals.get().map(term => (term.id === id && term.auto ? { ...term, title: name } : term)))
}
