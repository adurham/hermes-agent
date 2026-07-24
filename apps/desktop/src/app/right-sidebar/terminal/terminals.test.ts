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

  // Regression for the "3 tabs instead of 2" report: the pane's very first
  // tab (opened blind, before any project had ever been entered) has no
  // projectId. Re-entering the project it happens to sit in must adopt that
  // tab, not spawn a duplicate next to it.
  it('adopts a pre-existing unbound tab whose cwd already sits under the project root', async () => {
    const { $terminals, createTerminal, ensureProjectTerminal } = await loadTerminalStore()

    const blind = createTerminal('/repo/exo')
    const hermes = ensureProjectTerminal('p_hermes', '/repo/hermes-agent')
    const exo = ensureProjectTerminal('p_exo', '/repo/exo')

    expect(exo).toBe(blind)
    expect($terminals.get()).toHaveLength(2)
    expect($terminals.get().find(t => t.id === blind)?.projectId).toBe('p_exo')
    expect(hermes).not.toBe(blind)
  })

  it('stays at two tabs across repeated switches once a blind tab has been adopted', async () => {
    const { $activeTerminalId, $terminals, ensureProjectTerminal } = await loadTerminalStore()

    ensureProjectTerminal('p_exo', '/repo/exo') // adopts the pane's initial blind tab in the real flow
    ensureProjectTerminal('p_hermes', '/repo/hermes-agent')
    ensureProjectTerminal('p_exo', '/repo/exo')
    ensureProjectTerminal('p_hermes', '/repo/hermes-agent')
    const finalId = ensureProjectTerminal('p_exo', '/repo/exo')

    expect($terminals.get()).toHaveLength(2)
    expect($activeTerminalId.get()).toBe(finalId)
  })

  it('adopts a tab whose live shell cd-d into the project root (restoreCwd), not just its launch cwd', async () => {
    const { $terminals, createTerminal, ensureProjectTerminal, updateTerminalRestoreCwd } = await loadTerminalStore()

    const blind = createTerminal('/detached/start')
    updateTerminalRestoreCwd(blind, '/repo/exo/packages/api')

    const exo = ensureProjectTerminal('p_exo', '/repo/exo')

    expect(exo).toBe(blind)
    expect($terminals.get()).toHaveLength(1)
  })

  it('does not adopt an unbound tab outside the project root', async () => {
    const { $terminals, createTerminal, ensureProjectTerminal } = await loadTerminalStore()

    const unrelated = createTerminal('/repo/unrelated')
    const exo = ensureProjectTerminal('p_exo', '/repo/exo')

    expect(exo).not.toBe(unrelated)
    expect($terminals.get()).toHaveLength(2)
  })
})

describe('ensureTerminal', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('creates a plain unbound tab when no project is passed', async () => {
    const { $terminals, ensureTerminal } = await loadTerminalStore()

    ensureTerminal()

    expect($terminals.get()).toHaveLength(1)
    expect($terminals.get()[0]?.projectId).toBeUndefined()
  })

  it('creates the first-ever tab already bound to the active project, not a blind tab needing later adoption', async () => {
    const { $terminals, ensureTerminal } = await loadTerminalStore()

    ensureTerminal({ id: 'p_exo', cwd: '/repo/exo' })

    expect($terminals.get()).toHaveLength(1)
    expect($terminals.get()[0]).toMatchObject({ cwd: '/repo/exo', projectId: 'p_exo' })
  })

  it('is a no-op once a tab already exists, even with a project passed', async () => {
    const { $terminals, ensureTerminal } = await loadTerminalStore()

    ensureTerminal()
    ensureTerminal({ id: 'p_exo', cwd: '/repo/exo' })

    expect($terminals.get()).toHaveLength(1)
  })
})
