import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { setSessions } from '@/store/session'
import { sessionTileDelegate } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { useSessionTileDelegate } from './use-session-tile-delegate'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getSessionMessages: vi.fn(async () => ({ messages: [], session_id: '' }))
}))

const { getSessionMessages } = await import('@/hermes')

const row = (over: Partial<SessionInfo>): SessionInfo =>
  ({
    ended_at: null,
    id: 'live',
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    // Tests below don't exercise the transcript-hydration guard unless they
    // opt in explicitly (message_count > 0).
    message_count: 0,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source: null,
    started_at: 0,
    title: null,
    ...over
  }) as SessionInfo

function renderTile(requestGateway: ReturnType<typeof vi.fn>, updateSessionState = vi.fn()) {
  renderHook(() =>
    useSessionTileDelegate({
      archiveSession: vi.fn(async () => undefined),
      branchStoredSession: vi.fn(async () => undefined),
      executeSlashCommand: vi.fn(async () => undefined) as never,
      removeSession: vi.fn(async () => undefined),
      requestGateway: requestGateway as never,
      runtimeIdByStoredSessionIdRef: { current: new Map() },
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState
    })
  )

  return updateSessionState
}

describe('useSessionTileDelegate resumeTile', () => {
  beforeEach(() => {
    setSessions([])
    vi.mocked(getSessionMessages).mockClear()
  })

  afterEach(() => {
    setSessions([])
  })

  it('resumes a cold tile and seeds its transcript from the resume payload', async () => {
    setSessions([row({ id: 'stored-y' })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-2' } as never) : ({} as never)
    )

    renderTile(requestGateway)
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-y')

    expect(runtimeId).toBe('runtime-2')
    expect(requestGateway).toHaveBeenCalledWith('session.resume', {
      session_id: 'stored-y',
      cols: 96
    })
  })

  it('rejects a resume that comes back empty for a session with known history, instead of caching a blank tile', async () => {
    // Reproduces the "click into a tab that was already open after quit/reopen
    // and it shows blank" bug: a persisted tile's cold resume can silently
    // hydrate empty (a stale/cold gateway swap mid-request, a transient 404
    // the `.catch`es upstream swallow) even though the stored row proves the
    // session has real history. Before this guard, resumeTile cached that
    // empty transcript as truth and the tile rendered permanently blank with
    // no error and no Retry affordance. Now it must throw instead, so
    // session-tile.tsx's existing error card + Retry path catches it — and,
    // critically, updateSessionState must never be called with the empty
    // messages array.
    setSessions([row({ id: 'stored-z', message_count: 42 })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-3', messages: [] } as never) : ({} as never)
    )

    const updateSessionState = renderTile(requestGateway)

    await expect(sessionTileDelegate()!.resumeTile('stored-z')).rejects.toThrow(
      'resume returned no messages for a non-empty session'
    )

    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('accepts a genuinely empty resume for a brand-new session (message_count 0)', async () => {
    // The guard must not false-positive on a real fresh/empty session — only
    // a KNOWN-non-empty stored row (message_count > 0) with an empty resume
    // result is treated as a hydration failure.
    setSessions([row({ id: 'stored-w', message_count: 0 })])

    const requestGateway = vi.fn(async (method: string) =>
      method === 'session.resume' ? ({ session_id: 'runtime-4', messages: [] } as never) : ({} as never)
    )

    const updateSessionState = renderTile(requestGateway)
    const runtimeId = await sessionTileDelegate()!.resumeTile('stored-w')

    expect(runtimeId).toBe('runtime-4')
    expect(updateSessionState).toHaveBeenCalled()
  })
})
