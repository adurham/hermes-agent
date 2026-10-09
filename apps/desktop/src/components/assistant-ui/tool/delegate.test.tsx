import { cleanup, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { I18nProvider } from '@/i18n/context'
import type { ChatMessage } from '@/lib/chat-messages'
import { $activeSessionId } from '@/store/session'
import {
  $subagentsBySession,
  clearSessionSubagents,
  type SubagentProgress,
  upsertSubagent
} from '@/store/subagents'

import { DelegateTool } from './delegate'

const SESSION = 'sess-delegate-test'

const subagent = (overrides: Partial<SubagentProgress> = {}): SubagentProgress => ({
  filesRead: [],
  filesWritten: [],
  goal: 'Research Cursor',
  id: 'sub-1',
  parentId: null,
  startedAt: 0,
  status: 'completed',
  stream: [],
  taskCount: 1,
  taskIndex: 0,
  updatedAt: 0,
  ...overrides
})

function view(): SessionView {
  return {
    kind: 'primary',
    $runtimeId: atom<string | null>(SESSION),
    $storedId: atom<string | null>(null),
    $messages: atom<ChatMessage[]>([]),
    $busy: atom(false),
    $awaitingResponse: atom(false),
    $messagesEmpty: atom(true),
    $lastVisibleIsUser: atom(false),
    $turnStartedAt: atom<null | number>(null),
    $cwd: atom(''),
    $model: atom(''),
    $provider: atom(''),
    $fast: atom(false),
    $reasoningEffort: atom(''),
    $reasoningEffortPending: atom(false),
    $reasoningEffortWire: atom('')
  } satisfies SessionView
}

function renderCard() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <SessionViewProvider value={view()}>
        <DelegateTool
          args={{ tasks: [{ goal: 'Inspect the delegate card' }] }}
          result={undefined}
          toolCallId="call-1"
        />
      </SessionViewProvider>
    </I18nProvider>
  )
}

afterEach(() => {
  cleanup()
  clearSessionSubagents(SESSION)
  $subagentsBySession.set({})
  $activeSessionId.set(null)
})

describe('delegate card fade', () => {
  // Scaffold opacity opens a stacking context. The activity ticker is a
  // transformed reel clipped to one line; when the mark sat on the wrapper
  // around both, the reel's clip degraded and every old activity line painted
  // through the current one (#105579). The mark belongs on the goal row; the
  // ticker stays outside it.
  it('does not put the activity ticker inside a scaffold fade', () => {
    $activeSessionId.set(SESSION)
    upsertSubagent(
      SESSION,
      {
        goal: 'Inspect the delegate card',
        status: 'running',
        subagent_id: 'delegate-tool:call-1:0',
        task_index: 0,
        text: 'Thinking about the card'
      },
      true,
      'subagent.thinking'
    )
    upsertSubagent(
      SESSION,
      {
        subagent_id: 'delegate-tool:call-1:0',
        task_index: 0,
        tool_name: 'terminal',
        tool_preview: 'git status'
      },
      false,
      'subagent.tool'
    )

    const { container } = renderCard()
    const ticker = container.querySelector('[data-tool-ticker]')
    const marked = container.querySelector('[data-conversation-scaffold]')

    expect(ticker).not.toBeNull()
    expect(marked).not.toBeNull()
    expect(ticker?.closest('[data-conversation-scaffold]')).toBeNull()
    // The card wrapper itself carries no mark — the fade is per surface, never
    // on a container (styles.css invariant).
    expect(container.querySelector('[data-delegate-card]')?.hasAttribute('data-conversation-scaffold')).toBe(false)
  })
})

// The delegate tool-call card is one of the three independent render sites
// for a subagent's model name — this proves the fallback indicator actually
// reaches the DOM, not just the data layer feeding it.
describe('DelegateTool — fallback model indicator', () => {
  it('renders the plain model name when not in fallback', () => {
    $activeSessionId.set('sess-1')
    $subagentsBySession.set({ 'sess-1': [subagent({ model: 'anthropic/claude-opus-5' })] })

    render(<DelegateTool args={{ tasks: [{ goal: 'Research Cursor' }] }} result={undefined} toolCallId="call-1" />)

    expect(screen.getByText('Opus 5')).toBeTruthy()
    expect(screen.queryByText(/⚠/)).toBeNull()
  })

  it('renders the fallback glyph + swap when fallbackActive is true', () => {
    $activeSessionId.set('sess-1')
    $subagentsBySession.set({
      'sess-1': [subagent({ fallbackActive: true, model: 'anthropic/claude-opus-5', primaryModel: 'glm-5.3' })]
    })

    render(<DelegateTool args={{ tasks: [{ goal: 'Research Cursor' }] }} result={undefined} toolCallId="call-2" />)

    expect(screen.getByText('⚠ Opus 5 (fallback from GLM 5.3)')).toBeTruthy()
  })

  it('shows no glyph for an older backend that never sends the new fields', () => {
    $activeSessionId.set('sess-1')
    $subagentsBySession.set({ 'sess-1': [subagent({ model: 'anthropic/claude-opus-5' })] })

    render(<DelegateTool args={{ tasks: [{ goal: 'Research Cursor' }] }} result={undefined} toolCallId="call-3" />)

    expect(screen.queryByText(/⚠/)).toBeNull()
  })
})
