import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, mkdir, open, readFile, realpath, rename, unlink } from 'node:fs/promises'
import type { FileHandle } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import type { ChannelBuild } from './channel-protocol'
import type { PinnedArtifact } from './artifact'
import type { RetirementConnection } from './retirement-connections'
import { runRetirementCommand } from './retirement-native'

export type RetirementStage =
  'prepared' | 'destination-installed' | 'destination-ready' | 'preview-removed' | 'complete'
export type RetirementPlatform = 'darwin' | 'win32'

export interface RetirementNativeIdentity {
  platform: RetirementPlatform
  appPath: string
  identity: string
  nativeVersion: string
  signer: string
  architecture: 'arm64' | 'x64'
  applicationId: string | null
}

export interface RetirementSource extends RetirementNativeIdentity {
  executable: string
  packageFullName: string | null
  home: string
  userData: string
  profile: string
  connectionId: string
  /** Include the AppX LocalState/LocalCache roots, not only InstallLocation. */
  removalRoots: string[]
}

export interface RetirementDestination extends RetirementNativeIdentity {
  packageFamilyName: string | null
  artifact: PinnedArtifact
  commit: string
}

export type RetirementWorkspaceChoice = 'open-preview' | 'keep-stable'

export interface RetirementConsent {
  installStable: true
  removePreview: true
  workspaceChoice: RetirementWorkspaceChoice
}

export interface RetirementSelection {
  home: string
  profile: string
  connectionId: string
  choice: RetirementWorkspaceChoice
  reauthenticate: boolean
  connection?: RetirementConnection
  conflictConsent?: { home: string; profile: string; connectionId: string; operatorOverride: boolean }
}

export interface RetirementRequest {
  protocol: 1
  id: string
  token: string
  source: RetirementSource
  destination: RetirementDestination
  selection: RetirementSelection
  sourceBuild: ChannelBuild
  destinationManifestSha256: string
  consent: { install: boolean; removePreview: boolean; replaceExistingStable: boolean }
}

export interface RetirementStateSnapshot {
  snapshotHome: string
  selectedHome: string
}

export interface RetirementReady {
  requestDigest: string
  id: string
  home: string
  profile: string
  connectionId: string
  executable: string
  commit: string
  pid: number
  instance: string
  mac: string
}

export interface RetirementRecord {
  request: RetirementRequest
  requestDigest: string
  stage: RetirementStage
  state: RetirementStateSnapshot
  receiverStarted: boolean
  receiverApplied: boolean
  previousSelection: { home: string; profile: string; connectionId: string; operatorOverride: boolean } | null
  ready: RetirementReady | null
}

export interface RetirementCorrelation {
  id: string
  token: string
}

export function newRetirementCorrelation(): RetirementCorrelation {
  return { id: randomBytes(16).toString('hex'), token: randomBytes(32).toString('hex') }
}

export function defaultRetirementRoot(): string {
  return path.join(os.homedir(), '.hermes-desktop-retirement')
}

async function protectWindowsDirectory(directory: string): Promise<void> {
  const encoded: string = Buffer.from(directory, 'utf8').toString('base64')
  const script: string = `$ErrorActionPreference='Stop'; $p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encoded}')); $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; $acl=[Security.AccessControl.DirectorySecurity]::new(); $acl.SetOwner($sid); $acl.SetAccessRuleProtection($true,$false); $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')); Set-Acl -LiteralPath $p -AclObject $acl; $read=Get-Acl -LiteralPath $p; if ($read.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $sid.Value -or -not $read.AreAccessRulesProtected) { throw 'Private journal ACL did not apply' }; foreach ($rule in $read.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) { if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -cne $sid.Value) { throw 'Journal ACL grants another identity access' } }`
  await runRetirementCommand(
    path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'),
    ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')]
  )
}

export function retirementDigest(request: RetirementRequest): string {
  return createHash('sha256').update(JSON.stringify(request)).digest('hex')
}

export function insideRetirementRoot(root: string, candidate: string): boolean {
  const relative: string = path.relative(root, candidate)

  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
}

export async function canonicalRetirementPath(candidate: string): Promise<string> {
  if (!path.isAbsolute(candidate) || candidate.includes('\0')) {
    throw new Error('Retirement requires absolute local paths')
  }

  try {
    return await realpath(candidate)
  } catch (error) {
    if (!(error instanceof Error) || !('code' in error) || error.code !== 'ENOENT') {
      throw error
    }

    const parent: string = path.dirname(candidate)

    if (parent === candidate) {
      throw error
    }

    return path.join(await canonicalRetirementPath(parent), path.basename(candidate))
  }
}

