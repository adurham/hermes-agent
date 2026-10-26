import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { renderMessageStream } from '@/app/session/hooks/use-message-stream/test-harness'
import { STREAM_DELTA_FLUSH_MS } from '@/app/session/hooks/use-message-stream/utils'
import type { ChatMessage, ChatMessagePart } from '@/lib/chat-messages'

import { useRuntimeMessageRepository } from './runtime-repository'

// One stored reply reaches the store through more than one path (live stream,
// the committed row persisted before a tool round, a history read on
// re-attach). Whatever path lets a second copy in, the transcript the runtime
// renders shows the reply once.

const ANSWER = 'The config loads from the profile home, so the override wins over the default.'
const NEXT = 'Both checks pass; the second run confirms it.'

const text = (body: string): ChatMessagePart => ({ type: 'text', text: body })

const tool = (id: string, command: string): ChatMessagePart =>
  ({ type: 'tool-call', toolCallId: id, toolName: 'terminal', args: { command }, result: 'ok' }) as ChatMessagePart

const renderedTexts = (messages: ChatMessage[]) => {
  const { result } = renderHook(() => useRuntimeMessageRepository(messages))

  return result.current.messages
    .filter(item => item.message.role === 'assistant')
    .map(item =>
      item.message.content.flatMap(part =>
        part.type === 'text' ? [part.text] : part.type === 'tool-call' ? [part.toolCallId] : []
      )
    )
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('a turn reply renders once', () => {
  it('re-attach: the committed row and its live copies render as one reply plus the new tail (#127288)', () => {
    const transcript: ChatMessage[] = [
      { id: 'u1', role: 'user', parts: [text('check the config')], rowId: 6916 },
      {
        id: 'row-6917',
        role: 'assistant',
        rowId: 6917,
        parts: [{ type: 'reasoning', text: 'look first' }, text(ANSWER), tool('call_a', 'cat a'), tool('call_b', 'cat b')]
      },
      { id: 'assistant-stream-1', role: 'assistant', interim: true, parts: [text(ANSWER)] },
      {
        id: 'assistant-stream-2',
        role: 'assistant',
        pending: true,
        parts: [tool('call_a', 'cat a'), tool('call_b', 'cat b'), text(NEXT)]
      }
    ]

    expect(renderedTexts(transcript)).toEqual([[ANSWER, 'call_a', 'call_b'], [NEXT]])
  })

  it('a message.start between the interim seal and the re-stream paints the commentary once (#144609)', async () => {
    const SID = 'turn-reply-fold-144609'
    const COMMENTARY = 'For fidelity the decisive knob is lm_head. Let me verify what each checkpoint does with it.'
    const FINAL = 'Checkpoint B keeps lm_head in full precision; A quantizes it.'
    vi.useFakeTimers()
    const stream = renderMessageStream(SID)

    const send = (type: string, payload: Record<string, unknown>) =>
      act(() => stream.handleEvent({ type: type as never, payload, session_id: SID }))

    const flush = () => act(() => vi.advanceTimersByTime(STREAM_DELTA_FLUSH_MS * 2))

    await send('message.start', {})
    await send('message.interim', { text: COMMENTARY, already_streamed: true })
    await send('message.start', {})
    await send('message.delta', { text: COMMENTARY })
    await flush()
    await send('message.interim', { text: COMMENTARY, already_streamed: true })
    await send('tool.start', { name: 'terminal', tool_id: 'call_v', args: { command: 'ls' } })
    await send('tool.complete', { name: 'terminal', tool_id: 'call_v', result: 'ok' })
    await send('message.delta', { text: FINAL })
    await flush()
    await send('message.complete', { text: FINAL })

    const texts = renderedTexts(stream.state(SID).messages).flat()

    expect(texts.filter(value => value === COMMENTARY)).toHaveLength(1)
    expect(texts.filter(value => value === FINAL)).toHaveLength(1)
  })

  it('a stored prompt delivered twice is one turn, and its reply copies fold', () => {
    const { result } = renderHook(() =>
      useRuntimeMessageRepository([
        { id: 'user-temp-1', role: 'user', parts: [text('check the config')], rowId: 41 },
        { id: 'assistant-stream-1', role: 'assistant', parts: [text(ANSWER)] },
        { id: 'row-41', role: 'user', parts: [text('check the config')], rowId: 41 },
        { id: 'row-42', role: 'assistant', parts: [text(ANSWER)], rowId: 42 }
      ])
    )

    expect(result.current.messages.map(item => item.message.id)).toEqual(['user-temp-1', 'row-42'])
  })

  it('keeps commentary the model authored twice in two tool rounds', () => {
    const transcript: ChatMessage[] = [
      { id: 'u1', role: 'user', parts: [text('run both')] },
      { id: 's1', role: 'assistant', interim: true, parts: [text('Checking.'), tool('call_1', 'ls a')] },
      { id: 's2', role: 'assistant', interim: true, parts: [text('Checking.'), tool('call_2', 'ls b')] },
      { id: 's3', role: 'assistant', parts: [text(NEXT)] }
    ]

    expect(renderedTexts(transcript)).toEqual([['Checking.', 'call_1'], ['Checking.', 'call_2'], [NEXT]])
  })
})
