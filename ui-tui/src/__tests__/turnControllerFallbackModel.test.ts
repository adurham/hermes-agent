import { beforeEach, describe, expect, it } from 'vitest'

import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'

// turnController.upsertSubagent() carries the fallback-indicator fields
// (model, provider, fallback_active, primary_model, primary_provider,
// model_label) from the wire payload onto SubagentProgress, and must not let
// a later event that omits a field clobber a value learned from an earlier
// one — the same merge idiom as every other field in this function.
describe('turnController.upsertSubagent — fallback fields', () => {
  beforeEach(() => {
    resetTurnState()
    turnController.fullReset()
  })

  it('carries the new fields from a subagent.start payload', () => {
    turnController.upsertSubagent(
      {
        fallback_active: true,
        goal: 'do work',
        model: 'claude-opus-5',
        model_label: '⚠ claude-opus-5 (fallback from glm-5.3)',
        primary_model: 'glm-5.3',
        primary_provider: 'zhipu',
        provider: 'anthropic',
        subagent_id: 'a1',
        task_index: 0
      },
      c => ({ status: 'running' })
    )

    const item = getTurnState().subagents[0]
    expect(item?.model).toBe('claude-opus-5')
    expect(item?.provider).toBe('anthropic')
    expect(item?.fallbackActive).toBe(true)
    expect(item?.primaryModel).toBe('glm-5.3')
    expect(item?.primaryProvider).toBe('zhipu')
    expect(item?.modelLabel).toBe('⚠ claude-opus-5 (fallback from glm-5.3)')
  })

  it('defaults every new field to undefined when the gateway omits them (older backend)', () => {
    turnController.upsertSubagent({ goal: 'do work', subagent_id: 'a1', task_index: 0 }, c => ({ status: 'running' }))

    const item = getTurnState().subagents[0]
    expect(item?.fallbackActive).toBeUndefined()
    expect(item?.primaryModel).toBeUndefined()
    expect(item?.primaryProvider).toBeUndefined()
    expect(item?.provider).toBeUndefined()
    expect(item?.modelLabel).toBeUndefined()
  })

  it('a later event omitting fallback fields does not clobber previously-known values', () => {
    turnController.upsertSubagent(
      {
        fallback_active: true,
        goal: 'do work',
        model: 'claude-opus-5',
        model_label: '⚠ claude-opus-5 (fallback from glm-5.3)',
        primary_model: 'glm-5.3',
        primary_provider: 'zhipu',
        provider: 'anthropic',
        subagent_id: 'a1',
        task_index: 0
      },
      c => ({ status: 'running' })
    )

    // A subsequent progress event carries only `text` — no model/fallback
    // fields at all — as real subagent.progress events do.
    turnController.upsertSubagent(
      { goal: 'do work', subagent_id: 'a1', task_index: 0, text: 'still working' },
      c => ({ status: 'running' }),
      { createIfMissing: false }
    )

    const item = getTurnState().subagents[0]
    expect(item?.model).toBe('claude-opus-5')
    expect(item?.provider).toBe('anthropic')
    expect(item?.fallbackActive).toBe(true)
    expect(item?.primaryModel).toBe('glm-5.3')
    expect(item?.primaryProvider).toBe('zhipu')
    expect(item?.modelLabel).toBe('⚠ claude-opus-5 (fallback from glm-5.3)')
  })

  it('a later event can flip fallbackActive back to false once the primary model recovers', () => {
    turnController.upsertSubagent(
      {
        fallback_active: true,
        goal: 'do work',
        model: 'claude-opus-5',
        primary_model: 'glm-5.3',
        subagent_id: 'a1',
        task_index: 0
      },
      c => ({ status: 'running' })
    )

    turnController.upsertSubagent(
      { fallback_active: false, goal: 'do work', model: 'glm-5.3', subagent_id: 'a1', task_index: 0 },
      c => ({ status: 'running' }),
      { createIfMissing: false }
    )

    const item = getTurnState().subagents[0]
    expect(item?.fallbackActive).toBe(false)
    expect(item?.model).toBe('glm-5.3')
  })
})
