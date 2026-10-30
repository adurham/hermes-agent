// Stream-aware background throttling for chat windows.
//
// Chat windows must paint the live transcript while blurred, occluded, or
// minimized — but a static `backgroundThrottling: false` in webPreferences
// costs far more than that feature needs: it pins the renderer's
// `document.visibilityState` to 'visible' for the life of the window, which
// turns every visibility-gated poll and clock tick in the renderer into an
// always-on timer. An idle, hidden Hermes burned ~20% CPU forever.
//
// So throttling is a runtime dial instead: the renderers already report
// "which chats are mid-turn" for the quit guard (`hermes:active-work`), and
// this controller rides the merged edge of those reports. A turn in flight
// restores full cadence — but only for a window that is actually on screen.
// A window that is minimized, hidden, or on another Space keeps Chromium's
// throttling even mid-turn: nobody is looking at the frames it would paint,
// the deltas are already in the store, and the first paint after the window
// is shown renders the whole transcript. That distinction is the difference
// between "one session is streaming, so painting costs a little" and "six
// sessions are streaming, so the compositor runs at display refresh while the
// user works in another app" — the always-on-laptop case this file exists for.
//
// All turns done → after a short trailing delay (so tail flushes land at full
// cadence) Chromium's default throttling returns and even visible windows go
// quiet.
//
// On native Wayland only, a fullscreen chat may be misclassified as occluded
// after sitting idle (#94865). The caller can opt into keeping the fleet
// unthrottled while any registered window is fullscreen. The policy is
// deliberately opt-in so macOS/Windows/X11 keep the idle-CPU contract.
//
// Pure and Electron-free (timers + the WebContents surface are injected) so it
// can be unit-tested, mirroring session-windows.ts.

/** How long after the last turn ends before throttling is restored. Covers the
 * stream queue's final coalesced flush and the settle writes that trail a
 * turn's completion, so re-throttling never strands a visible delta. */
const RETHROTTLE_DELAY_MS = 5_000

export interface ThrottleWindowLike {
  isDestroyed(): boolean
  isFullScreen?: () => boolean
  /** Absent on test doubles and non-window surfaces: treated as on screen. */
  isMinimized?: () => boolean
  /** Absent on test doubles and non-window surfaces: treated as on screen. */
  isVisible?: () => boolean
  webContents?: {
    isDestroyed(): boolean
    setBackgroundThrottling(allowed: boolean): void
  } | null
}

interface TimersLike {
  clearTimeout(handle: unknown): void
  setTimeout(fn: () => void, ms: number): unknown
}

export interface StreamThrottleOptions {
  /** Native-Wayland workaround for #94865. False by default so other
   * platforms retain Chromium's idle background throttling. */
  keepFullscreenPainting?: boolean
}

export interface StreamThrottle {
  /** True while a turn is in flight (on-screen windows are then unthrottled). */
  isUnthrottled(): boolean
  /** Track a chat window; applies the current state immediately and stops
   * tracking on close. */
  register(win: ThrottleWindowLike & { on?: (event: string, fn: () => void) => void }): void
  /** Report whether any turn is in flight across all renderers. */
  update(busy: boolean): void
}

/** Events that can change whether a window counts as on screen. */
const VISIBILITY_EVENTS = ['show', 'hide', 'minimize', 'restore'] as const

export function createStreamThrottle(
  timers: TimersLike = { clearTimeout: handle => clearTimeout(handle as never), setTimeout },
  delayMs: number = RETHROTTLE_DELAY_MS,
  options: StreamThrottleOptions = {}
): StreamThrottle {
  const windows = new Set<ThrottleWindowLike>()
  const keepFullscreenPainting = options.keepFullscreenPainting === true
  let unthrottled = false
  let trailing: unknown = null
  let lastBusy = false

  function anyFullscreen(): boolean {
    if (!keepFullscreenPainting) {
      return false
    }

    for (const win of windows) {
      try {
        if (!win.isDestroyed() && win.isFullScreen?.()) {
          return true
        }
      } catch {
        // A window mid-teardown can throw from geometry queries; skip it.
      }
    }

    return false
  }

  /** Is this window actually being looked at? A minimized window and a hidden
   * one are not; anything that cannot be asked is assumed visible, which is
   * the contract every pre-visibility caller relies on. */
  function onScreen(win: ThrottleWindowLike): boolean {
    try {
      if (win.isMinimized?.() === true) {
        return false
      }

      return win.isVisible?.() !== false
    } catch {
      return true
    }
  }

  function apply(win: ThrottleWindowLike) {
    if (win.isDestroyed()) {
      windows.delete(win)

      return
    }

    const contents = win.webContents

    if (!contents || contents.isDestroyed()) {
      return
    }

    // Throttling stays ON for a window nobody can see, even mid-turn: the
    // paint is invisible, and Chromium's own occlusion tracking is the only
    // signal that knows about another app covering the window.
    const throttle = !(unthrottled && onScreen(win))

    try {
      contents.setBackgroundThrottling(throttle)
    } catch {
      // A window mid-teardown can throw; it's about to leave the set anyway.
    }
  }

  function applyAll() {
    for (const win of windows) {
      apply(win)
    }
  }

  function armRethrottleIfIdle() {
    if (lastBusy || anyFullscreen() || !unthrottled || trailing !== null) {
      return
    }

    trailing = timers.setTimeout(() => {
      trailing = null
      unthrottled = false
      applyAll()
    }, delayMs)
  }

  return {
    isUnthrottled: () => unthrottled,

    register(win) {
      windows.add(win)
      win.on?.('closed', () => {
        windows.delete(win)
        armRethrottleIfIdle()
      })

      // Minimizing or hiding a window mid-turn must hand it back to Chromium,
      // and showing it again must restore the full-cadence stream.
      for (const event of VISIBILITY_EVENTS) {
        win.on?.(event, () => {
          apply(win)
        })
      }

      if (keepFullscreenPainting) {
        win.on?.('enter-full-screen', () => {
          if (trailing !== null) {
            timers.clearTimeout(trailing)
            trailing = null
          }

          if (!unthrottled) {
            unthrottled = true
            applyAll()
          }
        })
        win.on?.('leave-full-screen', () => {
          armRethrottleIfIdle()
        })
      }

      apply(win)
    },

    update(busy) {
      lastBusy = busy

      if (busy) {
        if (trailing !== null) {
          timers.clearTimeout(trailing)
          trailing = null
        }

        if (!unthrottled) {
          unthrottled = true
          applyAll()
        }

        return
      }

      if (anyFullscreen()) {
        return
      }

      // Trailing edge: keep full cadence briefly so the final flush paints.
      armRethrottleIfIdle()
    }
  }
}
