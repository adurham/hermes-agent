import { getApiRequestConnection, getApiRequestProfile, setSttLease } from '@/hermes'

import type { AudioSttLeaseResponse } from '@/types/hermes'

// The desktop's voice-input sessions — push-to-talk dictation and the voice
// conversation loop — are the user telling us STT is about to be needed (or no
// longer is). The backend turns that into engine lifecycle: acquiring a lease
// pre-loads the local faster-whisper model (first-use download + load) so the
// transcription request starts hot instead of paying the cold cost inside its
// timeout (issue #105955); releasing drops the refcount but keeps the shared
// model resident for the gateway/CLI surfaces.
//
// This module is the renderer's single choke point for that signal. It dedupes
// (recorder and conversation loop observe the same mic session), serializes
// per lease so a fast on→off→on can't be reordered on the wire, and never
// surfaces failures — warm-up is an optimization; recording must not depend
// on it.
//
// Three invariants the backend's lease semantics force (they diverge from the
// TTS mirror this was drawn from — the STT engine does NOT pin the model while
// a lease is held):
//
// 1. Registration ≠ readiness. Knowing the backend holds the lease says
//    nothing about the model being warm: the idle-unload watcher can evict it
//    mid-conversation (during a long reply or a mute), and a failed warm-up
//    comes back as HTTP 200 with `action: 'error'`. Warm-up is therefore
//    VALIDATED on every acquire outcome and revalidated on later listening
//    starts (a fresh validation is trusted for a short window only) — never
//    remembered as permanent.
// 2. Readiness is bounded and carried into transcription. Awaiting
//    `syncSttLease(lease, true, owner)` resolves only when the current
//    warm-up has settled within its own request budget — the hooks use that
//    as a readiness barrier before submitting audio, so the transcription
//    request's deadline measures decoding, not the loader's cold start
//    (#105955 review: a 190 s cold load + a 3 s recording used to exhaust
//    the 180 s transcription floor before decoding began).
// 3. A lease belongs to the connection/profile that acquired it. The owner
//    is captured when the intent is registered and carried on the queued wire
//    call, so switching gateway/profile mid-flight sends the release to the
//    scope that holds the lease — and a new owner's acquire warms its own
//    backend instead of being swallowed by the old owner's remembered state.
//    Queue and dedupe identity both include the owner.

// Per-renderer id so two windows recording at once hold DISTINCT leases —
// window A finishing must not release the engine window B is still using.
const RENDERER_ID = Math.random().toString(36).slice(2, 10)

export const VOICE_INPUT_LEASE = `desktop:voice-input:${RENDERER_ID}`

/**
 * How long a validated warm-up is trusted before the next listening start
 * revalidates it with the backend. The STT idle-unload watcher can evict the
 * model at any configured moment, so readiness is only ever fresh for a short
 * window — a cache-hit revalidate is cheap; a suppressed re-warm after an
 * eviction makes the next transcription pay the cold load again.
 */
const STT_WARM_REVALIDATE_MS_DEFAULT = 60_000
let sttWarmRevalidateMs = STT_WARM_REVALIDATE_MS_DEFAULT

/**
 * The backend connection/profile that owns a lease operation. `undefined` →
 * the ambient scope captured when the intent is registered (single-profile
 * windows). An explicit `{ connectionId: 'local', profile }` stays explicit
 * through the queue so a later primary/remote selection cannot reinterpret it.
 */
export type SttLeaseOwner = undefined | { connectionId?: null | string; profile?: null | string }

interface Owner {
  connectionId: null | string
  profile: null | string
}

function resolveOwner(owner: SttLeaseOwner): Owner {
  if (owner === undefined) {
    return { connectionId: getApiRequestConnection(), profile: getApiRequestProfile() }
  }

  return {
    connectionId: owner.connectionId === undefined ? getApiRequestConnection() : owner.connectionId,
    profile: owner.profile === undefined ? getApiRequestProfile() : owner.profile
  }
}

/** Queue/dedupe identity: the same lease on a different owner is a different lease. */
function keyFor(lease: string, owner: Owner): string {
  return `${lease}::${owner.connectionId ?? ''}::${owner.profile ?? ''}`
}

/** Validated outcome of an acquire: warm, settled-nothing-to-warm (cloud), or cold (must retry). */
type WarmState = 'cold' | 'noop' | 'warm'

