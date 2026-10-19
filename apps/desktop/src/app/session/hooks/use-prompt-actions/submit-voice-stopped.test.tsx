import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'
import { $sessionStates } from '@/store/session-states'

import { useSubmitPrompt } from './submit'
import type { GatewayRequest } from './utils'

vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

beforeEach(() => {
  window.localStorage.clear()
  $sessions.set([])
  $sessionStates.set({})
  $connection.set(null)
  $activeGatewayProfile.set('default')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

type ClientSessionState = ReturnType<typeof createClientSessionState>

// TUI-26: a Desktop viewer of a LOCAL session whose voice chat another viewer (Ink) turned on types
// a bare "stop". The owner ends that voice chat and admits nothing: `{voice_stopped: true}`. The send
// is settled (no error bubble, no retry identity left), and its bubble goes, since no turn holds it.
it('a voice_stopped receipt settles the send with no turn, no error and no retained retry', async () => {
  let state: ClientSessionState = createClientSessionState()

  const requestGateway = vi.fn(async (_method: string, params?: Record<string, unknown>) =>
    ({ voice_stopped: true, session_id: 'runtime-a', submission_id: params?.submission_id }) as never
  )

  const deps = {
    activeSessionIdRef: { current: 'runtime-a' as string | null },
    selectedStoredSessionIdRef: { current: 'stored-a' as string | null },
    busyRef: { current: false },
    copy: en.desktop,
    createBackendSessionForSend: vi.fn(async () => null),
    getRoutedStoredSessionId: () => null,
    getRuntimeIdForStoredSession: () => 'runtime-a',
    getRouteToken: () => 'same-route',
    requestGateway: requestGateway as GatewayRequest,
    runtimeIdByStoredSessionIdRef: { current: new Map([['stored-a', 'runtime-a']]) },
    resumeStoredSession: vi.fn(),
    syncAttachmentsForSubmit: vi.fn(async (sessionId: string) => ({ sessionId, attachments: [] })),
    updateSessionState: vi.fn((_sid: string, updater: (s: ClientSessionState) => ClientSessionState) => {
      state = updater(state)

      return state
    })
  }

  const { result } = renderHook(() => useSubmitPrompt(deps as never))
  await act(async () => {
    expect(await result.current('stop', { submission_id: 'stop-1' })).toBe(true)
  })

  expect(requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(1)
  expect(state.messages.filter(message => message.role === 'user')).toHaveLength(0)
  expect(state.messages.some(message => message.error)).toBe(false)
  expect(state.busy).toBe(false)
  expect(state.awaitingResponse).toBe(false)
  expect(window.localStorage.getItem('hermes.desktop.preparedSubmissions.v1') ?? '').not.toContain('stop-1')
})
