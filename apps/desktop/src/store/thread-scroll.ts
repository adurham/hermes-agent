import { atom, type WritableAtom } from 'nanostores'

import { activeConnectionScopeSuffix } from '@/lib/connection-scoped'
import { readKey, writeKey } from '@/lib/storage'
import { $activeProfile, normalizeProfileKey } from '@/store/profile'

// "Is the thread parked at the bottom" is owned by use-stick-to-bottom inside
// ThreadMessageList (the scroll container). That state lives only in that
// subtree, so ThreadMessageList mirrors it into these atoms for the composer,
// status stack, and floating jump button — all of which render OUTSIDE the thread.
//
// Each session owns its live chrome, including when two split panes are visible.
// Hidden keep-alive panes may neither publish nor reset a visible pane's state.
// Keep the flags separate so their thresholds can diverge without changing consumers.
export const $threadScrolledUpBySession = atom<Record<string, boolean>>({})
export const $threadJumpButtonVisibleBySession = atom<Record<string, boolean>>({})
export const $threadMessagesBelowBySession = atom<Record<string, number>>({})

export const publishThreadMessagesBelow = (
  count: number,
  publisher: { paneVisible: boolean; sessionId?: string | null }
): void => {
  if (publisher.paneVisible) {
    setSessionValue($threadMessagesBelowBySession, publisher.sessionId, count, 0)
  }
}

// Skip no-op writes and remove default entries so scroll ticks don't churn subscribers.
// Missing identities are not a shared bucket: callers use their existing surface id
// until a runtime session exists.
function setSessionValue<T extends boolean | number>(
  target: WritableAtom<Record<string, T>>,
  sessionId: string | null | undefined,
  value: T,
  empty: T
): void {
  if (!sessionId) {
    return
  }

  const current = target.get()

  if ((current[sessionId] ?? empty) === value) {
    return
  }

  const next = { ...current }

  if (value === empty) {
    delete next[sessionId]
  } else {
    next[sessionId] = value
  }

  target.set(next)
}

// `scrolledUp` dims the composer; it diverges from the jump pill once the reader
// stalls (see list.tsx), so the composer comes back while the pill stays.
export const setThreadAtBottom = (isAtBottom: boolean, sessionId: string | null = null, scrolledUp = !isAtBottom) => {
  setSessionValue($threadScrolledUpBySession, sessionId, scrolledUp, false)
  setSessionValue($threadJumpButtonVisibleBySession, sessionId, !isAtBottom, false)
}

export const resetThreadScroll = (sessionId: string | null = null) => {
  setThreadAtBottom(true, sessionId)
  setSessionValue($threadMessagesBelowBySession, sessionId, 0, 0)
}

export const publishThreadAtBottom = (
  isAtBottom: boolean,
  publisher: { paneVisible: boolean; sessionId?: string | null },
  scrolledUp = !isAtBottom
): void => {
  if (!publisher.paneVisible) {
    return
  }

  setThreadAtBottom(isAtBottom, publisher.sessionId, scrolledUp)
}

export const resetPublishedThreadScroll = (publisher: { paneVisible: boolean; sessionId?: string | null }): void => {
  if (!publisher.paneVisible) {
    return
  }

  resetThreadScroll(publisher.sessionId)
}

// Cross-component bridge: the jump button lives by the composer, the viewport's
// `scrollToBottom` lives inside the thread. The bridge registers a handler; the
// button fires it. Mirrors the composer focus/insert emitter pattern.
const handlers = new Map<string | null, Set<() => void>>()

export const onScrollToBottomRequest = (handler: () => void, sessionId: string | null = null) => {
  const scoped = handlers.get(sessionId) ?? new Set<() => void>()

  scoped.add(handler)
  handlers.set(sessionId, scoped)

  return () => {
    scoped.delete(handler)

    if (scoped.size === 0) {
      handlers.delete(sessionId)
    }
  }
}

export const requestScrollToBottom = (sessionId: string | null = null) => {
  handlers.get(sessionId)?.forEach(handler => handler())
}

export type ThreadPageDirection = -1 | 1

