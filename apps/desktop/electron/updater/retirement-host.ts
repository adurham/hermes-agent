import { open, readFile, readdir, unlink } from 'node:fs/promises'
import type { FileHandle } from 'node:fs/promises'
import path from 'node:path'

import { adoptDesktopHome, readDesktopBootPreference, type DesktopBootPreference } from '../desktop-boot-preference'
import type { PayloadInfo } from '../payload-backend'
import { ChannelResolver, type ChannelRetirement } from './channel'
import type { ChannelRetirementCallbacks, ChannelRetirementStatus } from './channel-strategy'
import { probeRetirementSnapshot, retirementSnapshotTools } from './retirement-compatibility'
import { assertPersistentRetirementHome, discoverRetirementDestination, discoverRetirementSource } from './retirement-discovery'
import { MacRetirementAdapter } from './retirement-macos'
import { retirementPathExists } from './retirement-native'
import { snapshotRetirementHome } from './retirement-preservation'
import { completeRetirementReception, parseRetirementArgument, receiveRetirement, type RetirementExistingSelection, type RetirementReceiverDependencies } from './retirement-receiver'
import { defaultRetirementRoot, insideRetirementRoot, newRetirementCorrelation, RetirementJournal, type RetirementRecord, type RetirementRequest } from './retirement-state'
import { prepareRetirement, resumeRetirement, type RetirementDependencies, type RetirementOutcome } from './retirement'
import { WindowsRetirementAdapter } from './retirement-windows'
import type { UpdaterApplyResultWire } from './index'

export type RetirementAdapter = MacRetirementAdapter | WindowsRetirementAdapter
export function nativeRetirementAdapter(): RetirementAdapter {
  if (process.platform === 'darwin') { return new MacRetirementAdapter() }
  if (process.platform === 'win32') { return new WindowsRetirementAdapter() }
  throw new Error('Native retirement requires macOS or Windows')
}

export async function assertRetirementQualified(request: RetirementRequest): Promise<void> {
  const build = request.qualification.sourceBuild
  if (!build) { throw new Error('Retirement journal has no baked source build binding') }
  const resolved = await new ChannelResolver({ build, platform: request.source.platform,
    arch: request.source.architecture, signer: request.source.signer }).resolve()
  if (resolved.kind !== 'retirement' ||
      resolved.retirement.target.manifest.request.commit !== request.destination.commit ||
      resolved.retirement.target.package.artifact.sha256 !== request.destination.artifact.sha256 ||
      resolved.retirement.target.package.identity !== request.destination.identity ||
      resolved.retirement.constraints.at(-1)?.compatibilitySha256 !== request.qualification.sha256) {
    throw new Error('The exact retirement qualification is no longer available')
  }
}

/** A cross-process lock lives outside either application's removal footprint. */
async function acquireRetirementLifecycle(request: RetirementRequest): Promise<() => Promise<void>> {
  const journal: RetirementJournal = await RetirementJournal.open(defaultRetirementRoot(), request.id)
  const lock: string = path.join(journal.directory, 'lifecycle.lock')
  const handle: FileHandle = await open(lock, 'wx', 0o600)
  await handle.writeFile(String(process.pid))
  return async (): Promise<void> => { await handle.close(); await unlink(lock) }
}

export interface RetirementStartupOptions {
  argv: readonly string[]
  userData: string
  defaultHome: string
  operatorOverride: boolean
  payload: PayloadInfo | null
  /** A live stable process cannot silently retarget already-open databases. */
  runningSelection?: { home: string; profile: string; connectionId: string }
  journalRoot?: string
  adapter?: RetirementAdapter
  assertQualified?: (request: RetirementRequest) => Promise<void>
  probeSnapshot?: (record: RetirementRecord, payload: PayloadInfo) => Promise<void>
}
export interface RetirementReception {
  journal: RetirementJournal
  token: string
  dependencies: RetirementReceiverDependencies
}

interface StoredLocalSelection {
  mode?: string
  primary?: string
  lastUsed?: string
  launchMode?: string
  profileRemotes?: { [profile: string]: { mode?: string } }
}

