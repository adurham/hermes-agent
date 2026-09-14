const { test } = require('node:test')
const assert = require('node:assert/strict')
const { createHash } = require('node:crypto')
const { validateJourney, verifyBytes, verifyManifest } = require('./channel-retirement-manifest.cjs')

function fixture() {
  function side(buildId, identity, sequence, releaseTag) {
    const request = { buildId, sequence, commit: 'a'.repeat(40), repository: 'example/hermes',
      publicBase: 'http://127.0.0.1:9876', version: releaseTag ? '1.0.0' : `0.0.${sequence}`,
      releaseTag, identity: { appId: identity } }
    const manifest = { request, packages: [{ platform: 'darwin', arch: 'arm64', variant: 'bundled',
      identity, version: request.version, artifact: { sha256: 'f'.repeat(64), size: 5 } }] }
    const body = Buffer.from(JSON.stringify(manifest))
    return { manifest, head: { buildId, sequence, manifestKey: `releases/channel-builds/${buildId}/build.json`,
      sha256: createHash('sha256').update(body).digest('hex') } }
  }
  return { schema: 1, platform: 'darwin', arch: 'arm64', repository: 'example/hermes', controllerCommit: 'b'.repeat(40),
    route: 'direct', stable: 'absent', A: side('a'.repeat(32), 'preview.app', 1), S: side('b'.repeat(32), 'stable.app', 2, 'v1.0.0') }
}

test('retirement validates cross-identity artifacts without relaxing ordinary update identity', () => {
  assert.equal(validateJourney(fixture(), 'darwin', 'arm64').S.package.identity, 'stable.app')
  const bad = fixture()
  bad.S.manifest.request.identity.appId = 'preview.app'
  bad.S.manifest.packages[0].identity = 'preview.app'
  assert.throws(() => validateJourney(bad, 'darwin', 'arm64'), /distinct/)
  assert.throws(() => validateJourney(fixture(), 'win32', 'arm64'), /platform/)
  const wrongHead = fixture()
  wrongHead.A.head.buildId = 'e'.repeat(32)
  assert.throws(() => validateJourney(wrongHead, 'darwin', 'arm64'), /binding/)
})

test('archive and manifest checks bind exact bytes, not artifact filenames', () => {
  const bytes = Buffer.from('native package fixture')
  const expected = { size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') }
  verifyBytes(bytes, expected)
  assert.throws(() => verifyBytes(Buffer.concat([bytes, Buffer.from('!')]), expected), /hash/)
  const side = fixture().A
  verifyManifest(Buffer.from(JSON.stringify(side.manifest)), side)
  assert.throws(() => verifyManifest(Buffer.from('{}'), side), /manifest/)
})