// Bare PageUp/PageDown are global keybinds, while the scroll owner lives in
// the focused thread. Route the intent by stable session key so kept-alive and
// split transcripts cannot page together.
const pageHandlers = new Map<string | null, Set<(direction: ThreadPageDirection) => void>>()

export const onThreadPageScrollRequest = (
  handler: (direction: ThreadPageDirection) => void,
  sessionKey: string | null = null
) => {
  const scoped = pageHandlers.get(sessionKey) ?? new Set<(direction: ThreadPageDirection) => void>()

  scoped.add(handler)
  pageHandlers.set(sessionKey, scoped)

  return () => {
    scoped.delete(handler)

    if (scoped.size === 0) {
      pageHandlers.delete(sessionKey)
    }
  }
}

export const requestThreadPageScroll = (direction: ThreadPageDirection, sessionKey: string | null = null) => {
  pageHandlers.get(sessionKey)?.forEach(handler => handler(direction))
}

// Inline edit grows a sticky human bubble. Fire on pointerdown so the viewport
// escapes stick-to-bottom before focus/layout; close clears the edit flag when
// the inline composer unmounts.
const editOpenHandlers = new Set<() => void>()
const editCloseHandlers = new Set<() => void>()

export const onThreadEditOpen = (handler: () => void) => {
  editOpenHandlers.add(handler)

  return () => void editOpenHandlers.delete(handler)
}

export const notifyThreadEditOpen = () => editOpenHandlers.forEach(handler => handler())

export const onThreadEditClose = (handler: () => void) => {
  editCloseHandlers.add(handler)

  return () => void editCloseHandlers.delete(handler)
}

export const notifyThreadEditClose = () => editCloseHandlers.forEach(handler => handler())

// ── Per-session scroll position persistence ──────────────────────────────────
// When the user scrolls up to read history, their distance-from-bottom is
// saved keyed by sessionKey and profile. On return, the session-switch settle
// loop restores it instead of pinning to the bottom, so the reading position
// survives session switches. Offsets are stored as distance-from-bottom, not
// scrollTop: the render-budget backfill prepends older turns and the switch
// relayout reshapes content above the on-screen rows, and bottom-anchored math
// keeps the restored view steady under that churn — the same reason the
// "Show earlier" flow in list.tsx restores from the bottom edge.
export type ThreadScrollState = { kind: 'bottom' } | { fromBottom: number; kind: 'offset' }

export const THREAD_SCROLL_BOTTOM: ThreadScrollState = { kind: 'bottom' }

// How far off the bottom a reader can be and still be recorded as "at the bottom".
//
// Not a subpixel epsilon. While the transcript re-renders under a reader who is following the tail —
// a tool card emitting progress, a streaming block, a pruned pane re-laying out — the follow loop is
// a frame behind the growth, and the live traces show it landing 17-80px off the bottom (e.g.
// `state: {fromBottom: 24.18, kind: 'offset'}` immediately followed by the same session measuring
// itself at the bottom). Storing that lag as a deliberate reading position is what made a remembered
// position drift: restoring it put the reader just outside the follow zone, so the follow disarmed
// and every later re-render of the same content walked them further up — the reported "it's a few
// messages up again, and the agent isn't even making new messages" (the growth is the same card
// re-rendering). The harness measures a message row at 396px; in the app the band is at most about
// one short one-line message, and it sits at the very bottom of the scroll area where the reader is
// looking at the tail anyway — so the reader who stopped just shy of the edge is yanked by an amount
// they cannot see, and no position inside the band is meaningfully different from the bottom.
export const THREAD_SCROLL_STICKY_THRESHOLD_PX = 96

export type ThreadScrollMetrics = {
  clientHeight: number
  scrollHeight: number
  scrollTop: number
}

export function threadScrollDistanceFromBottom(metrics: ThreadScrollMetrics): number {
  return Math.max(0, metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight)
}

/** Classify live metrics as sticky-bottom or an exact reading offset. */
export function threadScrollStateFromMetrics(
  metrics: ThreadScrollMetrics,
  threshold = THREAD_SCROLL_STICKY_THRESHOLD_PX
): ThreadScrollState {
  const fromBottom = threadScrollDistanceFromBottom(metrics)

  return fromBottom <= threshold ? THREAD_SCROLL_BOTTOM : { fromBottom, kind: 'offset' }
}

