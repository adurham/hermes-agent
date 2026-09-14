import { createHash } from 'node:crypto'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { WindowsRetirementAdapter } from './retirement-windows'
import type { NativeCommandResult } from './retirement-native'
import { RetirementJournal, type RetirementRequest } from './retirement-state'
import { runRetirementPowerShell } from './retirement-discovery'

function windowsRequest(): RetirementRequest {
  const native = { platform: 'win32' as const, architecture: 'x64' as const, signer: 'CN=Fixture', nativeVersion: '1.2.3.0', applicationId: 'HermesBundled' }
  return { protocol: 1, id: 'a'.repeat(32), token: 'b'.repeat(64),
    source: { ...native, identity: 'NousResearch.Preview', appPath: 'C:\\Apps\\Preview', executable: 'C:\\Apps\\Preview\\preview.exe',
      packageFullName: 'NousResearch.Preview_1.0.0.0_x64__abcdefghijklm', home: 'C:\\home', userData: 'C:\\desktop', profile: 'default', connectionId: 'local', removalRoots: ['C:\\Apps\\Preview'] },
    destination: { ...native, identity: 'NousResearch.HermesBundled', appPath: 'C:\\Apps\\Stable', packageFamilyName: 'NousResearch.HermesBundled_abcdefghijklm', commit: 'c'.repeat(40),
      artifact: { url: 'https://example.com/releases/tag/v1.2.3/stable.msixbundle', sha256: 'd'.repeat(64), size: 1, format: 'msixbundle' } },
    destinationManifestSha256: 'e'.repeat(64),
    sourceBuild: { buildId: '1'.repeat(32), channel: 'preview', sequence: 1, repository: 'NousResearch/hermes-agent', commit: 'f'.repeat(40), version: '0.0.1', sourceVersion: '1.2.2', windowsVersion: '0.0.1.0', publicBase: 'https://example.com', bundleEnv: {},
      identity: { token: '2'.repeat(16), displayName: 'Preview', appId: 'com.nousresearch.preview', appNamePascal: 'Preview', artifactNamePascal: 'Preview', cliName: 'preview', windowsExecutableName: 'preview', msixAppIdWithOrg: 'NousResearch.Preview' } },
    selection: { home: 'C:\\home', profile: 'default', connectionId: 'local', choice: 'open-preview', reauthenticate: false },
    consent: { install: true, removePreview: true, replaceExistingStable: false } }
}

test('pinned installation precedes metadata-only updater registration and exact read-back', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-windows-install-'))
  try {
    const request: RetirementRequest = windowsRequest()
    const journal: RetirementJournal = await RetirementJournal.open(directory, request.id)
    const bytes: Buffer = Buffer.from('native signature verification belongs to the command boundary')
    request.destination.artifact.size = bytes.length
    request.destination.artifact.sha256 = createHash('sha256').update(bytes).digest('hex')
    const artifact: string = path.join(journal.directory, 'destination.msixbundle')
    await writeFile(artifact, bytes)
    const operations: string[] = []
    const command = async (_script: string, input: string): Promise<NativeCommandResult> => {
      const payload: { operation: string; artifact: string | null } = JSON.parse(input)
      operations.push(payload.operation)
      expect(payload.artifact).toBe(payload.operation === 'install' ? artifact : null)
      return { stdout: '{}', stderr: '' }
    }
    await new WindowsRetirementAdapter(command).install(request, journal)
    expect(operations).toEqual(['install', 'register-updater', 'verify'])
    operations.length = 0
    const unavailable = async (script: string, input: string): Promise<NativeCommandResult> => {
      const payload: { operation: string } = JSON.parse(input)
      if (payload.operation === 'register-updater') { throw new Error('native registration failed') }
      return command(script, input)
    }
    await expect(new WindowsRetirementAdapter(unavailable).install(request, journal)).rejects.toThrow('registration failed')
    expect(operations).toEqual(['install'])
    operations.length = 0
    await writeFile(artifact, Buffer.alloc(bytes.length))
    await expect(new WindowsRetirementAdapter(command).install(request, journal)).rejects.toThrow('digest')
    expect(operations).toEqual([])
  } finally { await rm(directory, { recursive: true, force: true }) }
})

// Native Windows lane: require the real source-registration API; no package writes.
test.runIf(process.platform === 'win32')('Windows exposes metadata-only update registration and current-user WinRT queries', async (): Promise<void> => {
  const result: NativeCommandResult = await runRetirementPowerShell(String.raw`
$ErrorActionPreference='Stop'
Get-Command Set-AppxPackageAutoUpdateSettings -ErrorAction Stop | Out-Null
$manager=[Windows.Management.Deployment.PackageManager,Windows.Management.Deployment,ContentType=WindowsRuntime]::new()
$packages=@(Get-AppxPackage | Where-Object { $_.Status.ToString() -eq 'Ok' })
if (-not $packages.Count) { throw 'Native runner has no current-user packages to query' }
$native=$manager.FindPackageForUser('', $packages[0].PackageFullName)
$source=$native.GetAppInstallerInfo()
@{ found=($native.Id.FullName -ceq $packages[0].PackageFullName) } | ConvertTo-Json -Compress
`)
  expect(JSON.parse(result.stdout)).toEqual({ found: true })
  const request: RetirementRequest = windowsRequest()
  request.destination.identity = `NousResearch.RetirementProbe${Date.now()}`
  await expect(new WindowsRetirementAdapter().verifyDestination(request)).rejects.toThrow('Expected exactly one current-user package')
})

test('native verification binds the subsequent stable updater to the same archive authority', async (): Promise<void> => {
  const request: RetirementRequest = windowsRequest()
  let payload: { appInstallerUri: string } | undefined
  const command = async (_script: string, input: string): Promise<NativeCommandResult> => {
    payload = JSON.parse(input)
    return { stdout: '{}', stderr: '' }
  }
  await new WindowsRetirementAdapter(command).verifyDestination(request)
  expect(payload?.appInstallerUri).toBe('https://example.com/releases/win32/stable/stable.appinstaller')
  request.destination.artifact.url = 'https://other.example/releases/tag/v1.2.3/stable.msixbundle'
  await expect(new WindowsRetirementAdapter(command).verifyDestination(request)).rejects.toThrow('authority')
})
