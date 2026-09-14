import { chmod, copyFile, lstat, mkdir, open, readdir, rename } from 'node:fs/promises'
import type { FileHandle } from 'node:fs/promises'
import path from 'node:path'

import { runRetirementCommand } from './retirement-native'
import { canonicalRetirementPath, insideRetirementRoot } from './retirement-state'

export interface RetirementSnapshotTools {
  python: string
  backupScript: string
}

const sqliteSnapshot: string = `import importlib.util, pathlib, sqlite3, sys
spec = importlib.util.spec_from_file_location('hermes_retirement_backup', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
source, target = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
if not module._safe_copy_db(source, target):
    raise RuntimeError('WAL-safe retirement snapshot failed')
with sqlite3.connect(target) as connection:
    if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise RuntimeError('Retirement snapshot integrity failed')
`

async function isSqlite(file: string): Promise<boolean> {
  const handle: FileHandle = await open(file, 'r')

  try {
    const header: Buffer = Buffer.alloc(16)
    await handle.read(header, 0, 16, 0)

    return header.toString('utf8') === 'SQLite format 3\0'
  } finally {
    await handle.close()
  }
}

async function copyStateTree(source: string, target: string, tools: RetirementSnapshotTools): Promise<void> {
  await mkdir(target, { mode: 0o700 })
  const names: string[] = await readdir(source)

  for (const name of names) {
    const input: string = path.join(source, name)
    const output: string = path.join(target, name)
    const info: Awaited<ReturnType<typeof lstat>> = await lstat(input)

    if (info.isSymbolicLink()) {
      throw new Error('Snapshot contains a symlink; resolve its preservation policy before retirement')
    }

    if (info.isDirectory()) {
      await copyStateTree(input, output, tools)

      continue
    }

    if (!info.isFile()) {
      throw new Error('Snapshot contains a live socket or special file; quiesce its owner first')
    }

    if (name.endsWith('-wal') || name.endsWith('-shm')) {
      const database: string = input.slice(0, -4)

      if (!(await isSqlite(database))) {
        throw new Error('Unrecognized SQLite sidecar in retirement snapshot')
      }

      continue
    }

    if (await isSqlite(input)) {
      await runRetirementCommand(tools.python, ['-c', sqliteSnapshot, tools.backupScript, input, output])
    } else {
      await copyFile(input, output)
    }

    await chmod(output, info.mode & 0o700)
  }
}

/** Caller holds the lifecycle lock. This is private preservation, not a profile export. */
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

  const partial: string = `${target}.partial`
  await copyStateTree(source, partial, tools)
  await rename(partial, target)

  return target
}
