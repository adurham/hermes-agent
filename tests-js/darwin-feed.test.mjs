import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, expect, it, vi } from 'vitest'
import yaml from 'js-yaml'
import { macFeedReferences, mergeMacFeeds, parseMacFeed, publishMacFeed } from '../scripts/darwin-feed.mjs'
import { cacheControlFor, cmdFinalize, cmdPrune, cmdPut } from '../scripts/r2-release.mjs'
import feedContract from '../apps/desktop/update-feed.cjs'

function inputs(version = '0.28.0', light = false) {
  const channel = version.includes('-canary.') ? 'canary' : 'stable'
  const bytes = new Map()
  const legs = Object.fromEntries(['arm64', 'x64'].map((arch, i) => {
    const name = `${light ? 'HermesLight' : 'HermesBundled'}-${version}-mac-${arch}.zip`
    const data = Buffer.from(`test artifact ${arch}`)
    bytes.set(`releases/tag/v${version}/${name}`, data)
    const file = { url: name, size: data.length, sha512: createHash('sha512').update(data).digest('base64') }
    return [`${arch}-${channel}-mac.yml`, yaml.dump({ version, files: [file], path: name, sha512: file.sha512, releaseDate: `2026-09-0${i + 1}T00:00:00Z`, releaseNotes: 'two lines\nof release notes' })]
  }))
  return { legs, bytes }
}

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

function credentials() {
  for (const name of ['CLOUDFLARE_R2_ACCOUNT_ID', 'CLOUDFLARE_R2_ACCESS_KEY_ID', 'CLOUDFLARE_R2_SECRET_ACCESS_KEY', 'CLOUDFLARE_R2_BUCKET']) vi.stubEnv(name, 'fixture')
}

it('never overwrites a published macOS artifact on a same-tag rerun', async () => {
  credentials()
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mac-artifact-'))
  const name = 'HermesBundled-0.28.0-mac-arm64.zip'
  const file = path.join(dir, name)
  const bytes = Buffer.from('already published artifact')
  fs.writeFileSync(file, bytes)
  vi.stubGlobal('fetch', vi.fn(async (_url, options) => {
    if (options.method === 'PUT') {
      expect(options.headers['If-None-Match']).toBe('*')
      for await (const chunk of options.body) { expect(chunk.length).toBeGreaterThan(0) }
      return new Response('', { status: 412 })
    }
    if (options.method === 'HEAD') return new Response(null, { headers: { 'content-length': String(bytes.length) } })
    return new Response(bytes)
  }))
  try { await cmdPut({ tag: 'v0.28.0', key: name, file }) }
  finally { fs.rmSync(dir, { recursive: true, force: true }) }
})

it('the real prune command protects live macOS ZIPs and blockmaps, and fails closed', async () => {
  credentials()
  const plan = mergeMacFeeds(inputs('0.28.0-canary.20200101000000').legs, 'v0.28.0-canary.20200101000000')
  const references = macFeedReferences(plan.text)
  const stale = 'releases/tag/v0.27.0-canary.20200101000000/unreferenced.zip'
  const keys = [plan.key, ...references, stale]
  const listing = `<ListBucketResult>${keys.map(key => `<Contents><Key>${key}</Key></Contents>`).join('')}<IsTruncated>false</IsTruncated></ListBucketResult>`
  const deleted = []
  let readable = true
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (new URL(url).search) return new Response(listing)
    if (options.method === 'DELETE') { deleted.push(decodeURIComponent(new URL(url).pathname)); return new Response('') }
    return readable ? new Response(plan.text) : new Response('', { status: 503 })
  }))
  await cmdPrune({ keepDays: 14, dryRun: false })
  expect(deleted).toEqual([`/fixture/${stale}`])
  deleted.length = 0
  readable = false
  await expect(cmdPrune({ keepDays: 14, dryRun: false })).rejects.toThrow()
  expect(deleted).toEqual([])
})

