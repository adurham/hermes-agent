#!/usr/bin/env node
// Stage pinned archive bytes only. This command never publishes or installs.
import { createHash } from 'node:crypto'
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'
import { downloadArtifact } from './bundle-inputs.mjs'
import { validateJourney, verifyManifest } from './channel-retirement-manifest.cjs'

/** @param {string} url @param {string} sha256 @returns {Promise<Buffer>} */
async function readObject(url, sha256) {
  const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(60_000), cache: 'no-store' })
  if (!response.ok || !response.body) throw new Error(`Archive read failed: HTTP ${response.status}`)
  const chunks = []
  let size = 0
  for await (const chunk of response.body) {
    size += chunk.length
    if (size > 4 * 1024 * 1024) throw new Error('Archive metadata exceeds limit')
    chunks.push(chunk)
  }
  const bytes = Buffer.concat(chunks)
  if (createHash('sha256').update(bytes).digest('hex') !== sha256) throw new Error('Archive metadata SHA256 mismatch')
  return bytes
}


/** @param {{manifest: string, manifestSha256: string, platform: string, arch: string, out: string}} options */
export async function stageJourney({ manifest, manifestSha256, platform, arch, out }) {
  if (!/^[a-f0-9]{64}$/.test(manifestSha256)) throw new Error('Exact journey SHA256 is required')
  const bytes = await readFile(manifest)
  if (createHash('sha256').update(bytes).digest('hex') !== manifestSha256) throw new Error('Journey SHA256 mismatch')
  const journey = validateJourney(JSON.parse(bytes), platform, arch)
  // Exclusive directory creation rejects stale inputs and receipts from another run.
  await mkdir(out)
  const base = journey.A.manifest.request.publicBase
  const slots = ['A', ...(journey.B ? ['B'] : []), 'S', 'T']

  for (const name of slots) {
    const side = journey[name]
    const archived = await readObject(`${base}/${side.head.manifestKey}`, side.head.sha256)
    verifyManifest(archived, side)
    await writeFile(path.join(out, `${name}-manifest.json`), archived, { flag: 'wx' })
  }
  for (const name of slots) {
    const side = journey[name]
    const artifact = side.package.artifact
    const extension = platform === 'darwin' ? '.zip' : '.msixbundle'
    if (!artifact.key.endsWith(extension)) throw new Error(`Native journey needs ${extension}`)
    const target = path.resolve(out, name + extension)
    await downloadArtifact({ url: `${base}/${artifact.key}`, sha256: artifact.sha256 }, target)
    if ((await stat(target)).size !== artifact.size) throw new Error('Native artifact size mismatch')
    side.artifactPath = target
  }
  // Deliberately not receipt.json: download validation is not native evidence.
  const target = path.join(out, 'journey-inputs.json')
  await writeFile(target, JSON.stringify(journey, null, 2) + '\n', { flag: 'wx' })
  return target
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const { values } = parseArgs({ options: {
    manifest: { type: 'string' }, 'manifest-sha256': { type: 'string' },
    platform: { type: 'string' }, arch: { type: 'string' }, out: { type: 'string' },
  } })
  if (!values.manifest || !values['manifest-sha256'] || !values.platform || !values.arch || !values.out) {
    throw new Error('--manifest, --manifest-sha256, --platform, --arch and --out are required')
  }
  console.log(await stageJourney({ ...values, manifestSha256: values['manifest-sha256'] }))
}
