import type { OnboardingStartResult } from '@hermes/shared'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $chatOnboardingSolo, $introView } from '@/store/onboarding-intro'
import { $onboardingPhase } from '@/store/onboarding-phase'
import { startOnboarding } from '@/store/onboarding-run'
import { $activeSessionId, $connection, $selectedStoredSessionId } from '@/store/session'

import { $introCopy } from './intro'
import { firstRunIntro } from './kickoff'

const size = vi.fn()
const started: OnboardingStartResult = { resumed: false, session_id: 'rt-1', stored_session_id: 'st-1' }
const ensureProfile = async (): Promise<void> => undefined

// SAFETY: the fake answers onboarding.start with its contract result; nothing else reads the answer.
const request = async <T>(): Promise<T> => started as T

// What wiring.tsx's adoptSessionRoute does: the stored chat becomes the selected, routed session.
const adopt = vi.fn(async (storedId: string) => {
  $selectedStoredSessionId.set(storedId)
  $activeSessionId.set('rt-1')
})

beforeEach(() => {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { chatOnboarding: { size }, guestOnboardingEnabled: true }
  })
  // SAFETY: factsSource reads only `mode`; a local connection skips the machine comparison.
  $connection.set({ mode: 'local' } as never)
  $onboardingPhase.set('idle')
  $introView.set('off')
  $selectedStoredSessionId.set(null)
  size.mockClear()
})

describe('first-run kickoff', () => {
  it('lands on the chat onboarding.start returned and opens the intro over it', async () => {
    const views: string[] = []
    const stop = $introView.subscribe(view => views.push(view))

    const opened = await startOnboarding(request, adopt, { ensureProfile, intro: firstRunIntro() })
    stop()

    expect(opened).toBe(true)
    expect(adopt).toHaveBeenCalledWith('st-1', true)
    expect($selectedStoredSessionId.get()).toBe('st-1')
    expect(views).toEqual(['off', 'starting', 'intro'])
    expect($chatOnboardingSolo.get()).toBe(true)
    expect(size).toHaveBeenCalledWith('onboarding')
    // A blank chat plays main's intro copy; the backend's greeting row replaces it.
    expect($introCopy.get()).toBe('playing')
  })
})
