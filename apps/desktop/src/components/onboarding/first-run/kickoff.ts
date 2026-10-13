import type { OnboardingStartResult } from '@hermes/shared'

import { chatMessageText } from '@/lib/chat-messages'
import { BACKEND_BOOT_WAIT_TIMEOUT_MS } from '@/lib/with-timeout'
import { $introStartExit } from '@/store/onboarding-intro'
import { $onboardingEnded, type OnboardingIntroHooks } from '@/store/onboarding-run'
import { $activeSessionId } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { $chatOnboardingThreadIds, openDemoLayout } from './assembly'
import { $introTurnSent, failIntro, leaveIntro, openIntro, startIntro } from './intro'

// The opening turn writes the scripted greeting; a chat that already shows it (a relaunch) skips the copy.
function hasAssistantWords(runtimeId: null | string): boolean {
  const messages = runtimeId ? ($sessionStates.get()[runtimeId]?.messages ?? []) : []

  return messages.some(message => message.role === 'assistant' && !message.hidden && chatMessageText(message).trim())
}

/**
 * Main's first-run start around `onboarding.start`: the boot overlay holds, the window takes the demo
 * layout once the start answers, and the intro opens once the returned chat is the one on screen.
 * Past the boot budget the starting screen offers a way out; the chat then stays closed this launch.
 */
export function firstRunIntro(): OnboardingIntroHooks {
  let abandoned = false
  let offerExit: ReturnType<typeof setTimeout> | undefined

  const clearExit = () => {
    clearTimeout(offerExit)
    $introStartExit.set(null)
  }

  return {
    starting: () => {
      startIntro()
      $introTurnSent.set(false)
      offerExit = setTimeout(
        () =>
          $introStartExit.set(() => {
            abandoned = true
            clearExit()
            failIntro()
          }),
        BACKEND_BOOT_WAIT_TIMEOUT_MS
      )
    },
    started: () => {
      clearExit()

      if (abandoned) {
        return false
      }

      openDemoLayout()

      return true
    },
    opened: (started: OnboardingStartResult) => {
      const runtimeId = $activeSessionId.get()

      $chatOnboardingThreadIds.set([
        ...new Set([started.stored_session_id, started.session_id, ...(runtimeId ? [runtimeId] : [])])
      ])
      // onboarding.start has already submitted the opening turn.
      $introTurnSent.set(true)
      openIntro(!hasAssistantWords(runtimeId))
    },
    failed: () => {
      clearExit()
      failIntro()
    }
  }
}

// The run ended (the model said done, or Skip setup): the intro ends with it.
$onboardingEnded.listen(outcome => {
  if (outcome) {
    leaveIntro()
  }
})
