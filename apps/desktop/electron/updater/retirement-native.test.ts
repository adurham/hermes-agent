import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { afterEach, expect, test } from 'vitest'

import { assertRetirementStamp, assertRunningRetirementStamp, verifyRetirementArtifact } from './retirement-native'
import { snapshotRetirementHome } from './retirement-preservation'
import type { RetirementDestination } from './retirement-state'

const directories: string[] = []
afterEach(async (): Promise<void> => {
  for (const directory of directories.splice(0)) {
    await rm(directory, { recursive: true, force: true })
  }
})

test('snapshot includes WAL-backed state and private config without copying Electron envelopes', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'hermes-retirement-snapshot-'))
  directories.push(directory)
  const home: string = path.join(directory, 'home')
  await mkdir(home)
  await writeFile(path.join(home, 'config.yaml'), 'model: test-model\n')
  const database: DatabaseSync = new DatabaseSync(path.join(home, 'state.db'))
  database.exec(
    "PRAGMA journal_mode=WAL; CREATE TABLE witness(value TEXT); INSERT INTO witness VALUES ('survives-wal')"
  )

  try {
    const snapshot: string = await snapshotRetirementHome(home, path.join(directory, 'snapshot'), {
      python: 'python3',
      backupScript: path.resolve('../../hermes_cli/backup_sqlite.py')
    })

    const copied: DatabaseSync = new DatabaseSync(path.join(snapshot, 'state.db'), { readOnly: true })

    try {
      expect(copied.prepare('SELECT value FROM witness').get()?.value).toBe('survives-wal')
    } finally {
      copied.close()
    }

    expect(await readFile(path.join(snapshot, 'config.yaml'), 'utf8')).toBe('model: test-model\n')
  } finally {
    database.close()
  }
})

test('native preparation rejects bytes changed after download and wrong stamped commit or owner', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'hermes-native-retirement-'))
  directories.push(directory)
  const artifact: string = path.join(directory, 'stable.zip')
  const bytes: Buffer = Buffer.from('signed bytes are checked by the native host')
  await writeFile(artifact, bytes)

  const target: RetirementDestination = {
    platform: 'darwin',
    appPath: path.join(directory, 'Hermes.app'),
    identity: 'chat.nous.hermes',
    nativeVersion: '1.0.0',
    signer: 'ABCDE12345',
    architecture: 'arm64',
    applicationId: null,
    packageFamilyName: null,
    commit: 'a'.repeat(40),
    artifact: {
      url: 'https://example.com/a.zip',
      sha256: createHash('sha256').update(bytes).digest('hex'),
      size: bytes.length,
      format: 'zip'
    }
  }

  expect((): void => assertRunningRetirementStamp(target)).toThrow('baked stamp')
  await expect(verifyRetirementArtifact(artifact, target.artifact)).resolves.toBeUndefined()
  await writeFile(artifact, Buffer.alloc(bytes.length))
  await expect(verifyRetirementArtifact(artifact, target.artifact)).rejects.toThrow('digest')
  const stamp: string = path.join(directory, 'install-stamp.json')
  await writeFile(
    stamp,
    JSON.stringify({
      commit: target.commit,
      payload: 'bundled',
      distribution: 'desktop-app',
      dirty: false,
      updateMechanism: 'electron-updater'
    })
  )
  await expect(assertRetirementStamp(stamp, target)).resolves.toBeUndefined()
  await writeFile(
    stamp,
    JSON.stringify({
      commit: 'b'.repeat(40),
      payload: 'bundled',
      distribution: 'desktop-app',
      dirty: false,
      updateMechanism: 'external'
    })
  )
  await expect(assertRetirementStamp(stamp, target)).rejects.toThrow('stamp')
})
