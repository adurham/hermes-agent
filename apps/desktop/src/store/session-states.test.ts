import { afterEach, describe, expect, it, vi } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import { $selectedStoredSessionId } from '@/store/session'
import type { SessionTile } from '@/store/session-states'
import { $sessionTiles, goToSession, orderTilesByTree, selectionHomesToWorkspace } from '@/store/session-states'

const tile = (storedSessionId: string): SessionTile => ({ storedSessionId })
const tilePane = (id: string) => `session-tile:${id}`

describe('orderTilesByTree', () => {
  it('no-ops (null) without a tree or below two tiles', () => {
    expect(orderTilesByTree(null, [tile('a'), tile('b')])).toBeNull()
    expect(orderTilesByTree(group([tilePane('a')]), [tile('a')])).toBeNull()
  })

  it('reorders tiles to layout-tree encounter order across a split', () => {
    const tree = split('row', [group(['workspace', tilePane('b')]), group([tilePane('a')])])

    expect(orderTilesByTree(tree, [tile('a'), tile('b')])).toEqual([tile('b'), tile('a')])
  })

  it('returns null when the array already matches strip order (skip persist)', () => {
    const tree = split('row', [group([tilePane('b')]), group([tilePane('a')])])

    expect(orderTilesByTree(tree, [tile('b'), tile('a')])).toBeNull()
  })

  it('sorts not-yet-adopted tiles after placed ones, stably', () => {
    const tree = group(['workspace', tilePane('b')])

    expect(orderTilesByTree(tree, [tile('a'), tile('b'), tile('c')])).toEqual([tile('b'), tile('a'), tile('c')])
  })
})

describe('selectionHomesToWorkspace', () => {
  const tiles = [tile('a'), tile('b')]

  it('homes for a null selection or a non-tile session', () => {
    expect(selectionHomesToWorkspace(null, tiles)).toBe(true)
    expect(selectionHomesToWorkspace('c', tiles)).toBe(true)
  })

  it('skips homing when the selected id is already an open tile', () => {
    expect(selectionHomesToWorkspace('a', tiles)).toBe(false)
  })
})

describe('goToSession', () => {
  afterEach(() => {
    $sessionTiles.set([])
    $selectedStoredSessionId.set(null)
  })

  // Regression coverage for the duplicate-tab bug: any navigation entry point
  // (session switcher, hotkeys, command palette, notifications, cron/command
  // center) that bypasses this dedup check produces two tabs for one session —
  // one tile tab + one workspace tab — because the workspace pane renders
  // whatever $selectedStoredSessionId points to regardless of $sessionTiles.
  it('fronts the existing tile instead of navigating when the session is already tiled', () => {
    $sessionTiles.set([tile('already-open')])
    const navigate = vi.fn()

    goToSession(navigate, 'already-open')

    expect(navigate).not.toHaveBeenCalled()
  })

  it('fronts the workspace instead of re-navigating when the session is already the main session', () => {
    $selectedStoredSessionId.set('main-session')
    const navigate = vi.fn()

    goToSession(navigate, 'main-session')

    expect(navigate).not.toHaveBeenCalled()
  })

  it('navigates when the session has no open tab yet', () => {
    const navigate = vi.fn()

    goToSession(navigate, 'fresh-session')

    expect(navigate).toHaveBeenCalledWith('/fresh-session', undefined)
  })

  it('forwards navigate options (e.g. replace) on the fallback path', () => {
    const navigate = vi.fn()

    goToSession(navigate, 'fresh-session', { replace: true })

    expect(navigate).toHaveBeenCalledWith('/fresh-session', { replace: true })
  })
})
