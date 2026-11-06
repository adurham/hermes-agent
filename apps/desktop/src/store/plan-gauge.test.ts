import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import {
  $planGauges,
  forgetPlanGauge,
  notePlanGauge,
  planGaugeFor,
  planGaugeSlot,
  planScopeFor,
  resetPlanGauges
} from '@/store/plan-gauge'
import { $activeSessionId, $currentUsage } from '@/store/session'
import { $sessionStates, recordSessionEventScope } from '@/store/session-states'
import type { PlanGauge } from '@/types/hermes'

const WINDOW_RESET = 4_000_000_000_000

const gauge = (used_bp: number, reset: null | number = WINDOW_RESET): PlanGauge => ({
  id: 'day',
  label: 'Today',
  provider: 'nous',
  resets_at_ms: reset,
  used_bp
})

const claudeGauge = (used_bp: number, reset: null | number = WINDOW_RESET): PlanGauge => ({
  id: 'week',
  label: 'Current week',
  provider: 'claude-subscription-directsdk-experimental',
  resets_at_ms: reset,
  used_bp
})

const sessionState = (state: Partial<ReturnType<typeof createClientSessionState>> = {}) => ({
  ...createClientSessionState(),
  ...state
})

beforeEach(() => {
  resetPlanGauges()
  $sessionStates.set({})
  $activeSessionId.set(null)
  $currentUsage.set({ calls: 0, input: 0, output: 0, total: 0 })
})

afterEach(() => {
  vi.useRealTimers()
  resetPlanGauges()
})

describe('the plan allowance is an account fact', () => {
  it('shows one session observations on every session of the same source', () => {
    // Only ONE tab has run a turn in this process; the account's allowance must still be readable
    // from the other tab — the inconsistency this store exists to remove.
    $sessionStates.set({ runtimeA: sessionState({ usage: { calls: 3, input: 1, output: 1, total: 2, plan_gauge: gauge(6_800) } }) })

    const scope = planScopeFor('runtimeB')

    expect(planScopeFor('runtimeA')).toBe(scope)
    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.used_bp).toBe(6_800)
  })

  it('keeps the newest window and never drags the number backwards inside one', () => {
    const scope = planScopeFor('runtimeA')

    notePlanGauge(scope, gauge(6_800), 'runtimeA')
    // A session resumed from an older snapshot still carries yesterday's lower figure.
    notePlanGauge(scope, gauge(6_100), 'runtimeB')
    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.used_bp).toBe(6_800)

    notePlanGauge(scope, gauge(7_200), 'runtimeB')
    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.used_bp).toBe(7_200)

    // A new allowance window wins even though its counter starts lower.
    notePlanGauge(scope, gauge(400, WINDOW_RESET + 86_400_000), 'runtimeA')
    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.used_bp).toBe(400)
    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.resets_at_ms).toBe(WINDOW_RESET + 86_400_000)

    // ...and a snapshot from the window BEFORE it is ignored.
    notePlanGauge(scope, gauge(9_900, WINDOW_RESET), 'runtimeC')
    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.used_bp).toBe(400)
  })

  it('does not carry one source allowance onto another', () => {
    recordSessionEventScope({ connectionId: 'work', profile: 'coder', session_id: 'remote-1' })
    $sessionStates.set({
      'remote-1': sessionState({ usage: { calls: 1, input: 1, output: 1, total: 1, plan_gauge: gauge(9_000) } })
    })

    const remoteScope = planScopeFor('remote-1')

    expect(remoteScope).toContain('coder')
    expect(planGaugeFor($planGauges.get(), remoteScope, 'nous')?.used_bp).toBe(9_000)
    expect(planGaugeFor($planGauges.get(), planScopeFor(null), 'nous')).toBeNull()
  })

  it('keeps one slot per plan owner: a switch to Claude cannot be overwritten by the spent Nous plan', () => {
    // The reported bug: both accounts report into the same backend source. A single slot per scope
    // kept the higher (Nous, 100%) number and the bar stayed on it after the manual switch.
    const scope = planScopeFor('runtimeA')

    notePlanGauge(scope, gauge(10_000), 'runtimeA')
    notePlanGauge(scope, claudeGauge(4_200), 'runtimeB')

    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.used_bp).toBe(10_000)
    expect(planGaugeFor($planGauges.get(), scope, 'claude-subscription-directsdk-experimental')?.used_bp).toBe(4_200)
    // A provider with no observation in this source shows nothing — never another account's plan.
    expect(planGaugeFor($planGauges.get(), scope, 'ollama-cloud')).toBeNull()

    // Before its first reply a session does not know its provider; two owners is no honest guess.
    expect(planGaugeFor($planGauges.get(), scope)).toBeNull()
  })

  it('shows the scope only allowance before the session knows its provider', () => {
    const scope = planScopeFor('runtimeA')

    notePlanGauge(scope, claudeGauge(4_200), 'runtimeB')

    // One plan owner in the source: the pre-first-reply tab shows the account number, as it would
    // a moment later; with two owners (previous test) no guess is made.
    expect(planGaugeFor($planGauges.get(), scope)?.used_bp).toBe(4_200)
    expect(planGaugeFor($planGauges.get(), scope, 'claude-subscription-directsdk-experimental')?.used_bp).toBe(4_200)
  })

  it('drops the slot once the allowance window it describes has reset', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    const reset = 1_000_000 + 60_000
    const scope = planScopeFor('runtimeA')

    notePlanGauge(scope, gauge(9_500, reset))
    expect($planGauges.get()[planGaugeSlot(scope, 'nous')]).toBeDefined()

    vi.advanceTimersByTime(61_001)

    // A passed reset is no longer current (gateway/session_usage.py::plan_gauge) — the number goes
    // rather than lying about a window that ended.
    expect($planGauges.get()[planGaugeSlot(scope, 'nous')]).toBeUndefined()
    expect(planGaugeFor($planGauges.get(), scope, 'nous')).toBeNull()
  })

  it('keeps a window whose reset is beyond the timer ceiling (a month-long allowance)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_000_000)
    const scope = planScopeFor('runtimeA')

    // 40 days out: `setTimeout` caps at ~24.8 days, and a clamped 1ms timer used to expire the slot
    // the moment it was armed — a monthly allowance read as "no plan at all".
    notePlanGauge(scope, gauge(3_000, Date.now() + 40 * 86_400_000))
    vi.advanceTimersByTime(5_000)

    expect(planGaugeFor($planGauges.get(), scope, 'nous')?.used_bp).toBe(3_000)
  })

  it('observes the active session twin too, so a stats-only path still reaches the slot', () => {
    $activeSessionId.set('runtimeA')
    $currentUsage.set({ calls: 2, input: 1, output: 1, total: 2, plan_gauge: gauge(5_000) })

    expect(planGaugeFor($planGauges.get(), planScopeFor('runtimeA'), 'nous')?.used_bp).toBe(5_000)
  })

  it('forgets every owner slot of a source on request (teardown / a source that went away)', () => {
    const scope = planScopeFor('runtimeA')
    notePlanGauge(scope, gauge(6_800))
    notePlanGauge(scope, claudeGauge(4_200))
    forgetPlanGauge(scope)

    expect($planGauges.get()[planGaugeSlot(scope, 'nous')]).toBeUndefined()
    expect($planGauges.get()[planGaugeSlot(scope, 'claude-subscription-directsdk-experimental')]).toBeUndefined()
  })
})
