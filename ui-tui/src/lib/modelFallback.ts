/**
 * Fallback-model display helper (ui-tui side).
 *
 * A subagent can silently fail over from the model it was dispatched with to
 * a fallback model/provider — the gateway keeps sending live progress, so
 * without an explicit indicator the UI just shows the *configured* model
 * forever, even after every API call switched providers. Every render site
 * that shows a subagent's model must route through here so the glyph and
 * wording stay identical across the codebase.
 *
 * House style (matches the Python side):
 *   - Not in fallback: render the model exactly as today, no visual change.
 *   - In fallback: `⚠` prefix, verbose form
 *     `⚠ claude-opus-5 (fallback from glm-5.3)`, compact form
 *     `⚠ glm-5.3→claude-opus-5` where horizontal space is tight.
 *
 * Keep in sync with apps/desktop/src/lib/modelFallback.ts — same contract,
 * separate build trees, intentionally not shared code.
 */

export interface FallbackModelInfo {
  fallbackActive?: boolean
  model?: string
  /** Backend-prerendered display string. Preferred over local composition when present. */
  modelLabel?: string
  primaryModel?: null | string
}

const FALLBACK_GLYPH = '⚠'

/**
 * Verbose label: `⚠ claude-opus-5 (fallback from glm-5.3)`, or a plain model
 * name when not in fallback (or when the fields needed to prove fallback are
 * absent — an un-upgraded gateway must never render a stray glyph).
 *
 * Prefers the backend's prerendered `modelLabel` when present so the two
 * renderers can't drift; falls back to local composition from the
 * structured fields so the UI still works if only those arrive.
 */
export function fallbackModelLabel(info: FallbackModelInfo): string {
  if (info.modelLabel) {
    return info.modelLabel
  }

  const model = info.model ?? ''

  if (!info.fallbackActive || !info.primaryModel || !model) {
    return model
  }

  return `${FALLBACK_GLYPH} ${model} (fallback from ${info.primaryModel})`
}

/**
 * Compact label for width-constrained rows: `⚠ glm-5.3→claude-opus-5`, or a
 * plain model name when not in fallback.
 */
export function fallbackModelLabelCompact(info: FallbackModelInfo): string {
  const model = info.model ?? ''

  if (!info.fallbackActive || !info.primaryModel || !model) {
    return model
  }

  return `${FALLBACK_GLYPH} ${info.primaryModel}→${model}`
}