/**
 * A settled acquire is an honest warm-up only when the backend warmed (or had)
 * the model. `noop` is the legitimate cloud/local_command-with-no-model
 * outcome — settled, nothing to warm, not an error. `error` (and `ok: false`)
 * means the model is NOT hot regardless of the HTTP 200, so the next listen
 * must retry instead of trusting the registration.
 */
function classifyAcquire(response: AudioSttLeaseResponse): WarmState {
  if (!response?.ok || response.action === 'error') {
    return 'cold'
  }

  if (response.warmed === true || response.action === 'cached' || response.action === 'loaded') {
    return 'warm'
  }

  return 'noop'
}

/** Latest registered intent per (lease, owner): true = acquire, false = release. */
const intent = new Map<string, boolean>()
/** Last acquire's validated warm-up per (lease, owner), with when it was validated. */
const warm = new Map<string, { at: number; state: WarmState }>()
const inFlight = new Map<string, Promise<void>>()
/** Keys with an acquire queued or on the wire — later identical acquires ride that promise. */
const acquiring = new Set<string>()

/**
 * Bring the backend's view of `lease` in line with `active`, owned by `owner`.
 * Idempotent within the freshness window: a repeat acquire while a validated
 * warm-up is fresh (or an identical acquire is already in flight) is a no-op —
 * but a stale/failed validation re-warms, because unlike TTS the backend does
 * not pin the model while a lease is held. The initial `false` (nothing was
 * ever acquired) is also skipped — releasing a lease we never held would only
 * churn the backend on app start.
 */
export function syncSttLease(lease: string, active: boolean, owner?: SttLeaseOwner): Promise<void> {
  const scope = resolveOwner(owner)
  const key = keyFor(lease, scope)

  if (!active && intent.get(key) !== true) {
    // Never held (or already released): nothing to send. A release queued
    // behind an in-flight acquire has intent true and rides the chain below.
    return inFlight.get(key) ?? Promise.resolve()
  }

  if (active) {
    // An identical acquire is already going out — dedupe at the source so the
    // wire sees one acquire, and callers (the readiness barrier) can await it.
    if (acquiring.has(key)) {
      return inFlight.get(key) ?? Promise.resolve()
    }

    const warmState = warm.get(key)
    if (
      intent.get(key) === true &&
      warmState &&
      warmState.state !== 'cold' &&
      Date.now() - warmState.at < sttWarmRevalidateMs
    ) {
      // Registered AND readiness freshly validated — nothing to say.
      return inFlight.get(key) ?? Promise.resolve()
    }
  }

  intent.set(key, active)
  if (!active) {
    warm.delete(key)
  }

  const previous = inFlight.get(key) ?? Promise.resolve()

  const next = previous
    .then(async () => {
      // Latest intent wins: if the lease flipped again while we were queued,
      // the newer call sends its own state and this one has nothing to say.
      if (intent.get(key) !== active) {
        return
      }

      const response = await setSttLease(lease, active, scope)
      if (active) {
        warm.set(key, { at: Date.now(), state: classifyAcquire(response) })
      }
    })
    .catch(() => {
      // Backend not up yet / older backend without the endpoint / warm-up
      // failure: forget what we "sent" so the next flip retries honestly.
      // (A warm-up failure can also arrive as a RESOLVED `action: 'error'`
      // body — classifyAcquire records that as 'cold' so the next listening
      // start re-warms instead of trusting the registration.)
      if (intent.get(key) === active) {
        intent.delete(key)
      }
      warm.delete(key)
    })
    .finally(() => {
      if (active) {
        acquiring.delete(key)
      }
      if (inFlight.get(key) === next) {
        inFlight.delete(key)
      }
    })

  if (active) {
    acquiring.add(key)
  }
  inFlight.set(key, next)

  return next
}

/** Test seams. */
export function resetSttLeasesForTests() {
  intent.clear()
  warm.clear()
  inFlight.clear()
  acquiring.clear()
  sttWarmRevalidateMs = STT_WARM_REVALIDATE_MS_DEFAULT
}

export function setSttWarmRevalidateMsForTests(ms: number) {
  const saved = sttWarmRevalidateMs
  sttWarmRevalidateMs = ms

  return () => {
    sttWarmRevalidateMs = saved
  }
}
