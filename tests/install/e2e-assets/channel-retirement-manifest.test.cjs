const { test } = require('node:test')
const assert = require('node:assert/strict')
const { createHash, createHmac } = require('node:crypto')
const http = require('node:http')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { validateJourney, verifyBytes, verifyManifest } = require('./channel-retirement-manifest.cjs')

const digest = bytes => createHash('sha256').update(bytes).digest('hex')
// Protocol fixtures only. These bytes are never installed or called native proof.
function fixture(base = 'https://archive.example.test/disposable') {
  const objects = new Map()
  function put(key, value) {
    const bytes = Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value))
    objects.set('/disposable/' + key, bytes)
    return { key, sha256: digest(bytes), size: bytes.length }
  }
  function side(id, nativeIdentity, sequence, version, channel, releaseTag) {
    const buildId = id.repeat(32)
    const identity = { appId: nativeIdentity, appNamePascal: nativeIdentity === 'preview.app' ? 'Preview' : 'Stable' }
    const request = { schema: 1, buildId, sequence, commit: id.repeat(40), repository: 'example/hermes', channel,
      publicBase: base, version, sourceVersion: version, identity }
    if (releaseTag) request.releaseTag = releaseTag
    const prefix = `releases/channel-builds/${buildId}/`
    const artifact = put(prefix + 'app.zip', Buffer.from(`not a native package: ${id}`))
    const manifest = { schema: 1, request, packages: [{ platform: 'darwin', arch: 'arm64', variant: 'bundled',
      identity: nativeIdentity, version, teamId: 'ABCDEFGHIJ', artifact, feed: { key: prefix + 'stable-mac.yml', channel: 'stable' } }] }
    if (releaseTag) manifest.receiverProtocol = 1
    const ref = put(prefix + 'build.json', manifest)
    return { manifest, head: { buildId, sequence, manifestKey: ref.key, sha256: ref.sha256 } }
  }
  const journey = { schema: 1, platform: 'darwin', arch: 'arm64', repository: 'example/hermes', controllerCommit: 'f'.repeat(40),
    route: 'via-update', stable: 'running', home: 'shared', minimumVersion: '1.0.0',
    A: side('a', 'preview.app', 1, '0.0.1', 'test-preview'), B: side('b', 'preview.app', 2, '0.0.2', 'test-preview'),
    S: side('c', 'stable.app', 1, '1.0.0', 'stable', 'v1.0.0'), T: side('d', 'stable.app', 2, '1.1.0', 'stable', 'v1.1.0') }
  return { journey, objects }
}

test('lifecycle inputs bind distinct identities, native ordering and receiver support', () => {
  const { journey } = fixture()
  const validated = validateJourney(journey, 'darwin', 'arm64')
  assert.equal(validated.S.package.identity, 'stable.app')
  assert.equal(journey.S.package, undefined, 'validation must not mutate the signed input')
  const direct = structuredClone(journey)
  direct.route = 'direct'; delete direct.B
  validateJourney(direct, 'darwin', 'arm64')
  const cases = [
    [j => { delete j.T }, /binding/],
    [j => { delete j.S.manifest.receiverProtocol }, /receiver/],
    [j => { j.T.manifest.request.version = j.S.manifest.request.version; j.T.manifest.request.releaseTag = 'v1.0.0'; j.T.manifest.packages[0].version = j.S.manifest.request.version }, /native version/],
    [j => { j.B.manifest.request.identity.extra = 'different' }, /identity/],
    [j => { j.S.head.buildId = 'e'.repeat(32) }, /binding/],
    [j => { j.S.manifest.request.publicBase = 'https://another.example' }, /authority/],
    [j => { j.S.manifest.packages[0].artifact.key = 'releases/../foreign.zip' }, /object key/],
    [j => { j.S.manifest.packages[0].teamId = '0123456789' }, /signing/],
    [j => { j.home = 'invented' }, /scenario/],
  ]
  for (const [mutate, error] of cases) {
    const bad = structuredClone(journey); mutate(bad)
    assert.throws(() => validateJourney(bad, 'darwin', 'arm64'), error)
  }
  assert.throws(() => validateJourney(journey, 'win32', 'arm64'), /platform/)
  // Platform is manifest data here, not a mocked native host.
  const windows = structuredClone(journey)
  windows.platform = 'win32'
  for (const name of ['A', 'B', 'S', 'T']) {
    const side = windows[name]
    const request = side.manifest.request
    const pkg = side.manifest.packages[0]
    request.identity.msixAppIdWithOrg = pkg.identity
    request.windowsVersion = request.releaseTag ? `${request.version}.0` : `0.0.${request.sequence}.0`
    pkg.version = request.windowsVersion
    pkg.platform = 'win32'; pkg.publisher = 'CN=Test fixture'
    pkg.artifact.key = pkg.artifact.key.replace('.zip', '.msixbundle')
    pkg.feed.key = pkg.feed.key.replace('stable-mac.yml', 'update.appinstaller')
  }
  validateJourney(windows, 'win32', 'arm64')
  windows.T.manifest.packages[0].publisher = 'CN=Foreign fixture'
  assert.throws(() => validateJourney(windows, 'win32', 'arm64'), /signing/)
  const body = Buffer.from(JSON.stringify(journey.A.manifest))
  verifyManifest(body, journey.A)
  verifyBytes(body, { size: body.length, sha256: digest(body) })
  assert.throws(() => verifyManifest(Buffer.from('{}'), journey.A), /manifest/)
  assert.throws(() => verifyBytes(body, { size: body.length + 1, sha256: digest(body) }), /hash/)
})