/**
 * How far a scrollTop may sit from an in-flight restore's target and still count as the app's own
 * scroll rather than the reader's.
 *
 * The app records `ownScrollTop` when it applies a target, but a content resize in the same frame (a
 * shrinking card, the composer clearance growing) makes the browser clamp scrollTop before the app
 * measures the new height — so `ownScrollTop` is stale by exactly that jitter, and the scroll
 * handler's exact-match test called the app's own clamp a FOREIGN scroll. That cancelled the restore
 * and left the reader wherever the transient frame was, which is the reported "I scrolled to the
 * bottom, bounced, and came back a few messages up": live trace has it as top=192 own=206,
 * top=1198 own=1212, top=509 own=523, top=1914 own=1927 — every one 13-14px, every one with the
 * target already at the new bottom. Well inside a message row, so a real scrollbar drag, rail jump or
 * find-in-page reveal still ends the restore.
 */
export const THREAD_SCROLL_GLUE_TOLERANCE_PX = 32

/**
 * True when a scroll position is the app's own rather than reader input: either the position it
 * applied itself, or — while the restore is still landing — a small deviation from its target, which
 * is the browser clamping after a same-frame content resize.
 */
export function isOwnThreadScroll(args: {
  heightChanged: boolean
  loadSettled: boolean
  ownScrollTop: number | null
  scrollTop: number
  targetTop: number
}): boolean {
  if (args.scrollTop === args.ownScrollTop || args.scrollTop === args.targetTop) {
    return true
  }

  return (
    !args.loadSettled &&
    args.heightChanged &&
    Math.abs(args.scrollTop - args.targetTop) <= THREAD_SCROLL_GLUE_TOLERANCE_PX
  )
}

/** The scrollTop that re-applies `state` at the current content height. */
export function threadScrollTargetTop(
  state: ThreadScrollState,
  metrics: Pick<ThreadScrollMetrics, 'clientHeight' | 'scrollHeight'>
): number {
  const max = Math.max(0, metrics.scrollHeight - metrics.clientHeight)

  return state.kind === 'bottom' ? max : Math.max(0, max - state.fromBottom)
}

/** How far a reader may drift from a captured anchor and still count as still at it. */
export const THREAD_SCROLL_ANCHOR_DRIFT_PX = 32

/**
 * May a parked anchor be applied to the viewport as it stands now?
 *
 * `restoreFromBottomRef` carries two different things through the same slot:
 *
 * - a CAPTURED anchor (a prepend/expand, or a restore the reader's own input cancelled) recording
 *   where the reader WAS — a bottom intent as `clientHeight`, otherwise their distance from the
 *   transcript bottom. Applying that to a reader who has since scrolled drags them back to a stale
 *   position, and it is an unconditional app write: no library escape can protect them from it. The
 *   probe build of 2026-10-30 has the shape — a settled reader left the bottom, the next streamed
 *   growth re-pinned them to the bottom, and the session was then recorded as `{kind:'bottom'}`, so
 *   the tab could never come back to where it was left.
 * - a PARKED OFFSET target (an unreachable remembered offset, parked on the settle loop's clamped
 *   exit) which is the position the reader ASKED for and is applied when the content finally covers
 *   it; its reader is still at the bottom of the short transcript, because that is where the clamp
 *   put them.
 *
 * So a captured anchor is applied when it still describes the reader — where the anchor puts them, or
 * the scrollTop they had when it was taken, so a prepend the browser has not yet adjusted for still
 * lands. Anything else means they have moved on, and the anchor is dropped rather than applied
 * behind them.
 */
export function shouldApplyThreadScrollAnchor(args: {
  anchor: number
  anchorHeight: number | null
  metrics: ThreadScrollMetrics
}): boolean {
  const { anchor, anchorHeight, metrics } = args
  const fromContentBottom = metrics.scrollHeight - metrics.scrollTop

  // Still where the anchor puts them (the browser adjusted scrollTop for the prepend itself), or
  // still at the scrollTop they had when it was taken (it has not adjusted yet). A reader who has
  // scrolled is at neither, and gets nothing.
  if (Math.abs(fromContentBottom - anchor) <= THREAD_SCROLL_ANCHOR_DRIFT_PX) {
    return true
  }

  return anchorHeight !== null && Math.abs(metrics.scrollTop - (anchorHeight - anchor)) <= THREAD_SCROLL_ANCHOR_DRIFT_PX
}

