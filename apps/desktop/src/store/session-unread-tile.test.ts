import { beforeEach, describe, expect, it } from 'vitest'

import * as model from '@/components/pane-shell/tree/model'
import { declareDefaultTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { createClientSessionState } from '@/lib/chat-runtime'

import { $selectedStoredSessionId, $unreadFinishedSessionIds } from './session'
import { publishSessionState } from './session-states'

// The completed-unread dot is keyed on the FOCUSED session, not the selected
// one. A tile is never $selectedStoredSessionId, so keying either half on the
// selection left a tiled session's dot green with no way to clear it.
//
// Static imports on purpose: re-evaluating this module graph per test
// (vi.resetModules) takes ~10s cold and blew the 10s hook timeout. Each test
// only needs fresh store state, which beforeEach resets directly.

describe('completed-unread dot follows the focused session', () => {
  let finishTurn: (storedSessionId: string) => void

  beforeEach(() => {
    for (const id of ['workspace', 'session-tile:tiled']) {
      registry.register({
        area: 'panes',
        data: id === 'workspace' ? { placement: 'main', uncloseable: true } : { placement: 'main' },
        id,
        render: () => null,
        title: id
      })
    }

    // The workspace holds the primary chat, a second zone holds the tile.
    declareDefaultTree(
      model.split('row', [
        model.group(['workspace'], { active: 'workspace', id: 'grp-main' }),
        model.group(['session-tile:tiled'], { active: 'session-tile:tiled', id: 'grp-tile' })
      ])
    )

    $unreadFinishedSessionIds.set([])
    $selectedStoredSessionId.set('primary')

    finishTurn = (storedSessionId: string) => {
      const working = { ...createClientSessionState(null), busy: true, storedSessionId }
      publishSessionState(`rt-${storedSessionId}`, working)
      publishSessionState(`rt-${storedSessionId}`, { ...working, busy: false })
    }
  })

  it('clears the dot when an already-open tile is fronted', () => {
    noteActiveTreeGroup('grp-main')
    finishTurn('tiled')
    expect($unreadFinishedSessionIds.get()).toEqual(['tiled'])

    // Fronting the tile is what a tab click does. Before the fix nothing on
    // this path cleared the marker, so the dot stayed green.
    noteActiveTreeGroup('grp-tile')
    expect($unreadFinishedSessionIds.get()).toEqual([])
  })

  it('never marks a tile that finishes while it is the focused one', () => {
    noteActiveTreeGroup('grp-tile')
    finishTurn('tiled')

    expect($unreadFinishedSessionIds.get()).toEqual([])
  })

  it('marks the primary session when a tile has focus', () => {
    noteActiveTreeGroup('grp-tile')
    finishTurn('primary')

    expect($unreadFinishedSessionIds.get()).toEqual(['primary'])
  })
})
