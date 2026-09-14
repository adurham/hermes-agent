import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { resolveDesktopHermesHome } from '../data-paths'
import { readDesktopBootPreference, readStartupDesktopPreference } from '../desktop-boot-preference'
import type { PayloadInfo } from '../payload-backend'

import { prepareRetirementStartup, type RetirementStartupOptions } from './retirement-host'
import { adoptRetirementConnection, retirementConnection, retirementSelectedConnection } from './retirement-connections'
import { MacRetirementAdapter } from './retirement-macos'
import type { RetirementWorkspaceConflict } from './retirement-receiver'
import { retirementArgument } from './retirement-receiver'
import { retirementDigest, RetirementJournal, type RetirementRequest } from './retirement-state'

class TestDestination extends MacRetirementAdapter {
  removed: boolean = false
  override async verifyDestination(): Promise<void> {}
  override async removePreview(): Promise<void> { this.removed = true }
  override async isPreviewRemoved(): Promise<boolean> { return this.removed }
  override async verifyRunningDestination(request: RetirementRequest): Promise<string> {
    return path.join(request.destination.appPath, 'Contents/MacOS/Hermes')
  }
}

async function fixture(
  directory: string
): Promise<{ options: RetirementStartupOptions; file: string; journal: RetirementJournal }> {
  const home: string = path.join(directory, 'preview-home')
  const userData: string = path.join(directory, 'stable-desktop')
  await Promise.all([mkdir(home), mkdir(userData)])
  await writeFile(path.join(home, 'witness'), 'untouched')

  const identity = {
    platform: 'darwin' as const,
    architecture: 'arm64' as const,
    signer: 'TESTTEAM12',
    nativeVersion: '1.0.0',
    applicationId: null
  }

  const request: RetirementRequest = {
    protocol: 1,
    id: 'a'.repeat(32),
    token: 'b'.repeat(64),
    source: {
      ...identity,
      identity: 'chat.nous.preview',
      appPath: path.join(directory, 'Preview.app'),
      executable: '/preview',
      packageFullName: null,
      home,
      userData: path.join(directory, 'preview-desktop'),
      profile: 'default',
      connectionId: 'local',
      removalRoots: [path.join(directory, 'Preview.app')]
    },
    destination: {
      ...identity,
      identity: 'chat.nous.hermes',
      appPath: path.join(directory, 'Hermes.app'),
      packageFamilyName: null,
      commit: 'c'.repeat(40),
      artifact: { url: 'https://example.com/stable.zip', sha256: 'd'.repeat(64), size: 1, format: 'zip' }
    },
    selection: { home, profile: 'default', connectionId: 'local', choice: 'open-preview', reauthenticate: false },
    destinationManifestSha256: 'e'.repeat(64),
    sourceBuild: {
      buildId: '1'.repeat(32), channel: 'preview', sequence: 1, repository: 'NousResearch/hermes-agent',
      commit: 'f'.repeat(40), version: '0.0.1', sourceVersion: '1.2.2', windowsVersion: '0.0.1.0',
      publicBase: 'https://example.com', bundleEnv: {},
      identity: { token: '2'.repeat(16), displayName: 'Preview', appId: 'chat.nous.preview',
        appNamePascal: 'Preview', artifactNamePascal: 'Preview', cliName: 'preview',
        windowsExecutableName: 'preview', msixAppIdWithOrg: 'NousResearch.Preview' }
    },
    consent: { install: true, removePreview: true, replaceExistingStable: false }
  }

  const journalRoot: string = path.join(directory, 'transactions')
  const journal = await RetirementJournal.open(journalRoot, request.id)
  await journal.write({
    request,
    requestDigest: retirementDigest(request),
    stage: 'destination-installed',
    state: { snapshotHome: path.join(directory, 'snapshot'), selectedHome: home },
    receiverApplied: false,
    receiverStarted: false,
    previousSelection: null,
    ready: null
  })

  const payload: PayloadInfo = {
    root: directory,
    shim: '/unused',
    repoDir: directory,
    toolsDir: directory,
    storePython: '/unused',
    sitePackages: directory,
    commands: {}
  }

  return {
    file: path.join(userData, 'active-profile.json'),
    journal,
    options: {
      argv: [retirementArgument(request.id, request.token)],
      userData,
      defaultHome: path.join(directory, 'default-home'),
      operatorOverride: false,
      payload,
      journalRoot,
      adapter: new TestDestination(),
      assertAdmitted: async (): Promise<void> => {},
      probeSnapshot: async (): Promise<void> => {}
    }
  }
}

