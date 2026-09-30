import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const setSttLease = vi.fn(
  async (
    _lease: string,
    _active: boolean,
    _owner?: { connectionId?: null | string; profile?: null | string }
  ) => ({ ok: true }) as { ok: boolean; action?: string; warmed?: boolean }
)

vi.mock('@/hermes', () => ({
  getApiRequestConnection: () => currentScope.connectionId,
  getApiRequestProfile: () => currentScope.profile,
  setSttLease: (
    lease: string,
    active: boolean,
    owner?: { connectionId?: null | string; profile?: null | string }
  ) => setSttLease(lease, active, owner)
}))

import {
  resetSttLeasesForTests,
  setSttWarmRevalidateMsForTests,
  syncSttLease,
  VOICE_INPUT_LEASE
} from './stt-lease'

// Ambient scope the (mocked) api helpers would read; tests flip it to model a
// gateway/profile switch between enqueue and settle.
const currentScope = { connectionId: null as null | string, profile: null as null | string }

const resetScope = () => {
  currentScope.connectionId = null
  currentScope.profile = null
}

describe('syncSttLease', () => {
  beforeEach(() => {
    resetSttLeasesForTests()
    resetScope()
    setSttLease.mockReset()
    setSttLease.mockImplementation(async () => ({ ok: true }))
  })

  afterEach(() => {
    resetSttLeasesForTests()
    resetScope()
  })

  it('acquires on the first on and releases on off', async () => {
    await syncSttLease(VOICE_INPUT_LEASE, true)
    await syncSttLease(VOICE_INPUT_LEASE, false)

    expect(setSttLease.mock.calls).toEqual([
      [VOICE_INPUT_LEASE, true, { connectionId: null, profile: null }],
      [VOICE_INPUT_LEASE, false, { connectionId: null, profile: null }]
    ])
  })

  it('skips an initial off — never releases a lease it did not hold', async () => {
    await syncSttLease(VOICE_INPUT_LEASE, false)

    expect(setSttLease).not.toHaveBeenCalled()
  })

  it('dedupes a repeat of the last sent state while the warm-up is fresh', async () => {
    await syncSttLease(VOICE_INPUT_LEASE, true)
    await syncSttLease(VOICE_INPUT_LEASE, true)
    await syncSttLease(VOICE_INPUT_LEASE, true)

    expect(setSttLease).toHaveBeenCalledTimes(1)
  })

  it('queues an off behind an in-flight on so the wire never sees them reordered', async () => {
    let finishAcquire: () => void = () => undefined
    setSttLease.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finishAcquire = () => resolve({ ok: true })
        })
    )

    const on = syncSttLease(VOICE_INPUT_LEASE, true)
    // Let the acquire actually go out (it runs on a microtask).
    await Promise.resolve()
    expect(setSttLease.mock.calls).toEqual([[VOICE_INPUT_LEASE, true, { connectionId: null, profile: null }]])

    const off = syncSttLease(VOICE_INPUT_LEASE, false)
    await Promise.resolve()
    // Still only the acquire — the release waits for it to finish.
    expect(setSttLease).toHaveBeenCalledTimes(1)

    finishAcquire()
    await Promise.all([on, off])

    expect(setSttLease.mock.calls).toEqual([
      [VOICE_INPUT_LEASE, true, { connectionId: null, profile: null }],
      [VOICE_INPUT_LEASE, false, { connectionId: null, profile: null }]
    ])
  })

  it('coalesces a flip that reverses before its call went out — latest intent wins', async () => {
    const on = syncSttLease(VOICE_INPUT_LEASE, true)
    const off = syncSttLease(VOICE_INPUT_LEASE, false)
    await Promise.all([on, off])

    // The acquire never had a chance to go out; only the terminal state is sent
    // (a release of a never-held lease is a backend no-op).
    expect(setSttLease.mock.calls).toEqual([[VOICE_INPUT_LEASE, false, { connectionId: null, profile: null }]])
  })

  it('forgets the sent state on failure so the next flip retries', async () => {
    setSttLease.mockImplementationOnce(async () => {
      throw new Error('backend not ready')
    })

    await expect(syncSttLease(VOICE_INPUT_LEASE, true)).resolves.toBeUndefined()
    await syncSttLease(VOICE_INPUT_LEASE, true)

    expect(setSttLease).toHaveBeenCalledTimes(2)
  })

  it('never rejects — recording must not depend on warm-up', async () => {
    setSttLease.mockRejectedValue(new Error('backend gone'))

    await expect(syncSttLease(VOICE_INPUT_LEASE, true)).resolves.toBeUndefined()
    await expect(syncSttLease(VOICE_INPUT_LEASE, false)).resolves.toBeUndefined()
  })

  it('voice-input lease is per renderer', () => {
    expect(VOICE_INPUT_LEASE).toMatch(/^desktop:voice-input:[a-z0-9]+$/)
  })

  // ── Readiness is validated, never remembered (#105955 review) ────────────

  it('treats an application-level error outcome as not-warm and retries the next acquire', async () => {
    // /api/audio/stt-lease deliberately resolves HTTP 200 with
    // {ok: true, action: 'error', warmed: false} — a failed preload must not
    // suppress subsequent listening-cycle retries.
    setSttLease.mockImplementationOnce(async () => ({
      ok: true,
      action: 'error',
      warmed: false
    }))

    await syncSttLease(VOICE_INPUT_LEASE, true)
    await syncSttLease(VOICE_INPUT_LEASE, true)

    expect(setSttLease).toHaveBeenCalledTimes(2)
  })

  it('re-warms after the validation window lapses — idle eviction is invisible to the client', async () => {
    const restore = setSttWarmRevalidateMsForTests(0)
    try {
      setSttLease.mockImplementation(async () => ({ ok: true, action: 'cached', warmed: true }))

      await syncSttLease(VOICE_INPUT_LEASE, true)
      await syncSttLease(VOICE_INPUT_LEASE, true)

      // The backend's idle-unload watcher can evict the model at any time;
      // registration alone must not be treated as permanent readiness.
      expect(setSttLease).toHaveBeenCalledTimes(2)
    } finally {
      restore()
    }
  })

  it('accepts a settled cloud noop as warm — no retry churn for non-local providers', async () => {
    setSttLease.mockImplementation(async () => ({ ok: true, action: 'noop', warmed: false }))

    await syncSttLease(VOICE_INPUT_LEASE, true)
    await syncSttLease(VOICE_INPUT_LEASE, true)

    expect(setSttLease).toHaveBeenCalledTimes(1)
  })

  it('dedupes concurrent identical acquires into one wire call', async () => {
    let finishAcquire: () => void = () => undefined
    setSttLease.mockImplementation(
      () =>
        new Promise(resolve => {
          finishAcquire = () => resolve({ ok: true, action: 'loaded', warmed: true })
        })
    )

    const first = syncSttLease(VOICE_INPUT_LEASE, true)
    // The queued acquire runs on a microtask — let it reach the wire before
    // the second identical acquire is registered.
    await Promise.resolve()
    const second = syncSttLease(VOICE_INPUT_LEASE, true)

    expect(setSttLease).toHaveBeenCalledTimes(1)

    finishAcquire()
    await Promise.all([first, second])

    expect(setSttLease).toHaveBeenCalledTimes(1)
  })

  // ── A lease belongs to the connection/profile that acquired it ────────────

  it('binds queued operations to the owner captured at enqueue time', async () => {
    // gateway-A / worker_alpha acquires; the response is held so a release can
    // queue behind it; the source switches to gateway-B before either settles.
    let finishAcquire: () => void = () => undefined
    setSttLease.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finishAcquire = () => resolve({ ok: true, action: 'loaded', warmed: true })
        })
    )

    const on = syncSttLease(VOICE_INPUT_LEASE, true, { connectionId: 'gateway-a', profile: 'worker_alpha' })
    await Promise.resolve()
    const off = syncSttLease(VOICE_INPUT_LEASE, false, { connectionId: 'gateway-a', profile: 'worker_alpha' })
    await Promise.resolve()

    // Switch the ambient selection mid-flight.
    currentScope.connectionId = 'gateway-b'
    currentScope.profile = 'worker_beta'

    finishAcquire()
    await Promise.all([on, off])

    // Both wire calls carry the acquiring owner; the release lands on the
    // backend that holds the lease, not the newly selected one.
    expect(setSttLease.mock.calls).toEqual([
      [VOICE_INPUT_LEASE, true, { connectionId: 'gateway-a', profile: 'worker_alpha' }],
      [VOICE_INPUT_LEASE, false, { connectionId: 'gateway-a', profile: 'worker_alpha' }]
    ])
  })

  it('captures the ambient owner at call time when none is passed', async () => {
    currentScope.connectionId = 'gateway-a'
    currentScope.profile = 'worker_alpha'

    let finishAcquire: () => void = () => undefined
    setSttLease.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finishAcquire = () => resolve({ ok: true, action: 'loaded', warmed: true })
        })
    )

    const on = syncSttLease(VOICE_INPUT_LEASE, true)
    await Promise.resolve()
    const off = syncSttLease(VOICE_INPUT_LEASE, false)
    await Promise.resolve()

    currentScope.connectionId = 'gateway-b'
    currentScope.profile = 'worker_beta'

    finishAcquire()
    await Promise.all([on, off])

    expect(setSttLease.mock.calls).toEqual([
      [VOICE_INPUT_LEASE, true, { connectionId: 'gateway-a', profile: 'worker_alpha' }],
      [VOICE_INPUT_LEASE, false, { connectionId: 'gateway-a', profile: 'worker_alpha' }]
    ])
  })

  it('sends its own acquire to a new owner after a source switch — same lease, different backend', async () => {
    setSttLease.mockImplementation(async () => ({ ok: true, action: 'loaded', warmed: true }))

    await syncSttLease(VOICE_INPUT_LEASE, true, { connectionId: 'gateway-a', profile: 'worker_alpha' })
    await syncSttLease(VOICE_INPUT_LEASE, true, { connectionId: 'gateway-b', profile: 'worker_beta' })

    // A's remembered state must not satisfy B's acquire: warm-up must not
    // disappear after a source change in the same renderer.
    expect(setSttLease.mock.calls).toEqual([
      [VOICE_INPUT_LEASE, true, { connectionId: 'gateway-a', profile: 'worker_alpha' }],
      [VOICE_INPUT_LEASE, true, { connectionId: 'gateway-b', profile: 'worker_beta' }]
    ])
  })

  it('keeps an explicit local owner explicit so a later remote primary cannot reinterpret it', async () => {
    setSttLease.mockImplementation(async () => ({ ok: true, action: 'loaded', warmed: true }))

    await syncSttLease(VOICE_INPUT_LEASE, true, { connectionId: 'local', profile: 'default' })

    expect(setSttLease.mock.calls[0][2]).toEqual({ connectionId: 'local', profile: 'default' })
  })
})