async function assertLocalDesktopSelection(userData: string, profile: string): Promise<void> {
  for (const file of ['connection.json', 'connections.json']) {
    const location: string = path.join(userData, file)
    if (!(await retirementPathExists(location))) { continue }
    const stored: StoredLocalSelection = JSON.parse(await readFile(location, 'utf8'))
    if ((stored.mode && stored.mode !== 'local') || (stored.primary && stored.primary !== 'local') ||
        (stored.launchMode === 'last-used' && stored.lastUsed && stored.lastUsed !== 'local') ||
        (stored.profileRemotes?.[profile]?.mode && stored.profileRemotes[profile].mode !== 'local')) {
      throw new Error('Stable has a remote workspace selected. Select Local in stable, then retry migration. Saved connections are preserved.')
    }
  }
}

/** Run only in the lock-owning destination, before normal home selection/migrations. */
export async function prepareRetirementStartup(options: RetirementStartupOptions): Promise<RetirementReception | null> {
  let correlation = parseRetirementArgument(options.argv)
  if (!correlation && options.payload) {
    const pending = await pendingRetirement(options.journalRoot ?? defaultRetirementRoot(),
      (record: RetirementRecord): boolean => record.receiverApplied && insideRetirementRoot(record.request.destination.appPath, process.execPath))
    if (pending) { correlation = { id: pending.id, token: (await pending.read()).request.token } }
  }
  if (!correlation) { return null }
  if (!options.payload) { throw new Error('The retirement receiver requires the bundled stable application') }
  const adapter: RetirementAdapter = options.adapter ?? nativeRetirementAdapter()
  const journal: RetirementJournal = await RetirementJournal.open(options.journalRoot ?? defaultRetirementRoot(), correlation.id)
  const file: string = path.join(options.userData, 'active-profile.json')
  let expected: DesktopBootPreference | null = readDesktopBootPreference(file)
  const dependencies: RetirementReceiverDependencies = {
    verifyRunningDestination: async (request: RetirementRequest): Promise<string> => {
      const executable: string = await adapter.verifyRunningDestination(request)
      await (options.assertQualified ?? assertRetirementQualified)(request)
      return executable
    },
    assertCompatibleSnapshot: async (record: RetirementRecord): Promise<void> => {
      await (options.probeSnapshot ?? probeRetirementSnapshot)(record, options.payload!)
    },
    readSelection: async (): Promise<RetirementExistingSelection | null> => {
      expected = readDesktopBootPreference(file)
      const record: RetirementRecord = await journal.read()
      await assertLocalDesktopSelection(options.userData, record.request.selection.profile)
      if (options.runningSelection) { return { ...options.runningSelection, operatorOverride: true } }
      if (!expected && !options.operatorOverride && !(await retirementPathExists(path.join(options.defaultHome, 'config.yaml')))) { return null }
      return { home: expected?.home ?? options.defaultHome, profile: expected?.profile ?? 'default',
        connectionId: 'local', operatorOverride: options.operatorOverride }
    },
    adoptSelection: async (record: RetirementRecord): Promise<void> => {
      const selection = record.request.selection
      if (selection.connectionId !== 'local') { throw new Error('Open the remote connection in stable and sign in before migrating. No encrypted credentials were copied.') }
      adoptDesktopHome(file, { home: selection.home, profile: selection.profile }, expected)
    },
    // Bound below only after the real renderer and backend have started.
    assertReady: async (): Promise<never> => { throw new Error('Destination renderer and backend have not reported readiness') }
  }
  await receiveRetirement(journal, correlation.token, dependencies)
  return { journal, token: correlation.token, dependencies }
}

export async function finishRetirementStartup(
  reception: RetirementReception,
  assertReady: (record: RetirementRecord) => Promise<void>
): Promise<RetirementOutcome> {
  reception.dependencies.assertReady = assertReady
  const record: RetirementRecord = await reception.journal.read()
  if (record.stage === 'destination-installed') {
    await completeRetirementReception(reception.journal, reception.token, reception.dependencies)
  }
  return resumeRetirement(reception.journal, {
    native: nativeRetirementAdapter(), assertQualified: assertRetirementQualified,
    acquireLifecycle: acquireRetirementLifecycle,
    assertSourceQuiescent: async (): Promise<void> => { /* Native removal rechecks all source package processes. */ },
    assertDestinationReady: assertReady,
    prepareState: async (): Promise<never> => { throw new Error('The destination cannot prepare a source snapshot') }
  }, 'destination')
}

