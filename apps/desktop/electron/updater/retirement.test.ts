import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { afterEach, expect, test } from 'vitest'

import { prepareRetirement, resumeRetirement, type RetirementDependencies } from './retirement'
import { MacRetirementAdapter } from './retirement-macos'
import {
  completeRetirementReception,
  receiveRetirement,
  type RetirementReceiverDependencies
} from './retirement-receiver'
import {
  RetirementJournal,
  type RetirementReady,
  type RetirementRecord,
  type RetirementRequest
} from './retirement-state'
import { WindowsRetirementAdapter } from './retirement-windows'

const temporary: string[] = []

afterEach(async (): Promise<void> => {
  for (const directory of temporary.splice(0)) {
    await rm(directory, { recursive: true, force: true })
  }
})

async function fixture(): Promise<{
  store: RetirementJournal
  request: RetirementRequest
  calls: string[]
  deps: RetirementDependencies
}> {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'hermes-retirement-'))
  temporary.push(directory)
  const home: string = path.join(directory, 'home')
  const userData: string = path.join(directory, 'preview-user-data')
  const sourceApp: string = path.join(directory, 'Preview.app')
  await Promise.all([mkdir(home), mkdir(userData), mkdir(sourceApp)])
  await writeFile(path.join(home, 'witness'), 'preserve me')
  const snapshot: string = path.join(directory, 'snapshot')
  await mkdir(snapshot)
  await writeFile(path.join(snapshot, 'witness'), 'preserve me')

  const request: RetirementRequest = {
    protocol: 1,
    id: 'a'.repeat(32),
    token: 'b'.repeat(64),
    source: {
      platform: 'darwin',
      appPath: sourceApp,
      executable: path.join(sourceApp, 'Contents/MacOS/Preview'),
      identity: 'chat.nous.preview',
      nativeVersion: '0.0.2',
      signer: 'ABCDE12345',
      architecture: 'arm64',
      packageFullName: null,
      applicationId: null,
      home,
      userData,
      profile: 'default',
      connectionId: 'local',
      removalRoots: [sourceApp]
    },
    destination: {
      platform: 'darwin',
      appPath: path.join(directory, 'Hermes.app'),
      identity: 'chat.nous.hermes',
      nativeVersion: '1.0.0',
      signer: 'ABCDE12345',
      architecture: 'arm64',
      applicationId: null,
      packageFamilyName: null,
      artifact: { url: 'https://example.com/stable.zip', sha256: 'c'.repeat(64), size: 123, format: 'zip' },
      commit: 'd'.repeat(40)
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

  const calls: string[] = []
  const store: RetirementJournal = await RetirementJournal.open(path.join(directory, 'transactions'), request.id)

  const deps: RetirementDependencies = {
    assertAdmitted: async (): Promise<void> => {
      calls.push('qualified')
    },
    prepareState: async (): Promise<{ snapshotHome: string; selectedHome: string }> => {
      calls.push('snapshot')

      return { snapshotHome: snapshot, selectedHome: home }
    },
    assertSourceQuiescent: async (): Promise<void> => {
      calls.push('quiescent')
    },
    assertDestinationReady: async (): Promise<void> => {
      calls.push('ready-now')
    },
    native: {
      install: async (): Promise<void> => {
        calls.push('install')
      },
      verifyDestination: async (): Promise<void> => {
        calls.push('verify')
      },
      activate: async (): Promise<void> => {
        calls.push('activate')
      },
      removePreview: async (): Promise<void> => {
        calls.push('remove')
      },
      isPreviewRemoved: async (): Promise<boolean> => calls.includes('remove')
    }
  }

  return { store, request, calls, deps }
}

function receiverDeps(request: RetirementRequest, calls: string[]): RetirementReceiverDependencies {
  return {
    verifyRunningDestination: async (): Promise<string> =>
      path.join(request.destination.appPath, 'Contents/MacOS/Hermes'),
    assertCompatibleSnapshot: async (): Promise<void> => {
      calls.push('compatible')
    },
    readSelection: async (): Promise<null> => null,
    adoptSelection: async (): Promise<void> => {
      calls.push('adopt')
    },
    assertReady: async (): Promise<void> => {
      calls.push('ready')
    }
  }
}

test('receiver checks compatibility before adopting state and cleanup failure resumes forward without duplicate adoption', async (): Promise<void> => {
  const { store, request, calls, deps } = await fixture()
  await prepareRetirement(store, request, deps)
  await resumeRetirement(store, deps, 'source')
  const receiver: RetirementReceiverDependencies = receiverDeps(request, calls)

  receiver.assertCompatibleSnapshot = async (): Promise<void> => {
    throw new Error('incompatible snapshot')
  }

  await expect(receiveRetirement(store, request.token, receiver)).rejects.toThrow('incompatible snapshot')
  expect(calls).not.toContain('adopt')
  expect((await store.read()).receiverStarted).toBe(false)

  receiver.assertCompatibleSnapshot = async (): Promise<void> => {
    calls.push('compatible')
  }

  await receiveRetirement(store, request.token, receiver)
  await receiveRetirement(store, request.token, receiver)
  expect(calls.filter((call: string): boolean => call === 'adopt')).toHaveLength(1)
  await completeRetirementReception(store, request.token, receiver)
  const originalRemove: RetirementDependencies['native']['removePreview'] = deps.native.removePreview

  deps.native.removePreview = async (): Promise<void> => {
    throw new Error('native package busy')
  }

  expect((await resumeRetirement(store, deps, 'destination')).status).toBe('cleanup-pending')
  expect((await store.read()).stage).toBe('destination-ready')
  deps.native.removePreview = originalRemove
  expect((await resumeRetirement(store, deps, 'destination')).status).toBe('complete')
  expect((await resumeRetirement(store, deps, 'destination')).status).toBe('complete')
  expect(calls.filter((call: string): boolean => call === 'remove')).toHaveLength(1)
  expect(await readFile(path.join(request.source.home, 'witness'), 'utf8')).toBe('preserve me')
})

test('configured stable conflicts, operator overrides and stale correlation fail without adoption', async (): Promise<void> => {
  const { store, request, calls, deps } = await fixture()
  await prepareRetirement(store, request, deps)
  await resumeRetirement(store, deps, 'source')
  const receiver: RetirementReceiverDependencies = receiverDeps(request, calls)
  receiver.readSelection = async (): Promise<{
    home: string
    profile: string
    connectionId: string
    operatorOverride: boolean
  }> => ({ home: '/different', profile: 'work', connectionId: 'local', operatorOverride: true })
  await expect(receiveRetirement(store, request.token, receiver)).rejects.toThrow('conflict')
  await expect(receiveRetirement(store, '0'.repeat(64), receiver)).rejects.toThrow('correlation')
  expect(calls).not.toContain('adopt')
  expect(calls).not.toContain('remove')
})

test('remote selection requires explicit reauthentication rather than copying encrypted credentials', async (): Promise<void> => {
  const { store, request, calls, deps } = await fixture()
  request.selection.connectionId = 'remote-account'
  await prepareRetirement(store, request, deps)
  await resumeRetirement(store, deps, 'source')
  await expect(receiveRetirement(store, request.token, receiverDeps(request, calls))).rejects.toThrow(
    'Reauthentication'
  )
  expect(calls).not.toContain('adopt')
})

test('stale receipts and a destination that stopped being ready never authorize removal', async (): Promise<void> => {
  const { store, request, calls, deps } = await fixture()
  await prepareRetirement(store, request, deps)
  await resumeRetirement(store, deps, 'source')
  const receiver: RetirementReceiverDependencies = receiverDeps(request, calls)
  await receiveRetirement(store, request.token, receiver)
  const ready: RetirementReady = await completeRetirementReception(store, request.token, receiver)
  const record: RetirementRecord = await store.read()
  await store.write({ ...record, ready: { ...ready, requestDigest: '0'.repeat(64) } })
  await expect(resumeRetirement(store, deps, 'destination')).rejects.toThrow('receipt')
  await store.write(record)

  deps.assertDestinationReady = async (): Promise<void> => {
    throw new Error('target backend disconnected')
  }

  expect((await resumeRetirement(store, deps, 'destination')).status).toBe('cleanup-pending')
  expect(calls).not.toContain('remove')
})

test('cancel, backup failure and removal-scoped state do not reach native installation', async (): Promise<void> => {
  const { store, request, calls, deps } = await fixture()
  await expect(
    prepareRetirement(store, { ...request, consent: { ...request.consent, install: false } }, deps)
  ).rejects.toThrow('consent')

  deps.prepareState = async (): Promise<never> => {
    throw new Error('backup unavailable')
  }

  await expect(prepareRetirement(store, request, deps)).rejects.toThrow('backup unavailable')
  request.source.removalRoots.push(request.source.home)
  deps.prepareState = async (): Promise<{ snapshotHome: string; selectedHome: string }> => ({
    snapshotHome: path.join(path.dirname(request.source.home), 'snapshot'),
    selectedHome: request.source.home
  })
  await expect(prepareRetirement(store, request, deps)).rejects.toThrow('removal footprint')
  expect(calls).not.toContain('install')
  expect(calls).not.toContain('remove')
  expect(await readFile(path.join(request.source.home, 'witness'), 'utf8')).toBe('preserve me')
})

test.each(['preview-removed', 'complete'] as const)(
  'activation can enter the receiver and interruption before %s does not remove twice',
  async (failedStage): Promise<void> => {
    const { store, request, calls, deps } = await fixture()
    await prepareRetirement(store, request, deps)
    const receiver: RetirementReceiverDependencies = receiverDeps(request, calls)

    deps.native.activate = async (): Promise<void> => {
      await receiveRetirement(store, request.token, receiver)
    }

    await resumeRetirement(store, deps, 'source')
    await completeRetirementReception(store, request.token, receiver)
    const write: RetirementJournal['write'] = store.write.bind(store)

    store.write = async (record: RetirementRecord): Promise<void> => {
      if (record.stage === failedStage) {
        throw new Error('power loss before journal rename')
      }

      await write(record)
    }

    expect((await resumeRetirement(store, deps, 'destination')).status).toBe('cleanup-pending')
    expect((await store.read()).stage).toBe(failedStage === 'preview-removed' ? 'destination-ready' : 'preview-removed')
    store.write = write
    const reopened: RetirementJournal = await RetirementJournal.open(store.root, request.id)
    await receiveRetirement(reopened, request.token, receiver)
    expect((await resumeRetirement(reopened, deps, 'destination')).status).toBe('complete')
    expect(calls.filter((call: string): boolean => call === 'remove')).toHaveLength(1)
  }
)

test('an installed target without a correlated ready receipt never authorizes removal, including restart', async (): Promise<void> => {
  const { store, request, calls, deps } = await fixture()
  await prepareRetirement(store, request, deps)
  expect((await resumeRetirement(store, deps, 'source')).status).toBe('awaiting-destination')
  const reopened: RetirementJournal = await RetirementJournal.open(store.root, request.id)
  expect((await resumeRetirement(reopened, deps, 'source')).status).toBe('awaiting-destination')
  expect(calls.filter((call: string): boolean => call === 'install')).toHaveLength(1)
  expect(calls).not.toContain('remove')
  expect((await reopened.read()).stage).toBe('destination-installed')
  await expect(new MacRetirementAdapter().removePreview(request, reopened)).rejects.toThrow('destination-ready')
  await expect(new WindowsRetirementAdapter().removePreview(request, reopened)).rejects.toThrow('destination-ready')
  expect(await readFile(path.join(request.source.home, 'witness'), 'utf8')).toBe('preserve me')
})
