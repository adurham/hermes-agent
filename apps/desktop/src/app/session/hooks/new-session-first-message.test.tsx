import { useStore } from '@nanostores/react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getLatestSessionMessages } from '@/hermes'
import type { ChatMessage } from '@/lib/chat-messages'
import { requestGatewayForProfile } from '@/store/gateway'
import {
  $activeSessionId,
  $messages,
  $selectedStoredSessionId,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'

import { useSessionActions } from './use-session-actions'
import { useSessionStateCache } from './use-session-state-cache'

// The persisted transcript is HTTP; the first user row is not readable there yet when the route lands.
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getLatestSessionMessages: vi.fn(),
  getSession: vi.fn()
}))

// Profile-owned rows route session RPCs over a per-profile socket; the test routes them to its fake.
vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForProfile: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

const PROMPT = 'Plan my week.\n\nAbout me:\n- Call me Sid.'

interface Ready {
  messagesOf: (runtimeId: string) => readonly ChatMessage[]
  resume: ReturnType<typeof useSessionActions>['resumeSession']
  submitNew: ReturnType<typeof useSessionActions>['submitTextToNewSession']
}

// Real session cache + real view sync: the user row has to survive the route's warm resume.
function Harness({
  onReady,
  requestGateway
}: {
  onReady: (ready: Ready) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const activeSessionId = useStore($activeSessionId)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const busyRef = useRef(false)

  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId,
    setAwaitingResponse,
    setBusy,
    setMessages
  })

  const actions = useSessionActions({
    activeSessionId,
    activeSessionIdRef: cache.activeSessionIdRef,
    busyRef,
    creatingSessionRef: useRef(false),
    ensureSessionState: cache.ensureSessionState,
    getRouteToken: () => 'new-session',
    getRoutedStoredSessionId: () => null,
    holdSessionTranscriptView: cache.holdSessionTranscriptView,
    navigate: vi.fn() as never,
    requestGateway,
    routedSessionId: null,
    resetViewSync: cache.resetViewSync,
    runtimeIdByStoredSessionIdRef: cache.runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef: cache.selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef: cache.sessionStateByRuntimeIdRef,
    syncSessionStateToView: cache.syncSessionStateToView,
    updateSessionState: cache.updateSessionState
  })

  useEffect(() => {
    onReady({
      messagesOf: runtimeId => cache.sessionStateByRuntimeIdRef.current.get(runtimeId)?.messages ?? [],
      resume: actions.resumeSession,
      submitNew: actions.submitTextToNewSession
    })
  }, [actions.resumeSession, actions.submitTextToNewSession, cache, onReady])

  return null
}

const userRows = (messages: readonly ChatMessage[]) => messages.filter(message => message.role === 'user')

async function mount({ refuse = false } = {}) {
  const requestGateway = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
    if (method === 'session.create') {
      return { session_id: 'rt-new', stored_session_id: 'stored-new' } as never
    }

    if (method === 'prompt.submit' && refuse) {
      throw new Error('refused')
    }

    if (method === 'session.activate') {
      return {
        info: {},
        message_count: 0,
        messages: [],
        messages_omitted: true,
        resumed: 'stored-new',
        running: true,
        session_id: 'rt-new',
        session_key: 'stored-new'
      } as never
    }

    return {} as never
  })

  let ready!: Ready
  render(<Harness onReady={value => (ready = value)} requestGateway={requestGateway} />)
  await waitFor(() => expect(ready).toBeDefined())
  vi.mocked(requestGatewayForProfile).mockImplementation((_profile, method, params) => requestGateway(method, params))

  return { ready, requestGateway }
}

describe('submitTextToNewSession first message', () => {
  beforeEach(() => {
    vi.mocked(getLatestSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-new' } as never)
  })

  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    setSessions([])
    vi.restoreAllMocks()
  })

  it('shows the submitted text as the first user message once the route opens the new chat', async () => {
    const { ready, requestGateway } = await mount()

    await act(async () => {
      await ready.submitNew(PROMPT)
    })

    // The navigate to #/stored-new lands here: use-route-resume resumes the routed id.
    await act(async () => {
      await ready.resume('stored-new', true)
    })

    await waitFor(() => expect(userRows($messages.get())).toHaveLength(1))
    expect($activeSessionId.get()).toBe('rt-new')
    expect(userRows($messages.get())[0]?.parts).toEqual([expect.objectContaining({ text: PROMPT, type: 'text' })])
    expect(requestGateway).toHaveBeenCalledWith('prompt.submit', { session_id: 'rt-new', text: PROMPT })
  })

  it('keeps one first message when the persisted transcript already has it', async () => {
    vi.mocked(getLatestSessionMessages).mockResolvedValue({
      messages: [{ content: PROMPT, role: 'user', timestamp: 1 }],
      session_id: 'stored-new'
    } as never)
    const { ready } = await mount()

    await act(async () => {
      await ready.submitNew(PROMPT)
    })

    await act(async () => {
      await ready.resume('stored-new', true)
    })

    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalled())
    await waitFor(() => expect(userRows($messages.get())).toHaveLength(1))
  })

  it('drops the first message when the prompt is refused', async () => {
    const { ready } = await mount({ refuse: true })

    await act(async () => {
      await expect(ready.submitNew(PROMPT)).rejects.toThrow('refused')
    })

    expect(userRows(ready.messagesOf('rt-new'))).toHaveLength(0)
  })
})
