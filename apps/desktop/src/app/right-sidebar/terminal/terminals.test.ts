import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const STORAGE_KEY = 'hermes.desktop.terminals.v1'

async function loadTerminalStore() {
  vi.doMock('@/store/session', () => ({
    $currentCwd: atom('/workspace')
  }))

  return import('./terminals')
}

describe('terminal store persistence', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('restores user tabs, active tab, and history on module load', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'term-two',
        terminals: [
          { auto: false, cwd: '/repo/one', id: 'term-one', reviveBuffer: 'last output', title: 'zsh' },
          { auto: true, cwd: '/repo/two', id: 'term-two', title: 'Terminal' }
        ]
      })
    )

    const { $activeTerminalId, $terminals } = await loadTerminalStore()

    expect($activeTerminalId.get()).toBe('term-two')
    expect($terminals.get()).toEqual([
      { auto: false, cwd: '/repo/one', id: 'term-one', kind: 'user', reviveBuffer: 'last output', title: 'zsh' },
      { auto: true, cwd: '/repo/two', id: 'term-two', kind: 'user', title: 'Terminal' }
    ])
  })

  it('persists user tabs and history synchronously, skipping agent mirrors', async () => {
    const { createTerminal, ensureAgentTerminal, renameTerminal, selectTerminal, updateTerminalReviveBuffer } =
      await loadTerminalStore()

    const userId = createTerminal('/repo')
    renameTerminal(userId, 'server')
    updateTerminalReviveBuffer(userId, 'recent scrollback')
    ensureAgentTerminal('proc-1', 'background task')
    selectTerminal(userId)

    // No flush/tick: persistence is synchronous, so the snapshot is already on
    // disk (this is what makes app-quit restore reliable).
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({
      activeTerminalId: userId,
      terminals: [{ auto: false, cwd: '/repo', id: userId, reviveBuffer: 'recent scrollback', title: 'server' }]
    })
  })

  it('never attaches a revive buffer to an agent tab', async () => {
    const { $terminals, ensureAgentTerminal, updateTerminalReviveBuffer } = await loadTerminalStore()

    const agentId = ensureAgentTerminal('proc-1', 'background task')!
    updateTerminalReviveBuffer(agentId, 'should be ignored')

    expect($terminals.get().find(term => term.id === agentId)?.reviveBuffer).toBeUndefined()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('tail-trims an oversized revive buffer to stay under the storage budget', async () => {
    const { $terminals, createTerminal, updateTerminalReviveBuffer } = await loadTerminalStore()

    const userId = createTerminal('/repo')
    const huge = 'x'.repeat(60_000)
    updateTerminalReviveBuffer(userId, huge)

    const stored = $terminals.get().find(term => term.id === userId)?.reviveBuffer ?? ''
    expect(stored.length).toBe(48_000)
    expect(stored).toBe(huge.slice(-48_000))
  })

  it('clears remembered tabs when all terminals close', async () => {
    const { closeAllTerminals, createTerminal } = await loadTerminalStore()

    createTerminal('/repo')
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull()

    closeAllTerminals()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('restores and persists the last observed cwd so a reopened tab lands where the user cd-d', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'term-one',
        terminals: [{ auto: false, cwd: '/repo', id: 'term-one', restoreCwd: '/repo/packages/api', title: 'zsh' }]
      })
    )

    const { $terminals, updateTerminalRestoreCwd } = await loadTerminalStore()

    expect($terminals.get()[0]?.restoreCwd).toBe('/repo/packages/api')

    updateTerminalRestoreCwd('term-one', '/repo/packages/web')
    expect($terminals.get()[0]?.restoreCwd).toBe('/repo/packages/web')
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}').terminals[0].restoreCwd).toBe(
      '/repo/packages/web'
    )
  })

  it('never attaches a restore cwd to an agent tab and ignores empty values', async () => {
    const { $terminals, createTerminal, ensureAgentTerminal, updateTerminalRestoreCwd } = await loadTerminalStore()

    const userId = createTerminal('/repo')
    const agentId = ensureAgentTerminal('proc-1', 'background task')!

    updateTerminalRestoreCwd(agentId, '/somewhere')
    updateTerminalRestoreCwd(userId, '   ')

    expect($terminals.get().find(term => term.id === agentId)?.restoreCwd).toBeUndefined()
    expect($terminals.get().find(term => term.id === userId)?.restoreCwd).toBeUndefined()
  })
})

describe('ensureProjectTerminal', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('creates a project-bound tab at the given cwd and focuses it', async () => {
    const { $activeTerminalId, $terminals, ensureProjectTerminal } = await loadTerminalStore()

    const id = ensureProjectTerminal('p_abc', '/repo/project-a')

    expect($activeTerminalId.get()).toBe(id)
    const term = $terminals.get().find(t => t.id === id)
    expect(term).toMatchObject({ cwd: '/repo/project-a', projectId: 'p_abc' })
  })

  it('reuses the same tab on repeat calls instead of creating a new one', async () => {
    const { $terminals, ensureProjectTerminal } = await loadTerminalStore()

    const first = ensureProjectTerminal('p_abc', '/repo/project-a')
    const second = ensureProjectTerminal('p_abc', '/repo/project-a')

    expect(second).toBe(first)
    expect($terminals.get().filter(t => t.projectId === 'p_abc')).toHaveLength(1)
  })

  it('gives each project its own tab and switches between them', async () => {
    const { $activeTerminalId, ensureProjectTerminal } = await loadTerminalStore()

    const a = ensureProjectTerminal('p_a', '/repo/a')
    const b = ensureProjectTerminal('p_b', '/repo/b')

    expect(b).not.toBe(a)
    expect($activeTerminalId.get()).toBe(b)

    expect(ensureProjectTerminal('p_a', '/repo/a')).toBe(a)
    expect($activeTerminalId.get()).toBe(a)
  })

  it('resumes the last-active tab for a project even if the user opened extra tabs there', async () => {
    const { $activeTerminalId, createTerminal, ensureProjectTerminal, selectTerminal } = await loadTerminalStore()

    const first = ensureProjectTerminal('p_a', '/repo/a')
    const extra = createTerminal('/repo/a/sub')
    selectTerminal(extra)

    // A plain createTerminal tab has no projectId, so it never becomes the
    // remembered tab for p_a — switching away and back returns to `first`.
    ensureProjectTerminal('p_b', '/repo/b')
    expect(ensureProjectTerminal('p_a', '/repo/a')).toBe(first)
    expect($activeTerminalId.get()).toBe(first)
  })
})
