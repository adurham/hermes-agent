import type { Usage } from '@hermes/shared/gateway-events'

export const ZERO: Usage = { calls: 0, input: 0, output: 0, total: 0 }

const num = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined

/** A canonical local viewer's `session.usage` tick carries the owner's session-cumulative counters at
 * the top level (`gateway/session_usage.py::usage_stats`, the `session.stats` dict); main's sidecar
 * ticker sent its `_get_usage` shape under `usage`. Both land as the same status-bar `Usage`
 * (`session_usage.tui_usage` is the Python twin used for `message.complete.usage`). */
export function usageFromTick(payload: unknown): Partial<Usage> | undefined {
  if (!payload || typeof payload !== 'object') {
    return undefined
  }

  const p = payload as Record<string, unknown>

  if (p.usage && typeof p.usage === 'object') {
    return p.usage as Partial<Usage>
  }

  if (num(p.api_calls) === undefined && num(p.total_tokens) === undefined) {
    return undefined
  }

  const usage: Partial<Usage> = {
    calls: num(p.api_calls) ?? 0,
    completion: num(p.completion_tokens) ?? 0,
    input: num(p.input_tokens) || num(p.prompt_tokens) || 0,
    output: num(p.output_tokens) || num(p.completion_tokens) || 0,
    prompt: num(p.prompt_tokens) ?? 0,
    reasoning: num(p.reasoning_tokens) ?? 0,
    total: num(p.total_tokens) ?? 0
  }

  if (typeof p.model === 'string' && p.model) {
    usage.model = p.model
  }

  const used = num(p.last_prompt_tokens) ?? 0
  const max = num(p.context_length) ?? 0

  if (used && max) {
    const source = typeof p.context_source === 'string' && p.context_source ? p.context_source : 'provider_usage'
    const clamped = Math.min(used, max)

    Object.assign(usage, {
      context_estimated: source !== 'provider_usage',
      context_max: max,
      context_percent: Math.max(0, Math.min(100, Math.round((clamped / max) * 100))),
      context_source: source,
      context_used: clamped
    })
  }

  if (p.context_length !== undefined && p.context_length !== null) {
    usage.compressions = num(p.compression_count) ?? 0
  }

  const promptTotal = num(p.prompt_tokens) ?? 0
  const cacheRead = num(p.cache_read_tokens) ?? 0

  if (promptTotal > 0 && cacheRead > 0) {
    usage.cache_hit_pct = Math.max(0, Math.min(100, Math.round((cacheRead / promptTotal) * 100)))
  }

  const latency = num(p.avg_latency_s)
  const tps = num(p.tokens_per_s)

  if (latency !== undefined) {
    usage.avg_latency_s = Math.round(latency * 10) / 10
  }

  if (tps !== undefined) {
    usage.avg_tps = Math.round(tps * 10) / 10
  }

  if (num(p.active_subagents) !== undefined) {
    usage.active_subagents = num(p.active_subagents)
  }

  return usage
}