/**
 * The reader has left the bottom band and the sticky follow must be disarmed.
 *
 * The follow is only correct while the reader is AT the bottom, and the app knows that from its own
 * classification — the same one that records the reading position. It must act on it here rather
 * than leave the escape to use-stick-to-bottom's heuristic: that heuristic drops the reader's own
 * scroll whenever a content resize landed in the same frame (`if (state.resizeDifference ||
 * scrollTop === ignoreScrollToTop) return`, before it can set `escapedFromLock`), and its only other
 * escape is `handleWheel`, which needs a wheel event. A live turn resizes the transcript every few
 * frames, so scrollbar drags, keyboard pages and find-in-page reveals — the moves that cover
 * hundreds of pixels in a single scroll event, which no wheel can produce — routinely failed to
 * escape: the follow stayed locked and the next growth re-pinned the bottom over the reader.
 * Measured live (the `[scroll-trace]` probe, the build of 2026-10-30 11:37-14:07): every one of
 * those positions was destroyed 4-28ms after it was taken, every yank landing on the library's own
 * target (`scrollHeight - 1 - clientHeight`, one pixel short of `threadScrollTargetTop`), and the
 * position was then recorded as `{kind:'bottom'}` — which is why the per-session store held nothing
 * but bottoms and a tab never came back where it was left.
 *
 * A transition only: an app write at an offset (an offset restore, a prepend anchor, "Show earlier")
 * already stops the follow before it moves the viewport, and re-running this on every scroll event
 * would be a no-op anyway — but keeping it a transition means the disarm costs one call per escape.
 */
export function shouldDisarmThreadFollow(previous: ThreadScrollState, next: ThreadScrollState): boolean {
  return previous.kind !== 'offset' && next.kind === 'offset'
}

// Composer metrics write --composer-measured-height onto the chat surface,
// which grows [data-slot="aui_composer-clearance"] and can shrink the
// clampToComposer viewport. The post-settle restore ResizeObserver sees that
// as a content resize and used to re-pin a frozen fromBottom — rewriting
// scrollTop on every keystroke. Transcript height is scrollHeight minus the
// clearance spacer, so composer-only layout is distinguishable from real
// message/prepend/streaming growth.
export type ThreadScrollRestoreResizeMetrics = {
  clearanceHeight: number
  clientHeight: number
  scrollHeight: number
}

/** `data-slot` of the spacer that reserves room under the last row for the composer. */
export const COMPOSER_CLEARANCE_SLOT = 'aui_composer-clearance'

/** Viewport metrics with the composer clearance spacer measured separately. */
export function readThreadScrollResizeMetrics(
  viewport: HTMLElement,
  clearance: HTMLElement | null
): ThreadScrollRestoreResizeMetrics {
  return {
    clearanceHeight: clearance?.clientHeight ?? 0,
    clientHeight: viewport.clientHeight,
    scrollHeight: viewport.scrollHeight
  }
}

export function threadScrollTranscriptHeight(
  metrics: Pick<ThreadScrollRestoreResizeMetrics, 'clearanceHeight' | 'scrollHeight'>
): number {
  return Math.max(0, metrics.scrollHeight - Math.max(0, metrics.clearanceHeight))
}

/**
 * Post-settle restore RO may re-pin the restored target only when transcript
 * rows actually changed height. Composer clearance / viewport-box resizes and
 * no-op RO deliveries must not rewrite scrollTop — for a bottom target either,
 * or every keystroke yanks a view the user moved off the bottom back down.
 */
export function shouldReapplyFrozenThreadScrollOffset(
  target: ThreadScrollState,
  settled: boolean,
  previous: Pick<ThreadScrollRestoreResizeMetrics, 'clearanceHeight' | 'scrollHeight'>,
  next: Pick<ThreadScrollRestoreResizeMetrics, 'clearanceHeight' | 'scrollHeight'>
): boolean {
  if (!settled) {
    return false
  }

  const before = Math.round(threadScrollTranscriptHeight(previous))
  const after = Math.round(threadScrollTranscriptHeight(next))

  // A bottom target follows growth only — a shrink is clamped by the browser.
  return target.kind === 'bottom' ? after > before : after !== before
}

