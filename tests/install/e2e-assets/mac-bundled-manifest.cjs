'use strict'
// mac-bundled-manifest.cjs — pure validation/assertion helpers for the
// macOS packaged-app -> open-app-update E2E arm
// (tests/install/macos-bundled-e2e.sh). No dependencies, no side effects:
// everything here is requireable from vitest (tests-js) and from the
// driver's node invocations on the macOS runner.
//
// The bundle manifest itself is resolved by the PARENT-owned common
// resolver: tests/install/e2e-assets/bundle-inputs.mjs
//   --manifest-url URL --platform macos --arch arm64|x64 --out WORKROOT
// which validates remote bytes (commits, versions, sha256) and writes
// WORKROOT/bundle-inputs.json with old/new.artifact.path added (local
// downloaded artifact). This module only asserts the normalized result.

const ARCHES = ['arm64', 'x64']
const SHA256_RE = /^[0-9a-f]{64}$/
// stable tags: vX.Y.Z; canary: vX.Y.Z-canary.YYYYMMDDHHMMSS
const TAG_RE = /^v\d+\.\d+\.\d+(?:-canary\.20\d{6}(?:\d{6})?)?$/

function validateSide(side, label) {
  if (!side || typeof side !== 'object') {
    throw new Error(`${label}: missing side object`)
  }
  for (const key of ['tag', 'version', 'commit', 'identity']) {
    if (typeof side[key] !== 'string' || !side[key]) {
      throw new Error(`${label}.${key}: expected a non-empty string`)
    }
  }
  if (!TAG_RE.test(side.tag)) {
    throw new Error(`${label}.tag: ${JSON.stringify(side.tag)} is not a release tag`)
  }
  if (side.version !== side.tag.slice(1)) {
    throw new Error(`${label}: version ${side.version} does not match tag ${side.tag}`)
  }
  if (!/^[0-9a-f]{40}$/.test(side.commit)) {
    throw new Error(`${label}.commit: expected a 40-hex SHA, got ${JSON.stringify(side.commit)}`)
  }
  const artifact = side.artifact
  if (!artifact || typeof artifact !== 'object') {
    throw new Error(`${label}.artifact: missing object`)
  }
  if (typeof artifact.url !== 'string' || !artifact.url) {
    throw new Error(`${label}.artifact.url: expected a non-empty string`)
  }
  if (typeof artifact.path !== 'string' || !artifact.path) {
    throw new Error(`${label}.artifact.path: missing; the parent resolver must download the artifact first`)
  }
  if (!SHA256_RE.test(String(artifact.sha256 || '').toLowerCase())) {
    throw new Error(`${label}.artifact.sha256: expected 64 hex chars`)
  }
}

/**
 * Validate the normalized bundle-inputs.json for this arm.
 * @param {{schema?: unknown, platform?: unknown, arch?: unknown,
 *          old?: unknown, new?: unknown}} manifest
 * @param {{platform: 'macos', arch: 'arm64'|'x64'}} want
 */
function validateBundleManifest(manifest, want) {
  if (!manifest || typeof manifest !== 'object') {
    throw new Error('bundle-inputs.json: not an object')
  }
  if (manifest.schema !== 1) {
    throw new Error(`bundle-inputs.json: unsupported schema ${JSON.stringify(manifest.schema)}`)
  }
  if (manifest.platform !== want.platform) {
    throw new Error(`bundle-inputs.json: platform ${JSON.stringify(manifest.platform)} != ${want.platform}`)
  }
  if (!ARCHES.includes(want.arch)) {
    throw new Error(`arch: ${JSON.stringify(want.arch)} is not one of ${ARCHES.join('|')}`)
  }
  if (manifest.arch !== want.arch) {
    throw new Error(`bundle-inputs.json: arch ${JSON.stringify(manifest.arch)} != ${want.arch}`)
  }
  validateSide(manifest.old, 'old')
  validateSide(manifest.new, 'new')
  if (manifest.old.commit === manifest.new.commit) {
    throw new Error('old.commit == new.commit: no update would be available')
  }
  if (manifest.old.identity !== manifest.new.identity) {
    // Squirrel.Mac refuses an update whose code signature changes team.
    throw new Error(
      `old.identity ${manifest.old.identity} != new.identity ${manifest.new.identity}: ` +
      'Squirrel.Mac would reject this pair (signature gate)')
  }
  return manifest
}

/** Parse the TeamIdentifier line out of `codesign -dv` output. */
function codesignTeam(codesignOutput) {
  const match = /TeamIdentifier=(?:not set|=)?\s*([A-Z0-9]+)/.exec(codesignOutput)
  return match ? match[1] : null
}

/** True when `codesign --verify` output reports a valid signature. */
function codesignValid(verifyOutput) {
  return /validated/.test(verifyOutput)
}

/**
 * Assertions for an installed bundle's install-stamp.json
 * (Contents/Resources/install-stamp.json) against a manifest side.
 * Mirrors apps/desktop/scripts/write-build-stamp.mjs's bundled shape.
 */
function stampAssertions(stamp, side) {
  const problems = []
  if (!stamp || typeof stamp !== 'object') {
    return ['install-stamp.json: not an object']
  }
  if (stamp.payload !== 'bundled') {
    problems.push(`stamp.payload ${JSON.stringify(stamp.payload)} != 'bundled'`)
  }
  if (stamp.updateMechanism !== 'electron-updater') {
    problems.push(`stamp.updateMechanism ${JSON.stringify(stamp.updateMechanism)} != 'electron-updater'`)
  }
  if (stamp.commit !== side.commit) {
    problems.push(`stamp.commit ${JSON.stringify(stamp.commit)} != ${side.commit}`)
  }
  if (stamp.tag !== side.tag) {
    problems.push(`stamp.tag ${JSON.stringify(stamp.tag)} != ${side.tag}`)
  }
  if (stamp.store === true) {
    problems.push('stamp.store must not be true for a release bundle')
  }
  return problems
}

module.exports = { ARCHES, validateBundleManifest, codesignTeam, codesignValid, stampAssertions }
