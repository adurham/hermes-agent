'use strict'
// Cross-identity journey input only; ordinary bundle validation stays strict.
const { createHash } = require('node:crypto')
const { isDeepStrictEqual } = require('node:util')
const semver = require('semver')

/** @typedef {import('../../../apps/desktop/electron/updater/channel-protocol.ts').ChannelManifest} Manifest */
/** @typedef {{manifest: Manifest, head: import('../../../apps/desktop/electron/updater/channel-protocol.ts').ChannelHead,
 * package: import('../../../apps/desktop/electron/updater/channel-protocol.ts').ChannelPackage, artifactPath?: string}} Side */
/** @typedef {{schema: 1, platform: 'darwin'|'win32', arch: 'arm64'|'x64', repository: string,
 * controllerCommit: string, route: 'direct'|'via-update', stable: 'absent'|'running', home: 'shared',
 * minimumVersion: string, A: Side, B?: Side, S: Side, T: Side}} Journey */

const SHA256 = /^[a-f0-9]{64}$/
const COMMIT = /^[a-f0-9]{40}$/
const SLUG = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/

function archiveBase(value) {
  const url = new URL(value)
  if (!['https:', 'http:'].includes(url.protocol) ||
      (url.protocol === 'http:' && url.hostname !== '127.0.0.1') ||
      url.username || url.password || url.search || url.hash ||
      /[%\\;\s]/.test(value) || value.endsWith('/') ||
      value.split('/').some(part => part === '.' || part === '..')) {
    throw new Error('Archive must use canonical HTTPS or explicit loopback without credentials')
  }
  return value
}

function objectKey(key) {
  if (!/^releases\/[A-Za-z0-9_./+-]+$/.test(key) || key.split('/').some(part =>
    !part || part === '.' || part === '..' || part.endsWith('.') || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))) {
    throw new Error('Invalid immutable object key')
  }
  return key
}


function validateScenario(input, platform, arch) {
  if (input.schema !== 1 || input.platform !== platform || input.arch !== arch ||
      !['darwin', 'win32'].includes(platform) || !['arm64', 'x64'].includes(arch)) {
    throw new Error('Native journey platform/architecture mismatch')
  }
  if (!COMMIT.test(input.controllerCommit) || !/^[\w-]+\/[\w.-]+$/.test(input.repository)) {
    throw new Error('Missing controller authority')
  }
  if (!['direct', 'via-update'].includes(input.route) || !['absent', 'running'].includes(input.stable) ||
      input.home !== 'shared') throw new Error('Unsupported journey scenario')
  if ((input.route === 'direct') !== (input.B === undefined)) throw new Error('B is required only for via-update')
  if (!semver.valid(input.minimumVersion) || semver.prerelease(input.minimumVersion)) throw new Error('Invalid stable version floor')
}

function validateBinding(side, name, repository) {
  const request = side?.manifest?.request
  const head = side?.head
  if (!request || !head || side.manifest.schema !== 1 || request.schema !== 1 ||
      !/^[a-f0-9]{32}$/.test(request.buildId) || !COMMIT.test(request.commit) ||
      request.repository !== repository || !Number.isSafeInteger(request.sequence) || request.sequence < 1) {
    throw new Error(`Invalid ${name} immutable build binding`)
  }
  if (head.buildId !== request.buildId || head.sequence !== request.sequence ||
      head.manifestKey !== `releases/channel-builds/${request.buildId}/build.json` || !SHA256.test(head.sha256)) {
    throw new Error(`Invalid ${name} immutable head binding`)
  }
  if (!SLUG.test(request.channel) || request.channel.length > 32) throw new Error('Invalid channel name')
  archiveBase(request.publicBase)
  return request
}

