import { readdir } from 'node:fs/promises'
import path from 'node:path'

import { adoptDesktopHome, type DesktopBootPreference, readDesktopBootPreference } from '../desktop-boot-preference'
import type { PayloadInfo } from '../payload-backend'

import { ChannelResolver, type ChannelRetirement } from './channel'
import type { ChannelRetirementCallbacks, ChannelRetirementStatus } from './channel-strategy'
import {
  awaitRetirementCleanup, prepareRetirement, resumeRetirement, type RetirementDependencies,
  type RetirementOutcome, type RetirementReadinessProbe, verifyRetirementReadiness
} from './retirement'
import { probeRetirementSnapshot, retirementSnapshotTools } from './retirement-compatibility'
import { adoptRetirementConnection, assertRetirementConnection, prepareRetirementConnection, type RetirementConnectionSelection, retirementSelectedConnection } from './retirement-connections'
import {
  discoverRetirementDestination,
  discoverRetirementSource
} from './retirement-discovery'
import { MacRetirementAdapter } from './retirement-macos'
import { retirementPathExists } from './retirement-native'
import { snapshotRetirementHome } from './retirement-preservation'
import {
  completeRetirementReception,
  parseRetirementArgument,
  receiveRetirement,
  type RetirementExistingSelection,
  type RetirementReceiverDependencies
} from './retirement-receiver'
import {
  canonicalRetirementPath,
  defaultRetirementRoot,
  insideRetirementRoot,
  newRetirementCorrelation,
  type RetirementConsent,
  RetirementJournal,
  type RetirementRecord,
  type RetirementRequest,
  type RetirementSelection,
  type RetirementWorkspaceChoice
} from './retirement-state'
import { WindowsRetirementAdapter } from './retirement-windows'

import type { UpdaterApplyResultWire } from './index'

export type RetirementAdapter = MacRetirementAdapter | WindowsRetirementAdapter

export function nativeRetirementAdapter(): RetirementAdapter {
  if (process.platform === 'darwin') {
    return new MacRetirementAdapter()
  }

  if (process.platform === 'win32') {
    return new WindowsRetirementAdapter()
  }

  throw new Error('Native retirement requires macOS or Windows')
}

export async function assertRetirementAdmitted(request: RetirementRequest): Promise<void> {
  const build = request.sourceBuild
  const resolved = await new ChannelResolver({
    build,
    platform: request.source.platform,
    arch: request.source.architecture,
    signer: request.source.signer
  }).resolve()

  if (
    resolved.kind !== 'retirement' ||
    resolved.retirement.target.manifest.request.commit !== request.destination.commit ||
    resolved.retirement.target.package.artifact.sha256 !== request.destination.artifact.sha256 ||
    resolved.retirement.target.package.identity !== request.destination.identity ||
    resolved.retirement.target.manifestSha256 !== request.destinationManifestSha256
  ) {
    throw new Error('The pinned retirement destination is no longer available')
  }
}

export interface RetirementStartupOptions {
  argv: readonly string[]
  userData: string
  defaultHome: string
  operatorOverride: boolean
  payload: PayloadInfo | null
  /** A live stable process cannot silently retarget already-open databases. */
  runningSelection?: () => Pick<RetirementExistingSelection, 'home' | 'profile' | 'connectionId'>
  confirmWorkspaceConflict?: RetirementReceiverDependencies['confirmWorkspaceConflict']
  journalRoot?: string
  adapter?: RetirementAdapter
  assertAdmitted?: (request: RetirementRequest) => Promise<void>
  probeSnapshot?: (record: RetirementRecord, payload: PayloadInfo) => Promise<void>
}
export interface RetirementReception {
  complete(probe: RetirementReadinessProbe): Promise<RetirementOutcome>
}


