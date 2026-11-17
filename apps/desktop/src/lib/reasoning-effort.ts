import { DEFAULT_REASONING_EFFORT, isReasoningEffort, type ReasoningEffort } from '@hermes/shared'

import { normalize } from '@/lib/text'

/** Compact labels for chrome where space is tight (pill, picker rows). Menus
 *  and settings use the translated `shell.modelOptions` strings instead. */
const SHORT_LABELS: Record<string, string> = {
  none: 'Off',
  minimal: 'Min',
  low: 'Low',
  medium: 'Med',
  high: 'High',
  xhigh: 'XHigh',
  max: 'Max',
  ultra: 'Ultra'
}

/**
 * A pick the route does not send verbatim: `ultra` is a Hermes-internal step
 * that routes clamp to their strongest level (`max` on OpenAI-compatible wires; the Codex
 * app-server sends it verbatim as codex's own mode), and the
 * CLI's `/reasoning` says so ("ultra (sends max on this route)"). The wire
 * level comes from the gateway's `session.info.reasoning_effort_wire`; nothing
 * is inferred client-side, so an unknown ('' — not yet stamped, or an
 * optimistic pick) or verbatim wire reads as "no clamp".
 */
export function reasoningEffortClamp(
  effort: string,
  wire: string | undefined
): { effort: ReasoningEffort; wire: ReasoningEffort } | null {
  const picked = normalize(effort)
  const sent = normalize(wire ?? '')

  if (!sent || sent === picked || !isReasoningEffort(picked) || !isReasoningEffort(sent)) {
    return null
  }

  return { effort: picked, wire: sent }
}

/** Compact label; a clamped pick shows both ends ("Ultra→Max") so the pill
 *  never presents a Hermes step as a wire level the route does not have. */
export function reasoningEffortLabel(effort: string, wire?: string): string {
  const key = normalize(effort)
  const clamp = reasoningEffortClamp(effort, wire)

  if (clamp) {
    return `${SHORT_LABELS[clamp.effort]}→${SHORT_LABELS[clamp.wire]}`
  }

  return key ? (SHORT_LABELS[key] ?? effort) : ''
}

/** Thinking is on unless a level explicitly says otherwise; an empty value
 *  means "inherit", so it resolves through `fallback` first. */
export const isThinkingEnabled = (effort: string, fallback: string = DEFAULT_REASONING_EFFORT): boolean =>
  normalize(effort || fallback) !== 'none'

/** The level a scale control should show. Empty inherits `fallback`; `none`
 *  (thinking off) selects nothing; anything unrecognized clamps to the default. */
export function resolveReasoningEffort(effort: string, fallback: string = DEFAULT_REASONING_EFFORT): string {
  const value = normalize(effort || fallback)

  if (value === 'none') {
    return ''
  }

  return isReasoningEffort(value) ? value : DEFAULT_REASONING_EFFORT
}

/** Levels an ollama.com route never had, and the level it runs instead.
 *  `ultra` is Hermes-internal (no wire takes it) and `xhigh` is not one of that endpoint's
 *  five words — it answers 200 and runs the server default. */
const OLLAMA_CLOUD_LEGACY_EFFORT: Record<string, string> = { ultra: 'max', xhigh: 'max' }

/** Rewrite a sticky pick recorded before `provider` declared its vocabulary.
 *
 *  Composer selections and per-model presets outlive a deploy and both outrank the profile
 *  default for a new session, so without this a fresh chat seeds a level its own menu cannot
 *  offer (`XHigh` on a route whose picker only has low/medium/high/max). */
export function migrateOllamaCloudEffort(provider: string, effort: string): string {
  return provider.startsWith('ollama-cloud') ? (OLLAMA_CLOUD_LEGACY_EFFORT[effort] ?? effort) : effort
}
