import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { chatMessageText } from '@/lib/chat-messages'
import { clearSessionTodos } from '@/store/todos'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'session-1'

let stream: MessageStreamHarness

function mountStream() {
  stream = renderMessageStream(SID)
}

const start = () => act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))

const delta = (text: string) =>
  act(() => stream.handleEvent({ payload: { text }, session_id: SID, type: 'message.delta' }))

const interim = (text: string) =>
  act(() => stream.handleEvent({ payload: { text, already_streamed: true }, session_id: SID, type: 'message.interim' }))

const toolStart = (toolId: string, name: string) =>
  act(() => stream.handleEvent({ payload: { args: {}, name, tool_id: toolId }, session_id: SID, type: 'tool.start' }))

const toolComplete = (toolId: string, name: string) =>
  act(() =>
    stream.handleEvent({ payload: { name, result: 'ok', tool_id: toolId }, session_id: SID, type: 'tool.complete' })
  )

const complete = (text: string) =>
  act(() => stream.handleEvent({ payload: { text }, session_id: SID, type: 'message.complete' }))

/** The turn-close contract: the model streamed a draft, called `end_turn`, and the harness
 *  delivered that call's summary over the draft's content (`final_content_replaced`). */
const completeReplaced = (text: string) =>
  act(() =>
    stream.handleEvent({ payload: { final_content_replaced: true, text }, session_id: SID, type: 'message.complete' })
  )

function getState(): ClientSessionState {
  return stream.state()
}

function assistantMessages() {
  return getState().messages.filter(m => m.role === 'assistant' && !m.hidden)
}

function assistantTexts(): string[] {
  return assistantMessages()
    .map(m => chatMessageText(m))
    .filter(Boolean)
}

const DRAFT = 'Done — consult now runs Fable 5.2.\n\nWhat I changed:\n- updated the CLI\n- set the config key'
const SUMMARY = 'Consult default is now Fable 5.2, verified live. CLI updated first, config set in both profiles.'

describe('useMessageStream turn-close replacement settlement', () => {
  beforeEach(() => {
    clearSessionTodos(SID)
  })

  afterEach(() => {
    cleanup()
    clearSessionTodos(SID)
    vi.restoreAllMocks()
  })

  it('replaces the streamed draft with the end_turn summary instead of painting the reply twice', async () => {
    mountStream()
    await start()

    // A tool round, then the draft the model streamed, then the close's own tool chip — the
    // draft bubble carries text ABOVE that chip, which a merge would keep as confirmed text.
    await toolStart('t1', 'terminal')
    await toolComplete('t1', 'terminal')
    await delta(DRAFT)
    await toolStart('t2', 'end_turn')
    await toolComplete('t2', 'end_turn')
    await interim(DRAFT)
    await completeReplaced(SUMMARY)

    const messages = assistantMessages()

    expect(assistantTexts()).toEqual([SUMMARY])
    // The tool chips survive; only the replaced draft text is gone.
    expect(messages.at(-1)?.parts.some(part => part.type === 'tool-call')).toBe(true)
    expect(chatMessageText(messages.at(-1)!)).not.toContain('What I changed')
  })

  it('drops the rejected prose AND the trailing prose around the end_turn chip', async () => {
    mountStream()
    await start()

    // The recorded shape: a tool round, a text-only ending the harness rejected (still
    // streamed to the screen), the retried `end_turn` call, then the model's trailing prose
    // once the close tool returned. The completion carries the summary — the one reply.
    await toolStart('t1', 'terminal')
    await toolComplete('t1', 'terminal')
    await delta(DRAFT)
    await toolStart('t2', 'end_turn')
    await toolComplete('t2', 'end_turn')
    await delta('Closing note that restates the report.')
    await completeReplaced(SUMMARY)

    const messages = assistantMessages()

    expect(assistantTexts()).toEqual([SUMMARY])
    expect(chatMessageText(messages.at(-1)!)).not.toContain('What I changed')
    expect(chatMessageText(messages.at(-1)!)).not.toContain('Closing note')
    expect(messages.at(-1)?.parts.filter(part => part.type === 'tool-call')).toHaveLength(2)
  })

  it('keeps narration from earlier tool rounds when the close replaces the draft', async () => {
    mountStream()
    await start()

    await delta('Checking the logs first.')
    await toolStart('t1', 'terminal')
    await toolComplete('t1', 'terminal')
    await delta(DRAFT)
    await toolStart('t2', 'end_turn')
    await toolComplete('t2', 'end_turn')
    await delta('Trailing restatement.')
    await completeReplaced(SUMMARY)

    const text = assistantMessages()
      .map(m => chatMessageText(m))
      .join('\n')

    expect(text).toContain('Checking the logs first.')
    expect(text).toContain(SUMMARY)
    expect(text).not.toContain('What I changed')
    expect(text).not.toContain('Trailing restatement.')
  })

  it('does not paint a second bubble when the same close is delivered twice', async () => {
    mountStream()
    await start()

    // The shape that duplicated in the wild: the close's summary settles ABOVE its own
    // `end_turn` chip, so the bubble's suffix after the last tool call is empty. A repeat
    // frame of the same turn (a re-delivered message.complete) compared only that suffix,
    // saw nothing, and appended the answer a second time.
    await toolStart('t1', 'terminal')
    await toolComplete('t1', 'terminal')
    await delta(DRAFT)
    await toolStart('t2', 'end_turn')
    await toolComplete('t2', 'end_turn')
    await interim(DRAFT)
    await completeReplaced(SUMMARY)
    await completeReplaced(SUMMARY)

    expect(assistantTexts()).toEqual([SUMMARY])
  })

  it('never lets a replaced completion overwrite an already-settled reply', async () => {
    mountStream()
    await start()
    await interim('old')
    await complete('old')

    // The interim boundary is spent: a late replaced final is a distinct reply.
    await completeReplaced('new')

    expect(assistantTexts()).toEqual(['old', 'new'])
  })
})