export function validateRetirementRequest(request: RetirementRequest): void {
  if (request.protocol !== 1 || !/^[a-f0-9]{32}$/.test(request.id) || !/^[a-f0-9]{64}$/.test(request.token)) {
    throw new Error('Invalid retirement correlation')
  }

  if (!request.consent.install || !request.consent.removePreview) {
    throw new Error('Retirement consent required')
  }

  const destination: RetirementDestination = request.destination
  validateRetirementTarget(request)

  if (request.source.platform !== destination.platform || request.source.identity === destination.identity) {
    throw new Error('Retirement must cross native identities on one platform')
  }

  for (const identity of [request.source, destination]) {
    if (
      !identity.identity ||
      !identity.signer ||
      !identity.nativeVersion ||
      !['arm64', 'x64'].includes(identity.architecture)
    ) {
      throw new Error('Incomplete native retirement identity')
    }
  }

  for (const profile of [request.source.profile, request.selection.profile]) {
    if (!/^[a-zA-Z0-9_-]+$/.test(profile)) {
      throw new Error('Invalid retirement profile')
    }
  }

  if (!['open-preview', 'keep-stable'].includes(request.selection.choice)) {
    throw new Error('Explicit retirement workspace choice required')
  }

  if (!request.selection.connectionId || !request.source.removalRoots.length) {
    throw new Error('Incomplete retirement state inventory')
  }
}

function validateRetirementTarget(request: RetirementRequest): void {
  const destination: RetirementDestination = request.destination

  if (
    !/^[a-f0-9]{40}$/.test(destination.commit) ||
    !/^[a-f0-9]{64}$/.test(destination.artifact.sha256) ||
    !Number.isSafeInteger(destination.artifact.size) ||
    destination.artifact.size <= 0
  ) {
    throw new Error('Invalid pinned retirement artifact')
  }

  const url: URL = new URL(destination.artifact.url)

  if (url.protocol !== 'https:' || url.username || url.password || url.hash) {
    throw new Error('Retirement requires public HTTPS artifact URL')
  }

  if (
    !/^[a-f0-9]{64}$/.test(request.destinationManifestSha256) ||
    !/^[a-f0-9]{40}$/.test(request.sourceBuild.commit)
  ) {
    throw new Error('Missing pinned retirement build')
  }
}

function receiptMac(request: RetirementRequest, ready: Omit<RetirementReady, 'mac'>): string {
  return createHmac('sha256', Buffer.from(request.token, 'hex')).update(JSON.stringify(ready)).digest('hex')
}

export function signRetirementReady(request: RetirementRequest, ready: Omit<RetirementReady, 'mac'>): RetirementReady {
  return { ...ready, mac: receiptMac(request, ready) }
}

export function assertRetirementReady(record: RetirementRecord, ready: RetirementReady): void {
  const { mac, ...body }: RetirementReady = ready
  const expected: string = receiptMac(record.request, body)

  if (
    !/^[a-f0-9]{64}$/.test(mac) ||
    !timingSafeEqual(Buffer.from(expected, 'hex'), Buffer.from(mac, 'hex')) ||
    ready.id !== record.request.id ||
    ready.requestDigest !== record.requestDigest ||
    ready.home !== record.state.selectedHome ||
    ready.profile !== record.request.selection.profile ||
    ready.connectionId !== record.request.selection.connectionId ||
    ready.commit !== record.request.destination.commit ||
    !insideRetirementRoot(record.request.destination.appPath, ready.executable) ||
    !Number.isSafeInteger(ready.pid) ||
    ready.pid <= 0 ||
    !/^[a-f0-9]{64}$/.test(ready.instance)
  ) {
    throw new Error('Stale or mismatched retirement readiness receipt')
  }
}

/** Root must be private physical storage, outside AppX virtualization and removal roots. */
export class RetirementJournal {
  readonly directory: string
  private constructor(
    readonly root: string,
    readonly id: string
  ) {
    this.directory = path.join(root, id)
  }

  static async open(root: string, id: string): Promise<RetirementJournal> {
    if (!/^[a-f0-9]{32}$/.test(id)) {
      throw new Error('Invalid retirement transaction ID')
    }

    if ((await canonicalRetirementPath(root)) !== path.resolve(root)) {
      throw new Error('Journal root must not be a symlink')
    }

    await mkdir(root, { recursive: true, mode: 0o700 })

    const rootInfo: Awaited<ReturnType<typeof lstat>> = await lstat(root)

    if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) {
      throw new Error('Journal root must be an owned directory')
    }

    if (process.platform === 'win32') {
      await protectWindowsDirectory(root)
    }

    await mkdir(path.join(root, id), { mode: 0o700 }).catch((error: NodeJS.ErrnoException): void => {
      if (error.code !== 'EEXIST') {
        throw error
      }
    })