/** Run only in the lock-owning destination, before normal home selection/migrations. */
export async function prepareRetirementStartup(options: RetirementStartupOptions): Promise<RetirementReception | null> {
  let correlation = parseRetirementArgument(options.argv)

  if (!correlation && options.payload) {
    const pending = await pendingRetirement(
      options.journalRoot ?? defaultRetirementRoot(),
      (record: RetirementRecord): boolean =>
        record.receiverStarted && insideRetirementRoot(record.request.destination.appPath, process.execPath)
    )

    if (pending) {
      correlation = { id: pending.id, token: (await pending.read()).request.token }
    }
  }

  if (!correlation) {
    return null
  }

  if (!options.payload) {
    throw new Error('The retirement receiver requires the bundled stable application')
  }

  const adapter: RetirementAdapter = options.adapter ?? nativeRetirementAdapter()

  const journal: RetirementJournal = await RetirementJournal.open(
    options.journalRoot ?? defaultRetirementRoot(),
    correlation.id
  )

  const file: string = path.join(options.userData, 'active-profile.json')
  let expected: DesktopBootPreference | null = readDesktopBootPreference(file)

  const dependencies: RetirementReceiverDependencies = {
    runningSelection: options.runningSelection,
    confirmWorkspaceConflict: options.confirmWorkspaceConflict,
    verifyRunningDestination: async (request: RetirementRequest): Promise<string> => {
      const executable: string = await adapter.verifyRunningDestination(request)
      await (options.assertAdmitted ?? assertRetirementAdmitted)(request)

      return executable
    },
    assertCompatibleSnapshot: async (record: RetirementRecord): Promise<void> => {
      await (options.probeSnapshot ?? probeRetirementSnapshot)(record, options.payload!)
    },
    prepareConnection: (selection: RetirementSelection): RetirementSelection => {
      if (!selection.connection) { return selection }
      const resolved: RetirementConnectionSelection = prepareRetirementConnection(options.userData, selection.connectionId, selection.connection)
      return { ...selection, connectionId: resolved.id, connection: resolved.settings }
    },
    readSelection: async (): Promise<RetirementExistingSelection | null> => {
      expected = readDesktopBootPreference(file)

      const running: Pick<RetirementExistingSelection, 'home' | 'profile' | 'connectionId'> | undefined =
        options.runningSelection?.()


      if (running) {
        return { ...running, operatorOverride: options.operatorOverride }
      }

      const configured: boolean = !(
        !expected &&
        !options.operatorOverride &&
        !(await retirementPathExists(path.join(options.defaultHome, 'config.yaml'))) &&
        retirementSelectedConnection(options.userData, 'default') === 'local'
      )

      const selection: RetirementExistingSelection = {
        home: options.operatorOverride ? options.defaultHome : (expected?.home ?? options.defaultHome),
        profile: expected?.profile ?? 'default',
        connectionId: retirementSelectedConnection(options.userData, expected?.profile ?? 'default'),
        operatorOverride: options.operatorOverride
      }
      if (!configured) { selection.configured = false }
      return selection
    },
    adoptSelection: async (record: RetirementRecord): Promise<void> => {
      const selection = record.request.selection
      const current: string = retirementSelectedConnection(options.userData, selection.profile)
      if (current !== selection.connectionId && current !== record.previousSelection?.connectionId) {
        throw new Error('Stable connection changed while awaiting adoption; review the migration again')
      }
      adoptRetirementConnection(options.userData, selection.connectionId, selection.profile, selection.connection)
      adoptDesktopHome(file, { home: selection.home, profile: selection.profile }, expected)
    },
    // Bound below only after the real renderer and backend have started.
    assertReady: async (): Promise<never> => {
      throw new Error('Destination renderer and backend have not reported readiness')
    }
  }

  await receiveRetirement(journal, correlation.token, dependencies)

  const token: string = correlation.token
  return {
    complete: (probe: RetirementReadinessProbe): Promise<RetirementOutcome> => awaitRetirementCleanup(async () => {
      dependencies.assertReady = (record: RetirementRecord): Promise<void> => verifyRetirementReadiness(record, probe)
      const record: RetirementRecord = await journal.read()
      if (record.request.selection.choice === 'open-preview' && record.request.selection.connection) {
        assertRetirementConnection(options.userData, record.request.selection.connectionId, record.request.selection.connection)
      }
      await dependencies.assertReady(record)
      if (record.stage === 'destination-installed') {
        await completeRetirementReception(journal, token, dependencies)
      }
      return resumeRetirement(journal, {
        native: adapter,
        assertAdmitted: options.assertAdmitted ?? assertRetirementAdmitted,
        // Native cleanup checks all source package processes, not just this app's child.
        assertSourceQuiescent: async (): Promise<void> => {},
        assertDestinationReady: dependencies.assertReady
      }, 'destination')
    })
  }
}

async function pendingRetirement(
  root: string,
  matches: (record: RetirementRecord) => boolean
): Promise<RetirementJournal | null> {
  if (!(await retirementPathExists(root))) {
    return null
  }

  for (const id of await readdir(root)) {
    if (!/^[a-f0-9]{32}$/.test(id) || !(await retirementPathExists(path.join(root, id, 'journal.json')))) {
      continue
    }

    const journal = await RetirementJournal.open(root, id)
    const record = await journal.read()

    if (record.stage !== 'complete' && matches(record)) {
      return journal
    }
  }

  return null
}

/** Once stable has opened state, a restarted preview may only forward, never reopen its old backend. */
export async function forwardRetiredPreview(): Promise<boolean> {
  const pending = await pendingRetirement(
    defaultRetirementRoot(),
    (record: RetirementRecord): boolean =>
      record.receiverStarted && record.request.source.executable === process.execPath
  )

  if (!pending) {
    return false
  }

  const record = await pending.read()
  await assertRetirementAdmitted(record.request)
  await nativeRetirementAdapter().activate(record.request, pending)

  return true
}

export interface RetirementHostOptions {
  home: string
  userData: string
  payload: PayloadInfo
  profile: () => string
  connection: () => RetirementConnectionSelection
  stop: () => Promise<void>
  restore: () => Promise<void>
  quit: () => void
  progress: (message: string) => void
}

export class RetirementHost implements ChannelRetirementCallbacks {
  private journal: RetirementJournal | null = null
  constructor(private readonly options: RetirementHostOptions) {}

