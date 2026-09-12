import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, renderHook, type RenderResult, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesApiRequest, HermesConnection } from '@/global'
import type { LocalModelsStatus, LocalRuntimeJob, ModelOptionsResponse } from '@/types/hermes'

vi.mock('@/hermes', async (): Promise<object> => ({
  ...(await import('@/api/local-models')),
  getHermesConfigRecord: async (): Promise<object> => ({}),
  getGlobalModelOptions: async (): Promise<ModelOptionsResponse> => ({ providers: [] })
}))
vi.mock('@/store/profile', async (): Promise<object> => {
  const { atom } = await import('nanostores')

  return { $activeGatewayProfile: atom<string>('work') }
})
vi.mock('@/store/session', async (): Promise<object> => {
  const { atom } = await import('nanostores')

  return { $connection: atom<HermesConnection | null>(null), $defaultReasoningEffort: atom<string>('') }
})
vi.mock('@/store/notifications', (): object => ({ notify: vi.fn(), notifyError: vi.fn() }))

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { LocalModelsSettings } from '@/app/settings/local-models-settings'
import { ModelCatalogMenu, type ModelMenuController } from '@/app/shell/model-catalog-menu'
import { ModelPickerDialog } from '@/components/model-picker'
import { DropdownMenu, DropdownMenuContent } from '@/components/ui/dropdown-menu'
import { I18nProvider } from '@/i18n'
import { queryClient } from '@/lib/query-client'
import { $localModelsEnabled } from '@/store/local-models-flag'
import { notify } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection } from '@/store/session'
import { stubMenuDomApis, stubResizeObserver } from '@/test/jsdom'

import {
  checkLocalRuntimeUpdate,
  localModelsKey,
  localModelsOwner,
  useLocalModelsOwner,
  useLocalRuntimeJobs,
  watchLocalRuntimeJobs
} from './local-runtime-jobs'

stubResizeObserver()
stubMenuDomApis()

const status: LocalModelsStatus = {
  enabled: true,
  tag: 'installed',
  configured_tag: 'installed',
  update_available: false,
  runtime_installed: true,
  runtime_backend: 'cpu',
  server_running: false,
  server_base_url: null,
  active_model_id: null,
  loaded_models: {},
  models: [],
  models_dir: '/models'
}

const running: LocalRuntimeJob = {
  job_id: 'same-id',
  kind: 'model-download',
  model_id: 'model',
  target: 'Download A',
  status: 'running',
  phase: 'downloading',
  detail: '',
  done_bytes: 1,
  total_bytes: 2,
  percent: 50,
  error: null
}

let jobs: LocalRuntimeJob[] = []

const api = vi.fn(async (request: HermesApiRequest): Promise<unknown> => {
  if (request.path.endsWith('/status')) {
    return structuredClone(status)
  }

  if (request.path.endsWith('/catalog')) {
    return { models: [] }
  }

  if (request.path.endsWith('/jobs')) {
    return { jobs: structuredClone(jobs) }
  }

  if (request.path.endsWith('/hardware')) {
    return {}
  }

  throw new Error(`Unexpected request ${request.path}`)
})

const controller: ModelMenuController = {
  current: { model: '', provider: '', effort: '', fast: false },
  applyPreset: (): void => {},
  presetFor: (): object => ({}),
  select: (): void => {},
  setOptions: (): void => {}
}

beforeEach((): void => {
  vi.useFakeTimers()
  queryClient.clear()
  jobs = []
  api.mockClear()
  $connection.set(null)
  vi.mocked(notify).mockClear()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
  setApiRequestConnection('A')
  setApiRequestProfile('work')
  $activeGatewayProfile.set('work')
  $localModelsEnabled.set(true)
})
afterEach((): void => {
  cleanup()
  queryClient.clear()
  vi.useRealTimers()
})

