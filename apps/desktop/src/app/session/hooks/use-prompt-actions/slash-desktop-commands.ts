import { chatMessageText, toChatMessages } from '@/lib/chat-messages'
import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import { openCommandPalette } from '@/store/command-palette'
import { $displayTimestamps, setDisplayTimestampsWindowOverride } from '@/store/display-timestamps'
import { runExportProfileFlow, runImportProfileFlow } from '@/store/profile-share'
import { $connection } from '@/store/session'
import type { SessionMessage } from '@/types/hermes'

import type { ClientSessionState } from '../../../types'

import type { GatewayRequest } from './utils'

// T11 (#134889): /rollback, /agents (/tasks), /config and /history on the session owner's existing
// verbs. The shared gateway refuses these as `slash.exec` (`gateway/session_commands.py`), and
// `/config` / `/history` were marked terminal-only, so Desktop either dead-ended or never offered them.
// Each verb here is the one Ink and the owner already serve: rollback.list|diff|restore,
// subagent.list + process.list, config.get key=full (the presentation-only projection) and the
// session.resume transcript snapshot.

interface SlashOutput {
  render: (text: string) => void
  sessionId: string
  storedSessionId?: null | string
}

export interface DesktopReadCommandEnv<C extends { arg: string }> {
  requestGateway: GatewayRequest
  updateSessionState: (
    sessionId: string,
    updater: (state: ClientSessionState) => ClientSessionState,
    storedSessionId?: null | string
  ) => ClientSessionState
  withSlashOutput: (ctx: C) => Promise<null | SlashOutput>
}

interface CheckpointRow {
  hash?: string
  message?: string
  timestamp?: string
}

interface RestoreResult {
  error?: string
  history_removed?: number
  reason?: string
  restored_to?: string
  success?: boolean
}

const errorText = (err: unknown) => `error: ${err instanceof Error ? err.message : String(err)}`

const SAVE_USAGE = 'usage: /save [json|md|html] [filename] [redact]'

const SAVE_FORMATS: Record<string, 'html' | 'json' | 'md'> = {
  html: 'html',
  json: 'json',
  markdown: 'md',
  md: 'md',
  snapshot: 'json'
}

const SAVE_FILTERS: Record<string, { extensions: string[]; name: string }> = {
  html: { extensions: ['html'], name: 'HTML' },
  json: { extensions: ['json'], name: 'JSON' },
  md: { extensions: ['md'], name: 'Markdown' }
}

/** `/save` words → format, optional filename and redact flag (main's `/save` grammar); null = usage. */
export function saveRequest(
  arg: string
): { filename?: string; format: 'html' | 'json' | 'md'; redact: boolean } | null {
  const words = arg.trim().split(/\s+/).filter(Boolean)
  const redact = words.length > 0 && ['redact', '--redact'].includes(words[words.length - 1].toLowerCase())
  const rest = redact ? words.slice(0, -1) : words
  const format = rest.length ? SAVE_FORMATS[rest[0].toLowerCase()] : 'json'

  if (!format || rest.length > 2) {
    return null
  }

  return { format, redact, ...(rest[1] ? { filename: rest[1] } : {}) }
}

const isRemoteGateway = () => {
  const mode = ($connection.get() as { mode?: string } | null)?.mode

  return Boolean(mode && mode !== 'local')
}

const ROLLBACK_USAGE = 'usage: /rollback [list] | diff <n|hash> | <n|hash> [file] [--all]'
const HISTORY_PREVIEW = 400

function formatCheckpoints(result: { checkpoints?: CheckpointRow[]; enabled?: boolean } | null): string {
  if (result?.enabled === false) {
    return 'Checkpoints are not enabled for this session (set checkpoints.enabled in config.yaml).'
  }

  const rows = Array.isArray(result?.checkpoints) ? result.checkpoints : []

  if (!rows.length) {
    return 'No checkpoints yet for this working directory.'
  }

  return [
    'Checkpoints (newest first):',
    ...rows.map((row, index) =>
      `${index + 1}. ${String(row.hash ?? '').slice(0, 8)}  ${row.timestamp ?? ''}  ${row.message ?? ''}`.trimEnd()
    ),
    '',
    'Restore with /rollback <n> [file] (add --all to also overwrite your own edits); preview with /rollback diff <n>.'
  ].join('\n')
}