function validatePackage(side, request, platform, arch) {
  const packages = side.manifest.packages.filter(p => p.platform === platform && p.arch === arch && p.variant === 'bundled')
  if (packages.length !== 1) throw new Error('Missing unique native package')
  const pkg = packages[0]
  const darwin = platform === 'darwin'
  if (pkg.identity !== request.identity[darwin ? 'appId' : 'msixAppIdWithOrg'] ||
      pkg.version !== request[darwin ? 'version' : 'windowsVersion'] ||
      !SHA256.test(pkg.artifact.sha256) || !Number.isSafeInteger(pkg.artifact.size) || pkg.artifact.size <= 0) {
    throw new Error('Invalid native artifact')
  }
  const signer = darwin ? pkg.teamId : pkg.publisher
  if (!signer || (darwin && !/^[A-Z0-9]{10}$/.test(signer))) throw new Error('Missing native signing identity')
  const prefixes = [`releases/channel-builds/${request.buildId}/`]
  if (request.releaseTag) prefixes.push(`releases/tag/${request.releaseTag}/`)
  for (const key of [pkg.artifact.key, pkg.feed.key]) {
    if (!prefixes.some(prefix => objectKey(key).startsWith(prefix))) throw new Error('Artifact/feed outside immutable build')
  }
  if (pkg.feed.channel !== 'stable' || !pkg.feed.key.endsWith(darwin ? '/stable-mac.yml' : '.appinstaller')) {
    throw new Error('Invalid immutable native feed')
  }
  return { ...side, package: pkg }
}

function validatePreview(side, platform) {
  const request = side.manifest.request
  const version = platform === 'darwin' ? `0.0.${request.sequence}`
    : `0.${Math.floor(request.sequence / 65536)}.${request.sequence % 65536}.0`
  if (request.releaseTag || request.sequence > 0xffffffff || side.package.version !== version) {
    throw new Error('Preview package must use its allocated native sequence')
  }
}

function validateStable(side) {
  const request = side.manifest.request
  if (!semver.valid(request.version) || semver.prerelease(request.version) || request.releaseTag !== `v${request.version}`) {
    throw new Error('Destination must be a stable release')
  }
  if (request.channel !== 'stable' || side.manifest.receiverProtocol !== 1) throw new Error('Missing stable receiver support')
}

function assertAdvance(before, after, description) {
  if (before.manifest.request.channel !== after.manifest.request.channel ||
      !isDeepStrictEqual(before.manifest.request.identity, after.manifest.request.identity) ||
      after.head.sequence <= before.head.sequence) throw new Error(`${description} must advance within one identity`)
  const left = before.package.version.split('.').map(Number)
  const right = after.package.version.split('.').map(Number)
  if (left.length !== right.length || [...left, ...right].some(n => !Number.isSafeInteger(n) || n < 0)) {
    throw new Error('Native versions must be numeric')
  }
  const index = left.findIndex((n, i) => n !== right[i])
  if (index < 0 || right[index] <= left[index]) throw new Error(`${description} native version did not advance`)
}

function validateJourney(input, platform, arch) {
  validateScenario(input, platform, arch)
  const journey = { ...input }
  for (const name of ['A', 'S', 'T', ...(input.route === 'via-update' ? ['B'] : [])]) {
    const request = validateBinding(input[name], name, input.repository)
    journey[name] = validatePackage(input[name], request, platform, arch)
    if (request.publicBase !== input.A.manifest.request.publicBase) throw new Error('Journey archive authority differs')
  }
  validatePreview(journey.A, platform)
  if (journey.B) validatePreview(journey.B, platform)
  validateStable(journey.S)
  validateStable(journey.T)
  if (journey.A.package.identity === journey.S.package.identity ||
      journey.A.manifest.request.channel === journey.S.manifest.request.channel || journey.A.manifest.request.releaseTag) {
    throw new Error('Retirement must enter a distinct stable release identity')
  }
  const signer = platform === 'darwin' ? 'teamId' : 'publisher'
  for (const name of ['S', 'T', ...(journey.B ? ['B'] : [])]) {
    if (journey[name].package[signer] !== journey.A.package[signer]) throw new Error('Journey signing authority differs')
  }
  if (journey.B) assertAdvance(journey.A, journey.B, 'A to B')
  assertAdvance(journey.S, journey.T, 'S to T')
  if (semver.lt(journey.S.manifest.request.sourceVersion, input.minimumVersion)) throw new Error('Stable is below compatibility floor')
  return journey
}

function verifyBytes(bytes, expected) {
  if (bytes.length !== expected.size || createHash('sha256').update(bytes).digest('hex') !== expected.sha256) {
    throw new Error('Immutable artifact size/hash mismatch')
  }
}

function verifyManifest(bytes, side) {
  if (createHash('sha256').update(bytes).digest('hex') !== side.head.sha256 ||
      !isDeepStrictEqual(JSON.parse(bytes.toString('utf8')), side.manifest)) throw new Error('Archived manifest differs from journey')
}

module.exports = { archiveBase, objectKey, validateJourney, verifyBytes, verifyManifest }
