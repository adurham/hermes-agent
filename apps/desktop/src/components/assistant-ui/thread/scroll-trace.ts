/**
 * TEMPORARY diagnostic: per-session scroll memory (reading position) trace.
 *
 * Shipped in one build only, to capture WHY a tab round-trip sometimes loses the
 * reader's position. `console.error` is the only renderer console level the
 * desktop main process forwards to `~/.hermes/logs/desktop.log`
 * (see `electron/renderer-log.ts`), so every line here is an error-level
 * console call carrying a `[scroll-trace]` prefix.
 *
 * Remove this file and its call sites in `list.tsx` once the fix has landed.
 */

export const SCROLL_TRACE_ENABLED = true

/** One line per decision point. Never throws, never allocates in a hot loop. */
export function scrollTrace(event: string, data: Record<string, unknown>): void {
  if (!SCROLL_TRACE_ENABLED) {
    return
  }

  try {
    console.error(`[scroll-trace] ${event} ${JSON.stringify(data)}`)
  } catch {
    // Diagnostics must never break the transcript (circular data, exotic values).
  }
}