async function tick(ms: number = 0): Promise<void> {
  await act(async (): Promise<void> => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

function mountSettings(): RenderResult {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <I18nProvider>
          <LocalModelsSettings />
        </I18nProvider>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

it('uses one status polling clock across staggered Settings, picker and menu mounts', async (): Promise<void> => {
  const settings: RenderResult = mountSettings()
  await tick(400)
  render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <ModelPickerDialog
          currentModel=""
          currentProvider=""
          onOpenChange={(): void => {}}
          onSelect={(): void => {}}
          open
          ownerConnectionId="A"
          profile="work"
        />
      </I18nProvider>
    </QueryClientProvider>
  )
  await tick(400)
  render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <DropdownMenu open>
          <DropdownMenuContent>
            <ModelCatalogMenu controller={controller} ownerConnectionId="A" profile="work" />
          </DropdownMenuContent>
        </DropdownMenu>
      </I18nProvider>
    </QueryClientProvider>
  )
  await tick(2_000)

  const statusQuery = queryClient
    .getQueryCache()
    .find({ queryKey: localModelsKey({ connectionId: 'A', profile: 'work' }, 'status') })

  expect(
    statusQuery?.observers.filter(
      (observer): boolean => Boolean(observer.options.refetchInterval) && observer.options.enabled !== false
    )
  ).toHaveLength(1)
  expect(api.mock.calls.filter(([request]): boolean => request.path.endsWith('/status'))).toHaveLength(2)
  settings.unmount()
  await tick(2_000)
  expect(api.mock.calls.filter(([request]): boolean => request.path.endsWith('/status'))).toHaveLength(3)
  cleanup()
  await tick(4_000)
  expect(api.mock.calls.filter(([request]): boolean => request.path.endsWith('/status'))).toHaveLength(3)
})

it('keeps paused work alive after Settings unmount and isolates late results by owner', async (): Promise<void> => {
  jobs = [{ ...running, status: 'paused' }]
  const settings: RenderResult = mountSettings()
  await tick()
  settings.unmount()
  jobs = [running]
  await tick(3_000)
  expect(queryClient.getQueryData(localModelsKey({ connectionId: 'A', profile: 'work' }, 'jobs'))).toEqual([running])

  let release: (value: unknown) => void = (): void => {}

  const pending: Promise<unknown> = new Promise((resolve): void => {
    release = resolve
  })

  api.mockImplementationOnce((): Promise<unknown> => pending)
  watchLocalRuntimeJobs({ connectionId: 'A', profile: 'work' })
  setApiRequestConnection('B')
  jobs = [{ ...running, target: 'Download B' }]
  watchLocalRuntimeJobs({ connectionId: 'B', profile: 'work' })
  await tick()
  release({ jobs: [{ ...running, status: 'done' }] })
  await tick()
  expect(queryClient.getQueryData(localModelsKey({ connectionId: 'B', profile: 'work' }, 'jobs'))).toEqual(jobs)
  expect(notify).toHaveBeenCalledTimes(1)
  expect(vi.mocked(notify).mock.calls[0][0].title).toContain('A / work')
  jobs = [{ ...running, target: 'Download B', status: 'done' }]
  watchLocalRuntimeJobs({ connectionId: 'B', profile: 'work' })
  await tick()
  expect(notify).toHaveBeenCalledTimes(2)
  watchLocalRuntimeJobs({ connectionId: 'B', profile: 'work' })
  await tick()
  expect(notify).toHaveBeenCalledTimes(2)
})

it.each(['connection', 'profile'] as const)(
  'does not paint a late Settings snapshot after a %s switch',
  async (change: 'connection' | 'profile'): Promise<void> => {
    let release: (value: unknown) => void = (): void => {}

    const pending: Promise<unknown> = new Promise((resolve): void => {
      release = resolve
    })

    api.mockImplementationOnce((): Promise<unknown> => pending)
    const settings: RenderResult = mountSettings()
    await tick()
    await act(async (): Promise<void> => {
      if (change === 'connection') {
        setApiRequestConnection('B')
        $connection.set({
          baseUrl: 'http://B',
          connectionId: 'B',
          registryScoped: true,
          token: '',
          wsUrl: '',
          logs: [],
          isFullscreen: false,
          nativeOverlayWidth: 0,
          windowButtonPosition: null
        })
      } else {
        setApiRequestProfile('personal')
        $activeGatewayProfile.set('personal')
      }

      await vi.advanceTimersByTimeAsync(0)
    })
    await tick()
    expect(settings.container.textContent).toContain('installed')
    release({ ...status, tag: 'OLD-OWNER' })
    await tick()
    expect(settings.container.textContent).not.toContain('OLD-OWNER')

    const expected =
      change === 'connection' ? { connectionId: 'B', profile: 'work' } : { connectionId: 'A', profile: 'personal' }

    expect(
      api.mock.calls.some(
        ([request]): boolean =>
          request.path.endsWith('/status') &&
          request.connectionId === expected.connectionId &&
          request.profile === expected.profile
      )
    ).toBe(true)
  }
)

it('follows an authoritative route change even when its descriptor is unchanged', async (): Promise<void> => {
  const hook = renderHook(() => useLocalModelsOwner())
  expect(hook.result.current.connectionId).toBe('A')
  await act(async (): Promise<void> => {
    setApiRequestConnection('B')
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(hook.result.current.connectionId).toBe('B')
})

it('discards late legacy completions and update notices without invalidating the new catalog', async (): Promise<void> => {
  setApiRequestConnection(null)

  const connection: HermesConnection = {
    baseUrl: 'http://A',
    token: '',
    wsUrl: '',
    logs: [],
    isFullscreen: false,
    nativeOverlayWidth: 0,
    windowButtonPosition: null
  }

  $connection.set(connection)
  const owner = localModelsOwner()
  jobs = [running]
  watchLocalRuntimeJobs(owner)
  await tick()

  let releaseJobs: (value: unknown) => void = (): void => {}

  let releaseStatus: (value: unknown) => void = (): void => {}

  const pendingJobs: Promise<unknown> = new Promise((resolve): void => {
    releaseJobs = resolve
  })

  const pendingStatus: Promise<unknown> = new Promise((resolve): void => {
    releaseStatus = resolve
  })

  api.mockImplementationOnce((): Promise<unknown> => pendingJobs)
  watchLocalRuntimeJobs(owner)
  api.mockImplementationOnce((): Promise<unknown> => pendingStatus)
  const update: Promise<void> = checkLocalRuntimeUpdate(owner)
  $connection.set({ ...connection, baseUrl: 'http://B' })
  const key: readonly string[] = ['model-options', 'work', 'global']
  queryClient.setQueryData(key, { providers: [] })
  releaseJobs({ jobs: [{ ...running, status: 'done' }] })
  releaseStatus({ ...status, update_available: true })
  await update
  await tick()
  expect(queryClient.getQueryState(key)?.isInvalidated).toBe(false)
  expect(notify).not.toHaveBeenCalled()
})

it('reconnects mounted job subscribers if their query is removed', async (): Promise<void> => {
  jobs = [running]
  const owner = { connectionId: 'A', profile: 'work' }

  const hook = renderHook(
    (): string =>
      useLocalRuntimeJobs(owner, (rows: readonly LocalRuntimeJob[]): string => rows[0]?.status ?? 'missing'),
    {
      wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    }
  )

  await tick(1)
  expect(hook.result.current).toBe('running')
  await act(async (): Promise<void> => {
    queryClient.removeQueries({ queryKey: localModelsKey(owner, 'jobs'), exact: true })
    await vi.advanceTimersByTimeAsync(1)
  })
  hook.rerender()
  await tick(1)
  expect(hook.result.current).toBe('running')
  jobs = [{ ...running, status: 'paused' }]
  await tick(700)
  expect(hook.result.current).toBe('paused')
})

it('keeps menu focus and its scalar selection stable across byte updates and outages', async (): Promise<void> => {
  jobs = [running]
  let reads: number = 0

  const menuController: ModelMenuController = {
    ...controller,
    get current() {
      reads += 1

      return controller.current
    }
  }

  render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <DropdownMenu open>
          <DropdownMenuContent>
            <ModelCatalogMenu controller={menuController} ownerConnectionId="A" profile="work" />
          </DropdownMenuContent>
        </DropdownMenu>
      </I18nProvider>
    </QueryClientProvider>
  )
  await tick(1)
  const input: HTMLElement = screen.getByRole('textbox', { name: 'Search models' })
  fireEvent.change(input, { target: { value: 'Download' } })
  input.focus()
  await tick(1)
  const before: number = reads
  jobs = [{ ...running, done_bytes: 2, percent: 75 }]
  await tick(700)
  expect(screen.getByText('75%')).toBeTruthy()
  expect(window.document.activeElement).toBe(input)
  expect(reads).toBe(before)
  const key = localModelsKey({ connectionId: 'A', profile: 'work' }, 'jobs')
  const snapshot = queryClient.getQueryData(key)
  api.mockRejectedValueOnce(new Error('offline'))
  await tick(700)
  expect(queryClient.getQueryData(key)).toBe(snapshot)
  expect(screen.getByText('75%')).toBeTruthy()
  expect(window.document.activeElement).toBe(input)
})
