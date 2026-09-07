// updater/updater.test.ts — resolution precedence + wire-shape contracts
// for the strategy layer. Pure DI: no Electron, no payload.

import { describe, expect, it } from 'vitest'

import { appInstallerCheckToStatus, parseCheckOutput } from './app-installer'
import { buildManualUpdateCommand } from './checkout'
import { consumePendingRelaunch, PENDING_RELAUNCH_FILENAME, registerUpdateRelaunch, writePendingRelaunch } from './relaunch'

import { resolveUpdaterMechanism } from './index'

describe('resolveUpdaterMechanism — install ownership', () => {
  it.each(['bundled', 'light'] as const)('macOS %s updates without probing for Python', payload => {
    expect(resolveUpdaterMechanism({ isPackaged: true, payload, platform: 'darwin', updateMechanism: 'electron-updater', isWindowsStore: false })).toBe('electron-updater')
  })

  it.each([
    ['win32', false, 'app-installer'],
    ['win32', true, 'external'],
    ['linux', false, 'external'],
    ['darwin', false, 'external']
  ] as const)('preserves %s steward ownership (store=%s)', (platform, isWindowsStore, expected) => {
    expect(resolveUpdaterMechanism({ isPackaged: true, payload: 'bundled', platform, isWindowsStore, updateMechanism: 'external' })).toBe(expected)
  })

  it.each(['win32', 'darwin', 'linux'] as const)('dev and bootstrap %s retain checkout updates', platform => {
    const facts = { isPackaged: true, platform, payload: 'bootstrap' as const, updateMechanism: 'self' as const, isWindowsStore: false }
    const expected = platform === 'win32' ? 'windows-handoff' : 'posix-handoff'
    expect(resolveUpdaterMechanism(facts)).toBe(expected)
    expect(resolveUpdaterMechanism({ ...facts, isPackaged: false, payload: 'bundled' })).toBe(expected)
  })
})

describe('app-installer check → status wire', () => {
  it('available true → updateAvailable, no error', () => {
    const s = appInstallerCheckToStatus({ available: true }, '0.18.2')
    expect(s.supported).toBe(true)
    expect(s.mechanism).toBe('app-installer')
    expect(s.updateAvailable).toBe(true)
    expect(s.error).toBeUndefined()
  })

  it('available false → updateAvailable false, NO error (honest no-update)', () => {
    const s = appInstallerCheckToStatus({ available: false }, '0.18.2')
    expect(s.updateAvailable).toBe(false)
    expect(s.error).toBeUndefined()
  })

  it('available null → updateAvailable false WITH error (honest unknown, never "no update")', () => {
    const s = appInstallerCheckToStatus({ available: null, error: 'no winrt' }, '0.18.2')
    expect(s.updateAvailable).toBe(false)
    expect(s.error).toBe('no winrt')
  })
})

describe('parseCheckOutput', () => {
  it('parses boolean true/false availability', () => {
    expect(parseCheckOutput(0, '{"available": true, "availability": "Available"}')).toEqual({
      available: true,
      availability: 'Available',
      error: undefined
    })
    expect(parseCheckOutput(0, '{"available": false}')).toEqual({ available: false, availability: undefined, error: undefined })
  })

  it('available null surfaces the checker error', () => {
    expect(parseCheckOutput(2, '{"available": null, "error": "winrt import failed"}')).toEqual({
      available: null,
      error: 'winrt import failed'
    })
  })

  it('non-zero exit without parseable output names the exit code', () => {
    expect(parseCheckOutput(1, 'boom')).toEqual({ available: null, error: 'checker exited 1' })
  })

  it('exit 0 with no availability is unknown, not no-update', () => {
    expect(parseCheckOutput(0, '')).toEqual({ available: null, error: 'checker returned no availability' })
  })
})