/** `/rollback` argument words → the owner verb and its params; null for a usage error. */
export function rollbackRequest(
  arg: string
): { method: 'rollback.diff' | 'rollback.list' | 'rollback.restore'; params: Record<string, unknown> } | null {
  const words = arg.trim().split(/\s+/).filter(Boolean)
  const all = words.includes('--all')
  const rest = words.filter(word => word !== '--all')
  const first = rest[0]?.toLowerCase() ?? ''

  if (!first || first === 'list' || first === 'ls') {
    return rest.length <= 1 && !all ? { method: 'rollback.list', params: {} } : null
  }

  if (first === 'diff') {
    return rest.length === 2 && !all ? { method: 'rollback.diff', params: { hash: rest[1] } } : null
  }

  const filePath = rest.slice(1).join(' ')

  return {
    method: 'rollback.restore',
    params: { hash: rest[0], ...(filePath ? { file_path: filePath } : {}), ...(all ? { all: true } : {}) }
  }
}

function formatRestore(result: RestoreResult | null): string {
  if (!result?.success) {
    return `Rollback failed: ${result?.error || 'unknown error'}`
  }

  const lines = [`Restored checkpoint ${result.restored_to ?? ''}.`.replace(' .', '.')]

  if (result.reason) {
    lines.push(result.reason)
  }

  if (result.history_removed) {
    lines.push(`Rewound ${result.history_removed} message(s) from this conversation.`)
  }

  return lines.join('\n')
}

interface SubagentRow {
  depth?: number
  goal?: string
  model?: string
  status?: string
  subagent_id?: string
  tool_count?: number
}

interface ProcessRow {
  command?: string
  session_id?: string
  status?: string
}

function formatAgents(subagents: SubagentRow[], processes: ProcessRow[]): string {
  const running = processes.filter(row => !row.status || row.status === 'running')

  if (!subagents.length && !running.length) {
    return 'No active subagents or background processes in this chat.'
  }

  const lines: string[] = []

  if (subagents.length) {
    lines.push(`Subagents (${subagents.length}):`)

    for (const row of subagents) {
      const indent = '  '.repeat(Math.max(0, Number(row.depth ?? 1) - 1))
      const tools = row.tool_count ? ` · ${row.tool_count} tool calls` : ''
      lines.push(`${indent}- [${row.status ?? 'running'}] ${row.goal || row.subagent_id || 'subagent'}${tools}`)
    }
  }

  if (running.length) {
    lines.push(`Background processes (${running.length}):`)

    for (const row of running) {
      lines.push(`- ${row.session_id ?? ''} ${row.command ?? ''}`.trimEnd())
    }
  }

  return lines.join('\n')
}

// A standalone `hermes serve` answers config.get key=full with the raw effective config, so the
// renderer fails closed: it prints only the sections the canonical owner projects
// (gateway/session_config.py::_client_config) and omits everything else. Keep in sync.
const CONFIG_ALLOWED: Record<string, true | readonly string[]> = {
  display: true,
  approvals: true,
  paste_collapse_threshold: true,
  paste_collapse_char_threshold: true,
  voice: ['record_key', 'submit_mode']
}

// Leaf rule of hermes_cli/config.py::_is_secret_config_key (+ auth/cookie), applied inside
// allowlisted sections (the owner masks these; a legacy server sends them raw).
const CONFIG_SECRET_KEY =
  /^(api_?key|key|auth|cookie|token|access_token|refresh_token|id_token|secret|client_secret|password|passwd|authorization|private_key|bearer|jwt)$|_(api_key|token|secret|password|key|access_key)$/

const secretConfigKey = (key: string) => CONFIG_SECRET_KEY.test(key.toLowerCase().replace(/-/g, '_'))

