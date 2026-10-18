import { beforeEach, describe, expect, it, vi } from 'vitest'

const { refreshSupportedSessionControlAfterTurn } = vi.hoisted(() => ({
  refreshSupportedSessionControlAfterTurn: vi.fn(async () => undefined)
}))

vi.mock('@/store/session-control', () => ({ refreshSupportedSessionControlAfterTurn }))

import type { GatewayEventName } from '@hermes/shared'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $messagingSessions, $selectedStoredSessionId, $sessions, $unreadFinishedSessionIds } from '@/store/session'
import { $sessionStates, publishSessionState } from '@/store/session-states'
import { $unreadFinishedMarkers, noteDesktopAdmission } from '@/store/session-unread'
import { makeSessionInfo } from '@/test/session-info'

import { handleMessageStreamEvent } from './message-stream'
import type { GatewayEventContext } from './types'

function context(type: GatewayEventName): GatewayEventContext {
  return {
    deps: {
      activeGatewayProfile: 'default',
      activeSessionIdRef: { current: 's1' },
      appendAssistantDelta: vi.fn(),
      appendReasoningDelta: vi.fn(),
      compactedTurnRef: { current: new Set() },
      completeAssistantMessage: vi.fn(),
      failAssistantMessage: vi.fn(),
      finalizeInterimAssistantMessage: vi.fn(),
      flushQueuedDeltas: vi.fn(),
      dropQueuedDeltas: vi.fn(),
      hydrateFromStoredSession: vi.fn(async () => undefined),
      lastCwdInfoSessionRef: { current: null },
      nativeSubagentSessionsRef: { current: new Set() },
      queryClient: {} as GatewayEventContext['deps']['queryClient'],
      refreshHermesConfig: vi.fn(async () => undefined),
      scheduleSessionsRefresh: vi.fn(),
      sessionInterrupted: vi.fn(() => false),
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn(),
      upsertToolCall: vi.fn()
    },
    event: { type },
    explicitSid: 's1',
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload: { text: 'completed' },
    scheduleConfigRefresh: vi.fn(),
    sessionId: 's1'
  }
}

describe('handleMessageStreamEvent session-control integration', () => {
  it('refreshes only after message.complete, through the store seam', () => {
    expect(handleMessageStreamEvent(context('message.delta'))).toBe(true)
    expect(refreshSupportedSessionControlAfterTurn).not.toHaveBeenCalled()

    expect(handleMessageStreamEvent(context('message.complete'))).toBe(true)
    expect(refreshSupportedSessionControlAfterTurn).toHaveBeenCalledTimes(1)
    expect(refreshSupportedSessionControlAfterTurn).toHaveBeenCalledWith('s1')
  })
})

describe('unread dot for a messaging turn Desktop sent', () => {
  beforeEach(() => {
    $sessionStates.set({})
    $sessions.set([])
    $messagingSessions.set([makeSessionInfo({ id: 'tg-1', source: 'telegram' })])
    $selectedStoredSessionId.set(null)
    $unreadFinishedSessionIds.set([])
    $unreadFinishedMarkers.set({})
  })

  const complete = (admission?: string) => {
    const ctx = context('message.complete')
    ctx.sessionId = 'tg-1'
    ctx.event = { type: 'message.complete', admission_id: admission }
    handleMessageStreamEvent(ctx)
  }

  it('lights on the stamped completion of a turn Desktop submitted, once', () => {
    noteDesktopAdmission('desktop-turn')
    complete('desktop-turn')
    expect($unreadFinishedSessionIds.get()).toContain('tg-1')

    $unreadFinishedSessionIds.set([])
    complete('desktop-turn')
    expect($unreadFinishedSessionIds.get()).not.toContain('tg-1')
  })

  it('stays dark for a turn from the chat itself, and keeps a queued Desktop turn eligible', () => {
    noteDesktopAdmission('first')
    noteDesktopAdmission('queued-second')
    complete('chat-turn')
    complete('first')
    $unreadFinishedSessionIds.set([])

    complete('chat-turn-2')
    expect($unreadFinishedSessionIds.get()).not.toContain('tg-1')
    complete('queued-second')
    expect($unreadFinishedSessionIds.get()).toContain('tg-1')
  })

  it('stays dark while the conversation is on screen', () => {
    noteDesktopAdmission('desktop-turn')
    $selectedStoredSessionId.set('tg-1')
    complete('desktop-turn')
    expect($unreadFinishedSessionIds.get()).not.toContain('tg-1')
  })

  it('never lights a messaging session from a busy-to-idle settle, whatever turn it follows', () => {
    noteDesktopAdmission('desktop-turn')
    publishSessionState('rt-tg', { ...createClientSessionState('tg-1'), busy: true })
    publishSessionState('rt-tg', { ...createClientSessionState('tg-1'), busy: false })
    expect($unreadFinishedSessionIds.get()).not.toContain('tg-1')
  })
})
