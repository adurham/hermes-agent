import { atom, computed } from 'nanostores'

import { persistBoolean, storedBoolean } from '@/lib/storage'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $busy } from '@/store/session'

/**
 * Petdex mascot state for the desktop floating pet.
 *
 * The spritesheet payload comes from the gateway `pet.info` RPC (shared with
 * the TUI). The animation *state* is derived here from the same activity
 * signals the chat already tracks, mirroring the priority order documented in
 * `agent/pet/state.py` so the Python and TS surfaces never drift.
 */

export type PetState = 'idle' | 'wave' | 'run' | 'failed' | 'review' | 'jump' | 'waiting'

export interface PetInfo {
  enabled: boolean
  slug?: string
  displayName?: string
  mime?: string
  spritesheetBase64?: string
  // Stable sheet revision (`mtime_ns:size`) from the gateway; lets the desktop
  // skip full sprite payload refreshes when the active pet hasn't changed.
  spritesheetRevision?: string
  frameW?: number
  frameH?: number
  framesPerState?: number
  // Real (padding-trimmed) frame count per state row, from the engine. Lets the
  // canvas step only frames that exist instead of a fixed framesPerState, which
  // would animate into the transparent padding of ragged sheets (blank flash).
  framesByState?: Record<string, number>
  // Concrete Codex row counts (e.g. running-right may have 8 frames even though
  // the Hermes "run" activity state uses the in-place running row).
  framesByRow?: Record<string, number>
  loopMs?: number
  scale?: number
  stateRows?: string[]
}

export interface PetActivity {
  busy?: boolean
  awaitingInput?: boolean
  toolRunning?: boolean
  reasoning?: boolean
  error?: boolean
  justCompleted?: boolean
  celebrate?: boolean
}

/**
 * Resolve the animation state from coarse activity signals.
 *
 * Priority (highest first) mirrors `agent.pet.state.derive_pet_state`:
 * error → celebrate → justCompleted → awaitingInput → toolRunning → reasoning →
 * busy → idle. `awaitingInput` (a clarify/approval blocking on the user) outranks
 * the in-flight signals because the turn is paused on you, not working.
 */
export function derivePetState(activity: PetActivity): PetState {
  if (activity.error) {
    return 'failed'
  }

  if (activity.celebrate) {
    return 'jump'
  }

  if (activity.justCompleted) {
    return 'wave'
  }

  if (activity.awaitingInput) {
    return 'waiting'
  }

  if (activity.toolRunning) {
    return 'run'
  }

  if (activity.reasoning) {
    return 'review'
  }

  if (activity.busy) {
    return 'run'
  }

  return 'idle'
}

export const $petInfo = atom<PetInfo>({ enabled: false })
export const $petActivity = atom<PetActivity>({})

/** Pet installed + enabled with a loaded spritesheet (ready to show/react). */
export const $petActive = computed($petInfo, info => info.enabled && Boolean(info.spritesheetBase64))

/**
 * Profile the pet RPCs should resolve against. Pets are per-profile — the active
 * pet (`display.pet.*`) and the installed sprites live under each profile's
 * HERMES_HOME — so every pet RPC carries this. The gateway no-ops it for the
 * launch profile (own-profile backends already resolve it) and rebinds for any
 * other profile, which is what makes per-profile pets work in app-global remote
 * mode (one backend serving every profile).
 */
export function petProfile(): string {
  return normalizeProfileKey($activeGatewayProfile.get())
}

/**
 * Pet-local "you have a new message" flag, surfaced as the overlay's mail icon.
 * Deliberately not real unread tracking: it flips on when a turn finishes while
 * the app isn't focused, and off when the user opens the app via the mail icon
 * (or returns to the window). No persistence — it's a glance hint, not state.
 */
export const $petUnread = atom(false)
export const markPetUnread = () => $petUnread.set(true)
export const clearPetUnread = () => $petUnread.set(false)

