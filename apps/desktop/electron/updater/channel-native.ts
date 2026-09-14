import { createHash } from 'node:crypto'
import { open } from 'node:fs/promises'

import type { ChannelPackage } from './channel-protocol'

/** Native feeds carry their own hashes; also require the admitted build's hash. */
export async function verifyChannelDownload(files: string[], artifact: ChannelPackage['artifact']): Promise<void> {
  if (files.length !== 1) { throw new Error('Expected exactly one channel artifact') }
  const file = await open(files[0], 'r')
  try {
    const stat = await file.stat()
    if (!stat.isFile() || stat.size !== artifact.size) { throw new Error('Channel artifact size mismatch') }
    const digest = createHash('sha256')
    for await (const chunk of file.createReadStream({ autoClose: false })) { digest.update(chunk) }
    if (digest.digest('hex') !== artifact.sha256) { throw new Error('Channel artifact digest mismatch') }
  } finally { await file.close() }
}
