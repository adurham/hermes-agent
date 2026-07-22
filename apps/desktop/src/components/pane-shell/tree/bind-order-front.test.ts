import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Ground-truth repro for "opening the desktop app with the Terminal-deck
// layout shows the logs tab instead of terminal".
//
// terminal and logs are both TOOL PANES bound through controller.tsx's
// `bindPaneCollapse(paneId, $open, close, open)`, and they can share ONE
// zone (tab stack) — e.g. after using the Quad preset, which places them
// together. Each pane's visibility is its OWN independently persisted
// boolean store ($terminalTakeover, $logsOpen). Both can be persisted
// `true` from earlier interaction in the SAME session/history (e.g. the
// user toggled logs on once via ⌘K "Toggle logs", then later used the
// terminal — both flags end up true, only the tree's `active` field records
// which tab was actually last looked at).
//
// `bindPaneCollapse` used to call `setPaneCollapsed(paneId, !$open.get())`
// on mount UNCONDITIONALLY for every tool pane — and that call always fronts
// the pane when open=true. controller.tsx binds `terminal` BEFORE `logs`, so
// on every boot, `logs`'s mount-time sync ran last and always re-fronted
// logs over terminal — regardless of the persisted tree's own `active`
// field, regardless of which preset (e.g. Terminal deck) the user actually
// has selected. The fix: the mount-time sync passes `front: false`, so it
// only reconciles minimized/visible state, never steals the active tab from
// whatever the persisted tree already says.

describe('bindPaneCollapse-style mount sync does not let bind order pick the active tab', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    vi.resetModules()
  })

  it('terminal stays active on mount even though logs (bound later) is also persisted open', async () => {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')

    registry.register({
      id: 'terminal',
      area: 'panes',
      title: 'terminal',
      data: { placement: 'bottom' },
      render: () => null
    })
    registry.register({
      id: 'logs',
      area: 'panes',
      title: 'logs',
      data: { placement: 'bottom' },
      render: () => null
    })

    // Terminal deck: terminal+logs stacked in one zone, tree's own `active`
    // says terminal (this is what a real persisted tree would carry forward
    // from the last time the user actually looked at the terminal tab).
    const terminalDeckTree = model.split(
      'column',
      [
        model.split(
          'row',
          [model.group(['sessions'], { id: 'grp-sessions' }), model.group(['workspace'], { id: 'grp-main' })],
          [1, 3]
        ),
        model.group(['terminal', 'logs'], { id: 'grp-terminal', active: 'terminal' })
      ],
      [3, 1]
    )

    tree.declareDefaultTree(terminalDeckTree)
    // Force the exact starting shape regardless of any state a prior test
    // file left in the shared $layoutTree singleton — declareDefaultTree
    // alone would MERGE with whatever's already there via adoptMissingPanes,
    // which is correct app behavior but makes this test's precondition
    // depend on test execution order. Set the tree directly so the terminal
    // vs logs starting `active` is unambiguous.
    tree.$layoutTree.set(terminalDeckTree)

    // Mirror controller.tsx exactly: both stores are persisted `true` (open)
    // from earlier interaction — this is the real-world condition that
    // triggers the bug. Bind terminal FIRST, logs LAST, exactly like
    // controller.tsx's module-level bindPaneCollapse calls.
    const openMirror = (initial: boolean) => {
      let value = initial
      const listeners: Array<(v: boolean) => void> = []

      return {
        get: () => value,
        listen: (fn: (v: boolean) => void) => {
          listeners.push(fn)
        },
        set: (next: boolean) => {
          value = next
          listeners.forEach(fn => fn(next))
        }
      }
    }

    const $terminalTakeover = openMirror(true)
    const $logsOpen = openMirror(true)

    function bindPaneCollapse(
      paneId: string,
      $open: ReturnType<typeof openMirror>,
      close: () => void,
      open: () => void
    ) {
      tree.markCollapsePane(paneId)
      // The fix under test: front: false on the mount-time sync.
      tree.setPaneCollapsed(paneId, !$open.get(), false)
      $open.listen(isOpen => tree.setPaneCollapsed(paneId, !isOpen))
      tree.registerPaneCloser(paneId, close)
      tree.registerPaneOpener(paneId, open)
    }

    bindPaneCollapse(
      'terminal',
      $terminalTakeover,
      () => $terminalTakeover.set(false),
      () => $terminalTakeover.set(true)
    )
    bindPaneCollapse(
      'logs',
      $logsOpen,
      () => $logsOpen.set(false),
      () => $logsOpen.set(true)
    )

    const finalTree = tree.$layoutTree.get()
    const group = finalTree ? model.findGroupOfPane(finalTree, 'terminal') : null

    // Before the fix: logs (bound last) always won — active === 'logs'.
    expect(group?.active).toBe('terminal')
    expect(group?.minimized).toBeFalsy()
  })
})
