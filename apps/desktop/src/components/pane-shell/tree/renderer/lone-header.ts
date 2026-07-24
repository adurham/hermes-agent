/**
 * When a lone pane must keep its tab strip (name card + close).
 *
 * Default: a single pane isn't a "tab", so the header auto-hides. Exceptions
 * force it on so a closeable surface never becomes an unclosable dead zone:
 *  - session tiles (`session-tile:*`) — even before chrome registers
 *  - any `placement: 'main'` contribution (workspace included — it always
 *    has a registered closer, see `registerPaneCloser('workspace', ...)` in
 *    wiring.tsx, so it is ALWAYS effectively closeable and must always show
 *    its tab so there's a ✕ to click; the old "clean no-tab default for a
 *    bare workspace" exception was removed once workspace got real
 *    browser-tab close semantics — a hidden header meant no ✕ existed at
 *    all, so the lone open tab could never be closed by mouse)
 *  - a collapse tool panel dragged into its own zone
 */

export interface LoneHeaderChrome {
  placement?: string
  uncloseable?: boolean
}

export function forceLoneHeaderForPanes(
  shown: readonly string[],
  chromeOf: (id: string) => LoneHeaderChrome,
  isCollapsePane: (id: string) => boolean
): boolean {
  if (shown.some(id => id.startsWith('session-tile:'))) {
    return true
  }

  if (shown.some(id => chromeOf(id).placement === 'main')) {
    return true
  }

  return shown.length === 1 && isCollapsePane(shown[0])
}
