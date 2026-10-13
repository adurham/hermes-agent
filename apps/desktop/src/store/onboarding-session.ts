import { atom } from 'nanostores'

import { storedSessionIdForRuntimeId } from './session-states'

export interface OnboardingSession {
  /** Names the run in `onboarding.ended` and `onboarding.state`. */
  runId: string
  runtimeId: string
  storedId: string
}

/** The onboarding chat while a run is in progress in this window. */
export const $onboardingSession = atom<null | OnboardingSession>(null)

/** Whether runtime id `sessionId` shows the onboarding chat in progress. Matched by the stored id too:
 *  a resume after a reconnect or a backend restart gives the chat a new runtime id. */
export function isOnboardingChat(sessionId: null | string | undefined): boolean {
  const session = $onboardingSession.get()

  return Boolean(
    sessionId && session && (session.runtimeId === sessionId || storedSessionIdForRuntimeId(sessionId) === session.storedId)
  )
}
