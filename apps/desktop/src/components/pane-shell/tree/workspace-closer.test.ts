import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The workspace pane is structurally uncloseable (dock anchor, drag payload
// target, `findGroupOfPane(tree, 'workspace')` assumed live everywhere), but
// wiring.tsx registers a real closer for it (`closeWorkspaceTab`'s
// promote-or-reset). This guards the anchor invariant that closer depends on:
// `closeTreePane('workspace')` must route through the registered closer and
// never fall through to the generic dismiss-from-tree path, which would rip
// the anchor pane out of the layout (see removePane in model.ts).

describe('workspace pane close resolution', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  afterEach(() => {
    vi.resetModules()
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
})
