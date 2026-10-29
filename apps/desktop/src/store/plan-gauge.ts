/**
 * The account's plan allowance (usage-window plans), one slot per backend source.
 *
 * A window allowance is an ACCOUNT fact, but it reaches the renderer per session: every session's
 * replies carry the gauge IT last observed, and each session observes at a different moment. So
 * reading the gauge off whichever session happened to be focused — what the statusbar did — painted
 * a different number on every tab of the SAME account, and nothing at all on a tab whose session had
 * not run yet in this process. The statusbar reads this slot instead: every observation lands here,
 * the newest one wins, and every tab of one source agrees on the number.
 *
 * Keyed by the source the observing session belongs to (`planScopeFor`), so a second connected
 * gateway or profile keeps its own account's allowance.
 */
import { registryBackendScopeKey } from '@hermes/shared'
import { atom } from 'nanostores'

import type { PlanGauge } from '@/types/hermes'

import { activeGatewayConnectionId } from './gateway'
import { $activeGatewayProfile } from './profile'
import { $activeSessionId, $currentUsage } from './session'
import { $sessionStates, sessionEventScopeFor } from './session-states'

export interface PlanGaugeObservation {
  /** Arrival time of the observation (diagnostics only — freshness is per WINDOW, see `supersedes`). */
  at: number
  gauge: PlanGauge
  /** The runtime session that observed it (diagnostics only). */
  sessionId?: string
}

export const $planGauges = atom<Record<string, PlanGaugeObservation>>({})

/**
 * The source a runtime session's allowance belongs to: the scope its own events proved, else the
 * actively served source — `session-states.recordSessionEventScope` records nothing for the local
 * primary, and the reader (statusbar) and the writers here must land on the same key.
 */
export function planScopeFor(runtimeId: null | string | undefined): string {
  return (
    sessionEventScopeFor(runtimeId) ??
    registryBackendScopeKey(activeGatewayConnectionId(), $activeGatewayProfile.get())
  )
}

/**
 * Whether `next` may displace `previous`. Within one window (same reset instant) only a HIGHER
 * used_bp moves the number: usage accrues monotonically, so a session resumed from an older
 * snapshot must not drag the account's readout backwards. Across windows the later reset wins, and
 * an unknown reset never displaces a known one; a reset the server moved backwards is stale.
 */
function supersedes(previous: PlanGauge, next: PlanGauge): boolean {
  const before = previous.resets_at_ms ?? null
  const after = next.resets_at_ms ?? null

  if (before !== after) {
    return after !== null && (before === null || after > before)
  }

  return next.used_bp > previous.used_bp
}

const expiryTimers = new Map<string, ReturnType<typeof setTimeout>>()

// setTimeout's ceiling (~24.8 days): a longer window re-arms when the cap elapses instead of firing
// at once — a monthly allowance used to expire the slot on the spot (and take the readout with it).
const MAX_TIMER_MS = 2_147_483_647

function clearExpiry(scopeKey: string): void {
  const timer = expiryTimers.get(scopeKey)

  if (timer !== undefined) {
    clearTimeout(timer)
    expiryTimers.delete(scopeKey)
  }
}

/** Drop the slot: the allowance window it describes has ended, so the number is no longer current
 *  (the gateway's own rule, `gateway/session_usage.py::plan_gauge`). */
export function forgetPlanGauge(scopeKey: string): void {
  clearExpiry(scopeKey)
  const { [scopeKey]: _dropped, ...rest } = $planGauges.get()
  $planGauges.set(rest)
}

function armExpiry(scopeKey: string, gauge: PlanGauge): void {
  clearExpiry(scopeKey)
  const reset = gauge.resets_at_ms

  if (typeof reset !== 'number' || reset <= 0) {
    return
  }

  const delay = reset - Date.now()

  if (delay <= 0) {
    forgetPlanGauge(scopeKey)

    return
  }

  expiryTimers.set(
    scopeKey,
    setTimeout(() => expireSlot(scopeKey), Math.min(delay + 1_000, MAX_TIMER_MS))
  )
}

/** A timer fired: drop the slot if the CURRENT observation's window has ended, else re-arm (the cap). */
function expireSlot(scopeKey: string): void {
  expiryTimers.delete(scopeKey)
  const current = $planGauges.get()[scopeKey]
  const reset = current?.gauge.resets_at_ms

  if (!current || typeof reset !== 'number' || reset <= 0) {
    return
  }

  if (reset <= Date.now()) {
    forgetPlanGauge(scopeKey)

    return
  }

  armExpiry(scopeKey, current.gauge)
}

/** Record one observation of the account's allowance. Newest wins within the rules of `supersedes`. */
export function notePlanGauge(scopeKey: string, gauge: PlanGauge, sessionId?: string): void {
  const current = $planGauges.get()[scopeKey]

  if (current && !supersedes(current.gauge, gauge)) {
    return
  }

  $planGauges.set({ ...$planGauges.get(), [scopeKey]: { at: Date.now(), gauge, sessionId } })
  armExpiry(scopeKey, gauge)
}

/** Test/teardown reset; the app never clears the slots wholesale. */
export function resetPlanGauges(): void {
  for (const scopeKey of [...expiryTimers.keys()]) {
    clearExpiry(scopeKey)
  }

  $planGauges.set({})
}

// Every usage payload carrying the gauge lands in the session's slice (`$sessionStates`), and the
// ACTIVE session's twin in `$currentUsage` — a couple of paths (the degraded resume, the
// post-compaction stats read) write only the latter. Observing both covers every producer without
// touching each call site; the overlap is a no-op under `supersedes`.
$sessionStates.listen(states => {
  for (const [runtimeId, state] of Object.entries(states)) {
    const gauge = state.usage?.plan_gauge

    if (gauge) {
      notePlanGauge(planScopeFor(runtimeId), gauge, runtimeId)
    }
  }
})

$currentUsage.listen(usage => {
  const gauge = usage.plan_gauge

  if (!gauge) {
    return
  }

  const runtimeId = $activeSessionId.get()
  notePlanGauge(planScopeFor(runtimeId), gauge, runtimeId ?? undefined)
})
