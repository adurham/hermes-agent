import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'

import { expect, it, describe } from 'vitest'

const require = createRequire(import.meta.url)
const {
  validateBundleManifest,
  codesignTeam,
  stampAssertions,
} = require('../tests/install/e2e-assets/mac-bundled-manifest.cjs')

const side = (over = {}) => ({
  tag: 'v0.28.0',
  version: '0.28.0',
  commit: 'a'.repeat(40),
  identity: 'TEAM1234',
  artifact: { url: 'https://example.com/HermesBundled-0.28.0-mac-arm64.zip', sha256: 'b'.repeat(64), path: '/tmp/old.zip' },
  ...over,
})
const manifest = (over = {}) => ({
  schema: 1,
  platform: 'macos',
  arch: 'arm64',
  old: side(),
  new: side({
    tag: 'v0.29.0',
    version: '0.29.0',
    commit: 'c'.repeat(40),
    artifact: { url: 'https://example.com/HermesBundled-0.29.0-mac-arm64.zip', sha256: 'd'.repeat(64), path: '/tmp/new.zip' },
  }),
  ...over,
})
const want = { platform: 'macos', arch: 'arm64' }

describe('validateBundleManifest', () => {
  it('accepts a well-formed schema-1 pair', () => {
    expect(() => validateBundleManifest(manifest(), want)).not.toThrow()
  })

  it('rejects wrong schema, platform and arch', () => {
    expect(() => validateBundleManifest(manifest({ schema: 2 }), want)).toThrow(/schema/)
    expect(() => validateBundleManifest(manifest({ platform: 'windows' }), want)).toThrow(/platform/)
    expect(() => validateBundleManifest(manifest({ arch: 'x64' }), want)).toThrow(/arch/)
    expect(() => validateBundleManifest(manifest(), { platform: 'macos', arch: 'riscv' })).toThrow(/arch/)
  })

  it('rejects version/tag and commit-shaped lies', () => {
    expect(() => validateBundleManifest(manifest({ old: side({ version: '0.99.0' }) }), want)).toThrow(/version/)
    expect(() => validateBundleManifest(manifest({ old: side({ tag: 'notatag' }) }), want)).toThrow(/tag/)
    expect(() => validateBundleManifest(manifest({ old: side({ commit: 'zz' }) }), want)).toThrow(/commit/)
  })

  it('requires the resolver to have downloaded the artifact', () => {
    expect(() => validateBundleManifest(
      manifest({ old: side({ artifact: { url: 'x', sha256: 'b'.repeat(64) } }) }), want,
    )).toThrow(/artifact.path/)
    expect(() => validateBundleManifest(
      manifest({ old: side({ artifact: { url: 'x', sha256: 'nothex', path: '/tmp/x' } }) }), want,
    )).toThrow(/sha256/)
  })

  it('rejects a no-op pair and a team-changing pair (Squirrel gate)', () => {
    expect(() => validateBundleManifest(manifest({ new: side() }), want)).toThrow(/no update/)
    expect(() => validateBundleManifest(
      manifest({ new: side({ commit: 'c'.repeat(40), identity: 'OTHER999', tag: 'v0.29.0', version: '0.29.0', artifact: { url: 'u', sha256: 'd'.repeat(64), path: '/tmp/n' } }) }), want,
    )).toThrow(/Squirrel/)
  })
})

describe('codesignTeam', () => {
  it('parses the team from codesign -dv output', () => {
    const out = [
      'Executable=/tmp/Hermes.app/Contents/MacOS/Hermes',
      'Identifier=com.nousresearch.hermes-bundled',
      'TeamIdentifier=TEAM1234',
    ].join('\n')
    expect(codesignTeam(out)).toBe('TEAM1234')
  })
  it('returns null when unsigned', () => {
    expect(codesignTeam('Identifier=com.x\nTeamIdentifier=not set')).toBeNull()
  })
})

describe('stampAssertions', () => {
  const sideArg = { commit: 'a'.repeat(40), tag: 'v0.28.0' }
  const good = {
    schemaVersion: 2,
    commit: 'a'.repeat(40),
    branch: 'main',
    payload: 'bundled',
    store: false,
    distribution: 'desktop-app',
    updateMechanism: 'electron-updater',
    tag: 'v0.28.0',
  }
  it('accepts the bundled electron-updater stamp', () => {
    expect(stampAssertions(good, sideArg)).toEqual([])
  })
  it('flags wrong mechanism, payload, commit and tag', () => {
    const problems = stampAssertions(
      { ...good, updateMechanism: 'external', payload: 'light', commit: 'b'.repeat(40), tag: null },
      sideArg,
    )
    expect(problems).toHaveLength(4)
  })
  it('rejects non-objects and store submissions', () => {
    expect(stampAssertions(null, sideArg)).toHaveLength(1)
    expect(stampAssertions({ ...good, store: true }, sideArg).join(' ')).toMatch(/store/)
  })
})

describe('mac-bundled-feed materializer', () => {
  it('emits the production update-feed contract for the real NEW zip', async () => {
    const { materializeFeed, feedFileNames } = await import('../tests/install/e2e-assets/mac-bundled-feed.mjs')
    const { createHash } = await import('node:crypto')

    // The feed layout comes from the ONE production contract, not a copy.
    const { darwinFeed } = require('../apps/desktop/update-feed.cjs')
    expect(darwinFeed('stable').directory).toBe('releases/darwin/stable')
    expect(feedFileNames('arm64').prefixed).toBe('arm64-stable-mac.yml')
    expect(feedFileNames('x64').prefixed).toBe('stable-mac.yml')

    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mac-feed-'))
    const zip = path.join(outDir, 'HermesBundled-0.29.0-mac-arm64.zip')
    const zipBytes = Buffer.from('fake signed zip for feed shape tests')
    fs.writeFileSync(zip, zipBytes)
    const expectedSha512 = createHash('sha512').update(zipBytes).digest('base64')

    const receipt = materializeFeed({
      outDir,
      zipPath: zip,
      version: '0.29.0',
      tag: 'v0.29.0',
      arch: 'arm64',
      releaseDate: '2026-09-07T00:00:00.000Z',
    })

    // The artifact is served under the merged production URL shape.
    expect(receipt.artifactUrlPath).toBe('/releases/tag/v0.29.0/HermesBundled-0.29.0-mac-arm64.zip')
    expect(receipt.written).toEqual([
      'releases/darwin/stable/arm64-stable-mac.yml',
      'releases/darwin/stable/stable-mac.yml',
    ])
    const yml = fs.readFileSync(path.join(outDir, 'releases/darwin/stable/arm64-stable-mac.yml'), 'utf8')
    expect(yml).toContain('version: 0.29.0')
    expect(yml).toContain('  - url: /releases/tag/v0.29.0/HermesBundled-0.29.0-mac-arm64.zip')
    expect(yml).toContain(`    sha512: ${expectedSha512}`)
    expect(yml).toContain(`    size: ${zipBytes.length}`)
    expect(yml).toContain(`path: /releases/tag/v0.29.0/HermesBundled-0.29.0-mac-arm64.zip`)
    expect(yml).toContain(`sha512: ${expectedSha512}`)
    // The served artifact bytes are the real NEW zip, copied verbatim.
    expect(fs.readFileSync(path.join(outDir, 'releases/tag/v0.29.0/HermesBundled-0.29.0-mac-arm64.zip')))
      .toEqual(zipBytes)
  })
})
