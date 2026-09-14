import { lstat, open, rename, rm } from 'node:fs/promises'
import path from 'node:path'

import { runRetirementCommand } from './retirement-native'
import { canonicalRetirementPath, insideRetirementRoot } from './retirement-state'

export interface RetirementSnapshotTools {
  python: string
  repo: string
  sitePackages: string
}

const migrationSnapshot: string = `import os, pathlib, sys
repo, site, source, target = sys.argv[1:]
sys.path[:0] = [repo, site]
os.environ['HERMES_HOME'] = source
os.environ['HERMES_SHARED_AUTH_DIR'] = str(pathlib.Path(source) / 'shared')
from hermes_cli.backup_migration import snapshot_migration_home
snapshot_migration_home(pathlib.Path(source), pathlib.Path(target))
`


async function assertPrivateDirectory(directory: string): Promise<void> {
  const info: Awaited<ReturnType<typeof lstat>> = await lstat(directory)

  if (
    !info.isDirectory() ||
    info.isSymbolicLink() ||
    (process.platform !== 'win32' && ((info.mode & 0o077) !== 0 || info.uid !== process.getuid?.()))
  ) {
    throw new Error('Snapshot staging must be a private owned directory')
  }
}

async function discardInterruptedSnapshot(directory: string): Promise<void> {
  try {
    await assertPrivateDirectory(directory)
  } catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') {
      return
    }

    throw error
  }

  await rm(directory, { recursive: true })
}

/** Caller holds journal.exclusive and has not published the prepared journal yet. */
export async function snapshotRetirementHome(
  home: string,
  destination: string,
  tools: RetirementSnapshotTools
): Promise<string> {
  const source: string = await canonicalRetirementPath(home)
  const target: string = await canonicalRetirementPath(destination)

  if (insideRetirementRoot(source, target) || insideRetirementRoot(target, source)) {
    throw new Error('Snapshot and live state roots overlap')
  }

  if (target !== path.resolve(destination)) {
    throw new Error('Snapshot staging must be a private owned directory')
  }

  await assertPrivateDirectory(path.dirname(target))
  const partial: string = `${target}.partial`
  // Neither tree is authoritative until preparation publishes its journal.
  await discardInterruptedSnapshot(partial)
  await discardInterruptedSnapshot(target)
  await runRetirementCommand(tools.python, ['-c', migrationSnapshot, tools.repo, tools.sitePackages, source, partial])
  await rename(partial, target)
  if (process.platform !== 'win32') {
    const parent = await open(path.dirname(target), 'r')
    try { await parent.sync() } finally { await parent.close() }
  }

  return target
}