/**
 * Pet zone: confine the pet to a dedicated layout pane instead of the full
 * window. Persisted per-device (like roam), not per-profile.
 */
const PET_ZONE_KEY = 'hermes.desktop.pet-zone.v1'
export const $petZoneEnabled = atom<boolean>(storedBoolean(PET_ZONE_KEY, false))

export const setPetZoneEnabled = (on: boolean) => {
  $petZoneEnabled.set(on)
  persistBoolean(PET_ZONE_KEY, on)
}

/** Steady activity flags (toolRunning / reasoning) set + cleared by the stream. */
export const setPetActivity = (next: Partial<PetActivity>) => $petActivity.set({ ...$petActivity.get(), ...next })

let flashTimer: ReturnType<typeof setTimeout> | undefined

/**
 * Monotonic nonce, bumped every time a `celebrate` beat is requested via
 * {@link flashPetActivity} — including repeat requests while a previous
 * celebrate beat is still decaying. This exists because `$petState` is a
 * `computed` atom: nanostores' `atom.set()` only notifies listeners when the
 * VALUE changes, and `derivePetState()` still resolves to the same string
 * `'jump'` on a repeat celebrate (e.g. clicking the pet again before the
 * first beat's 1.6s decay finishes) — so `$petState` silently no-ops and any
 * effect keyed on a `!== 'jump'` → `'jump'` transition never re-fires.
 * Consumers that need to replay a one-shot effect (the jump bob) on every
 * celebrate request, not just the first, should listen to this instead of
 * (or alongside) `$petState`.
 */
export const $petJumpBeat = atom<number>(0)
export const triggerPetJumpBeat = () => $petJumpBeat.set($petJumpBeat.get() + 1)

/** Fire a transient reaction beat (error / celebrate / justCompleted) that
 *  decays back to the steady state after `ms`.
 *
 *  Each beat first clears its siblings so a stale one can't win the priority
 *  race: without this, a completion beat (`celebrate`) would merge on top of a
 *  lingering `error`, and `derivePetState` checks `error` first — so a clean
 *  finish would render the sad/failed pose. */
export const flashPetActivity = (next: Partial<PetActivity>, ms = 1600) => {
  setPetActivity({ celebrate: false, error: false, justCompleted: false, ...next })

  if (next.celebrate) {
    triggerPetJumpBeat()
  }

  clearTimeout(flashTimer)
  flashTimer = setTimeout(() => setPetActivity({ celebrate: false, error: false, justCompleted: false }), ms)
}

export const setPetInfo = (info: PetInfo) => $petInfo.set(info)

/**
 * Resolve the live activity state from the dedicated activity atom, falling back
 * to the always-present `$busy` chat signal so the pet reacts out of the box.
 *
 * `awaitingInput` (a clarify/approval blocking on the user) is an explicit flag
 * on `$petActivity` — set by the controller from `$attentionSessionIds` and
 * mirrored to the pop-out overlay through the same atom, so both surfaces agree
 * without the overlay needing the session list.
 */
function deriveLivePetState(activity: PetActivity, busy: boolean): PetState {
  const live = activity.busy ?? busy

  return derivePetState({
    busy: live,
    awaitingInput: activity.awaitingInput,
    // Steady flags only count mid-turn — ignore stale ones once at rest so an
    // interrupted turn can't pin the pet on `run`/`review`.
    toolRunning: live && activity.toolRunning,
    reasoning: live && activity.reasoning,
    error: activity.error,
    justCompleted: activity.justCompleted,
    celebrate: activity.celebrate
  })
}

/**
 * Opt-in: let the floating mascot wander around the window on its own while
 * idle. Pure desktop-client behavior (no agent/config dependency), so it lives
 * in localStorage like the pet's drag position — per-device, not per-profile.
 */
const ROAM_KEY = 'hermes.desktop.pet-roam.v1'
export const $petRoam = atom<boolean>(storedBoolean(ROAM_KEY, false))

