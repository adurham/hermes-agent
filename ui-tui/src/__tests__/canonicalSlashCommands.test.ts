import { beforeEach, expect, it, vi } from 'vitest'

import { createSlashHandler } from '../app/createSlashHandler.js'
import { noteCanonicalCompletion } from '../app/slash/canonicalSessionCommands.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'

// pastels §2.5: on the shared (canonical) gateway Ink's /title /undo /retry /usage /tools must use
// the verbs that gateway serves — never the legacy session.title / session.undo / session.usage /
// slash.exec RPCs it answers with unknown_method / unsupported_command.

const flush = () => new Promise(resolve => setImmediate(resolve))
const LEGACY = ['session.title', 'session.undo', 'session.usage', 'slash.exec', 'tools.configure']

function harness() {
  const messages = [
    { role: 'user', content: 'first', row_id: 11 },
    { role: 'assistant', content: 'a1', row_id: 12 },
    { role: 'user', content: 'second question', row_id: 13 },
    { role: 'assistant', content: 'a2', row_id: 14 }
  ]

  const request = vi.fn(async (method: string, params: any) => {
    if (method === 'session.resume') {
      return { session_id: 'owner', revision: 7, execution_generation: 3, messages }
    }

    if (method === 'session.mutate' && params.operation === 'rename') {
      return { session_id: 'owner', operation: 'rename', revision: 8, title: params.payload.title }
    }

    if (method === 'session.mutate' && params.operation === 'rewind') {
      return {
        session_id: 'owner',
        operation: 'rewind',
        revision: 8,
        execution_generation: 4,
        rewound_count: 2,
        target_message: { role: 'user', content: 'second question' }
      }
    }

    if (method === 'session.mutate' && params.operation === 'reasoning') {
      return { session_id: 'owner', operation: 'reasoning', revision: 8, execution_generation: 4, effort: params.payload.effort }
    }

    if (method === 'session.stats') {
      return {
        api_calls: 2, compression_count: 0, context_length: 128000, context_source: 'provider_usage',
        input_tokens: 200, last_prompt_tokens: 100, model: 'fake-model', output_tokens: 40, prompt_tokens: 200,
        total_tokens: 240
      }
    }

    if (method === 'prompt.receipt') {
      return { status: 'terminal', result: { model: 'm', input_tokens: 5, output_tokens: 7, total_tokens: 12, api_calls: 1 } }
    }

    if (method === 'session.info') {
      return { launch_request: { toolsets: ['terminal', 'web'] } }
    }

    if (method === 'config.get') {
      return { value: params.key === 'skin' ? 'default' : 'auto' }
    }

    if (method === 'config.set') {
      return { key: params.key, value: params.value }
    }

    if (method === 'slash.exec' && params.command === 'status') {
      return { output: 'Session: owner' }
    }

    throw new Error(`unknown_method: ${method}`)
  })

  const sys = vi.fn()
  const send = vi.fn()
  const panel = vi.fn()
  const page = vi.fn()

  const ctx = {
    slashFlightRef: { current: 0 },
    gateway: { gw: { isCanonical: true, request }, rpc: request },
    transcript: { page, panel, send, sys, setHistoryItems: vi.fn(), trimLastExchange: (items: unknown[]) => items },
    local: { getHistoryItems: () => messages, getLastUserMsg: () => 'typed elsewhere', maybeWarn: vi.fn() },
    session: { resumeById: vi.fn() },
    composer: { enqueue: vi.fn() }
  }

  return { page, panel, request, send, slash: createSlashHandler(ctx as any), sys }
}

beforeEach(() => {
  resetUiState()
  patchUiState({ sid: 'owner', info: { model: 'm', skills: {}, tools: {}, execution_epoch: '1', execution_generation: 3 } as any })
})

it('/title renames and /undo, /retry rewind through session.mutate with the snapshot CAS and row id', async () => {
  const { request, send, slash, sys } = harness()

  slash('/title Probe Renamed')
  await flush()
  expect(request).toHaveBeenCalledWith('session.mutate', {
    session_id: 'owner',
    request_id: expect.any(String),
    expected_revision: 7,
    operation: 'rename',
    payload: { title: 'Probe Renamed' }
  })
  expect(getUiState().sessionTitle).toBe('Probe Renamed')

  for (const command of ['/undo', '/retry']) {
    slash(command)
    await flush()
  }

  const rewinds = request.mock.calls.filter(([method, params]) => method === 'session.mutate' && params.operation === 'rewind')

  expect(rewinds).toHaveLength(2)

  for (const [, params] of rewinds) {
    expect(params).toMatchObject({
      expected_revision: 7,
      expected_generation: 3,
      payload: { target_message_id: 13 }
    })
  }

  expect(sys).toHaveBeenCalledWith('undid 2 messages')
  // Main's /retry resubmits the rewound turn's own text.
  expect(send).toHaveBeenCalledWith('second question')
  expect(request.mock.calls.some(([method]) => LEGACY.includes(method))).toBe(false)
})

