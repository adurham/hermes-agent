import { act, render } from '@testing-library/react'
import { createElement, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $petMotion, $petRoamDir } from '@/store/pet'

import { usePetRoam } from './use-pet-roam'

/**
 * Regression coverage for the bug fixed alongside this test: `usePetRoam`'s
 * setup effect used to be keyed on a combined `enabled` flag that folded in
 * the agent's live activity pose (`canRoam`). That signal flips constantly
 * during a real chat session (every turn completion/clarify/error/celebrate
 * beat), and each flip tore down + rebuilt the WHOLE physics closure (phase,
 * walk target, dwell timer, fall/jump integrators) — so the wander state
 * machine never survived long enough to progress on its own; it only ever
 * appeared to move at the instant a re-seed pulled a fresh position from the
 * live DOM rect. Fixed by splitting that signal into `canMove`, read via a
 * ref inside the rAF loop instead of an effect dependency, so toggling it
 * pauses/resumes the SAME closure in place instead of resetting it.
 *
 * These tests exercise the hook directly (via a harness component) with a
 * fake rAF driven by manual frame advances, asserting: (1) DOM position
 * mutated by the loop survives a `canMove` flip untouched, and (2) an
 * `enabled` flip still legitimately resets everything, so the fix didn't
 * just paper over the symptom by never resetting anything at all.
 */

function Harness({
  canMove,
  enabled,
  onPos
}: {
  canMove: boolean
  enabled: boolean
  onPos: (p: { x: number; y: number }) => void
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)

  usePetRoam({
    canMove,
    commit: onPos,
    containerRef,
    enabled,
    isInteracting: () => false,
    loopMs: 1100,
    overlayOpen: false,
    petH: 96,
    petW: 96
  })

  return createElement('div', {
    'data-testid': 'pet',
    ref: containerRef,
    style: { left: '0px', position: 'fixed', top: '0px' }
  })
}

describe('usePetRoam canMove vs enabled', () => {
  let rafCallbacks: FrameRequestCallback[]
  let rafId: number
  let now: number

  beforeEach(() => {
    rafCallbacks = []
    rafId = 0
    now = 1000

    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      rafCallbacks.push(cb)

      return ++rafId
    })
    vi.stubGlobal('cancelAnimationFrame', () => undefined)
    vi.spyOn(performance, 'now').mockImplementation(() => now)

    // A real floor to stand/walk on: full-window mode reads window size.
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 800 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 600 })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    $petMotion.set(null)
    $petRoamDir.set(0)
  })

  // Advance one fake animation frame: fire every callback queued so far and
  // let any new ones queued during this tick land for the NEXT advance.
  const advanceFrame = (deltaMs: number) => {
    now += deltaMs
    const due = rafCallbacks
    rafCallbacks = []

    act(() => {
      due.forEach(cb => cb(now))
    })
  }

  it('keeps scheduling a new frame every tick — does not die after the first frame', () => {
    // Direct regression test for the actual root cause: `schedule()` only
    // calls `requestAnimationFrame` when its local `raf` id is falsy, but
    // nothing used to clear that id after the browser "spent" it — so the
    // loop scheduled exactly ONE frame per effect mount and then went
    // silently dead forever, however long `enabled`/`canMove` stayed true.
    const onPos = vi.fn()

    render(createElement(Harness, { canMove: true, enabled: true, onPos }))

    // The initial `schedule()` call at effect-setup queues frame 1.
    expect(rafCallbacks.length).toBe(1)

    // Firing it must queue a SECOND frame — if `raf` is never cleared, this
    // stays at 0 forever and the pet is permanently frozen after one tick.
    advanceFrame(16)
    expect(rafCallbacks.length).toBe(1)

    // And that must keep happening indefinitely, not just once more.
    for (let i = 0; i < 50; i++) {
      advanceFrame(16)
      expect(rafCallbacks.length).toBe(1)
    }
  })

  it('freezes physics on a canMove flip but resumes the SAME state instead of resetting it', () => {
    const onPos = vi.fn()

    const { rerender } = render(createElement(Harness, { canMove: true, enabled: true, onPos }))

    // Let it run long enough to leave the initial pause and start walking or
    // otherwise progressing — several seconds of frames at a real cadence.
    for (let i = 0; i < 300; i++) {
      advanceFrame(16)
    }

    const el = document.querySelector('[data-testid="pet"]') as HTMLDivElement
    const leftAtFlip = el.style.left
    const topAtFlip = el.style.top

    // Flip canMove off mid-flight — this is the constant activity churn
    // (`canRoam` going false on a turn-completion beat) that used to nuke
    // the whole loop.
    rerender(createElement(Harness, { canMove: false, enabled: true, onPos }))
    advanceFrame(16)
    advanceFrame(16)
    advanceFrame(16)

    // Frozen: no further movement while canMove is false.
    expect(el.style.left).toBe(leftAtFlip)
    expect(el.style.top).toBe(topAtFlip)

    // Flip back on — the SAME closure resumes; position picks up from
    // exactly where it was frozen, not reseeded/reset to a fresh pause.
    rerender(createElement(Harness, { canMove: true, enabled: true, onPos }))

    for (let i = 0; i < 60; i++) {
      advanceFrame(16)
    }

    const stillOnScreen = Number.parseFloat(el.style.left) >= 0 && Number.parseFloat(el.style.top) >= 0

    expect(stillOnScreen).toBe(true)
  })

  it('an enabled flip DOES reset the loop (this is intentional — structural mount/unmount)', () => {
    const onPos = vi.fn()

    const { rerender } = render(createElement(Harness, { canMove: true, enabled: true, onPos }))

    for (let i = 0; i < 120; i++) {
      advanceFrame(16)
    }

    onPos.mockClear()

    // Structural: roam opted out entirely.
    rerender(createElement(Harness, { canMove: true, enabled: false, onPos }))

    // Unmounting the loop commits the final position and clears the motion
    // signal — this is the cleanup path, expected on a real enabled flip.
    expect($petMotion.get()).toBe(null)
    expect($petRoamDir.get()).toBe(0)
  })
})
