import { describe, expect, it } from 'vitest'

import {
  $petActivity,
  $petAtRest,
  $petJumpBeat,
  $petMotion,
  $petRoamPaused,
  $petState,
  derivePetState,
  flashPetActivity,
  setPetActivity
} from './pet'

describe('derivePetState', () => {
  it('rests at idle by default and uses waiting when awaiting input', () => {
    expect(derivePetState({})).toBe('idle')
    expect(derivePetState({ awaitingInput: true })).toBe('waiting')
  })

  it('runs when busy or a tool is executing', () => {
    expect(derivePetState({ busy: true })).toBe('run')
    expect(derivePetState({ toolRunning: true })).toBe('run')
  })

  it('reviews while reasoning (below tool, above bare busy)', () => {
    expect(derivePetState({ reasoning: true })).toBe('review')
    expect(derivePetState({ reasoning: true, busy: true })).toBe('review')
    expect(derivePetState({ reasoning: true, toolRunning: true })).toBe('run')
  })

  it('waits (blocked on the user) above the in-flight signals', () => {
    expect(derivePetState({ awaitingInput: true, toolRunning: true, busy: true })).toBe('waiting')
    // but a finish beat still wins over waiting
    expect(derivePetState({ justCompleted: true, awaitingInput: true })).toBe('wave')
  })

  it('honors the full priority chain: error > celebrate > complete > tool', () => {
    expect(derivePetState({ error: true, celebrate: true, busy: true })).toBe('failed')
    expect(derivePetState({ celebrate: true, justCompleted: true, toolRunning: true })).toBe('jump')
    expect(derivePetState({ justCompleted: true, toolRunning: true })).toBe('wave')
  })
})

describe('roam motion', () => {
  it('only reports at-rest when the agent-driven state is plain idle', () => {
    $petActivity.set({})
    expect($petAtRest.get()).toBe(true)

    $petActivity.set({ busy: true })
    expect($petAtRest.get()).toBe(false)

    $petActivity.set({})
    expect($petAtRest.get()).toBe(true)
  })

  it('shows the roam pose while wandering, but never overrides real activity', () => {
    $petActivity.set({})
    $petMotion.set('run')
    expect($petState.get()).toBe('run')

    // Hops surface the jump pose.
    $petMotion.set('jump')
    expect($petState.get()).toBe('jump')

    // Activity wins over a wander in progress.
    $petActivity.set({ reasoning: true, busy: true })
    expect($petState.get()).toBe('review')

    // Back at rest, the wander resumes its pose; clearing it returns to idle.
    $petActivity.set({})
    expect($petState.get()).toBe('jump')
    $petMotion.set(null)
    expect($petState.get()).toBe('idle')

    $petActivity.set({})
  })
})

describe('$petRoamPaused (running-into-a-wall fix)', () => {
  it('shows idle instead of a stuck running pose when roam settles while still busy', () => {
    // Regression: the pet strolls into a wall/settles while the agent is
    // STILL busy. Roam correctly stops moving it ($petMotion -> null), but
    // the busy signal alone can't tell "roam has nothing to show" apart from
    // "roam isn't running at all" — so without $petRoamPaused the running-leg
    // pose kept playing forever against a pet that had already stopped.
    $petActivity.set({ busy: true })
    $petMotion.set('run')
    expect($petState.get()).toBe('run')

    // Roam settles into a loafing beat (beginPause in use-pet-roam.ts).
    $petMotion.set(null)
    $petRoamPaused.set(true)
    expect($petState.get()).toBe('idle')

    // Roam picks a new stroll target and starts walking again — the running
    // pose should resume even though the agent never stopped being busy.
    $petMotion.set('run')
    $petRoamPaused.set(false)
    expect($petState.get()).toBe('run')

    $petActivity.set({})
    $petMotion.set(null)
    $petRoamPaused.set(false)
  })

  it('never affects the busy pose when roam is disabled ($petRoamPaused stays false)', () => {
    // A roam-disabled pet (or the pop-out overlay / generate preview, neither
    // of which mount usePetRoam) never sets $petRoamPaused — this asserts the
    // fix is a no-op for that entire population.
    $petActivity.set({ busy: true })
    expect($petRoamPaused.get()).toBe(false)
    expect($petState.get()).toBe('run')

    $petActivity.set({})
  })

  it('does not interfere with non-run states (waiting/review/failed keep priority)', () => {
    $petRoamPaused.set(true)
    $petMotion.set(null)

    expect(derivePetState({ awaitingInput: true })).toBe('waiting')
    $petActivity.set({ awaitingInput: true })
    expect($petState.get()).toBe('waiting')

    $petActivity.set({})
    $petRoamPaused.set(false)
  })
})

describe('flashPetActivity', () => {
  it('clears stale sibling beats so a completion never inherits a prior error', () => {
    // A turn errors (sad), then the next turn finishes cleanly. The celebrate
    // beat must win — error is highest priority, so a merge-only flash would
    // keep the pet on the failed pose.
    setPetActivity({ error: true })
    flashPetActivity({ celebrate: true })

    expect($petActivity.get().error).toBe(false)
    expect($petState.get()).toBe('jump')

    setPetActivity({})
  })

  it('bumps $petJumpBeat on every celebrate call, even while already celebrating', () => {
    // Regression: $petState is a computed atom, and nanostores only notifies
    // listeners on a VALUE change. Two celebrate calls in a row both resolve
    // to the same 'jump' string, so $petState alone can't signal "replay the
    // bob" for a second click/beat landing inside the first one's decay
    // window (e.g. clicking the pet twice in quick succession). $petJumpBeat
    // must bump on every celebrate request regardless of the current pose.
    const before = $petJumpBeat.get()

    flashPetActivity({ celebrate: true })
    expect($petJumpBeat.get()).toBe(before + 1)

    // Still celebrating (same 'jump' state) — must bump again anyway.
    flashPetActivity({ celebrate: true })
    expect($petJumpBeat.get()).toBe(before + 2)

    setPetActivity({})
  })

  it('does not bump $petJumpBeat for non-celebrate beats', () => {
    const before = $petJumpBeat.get()

    flashPetActivity({ error: true })
    expect($petJumpBeat.get()).toBe(before)

    setPetActivity({})
  })
})