it('merges native legs with different signatures/dates and preserves release metadata', () => {
  const { legs } = inputs()
  const plan = mergeMacFeeds(legs, 'v0.28.0')
  const feed = parseMacFeed(plan.text)
  expect(feed.files).toHaveLength(2)
  expect(feed.releaseNotes).toBe('two lines\nof release notes')
  expect(plan.key).toBe('releases/darwin/stable/stable-mac.yml')
  expect(macFeedReferences(plan.text)).toEqual(feed.files.flatMap(file => [file.url.slice(1), `${file.url.slice(1)}.blockmap`]))
  expect(cacheControlFor(plan.key)).toBe('no-store')
  const selection = feedContract.darwinFeed('canary', true)
  expect(mergeMacFeeds(inputs('0.29.0-canary.20260906000000', true).legs, 'v0.29.0-canary.20260906000000', true).key)
    .toBe(`${selection.directory}/${selection.fileName}`)
})

it.each(['missing', 'version', 'variant', 'hash', 'legacy', 'traversal'])('rejects %s instead of publishing a partial or wrong feed', kind => {
  const { legs } = inputs()
  const key = 'arm64-stable-mac.yml'
  const feed = yaml.load(legs[key])
  if (kind === 'missing') delete legs[key]
  else {
    if (kind === 'version') feed.version = '0.27.0'
    if (kind === 'variant') feed.files[0].url = feed.files[0].url.replace('HermesBundled', 'HermesLight')
    if (kind === 'hash') feed.files[0].sha512 = 'invalid'
    if (kind === 'legacy') feed.sha512 = 'wrong'
    if (kind === 'traversal') feed.files[0].url = '../other.zip'
    legs[key] = yaml.dump(feed)
  }
  expect(() => mergeMacFeeds(legs, 'v0.28.0')).toThrow()
})

it('verifies all artifacts before a conditional pointer write and readback', async () => {
  const plan = mergeMacFeeds(inputs().legs, 'v0.28.0')
  let live = { text: mergeMacFeeds(inputs('0.27.0').legs, 'v0.27.0').text, etag: 'old' }
  const events = []
  await publishMacFeed(plan, {
    read: async () => live,
    verify: async key => { events.push(key) },
    write: async (key, text, etag) => { expect(etag).toBe('old'); events.push(key); live = { text, etag: 'new' } }
  })
  expect(events.at(-1)).toBe(plan.key)
  expect(events).toHaveLength(3)
  const write = vi.fn()
  await expect(publishMacFeed(mergeMacFeeds(inputs('0.27.0').legs, 'v0.27.0'), { read: async () => live, verify: vi.fn(), write })).rejects.toThrow('backward')
  await expect(publishMacFeed(plan, { read: async () => null, verify: async () => { throw new Error('corrupt bytes') }, write })).rejects.toThrow('corrupt bytes')
  expect(write).not.toHaveBeenCalled()
})

it('finalize uses the real signed transport, validates streamed hashes and publishes last', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mac-feed-'))
  const { legs, bytes } = inputs()
  for (const [name, text] of Object.entries(legs)) fs.writeFileSync(path.join(dir, name), text)
  for (const [key, value] of Object.entries({ CLOUDFLARE_R2_ACCOUNT_ID: 'fixture', CLOUDFLARE_R2_ACCESS_KEY_ID: 'fixture', CLOUDFLARE_R2_SECRET_ACCESS_KEY: 'fixture', CLOUDFLARE_R2_BUCKET: 'bucket' })) vi.stubEnv(key, value)
  const calls = []
  let published
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    const key = decodeURIComponent(new URL(url).pathname).replace('/bucket/', '')
    const method = options.method || 'GET'
    calls.push([method, key])
    expect(options.headers.authorization).toContain('AWS4-HMAC-SHA256')
    if (method === 'PUT') {
      expect(options.headers['If-None-Match']).toBe('*')
      expect(options.headers['Cache-Control']).toBe('no-store')
      published = options.body.toString()
      return new Response('', { status: 200 })
    }
    if (method === 'HEAD') return new Response(null, { headers: { 'content-length': Buffer.byteLength(published).toString() } })
    if (key.endsWith('-mac.yml')) return published ? new Response(published, { headers: { etag: 'new' } }) : new Response('', { status: 404 })
    if (bytes.has(key)) return new Response(bytes.get(key))
    throw new Error(`unexpected request ${key}`)
  }))
  try {
    await cmdFinalize({ tag: 'v0.28.0', dir })
    expect(parseMacFeed(published).files).toHaveLength(2)
    expect(calls.filter(([method]) => method === 'PUT')).toHaveLength(1)
    expect(calls.slice(0, 3).every(([method]) => method === 'GET')).toBe(true)
  } finally { fs.rmSync(dir, { recursive: true, force: true }) }
})
