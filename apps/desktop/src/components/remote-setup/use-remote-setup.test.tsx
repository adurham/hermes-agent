import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionProbeResult } from '@/global'
import { deferred } from '@/test/deferred'

import { useRemoteSetup } from './use-remote-setup'
import type { RemoteSetupHost } from './use-remote-setup'

function probeResult(authMode: 'oauth' | 'token', label: string): DesktopConnectionProbeResult {
  return {
    authMode,
    baseUrl: `https://${label}.example`,
    reachable: true,
    error: null,
    version: null,
    providers: [{ name: label, displayName: label }]
  }
}

beforeEach(() => {
  vi.useFakeTimers()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('remote setup owner', () => {
  it.each<RemoteSetupHost>(['first-run', 'settings', 'registry'])(
    'rejects stale probe results in %s despite fresh host callbacks',
    async (host: RemoteSetupHost): Promise<void> => {
      const oldProbe = deferred<DesktopConnectionProbeResult>()
      const newProbe = deferred<DesktopConnectionProbeResult>()
      const probeConnectionConfig = vi.fn().mockReturnValueOnce(oldProbe.promise).mockReturnValueOnce(newProbe.promise)

      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { probeConnectionConfig } })
      const { result, rerender } = renderHook(() => useRemoteSetup({ host, onNotice: () => {} }))
      act(() => {
        result.current.setAuthMode('oauth')
        result.current.setUrl('https://a.example')
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500)
      })
      act(() => result.current.setUrl('https://b.example'))
      rerender()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500)
      })
      expect(probeConnectionConfig.mock.calls).toEqual([['https://a.example'], ['https://b.example']])
      await act(async (): Promise<void> => {
        newProbe.resolve(probeResult('oauth', 'new'))
        oldProbe.resolve(probeResult('token', 'old'))
      })
      expect(result.current.payload).toEqual({
        mode: 'remote',
        remoteUrl: 'https://b.example',
        remoteAuthMode: 'oauth',
        remoteToken: undefined
      })
      expect(result.current.providerLabel).toBe('new')
    }
  )

  it('does not publish login completion after the editor unmounts without a probe bridge', async () => {
    const onNotice = vi.fn()
    const pendingLogin = deferred<{ connected: boolean }>()
    const oauthLoginConnectionConfig = vi.fn().mockReturnValue(pendingLogin.promise)

    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { oauthLoginConnectionConfig } })
    const { result, unmount } = renderHook(() => useRemoteSetup({ host: 'registry', onNotice }))
    act(() => {
      result.current.setAuthMode('oauth')
      result.current.setUrl('https://a.example')
    })
    let login!: Promise<void>
    await act(async () => {
      login = result.current.signIn()
    })
    expect(oauthLoginConnectionConfig).toHaveBeenCalledExactlyOnceWith('https://a.example')
    unmount()
    await act(async (): Promise<void> => {
      pendingLogin.resolve({ connected: true })
      await login
    })
    expect(onNotice).not.toHaveBeenCalled()
  })
})
