import { afterEach, describe, expect, it, vi } from 'vitest'

import { $activeSessionId } from '@/store/session'
import { $sudoRequest, clearSudoRequest } from '@/store/prompts'

import { handleInputRequestEvent } from './input-requests'
import type { GatewayEventContext } from './types'

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification: vi.fn() }))

function context(type: string, requestId: string, routedSession: string | null): GatewayEventContext {
  const payload = { request_id: requestId, profile_key: '/home/h/.hermes' }
  return {
    deps: { updateSessionState: vi.fn() } as unknown as GatewayEventContext['deps'],
    event: { payload, type },
    explicitSid: '',
    fromActiveSource: () => true,
    isActiveEvent: true,
    occurredAt: 1,
    payload: payload as GatewayEventContext['payload'],
    scheduleConfigRefresh: vi.fn(),
    sessionId: routedSession
  }
}

describe('Bot Screen install password card', () => {
  afterEach(() => {
    clearSudoRequest()
    $activeSessionId.set(null)
  })

  it('belongs to the app, not to the chat that happened to be open: it survives a chat switch and its expiry finds it', () => {
    // The gateway emits the card sessionless; the stream resolver attributes an unscoped event to the
    // ambient chat. Stored under that chat, the card vanished the moment the user switched chats and
    // the expiry (routed to the NEW ambient chat) never reached it.
    $activeSessionId.set('chat-a')
    expect(handleInputRequestEvent(context('display.install.sudo.request', 'req-1', 'chat-a'))).toBe(true)
    expect($sudoRequest.get()?.requestId).toBe('req-1')

    $activeSessionId.set('chat-b')
    expect($sudoRequest.get()?.requestId).toBe('req-1')

    expect(handleInputRequestEvent(context('display.install.sudo.expire', 'req-1', 'chat-b'))).toBe(true)
    expect($sudoRequest.get()).toBeNull()
  })
})
