/**
 * A plugin page route registered AFTER the workspace surface mounts must
 * become navigable. Regression for late-loaded desktop plugins (disk plugins
 * load async): the route table was compiled into a memo slot keyed on
 * unrelated props, so a late `routes`-area registration never entered the
 * table — the sidebar row rendered but navigating to the path fell through
 * to the `:sessionId` chat route. Uses the REAL useContributions + registry
 * (unlike surfaces.test.tsx) because the reactive flow is the subject.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import { ChatRoutesSurface } from './surfaces'
import type { WiringActions } from './types'

vi.mock('@/store/connections', () => ({ $activeConnectionId: atom('local') }))
vi.mock('@/store/gateway', () => ({ $gateway: atom<unknown>(null) }))
vi.mock('@/store/profile', () => ({
  normalizeProfileKey: (k: string) => k,
  $activeGatewayProfile: atom('default'),
  // src/store/layout.ts reads these at module load, and this suite's
  // import graph reaches it through src/store/preview.ts — a mock that
  // only defines $activeGatewayProfile makes layout.ts throw
  // 'No "$showAllProfiles" export is defined on the "@/store/profile" mock'.
  $showAllProfiles: atom(false),
  setShowAllProfiles: () => {}
}))
vi.mock('@/store/session', () => ({
  $turnStartedAt: atom(null),
  $currentReasoningEffortWire: atom(null),
  $currentReasoningEffort: atom(null),
  $currentProvider: atom(null),
  $currentModel: atom(null),
  $currentFastMode: atom(null),
  $currentCwd: atom(null),
  $awaitingResponse: atom(null),
  $busy: atom(null),
  $messages: atom(null),
  $activeSessionId: atom(null),
  $freshDraftReady: atom(false),
  $gatewayState: atom('open'),
  // src/store/session-states.ts (reached via the session-unread chain) subscribes
  // to these at module load and calls .listen() on them, so they must be real
  // atoms — undefined here fails the whole suite at import time with
  // 'No "$sessions" export is defined on the "@/store/session" mock'.
  $sessions: atom([]),
  $cronSessions: atom([]),
  $messagingSessions: atom([]),
  $selectedStoredSessionId: atom(null),
  $unreadFinishedSessionIds: atom([]),
  sessionMatchesStoredId: () => false,
  sessionPinId: (id: string) => id
}))
vi.mock('../chat', () => ({ ChatView: () => <div data-testid="chat-view" /> }))
vi.mock('../chat/sidebar', () => ({ ChatSidebar: () => null }))
vi.mock('../right-sidebar/terminal/chrome', () => ({ TerminalPaneChrome: () => null }))
vi.mock('../shell/hooks/use-status-snapshot', () => ({ useStatusSnapshot: () => ({}) }))
vi.mock('../shell/hooks/use-statusbar-items', () => ({
  useStatusbarItems: () => ({ leftStatusbarItems: [], statusbarItems: [] })
}))
vi.mock('../shell/statusbar-controls', () => ({ StatusbarControls: () => null }))
vi.mock('./latest-actions', () => ({ latestChatActions: () => ({}), latestSidebarActions: () => ({}) }))
vi.mock('./panes', () => ({ setStatusbarItemGroup: vi.fn(), useStatusbarContributions: () => [] }))
vi.mock('../shell/model-menu-panel', () => ({ ModelMenuPanel: () => null }))
vi.mock('../shell/reasoning-menu-panel', () => ({ ReasoningMenuPanel: () => null }))

afterEach(() => {
  cleanup()
})

describe('ChatRoutesSurface late-registered plugin routes', () => {
  it('renders a page whose route registers after mount', () => {
    const actions = {} as unknown as WiringActions

    render(
      <MemoryRouter initialEntries={['/late-plugin']}>
        <ChatRoutesSurface actions={actions} />
      </MemoryRouter>
    )

    // Before registration the path falls through to the chat catch-all.
    expect(screen.queryByTestId('late-page')).toBeNull()
    expect(screen.getByTestId('chat-view')).toBeTruthy()

    let dispose = () => {}
    act(() => {
      dispose = registry.register({
        area: 'routes',
        id: 'late-plugin:page',
        data: { path: '/late-plugin' },
        render: () => <div data-testid="late-page" />
      })
    })

    // The late registration must reach the route table and win over the
    // `:sessionId` dynamic route.
    expect(screen.getByTestId('late-page')).toBeTruthy()
    expect(screen.queryByTestId('chat-view')).toBeNull()

    act(() => dispose())
  })
})
