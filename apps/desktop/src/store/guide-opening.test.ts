import { expect, it, vi } from 'vitest'

it('keeps the opening visible through a shared kickoff and settles only after the seed is durable', async () => {
  vi.stubGlobal('hermesDesktop', { guestOnboardingEnabled: true })
  const { $guideOpening, $onboardingGate, runGuideKickoff, skipGuide } = await import('./onboarding-gate')
  $onboardingGate.set({ phase: 'cinematic', guideQueued: true, guideKickoff: 'idle' })

  let finishSeed: (ready: boolean) => void = () => {}

  const seed = new Promise<boolean>(resolve => {
    finishSeed = resolve
  })

  const kickoff = vi.fn(() => seed)
  expect($guideOpening.get()).toBe(true)

  const first = runGuideKickoff(kickoff)
  const second = runGuideKickoff(kickoff)
  expect(first).toBe(second)
  expect($onboardingGate.get().guideKickoff).toBe('starting')
  expect($guideOpening.get()).toBe(true)
  finishSeed(true)
  await first
  expect(kickoff).toHaveBeenCalledOnce()
  expect($guideOpening.get()).toBe(false)
  expect($onboardingGate.get().phase).toBe('guided')
  skipGuide()
  expect($guideOpening.get()).toBe(false)
  vi.unstubAllGlobals()
})