// `scheme://user:pass@host` -> `scheme://host`.
const stripUserinfo = (value: string) => value.replace(/([a-z][a-z\d+.-]*:\/\/)[^/?#\s@]*@/gi, '$1')

const scrubConfig = (key: string, value: unknown) =>
  key && secretConfigKey(key) ? undefined : typeof value === 'string' ? stripUserinfo(value) : value

function clientConfigProjection(config: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}

  for (const [key, allowed] of Object.entries(CONFIG_ALLOWED)) {
    const value = config[key]

    if (value === undefined) {
      continue
    }

    if (allowed === true) {
      out[key] = value
    } else if (value && typeof value === 'object' && !Array.isArray(value)) {
      const section = value as Record<string, unknown>
      out[key] = Object.fromEntries(allowed.filter(name => name in section).map(name => [name, section[name]]))
    }
  }

  return out
}

// `voice.record_key` is a keybinding the owner allowlists after masking, not a credential.
const CONFIG_NOT_SECRET = new Set(['voice.record_key'])

function yamlLines(value: unknown, indent = '', path = ''): string[] {
  if (value === null || typeof value !== 'object') {
    return [`${indent}${String(scrubConfig('', value))}`]
  }

  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    secretConfigKey(key) && !CONFIG_NOT_SECRET.has(`${path}${key}`)
      ? []
      : child !== null && typeof child === 'object' && !Array.isArray(child)
        ? Object.keys(child).length
          ? [`${indent}${key}:`, ...yamlLines(child, `${indent}  `, `${path}${key}.`)]
          : []
        : [
            `${indent}${key}: ${
              Array.isArray(child) ? JSON.stringify(child, scrubConfig) : String(scrubConfig('', child))
            }`
          ]
  )
}

/** `/history` text from the same public projection the transcript renders (archived turns shown,
 *  hidden/model-only rows skipped, display_content preferred). */
export function formatHistory(rows: SessionMessage[]): string {
  const turns = toChatMessages(rows).flatMap(message => {
    const text = chatMessageText(message).trim()

    if (message.hidden || !text || (message.role !== 'user' && message.role !== 'assistant')) {
      return []
    }

    const clipped = text.length > HISTORY_PREVIEW ? `${text.slice(0, HISTORY_PREVIEW)}…` : text

    return [`${message.role === 'user' ? 'You' : 'Hermes'}: ${clipped}`]
  })

  return turns.length ? [`Conversation history (${turns.length} messages):`, ...turns].join('\n') : 'No messages yet.'
}

