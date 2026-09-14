import { createHash } from 'node:crypto'
import { createServer, type Server } from 'node:http'
import { afterEach, expect, test } from 'vitest'

import { ChannelResolver } from './channel'
import { ChannelStrategy } from './channel-strategy'
import feedContract from '../../update-feed.cjs'
import type { ChannelBuild, ChannelManifest, ChannelRecord, RetiredChannel } from './channel-protocol'

const servers: Server[] = []
afterEach(async (): Promise<void> => {
  await Promise.all(servers.splice(0).map((server: Server): Promise<void> => new Promise((resolve): void => {
    server.closeAllConnections()
    server.close((): void => resolve())
  })))
})

async function fixture(): Promise<{
  build: ChannelBuild; record: ChannelRecord; manifest: ChannelManifest;
  objects: Map<string, string>; requests: string[]; publish: () => void;
}> {
  const objects = new Map<string, string>()
  const requests: string[] = []
  const server = createServer((request, response): void => {
    requests.push(request.url!)
    const value = objects.get(request.url!)
    response.writeHead(value === undefined ? 404 : 200, { 'Content-Type': 'application/json' })
    response.end(value ?? 'missing')
  })
  servers.push(server)
  await new Promise<void>((resolve): void => { server.listen(0, '127.0.0.1', resolve) })
  const address = server.address()
  // oxlint-disable-next-line anti-slop/no-runtime-typeof -- Node's listen() address boundary admits pipes or TCP; this fixture requires TCP.
  if (!address || typeof address === 'string') { throw new Error('No fixture port') }
  const build: ChannelBuild = {
    buildId: 'a'.repeat(32), channel: 'fresh-preview-29', sequence: 1,
    repository: 'example/hermes-agent', commit: 'b'.repeat(40), sourceVersion: '1.2.3',
    version: '0.0.1', windowsVersion: '0.0.1.0', publicBase: `http://127.0.0.1:${address.port}`,
    bundleEnv: {}, identity: {
      token: '1234567890abcdef', displayName: 'Hermes fresh-preview-29',
      appId: 'chat.nous.hermes.h1234567890abcdef', appNamePascal: 'HermesH1234567890abcdef',
      artifactNamePascal: 'HermesH1234567890abcdef', cliName: 'hermes-fresh-preview-29',
      windowsExecutableName: 'HermesH1234567890abcdef.exe', msixAppIdWithOrg: 'NousResearch.HermesH1234567890abcdef'
    }
  }
  const next = { ...build, buildId: 'c'.repeat(32), sequence: 2, version: '0.0.2', windowsVersion: '0.0.2.0' }
  const prefix = `releases/channel-builds/${next.buildId}/`
  const manifest: ChannelManifest = {
    schema: 1, request: { schema: 1, ...next }, packages: [{
      platform: 'darwin', arch: 'arm64', variant: 'bundled', version: next.version,
      identity: build.identity.appId, teamId: 'ABCDE12345',
      artifact: { key: `${prefix}darwin/Hermes.zip`, sha256: 'd'.repeat(64), size: 100 },
      feed: { key: `${prefix}darwin/stable-mac.yml`, channel: 'stable' }
    }]
  }
  const record: ChannelRecord = {
    schema: 1, name: build.channel, repository: build.repository, policy: 'preview', state: 'active',
    revision: 2, nextSequence: 3, identity: build.identity,
    head: { buildId: next.buildId, sequence: 2, manifestKey: `${prefix}build.json`, sha256: '' }
  }
  const publish = (): void => {
    const body = JSON.stringify(manifest)
    if (record.state !== 'active' || !record.head) { throw new Error('Fixture requires active head') }
    record.head.sha256 = createHash('sha256').update(body).digest('hex')
    objects.set(`/${record.head.manifestKey}`, body)
    objects.set(`/releases/channels/${record.name}.json`, JSON.stringify(record))
  }
  publish()
  return { build, record, manifest, objects, requests, publish }
}

