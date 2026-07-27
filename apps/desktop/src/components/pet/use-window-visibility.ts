// Real OS-level window visibility for cosmetic per-renderer animation loops
// (the pet sprite, its roam loop) that want to pause while genuinely off
// screen. Deliberately NOT `document.hidden`/`visibilitychange`: this app
// disables Chromium's own occlusion/backgrounding machinery app-wide
// (disable-renderer-backgrounding, disable-backgrounding-occluded-windows,
// disable-background-timer-throttling — see electron/main.ts) so a streaming
// chat reply doesn't stall on refocus, and that also makes the Page
// Visibility API unreliable in this configuration: a window created with
// `show: false` can get stuck reporting `hidden` forever even once genuinely
// shown, because the show→visible transition stops propagating correctly to
// Blink when occlusion tracking is off. See FORK.md's 2026-07-26 entries for
// the regression this fixed.
//
// The main process is the correct source of truth here (wireCommonWindowHandlers
// pushes win.isVisible() && !win.isMinimized() over IPC on show/hide/minimize/
// restore) — this hook is a thin subscription to that signal.
//
// Defaults to `true`: if hermesDesktop.onWindowVisibilityChanged is
// unavailable (tests, a non-Electron context, or before the first IPC round
// trip lands) callers must NOT start paused, or they'd freeze on mount with
// no way to ever resume.
import { useEffect, useRef } from 'react'

export function subscribeWindowVisibility(onChange: (visible: boolean) => void): () => void {
  const off = window.hermesDesktop?.onWindowVisibilityChanged?.(payload => onChange(payload.visible))

  return off ?? (() => undefined)
}

/**
 * Ref-based variant for imperative rAF loops that read visibility inside a
 * `useEffect` without re-running the effect on every visibility flip (the
 * effect owns its own subscription lifecycle instead).
 */
export function useWindowVisibilityRef() {
  const visibleRef = useRef(true)

  useEffect(() => subscribeWindowVisibility(next => (visibleRef.current = next)), [])

  return visibleRef
}
