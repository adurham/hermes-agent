import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import type { ChannelTarget } from './channel'
import type { ChannelIdentity, ChannelPackage, ChannelRequest } from './channel-protocol'
import { discoverRetirementDestination, runRetirementPowerShell } from './retirement-discovery'
import type { RetirementSource } from './retirement-state'

function retirementTarget(platform: ChannelPackage['platform']): ChannelTarget {
  const identity: ChannelIdentity = { token: 'a'.repeat(16), displayName: 'Hermes Agent', appId: 'com.nousresearch.hermes-bundled',
    appNamePascal: 'HermesBundled', artifactNamePascal: 'HermesBundled', cliName: 'hermes', windowsExecutableName: 'Hermes Agent', msixAppIdWithOrg: 'NousResearch.HermesBundled' }
  const request: ChannelRequest = { schema: 1, buildId: 'b'.repeat(32), channel: 'stable', sequence: 2,
    repository: 'NousResearch/hermes-agent', commit: 'c'.repeat(40), sourceVersion: '1.2.3', version: '1.2.3', windowsVersion: '1.2.3.0',
    publicBase: 'https://example.com', identity, bundleEnv: {} }
  const pkg: ChannelPackage = { platform, arch: 'x64', variant: 'bundled', version: platform === 'darwin' ? request.version : request.windowsVersion,
    identity: platform === 'darwin' ? identity.appId : identity.msixAppIdWithOrg,
    artifact: { key: 'releases/fixture/stable.msixbundle', sha256: 'd'.repeat(64), size: 1 }, feed: { key: 'releases/fixture/stable.appinstaller', channel: 'stable' } }
  return { channel: { schema: 1, state: 'active', name: 'stable', policy: 'stable-release', repository: request.repository,
    identity, revision: 2, nextSequence: 3, head: null }, manifest: { schema: 1, request, packages: [pkg] }, package: pkg,
    manifestSha256: 'e'.repeat(64), artifactUrl: 'https://example.com/releases/fixture/stable.msixbundle', feedUrl: 'https://example.com/releases/fixture/stable.appinstaller' }
}

function retirementSource(directory: string, platform: ChannelPackage['platform']): RetirementSource {
  return { platform, architecture: 'x64', identity: 'NousResearch.Preview', signer: 'TESTTEAM12', nativeVersion: '1.0.0.0',
    appPath: path.join(directory, 'Preview.app'), executable: path.join(directory, 'Preview.app/preview'), applicationId: 'PreviewApplication',
    packageFullName: 'NousResearch.Preview_1.0.0.0_x64__abcdefghijklm', home: path.join(directory, 'home'), userData: path.join(directory, 'desktop'),
    profile: 'default', connectionId: 'local', removalRoots: [path.join(directory, 'Preview.app')] }
}

test('Mac discovery reuses the uniquely owned bundle by identity and names a new install from product metadata', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-mac-discovery-'))
  const target: ChannelTarget = retirementTarget('darwin')
  const source: RetirementSource = retirementSource(directory, 'darwin')
  const renamed: string = path.join(directory, 'My stable application.app')
  const inspectMac = async (app: string): Promise<{ identity: string }> => JSON.parse(await readFile(path.join(app, 'identity.json'), 'utf8'))
  const discovery = { mac: { roots: [directory], registered: async (): Promise<string[]> => [], inspect: inspectMac } }
  try {
    expect((await discoverRetirementDestination(target, source, discovery)).appPath).toBe(path.join(directory, `${target.manifest.request.identity.displayName}.app`))
    await mkdir(path.join(renamed, 'Contents'), { recursive: true, mode: 0o700 })
    await writeFile(path.join(renamed, 'Contents/Info.plist'), '')
    await writeFile(path.join(renamed, 'identity.json'), JSON.stringify({ identity: target.package.identity }))
    expect((await discoverRetirementDestination(target, source, discovery)).appPath).toBe(renamed)
    const duplicate: string = path.join(directory, 'Other stable.app')
    await mkdir(path.join(duplicate, 'Contents'), { recursive: true, mode: 0o700 })
    await writeFile(path.join(duplicate, 'Contents/Info.plist'), '')
    await writeFile(path.join(duplicate, 'identity.json'), JSON.stringify({ identity: target.package.identity }))
    await expect(discoverRetirementDestination(target, source, discovery)).rejects.toThrow('Multiple')
    await rm(duplicate, { recursive: true })
    await expect(discoverRetirementDestination(target, source, { mac: {
      ...discovery.mac, inspect: async (): Promise<{ identity: string }> => { throw new Error('different installation steward') }
    } })).rejects.toThrow('steward')
  } finally { await rm(directory, { recursive: true, force: true }) }
})

// Native Windows lane: queries the actual AppX default volume; never installs a package.
test.runIf(process.platform === 'win32')('Windows discovery uses the OS package store, not the preview volume', async (): Promise<void> => {
  const target: ChannelTarget = retirementTarget('win32')
  target.package.identity = `NousResearch.RetirementProbe${Date.now()}`
  const source: RetirementSource = retirementSource(os.tmpdir(), 'win32')
  const result = await discoverRetirementDestination(target, source)
  const volume = await runRetirementPowerShell('(Get-AppxDefaultVolume).PackageStorePath | ConvertTo-Json -Compress')
  expect(path.dirname(result.appPath).toLowerCase()).toBe(String(JSON.parse(volume.stdout)).toLowerCase())
  expect(result.applicationId).toBe(target.manifest.request.identity.appNamePascal)
})

// Native macOS lane: parses a real plist and rejects an unsigned matching bundle.
test.runIf(process.platform === 'darwin')('Mac discovery refuses a matching bundle without native signing ownership', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-mac-native-'))
  try {
    const target: ChannelTarget = retirementTarget('darwin')
    const renamed: string = path.join(directory, 'Renamed stable.app')
    await mkdir(path.join(renamed, 'Contents'), { recursive: true, mode: 0o700 })
    await writeFile(path.join(renamed, 'Contents/Info.plist'), `<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleIdentifier</key><string>${target.package.identity}</string><key>CFBundleShortVersionString</key><string>1.2.3</string><key>CFBundleExecutable</key><string>Hermes</string></dict></plist>`)
    await expect(discoverRetirementDestination(target, retirementSource(directory, 'darwin'), {
      mac: { roots: [directory], registered: async (): Promise<string[]> => [] }
    })).rejects.toThrow('Native retirement operation failed')
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test('destination binds the target application ID and the native install location, not preview naming', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-discovery-'))
  try {
    const target: ChannelTarget = retirementTarget('win32')
    const source: RetirementSource = retirementSource(directory, 'win32')
    const installedPath: string = path.join(directory, 'different-package-volume', 'native-target')
    const result = await discoverRetirementDestination(target, source, {
      windows: async (): Promise<string> => installedPath
    })
    expect(result.applicationId).toBe(target.manifest.request.identity.appNamePascal)
    expect(result.applicationId).not.toBe(source.applicationId)
    expect(result.appPath).toBe(installedPath)
  } finally { await rm(directory, { recursive: true, force: true }) }
})