describe('buildManualUpdateCommand', () => {
  it('bare command on main and detached HEAD', () => {
    expect(buildManualUpdateCommand('main')).toBe('hermes update')
    expect(buildManualUpdateCommand('HEAD')).toBe('hermes update')
    expect(buildManualUpdateCommand(null)).toBe('hermes update')
  })

  it('branch-pinned for non-main checkouts', () => {
    expect(buildManualUpdateCommand('ethie/pm')).toBe('hermes update --branch ethie/pm')
  })
})

describe('pending relaunch marker', () => {
  const KEY = PENDING_RELAUNCH_FILENAME

  function fakeFs(files: Record<string, string>) {
    const has = (f: string) => f.replace(/\\/g, '/').endsWith(`/${KEY}`) || f === KEY

    return {
      existsSync: (f: string) => has(f),
      readFileSync: (f: string) => files[Object.keys(files).find(k => k.replace(/\\/g, '/') === f.replace(/\\/g, '/')) ?? f],
      unlinkSync: (f: string) => {
        const key = Object.keys(files).find(k => k.replace(/\\/g, '/') === f.replace(/\\/g, '/'))

        if (key) {delete files[key]}
      }
    }
  }

  it('write then consume on a different version = update relaunch', () => {
    const files: Record<string, string> = {}
    expect(writePendingRelaunch('/home', '0.18.2', (f, c) => { files[f] = c as string })).toBe(true)
    expect(JSON.parse(Object.values(files)[0]).fromVersion).toBe('0.18.2')

    const r = consumePendingRelaunch('/home', '0.18.3', fakeFs(files))
    expect(r.wasUpdateRelaunch).toBe(true)
    expect(r.fromVersion).toBe('0.18.2')
    // one-shot: consumed
    expect(Object.keys(files)).toHaveLength(0)
  })

  it('same version = update never landed; marker consumed silently', () => {
    const files: Record<string, string> = {}
    writePendingRelaunch('/home', '0.18.2', (f, c) => { files[f] = c as string })

    const r = consumePendingRelaunch('/home', '0.18.2', fakeFs(files))
    expect(r.wasUpdateRelaunch).toBe(false)
    expect(Object.keys(files)).toHaveLength(0)
  })

  it('no marker = normal launch', () => {
    expect(consumePendingRelaunch('/home', '0.18.3', fakeFs({})).wasUpdateRelaunch).toBe(false)
  })

  it('corrupt marker is consumed and treated as unknown', () => {
    const files: Record<string, string> = {}
    writePendingRelaunch('/home', '0.18.2', (f, c) => { files[f] = c as string })

    // corrupt the contents under the same key
    for (const k of Object.keys(files)) {files[k] = 'not json'}

    const r = consumePendingRelaunch('/home', '0.18.3', fakeFs(files))
    expect(r.wasUpdateRelaunch).toBe(false)
    expect(Object.keys(files)).toHaveLength(0)
  })
})

describe('registerUpdateRelaunch — the mechanism, not just the marker', () => {
  it('writes the marker AND starts the relaunch mechanism', async () => {
    const files: Record<string, string> = {}
    let started = 0

    const ok = await registerUpdateRelaunch(
      '/home',
      '0.18.2',
      { relaunch: () => { started += 1 } },
      (f, c) => { files[f] = c as string }
    )

    expect(ok).toBe(true)
    expect(started).toBe(1)
    expect(Object.keys(files)).toHaveLength(1)
  })

  it('awaits the mechanism outcome: a waiter that reports false leaves the marker as the only trace', async () => {
    const files: Record<string, string> = {}

    const ok = await registerUpdateRelaunch(
      '/home',
      '0.18.2',
      { relaunch: async () => false },
      (f, c) => { files[f] = c as string }
    )

    expect(ok).toBe(false)
    expect(Object.keys(files)).toHaveLength(1)
  })

  it('a mechanism throw never blocks the update (marker-only manual relaunch)', async () => {
    const files: Record<string, string> = {}

    const ok = await registerUpdateRelaunch(
      '/home',
      '0.18.2',
      { relaunch: () => { throw new Error('spawn failed') } },
      (f, c) => { files[f] = c as string }
    )

    expect(ok).toBe(false)
    expect(Object.keys(files)).toHaveLength(1)
  })
})