// Storage is scoped per profile with the same `.profile.<encoded>` suffix the
// app's other persisted session state uses (session.ts profileNavigationKey),
// so two profiles can never read or evict each other's reading positions.
const SCROLL_POS_KEY_BASE = 'hermes.desktop.threadScroll.v1'

export function threadScrollStorageKey(profile = $activeProfile.get()): string {
  return `${SCROLL_POS_KEY_BASE}.profile.${encodeURIComponent(normalizeProfileKey(profile))}${activeConnectionScopeSuffix()}`
}

// Bounded so a marathon runtime that touches hundreds of sessions doesn't grow
// the map forever. JS object insertion order gives LRU eviction — saving
// delete-and-re-adds the key, so the front is always the least-recently-used.
export const THREAD_SCROLL_MEMORY_LIMIT = 120

function isValidState(value: unknown): value is ThreadScrollState {
  if (!value || typeof value !== 'object') {
    return false
  }

  const record = value as Record<string, unknown>

  if (record.kind === 'bottom') {
    return true
  }

  return (
    record.kind === 'offset' &&
    typeof record.fromBottom === 'number' &&
    Number.isFinite(record.fromBottom) &&
    record.fromBottom >= 0
  )
}

function loadPositions(storageKey: string): Record<string, ThreadScrollState> {
  const raw = readKey(storageKey)

  if (!raw) {
    return {}
  }

  try {
    const parsed = JSON.parse(raw) as unknown

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {}
    }

    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).filter((entry): entry is [string, ThreadScrollState] =>
        isValidState(entry[1])
      )
    )
  } catch {
    return {}
  }
}

function persistPositions(storageKey: string, positions: Record<string, ThreadScrollState>) {
  const keys = Object.keys(positions)

  while (keys.length > THREAD_SCROLL_MEMORY_LIMIT) {
    delete positions[keys[0]!]
    keys.shift()
  }

  writeKey(storageKey, keys.length === 0 ? null : JSON.stringify(positions))
}

export function getThreadScrollPosition(
  sessionKey: string,
  storageKey = threadScrollStorageKey()
): ThreadScrollState | undefined {
  return loadPositions(storageKey)[sessionKey]
}

export function saveThreadScrollPosition(
  sessionKey: string,
  state: ThreadScrollState,
  storageKey = threadScrollStorageKey()
) {
  const positions = loadPositions(storageKey)

  // Delete then re-add to track recency (insertion order = LRU anchor).
  delete positions[sessionKey]
  positions[sessionKey] = state
  persistPositions(storageKey, positions)
}

export function clearThreadScrollPosition(sessionKey: string, storageKey = threadScrollStorageKey()) {
  const positions = loadPositions(storageKey)

  if (positions[sessionKey] === undefined) {
    return
  }

  delete positions[sessionKey]
  persistPositions(storageKey, positions)
}

/**
 * The restore/record gate for the session-switch settle loop. Pure so the
 * warm/cold switch lifecycle is testable without a DOM:
 *
 * - cold (no transcript yet): forget any in-flight restore, do not record —
 *   an empty-transcript instance holds the PREVIOUS session's live state and
 *   must not file it under the new key.
 * - same key, already settled: the restore is done; keep recording only.
 * - same key, still settling: a dep identity change re-ran the effect
 *   mid-loop — re-arm the restore instead of stranding the viewport.
 * - anything else (first content for this key, or a key change): restore.
 */
export type ThreadScrollRestorePlan = { cold: boolean; gate: string | null | undefined; restore: boolean }

export function planThreadScrollRestore(
  prevGate: string | null | undefined,
  sessionKey: string | null | undefined,
  hasGroups: boolean,
  settled: boolean
): ThreadScrollRestorePlan {
  if (!hasGroups) {
    return { cold: true, gate: null, restore: false }
  }

  if (prevGate === sessionKey && settled) {
    return { cold: false, gate: sessionKey, restore: false }
  }

  return { cold: false, gate: sessionKey, restore: true }
}
