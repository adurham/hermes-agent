import { constants } from 'node:fs'
import { access, lstat, mkdir, mkdtemp, readdir, readFile, rename } from 'node:fs/promises'
import path from 'node:path'

import type * as Electron from 'electron'

import { assertNativeRetirementRemoval, type RetirementNativeAdapter } from './retirement'
import {
  assertRetirementStamp,
  assertRunningRetirementStamp,
  downloadRetirementArtifact,
  type NativeCommandResult,
  retirementPathExists,
  runRetirementCommand
} from './retirement-native'
import { retirementArgument } from './retirement-receiver'
import {
  canonicalRetirementPath,
  insideRetirementRoot,
  type RetirementDestination,
  type RetirementJournal,
  type RetirementNativeIdentity,
  type RetirementRequest
} from './retirement-state'

interface MacInfoPlist {
  CFBundleIdentifier: string
  CFBundleShortVersionString: string
  CFBundleExecutable: string
}
interface MacSourceStamp {
  commit: string
  distribution: string
  payload: string
  updateMechanism: string
}

function requireMac(): void {
  if (process.platform !== 'darwin') {
    throw new Error('Native macOS retirement requires macOS')
  }
}

async function assertOwnedBundle(app: string): Promise<void> {
  if (!app.endsWith('.app') || (await canonicalRetirementPath(app)) !== app) {
    throw new Error('Bundle must be a canonical .app path')
  }

  const info: Awaited<ReturnType<typeof lstat>> = await lstat(app)

  if (!info.isDirectory() || !process.getuid || info.uid !== process.getuid() || (info.mode & 0o022) !== 0) {
    throw new Error('App is not privately owned by this user; use its administrator or steward')
  }

  if (await retirementPathExists(path.join(app, 'Contents/_MASReceipt/receipt'))) {
    throw new Error('App Store owns this application')
  }

  await access(path.dirname(app), constants.W_OK)
}

async function verifyMacBundle(identity: RetirementNativeIdentity, app: string = identity.appPath): Promise<string> {
  requireMac()
  await runRetirementCommand('/usr/bin/codesign', ['--verify', '--deep', '--strict', '--verbose=2', app])
  const signature: NativeCommandResult = await runRetirementCommand('/usr/bin/codesign', ['-dv', '--verbose=4', app])

  if (
    /^TeamIdentifier=(.+)$/m.exec(signature.stderr)?.[1] !== identity.signer ||
    /^Identifier=(.+)$/m.exec(signature.stderr)?.[1] !== identity.identity
  ) {
    throw new Error('macOS signing team or identifier mismatch')
  }

  const plistResult: NativeCommandResult = await runRetirementCommand('/usr/bin/plutil', [
    '-convert',
    'json',
    '-o',
    '-',
    path.join(app, 'Contents/Info.plist')
  ])

  const plist: MacInfoPlist = JSON.parse(plistResult.stdout)

  if (
    plist.CFBundleIdentifier !== identity.identity ||
    plist.CFBundleShortVersionString !== identity.nativeVersion ||
    !plist.CFBundleExecutable ||
    /[/\\]/.test(plist.CFBundleExecutable)
  ) {
    throw new Error('macOS bundle identity or native version mismatch')
  }

  const executable: string = await canonicalRetirementPath(path.join(app, 'Contents/MacOS', plist.CFBundleExecutable))

  if (!insideRetirementRoot(await canonicalRetirementPath(app), executable)) {
    throw new Error('Bundle executable escapes the application')
  }

  const archs: NativeCommandResult = await runRetirementCommand('/usr/bin/lipo', ['-archs', executable])

  if (archs.stdout.trim() !== (identity.architecture === 'arm64' ? 'arm64' : 'x86_64')) {
    throw new Error('macOS native architecture mismatch')
  }

  await runRetirementCommand('/usr/sbin/spctl', ['-a', '-vv', '-t', 'exec', app])

  return executable
}

async function assertNoBundleProcesses(app: string): Promise<void> {
  const result: NativeCommandResult = await runRetirementCommand('/bin/ps', ['-axo', 'comm='])

  if (result.stdout.split('\n').some((command: string): boolean => command.trim().startsWith(`${app}/`))) {
    throw new Error('Application has live processes; stop through its lifecycle owner first')
  }
}

async function stageMacBundle(request: RetirementRequest, journal: RetirementJournal): Promise<string> {
  const artifact: string = await downloadRetirementArtifact(journal, request.destination)

  if (request.destination.artifact.format !== 'zip') {
    throw new Error('macOS retirement requires the signed update ZIP')
  }

  const entries: NativeCommandResult = await runRetirementCommand('/usr/bin/unzip', ['-Z1', artifact])

  for (const name of entries.stdout.split('\n').filter(Boolean)) {
    if (name.startsWith('/') || name.includes('\\') || name.split('/').includes('..')) {
      throw new Error('Unsafe retirement ZIP path')
    }
  }

  const stage: string = await mkdtemp(path.join(journal.directory, 'extract-'))
  await runRetirementCommand('/usr/bin/ditto', ['-x', '-k', artifact, stage], 600_000)
  const apps: string[] = (await readdir(stage)).filter((name: string): boolean => name.endsWith('.app'))

  if (apps.length !== 1) {
    throw new Error('Expected one application in the pinned ZIP')
  }

  const app: string = path.join(stage, apps[0])
  await verifyMacBundle(request.destination, app)
  await assertRetirementStamp(path.join(app, 'Contents/Resources/install-stamp.json'), request.destination)

  return app
}