export function desktopReadCommandHandlers<C extends { arg: string }>(env: DesktopReadCommandEnv<C>) {
  const { requestGateway, updateSessionState } = env

  return {
    rollback: async (ctx: C) => {
      const out = await env.withSlashOutput(ctx)

      if (!out) {
        return
      }

      const request = rollbackRequest(ctx.arg)

      if (!request) {
        return out.render(ROLLBACK_USAGE)
      }

      try {
        const result = await requestGateway<unknown>(request.method, { session_id: out.sessionId, ...request.params })

        if (request.method === 'rollback.list') {
          return out.render(formatCheckpoints(result as Parameters<typeof formatCheckpoints>[0]))
        }

        if (request.method === 'rollback.diff') {
          const diff = result as { diff?: string; stat?: string } | null

          return out.render([diff?.stat, diff?.diff].filter(Boolean).join('\n') || 'No changes since that checkpoint.')
        }

        const restore = result as RestoreResult | null
        let text = formatRestore(restore)

        // A whole-tree restore also rewound the conversation on the owner: reload the target
        // session's transcript so the removed turns leave the screen. File-only restores keep it.
        if (restore?.success && restore.history_removed && !request.params.file_path) {
          try {
            const snapshot = await requestGateway<{ messages?: SessionMessage[] }>('session.resume', {
              session_id: out.sessionId
            })

            if (Array.isArray(snapshot?.messages)) {
              const messages = toChatMessages(snapshot.messages)
              updateSessionState(out.sessionId, state => ({ ...state, messages }), out.storedSessionId)
            }
          } catch {
            text += '\nReopen this chat to see the rewound conversation.'
          }
        }

        out.render(text)
      } catch (err) {
        out.render(errorText(err))
      }
    },
    agents: async (ctx: C) => {
      const out = await env.withSlashOutput(ctx)

      if (!out) {
        return
      }

      try {
        const [subagents, processes] = await Promise.all([
          requestGateway<{ subagents?: SubagentRow[] }>('subagent.list', { session_id: out.sessionId }),
          requestGateway<{ processes?: ProcessRow[] }>('process.list', { session_id: out.sessionId })
        ])

        out.render(
          formatAgents(
            Array.isArray(subagents?.subagents) ? subagents.subagents : [],
            Array.isArray(processes?.processes) ? processes.processes : []
          )
        )
      } catch (err) {
        out.render(errorText(err))
      }
    },
    config: async (ctx: C) => {
      const out = await env.withSlashOutput(ctx)

      if (!out) {
        return
      }

      try {
        const result = await requestGateway<{ config?: unknown }>('config.get', {
          session_id: out.sessionId,
          key: 'full'
        })

        const body =
          result?.config && typeof result.config === 'object' && !Array.isArray(result.config)
            ? yamlLines(clientConfigProjection(result.config as Record<string, unknown>))
            : []

        out.render(
          [
            'Configuration (display, approvals, paste and voice settings; everything else is not shown):',
            ...(body.length ? body : ['(defaults)']),
            '',
            'Change settings in Settings, or edit config.yaml with `hermes config edit` on the gateway host.'
          ].join('\n')
        )
      } catch (err) {
        out.render(errorText(err))
      }
    },
    save: async (ctx: C) => {
      const out = await env.withSlashOutput(ctx)

      if (!out) {
        return
      }

      const request = saveRequest(ctx.arg)

      if (!request) {
        return out.render(SAVE_USAGE)
      }

      let file: { content?: string; filename?: string; format?: string } | null

      try {
        file = await requestGateway('session.export', {
          session_id: out.sessionId,
          format: request.format,
          ...(request.redact ? { redact: true } : {})
        })
      } catch (err) {
        if (!isMissingRpcMethod(err)) {
          return out.render(errorText(err))
        }

        // An older gateway without session.export: its session.save writes default-named, unredacted
        // JSON on the gateway host, so only a plain /save may fall back to it.
        if (request.redact || request.format !== 'json' || request.filename) {
          return out.render(
            'Not saved: this gateway cannot export a redacted, named or non-JSON copy. Update the gateway, or run /save with no options to write raw JSON on the gateway host.'
          )
        }

        try {
          const saved = await requestGateway<{ file?: string }>('session.save', { session_id: out.sessionId })

          return out.render(`Saved on the gateway host: ${saved?.file ?? '(unknown path)'}`)
        } catch (fallbackErr) {
          return out.render(errorText(fallbackErr))
        }
      }

      const save = window.hermesDesktop?.saveTextFileAs

      if (typeof file?.content !== 'string' || !save) {
        return out.render('error: this desktop build cannot save files')
      }

      try {
        const result = await save({
          content: file.content,
          defaultPath: request.filename || file.filename,
          filters: [SAVE_FILTERS[request.format]],
          title: 'Save conversation'
        })

        out.render(result?.saved && result.path ? `Saved conversation to ${result.path}` : 'Not saved (cancelled).')
      } catch (err) {
        out.render(errorText(err))
      }
    },
    export: async (ctx: C) => {
      if (isRemoteGateway()) {
        const out = await env.withSlashOutput(ctx)

        return out?.render(
          '/export packages a profile from the gateway host, which is not this machine. Run `hermes profile export` on that host.'
        )
      }

      await runExportProfileFlow(ctx.arg.trim().split(/\s+/)[0] || undefined)
    },
    import: async (ctx: C) => {
      if (isRemoteGateway()) {
        const out = await env.withSlashOutput(ctx)

        return out?.render(
          '/import installs a profile on the gateway host, which is not this machine. Run `hermes profile import <archive>` on that host.'
        )
      }

      await runImportProfileFlow()
    },
    timestamps: async (ctx: C) => {
      const word = ctx.arg.trim().toLowerCase()
      const current = $displayTimestamps.get()
      const next = word === 'on' ? true : word === 'off' ? false : word === '' || word === 'toggle' ? !current : current

      if (word && !['on', 'off', 'toggle', 'status'].includes(word)) {
        const out = await env.withSlashOutput(ctx)

        return out?.render('usage: /timestamps [on|off|status]')
      }

      setDisplayTimestampsWindowOverride(next)

      if (word === 'status') {
        const out = await env.withSlashOutput(ctx)
        out?.render(
          `Timestamps: ${next ? 'on' : 'off'} (this window; set display.timestamps in config.yaml to keep it)`
        )
      }
    },
    palette: async () => {
      openCommandPalette()
    },
    history: async (ctx: C) => {
      const out = await env.withSlashOutput(ctx)

      if (!out) {
        return
      }

      try {
        const snapshot = await requestGateway<{ messages?: SessionMessage[] }>('session.resume', {
          session_id: out.sessionId
        })

        out.render(formatHistory(Array.isArray(snapshot?.messages) ? snapshot.messages : []))
      } catch (err) {
        out.render(errorText(err))
      }
    }
  }
}