test('receiver durably adopts before resolving home, is idempotent, and preserves a conflicting stable preference', async (): Promise<void> => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'retirement-host-'))

  try {
    const { file, journal, options } = await fixture(directory)
    expect(await prepareRetirementStartup(options)).not.toBeNull()
    const preference = readDesktopBootPreference(file)
    expect(resolveDesktopHermesHome({ home: directory, env: {}, adoptedHome: preference?.home })).toBe(
      path.join(directory, 'preview-home')
    )
    await prepareRetirementStartup(options)
    expect((await journal.read()).receiverApplied).toBe(true)
    expect(await readFile(path.join(directory, 'preview-home/witness'), 'utf8')).toBe('untouched')
    const changed: string = JSON.stringify({ profile: 'work', home: path.join(directory, 'other-home') })
    await writeFile(file, changed)
    await expect(prepareRetirementStartup(options)).rejects.toThrow('workspace changed')
    expect(await readFile(file, 'utf8')).toBe(changed)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})

test.each(['keep-stable', 'open-preview'] as const)(
  'explicit %s consent binds the current stable workspace without merging settings',
  async (choice): Promise<void> => {
    const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-consent-'))

    try {
      const { file, journal, options } = await fixture(directory)
      const stableHome: string = path.join(directory, 'stable-home')
      await mkdir(stableHome)
      const original: string = JSON.stringify({ home: stableHome, profile: 'work', customSetting: 'preserve' })
      await writeFile(file, original)
      const connectionFile: string = path.join(options.userData, 'connections.json')
      await writeFile(connectionFile, '{"primary":"local","saved":[{"id":"remote-saved"}]}')
      const prompts: RetirementWorkspaceConflict[] = []

      options.confirmWorkspaceConflict = async (conflict: RetirementWorkspaceConflict): Promise<typeof choice> => {
        prompts.push(conflict)

        return choice
      }

      if (choice === 'keep-stable') {
        options.runningSelection = () => ({ home: stableHome, profile: 'work', connectionId: 'local' })
      }

      const reception = await prepareRetirementStartup(options)
      expect(reception).not.toBeNull()
      expect(prompts).toHaveLength(1)
      expect(prompts[0].current).toEqual({
        home: stableHome,
        profile: 'work',
        connectionId: 'local',
        operatorOverride: false
      })
      expect(prompts[0].canOpenPreview).toBe(choice === 'open-preview')
      const record = await journal.read()
      expect(record.request.selection.choice).toBe(choice)
      expect(record.previousSelection?.home).toBe(stableHome)
      expect(record.request.selection.conflictConsent).toEqual(prompts[0].current)
      expect(record.state.selectedHome).toBe(choice === 'keep-stable' ? stableHome : record.request.source.home)

      if (choice === 'keep-stable') {
        expect(await readFile(file, 'utf8')).toBe(original)
      } else {
        expect(JSON.parse(await readFile(file, 'utf8')).customSetting).toBe('preserve')
      }

      expect(await readFile(connectionFile, 'utf8')).toBe('{"primary":"local","saved":[{"id":"remote-saved"}]}')
      await prepareRetirementStartup(options)
      expect(prompts).toHaveLength(1)
      expect(record.ready).toBeNull()
      expect(record.stage).toBe('destination-installed')
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  }
)

test.each(['before-write', 'after-write'] as const)(
  'receiver resumes %s interruption without asking again or losing stable settings',
  async (interruption): Promise<void> => {
    const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-adoption-retry-'))

    try {
      const { file, journal, options } = await fixture(directory)
      const stableHome: string = path.join(directory, 'stable-home')
      await mkdir(stableHome)
      await writeFile(file, JSON.stringify({ home: stableHome, profile: 'work', customSetting: 'preserved' }))
      const record = await journal.read()
      const previous = { home: stableHome, profile: 'work', connectionId: 'local', operatorOverride: false }
      record.request.selection.conflictConsent = previous
      record.previousSelection = previous
      record.receiverStarted = true
      record.requestDigest = retirementDigest(record.request)
      await journal.write(record)

      if (interruption === 'after-write') {
        await writeFile(
          file,
          JSON.stringify({ home: record.request.selection.home, profile: 'default', customSetting: 'preserved' })
        )
      }

      options.confirmWorkspaceConflict = async (): Promise<never> => {
        throw new Error('Must reuse prior consent')
      }

      expect(await prepareRetirementStartup(options)).not.toBeNull()
      expect((await journal.read()).receiverApplied).toBe(true)
      expect((await journal.read()).previousSelection).toEqual(previous)
      expect(JSON.parse(await readFile(file, 'utf8')).customSetting).toBe('preserved')
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  }
)

test('cancel, stale consent and running stable reject retargeting before any preference write', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-consent-stale-'))

  try {
    const { file, journal, options } = await fixture(directory)
    const stableHome: string = path.join(directory, 'stable-home')
    await mkdir(stableHome)
    const original: string = JSON.stringify({ home: stableHome, profile: 'work' })
    await writeFile(file, original)
    options.confirmWorkspaceConflict = async (): Promise<null> => null
    await expect(prepareRetirementStartup(options)).rejects.toThrow('cancelled')

    options.confirmWorkspaceConflict = async (): Promise<'open-preview'> => {
      await writeFile(file, JSON.stringify({ home: stableHome, profile: 'changed' }))

      return 'open-preview'
    }

    await expect(prepareRetirementStartup(options)).rejects.toThrow('changed')
    await writeFile(file, original)
    options.runningSelection = () => ({ home: stableHome, profile: 'work', connectionId: 'local' })
    options.confirmWorkspaceConflict = async (): Promise<'open-preview'> => 'open-preview'
    await expect(prepareRetirementStartup(options)).rejects.toThrow('Close stable')
    expect(await readFile(file, 'utf8')).toBe(original)
    let activeProfile: string = 'work'
    options.runningSelection = () => ({ home: stableHome, profile: activeProfile, connectionId: 'local' })

    options.confirmWorkspaceConflict = async (): Promise<'keep-stable'> => {
      activeProfile = 'changed'

      return 'keep-stable'
    }

    await expect(prepareRetirementStartup(options)).rejects.toThrow('changed')
    expect((await journal.read()).receiverStarted).toBe(false)
    expect((await journal.read()).ready).toBeNull()
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})

test('remote adoption strips credentials, survives an interrupted write and waits for authenticated readiness', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-remote-'))
  try {
    const { file, journal, options } = await fixture(directory)
    const record = await journal.read()
    record.request.selection.connectionId = 'retired-remote'
    record.request.selection.reauthenticate = true
    record.request.selection.connection = retirementConnection({ id: 'remote', kind: 'remote', label: 'Research',
      url: 'https://gateway.example', authMode: 'token', token: { ciphertext: 'private-preview-secret' }, headers: { Authorization: 'private-proxy-secret' } })
    record.previousSelection = { home: options.defaultHome, profile: 'default', connectionId: 'local', operatorOverride: false }
    record.receiverStarted = true
    record.requestDigest = retirementDigest(record.request)
    await journal.write(record)
    adoptRetirementConnection(options.userData, 'retired-remote', 'default', record.request.selection.connection)
    const reception = await prepareRetirementStartup(options)
    if (!reception || !(options.adapter instanceof TestDestination)) { throw new Error('Receiver did not start') }
    expect(readDesktopBootPreference(file)?.home).toBe(record.request.selection.home)
    expect(retirementSelectedConnection(options.userData, 'default')).toBe('retired-remote')
    const stored: string = await readFile(path.join(options.userData, 'connections.json'), 'utf8')
    expect(stored).not.toContain('private-preview-secret')
    expect(stored).not.toContain('private-proxy-secret')
    const adapter: TestDestination = options.adapter
    let attempts: number = 0
    const outcome = await reception.complete({ home: record.request.selection.home, profile: (): string => 'default',
      connectionId: (): string => retirementSelectedConnection(options.userData, 'default'), rendererMounted: (): boolean => true,
      backend: async (): Promise<void> => {
        expect(adapter.removed).toBe(false)
        if (attempts++ === 0) { throw new Error('reauthentication required') }
      } })
    expect(outcome.status).toBe('complete')
    expect((await journal.read()).stage).toBe('complete')
    expect(adapter.removed).toBe(true)
    expect(retirementSelectedConnection(options.userData, 'default')).toBe('retired-remote')
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('a removed package home stays adopted on cold boot', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-relocated-'))
  try {
    const { file, journal, options } = await fixture(directory)
    const record = await journal.read()
    const relocated: string = path.join(journal.directory, 'home')
    await mkdir(relocated)
    await writeFile(path.join(relocated, 'witness'), 'preserved')
    record.request.source.removalRoots.push(record.request.source.home)
    record.request.selection.home = relocated
    record.state.selectedHome = relocated
    record.requestDigest = retirementDigest(record.request)
    await journal.write(record)
    await prepareRetirementStartup(options)
    await rm(record.request.source.home, { recursive: true })
    expect(resolveDesktopHermesHome({ home: directory, env: {}, adoptedHome: readDesktopBootPreference(file)?.home })).toBe(relocated)
    expect(await readFile(path.join(relocated, 'witness'), 'utf8')).toBe('preserved')
    await prepareRetirementStartup(options)
    expect((await journal.read()).receiverApplied).toBe(true)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('malformed legacy preferences keep ordinary boot usable but block migration without overwriting bytes', async (): Promise<void> => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'retirement-preference-'))

  try {
    const { file, journal, options } = await fixture(directory)
    await writeFile(file, '{bad-json')
    const warnings: string[] = []
    expect(
      readStartupDesktopPreference(file, (message: string): void => {
        warnings.push(message)
      })
    ).toBeNull()
    expect(warnings).toHaveLength(1)
    await expect(prepareRetirementStartup(options)).rejects.toThrow()
    expect((await journal.read()).receiverStarted).toBe(false)
    expect(await readFile(file, 'utf8')).toBe('{bad-json')
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})
