'use strict'
// Native journey inputs are exact archived builds, not a relaxed OLD/NEW manifest.
const { createHash } = require('node:crypto')
const { isDeepStrictEqual } = require('node:util')

function validateJourney(input, platform, arch) {
  if (input.schema !== 1 || input.platform !== platform || input.arch !== arch ||
      !['darwin', 'win32'].includes(platform) || !['arm64', 'x64'].includes(arch)) {
    throw new Error('Native journey platform/architecture mismatch')
  }
  if (!/^[a-f0-9]{40}$/.test(input.controllerCommit) || !/^[\w-]+\/[\w.-]+$/.test(input.repository)) {
    throw new Error('Missing controller authority')
  }
  if (!['direct', 'via-update'].includes(input.route) || !['absent', 'running'].includes(input.stable)) {
    throw new Error('Unsupported journey scenario')
  }
  for (const name of ['A', 'S', ...(input.route === 'via-update' ? ['B'] : []), ...(input.T ? ['T'] : [])]) {
    const side = input[name]
    const request = side?.manifest?.request
    const head = side?.head
    if (!request || !head || !/^[a-f0-9]{32}$/.test(request.buildId) || !/^[a-f0-9]{40}$/.test(request.commit) ||
        request.repository !== input.repository || head.buildId !== request.buildId || head.sequence !== request.sequence ||
        head.manifestKey !== `releases/channel-builds/${request.buildId}/build.json` || !/^[a-f0-9]{64}$/.test(head.sha256)) {
      throw new Error(`Invalid ${name} immutable build binding`)
    }
    const url = new URL(request.publicBase)
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && url.hostname === '127.0.0.1')) {
      throw new Error('Journey archive must use HTTPS or explicit loopback')
    }
    const packages = side.manifest.packages.filter(p => p.platform === platform && p.arch === arch && p.variant === 'bundled')
    if (packages.length !== 1) throw new Error(`Missing ${name} native package`)
    const pkg = packages[0]
    if (pkg.identity !== request.identity[platform === 'darwin' ? 'appId' : 'msixAppIdWithOrg'] ||
        pkg.version !== request[platform === 'darwin' ? 'version' : 'windowsVersion'] ||
        !/^[a-f0-9]{64}$/.test(pkg.artifact.sha256) || !Number.isSafeInteger(pkg.artifact.size) || pkg.artifact.size <= 0) {
      throw new Error(`Invalid ${name} native artifact`)
    }
    side.package = pkg
  }
  if (input.A.package.identity === input.S.package.identity || input.S.manifest.request.releaseTag !== `v${input.S.manifest.request.version}`) {
    throw new Error('Retirement must enter a distinct stable release identity')
  }
  if (input.B && (input.A.package.identity !== input.B.package.identity || input.B.head.sequence <= input.A.head.sequence)) {
    throw new Error('A to B must advance within one preview identity')
  }
  if (input.T && (input.S.package.identity !== input.T.package.identity || input.T.head.sequence <= input.S.head.sequence)) {
    throw new Error('S to T must advance within stable identity')
  }
  return input
}

function verifyBytes(bytes, expected) {
  if (bytes.length !== expected.size || createHash('sha256').update(bytes).digest('hex') !== expected.sha256) {
    throw new Error('Immutable artifact size/hash mismatch')
  }
}

function verifyManifest(bytes, side) {
  if (createHash('sha256').update(bytes).digest('hex') !== side.head.sha256 ||
      !isDeepStrictEqual(JSON.parse(bytes.toString('utf8')), side.manifest)) {
    throw new Error('Archived manifest differs from journey')
  }
}

module.exports = { validateJourney, verifyBytes, verifyManifest }
