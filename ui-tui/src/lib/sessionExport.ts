import { mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { isAbsolute, join, resolve } from 'node:path'

export interface SessionExportPayload {
  content: string
  filename?: string
  format: string
}

const pad = (n: number) => String(n).padStart(2, '0')

/** `hermes_conversation_YYYYMMDD_HHMMSS.<fmt>`: the classic CLI's /save default name. */
export const defaultSaveName = (format: string, now = new Date()) =>
  `hermes_conversation_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_` +
  `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.${format}`

/** Write a `session.export` payload on THIS machine (the client), never the gateway host: a named
 *  file resolves against the cwd like the classic CLI, else `<HERMES_HOME>/sessions/saved/`.
 *  Returns the absolute path written. */
export function writeSessionExport(payload: SessionExportPayload, filename?: string): string {
  const home = process.env.HERMES_HOME ?? join(homedir(), '.hermes')

  const path = filename
    ? isAbsolute(filename.replace(/^~(?=$|\/)/, homedir()))
      ? filename.replace(/^~(?=$|\/)/, homedir())
      : resolve(process.cwd(), filename)
    : join(home, 'sessions', 'saved', defaultSaveName(payload.format))

  mkdirSync(join(path, '..'), { recursive: true })
  writeFileSync(path, payload.content, 'utf8')

  return path
}
