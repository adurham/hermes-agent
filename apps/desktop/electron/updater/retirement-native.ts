import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { lstat, open, readFile, rename, unlink } from 'node:fs/promises'
import type { FileHandle } from 'node:fs/promises'
import path from 'node:path'

import { INSTALL_STAMP } from '../install-stamp'

import type { RetirementDestination, RetirementJournal } from './retirement-state'

export interface NativeCommandResult {
  stdout: string
  stderr: string
}

export function runRetirementCommand(
  file: string,
  args: readonly string[],
  timeout: number = 120_000,
  input: string = ''
): Promise<NativeCommandResult> {
  return new Promise<NativeCommandResult>((resolve, reject): void => {
    const child: ReturnType<typeof execFile> = execFile(
      file,
      [...args],
      { encoding: 'utf8', windowsHide: true, timeout, maxBuffer: 4 * 1024 * 1024 },
      (error: Error | null, stdout: string, stderr: string): void => {
        if (error) {
          reject(new Error(`Native retirement operation failed: ${file}: ${stderr}`, { cause: error }))

          return
        }

        resolve({ stdout, stderr })
      }
    )

    child.stdin?.end(input)
  })
}

export async function verifyRetirementArtifact(
  file: string,
  artifact: RetirementDestination['artifact']
): Promise<void> {
  const info: Awaited<ReturnType<typeof lstat>> = await lstat(file)

  if (!info.isFile() || info.isSymbolicLink() || info.size !== artifact.size) {
    throw new Error('Retirement artifact size or type mismatch')
  }

  const hash: ReturnType<typeof createHash> = createHash('sha256')

  for await (const chunk of createReadStream(file)) {
    hash.update(chunk)
  }

  if (hash.digest('hex') !== artifact.sha256) {
    throw new Error('Retirement artifact digest mismatch')
  }
}

/** Download only the immutable digest-bound URL. Redirects cannot change its authority. */
export async function downloadRetirementArtifact(
  journal: RetirementJournal,
  target: RetirementDestination
): Promise<string> {
  const file: string = path.join(journal.directory, `destination.${target.artifact.format}`)

  try {
    await verifyRetirementArtifact(file, target.artifact)

    return file
  } catch (error) {
    if (!(error instanceof Error) || !('code' in error) || error.code !== 'ENOENT') {
      throw error
    }
  }

  const temporary: string = `${file}.partial`
  await unlink(temporary).catch((error: NodeJS.ErrnoException): void => {
    if (error.code !== 'ENOENT') {
      throw error
    }
  })

  const response: Response = await fetch(target.artifact.url, {
    redirect: 'error',
    signal: AbortSignal.timeout(600_000)
  })

  if (!response.ok || !response.body) {
    throw new Error(`Retirement download failed (${response.status})`)
  }

  const handle: FileHandle = await open(temporary, 'wx', 0o600)
  let size: number = 0

  try {
    for await (const chunk of response.body) {
      size += chunk.byteLength

      if (size > target.artifact.size) {
        throw new Error('Retirement download exceeds pinned size')
      }

      await handle.writeFile(chunk)
    }

    await handle.sync()
  } finally {
    await handle.close()
  }

  await verifyRetirementArtifact(temporary, target.artifact)
  await rename(temporary, file)

  return file
}

interface NativeInstallStamp {
  commit: string
  payload: string
  distribution: string
  dirty: boolean
  updateMechanism: string
}

export function assertRunningRetirementStamp(target: RetirementDestination): void {
  if (
    !INSTALL_STAMP ||
    INSTALL_STAMP.commit !== target.commit ||
    INSTALL_STAMP.payload !== 'bundled' ||
    INSTALL_STAMP.distribution !== 'desktop-app' ||
    INSTALL_STAMP.dirty ||
    INSTALL_STAMP.updateMechanism !== (target.platform === 'darwin' ? 'electron-updater' : 'app-installer')
  ) {
    throw new Error('Running destination baked stamp is not the pinned official stable release')
  }
}

export async function assertRetirementStamp(file: string, target: RetirementDestination): Promise<void> {
  const stamp: NativeInstallStamp = JSON.parse(await readFile(file, 'utf8'))

  if (
    stamp.commit !== target.commit ||
    stamp.payload !== 'bundled' ||
    stamp.distribution !== 'desktop-app' ||
    stamp.dirty !== false ||
    stamp.updateMechanism !== (target.platform === 'darwin' ? 'electron-updater' : 'app-installer')
  ) {
    throw new Error('Destination stamp does not identify the pinned stable bundled release')
  }
}

export async function retirementPathExists(file: string): Promise<boolean> {
  try {
    await lstat(file)

    return true
  } catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') {
      return false
    }

    throw error
  }
}
