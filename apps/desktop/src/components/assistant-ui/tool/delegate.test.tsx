import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $activeSessionId } from '@/store/session'
import { $subagentsBySession, type SubagentProgress } from '@/store/subagents'

import { DelegateTool } from './delegate'

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

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  $subagentsBySession.set({})
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

    expect(screen.getByText('⚠ Opus 5 (fallback from Glm 5.3)')).toBeTruthy()
  })

  it('shows no glyph for an older backend that never sends the new fields', () => {
    $activeSessionId.set('sess-1')
    $subagentsBySession.set({ 'sess-1': [subagent({ model: 'anthropic/claude-opus-5' })] })

    render(<DelegateTool args={{ tasks: [{ goal: 'Research Cursor' }] }} result={undefined} toolCallId="call-3" />)

    expect(screen.queryByText(/⚠/)).toBeNull()
  })
})
