import { randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { isDeepStrictEqual } from 'node:util'

export interface DesktopBootPreference {
  profile: string | null
  home?: string
  _migrated?: boolean
}

export interface AdoptedDesktopHome {
  home: string
  profile: string | null
}

/** Ordinary startup may ignore a broken legacy profile; adoption must still fail closed. */
export function readStartupDesktopPreference(file: string, warn: (message: string) => void): DesktopBootPreference | null {
  try { return readDesktopBootPreference(file) }
  catch (error) {
    warn(`Desktop boot preference was not loaded; preserving the original file: ${error instanceof Error ? error.message : String(error)}`)
    return null
  }
}

function validProfile(profile: string | null): boolean {
  return profile === null || /^[a-z0-9][a-z0-9_-]{0,63}$/.test(profile)
}

/** Missing is a fresh install; unreadable state must not be treated as permission to replace it. */
export function readDesktopBootPreference(file: string): DesktopBootPreference | null {
  let text: string

  try {
    text = readFileSync(file, 'utf8')
  } catch (error: unknown) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') { return null }
    throw error
  }

  const parsed: unknown = JSON.parse(text)

  if (!parsed || typeof parsed !== 'object' || !('profile' in parsed)) {
    throw new Error('Invalid desktop boot preference')
  }

  const profile: unknown = parsed.profile

  if (profile !== null && (typeof profile !== 'string' || !validProfile(profile))) {
    throw new Error('Invalid profile in desktop boot preference')
  }

  if ('home' in parsed && (typeof parsed.home !== 'string' || !path.isAbsolute(parsed.home))) {
    throw new Error('Invalid home in desktop boot preference')
  }

  if ('_migrated' in parsed && typeof parsed._migrated !== 'boolean') {
    throw new Error('Invalid migration marker in desktop boot preference')
  }

  return {
    ...parsed,
    profile: typeof profile === 'string' ? profile : null,
    ...('home' in parsed && typeof parsed.home === 'string' ? { home: parsed.home } : {}),
    ...('_migrated' in parsed && typeof parsed._migrated === 'boolean' ? { _migrated: parsed._migrated } : {})
  }
}

function updatePreference(file: string, update: (current: DesktopBootPreference | null) => DesktopBootPreference): DesktopBootPreference {
  mkdirSync(path.dirname(file), { recursive: true })
  const temporary: string = `${file}.${randomUUID()}.tmp`

  // The destination's single-instance main process owns all preference writes.
  // Keep read, compare, and rename synchronous so its IPC handlers cannot interleave.
  try {
    let current: DesktopBootPreference | null

    try {
      current = readDesktopBootPreference(file)
    } catch (error: unknown) {
      throw new Error('Cannot update an unreadable desktop boot preference', { cause: error })
    }

    const next: DesktopBootPreference = update(current)

    writeFileSync(temporary, JSON.stringify(next, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
    renameSync(temporary, file)

    return next
  } finally {
    rmSync(temporary, { force: true })
  }
}

export function writeDesktopProfile(file: string, profile: string | null): string | null {
  if (!validProfile(profile)) { throw new Error(`Invalid profile name: ${profile}`) }

  return updatePreference(file, (current: DesktopBootPreference | null): DesktopBootPreference => {
    const next: DesktopBootPreference = { ...current, profile }

    delete next._migrated

    return next
  }).profile
}

/** The acceptance prompt names the previous preference; a concurrent user choice invalidates it. */
export function adoptDesktopHome(file: string, target: AdoptedDesktopHome, expected: DesktopBootPreference | null): void {
  if (!path.isAbsolute(target.home) || !validProfile(target.profile)) {
    throw new Error('Invalid adopted desktop home or profile')
  }

  updatePreference(file, (current: DesktopBootPreference | null): DesktopBootPreference => {
    if (!isDeepStrictEqual(current, expected)) {
      throw new Error('Desktop boot preference changed; review the migration again')
    }

    return { ...current, home: target.home, profile: target.profile, _migrated: false }
  })
}
