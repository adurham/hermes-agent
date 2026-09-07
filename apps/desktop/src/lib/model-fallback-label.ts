/**
 * Fallback-model display helper (desktop side).
 *
 * A subagent can silently fail over from the model it was dispatched with to
 * a fallback model/provider — the live subagent store keeps tracking
 * progress, so without an explicit indicator the UI just shows the
 * *configured* model forever, even after every API call switched providers.
 * Every render site that shows a subagent's model must route through here
 * so the glyph and wording stay identical across the app.
 *
 * House style (matches the Python side and ui-tui):
 *   - Not in fallback: render the model exactly as today, no visual change.
 *   - In fallback: `⚠` prefix, verbose form
 *     `⚠ claude-opus-5 (fallback from glm-5.3)`, compact form
 *     `⚠ glm-5.3→claude-opus-5` where horizontal space is tight.
 *
 * Keep in sync with ui-tui/src/lib/modelFallback.ts — same contract,
 * separate build trees, intentionally not shared code.
 */

import { displayModelName } from '@/lib/model-status-label'

export interface FallbackModelInfo {
  fallbackActive?: boolean
  model?: string
  /** Backend-prerendered display string. Preferred over local composition when present. */
  modelLabel?: string
  primaryModel?: null | string
}

const FALLBACK_GLYPH = '⚠'

/**
 * Verbose label: `⚠ Opus 5 (fallback from GLM 5.3)`, or the plain
 * `displayModelName()` output when not in fallback (or when the fields
 * needed to prove fallback are absent — an un-upgraded backend must never
 * render a stray glyph). Always routes both names through
 * `displayModelName()` so the fallback annotation never bypasses the
 * normalization every other surface uses.
 */
export function fallbackModelLabel(info: FallbackModelInfo): string {
  if (info.modelLabel) {
    return info.modelLabel
  }

  const model = info.model ?? ''

  if (!model) {
    return ''
  }

  const name = displayModelName(model)

  if (!info.fallbackActive || !info.primaryModel) {
    return name
  }

  return `${FALLBACK_GLYPH} ${name} (fallback from ${displayModelName(info.primaryModel)})`
}

/**
 * Compact label for width-constrained rows: `⚠ GLM 5.3→Opus 5`, or the plain
 * `displayModelName()` output when not in fallback.
 */
export function fallbackModelLabelCompact(info: FallbackModelInfo): string {
  const model = info.model ?? ''

  if (!model) {
    return ''
  }

  const name = displayModelName(model)

  if (!info.fallbackActive || !info.primaryModel) {
    return name
  }

  return `${FALLBACK_GLYPH} ${displayModelName(info.primaryModel)}→${name}`
}
