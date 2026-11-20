import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $planGauges, notePlanGauge, planGaugeSlot, planScopeFor, resetPlanGauges } from '@/store/plan-gauge'
import type { PlanGauge } from '@/types/hermes'

import { deferred } from '../../../test/deferred'

import { PLAN_GAUGE_REFRESH_MS, usePlanGaugeRefresh } from './use-plan-gauge-refresh'

type GatewayRequester = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>

const SESSION = 'session-1'

const GAUGE: PlanGauge = {
  id: 'session',
  label: 'Current session',
  provider: 'claude-subscription',
  resets_at_ms: Date.now() + 3 * 60 * 60 * 1000,
  used_bp: 7_800
}

const HIGHER: PlanGauge = { ...GAUGE, used_bp: 8_200 }

const stored = (gauge: PlanGauge) => $planGauges.get()[planGaugeSlot(planScopeFor(SESSION), gauge.provider)]

// The production call site records into the account slot; the hook itself only
// decides WHEN to ask, so the tests record through the same store function.
const applyGauge = (gauge: PlanGauge) => notePlanGauge(planScopeFor(SESSION), gauge, SESSION)

function flushAsync() {
  return act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
}

beforeEach(() => {
  vi.useFakeTimers()
  resetPlanGauges()
  setVisibility('visible')
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
  resetPlanGauges()
  setVisibility('visible')
})

describe('usePlanGaugeRefresh', () => {
  it('reads the allowance on mount for an idle focused session', async () => {
    const requestGateway = vi.fn().mockResolvedValue({ plan_gauge: GAUGE }) as unknown as GatewayRequester

    renderHook(() =>
      usePlanGaugeRefresh({ applyGauge, busy: false, enabled: true, requestGateway, sessionId: SESSION })
    )

    await flushAsync()

    expect(requestGateway).toHaveBeenCalledWith('session.stats', { session_id: SESSION })
    expect(stored(GAUGE)?.gauge.used_bp).toBe(7_800)
  })

  it('re-reads on the interval so an idle tab follows the account', async () => {
    const requestGateway = vi
      .fn()
      .mockResolvedValueOnce({ plan_gauge: GAUGE })
      .mockResolvedValue({ plan_gauge: HIGHER }) as unknown as GatewayRequester

    renderHook(() =>
      usePlanGaugeRefresh({ applyGauge, busy: false, enabled: true, requestGateway, sessionId: SESSION })
    )

    await flushAsync()
    expect(stored(GAUGE)?.gauge.used_bp).toBe(7_800)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PLAN_GAUGE_REFRESH_MS)
    })

    expect(requestGateway).toHaveBeenCalledTimes(2)
    expect(stored(HIGHER)?.gauge.used_bp).toBe(8_200)
  })

  it('does not read while a turn is running — the streamed ticks carry the gauge', async () => {
    const requestGateway = vi.fn().mockResolvedValue({ plan_gauge: GAUGE }) as unknown as GatewayRequester

    renderHook(() =>
      usePlanGaugeRefresh({ applyGauge, busy: true, enabled: true, requestGateway, sessionId: SESSION })
    )

    await flushAsync()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PLAN_GAUGE_REFRESH_MS * 3)
    })

    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('picks the read up when the turn ends', async () => {
    const requestGateway = vi.fn().mockResolvedValue({ plan_gauge: GAUGE }) as unknown as GatewayRequester

    const { rerender } = renderHook(props => usePlanGaugeRefresh(props), {
      initialProps: { applyGauge, busy: true, enabled: true, requestGateway, sessionId: SESSION }
    })

    await flushAsync()
    expect(requestGateway).not.toHaveBeenCalled()

    rerender({ applyGauge, busy: false, enabled: true, requestGateway, sessionId: SESSION })
    await flushAsync()

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(stored(GAUGE)?.gauge.used_bp).toBe(7_800)
  })

  it('skips a hidden window and catches up the moment it is visible again', async () => {
    const requestGateway = vi.fn().mockResolvedValue({ plan_gauge: GAUGE }) as unknown as GatewayRequester

    setVisibility('hidden')

    renderHook(() =>
      usePlanGaugeRefresh({ applyGauge, busy: false, enabled: true, requestGateway, sessionId: SESSION })
    )

    await flushAsync()
    expect(requestGateway).not.toHaveBeenCalled()

    // The interval may fire while hidden: it must stay silent.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PLAN_GAUGE_REFRESH_MS * 2)
    })
    expect(requestGateway).not.toHaveBeenCalled()

    setVisibility('visible')
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await flushAsync()

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(stored(GAUGE)?.gauge.used_bp).toBe(7_800)
  })

  it('drops a response that lands after the focused session changed', async () => {
    // The stale reply carries a HIGHER figure on purpose: if it were applied it
    // would win the shared slot, so the assertion proves cancellation rather
    // than the store's ordering rule.
    const stale: PlanGauge = { ...GAUGE, used_bp: 9_000 }
    const first = deferred<{ plan_gauge: PlanGauge }>()

    const requestGateway = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue({ plan_gauge: HIGHER }) as unknown as GatewayRequester

    const { rerender } = renderHook(props => usePlanGaugeRefresh(props), {
      initialProps: { applyGauge, busy: false, enabled: true, requestGateway, sessionId: SESSION }
    })

    rerender({ applyGauge, busy: false, enabled: true, requestGateway, sessionId: 'session-2' })
    await flushAsync()

    await act(async () => {
      first.resolve({ plan_gauge: stale })
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(stored(HIGHER)?.gauge.used_bp).toBe(8_200)
  })

  it('reads nothing into the store when the account is not on a window plan', async () => {
    const requestGateway = vi.fn().mockResolvedValue({ plan_gauge: null }) as unknown as GatewayRequester

    renderHook(() =>
      usePlanGaugeRefresh({ applyGauge, busy: false, enabled: true, requestGateway, sessionId: SESSION })
    )

    await flushAsync()

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(Object.keys($planGauges.get())).toHaveLength(0)
  })

  it('stops reading once the gauge is hidden or the session is cleared', async () => {
    const requestGateway = vi.fn().mockResolvedValue({ plan_gauge: GAUGE }) as unknown as GatewayRequester

    const { rerender } = renderHook(props => usePlanGaugeRefresh(props), {
      initialProps: { applyGauge, busy: false, enabled: true, requestGateway, sessionId: SESSION }
    })

    await flushAsync()
    expect(requestGateway).toHaveBeenCalledTimes(1)

    rerender({ applyGauge, busy: false, enabled: false, requestGateway, sessionId: SESSION })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PLAN_GAUGE_REFRESH_MS * 2)
    })

    expect(requestGateway).toHaveBeenCalledTimes(1)
  })
})