    for (const directory of [root, path.join(root, id)]) {
      const info: Awaited<ReturnType<typeof lstat>> = await lstat(directory)

      if (
        !info.isDirectory() ||
        info.isSymbolicLink() ||
        (process.platform !== 'win32' &&
          ((info.mode & 0o077) !== 0 || !process.getuid || info.uid !== process.getuid()))
      ) {
        throw new Error('Retirement journal is not private and owned by this user')
      }
    }

    if (process.platform === 'win32') {
      await protectWindowsDirectory(path.join(root, id))
    }

    return new RetirementJournal(root, id)
  }

  async assertPaths(request: RetirementRequest): Promise<void> {
    const roots: string[] = await Promise.all(
      [request.source.appPath, ...request.source.removalRoots].map(canonicalRetirementPath)
    )

    for (const candidate of [this.directory, request.selection.home]) {
      const canonical: string = await canonicalRetirementPath(candidate)

      if (roots.some((root: string): boolean => insideRetirementRoot(root, canonical))) {
        throw new Error('State or journal is inside the preview removal footprint; preserve it before retirement')
      }
    }

    const sourceHome: string = await canonicalRetirementPath(request.source.home)
    if (roots.some(root => insideRetirementRoot(root, sourceHome))) {
      const relocated = await lstat(path.join(this.directory, 'home'))
      if (!relocated.isDirectory() || relocated.isSymbolicLink()) {
        throw new Error('Removal-scoped home has not been preserved')
      }
    }
    // Windows archives all package-private bytes before removal. No other
    // adapter may claim preservation of an Electron credential store it deletes.
    if (request.source.platform !== 'win32' && roots.some(root => insideRetirementRoot(root, request.source.userData))) {
      throw new Error('Desktop credentials are inside the removal footprint')
    }

    const sourceApp: string = await canonicalRetirementPath(request.source.appPath)
    const destinationApp: string = await canonicalRetirementPath(request.destination.appPath)

    if (insideRetirementRoot(sourceApp, destinationApp) || insideRetirementRoot(destinationApp, sourceApp)) {
      throw new Error('Source and destination application paths overlap')
    }
  }

  async read(): Promise<RetirementRecord> {
    const handle: FileHandle = await open(
      path.join(this.directory, 'journal.json'),
      constants.O_RDONLY | constants.O_NOFOLLOW
    )

    try {
      const record: RetirementRecord = JSON.parse(await handle.readFile('utf8'))
      validateRetirementRequest(record.request)

      if (
        record.request.id !== this.id ||
        record.requestDigest !== retirementDigest(record.request) ||
        !['prepared', 'destination-installed', 'destination-ready', 'preview-removed', 'complete'].includes(
          record.stage
        )
      ) {
        throw new Error('Retirement journal binding mismatch')
      }

      if (record.ready) {
        assertRetirementReady(record, record.ready)
      }

      return record
    } finally {
      await handle.close()
    }
  }

  async write(record: RetirementRecord): Promise<void> {
    const temporary: string = path.join(this.directory, `${randomBytes(16).toString('hex')}.partial`)
    const handle: FileHandle = await open(temporary, 'wx', 0o600)

    try {
      await handle.writeFile(JSON.stringify(record))
      await handle.sync()
    } finally {
      await handle.close()
    }

    await rename(temporary, path.join(this.directory, 'journal.json'))

    if (process.platform !== 'win32') {
      const directory: FileHandle = await open(this.directory, 'r')

      try {
        await directory.sync()
      } finally {
        await directory.close()
      }
    }
  }

  async exclusive<T>(operation: () => Promise<T>): Promise<T> {
    const lock: string = path.join(this.directory, 'writer.lock')
    let handle: FileHandle

    try {
      handle = await open(lock, 'wx', 0o600)
    } catch (error) {
      if (!(error instanceof Error) || !('code' in error) || error.code !== 'EEXIST') {
        throw error
      }

      const pid: number = Number(await readFile(lock, 'utf8'))

      if (!Number.isSafeInteger(pid) || pid <= 0) {
        throw new Error('Retirement journal lock needs manual recovery')
      }

      try {
        process.kill(pid, 0)
      } catch (probe) {
        if (probe instanceof Error && 'code' in probe && probe.code === 'ESRCH') {
          const reaper: FileHandle = await open(`${lock}.reclaim`, 'wx', 0o600)

          try {
            if (Number(await readFile(lock, 'utf8')) === pid) {
              await unlink(lock)
            }
          } finally {
            await reaper.close()
            await unlink(`${lock}.reclaim`)
          }

          return this.exclusive(operation)
        }

        throw probe
      }

      throw new Error('Retirement transaction is busy')
    }

    try {
      await handle.writeFile(String(process.pid))
      await handle.sync()

      return await operation()
    } finally {
      await handle.close()
      await unlink(lock)
    }
  }
}
