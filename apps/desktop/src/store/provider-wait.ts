import { atom, computed } from 'nanostores'

const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

export const $providerWaitSessions = atom<Record<string, string>>({})

export function sessionProviderWait(sessionId: null | string) {
  return computed($providerWaitSessions, sessions => sessions[keyFor(sessionId)] ?? '')
}

export function setSessionProviderWait(sessionId: string | null | undefined, text: string): void {
  const key = keyFor(sessionId)
  const sessions = $providerWaitSessions.get()
  const nextText = text.trim()

  if (!nextText) {
    if (!(key in sessions)) {
      return
    }

    const next = { ...sessions }
    delete next[key]
    $providerWaitSessions.set(next)

    return
  }

  if (sessions[key] === nextText) {
    return
  }

  $providerWaitSessions.set({ ...sessions, [key]: nextText })
}

export function clearSessionProviderWait(sessionId: string | null | undefined): void {
  setSessionProviderWait(sessionId, '')
}

export function clearAllProviderWaits(): void {
  $providerWaitSessions.set({})
}

/** Only the core's explained wait/reconnect frames belong in Desktop's status
 * row. Generic kawaii spinner rewrites remain presentation noise. */
export function providerWaitText(text: string): string {
  const value = text.trim()

  return /^(?:⏳|⚠|↻)\s*(?:waiting on|loading|no (?:output|response)|model returned)/i.test(value) ? value : ''
}

/** Parse a managed-local model-load frame ("⏳ loading <model> into memory —
 * 43% …") into its parts, or null for every other wait frame. The percent is
 * real (per-tensor load callback relayed by the server), so surfaces can
 * render an honest determinate bar instead of prose. */
export function parseModelLoadWait(text: string): null | { model: string; percent: number } {
  const m = /^⏳\s*loading\s+(.+?)\s+into memory\s+—\s+(\d{1,3})%/i.exec(text.trim())

  if (!m) {
    return null
  }

  return { model: m[1], percent: Math.max(0, Math.min(100, Number(m[2]))) }
}