it("/usage renders main's session totals from session.stats and /tools says what the shared gateway lacks", async () => {
  const { panel, request, slash, sys } = harness()

  noteCanonicalCompletion('owner', 'adm-1')
  slash('/usage')
  await flush()
  expect(request).toHaveBeenCalledWith('session.stats', { session_id: 'owner' })
  // Main's one Usage panel with session totals and the context line; no "last turn" panel.
  expect(panel).toHaveBeenCalledTimes(1)
  expect(panel).toHaveBeenCalledWith('Usage', [
    {
      rows: [
        ['Model', 'fake-model'],
        ['Input tokens', '200'],
        ['Output tokens', '40'],
        ['Total tokens', '240'],
        ['API calls', '2']
      ]
    },
    { text: expect.stringContaining('100 / 128,000 (0%)') }
  ])
  expect(sys.mock.calls.some(([line]) => /not available on the shared gateway/.test(line))).toBe(false)
  expect(getUiState().usage).toMatchObject({ calls: 2, total: 240, context_max: 128000 })

  slash('/tools')
  await flush()
  expect(sys).toHaveBeenCalledWith('toolsets fixed at session launch: terminal, web')
  expect(sys).toHaveBeenCalledWith('/tools is not available on the shared gateway yet')
  expect(sys.mock.calls.some(([line]) => /request_failed|unsupported_command|unknown_method/.test(line))).toBe(false)
  expect(request.mock.calls.some(([method]) => LEGACY.includes(method))).toBe(false)
})

it('/status reads through the gateway', async () => {
  const { page, request, slash } = harness()

  slash('/status')
  await flush()
  expect(request).toHaveBeenCalledWith('slash.exec', { command: 'status', session_id: 'owner' })
  expect(page).toHaveBeenCalledWith('Session: owner', expect.any(String))
  expect(request.mock.calls.map(([method]) => method)).not.toContain('session.status')
})

it('/stop is session-scoped; process-global /reload-mcp refuses on the shared gateway', async () => {
  const { request, slash, sys } = harness()

  for (const cmd of ['/stop', '/reload-mcp now']) {
    slash(cmd)
  }

  await flush()

  // dokterdok N2: a sessionless process.stop was the owner-wide kill_all.
  expect(request).toHaveBeenCalledWith('process.stop', { session_id: 'owner' })

  const methods = request.mock.calls.map(([method]) => method)

  for (const method of ['reload.mcp']) {
    expect(methods).not.toContain(method)
  }

  const notices = sys.mock.calls.map(([text]) => String(text))

  for (const name of ['reload-mcp']) {
    expect(notices).toContain(`/${name} is not available on the shared gateway yet`)
  }
})

it('setting writes the shared gateway has no verb for say so instead of a bare invalid_params; reads still work', async () => {
  const { request, slash, sys } = harness()

  for (const cmd of ['/reasoning high --global', '/fast fast', '/personality pirate', '/skin']) {
    slash(cmd)
  }

  await flush()

  expect(request.mock.calls.map(([method]) => method)).not.toContain('config.set')
  expect(request).toHaveBeenCalledWith('config.get', { key: 'skin' })

  const notices = sys.mock.calls.map(([text]) => String(text))

  for (const name of ['reasoning', 'fast', 'personality']) {
    expect(notices).toContain(`/${name} is not available on the shared gateway yet`)
  }

  expect(notices.some(line => /skin: default/.test(line))).toBe(true)
})

it('/skin writes the profile through the owner\'s config.set like main (the owner repaints via skin.changed)', async () => {
  const { request, slash, sys } = harness()

  slash('/skin mono')
  await flush()

  expect(request).toHaveBeenCalledWith('config.set', { key: 'skin', value: 'mono' })
  expect(sys.mock.calls.map(([text]) => String(text))).toEqual(['skin → mono'])
})

it('/theme and /indicator write the profile through the owner\'s config.set like main', async () => {
  const { request, slash, sys } = harness()

  slash('/theme dark')
  await flush()
  slash('/indicator ascii')
  await flush()

  expect(request).toHaveBeenCalledWith('config.set', { key: 'theme', value: 'dark' })
  expect(request).toHaveBeenCalledWith('config.set', { key: 'indicator', value: 'ascii' })

  const notices = sys.mock.calls.map(([text]) => String(text))

  expect(notices).toEqual(expect.arrayContaining(['theme → dark', 'indicator → ascii']))
  expect(notices.some(line => /not available/.test(line))).toBe(false)
  expect(getUiState().indicatorStyle).toBe('ascii')
})

it('session /reasoning <level> is the owner\'s fenced reasoning mutation (B14); display words still refuse', async () => {
  const { request, slash, sys } = harness()

  slash('/reasoning high')
  await flush()
  slash('/reasoning show')
  await flush()

  expect(request).toHaveBeenCalledWith('session.mutate', expect.objectContaining({
    session_id: 'owner', operation: 'reasoning', payload: { effort: 'high' }, expected_revision: 7, expected_generation: 3
  }))
  expect(request.mock.calls.map(([method]) => method)).not.toContain('config.set')

  const notices = sys.mock.calls.map(([text]) => String(text))

  expect(notices.some(line => /high/.test(line))).toBe(true)
  expect(notices).toContain('/reasoning is not available on the shared gateway yet')
})
