import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const inputHarness = vi.hoisted(() => ({
  handler: undefined as undefined | ((input: string, key: Record<string, boolean>) => void)
}))

// Stub useInput so the overlay doesn't try to enter raw mode under renderSync
// (PassThrough stdin doesn't support it). Box/Text pass through to real Ink.
vi.mock('@hermes/ink', async importOriginal => {
  const mod = await importOriginal()

  return {
    ...mod,
    useInput: (handler: (input: string, key: Record<string, boolean>) => void) => {
      inputHarness.handler = handler
    }
  }
})

import { resetDelegationState } from '../app/delegationStore.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { clearSpawnHistory } from '../app/spawnHistoryStore.js'
import { patchTurnState, resetTurnState } from '../app/turnStore.js'
import { AgentsOverlay } from '../components/agentsOverlay.js'
import type { GatewayClient } from '../gatewayClient.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'
import type { SubagentProgress } from '../types.js'

const t = DEFAULT_THEME

// gw.request('delegation.status', {}) fires once on mount; a never-resolving
// promise is fine here — applyDelegationStatus() only runs on success.
const gw = {
  request: () => new Promise<never>(() => {}),
  send: () => {}
} as unknown as GatewayClient

const subagent = (overrides: Partial<SubagentProgress> = {}): SubagentProgress => ({
  depth: 0,
  goal: 'do work',
  id: 'a1',
  index: 0,
  notes: [],
  parentId: null,
  status: 'running',
  taskCount: 1,
  thinking: [],
  toolCount: 0,
  tools: [],
  ...overrides
})

/** Mount AgentsOverlay via renderSync + PassThrough, drive it into detail
 *  mode (where the per-node `model` field renders), and return the plain-
 *  text output. */
async function renderDetail(subagents: SubagentProgress[]): Promise<string> {
  patchTurnState({ subagents })

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  let output = ''

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  inputHarness.handler = undefined
  const element = React.createElement(AgentsOverlay, { gw, onClose: () => {}, t })

  const instance = renderSync(element, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  // Enter detail mode on the first (selected-by-default) row. The mode flip
  // is a React state update, not a synchronous rerender() call, so wait for
  // it to actually land (detail mode's "depth ·" field line is the tell).
  inputHarness.handler?.('', { return: true })
  await vi.waitFor(() => expect(stripAnsi(output)).toContain('depth ·'))

  const text = stripAnsi(output)

  instance.unmount()
  instance.cleanup()

  return text
}

describe('AgentsOverlay — fallback model indicator', () => {
  beforeEach(() => {
    resetTurnState()
    resetDelegationState()
    resetOverlayState()
    clearSpawnHistory()
  })

  it('renders the plain model name when not in fallback', async () => {
    const out = await renderDetail([subagent({ model: 'glm-5.3' })])

    expect(out).toContain('glm-5.3')
    expect(out).not.toContain('⚠')
  })

  it('renders the fallback glyph + swap when fallbackActive is true', async () => {
    const out = await renderDetail([subagent({ fallbackActive: true, model: 'claude-opus-5', primaryModel: 'glm-5.3' })])

    expect(out).toContain('⚠')
    expect(out).toContain('claude-opus-5')
    expect(out).toContain('fallback from glm-5.3')
  })

  it('prefers the backend-provided modelLabel when present', async () => {
    const out = await renderDetail([
      subagent({
        fallbackActive: true,
        model: 'claude-opus-5',
        modelLabel: '⚠ backend-composed label',
        primaryModel: 'glm-5.3'
      })
    ])

    expect(out).toContain('⚠ backend-composed label')
  })

  it('shows no glyph for an older gateway that never sends the new fields', async () => {
    const out = await renderDetail([
      subagent({ fallbackActive: undefined, model: 'glm-5.3', primaryModel: undefined })
    ])

    expect(out).toContain('glm-5.3')
    expect(out).not.toContain('⚠')
  })
})