export class MacRetirementAdapter implements RetirementNativeAdapter {
  async verifyDestination(request: RetirementRequest): Promise<void> {
    await verifyMacBundle(request.destination)
    await assertRetirementStamp(
      path.join(request.destination.appPath, 'Contents/Resources/install-stamp.json'),
      request.destination
    )
  }

  async verifyRunningDestination(request: RetirementRequest): Promise<string> {
    assertRunningRetirementStamp(request.destination)
    await this.verifyDestination(request)
    const executable: string = await verifyMacBundle(request.destination)

    if ((await canonicalRetirementPath(process.execPath)) !== executable) {
      throw new Error('Receiver is not the exact installed stable executable')
    }

    return executable
  }

  async install(request: RetirementRequest, journal: RetirementJournal): Promise<void> {
    requireMac()
    const target: RetirementDestination = request.destination
    const parent: string = path.dirname(target.appPath)

    if ((await canonicalRetirementPath(parent)) !== parent) {
      throw new Error('Destination parent must be canonical')
    }

    await access(parent, constants.W_OK)

    if (await retirementPathExists(target.appPath)) {
      try {
        await this.verifyDestination(request)

        return
      } catch (error) {
        if (!request.consent.replaceExistingStable) {
          throw new Error('Existing stable differs from the exact target; replacement consent required', {
            cause: error
          })
        }
      }

      await assertOwnedBundle(target.appPath)

      const plistOutput: NativeCommandResult = await runRetirementCommand('/usr/bin/plutil', [
        '-convert',
        'json',
        '-o',
        '-',
        path.join(target.appPath, 'Contents/Info.plist')
      ])

      const previous: MacInfoPlist = JSON.parse(plistOutput.stdout)
      const oldParts: number[] = previous.CFBundleShortVersionString.split('.').map(Number)
      const targetParts: number[] = target.nativeVersion.split('.').map(Number)

      if (
        !/^\d+\.\d+\.\d+$/.test(previous.CFBundleShortVersionString) ||
        !/^\d+\.\d+\.\d+$/.test(target.nativeVersion)
      ) {
        throw new Error('Stable native versions must be numeric release versions')
      }

      const firstDifference: number = oldParts.findIndex(
        (part: number, index: number): boolean => part !== targetParts[index]
      )

      if (firstDifference < 0 || oldParts[firstDifference] > targetParts[firstDifference]) {
        throw new Error('Refusing stable downgrade or same-version replacement')
      }

      await verifyMacBundle({ ...target, nativeVersion: previous.CFBundleShortVersionString })

      const previousStamp: MacSourceStamp = JSON.parse(
        await readFile(path.join(target.appPath, 'Contents/Resources/install-stamp.json'), 'utf8')
      )

      if (
        previousStamp.distribution !== 'desktop-app' ||
        previousStamp.payload !== 'bundled' ||
        previousStamp.updateMechanism !== 'electron-updater'
      ) {
        throw new Error('Existing application has a different steward')
      }

      await assertNoBundleProcesses(target.appPath)
    }

    const extracted: string = await stageMacBundle(request, journal)
    const stagingDirectory: string = await mkdtemp(path.join(parent, `.hermes-retirement-${request.id}-`))
    const staged: string = path.join(stagingDirectory, 'Hermes.app')
    const backup: string = path.join(parent, `.hermes-retirement-${request.id}-previous.app`)

    await mkdir(staged, { mode: 0o700 })
    await runRetirementCommand('/usr/bin/ditto', [extracted, staged], 600_000)

    await verifyMacBundle(target, staged)
    await assertRetirementStamp(path.join(staged, 'Contents/Resources/install-stamp.json'), target)

    if (await retirementPathExists(target.appPath)) {
      if (await retirementPathExists(backup)) {
        throw new Error('Prior stable backup already exists; refusing overwrite')
      }

      await assertNoBundleProcesses(target.appPath)
      await rename(target.appPath, backup)
    }

    await rename(staged, target.appPath)
    await this.verifyDestination(request)
  }

  async activate(request: RetirementRequest, journal: RetirementJournal): Promise<void> {
    await this.verifyDestination(request)
    // -n starts the exact bundle even if another stable instance owns the lock.
    // Electron forwards the argument to that instance; the receiver verifies its own stamp.
    await runRetirementCommand('/usr/bin/open', [
      '-n',
      '-a',
      request.destination.appPath,
      '--args',
      retirementArgument(journal.id, request.token)
    ])
  }

  async removePreview(request: RetirementRequest, journal: RetirementJournal): Promise<void> {
    await assertNativeRetirementRemoval(journal, request)
    await this.verifyRunningDestination(request)
    const source: string = request.source.appPath
    await assertOwnedBundle(source)
    await verifyMacBundle(request.source)

    const stamp: MacSourceStamp = JSON.parse(
      await readFile(path.join(source, 'Contents/Resources/install-stamp.json'), 'utf8')
    )

    if (
      stamp.commit !== request.qualification.sourceCommit ||
      stamp.distribution !== 'desktop-app' ||
      stamp.payload !== 'bundled'
    ) {
      throw new Error('Preview source stamp changed before removal')
    }

    await assertNoBundleProcesses(source)
    const { shell }: typeof Electron = await import('electron')
    await shell.trashItem(source)
  }

  async isPreviewRemoved(request: RetirementRequest): Promise<boolean> {
    requireMac()

    return !(await retirementPathExists(request.source.appPath))
  }
}
