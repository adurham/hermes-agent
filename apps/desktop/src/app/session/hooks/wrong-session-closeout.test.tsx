// @vitest-environment jsdom
/**
 * Wrong-session close-out scenario for #86106 (a Desktop message persisted
 * to a stale session after switching chats), composed from the producers
 * mapped on the issue: a background session's delayed stored-id rotation
 * (#86359), a queued drain whose runtime went stale (fromQueue fence), and a
 * queued send with an attachment recovering against its OWN chat (#116287).
 *
 *   A active → B queued send created → user focuses tile C → B's runtime goes
 *   stale → A's delayed rotation lands → B's queued text + image drain → B's
 *   recovery mints a new runtime
 *
 * Invariants: focus stays where the user left it; prompt and attachment both
 * reach B's owner on B's new runtime; stored B maps only to B's runtime and
 * stored A only to A's. Real hooks (cache, session actions, prompt actions)
 * and the production owner-routing dispatcher; only the gateway edge is mocked.
 */
import { useStore } from '@nanostores/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import { createSessionRpcDispatcher } from '@/app/contrib/session-rpc-dispatcher'
import { sessionRoute } from '@/app/routes'
import { group } from '@/components/pane-shell/tree/model'
import { $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { textPart } from '@/lib/chat-messages'
import { requestGatewayForAgent, requestGatewayForProfile } from '@/store/gateway'
import {
  $activeSessionId,
  $activeSessionStoredIdRotation,
  $selectedStoredSessionId,
  setActiveSessionId,
  setActiveSessionStoredIdRotation,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'
import { $focusedStoredSessionId, $sessionTiles, clearAllSessionStates } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { usePromptActions } from './use-prompt-actions'
import { clearSingleFlightSessionResumeState } from './use-prompt-actions/single-flight-resume'
import { useSessionActions } from './use-session-actions'
import { useSessionStateCache } from './use-session-state-cache'

vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn(),
  retainGatewayForSessionTurn: vi.fn(async () => () => undefined)
}))

let routedStoredId: string | null = 'stored-A'
const navigate = vi.fn()

let handle: {
  cache: ReturnType<typeof useSessionStateCache>
  prompts: ReturnType<typeof usePromptActions>
}

function Harness() {
  const activeSessionId = useStore($activeSessionId)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const busyRef = useRef(false)
  const creatingSessionRef = useRef(false)

  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId,
    setAwaitingResponse,
    setBusy,
    setMessages
  })

  const requestGateway = createSessionRpcDispatcher({
    ...cache,
    ambientRequest: async () => {
      throw new Error('unexpected ambient request')
    }
  })

  const sessionActions = useSessionActions({
    activeSessionId,
    ...cache,
    busyRef,
    creatingSessionRef,
    getRouteToken: () => `${routedStoredId ? sessionRoute(routedStoredId) : '/'}::`,
    getRoutedStoredSessionId: () => routedStoredId,
    navigate: navigate as never,
    requestGateway,
    selectedStoredSessionId
  })

  const prompts = usePromptActions({
    activeSessionId,
    ...cache,
    busyRef,
    branchCurrentSession: async () => false,
    createBackendSessionForSend: sessionActions.createBackendSessionForSend,
    getRouteToken: () => `${routedStoredId ? sessionRoute(routedStoredId) : '/'}::`,
    getRoutedStoredSessionId: () => routedStoredId,
    handleSkinCommand: () => '',
    openMemoryGraph: () => undefined,
    refreshSessions: async () => undefined,
    requestGateway,
    resumeStoredSession: async () => {
      throw new Error('unexpected foreground resume')
    },
    startFreshSessionDraft: () => undefined,
    sttEnabled: false
  })

  handle = { cache, prompts }

  return null
}

afterEach(() => {
  cleanup()
  clearAllSessionStates()
  clearSingleFlightSessionResumeState()
  setActiveSessionStoredIdRotation(null)
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setSessions([])
  $sessionTiles.set([])
  $layoutTree.set(null)
  noteActiveTreeGroup(null)
  setBusy(false)
  setAwaitingResponse(false)
  setMessages([])
  window.history.pushState({}, '', '/')
  window.localStorage.clear()
  vi.clearAllMocks()
})