async function retiredFixture(): Promise<Awaited<ReturnType<typeof fixture>> & { retired: RetiredChannel }> {
  const f = await fixture()
  const retired: RetiredChannel = {
    ...f.record, head: structuredClone(f.record.head), state: 'retired', destination: 'stable', minimumVersion: '1.0.0',
    compatibilityKey: 'releases/retirements/receipt.json', compatibilitySha256: '', lastHead: structuredClone(f.record.head)
  }
  f.record.name = 'stable'
  f.record.policy = 'stable-release'
  f.record.identity = { ...f.build.identity, appId: 'chat.nous.hermes', token: 'fedcba0987654321' }
  f.manifest.request = { ...f.manifest.request, channel: 'stable', identity: f.record.identity, version: '1.2.3', windowsVersion: '1.2.3.0', releaseTag: 'v1.2.3' }
  f.manifest.packages[0].identity = f.record.identity.appId
  f.manifest.packages[0].version = '1.2.3'
  f.publish()
  const qualification = JSON.stringify({
    schema: 1, source: f.build.channel, destination: 'stable',
    sourceHead: retired.lastHead, destinationHead: f.record.head, minimumVersion: retired.minimumVersion,
    receiverProtocol: 1, coverage: [{ platform: 'darwin', arch: 'arm64',
      sourceIdentity: f.build.identity.appId, destinationIdentity: f.record.identity.appId,
      cohorts: [{ buildId: f.build.buildId, commit: f.build.commit }]
    }, { platform: 'win32', arch: 'x64',
      sourceIdentity: f.build.identity.msixAppIdWithOrg, destinationIdentity: f.record.identity.msixAppIdWithOrg,
      cohorts: [{ buildId: f.build.buildId, commit: f.build.commit }]
    }]
  })
  retired.compatibilitySha256 = createHash('sha256').update(qualification).digest('hex')
  f.objects.set(`/${retired.compatibilityKey}`, qualification)
  f.objects.set(`/releases/channels/${retired.name}.json`, JSON.stringify(retired))
  return { ...f, retired }
}

test('offers only a digest-bound qualified retirement to the official destination', async (): Promise<void> => {
  const { build } = await retiredFixture()
  const result = await new ChannelResolver({ build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }).resolve()
  expect(result.kind).toBe('retirement')
  if (result.kind !== 'retirement') { throw new Error('Expected retirement') }
  expect(result.retirement.target.channel.name).toBe('stable')
  expect(result.retirement.qualifications[0].coverage[0].cohorts).toEqual([{ buildId: build.buildId, commit: build.commit }])
})

test('pins the checked target during apply even after R2 advances, and never auto-downloads', async (): Promise<void> => {
  const f = await fixture()
  const calls: string[] = []
  let enteredResolve: () => void = (): void => {}
  let releaseResolve: () => void = (): void => {}
  const entered = new Promise<void>((resolve): void => { enteredResolve = resolve })
  const release = new Promise<void>((resolve): void => { releaseResolve = resolve })
  const strategy = new ChannelStrategy({
    resolver: new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }),
    build: f.build, mechanism: 'electron-updater',
    nativeFactory: (target) => ({
      mechanism: 'electron-updater',
      check: async () => { calls.push('check'); return { supported: true, updateAvailable: true } },
      apply: async () => {
        calls.push(target.manifest.request.buildId)
        enteredResolve()
        await release
        return { ok: true }
      }
    })
  })
  expect((await strategy.check()).updateAvailable).toBe(true)
  expect(calls).toEqual(['check'])
  const pending = strategy.apply()
  await entered
  f.objects.clear()
  await expect(strategy.check()).rejects.toThrow('in progress')
  await expect(strategy.apply()).rejects.toThrow('in progress')
  releaseResolve()
  expect((await pending).ok).toBe(true)
  expect(calls).toEqual(['check', 'c'.repeat(32)])
  expect(f.requests).toHaveLength(2)
})

