/**
 * Which pane renders a runtime session, and whether that pane is on screen.
 *
 * The stream flush scheduler (use-message-stream) is not a component, so it
 * cannot read `PaneVisibleContext`; it has to answer the same question from the
 * layout tree. `isPaneVisible` is the one truthful answer already in the tree
 * store — in the tree, not dismissed, not chrome-hidden, its zone un-minimized,
 * and holding its stack's active slot — so this module only adds the
 * runtime-session → pane-id hop and the fail-safe third state.
 */

import { findGroupOfPane, type LayoutNode } from '@/components/pane-shell/tree/model'
import {
  $dismissedPanes,
  $hiddenTreePanes,
  $layoutTree,
  isPaneVisible
} from '@/components/pane-shell/tree/store'
import { TILE_PANE_PREFIX } from '@/store/session-focus'
import { $sessionTiles } from '@/store/session-states'

import type { StreamPaneVisibility } from './utils'

/** The pane that holds the PRIMARY chat. A session tile is
 *  `session-tile:<storedId>` (see session-tile.tsx / session-focus.ts). */
const PRIMARY_PANE = 'workspace'

interface VisibilityDeps {
  isVisible?: (paneId: string) => boolean
  tiles?: readonly { runtimeId?: null | string; storedSessionId: string }[]
  tree?: LayoutNode | null
}

/** Every pane id that could be showing `runtimeId` right now. A session can be
 *  the primary chat AND a tile at the same time, so the answer is a list. */
export function streamSessionPaneIds(
  runtimeId: string,
  primaryRuntimeId: null | string,
  tiles: readonly { runtimeId?: null | string; storedSessionId: string }[] = $sessionTiles.get()
): string[] {
  return [
    ...(primaryRuntimeId && runtimeId === primaryRuntimeId ? [PRIMARY_PANE] : []),
    ...tiles
      .filter(tile => Boolean(runtimeId) && tile.runtimeId === runtimeId)
      .map(tile => `${TILE_PANE_PREFIX}${tile.storedSessionId}`)
  ]
}

/**
 * Is this session's transcript on screen?
 *
 * `unknown` — no pane in the layout tree claims this session — is NOT treated
 * as hidden: secondary windows, plain routes and a session that only streams
 * into its cache have no pane, and slowing a surface we cannot locate would be
 * a visible regression. Only a pane that provably exists and is provably not
 * fronted returns `hidden`.
 */
export function streamSessionPaneVisibility(
  runtimeId: string,
  primaryRuntimeId: null | string,
  { isVisible = isPaneVisible, tiles = $sessionTiles.get(), tree = $layoutTree.get() }: VisibilityDeps = {}
): StreamPaneVisibility {
  const paneIds = streamSessionPaneIds(runtimeId, primaryRuntimeId, tiles)
  const inTree = tree ? paneIds.filter(paneId => findGroupOfPane(tree, paneId)) : []

  if (inTree.length === 0) {
    return 'unknown'
  }

  return inTree.some(paneId => isVisible(paneId)) ? 'visible' : 'hidden'
}

/** Fire `listener` whenever a pane could have changed visibility: the tree
 *  shape/active tab, the dismissed + chrome-hidden sets, or a tile binding a
 *  runtime id. Called once by the flush scheduler to flush a revealed session
 *  immediately instead of waiting out its parked budget. */
export function subscribeStreamPaneVisibility(listener: () => void): () => void {
  const offs = [$layoutTree, $dismissedPanes, $hiddenTreePanes, $sessionTiles].map(atom => atom.listen(listener))

  return () => offs.forEach(off => off())
}
