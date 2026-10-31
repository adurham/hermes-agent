import { describe, expect, it } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'

import { streamSessionPaneIds, streamSessionPaneVisibility } from './session-pane-visibility'

const PRIMARY = 'runtime-primary'
const TILE_RUNTIME = 'runtime-tile'
const TILE_PANE = 'session-tile:stored-tile'

const tiles = [{ runtimeId: TILE_RUNTIME, storedSessionId: 'stored-tile' }]

const tree = (active: string) => group(['workspace', TILE_PANE], { active, id: 'grp-main' })

describe('streamSessionPaneIds', () => {
  it('maps the primary to the workspace pane and a runtime to its tile pane', () => {
    expect(streamSessionPaneIds(PRIMARY, PRIMARY, tiles)).toContain('workspace')
    expect(streamSessionPaneIds(TILE_RUNTIME, PRIMARY, tiles)).toEqual([TILE_PANE])
  })

  it('lists BOTH surfaces when the same session is primary and tiled', () => {
    const both = [{ runtimeId: PRIMARY, storedSessionId: 'stored-tile' }]

    expect(streamSessionPaneIds(PRIMARY, PRIMARY, both)).toEqual(['workspace', TILE_PANE])
  })

  it('ignores unbound tiles and a missing primary', () => {
    expect(streamSessionPaneIds(TILE_RUNTIME, null, [{ storedSessionId: 'stored-tile' }])).toEqual([])
    expect(streamSessionPaneIds('', null, tiles)).toEqual([])
  })
})

describe('streamSessionPaneVisibility', () => {
  it('reports a session in the active tab as visible', () => {
    expect(
      streamSessionPaneVisibility(TILE_RUNTIME, PRIMARY, { isVisible: () => true, tiles, tree: tree(TILE_PANE) })
    ).toBe('visible')
  })

  it('reports a session whose pane exists but is behind another tab as hidden', () => {
    // The zone is showing the primary chat; the tile's pane is kept mounted
    // (visibility: hidden) and must not drive 30fps of markdown re-parses.
    expect(
      streamSessionPaneVisibility(TILE_RUNTIME, PRIMARY, { isVisible: () => false, tiles, tree: tree('workspace') })
    ).toBe('hidden')
  })

  it('treats "no pane in the tree" as unknown, never as hidden', () => {
    // Secondary windows, plain routes, a tile whose pane is not docked yet: a
    // surface we cannot locate must keep its normal cadence.
    expect(streamSessionPaneVisibility(TILE_RUNTIME, PRIMARY, { isVisible: () => false, tiles, tree: null })).toBe(
      'unknown'
    )
    expect(
      streamSessionPaneVisibility(TILE_RUNTIME, PRIMARY, {
        isVisible: () => false,
        tiles: [{ runtimeId: TILE_RUNTIME, storedSessionId: 'never-docked' }],
        tree: group(['workspace'], { active: 'workspace', id: 'grp-main' })
      })
    ).toBe('unknown')
  })

  it('is visible when ANY of its panes is on screen', () => {
    const both = [{ runtimeId: PRIMARY, storedSessionId: 'stored-tile' }]

    const zones = split(
      'row',
      [
        group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        group([TILE_PANE], { active: TILE_PANE, id: 'grp-tile' })
      ],
      [1, 1],
      'split-main'
    )

    expect(
      streamSessionPaneVisibility(PRIMARY, PRIMARY, {
        isVisible: paneId => paneId === TILE_PANE,
        tiles: both,
        tree: zones
      })
    ).toBe('visible')
  })
})
