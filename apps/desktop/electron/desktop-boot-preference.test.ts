import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { resolveDesktopHermesHome } from './data-paths'
import { adoptDesktopHome, readDesktopBootPreference, writeDesktopProfile } from './desktop-boot-preference'

test('an adopted home survives profile changes and cold boot without overriding explicit launch state', (): void => {
  const directory: string = mkdtempSync(path.join(os.tmpdir(), 'hermes-boot-preference-'))
  const preference: string = path.join(directory, 'active-profile.json')
  const home: string = path.join(directory, 'preview-state')

  try {
    adoptDesktopHome(preference, { home, profile: 'work' }, null)
    writeDesktopProfile(preference, 'research')
    const stored = readDesktopBootPreference(preference)

    assert.deepEqual(stored, { home, profile: 'research' })
    assert.equal(resolveDesktopHermesHome({ home: directory, env: {}, adoptedHome: stored?.home }), home)
    assert.equal(resolveDesktopHermesHome({ home: directory, env: { HERMES_HOME: directory }, adoptedHome: home }), directory)
    assert.equal(resolveDesktopHermesHome({ home: directory, env: { HERMES_DESKTOP_USER_DATA_DIR: directory }, adoptedHome: home }), path.join(directory, 'hermes-home'))
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})

test('adoption refuses a concurrent preference change and preserves unreadable user settings', (): void => {
  const directory: string = mkdtempSync(path.join(os.tmpdir(), 'hermes-boot-conflict-'))
  const preference: string = path.join(directory, 'active-profile.json')

  try {
    writeDesktopProfile(preference, 'original')
    const original = readDesktopBootPreference(preference)
    writeDesktopProfile(preference, 'changed')
    assert.throws(() => adoptDesktopHome(preference, { home: directory, profile: 'preview' }, original), /changed/)
    assert.equal(readDesktopBootPreference(preference)?.profile, 'changed')
    writeFileSync(preference, '{broken', 'utf8')
    assert.throws(() => writeDesktopProfile(preference, 'preview'), /preference/)
    assert.equal(readFileSync(preference, 'utf8'), '{broken')
  } finally {
    rmSync(directory, { recursive: true, force: true })
  }
})
