import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { StarmapGraph } from '@/types/hermes'

const { fetches } = vi.hoisted(() => ({
  fetches: [] as Array<{ reject: (error: unknown) => void; resolve: (graph: StarmapGraph) => void }>
}))

vi.mock('@/hermes', () => ({
  getStarmapGraph: () =>
    new Promise<StarmapGraph>((resolve, reject) => {
      fetches.push({ reject, resolve })
    })
}))

const { $starmapError, $starmapGraph, $starmapLoading, loadStarmapGraph, resetStarmapGraph } =
  await import('./starmap')

const graph = (id: string): StarmapGraph => ({ edges: [], nodes: [{ id }] }) as unknown as StarmapGraph

beforeEach(async () => {
  // Settle leftovers from a previous test so its flight cannot write here.
  while (fetches.length) {
    fetches.shift()?.resolve(graph('leftover'))
    await new Promise(resolve => setTimeout(resolve, 0))
  }

  resetStarmapGraph()
  $starmapLoading.set(false)
})

describe('loadStarmapGraph', () => {
  it('drops the previous profile graph when a reset lands mid-load', async () => {
    const previous = loadStarmapGraph()
    resetStarmapGraph()
    const current = loadStarmapGraph()

    expect(fetches).toHaveLength(2)
    fetches[1].resolve(graph('current-node'))
    await current
    fetches[0].resolve(graph('previous-node'))
    await previous

    expect($starmapGraph.get()?.nodes.map(node => node.id)).toEqual(['current-node'])
    expect($starmapLoading.get()).toBe(false)
  })

  it('keeps the newer in-flight slot when the stranded load settles first', async () => {
    const previous = loadStarmapGraph()
    resetStarmapGraph()
    const current = loadStarmapGraph()

    fetches[0].reject(new Error('previous profile went away'))
    await previous

    expect($starmapError.get()).toBeNull()
    expect($starmapLoading.get()).toBe(true)
    // A caller arriving now joins the current profile's load, not a new fetch.
    void loadStarmapGraph()
    expect(fetches).toHaveLength(2)

    fetches[1].resolve(graph('current-node'))
    await current

    expect($starmapGraph.get()?.nodes.map(node => node.id)).toEqual(['current-node'])
    expect($starmapLoading.get()).toBe(false)
  })

  it('serves the cached graph without refetching', async () => {
    const pending = loadStarmapGraph()
    fetches[0].resolve(graph('cached-node'))
    await pending

    await loadStarmapGraph()

    expect(fetches).toHaveLength(1)
    expect($starmapGraph.get()?.nodes.map(node => node.id)).toEqual(['cached-node'])
  })
})
