import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resolveDesktopCommand } from '@/lib/desktop-slash-commands'
import { $connection } from '@/store/session'
import { dropSessionState } from '@/store/session-states'

import { actRender, Harness, type HarnessHandle, RUNTIME_SESSION_ID } from './index-test-utils'
import { clearSingleFlightSessionResumeState } from './single-flight-resume'

// T11 (#134889): Desktop commands the shared gateway refused as `slash.exec`, or that Desktop never
// offered, run on the owner's existing verbs instead.

vi.mock('@/hermes', () => ({
  getLatestSessionMessages: vi.fn(async () => ({ messages: [], session_id: 'session' })),
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSession: vi.fn(),
  PROMPT_SUBMIT_REQUEST_TIMEOUT_MS: 1_800_000,
  setApiRequestProfile: vi.fn(),
  transcribeAudio: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

type Responder = (method: string, params?: Record<string, unknown>) => unknown

function gateway(respond: Responder) {
  return vi.fn(async (method: string, params?: Record<string, unknown>) => (respond(method, params) ?? {}) as never)
}

async function mount(requestGateway: ReturnType<typeof gateway>) {
  let handle: HarnessHandle | null = null
  await actRender(
    <Harness onReady={h => (handle = h)} refreshSessions={async () => undefined} requestGateway={requestGateway} />
  )

  return handle!
}

const callsOf = (requestGateway: ReturnType<typeof gateway>, method: string) =>
  requestGateway.mock.calls.filter(([name]) => name === method).map(([, params]) => params)

const renderedText = (handle: HarnessHandle) =>
  handle
    .state()
    .messages.flatMap(message => message.parts.map(part => ('text' in part ? String(part.text) : '')))
    .join('\n')

beforeEach(() => {
  clearSingleFlightSessionResumeState()
  $connection.set({ mode: 'local', wsUrl: 'ws://localhost/api/ws?native_dial=unminted' } as never)
})

afterEach(() => {
  cleanup()
  $connection.set(null)
  dropSessionState(RUNTIME_SESSION_ID)
  vi.restoreAllMocks()
})

describe('desktop surfaces for /rollback /agents /config /history', () => {
  it('routes each to a local action (never slash.exec, never "terminal only")', () => {
    for (const [name, action] of Object.entries({
      '/rollback': 'rollback',
      '/agents': 'agents',
      '/tasks': 'agents',
      '/config': 'config',
      '/history': 'history'
    })) {
      expect(resolveDesktopCommand(name)?.surface).toEqual({ kind: 'action', action })
    }
  })
})

const CHECKPOINTS = {
  enabled: true,
  checkpoints: [
    { hash: 'aaaa1111', timestamp: '2026-10-18T10:00:00', message: 'before write_file' },
    { hash: 'bbbb2222', timestamp: '2026-10-18T09:00:00', message: 'before patch' }
  ]
}

describe('/rollback', () => {
  it('lists checkpoints through rollback.list', async () => {
    const requestGateway = gateway(method => (method === 'rollback.list' ? CHECKPOINTS : undefined))
    const handle = await mount(requestGateway)

    await handle.submitText('/rollback')

    expect(callsOf(requestGateway, 'rollback.list')).toEqual([{ session_id: RUNTIME_SESSION_ID }])
    expect(callsOf(requestGateway, 'slash.exec')).toEqual([])
    const text = renderedText(handle)
    expect(text).toContain('1. aaaa1111')
    expect(text).toContain('before patch')
  })

  it('says checkpoints are off when the owner reports them disabled', async () => {
    const requestGateway = gateway(method =>
      method === 'rollback.list' ? { enabled: false, checkpoints: [] } : undefined
    )

    const handle = await mount(requestGateway)

    await handle.submitText('/rollback list')

    expect(renderedText(handle)).toMatch(/checkpoints are not enabled/i)
  })

  it('restores checkpoint N (with --all and a file) through rollback.restore', async () => {
    const requestGateway = gateway(method =>
      method === 'rollback.restore' ? { success: true, restored_to: 'aaaa1111', history_removed: 2 } : undefined
    )

    const handle = await mount(requestGateway)

    await handle.submitText('/rollback 1 --all')
    await handle.submitText('/rollback 2 src/app.py')

    expect(callsOf(requestGateway, 'rollback.restore')).toEqual([
      { session_id: RUNTIME_SESSION_ID, hash: '1', all: true },
      { session_id: RUNTIME_SESSION_ID, hash: '2', file_path: 'src/app.py' }
    ])
    expect(renderedText(handle)).toContain('aaaa1111')
  })

  it('shows a failed restore as an error, and diffs through rollback.diff', async () => {
    const requestGateway = gateway(method => {
      if (method === 'rollback.restore') {
        return { success: false, error: 'Invalid checkpoint number. Use 1-2.' }
      }

      if (method === 'rollback.diff') {
        return { stat: ' a.py | 2 +-', diff: '-old\n+new' }
      }
    })

    const handle = await mount(requestGateway)

    await handle.submitText('/rollback 9')
    await handle.submitText('/rollback diff 1')

    expect(callsOf(requestGateway, 'rollback.diff')).toEqual([{ session_id: RUNTIME_SESSION_ID, hash: '1' }])
    const text = renderedText(handle)
    expect(text).toContain('Invalid checkpoint number')
    expect(text).toContain('+new')
  })
})

describe('/agents', () => {
  it("reads this session's subagents and background processes from the owner", async () => {
    const requestGateway = gateway(method => {
      if (method === 'subagent.list') {
        return {
          subagents: [{ subagent_id: 'sa-1', depth: 1, goal: 'research the API', status: 'running', tool_count: 3 }]
        }
      }

      if (method === 'process.list') {
        return { processes: [{ session_id: 'proc_1', command: 'npm run dev', status: 'running' }] }
      }
    })

    const handle = await mount(requestGateway)

    await handle.submitText('/tasks')

    expect(callsOf(requestGateway, 'subagent.list')).toEqual([{ session_id: RUNTIME_SESSION_ID }])
    expect(callsOf(requestGateway, 'process.list')).toEqual([{ session_id: RUNTIME_SESSION_ID }])
    expect(callsOf(requestGateway, 'slash.exec')).toEqual([])
    const text = renderedText(handle)
    expect(text).toContain('research the API')
    expect(text).toContain('npm run dev')
  })

  it('says nothing is running when both lists are empty', async () => {
    const requestGateway = gateway(method =>
      method === 'subagent.list' ? { subagents: [] } : method === 'process.list' ? { processes: [] } : undefined
    )

    const handle = await mount(requestGateway)

    await handle.submitText('/agents')

    expect(renderedText(handle)).toMatch(/no active subagents or background processes/i)
  })
})

describe('/config', () => {
  it("shows the profile's client config projection from config.get", async () => {
    const requestGateway = gateway(method =>
      method === 'config.get' ? { config: { display: { skin: 'mono', timestamps: true } } } : undefined
    )

    const handle = await mount(requestGateway)

    await handle.submitText('/config')

    expect(callsOf(requestGateway, 'config.get')).toEqual([{ session_id: RUNTIME_SESSION_ID, key: 'full' }])
    const text = renderedText(handle)
    expect(text).toContain('skin: mono')
    expect(text).toContain('Settings')
  })
})

describe('/history', () => {
  it("renders the owner's transcript snapshot", async () => {
    const requestGateway = gateway(method =>
      method === 'session.resume'
        ? {
            messages: [
              { role: 'user', content: 'first question', row_id: 1 },
              { role: 'assistant', content: [{ type: 'text', text: 'first answer' }], row_id: 2 },
              { role: 'tool', content: 'tool noise', row_id: 3 }
            ]
          }
        : undefined
    )

    const handle = await mount(requestGateway)

    await handle.submitText('/history')

    expect(callsOf(requestGateway, 'session.resume')).toEqual([{ session_id: RUNTIME_SESSION_ID }])
    expect(callsOf(requestGateway, 'slash.exec')).toEqual([])
    const text = renderedText(handle)
    expect(text).toContain('You: first question')
    expect(text).toContain('Hermes: first answer')
    expect(text).not.toContain('tool noise')
  })
})
