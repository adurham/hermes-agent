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

/** One allowance per (backend source, plan owner). A scope can host sessions on different
 *  providers — a chat switched from Nous to Claude keeps both accounts' observations — so the
 *  provider is part of the slot key. A single slot per scope let the first account's number win
 *  (`supersedes` only ever raises a percentage within a window) and put Nous's spent plan on a
 *  session answering on Claude. */
export function planGaugeSlot(scopeKey: string, provider?: string): string {
  return `${scopeKey}\u0000${String(provider ?? '').trim().toLowerCase()}`
}

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
 * The allowance to paint for one source and one plan owner. Exact owner first; a legacy observation
 * without a provider keeps working for an older gateway. A session that does not know its provider
 * yet (before its first reply) inherits the scope's only allowance — the account number the tab
 * would show a moment later; with several owners in one scope there is no honest guess, so none.
 */
export function planGaugeFor(
  gauges: Record<string, PlanGaugeObservation>,
  scopeKey: string,
  provider?: string
): PlanGauge | null {
  const owner = String(provider ?? '').trim().toLowerCase()

  if (owner) {
    return gauges[planGaugeSlot(scopeKey, owner)]?.gauge ?? null
  }

  const legacy = gauges[scopeKey]

  if (legacy) {
    return legacy.gauge
  }

  const scoped = Object.entries(gauges).filter(([key]) => key.startsWith(`${scopeKey}\u0000`))

  return scoped.length === 1 ? scoped[0][1].gauge : null
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

function clearExpiry(slotKey: string): void {
  const timer = expiryTimers.get(slotKey)

  if (timer !== undefined) {
    clearTimeout(timer)
    expiryTimers.delete(slotKey)
  }
}

function forgetSlot(slotKey: string): void {
  clearExpiry(slotKey)
  const { [slotKey]: _dropped, ...rest } = $planGauges.get()
  $planGauges.set(rest)
}

/** Drop every slot of one source: its allowance windows ended, so the numbers are no longer current
 *  (the gateway's own rule, `gateway/session_usage.py::plan_gauge`). */
export function forgetPlanGauge(scopeKey: string): void {
  const prefix = `${scopeKey}\u0000`

  for (const key of Object.keys($planGauges.get())) {
    if (key === scopeKey || key.startsWith(prefix)) {
      forgetSlot(key)
    }
  }
}

function armExpiry(slotKey: string, gauge: PlanGauge): void {
  clearExpiry(slotKey)
  const reset = gauge.resets_at_ms

  if (typeof reset !== 'number' || reset <= 0) {
    return
  }

  const delay = reset - Date.now()

  if (delay <= 0) {
    forgetSlot(slotKey)

    return
  }

  expiryTimers.set(
    slotKey,
    setTimeout(() => expireSlot(slotKey), Math.min(delay + 1_000, MAX_TIMER_MS))
  )
}

/** A timer fired: drop the slot if the CURRENT observation's window has ended, else re-arm (the cap). */
function expireSlot(slotKey: string): void {
  expiryTimers.delete(slotKey)
  const current = $planGauges.get()[slotKey]
  const reset = current?.gauge.resets_at_ms

  if (!current || typeof reset !== 'number' || reset <= 0) {
    return
  }

  if (reset <= Date.now()) {
    forgetSlot(slotKey)

    return
  }

  armExpiry(slotKey, current.gauge)
}

/** Record one observation of one account's allowance. Newest wins within the rules of `supersedes`. */
export function notePlanGauge(scopeKey: string, gauge: PlanGauge, sessionId?: string): void {
  const slotKey = planGaugeSlot(scopeKey, gauge.provider)
  const current = $planGauges.get()[slotKey]

  if (current && !supersedes(current.gauge, gauge)) {
    return
  }

  $planGauges.set({ ...$planGauges.get(), [slotKey]: { at: Date.now(), gauge, sessionId } })
  armExpiry(slotKey, gauge)
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