export const setPetRoam = (on: boolean) => {
  $petRoam.set(on)
  persistBoolean(ROAM_KEY, on)
}

/**
 * The pose the roam loop is currently driving: `run` while walking a surface,
 * `jump` while hopping/falling between surfaces, or `null` at rest. Surfaced
 * through `$petState` (below) so the canvas animates the wander without any prop
 * change or re-render — it already subscribes to `$petState`.
 */
export const $petMotion = atom<PetState | null>(null)

/**
 * True while the roam loop is physically moving the pet's `top` between
 * ledges (spring-up hop or fall), false otherwise — including while a
 * *stationary* jump pose plays (idle fidget, click-to-pet, turn-end
 * celebrate). `$petMotion`/`$petState` alone can't make this distinction:
 * the idle fidget writes `jump` onto the very same `$petMotion` channel the
 * roam loop's hop uses (see `floating-pet.tsx`'s fidget effect), so
 * `petState === 'jump'` is true in both cases. Consumers that want to play a
 * vertical "hop in place" animation for the stationary case must skip it
 * while this is true — the roam loop is already moving the DOM node itself,
 * and a second, independent vertical animation on top would fight it.
 */
export const $petRoamAirborne = atom<boolean>(false)

/**
 * Horizontal travel direction while roaming: -1 left, 1 right, 0 not walking.
 * The floating pet maps this to the directional run row + mirror, keeping the
 * wander loop free of sprite-row knowledge.
 */
export const $petRoamDir = atom<-1 | 0 | 1>(0)

/**
 * Whether the agent-driven state is at rest (plain `idle`). The idle-fidget
 * effect gates on this — never on `$petState` itself, which would feed back
 * on its own `$petMotion`-driven pose.
 */
export const $petAtRest = computed(
  [$petActivity, $busy],
  (activity, busy): boolean => deriveLivePetState(activity, busy) === 'idle'
)

/**
 * Whether the roam loop should be allowed to keep pacing right now. Broader
 * than `$petAtRest`: ordinary work (`run`/`review`) doesn't freeze the pet —
 * "thinking" or "running a tool" is exactly when idle pacing reads as most
 * alive, and those states already render with the same running-leg rows the
 * walk animation uses, so a stride mid-tool-call looks identical to a
 * stride at idle. Only the states meant to grab the user's attention with a
 * DISTINCT, stationary pose (`failed`, `waiting`, `wave`, `jump`) pause the
 * wander — `usePetRoam`'s `enabled` flipping false decays `$petRoamDir` to 0
 * within a frame, which is what actually stops `PetSprite`'s `rowOverride`
 * from masking that pose (see `roamWalkRow`: no override once dir is 0).
 */
export const $petCanRoam = computed([$petActivity, $busy], (activity, busy): boolean => {
  const state = deriveLivePetState(activity, busy)

  return state === 'idle' || state === 'run' || state === 'review'
})

/**
 * The live pet state. Activity always wins; only when the agent is at rest does
 * a roam pose (walking → `run`, hopping → `jump`) show through, so the wander
 * reads as deliberate movement.
 */
export const $petState = computed([$petActivity, $busy, $petMotion], (activity, busy, motion): PetState => {
  const base = deriveLivePetState(activity, busy)

  return base === 'idle' && motion ? motion : base
})

/**
 * Real agent-activity state, ignoring any roam/fidget pose. `$petState` is
 * the right thing to feed the SPRITE (roaming should read as movement), but
 * it is the WRONG thing to feed a status readout like `PetBubble`: the roam
 * loop's own `run`/`jump` motion is indistinguishable from genuine tool
 * activity once merged into `$petState`, so a wandering-but-idle pet would
 * show a "working…" bubble for a walk that isn't work. Consumers that show
 * agent-status TEXT (not just the sprite pose) should read this instead.
 */
export const $petRealState = computed([$petActivity, $busy], deriveLivePetState)
