import path from 'node:path'

import type { ChannelBuild } from './channel-protocol'
import type { ChannelTarget } from './channel'
import { runRetirementCommand, type NativeCommandResult } from './retirement-native'
import { canonicalRetirementPath, insideRetirementRoot, type RetirementDestination, type RetirementSource } from './retirement-state'

interface InstalledApp {
  identity: string
  signer: string
  appPath: string
  nativeVersion: string
  executable: string
  applicationId: string | null
  packageFullName: string | null
  packageFamilyName: string | null
  removalRoots: string[]
}

export async function runRetirementPowerShell(script: string, input: string = ''): Promise<NativeCommandResult> {
  if (process.platform !== 'win32') { throw new Error('This operation requires Windows') }
  return runRetirementCommand(
    path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'),
    ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
    600_000, input
  )
}

/** Derive trust from the signed running application, never from an R2 document. */
export async function inspectRunningChannelApp(build: ChannelBuild): Promise<InstalledApp> {
  const executable: string = await canonicalRetirementPath(process.execPath)
  if (process.platform === 'darwin') {
    const appPath: string = path.resolve(executable, '../../..')
    await runRetirementCommand('/usr/bin/codesign', ['--verify', '--deep', '--strict', appPath])
    const signature: NativeCommandResult = await runRetirementCommand('/usr/bin/codesign', ['-dv', '--verbose=4', appPath])
    const signer: string = /^TeamIdentifier=(.+)$/m.exec(signature.stderr)?.[1] || ''
    const identity: string = /^Identifier=(.+)$/m.exec(signature.stderr)?.[1] || ''
    if (!/^[A-Z0-9]{10}$/.test(signer) || identity !== build.identity.appId) {
      throw new Error('Running channel application signature does not match its baked identity')
    }
    return { identity, signer, appPath, executable, nativeVersion: build.version,
      applicationId: null, packageFullName: null, packageFamilyName: null, removalRoots: [appPath] }
  }
  const result: NativeCommandResult = await runRetirementPowerShell(String.raw`
$ErrorActionPreference='Stop'
$p=[Console]::In.ReadToEnd() | ConvertFrom-Json
$packages=@(Get-AppxPackage -Name $p.identity | Where-Object { $_.Name -ceq $p.identity })
if ($packages.Count -ne 1) { throw 'Running channel package is not uniquely registered' }
$pkg=$packages[0]
if ($pkg.Status.ToString() -cne 'Ok' -or $pkg.SignatureKind.ToString() -cne 'Developer' -or $pkg.IsDevelopmentMode -or $pkg.NonRemovable) { throw 'Channel package is not a trusted current-user sideload' }
if (-not $p.executable.StartsWith($pkg.InstallLocation.TrimEnd('\')+'\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Running executable is outside registered package' }
$manifest=Get-AppxPackageManifest -Package $pkg.PackageFullName
$apps=@($manifest.Package.Applications.Application | Where-Object { [IO.Path]::GetFullPath((Join-Path $pkg.InstallLocation $_.Executable)) -ieq $p.executable })
if ($apps.Count -ne 1) { throw 'Running executable is not the registered application' }
@{ identity=$pkg.Name; signer=$pkg.Publisher; appPath=$pkg.InstallLocation; nativeVersion=$pkg.Version.ToString(); executable=$p.executable; applicationId=$apps[0].Id; packageFullName=$pkg.PackageFullName; packageFamilyName=$pkg.PackageFamilyName; removalRoots=@($pkg.InstallLocation,(Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('Packages\'+$pkg.PackageFamilyName))) } | ConvertTo-Json -Compress
`, JSON.stringify({ identity: build.identity.msixAppIdWithOrg, executable }))
  const installed: InstalledApp = JSON.parse(result.stdout)
  if (installed.nativeVersion !== build.windowsVersion || installed.identity !== build.identity.msixAppIdWithOrg || !installed.signer) {
    throw new Error('Running package does not match its baked channel version')
  }
  return installed
}

export async function discoverRetirementSource(build: ChannelBuild, home: string, userData: string, profile: string): Promise<RetirementSource> {
  if ((process.platform !== 'darwin' && process.platform !== 'win32') || (process.arch !== 'x64' && process.arch !== 'arm64')) {
    throw new Error('Native retirement is supported only on bundled macOS and Windows')
  }
  const installed: InstalledApp = await inspectRunningChannelApp(build)
  return { ...installed, platform: process.platform, architecture: process.arch, home: await canonicalRetirementPath(home),
    userData: await canonicalRetirementPath(userData), profile, connectionId: 'local' }
}

export async function discoverRetirementDestination(target: ChannelTarget, source: RetirementSource): Promise<RetirementDestination> {
  const pkg = target.package
  const artifact: RetirementDestination['artifact'] = {
    url: target.artifactUrl, sha256: pkg.artifact.sha256, size: pkg.artifact.size,
    format: pkg.platform === 'darwin' ? 'zip' : pkg.artifact.key.endsWith('.msixbundle') ? 'msixbundle' : 'msix'
  }
  const common = { platform: pkg.platform, architecture: pkg.arch, identity: pkg.identity,
    nativeVersion: pkg.version, signer: source.signer, artifact, commit: target.manifest.request.commit }
  if (pkg.platform === 'darwin') {
    // The established sibling Applications location is writable by the same owner.
    const appPath: string = path.join(path.dirname(source.appPath), `${target.manifest.request.identity.appNamePascal}.app`)
    return { ...common, appPath, applicationId: null, packageFamilyName: null }
  }
  if (!source.packageFullName) { throw new Error('Missing running package identity') }
  const publisherId: string = source.packageFullName.split('_').at(-1) || ''
  if (!/^[a-z0-9]{13}$/.test(publisherId)) { throw new Error('Invalid native publisher ID') }
  const packageFamilyName: string = `${pkg.identity}_${publisherId}`
  // Add-AppxPackage installs current-user sideloads beside the registered source.
  const appPath: string = path.join(path.dirname(source.appPath), `${pkg.identity}_${pkg.version}_${pkg.arch}__${publisherId}`)
  const applicationId: string = source.applicationId || ''
  if (!applicationId) { throw new Error('Missing native application ID') }
  return { ...common, appPath, packageFamilyName, applicationId }
}

export function assertPersistentRetirementHome(source: RetirementSource): void {
  for (const root of source.removalRoots) {
    if (insideRetirementRoot(root, source.home) || insideRetirementRoot(root, source.userData)) {
      throw new Error('Your workspace or desktop credentials are inside package-private storage. Move the workspace to a persistent folder and reopen the preview before migrating; the preview has not been removed.')
    }
  }
}
