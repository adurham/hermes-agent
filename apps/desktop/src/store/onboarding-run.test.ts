import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $introView } from './onboarding-intro'
import { $onboardingPhase } from './onboarding-phase'
import { $onboardingDecided, $onboardingSurfaceClear } from './onboarding-presence'
import {
  $onboardingEnded,
  $onboardingSettleFrom,
  decideOnboarding,
  noteSetupBeatResult,
  skipOnboarding,
  startOnboarding
} from './onboarding-run'
import { $activeGatewayProfile, $freshSessionRequest, $newChatProfile } from './profile'
import { $connection, $selectedStoredSessionId } from './session'

const ensureProfile = async (): Promise<void> => undefined

// What resumeSession does for the test: the stored chat becomes the one on screen.
const resume = vi.fn(async (storedId: string) => $selectedStoredSessionId.set(storedId))

function backend(state: { eligible: boolean; run: boolean }) {
  const calls: string[] = []
  const started = { resumed: false, session_id: 'rt-1', stored_session_id: 'st-1' }

  const request = async <T,>(method: string): Promise<T> => {
    calls.push(method)

    // SAFETY: the fake answers onboarding.state and onboarding.start with their contract result shapes.
    return (method === 'onboarding.state' ? state : started) as T
  }

  return { calls, request }
}

beforeEach(() => {
  // SAFETY: factsSource reads only `mode`; a local connection skips the machine comparison.
  $connection.set({ mode: 'local' } as never)
  $onboardingPhase.set('idle')
  $onboardingEnded.set(null)
  $onboardingSettleFrom.set(null)
  $selectedStoredSessionId.set(null)
})

describe('onboarding run', () => {
  it('opens the onboarding chat when due and never shows a clear moment before it', async () => {
    const clear: boolean[] = []
    const stop = $onboardingSurfaceClear.subscribe(value => clear.push(value))
    const { calls, request } = backend({ eligible: true, run: true })

    await decideOnboarding(request, resume, { ensureProfile })
    stop()

    expect(calls).toEqual(['onboarding.state', 'onboarding.start'])
    expect(resume).toHaveBeenCalledWith('st-1', true)
    expect($onboardingDecided.get()).toBe(true)
    expect(clear).not.toContain(true)
  })

  it('ends once on the model finishing, and frees the first-run surfaces', async () => {
    const { request } = backend({ eligible: true, run: true })

    await startOnboarding(request, resume, { ensureProfile })
    noteSetupBeatResult(['rt-1', 'st-1'], { completed: true })
    noteSetupBeatResult(['rt-1', 'st-1'], { skipped_all: true })

    expect($onboardingPhase.get()).toBe('done')
    expect($onboardingEnded.get()).toBe('done')
    expect($onboardingSurfaceClear.get()).toBe(true)
  })

  it('frees the first-run surfaces in another chat mid-run and arms the end-of-onboarding wait', async () => {
    const { request } = backend({ eligible: true, run: true })

    await startOnboarding(request, resume, { ensureProfile })
    expect($onboardingSurfaceClear.get()).toBe(false)

    $selectedStoredSessionId.set('another-chat')

    expect($onboardingPhase.get()).toBe('shown')
    expect($onboardingSurfaceClear.get()).toBe(true)
    expect($onboardingSettleFrom.get()).not.toBeNull()
  })

  it('ignores a setup_beat result from another chat, like the old run a rerun closed', async () => {
    const { request } = backend({ eligible: true, run: true })
    await startOnboarding(request, resume, { ensureProfile })

    noteSetupBeatResult(['rt-old', 'st-old'], { skipped_all: true })

    expect($onboardingPhase.get()).toBe('shown')
    expect($onboardingEnded.get()).toBeNull()
  })

  it('returns the window to the profile it launched on when the start fails', async () => {
    $activeGatewayProfile.set('work')
    const switched = vi.fn(async (_profile: string) => undefined)
    const refuse = async (): Promise<never> => Promise.reject(new Error('backend down'))

    expect(await startOnboarding(refuse, resume, { ensureProfile: switched })).toBe(false)
    expect(switched.mock.calls).toEqual([['default'], ['work']])
    $activeGatewayProfile.set('default')
  })

  it('opens a new chat in the launch profile on Skip setup', async () => {
    $activeGatewayProfile.set('work')
    const { request } = backend({ eligible: true, run: true })
    await startOnboarding(request, resume, { ensureProfile })
    const fresh = $freshSessionRequest.get()

    await skipOnboarding(request, 'rt-1')
    $activeGatewayProfile.set('default')

    expect($onboardingEnded.get()).toBe('skipped')
    expect($freshSessionRequest.get()).toBe(fresh + 1)
    expect($newChatProfile.get()).toBe('work')
  })

  it('acks the free-tier notice when the onboarding intro opens', async () => {
    const { calls, request } = backend({ eligible: true, run: true })
    const intro = { failed: vi.fn(), opened: vi.fn(), started: vi.fn(), starting: vi.fn() }

    await startOnboarding(request, resume, { ensureProfile, intro })

    expect(intro.opened).toHaveBeenCalled()
    expect(calls).toContain('free_tier.ack_notice')
  })

  it('records the intro as seen when the user first leaves it mid-run', async () => {
    const { calls, request } = backend({ eligible: true, run: true })
    const intro = { failed: vi.fn(), opened: () => $introView.set('intro'), started: vi.fn(), starting: vi.fn() }

    await startOnboarding(request, resume, { ensureProfile, intro })
    $introView.set('ended')
    $introView.set('off')

    expect(calls.filter(method => method === 'onboarding.mark_seen')).toHaveLength(1)
  })

  it('keeps the run when the backend refuses the skip', async () => {
    const { request } = backend({ eligible: true, run: true })
    await startOnboarding(request, resume, { ensureProfile })

    const refuse = async (): Promise<never> => Promise.reject(new Error('skip refused'))

    await expect(skipOnboarding(refuse, 'rt-1')).rejects.toThrow('skip refused')
    expect($onboardingPhase.get()).toBe('shown')
    expect($onboardingEnded.get()).toBeNull()
  })
})
