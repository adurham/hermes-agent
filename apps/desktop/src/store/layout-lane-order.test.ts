import { afterEach, describe, expect, it } from 'vitest'

import {
  $sidebarLaneSessionOrderIds,
  pruneSidebarLaneSessionOrderIds,
  setSidebarLaneSessionOrderIds
} from '@/store/layout'

// Per-lane session drag order: each workspace lane (repo/branch/worktree)
// persists its own manual session order into ONE shared map, keyed by lane
// id, so lanes reorder independently without a store atom per lane.
describe('sidebar lane session order', () => {
  afterEach(() => {
    $sidebarLaneSessionOrderIds.set({})
  })

  it('starts empty', () => {
    expect($sidebarLaneSessionOrderIds.get()).toEqual({})
  })

  it('persists a lane order under its own key without touching other lanes', () => {
    setSidebarLaneSessionOrderIds('lane-a', ['s2', 's1'])
    setSidebarLaneSessionOrderIds('lane-b', ['s3', 's4'])

    expect($sidebarLaneSessionOrderIds.get()).toEqual({
      'lane-a': ['s2', 's1'],
      'lane-b': ['s3', 's4']
    })
  })

  it('removes the entry entirely when set to an empty order (no orphaned empty arrays)', () => {
    setSidebarLaneSessionOrderIds('lane-a', ['s2', 's1'])
    setSidebarLaneSessionOrderIds('lane-a', [])

    expect($sidebarLaneSessionOrderIds.get()).toEqual({})
  })

  it('is a no-op (keeps the same reference) when the order is unchanged', () => {
    setSidebarLaneSessionOrderIds('lane-a', ['s1', 's2'])

    const before = $sidebarLaneSessionOrderIds.get()

    setSidebarLaneSessionOrderIds('lane-a', ['s1', 's2'])

    expect($sidebarLaneSessionOrderIds.get()).toBe(before)
  })

  it('prunes entries for lanes no longer present, keeping live ones', () => {
    setSidebarLaneSessionOrderIds('live', ['s1'])
    setSidebarLaneSessionOrderIds('gone', ['s2'])

    pruneSidebarLaneSessionOrderIds(new Set(['live']))

    expect($sidebarLaneSessionOrderIds.get()).toEqual({ live: ['s1'] })
  })

  it('no-ops when every persisted lane is still live', () => {
    setSidebarLaneSessionOrderIds('live', ['s1'])

    const before = $sidebarLaneSessionOrderIds.get()

    pruneSidebarLaneSessionOrderIds(new Set(['live']))

    expect($sidebarLaneSessionOrderIds.get()).toBe(before)
  })
})
