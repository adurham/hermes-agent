import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { prepareRetirementConnection, retirementConnection } from './retirement-connections'

interface RemoteEntry {
  id: string
  kind: 'remote'
  label: string
  url: string
  authMode: 'token'
}

const research: Omit<RemoteEntry, 'id'> = {
  kind: 'remote',
  label: 'Research',
  url: 'https://gateway.example',
  authMode: 'token'
}

async function stableRegistry(entries: RemoteEntry[]): Promise<string> {
  const userData: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-connections-'))
  const primary: string = entries[0]?.id ?? 'local'
  await writeFile(
    path.join(userData, 'connections.json'),
    JSON.stringify({ primary, launchMode: 'primary', lastUsed: primary, connections: entries })
  )
  return userData
}

test('retired connections reject URL credentials before journal serialization', () => {
  expect(() => retirementConnection({
    id: 'remote',
    kind: 'remote',
    label: 'Demo',
    url: 'https://alice:fake-demo-secret@gateway.example',
    authMode: 'token'
  })).toThrowError(/remove url credentials/i)
})

test('prepare resolves an identical stable endpoint under its existing id', async () => {
  const userData = await stableRegistry([{ id: 'stable-remote', ...research }])
  try {
    const resolved = prepareRetirementConnection(userData, 'retired-remote', research)
    expect(resolved.id).toBe('stable-remote')
    expect(resolved.settings).toEqual(research)
  } finally {
    await rm(userData, { recursive: true, force: true })
  }
})

test('prepare rejects an ambiguous endpoint registered twice in stable', async () => {
  const userData = await stableRegistry([
    { id: 'stable-remote', ...research },
    { id: 'duplicate-remote', ...research }
  ])
  try {
    expect(() => prepareRetirementConnection(userData, 'retired-remote', research))
      .toThrowError(/multiple stable connections/i)
  } finally {
    await rm(userData, { recursive: true, force: true })
  }
})

test('prepare still applies registry validation for genuinely new endpoints', async () => {
  const userData = await stableRegistry([{ id: 'stable-remote', ...research }])
  try {
    // Same label, different endpoint: the ordinary registry rules decide.
    expect(() => prepareRetirementConnection(userData, 'retired-remote', { ...research, url: 'https://other.example' }))
      .toThrowError()
    const fresh = prepareRetirementConnection(userData, 'retired-remote', { ...research, label: 'Other', url: 'https://other.example' })
    expect(fresh.id).toBe('retired-remote')
  } finally {
    await rm(userData, { recursive: true, force: true })
  }
})
