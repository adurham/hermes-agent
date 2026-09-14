import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { readDesktopBootPreference, readStartupDesktopPreference } from '../desktop-boot-preference'
import { resolveDesktopHermesHome } from '../data-paths'
import type { PayloadInfo } from '../payload-backend'
import { prepareRetirementStartup, type RetirementStartupOptions } from './retirement-host'
import { MacRetirementAdapter } from './retirement-macos'
import { retirementArgument } from './retirement-receiver'
import { RetirementJournal, retirementDigest, type RetirementRequest } from './retirement-state'

class TestDestination extends MacRetirementAdapter {
  override async verifyRunningDestination(request: RetirementRequest): Promise<string> {
    return path.join(request.destination.appPath, 'Contents/MacOS/Hermes')
  }
}

async function fixture(directory: string): Promise<{ options: RetirementStartupOptions; file: string; journal: RetirementJournal }> {
  const home: string = path.join(directory, 'preview-home')
  const userData: string = path.join(directory, 'stable-desktop')
  await Promise.all([mkdir(home), mkdir(userData)])
  await writeFile(path.join(home, 'witness'), 'untouched')
  const identity = { platform: 'darwin' as const, architecture: 'arm64' as const, signer: 'TESTTEAM12', nativeVersion: '1.0.0', applicationId: null }
  const request: RetirementRequest = { protocol: 1, id: 'a'.repeat(32), token: 'b'.repeat(64),
    source: { ...identity, identity: 'chat.nous.preview', appPath: path.join(directory, 'Preview.app'), executable: '/preview',
      packageFullName: null, home, userData: path.join(directory, 'preview-desktop'), profile: 'default', connectionId: 'local', removalRoots: [path.join(directory, 'Preview.app')] },
    destination: { ...identity, identity: 'chat.nous.hermes', appPath: path.join(directory, 'Hermes.app'), packageFamilyName: null, commit: 'c'.repeat(40),
      artifact: { url: 'https://example.com/stable.zip', sha256: 'd'.repeat(64), size: 1, format: 'zip' } },
    selection: { home, profile: 'default', connectionId: 'local', choice: 'open-preview', reauthenticate: false },
    qualification: { sha256: 'e'.repeat(64), sourceCommit: 'f'.repeat(40), destinationCommit: 'c'.repeat(40), receiverProtocol: 1 },
    consent: { install: true, removePreview: true, replaceExistingStable: false } }
  const journalRoot: string = path.join(directory, 'transactions')
  const journal = await RetirementJournal.open(journalRoot, request.id)
  await journal.write({ request, requestDigest: retirementDigest(request), stage: 'destination-installed',
    state: { snapshotHome: path.join(directory, 'snapshot'), selectedHome: home }, receiverApplied: false, receiverStarted: false, previousSelection: null, ready: null })
  const payload: PayloadInfo = { root: directory, shim: '/unused', repoDir: directory, toolsDir: directory, storePython: '/unused', sitePackages: directory, commands: {} }
  return { file: path.join(userData, 'active-profile.json'), journal, options: { argv: [retirementArgument(request.id, request.token)], userData,
    defaultHome: path.join(directory, 'default-home'), operatorOverride: false, payload, journalRoot,
    adapter: new TestDestination(), assertQualified: async (): Promise<void> => {},
    probeSnapshot: async (): Promise<void> => {} } }
}

test('receiver durably adopts before resolving home, is idempotent, and preserves a conflicting stable preference', async (): Promise<void> => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'retirement-host-'))
  try {
    const { file, journal, options } = await fixture(directory)
    expect(await prepareRetirementStartup(options)).not.toBeNull()
    const preference = readDesktopBootPreference(file)
    expect(resolveDesktopHermesHome({ home: directory, env: {}, adoptedHome: preference?.home })).toBe(path.join(directory, 'preview-home'))
    await prepareRetirementStartup(options)
    expect((await journal.read()).receiverApplied).toBe(true)
    expect(await readFile(path.join(directory, 'preview-home/witness'), 'utf8')).toBe('untouched')
    const changed: string = JSON.stringify({ profile: 'work', home: path.join(directory, 'other-home') })
    await writeFile(file, changed)
    await expect(prepareRetirementStartup(options)).rejects.toThrow('workspace changed')
    expect(await readFile(file, 'utf8')).toBe(changed)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('malformed legacy preferences keep ordinary boot usable but block migration without overwriting bytes', async (): Promise<void> => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'retirement-preference-'))
  try {
    const { file, journal, options } = await fixture(directory)
    await writeFile(file, '{bad-json')
    const warnings: string[] = []
    expect(readStartupDesktopPreference(file, (message: string): void => { warnings.push(message) })).toBeNull()
    expect(warnings).toHaveLength(1)
    await expect(prepareRetirementStartup(options)).rejects.toThrow()
    expect((await journal.read()).receiverStarted).toBe(false)
    expect(await readFile(file, 'utf8')).toBe('{bad-json')
  } finally { await rm(directory, { recursive: true, force: true }) }
})
