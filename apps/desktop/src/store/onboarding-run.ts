/**
 * First-run onboarding as this window runs it: one chat in the default profile, opened by the
 * backend's `onboarding.start`, ended by the model (`setup_beat(beat="done")`) or by Skip setup.
 *
 * This module is the one writer of `$onboardingPhase` and of the `onboarding` presence surface. The
 * surface is open while a run is starting or its chat is on screen; in any other chat the app behaves
 * as it does outside onboarding (tips, the sign-in offer). The backend owns whether onboarding is due
 * (`onboarding.state`) and writes the install-level flag when a run ends.
 */

import type {
  MachineFactsResult,
  OnboardingRunStateResult,
  OnboardingStartResult
} from '@hermes/shared'
import { atom, computed } from 'nanostores'

import { getRuntimeI18nLocale } from '@/i18n/runtime'
import type { GatewayRequester } from '@/lib/yolo-session'

import { ackFreeTierNotice } from './free-tier'
import { activeGatewayConnectionId } from './gateway'
import { clearFreeTierIntro } from './onboarding'
import { $introView } from './onboarding-intro'
import { $onboardingPhase } from './onboarding-phase'
import { markOnboardingDecided, setOnboardingSurfaceActive } from './onboarding-presence'
import { $onboardingSession } from './onboarding-session'
import { $activeGatewayProfile, ensureGatewayAgent, ensureGatewayProfile, newSessionInProfile } from './profile'
import { $connection, $selectedStoredSessionId } from './session'
import type { SetupBeatToolResult } from './setup-beat'

