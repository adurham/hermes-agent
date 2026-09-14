import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { build } from 'esbuild'
import { expect, test } from 'vitest'

import { snapshotRetirementHome } from './retirement-preservation'
import { RetirementJournal } from './retirement-state'

const tools = { python: process.env.PYTHON || 'python3', repo: path.resolve('../..'), sitePackages: '' }

test('a killed journal owner and interrupted snapshot can be retried without touching live state', async (): Promise<void> => {
  const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-recovery-'))

  try {
    const home: string = path.join(directory, 'home')
    await mkdir(home)
    await writeFile(path.join(home, 'witness'), 'preserve me')
    const root: string = path.join(directory, 'transactions')
    const id: string = 'a'.repeat(32)
    const journal: RetirementJournal = await RetirementJournal.open(root, id)
    const target: string = path.join(journal.directory, 'snapshot')
    const childFile: string = path.join(directory, 'owner.mjs')
    await build({
      stdin: {
        contents: `
      import { RetirementJournal } from './retirement-state'
      import { mkdir, writeFile } from 'node:fs/promises'
      const journal = await RetirementJournal.open(${JSON.stringify(root)}, ${JSON.stringify(id)})
      await journal.exclusive(async () => {
        await mkdir(${JSON.stringify(target + '.partial')}, {mode: 0o700})
        await writeFile(${JSON.stringify(target + '.partial/stale')}, 'interrupted')
        process.send('locked')
        await new Promise(() => {})
      })
    `,
        resolveDir: path.resolve('electron/updater')
      },
      outfile: childFile,
      bundle: true,
      platform: 'node',
      format: 'esm'
    })
    const child = spawn(process.execPath, [childFile], { stdio: ['ignore', 'pipe', 'pipe', 'ipc'] })

    try {
      await once(child, 'message')
      await expect(journal.exclusive(async (): Promise<void> => {})).rejects.toThrow('busy')
    } finally {
      const exited = once(child, 'exit')
      child.kill('SIGKILL')
      await exited
    }

    const reopened: RetirementJournal = await RetirementJournal.open(root, id)
    await reopened.exclusive(async (): Promise<void> => {
      expect(await snapshotRetirementHome(home, target, tools)).toBe(target)
    })
    expect(await readFile(path.join(target, 'witness'), 'utf8')).toBe('preserve me')
    expect(await readFile(path.join(home, 'witness'), 'utf8')).toBe('preserve me')
    await expect(readFile(path.join(target, 'stale'))).rejects.toMatchObject({ code: 'ENOENT' })
    // A crash after snapshot rename but before journal publication also retries.
    await writeFile(path.join(home, 'witness'), 'latest quiesced state')
    await reopened.exclusive(async (): Promise<void> => {
      await snapshotRetirementHome(home, target, tools)
    })
    expect(await readFile(path.join(target, 'witness'), 'utf8')).toBe('latest quiesced state')
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})

test.skipIf(process.platform === 'win32')(
  'snapshot retry refuses a symlink staging root without deleting its target',
  async (): Promise<void> => {
    const directory: string = await mkdtemp(path.join(os.tmpdir(), 'retirement-snapshot-link-'))

    try {
      const home: string = path.join(directory, 'home')
      const target: string = path.join(directory, 'snapshot')
      await mkdir(home)
      await writeFile(path.join(home, 'witness'), 'preserve me')
      await symlink(home, `${target}.partial`)
      await expect(snapshotRetirementHome(home, target, tools)).rejects.toThrow('private owned directory')
      expect(await readFile(path.join(home, 'witness'), 'utf8')).toBe('preserve me')
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  }
)