test('retirement has a separate consented path and never invokes the ordinary native updater', async (): Promise<void> => {
  const f = await retiredFixture()
  let applied = false
  const strategy = new ChannelStrategy({
    resolver: new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }),
    build: f.build, mechanism: 'electron-updater',
    nativeFactory: (): never => { throw new Error('Not a same-identity update') },
    retirement: {
      check: async () => ({ state: 'available' }),
      apply: async () => { applied = true; return { ok: true, handedOff: true } }
    }
  })
  const status = await strategy.check()
  expect(status.updateAvailable).toBeUndefined()
  expect(status.retirement).toMatchObject({ state: 'available', destination: 'stable' })
  expect(applied).toBe(false)
  expect(await strategy.apply()).toMatchObject({ ok: false })
  expect(applied).toBe(false)
  expect(await strategy.applyRetirement()).toMatchObject({ ok: true })
  expect(applied).toBe(true)
})

test.each(['hash', 'identity', 'repository', 'signer', 'escape', 'schema', 'version'] as const)('rejects untrusted %s without falling back', async (fault): Promise<void> => {
  const f = await fixture()
  if (fault === 'identity') { f.manifest.request.identity.appId = 'chat.other.identity' }
  if (fault === 'repository') { f.manifest.request.repository = 'other/hermes-agent' }
  if (fault === 'signer') { f.manifest.packages[0].teamId = 'ZZZZZ99999' }
  if (fault === 'escape') { f.manifest.packages[0].feed.key = 'releases/other/stable-mac.yml' }
  if (fault === 'version') { f.manifest.request.version = '1.0.0' }
  f.publish()
  if (fault === 'hash') { f.objects.set(`/${f.record.head!.manifestKey}`, '{}') }
  if (fault === 'schema') { f.objects.set(`/releases/channels/${f.record.name}.json`, '{"schema":2}') }
  await expect(new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }).resolve()).rejects.toThrow()
})

test.each(['floor', 'cycle', 'coverage', 'digest', 'missing', 'unprotected'] as const)('blocks retirement with invalid %s', async (fault): Promise<void> => {
  const f = await retiredFixture()
  if (fault === 'unprotected') {
    f.record.policy = 'preview'
    f.manifest.request.version = '0.0.2'
    f.manifest.request.windowsVersion = '0.0.2.0'
    f.manifest.packages[0].version = '0.0.2'
    f.publish()
  }
  if (fault === 'floor') { f.retired.minimumVersion = '2.0.0' }
  if (fault === 'cycle') { f.retired.destination = f.build.channel }
  if (fault === 'coverage') { f.build.buildId = 'f'.repeat(32) }
  if (fault === 'digest') { f.retired.compatibilitySha256 = 'f'.repeat(64) }
  if (fault === 'missing') { f.objects.delete(`/${f.retired.compatibilityKey}`) }
  f.objects.set(`/releases/channels/${f.retired.name}.json`, JSON.stringify(f.retired))
  await expect(new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }).resolve()).rejects.toThrow()
})

test('missing and oversized metadata remain errors while unpublished channels are explicitly empty', async (): Promise<void> => {
  const f = await fixture()
  const resolver = new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' })
  f.objects.clear()
  await expect(resolver.resolve()).rejects.toThrow('404')
  f.objects.set(`/releases/channels/${f.build.channel}.json`, ' '.repeat(4 * 1024 * 1024 + 1))
  await expect(resolver.resolve()).rejects.toThrow('size limit')
  f.record.head = null
  f.objects.set(`/releases/channels/${f.build.channel}.json`, JSON.stringify(f.record))
  expect((await resolver.resolve()).kind).toBe('empty')
})

test.each(['UPPER', 'a--b', '../a', 'a%2fb', 'con', 'a\n', 'a'.repeat(33)])('refuses invalid channel slug %j without normalization', (name: string): void => {
  expect((): void => { feedContract.darwinFeed(name) }).toThrow()
})

test('refuses public HTTP archive origins before network access', async (): Promise<void> => {
  const f = await fixture()
  f.build.publicBase = 'http://example.com'
  expect((): void => { new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }) }).toThrow('HTTPS')
})