export type OnboardingRequest = <T = unknown>(
  method: string,
  params?: Parameters<GatewayRequester>[1],
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<T>

// A cold first boot answers onboarding.start late; the intro's way-out offer bounds the wait instead.
const NO_DEADLINE = 0

/** Opens a stored session on screen, replacing the route (`useSessionActions().resumeSession`). */
export type OnboardingResume = (storedId: string, replaceRoute?: boolean) => Promise<void>

/** How the last run in this window ended; the end-of-onboarding surfaces wake on it. */
export const $onboardingEnded = atom<'done' | 'skipped' | null>(null)

/** When the end-of-onboarding surfaces (the local-model offer) may start their settle wait: the run
 *  ended, or the user first left its chat mid-run. Null until then. */
export const $onboardingSettleFrom = atom<null | number>(null)

// The backend runs onboarding in its root profile whichever profile the app was on.
const ONBOARDING_PROFILE = 'default'

/** The profile (and registry source) the window was on before the run moved it to the default profile. */
interface LaunchSource {
  connectionId: null | string
  profile: string
}

let launch: LaunchSource | null = null

/** The run's chat has been on screen at least once; until then a route still opening is not a leave. */
const $onboardingChatSeen = atom(false)

/** A run is starting (its chat not on screen yet), or its chat is the one on screen. */
const $onboardingOnScreen = computed(
  [$onboardingPhase, $onboardingSession, $selectedStoredSessionId, $onboardingChatSeen],
  (phase, session, selected, seen) => phase === 'shown' && (!session || !seen || session.storedId === selected)
)

computed([$onboardingSession, $selectedStoredSessionId], (session, selected) =>
  Boolean(session && session.storedId === selected)
).subscribe(onChat => {
  if (onChat) {
    $onboardingChatSeen.set(true)
  }
})

$onboardingOnScreen.subscribe(onScreen => {
  setOnboardingSurfaceActive('onboarding', onScreen)

  if (!onScreen && $onboardingPhase.get() === 'shown' && $onboardingSettleFrom.get() === null) {
    $onboardingSettleFrom.set(Date.now())
  }
})

/** Same machine as this app (`local`), or a backend somewhere else (`remote`). A local connection is
 *  this machine; any other compares the backend's OS and architecture with Electron's. */
async function factsSource(request: OnboardingRequest): Promise<'local' | 'remote'> {
  if ($connection.get()?.mode === 'local') {
    return 'local'
  }

  const [profile, facts] = await Promise.all([
    window.hermesDesktop?.getMachineProfile?.().catch(() => null),
    request<MachineFactsResult>('machine.facts').catch(() => null)
  ])

  const arch = (value: null | string | undefined) => (value === 'x64' || value === 'amd64' ? 'x86_64' : value)

  const same =
    Boolean(profile && facts) &&
    profile?.platform === facts?.machine.os_family &&
    arch(profile?.arch) === arch(facts?.machine.native_arch)

  return same ? 'local' : 'remote'
}

/** The first-run intro around a start (`components/onboarding/first-run/kickoff.ts`). */
export interface OnboardingIntroHooks {
  /** Before `onboarding.start`: the boot overlay holds. `abandon`: the user went on without setup; the
   *  start fails and the chat stays closed. `seen`: the user left this run's intro on an earlier launch. */
  starting: (abandon: () => void, seen: boolean) => void
  /** The start answered, the chat not open yet. */
  started: (started: OnboardingStartResult) => void
  /** The chat is the one on screen. */
  opened: (started: OnboardingStartResult) => void
  failed: () => void
}

interface StartOptions {
  /** Points the window's gateway at the profile onboarding runs in (`store/profile.ts::ensureGatewayProfile`). */
  ensureProfile?: (profile: string) => Promise<void>
  intro?: OnboardingIntroHooks
  /** The user left this run's intro on an earlier launch (`onboarding.intro_seen`). */
  introSeen?: boolean
  rerun?: boolean
}

/** Open the onboarding chat (a new one for a rerun, else the run in progress or a new one). */
export async function startOnboarding(
  request: OnboardingRequest,
  resume: OnboardingResume,
  { ensureProfile = ensureGatewayProfile, intro, introSeen = false, rerun = false }: StartOptions = {}
): Promise<boolean> {
  $onboardingPhase.set('shown')
  $onboardingEnded.set(null)
  $onboardingChatSeen.set(false)

  const abandon = new AbortController()
  intro?.starting(() => abandon.abort(), introSeen)
  launch = { connectionId: activeGatewayConnectionId(), profile: $activeGatewayProfile.get() }

  try {
    await ensureProfile(ONBOARDING_PROFILE)

    const started = await request<OnboardingStartResult>(
      'onboarding.start',
      { facts_source: await factsSource(request), locale: getRuntimeI18nLocale(), rerun },
      NO_DEADLINE,
      abandon.signal
    )

    intro?.started(started)

    // The chat is on screen before the run names it, so the presence never sees a gap.
    await resume(started.stored_session_id, true)
    $onboardingSession.set({ runtimeId: started.session_id, storedId: started.stored_session_id })

    if (intro) {
      intro.opened(started)
      ackFreeTierIntro(request)
      markSeenOnLeave(request)
    }

    return true
  } catch (error) {
    // Going on without setup is the user's choice, not an error.
    if (!abandon.signal.aborted) {
      console.warn('[onboarding] could not start', error)
    }

    $onboardingSession.set(null)
    $onboardingPhase.set('failed')
    intro?.failed()
    await returnToLaunchProfile(ensureProfile)

    return false
  }
}

// The onboarding intro is the free-tier introduction, as on main: the ready screen must not follow it.
function ackFreeTierIntro(request: OnboardingRequest): void {
  clearFreeTierIntro()
  void ackFreeTierNotice(request).then(acked => {
    if (acked) {
      clearFreeTierIntro()
    }
  })
}

// Main's rule: once the user leaves the intro mid-run, a relaunch reopens the chat without the demo.
function markSeenOnLeave(request: OnboardingRequest): void {
  if ($introView.get() !== 'intro') {
    return
  }

  const stop = $introView.listen(view => {
    stop()

    if (view === 'ended' && $onboardingPhase.get() === 'shown') {
      void request('onboarding.intro_seen').catch(error => console.warn('[onboarding] intro_seen failed', error))
    }
  })
}

// Like main's failed kickoff: the window goes back to where it was before the switch.
async function returnToLaunchProfile(ensureProfile: (profile: string) => Promise<void>): Promise<void> {
  if (!launch) {
    return
  }

  const { connectionId, profile } = launch

  try {
    await (connectionId ? ensureGatewayAgent(connectionId, profile) : ensureProfile(profile))
  } catch (error) {
    console.warn('[onboarding] could not return to the launch profile', error)
  }
}

let decision: null | Promise<void> = null

/**
 * The main window's boot check, once per launch: open onboarding when the backend says it is due on
 * this install and the free tier can drive it. First-run surfaces wait until this has answered; the
 * presence surface is set before the answer is published, so they never see a clear moment in between.
 */
export function decideOnboarding(
  request: OnboardingRequest,
  resume: OnboardingResume,
  options?: StartOptions
): Promise<void> {
  decision ??= readAndStart(request, resume, options)

  return decision
}

async function readAndStart(request: OnboardingRequest, resume: OnboardingResume, options?: StartOptions): Promise<void> {
  try {
    const state = await request<OnboardingRunStateResult>('onboarding.state')

    if (state.run && state.eligible) {
      await startOnboarding(request, resume, { ...options, introSeen: state.intro_seen })
    }
  } catch (error) {
    console.warn('[onboarding] state could not be read', error)
  } finally {
    markOnboardingDecided()
  }
}

/** The run ended: the model finished it or the user skipped. Later calls for the same run do nothing. */
export function endOnboarding(outcome: 'done' | 'skipped'): void {
  if ($onboardingPhase.get() !== 'shown') {
    return
  }

  $onboardingPhase.set(outcome)
  $onboardingEnded.set(outcome)
  $onboardingSession.set(null)
  $onboardingSettleFrom.set($onboardingSettleFrom.get() ?? Date.now())
}

/** Skip setup: the backend ends the run and closes the card that waits, and a new chat opens in the
 *  profile the window launched on (main's rule). A failed skip leaves the run going, so Skip setup
 *  stays on offer; the caller reports the error. */
export async function skipOnboarding(request: OnboardingRequest, runtimeId: string): Promise<void> {
  await request('onboarding.skip', { session_id: runtimeId })
  endOnboarding('skipped')
  newSessionInProfile(launch?.profile ?? ONBOARDING_PROFILE)
}

/** A `setup_beat` result that ends the run: `completed` (the model said done) or `skipped_all`. `ids`:
 *  the event's runtime and stored session; a rerun closes the old chat's card, which must not end the new run. */
export function noteSetupBeatResult(ids: readonly (null | string)[], result: SetupBeatToolResult): void {
  const session = $onboardingSession.get()

  if (!session || !ids.some(id => id === session.runtimeId || id === session.storedId)) {
    return
  }

  if (result.completed) {
    endOnboarding('done')
  } else if (result.skipped_all) {
    endOnboarding('skipped')
  }
}
