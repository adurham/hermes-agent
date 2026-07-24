import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// isPaneCloseable / closeTreePane('workspace') resolution — the workspace
// pane is structurally uncloseable (dock anchor, drag payload target) but
// wiring.tsx registers a real closer for it (promote-or-reset). These tests
// cover the store-level contract that closer depends on: before a closer is
// registered, workspace must read as NOT closeable everywhere (× hidden,
// ⌘W no-ops, right-click Close hidden); once one is registered, it must read
// as closeable and closeTreePane must route to it instead of falling through
// to the generic dismiss-from-tree path (which would rip the anchor pane out
// of the layout — see removePane in model.ts).

describe('workspace pane close resolution', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  afterEach(() => {
    vi.resetModules()
  })

  it('reads as not closeable with no registered closer, then closeable once one is registered', async () => {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')

    registry.register({
      id: 'workspace',
      area: 'panes',
      title: 'New session',
      data: { placement: 'main', uncloseable: true },
      render: () => null
    })

    expect(tree.isPaneCloseable('workspace')).toBe(false)

    const closer = vi.fn()
    tree.registerPaneCloser('workspace', closer)

    expect(tree.isPaneCloseable('workspace')).toBe(true)
  })

  it('closeTreePane routes workspace through its registered closer, never the generic dismiss path', async () => {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')

    registry.register({
      id: 'workspace',
      area: 'panes',
      title: 'New session',
      data: { placement: 'main', uncloseable: true },
      render: () => null
    })

    const singlePaneTree = model.group(['workspace'], { id: 'grp-main' })

    tree.declareDefaultTree(singlePaneTree)
    tree.$layoutTree.set(singlePaneTree)

    const closer = vi.fn()
    tree.registerPaneCloser('workspace', closer)

    tree.closeTreePane('workspace')

    expect(closer).toHaveBeenCalledTimes(1)
    // The pane must still be in the tree — a real dismiss would have removed
    // it, which is exactly what the registered closer exists to prevent.
    expect(model.findGroupOfPane(tree.$layoutTree.get()!, 'workspace')).not.toBeNull()
  })

  it('⌘W (closeWorkspaceTab) no-ops with no registered closer, fires once one exists', async () => {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')

    registry.register({
      id: 'workspace',
      area: 'panes',
      title: 'New session',
      data: { placement: 'main', uncloseable: true },
      render: () => null
    })

    const singlePaneTree = model.group(['workspace'], { id: 'grp-main' })

    tree.declareDefaultTree(singlePaneTree)
    tree.$layoutTree.set(singlePaneTree)

    expect(tree.closeWorkspaceTab()).toBe(false)

    const closer = vi.fn()
    tree.registerPaneCloser('workspace', closer)

    expect(tree.closeWorkspaceTab()).toBe(true)
    expect(closer).toHaveBeenCalledTimes(1)
  })

  it('"Close all" includes workspace once a closer is registered', async () => {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')

    registry.register({
      id: 'workspace',
      area: 'panes',
      title: 'New session',
      data: { placement: 'main', uncloseable: true },
      render: () => null
    })
    registry.register({
      id: 'session-tile:abc',
      area: 'panes',
      title: 'Other session',
      data: { placement: 'main' },
      render: () => null
    })

    const stackedTree = model.group(['workspace', 'session-tile:abc'], { id: 'grp-main', active: 'workspace' })

    tree.declareDefaultTree(stackedTree)
    tree.$layoutTree.set(stackedTree)

    expect(tree.treeTabCloseTargets('workspace').all).toBe(1)

    const closer = vi.fn()
    tree.registerPaneCloser('workspace', closer)

    expect(tree.treeTabCloseTargets('workspace').all).toBe(2)
  })
})