test('Windows resolves its numeric native version, publisher and immutable descriptor without Python', async (): Promise<void> => {
  const f = await fixture()
  f.manifest.packages = [{
    platform: 'win32', arch: 'x64', variant: 'bundled', version: '0.0.2.0',
    identity: f.build.identity.msixAppIdWithOrg, publisher: 'CN=Nous Research',
    artifact: { key: `releases/channel-builds/${f.manifest.request.buildId}/win32/Hermes.msixbundle`, sha256: 'd'.repeat(64), size: 100 },
    feed: { key: `releases/channel-builds/${f.manifest.request.buildId}/win32/stable.appinstaller`, channel: 'stable' }
  }]
  f.publish()
  const result = await new ChannelResolver({ build: f.build, platform: 'win32', arch: 'x64', signer: 'CN=Nous Research' }).resolve()
  expect(result.kind).toBe('active')
  if (result.kind !== 'active') { throw new Error('Expected active') }
  expect(result.target.package.version).toBe('0.0.2.0')
  expect(result.target.feedUrl).toContain('/win32/stable.appinstaller')
})

test('failed checks clear a previously checked selection, never authorizing a stale apply', async (): Promise<void> => {
  const f = await fixture()
  let applied = false
  const strategy = new ChannelStrategy({
    resolver: new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }),
    build: f.build, mechanism: 'electron-updater',
    nativeFactory: () => ({ mechanism: 'electron-updater',
      check: async () => ({ supported: true, updateAvailable: true }),
      apply: async () => { applied = true; return { ok: true } }
    })
  })
  await strategy.check()
  f.objects.clear()
  await expect(strategy.check()).rejects.toThrow('404')
  await expect(strategy.apply()).rejects.toThrow('404')
  expect(applied).toBe(false)
})

test('long-offline previews retain the qualified migration target after stable advances', async (): Promise<void> => {
  const f = await retiredFixture()
  const qualifiedBuild = f.manifest.request.buildId
  f.manifest.request = { ...f.manifest.request, buildId: 'e'.repeat(32), sequence: 3, version: '1.3.0', windowsVersion: '1.3.0.0', sourceVersion: '1.3.0', releaseTag: 'v1.3.0' }
  f.record.head = { buildId: f.manifest.request.buildId, sequence: 3, manifestKey: `releases/channel-builds/${f.manifest.request.buildId}/build.json`, sha256: '' }
  f.record.nextSequence = 4
  f.manifest.packages[0] = { ...f.manifest.packages[0], version: '1.3.0',
    artifact: { ...f.manifest.packages[0].artifact, key: `releases/channel-builds/${f.manifest.request.buildId}/darwin/Hermes.zip` },
    feed: { key: `releases/channel-builds/${f.manifest.request.buildId}/darwin/stable-mac.yml`, channel: 'stable' }
  }
  f.publish()
  const result = await new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }).resolve()
  expect(result.kind).toBe('retirement')
  if (result.kind !== 'retirement') { throw new Error('Expected retirement') }
  expect(result.retirement.target.manifest.request.buildId).toBe(qualifiedBuild)
  expect(result.retirement.target.manifest.request.channel).toBe('stable')
})

test('rejects duplicate JSON members rather than disagreeing with the publisher decoder', async (): Promise<void> => {
  const f = await fixture()
  const body = f.objects.get(`/releases/channels/${f.build.channel}.json`)!
  f.objects.set(`/releases/channels/${f.build.channel}.json`, body.replace('"schema":1', '"schema":2,"schema":1'))
  await expect(new ChannelResolver({ build: f.build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }).resolve()).rejects.toThrow('Duplicate')
})

test('resolves an arbitrary R2 name through a real HTTP channel and digest-bound immutable build', async (): Promise<void> => {
  const { build, requests } = await fixture()
  const result = await new ChannelResolver({ build, platform: 'darwin', arch: 'arm64', signer: 'ABCDE12345' }).resolve()
  expect(result.kind).toBe('active')
  if (result.kind !== 'active') { throw new Error('Expected active build') }
  expect(result.target.manifest.request.sequence).toBe(2)
  expect(result.target.feedUrl).toBe(`${build.publicBase}/releases/channel-builds/${'c'.repeat(32)}/darwin/stable-mac.yml`)
  expect(requests).toEqual([`/releases/channels/${build.channel}.json`, `/releases/channel-builds/${'c'.repeat(32)}/build.json`])
})
