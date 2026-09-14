import { execFile } from 'node:child_process'
import { lstat, readFile } from 'node:fs/promises'

import { INSTALL_STAMP, type InstallStamp } from '../install-stamp'

import type { RetirementDestination } from './retirement-state'

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


type NativeInstallStamp = Pick<InstallStamp,
  'commit' | 'payload' | 'distribution' | 'dirty' | 'receiverProtocol' | 'updateMechanism' | 'tag' | 'source' | 'channelBuild'>

function stableOwned(stamp: NativeInstallStamp): boolean {
  return /^v\d+\.\d+\.\d+$/.test(stamp.tag ?? '') && !stamp.channelBuild &&
    stamp.source !== 'channel-build' && stamp.source !== 'commit-build'
}

export function assertRunningRetirementStamp(target: RetirementDestination): void {
  if (
    !INSTALL_STAMP ||
    !stableOwned(INSTALL_STAMP) ||
    INSTALL_STAMP.commit !== target.commit ||
    INSTALL_STAMP.payload !== 'bundled' ||
    INSTALL_STAMP.distribution !== 'desktop-app' ||
    INSTALL_STAMP.dirty ||
    INSTALL_STAMP.receiverProtocol !== 1 ||
    INSTALL_STAMP.updateMechanism !== (target.platform === 'darwin' ? 'electron-updater' : 'app-installer')
  ) {
    throw new Error('Running destination baked stamp is not the pinned official stable release')
  }
}

export async function assertRetirementStamp(file: string, target: RetirementDestination): Promise<void> {
  const stamp: NativeInstallStamp = JSON.parse(await readFile(file, 'utf8'))

  if (
    !stableOwned(stamp) ||
    stamp.commit !== target.commit ||
    stamp.payload !== 'bundled' ||
    stamp.distribution !== 'desktop-app' ||
    stamp.dirty !== false ||
    stamp.receiverProtocol !== 1 ||
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
