import { atom } from 'nanostores'

export interface OnboardingSession {
  runtimeId: string
  storedId: string
}

/** The onboarding chat while a run is in progress in this window. */
export const $onboardingSession = atom<null | OnboardingSession>(null)

/** Whether `sessionId` is the runtime id of the onboarding chat in progress. */
export function isOnboardingChat(sessionId: null | string | undefined): boolean {
  return Boolean(sessionId) && $onboardingSession.get()?.runtimeId === sessionId
}
