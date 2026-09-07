import { isDeepStrictEqual } from 'node:util'
import yaml from 'js-yaml'
import semver from 'semver'
import feedContract from '../apps/desktop/update-feed.cjs'

const { darwinFeed } = feedContract
const arches = ['arm64', 'x64']
const hashPattern = /^[A-Za-z0-9+/]{86}==$/

export function parseMacFeed(text) {
  const feed = yaml.load(text)
  if (!feed || typeof feed !== 'object' || !semver.valid(feed.version) || !Array.isArray(feed.files) || !feed.files.length) {
    throw new Error('Invalid macOS update feed')
  }
  for (const file of feed.files) {
    if (typeof file.url !== 'string' || !hashPattern.test(file.sha512) || !Number.isSafeInteger(file.size) || file.size <= 0) {
      throw new Error('Invalid macOS artifact metadata')
    }
  }
  if (feed.path && !feed.files.some(file => file.url === feed.path && file.sha512 === feed.sha512)) {
    throw new Error('Legacy feed path/hash disagrees with files')
  }
  return feed
}

export function macFeedReferences(text) {
  const feed = parseMacFeed(text)
  const references = []
  for (const file of feed.files) {
    // Only our immutable artifact namespace is eligible for publication or pruning.
    if (!/^\/releases\/tag\/v[0-9A-Za-z.+-]+\/[0-9A-Za-z._+-]+\.(zip|dmg)$/.test(file.url)) {
      throw new Error(`Invalid macOS artifact path: ${file.url}`)
    }
    const key = file.url.slice(1)
    references.push(key, `${key}.blockmap`)
  }
  return references
}

export function mergeMacFeeds(legs, tag, light = false) {
  const version = tag?.startsWith('v') ? tag.slice(1) : ''
  if (!semver.valid(version) || !/^v\d+\.\d+\.\d+(?:-canary\.\d{14})?$/.test(tag)) {
    throw new Error('Invalid macOS release tag')
  }
  const selection = darwinFeed(version.includes('-canary.') ? 'canary' : 'stable', light)
  const expected = arches.map(arch => `${arch}-${selection.fileName}`)
  if (!isDeepStrictEqual(Object.keys(legs).sort(), [...expected].sort())) {
    throw new Error('Expected exactly one ARM64 and one x64 macOS feed')
  }
  const files = new Map()
  let first
  for (const [index, name] of expected.entries()) {
    const leg = parseMacFeed(legs[name])
    if (leg.version !== version) { throw new Error(`Feed version does not match ${tag}`) }
    const prefix = `${light ? 'HermesLight' : 'HermesBundled'}-${version}-mac-${arches[index]}`
    if (!leg.files.some(file => file.url === `${prefix}.zip`)) { throw new Error(`Missing native ZIP for ${arches[index]}`) }
    for (const file of leg.files) {
      if (![`${prefix}.zip`, `${prefix}.dmg`].includes(file.url)) { throw new Error(`Wrong variant or architecture: ${file.url}`) }
      const prior = files.get(file.url)
      const rewritten = { ...file, url: `/releases/tag/${tag}/${file.url}` }
      if (prior && !isDeepStrictEqual(prior, rewritten)) { throw new Error(`Conflicting artifact: ${file.url}`) }
      files.set(file.url, rewritten)
    }
    first ??= leg
  }
  const merged = { ...first, files: [...files.values()] }
  if (merged.path) { merged.path = `/releases/tag/${tag}/${merged.path}` }
  const text = yaml.dump(merged, { lineWidth: -1, noRefs: true })
  macFeedReferences(text)
  return { key: `${selection.directory}/${selection.fileName}`, text, files: merged.files, version }
}

/** The transport verifies immutable bytes and conditionally replaces one pointer. */
export async function publishMacFeed(plan, transport) {
  const live = await transport.read(plan.key)
  if (live) {
    const oldFeed = parseMacFeed(live.text)
    const nextFeed = parseMacFeed(plan.text)
    const order = semver.compare(plan.version, oldFeed.version)
    if (order < 0) { throw new Error('Refusing to move the macOS feed backward') }
    if (order === 0) {
      if (!isDeepStrictEqual(oldFeed, nextFeed)) { throw new Error('Refusing to replace published version with different artifacts') }
      return
    }
  }
  for (const file of plan.files) { await transport.verify(file.url.slice(1), file) }
  await transport.write(plan.key, plan.text, live?.etag ?? null)
  const published = await transport.read(plan.key)
  if (!published || published.text !== plan.text) { throw new Error('macOS feed readback differs from publication') }
}