async function pendingRetirement(root: string, matches: (record: RetirementRecord) => boolean): Promise<RetirementJournal | null> {
  if (!(await retirementPathExists(root))) { return null }
  for (const id of await readdir(root)) {
    if (!/^[a-f0-9]{32}$/.test(id) || !(await retirementPathExists(path.join(root, id, 'journal.json')))) { continue }
    const journal = await RetirementJournal.open(root, id)
    const record = await journal.read()
    if (record.stage !== 'complete' && matches(record)) { return journal }
  }
  return null
}

/** Once stable has opened state, a restarted preview may only forward, never reopen its old backend. */
export async function forwardRetiredPreview(): Promise<boolean> {
  const pending = await pendingRetirement(defaultRetirementRoot(), (record: RetirementRecord): boolean =>
    record.receiverStarted && record.request.source.executable === process.execPath)
  if (!pending) { return false }
  const record = await pending.read()
  await assertRetirementQualified(record.request)
  await nativeRetirementAdapter().activate(record.request, pending)
  return true
}

export interface RetirementHostOptions {
  home: string
  userData: string
  payload: PayloadInfo
  profile: () => string
  isLocal: () => boolean
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
      this.journal ??= await pendingRetirement(defaultRetirementRoot(), (record: RetirementRecord): boolean =>
        record.request.source.userData === this.options.userData && record.request.qualification.sourceCommit === retirement.source.commit)
      await this.request(retirement)
      return { state: 'available' }
    } catch (error) {
      return { state: 'conflict', message: error instanceof Error ? error.message : String(error) }
    }
  }

  private async request(retirement: ChannelRetirement): Promise<RetirementRequest> {
    if (!this.options.isLocal()) {
      throw new Error('Select your local workspace before migrating. Remote connections must be opened and authenticated in stable separately; the preview and all saved credentials are preserved.')
    }
    const source = await discoverRetirementSource(retirement.source, this.options.home, this.options.userData, this.options.profile())
    assertPersistentRetirementHome(source)
    const destination = await discoverRetirementDestination(retirement.target, source)
    return { protocol: 1, ...newRetirementCorrelation(), source, destination,
      selection: { home: source.home, profile: source.profile, connectionId: 'local', choice: 'open-preview', reauthenticate: false },
      qualification: { sha256: retirement.constraints.at(-1)!.compatibilitySha256, sourceCommit: retirement.source.commit,
        destinationCommit: destination.commit, receiverProtocol: 1, sourceBuild: retirement.source },
      consent: { install: true, removePreview: true, replaceExistingStable: true } }
  }

  async apply(retirement: ChannelRetirement): Promise<UpdaterApplyResultWire> {
    let stopped: boolean = false
    try {
      const request: RetirementRequest = this.journal ? (await this.journal.read()).request : await this.request(retirement)
      this.journal ??= await RetirementJournal.open(defaultRetirementRoot(), request.id)
      const journal: RetirementJournal = this.journal
      const deps: RetirementDependencies = {
        native: nativeRetirementAdapter(), assertQualified: assertRetirementQualified,
        acquireLifecycle: acquireRetirementLifecycle,
        assertSourceQuiescent: async (): Promise<void> => { await this.options.stop(); stopped = true },
        assertDestinationReady: async (): Promise<never> => { throw new Error('Only the destination process may acknowledge readiness') },
        prepareState: async (): Promise<{ snapshotHome: string; selectedHome: string }> => ({
          snapshotHome: await snapshotRetirementHome(request.source.home, path.join(journal.directory, 'snapshot'), retirementSnapshotTools(this.options.payload)),
          selectedHome: request.selection.home
        })
      }
      this.options.progress('Preparing a private state snapshot before installing stable…')
      await prepareRetirement(journal, request, deps)
      // Preparation can be resumed; never reopen preview readers after stable admission.
      await this.options.stop(); stopped = true
      this.options.progress('Installing and opening the exact qualified stable application…')
      await resumeRetirement(journal, deps, 'source')
      this.options.quit()
      return { ok: true, handedOff: true }
    } catch (error) {
      const record: RetirementRecord | null = this.journal && await retirementPathExists(path.join(this.journal.directory, 'journal.json')) ? await this.journal.read() : null
      if (stopped && !record?.receiverStarted) { await this.options.restore() }
      const message: string = error instanceof Error ? error.message : String(error)
      return { ok: false, error: 'retirement-blocked', message }
    }
  }
}
