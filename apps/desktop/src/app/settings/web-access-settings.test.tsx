vi.mock('@/store/profile', async (): Promise<object> => {
  const { atom } = await import('nanostores')

  return {
    $activeGatewayProfile: atom<string>('default'),
    $profiles: atom<Array<{ name: string; is_default?: boolean }>>([]),
    normalizeProfileKey: (profile: string | null): string => profile || 'default'
  }
})
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getWebAccessStatus: vi.fn(),
  restartWebAccess: vi.fn(),
  setApiRequestProfile: vi.fn(),
  startWebAccess: vi.fn(),
  stopWebAccess: vi.fn()
}))

import { QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { WebAccessStatus } from '@/api/webapp-access'
import * as hermes from '@/hermes'
import { I18nProvider } from '@/i18n'
import { queryClient } from '@/lib/query-client'
import { $notifications, clearNotifications } from '@/store/notifications'

import { WebAccessSettings } from './web-access-settings'

const mocked = vi.mocked(hermes)

const OFF: WebAccessStatus = {
  enabled: false,
  gateway: { reason: null, state: 'off', url: null },
  lan_address: '192.168.1.50',
  mode: null,
  nous_signed_in: true,
  port: 9120,
  public_url: ''
}

function renderPane(status: WebAccessStatus) {
  mocked.getWebAccessStatus.mockResolvedValue(status)

  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <WebAccessSettings />
      </I18nProvider>
    </QueryClientProvider>
  )
}

beforeEach(() => queryClient.clear())

afterEach(() => {
  cleanup()
  clearNotifications()
  vi.clearAllMocks()
})

describe('WebAccessSettings', () => {
  it('offers Start only with a Nous account to sign in with', async () => {
    const signedOut = renderPane({ ...OFF, nous_signed_in: false })

    expect((await screen.findByRole('button', { name: 'Start' })).hasAttribute('disabled')).toBe(true)
    signedOut.unmount()
    queryClient.clear()
    renderPane(OFF)

    expect((await screen.findByRole('button', { name: 'Start' })).hasAttribute('disabled')).toBe(false)
  })

  it('says why the gateway cannot serve a turned-on web app', async () => {
    renderPane({
      ...OFF,
      enabled: true,
      gateway: { reason: 'port 9120 is in use', state: 'unavailable', url: null },
      mode: 'lan'
    })

    expect(await screen.findByText(/port 9120 is in use/)).toBeTruthy()
  })

  it('never reports a web app the gateway still serves as stopped', async () => {
    // Stop saved `enabled: false`, but the gateway refused to restart: it still serves the web app.
    renderPane({ ...OFF, gateway: { reason: null, state: 'serving', url: 'http://127.0.0.1:9120' } })

    expect(await screen.findByText('Restart needed')).toBeTruthy()
    expect(screen.queryByText('Stopped')).toBeNull()
  })

  it('shows a web app served on this computer only as local, never as your own URL', async () => {
    // `webapp.enabled: true` on loopback (the `hermes webapp` setup): reachable from this computer only.
    renderPane({
      ...OFF,
      enabled: true,
      gateway: { reason: null, state: 'serving', url: 'http://127.0.0.1:9120' },
      mode: 'local'
    })

    expect(await screen.findByRole('link', { name: /http:\/\/127\.0\.0\.1:9120/ })).toBeTruthy()
    expect(screen.getByText(/Only this computer can open it/)).toBeTruthy()
    expect(screen.queryByText('My own URL')).toBeNull()
    expect(screen.queryByText(/192\.168\.1\.50/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()
  })

  it('restarts the gateway for a saved change without starting web access again', async () => {
    // Saved `enabled: true`, but the gateway has not restarted onto it yet (a chat refused it).
    const pending: WebAccessStatus = { ...OFF, enabled: true, mode: 'lan' }
    mocked.restartWebAccess.mockResolvedValue({ ...pending, restart_error: 'busy', restart_started: false })
    renderPane(pending)

    fireEvent.click(await screen.findByRole('button', { name: 'Restart gateway' }))

    await waitFor(() => expect(mocked.restartWebAccess).toHaveBeenCalledTimes(1))
    expect(mocked.startWebAccess).not.toHaveBeenCalled()
    expect(mocked.stopWebAccess).not.toHaveBeenCalled()
  })

  it('says plainly that a change was saved when a running chat holds off the restart', async () => {
    const refusal =
      'Refusing to restart the gateway: 1 live session(s) would be interrupted.\n' +
      '    POST /api/gateway/restart?interrupt=true   # interrupts 1 live session(s)'

    mocked.startWebAccess.mockResolvedValue({
      ...OFF,
      enabled: true,
      mode: 'lan',
      restart_busy: true,
      restart_error: refusal,
      restart_started: false
    })
    renderPane(OFF)

    fireEvent.click(await screen.findByRole('button', { name: 'Start' }))

    await waitFor(() => expect($notifications.get()).toHaveLength(1))
    const [toast] = $notifications.get()
    expect(toast.kind).toBe('warning')
    expect(toast.title).toBe('Restart needed')
    expect(toast.message).toMatch(/^Saved\. A chat is running/)
    expect(JSON.stringify(toast)).not.toMatch(/Refusing|interrupt|Couldn't change/)
  })
})