  async check(retirement: ChannelRetirement): Promise<Pick<ChannelRetirementStatus, 'state' | 'message'>> {
    try {
      this.journal ??= await pendingRetirement(
        defaultRetirementRoot(),
        (record: RetirementRecord): boolean =>
          record.request.source.userData === this.options.userData &&
          record.request.sourceBuild.commit === retirement.source.commit
      )
      await this.request(retirement)

      return { state: 'available' }
    } catch (error) {
      return { state: 'conflict', message: error instanceof Error ? error.message : String(error) }
    }
  }

  private async request(
    retirement: ChannelRetirement,
    choice: RetirementWorkspaceChoice = 'open-preview'
  ): Promise<RetirementRequest> {
    const connection = this.options.connection()
    const source = await discoverRetirementSource(
      retirement.source,
      this.options.home,
      this.options.userData,
      this.options.profile()
    )
    source.connectionId = connection.id
    const keyPath: string | null = connection.settings?.keyPath ? await canonicalRetirementPath(connection.settings.keyPath) : null
    if (keyPath && source.removalRoots.some(root => insideRetirementRoot(root, keyPath))) {
      throw new Error('The SSH key is inside package-private storage. Move it to persistent storage and update the saved connection before migration')
    }

    const destination = await discoverRetirementDestination(retirement.target, source)
    const correlation = newRetirementCorrelation()
    const home: string = source.removalRoots.some(root => insideRetirementRoot(root, source.home))
      ? path.join(defaultRetirementRoot(), correlation.id, 'home') : source.home
    if (home !== source.home && (source.platform !== 'win32' || insideRetirementRoot(source.appPath, source.home))) {
      throw new Error('A workspace inside the application bundle needs a complete external archive before migration; preview preserved')
    }
    const connectionId: string = connection.id === 'local' ? 'local' : `retired-${retirement.source.identity.token}-${connection.id}`

    return {
      protocol: 1,
      ...correlation,
      source,
      destination,
      selection: { home, profile: source.profile, connectionId, choice,
        connection: connection.settings, reauthenticate: connection.id !== 'local' },
      sourceBuild: retirement.source,
      destinationManifestSha256: retirement.target.manifestSha256,
      consent: { install: true, removePreview: true, replaceExistingStable: true }
    }
  }

  async apply(retirement: ChannelRetirement, consent: RetirementConsent): Promise<UpdaterApplyResultWire> {
    let stopped: boolean = false

    try {
      if (
        consent.installStable !== true ||
        consent.removePreview !== true ||
        !['open-preview', 'keep-stable'].includes(consent.workspaceChoice)
      ) {
        throw new Error('Explicit retirement consent and workspace choice are required')
      }

      const previous: RetirementRecord | null =
        this.journal && (await retirementPathExists(path.join(this.journal.directory, 'journal.json')))
          ? await this.journal.read()
          : null

      const request: RetirementRequest = previous?.request ?? (await this.request(retirement, consent.workspaceChoice))

      if (this.journal && !previous) {
        request.id = this.journal.id
        if (request.selection.home !== request.source.home) {
          request.selection.home = path.join(this.journal.directory, 'home')
        }
      }

      this.journal ??= await RetirementJournal.open(defaultRetirementRoot(), request.id)
      const journal: RetirementJournal = this.journal

      const deps: RetirementDependencies = {
        native: nativeRetirementAdapter(),
        assertAdmitted: assertRetirementAdmitted,
        assertSourceQuiescent: async (): Promise<void> => {
          await this.options.stop()
          stopped = true
        },
        assertDestinationReady: async (): Promise<never> => {
          throw new Error('Only the destination process may acknowledge readiness')
        },
        prepareState: async (): Promise<{ snapshotHome: string; selectedHome: string }> => {
          const tools = retirementSnapshotTools(this.options.payload)
          const snapshotHome: string = await snapshotRetirementHome(request.source.home, path.join(journal.directory, 'snapshot'), tools)
          if (request.selection.home !== request.source.home) {
            await snapshotRetirementHome(request.source.home, request.selection.home, tools)
          }
          return { snapshotHome, selectedHome: request.selection.home }
        }
      }

      this.options.progress('Preparing a private state snapshot before installing stable…')
      await prepareRetirement(journal, request, deps)
      // Preparation can be resumed; never reopen preview readers after stable admission.
      await this.options.stop()
      stopped = true
      this.options.progress('Installing and opening the pinned stable application…')
      await resumeRetirement(journal, deps, 'source')
      this.options.quit()

      return { ok: true, handedOff: true }
    } catch (error) {
      const record: RetirementRecord | null =
        this.journal && (await retirementPathExists(path.join(this.journal.directory, 'journal.json')))
          ? await this.journal.read()
          : null

      if (stopped && !record?.receiverStarted) {
        await this.options.restore()
      }

      const message: string = error instanceof Error ? error.message : String(error)

      return { ok: false, error: 'retirement-blocked', message }
    }
  }
}