test('staging uses real HTTP bytes, refuses tampering and never emits native receipts', async () => {
  const { stageJourney } = await import('./channel-retirement-inputs.mjs')
  let objects = new Map()
  const server = http.createServer((req, res) => {
    const bytes = objects.get(req.url)
    res.writeHead(bytes ? 200 : 404); res.end(bytes || 'missing')
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'retirement-inputs-'))
  try {
    const fixtureData = fixture(`http://127.0.0.1:${server.address().port}/disposable`)
    objects = fixtureData.objects
    const bytes = Buffer.from(JSON.stringify(fixtureData.journey))
    const manifest = path.join(root, 'input.json'); await fs.writeFile(manifest, bytes)
    const options = { manifest, manifestSha256: digest(bytes), platform: 'darwin', arch: 'arm64', out: path.join(root, 'staged') }
    const staged = JSON.parse(await fs.readFile(await stageJourney(options)))
    for (const name of ['A', 'B', 'S', 'T']) {
      const downloaded = await fs.readFile(staged[name].artifactPath)
      assert.equal(digest(downloaded), staged[name].package.artifact.sha256)
    }
    await assert.rejects(fs.access(path.join(options.out, 'receipt.json')), /ENOENT/)
    await assert.rejects(stageJourney(options), /EEXIST/)
    await assert.rejects(stageJourney({ ...options, manifestSha256: '0'.repeat(64) }), /Journey SHA256/)
    const key = '/disposable/' + fixtureData.journey.S.head.manifestKey
    objects.set(key, Buffer.from('{}'))
    await assert.rejects(stageJourney({ ...options, out: path.join(root, 'tampered') }), /SHA256 mismatch/)
    await assert.rejects(fs.access(path.join(root, 'tampered', 'journey-inputs.json')), /ENOENT/)
  } finally {
    await new Promise(resolve => server.close(resolve))
    await fs.rm(root, { recursive: true, force: true })
  }
})

test('journal observations require exact pinned receiver and authenticated readiness', async () => {
  const { completedJournal } = await import('./channel-retirement-macos.mjs')
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'retirement-journal-'))
  const id = 'a'.repeat(32), token = 'b'.repeat(64), sha256 = 'c'.repeat(64)
  const dir = path.join(root, id), file = path.join(dir, 'journal.json')
  const request = { id, token, source: { appPath: '/preview', removalRoots: ['/preview'] },
    destination: { appPath: '/stable', commit: 'd'.repeat(40) }, destinationManifestSha256: sha256 }
  const requestDigest = digest(JSON.stringify(request))
  const ready = { id, requestDigest, pid: 123, executable: '/stable/bin', home: '/home/fixture',
    commit: request.destination.commit, connectionId: 'local', profile: 'default' }
  const record = { request, requestDigest, stage: 'complete', state: { selectedHome: ready.home },
    ready: { ...ready, mac: createHmac('sha256', Buffer.from(token, 'hex')).update(JSON.stringify(ready)).digest('hex') } }
  try {
    await fs.mkdir(dir)
    await fs.mkdir(path.join(root, 'e'.repeat(32)))
    await fs.writeFile(file, JSON.stringify(record))
    assert.equal(completedJournal(root, '/preview', '/stable', sha256).pid, 123)
    assert.throws(() => completedJournal(root, '/preview', '/stable', '0'.repeat(64)), /Assertion/)
    record.ready.pid++
    await fs.writeFile(file, JSON.stringify(record))
    assert.throws(() => completedJournal(root, '/preview', '/stable', sha256), /Assertion/)
  } finally { await fs.rm(root, { recursive: true, force: true }) }
})