it('a queued B send drained through A’s delayed rotation and B’s recovery lands in B without moving the user off tile C', async () => {
  const sessionIdOf = (params?: Record<string, unknown>) => String(params?.session_id ?? '')
  vi.mocked(requestGatewayForAgent).mockImplementation(async (_connection, _profile, rpc, params) => {
    const sessionId = sessionIdOf(params)

    if (rpc === 'session.resume') {
      return { session_id: sessionId === 'stored-B' ? 'rt-B2' : `rt-unexpected-${sessionId}` }
    }

    if (sessionId === 'rt-B') {
      // B's runtime was reaped while its send sat in the queue.
      throw new Error('4007 session not found')
    }

    if (rpc === 'image.attach') {
      return { attached: true, path: '/scratch/shot.png' }
    }

    if (rpc === 'prompt.submit') {
      return { status: 'streaming' }
    }

    throw new Error(`unexpected ${rpc} for ${sessionId}`)
  })

  // Distinct owners per chat: routing to the wrong runtime is visible as a
  // call on the wrong connection, not only as a wrong session_id.
  setSessions(
    ['A', 'B', 'C'].map(id => ({
      id: `stored-${id}`,
      connection_id: `connection-${id}`,
      profile: 'default',
      source: 'desktop',
      message_count: 1
    })) as SessionInfo[]
  )
  // A is the primary: selection, HashRouter route and active runtime all name it.
  routedStoredId = 'stored-A'
  window.history.pushState({}, '', '/#/stored-A')
  setSelectedStoredSessionId('stored-A')
  setActiveSessionId('rt-A')
  render(<Harness />)
  act(() => {
    for (const id of ['A', 'B', 'C']) {
      handle.cache.updateSessionState(
        `rt-${id}`,
        state => ({
          ...state,
          messages: [{ id: `history-${id}`, role: 'assistant', parts: [textPart(`history ${id}`)] }]
        }),
        `stored-${id}`
      )
    }
  })

  // The user opens C as a tile and is typing there when everything below lands.
  act(() => {
    $sessionTiles.set([{ runtimeId: 'rt-C', storedSessionId: 'stored-C' }])
    $layoutTree.set(group(['workspace', 'session-tile:stored-C'], { active: 'session-tile:stored-C', id: 'grp-main' }))
    noteActiveTreeGroup('grp-main')
  })
  expect($focusedStoredSessionId.get()).toBe('stored-C')

  // A's auto-compression rotates its stored id in the background (real
  // producer in useSessionStateCache, real consumer in useSessionActions).
  act(() => {
    handle.cache.updateSessionState('rt-A', state => state, 'stored-A-next')
  })
  await act(async () => undefined)

  expect(navigate).not.toHaveBeenCalled()
  expect($activeSessionStoredIdRotation.get()).toBeNull()
  expect($focusedStoredSessionId.get()).toBe('stored-C')
  expect($selectedStoredSessionId.get()).toBe('stored-A')
  expect(handle.cache.runtimeIdByStoredSessionIdRef.current.get('stored-A-next')).toBe('rt-A')

  // The composer queue drains B's send (text + image) with the runtime id it
  // captured when the send was queued — stale by now.
  await act(async () => {
    expect(
      await handle.prompts.submitText('B queued text', {
        attachments: [{ id: 'att-1', kind: 'image', label: 'shot.png', path: '/scratch/shot.png' } as never],
        composerScope: 'stored-B',
        fromQueue: true,
        sessionId: 'rt-B',
        storedSessionId: 'stored-B'
      })
    ).toBe(true)
  })

  const calls = vi
    .mocked(requestGatewayForAgent)
    .mock.calls.map(([connection, , rpc, params]) => [connection, rpc, sessionIdOf(params as never)])

  await waitFor(() => expect(calls).toContainEqual(['connection-B', 'prompt.submit', 'rt-B2']))
  expect(calls).toContainEqual(['connection-B', 'session.resume', 'stored-B'])
  expect(calls).toContainEqual(['connection-B', 'image.attach', 'rt-B2'])
  expect(calls.filter(([, rpc]) => rpc === 'session.resume')).toEqual([['connection-B', 'session.resume', 'stored-B']])
  expect(calls.some(([connection]) => connection !== 'connection-B')).toBe(false)
  expect(calls.some(([, , sessionId]) => sessionId === 'rt-A' || sessionId === 'rt-C')).toBe(false)
  expect(requestGatewayForProfile).not.toHaveBeenCalled()

  // Each stored id maps only to its own runtime; the foreground never moved.
  const bindings = handle.cache.runtimeIdByStoredSessionIdRef.current
  expect(bindings.get('stored-B')).toBe('rt-B2')
  expect(bindings.get('stored-A-next')).toBe('rt-A')
  expect(bindings.get('stored-C')).toBe('rt-C')
  expect([...bindings.values()].filter(runtime => runtime === 'rt-B2')).toEqual(['rt-B2'])
  expect(handle.cache.sessionStateByRuntimeIdRef.current.get('rt-B2')?.storedSessionId).toBe('stored-B')
  expect($activeSessionId.get()).toBe('rt-A')
  expect(handle.cache.activeSessionIdRef.current).toBe('rt-A')
  expect($selectedStoredSessionId.get()).toBe('stored-A')
  expect($focusedStoredSessionId.get()).toBe('stored-C')
  expect(navigate).not.toHaveBeenCalled()

  // B's user row is filed under B only (never under A's or C's state). The
  // optimistic bubble stays on the cache entry the queued send was seeded on;
  // the persisted row is the prompt.submit on rt-B2 asserted above.
  const userRowOwners = [...handle.cache.sessionStateByRuntimeIdRef.current.values()].flatMap(state =>
    state.messages.filter(message => message.role === 'user').map(() => state.storedSessionId)
  )

  expect(userRowOwners).toEqual(['stored-B'])
})
