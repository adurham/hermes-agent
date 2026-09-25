import { cleanup, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { APP_ROUTES, isOverlayView } from '../routes'

import { ChatRoutesSurface } from './surfaces'
import type { WiringActions } from './types'

vi.mock('@/contrib/react/use-contributions', () => ({ useContributions: vi.fn() }))
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
vi.mock('../capabilities', () => ({ CapabilitiesView: () => null }))
vi.mock('../messaging', () => ({ MessagingView: () => null }))
vi.mock('../artifacts', () => ({ ArtifactsView: () => null }))
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

afterEach(cleanup)

// The route table (`APP_ROUTES`) and the `<Route>` list are kept by hand in two
// files. A workspace page whose path is missing from the `<Route>` list matches
// `:sessionId`, so the app opens a chat for a session named after the page.
// Overlays are exempt: they are modal cards over whatever the shell shows.
describe('ChatRoutesSurface and APP_ROUTES', () => {
  const pages = APP_ROUTES.filter(route => route.view !== 'chat' && !isOverlayView(route.view))

  it.each(pages.map(route => [route.path]))('%s has its own route and never opens as a session', path => {
    const actions = { getGateway: () => null } as unknown as WiringActions

    render(
      <MemoryRouter initialEntries={[path]}>
        <ChatRoutesSurface actions={actions} />
      </MemoryRouter>
    )

    expect(screen.queryByTestId('chat-view')).toBeNull()
  })
})
