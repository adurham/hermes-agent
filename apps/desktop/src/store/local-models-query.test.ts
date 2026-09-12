import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'

import type { LocalRuntimeJob } from '@/types/hermes'

vi.mock('@/hermes', (): object => ({
  getLocalModelsJobs: vi.fn(),
  getLocalModelsStatus: vi.fn(),
  getLocalCatalog: vi.fn(),
  getLocalHardware: vi.fn()
}))
vi.mock('@/store/notifications', (): object => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/i18n', (): object => ({ translateNow: (key: string): string => key }))
vi.mock('@/store/profile', async (): Promise<object> => {
  const { atom } = await import('nanostores')

  return { $activeGatewayProfile: atom<string>('default') }
})
vi.mock('@/store/session', async (): Promise<object> => {
  const { atom } = await import('nanostores')

  return { $connection: atom(null) }
})

import { getLocalModelsJobs } from '@/hermes'
import { queryClient } from '@/lib/query-client'

import { localModelsKey } from './local-runtime-jobs'
import { watchLocalRuntimeJobs } from './local-runtime-jobs'

const clients: QueryClient[] = []
afterEach((): void => {
  for (const client of clients) {
    client.clear()
  }

  vi.clearAllMocks()
})

it('publishes a pinned job response into the shared QueryClient observer', async (): Promise<void> => {
  const client: QueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  const owner = { connectionId: 'A', profile: 'work' }

  const job: LocalRuntimeJob = {
    job_id: 'j',
    kind: 'model-download',
    model_id: 'm',
    target: 'Model A',
    status: 'running',
    phase: 'downloading',
    detail: '',
    done_bytes: 1,
    total_bytes: 2,
    error: null
  }

  vi.mocked(getLocalModelsJobs).mockResolvedValue({ jobs: [job] })

  const observer: QueryObserver<readonly LocalRuntimeJob[]> = new QueryObserver<readonly LocalRuntimeJob[]>(client, {
    queryKey: ['local-models', 'A', 'work', 'jobs'],
    enabled: false
  })

  const unsubscribe: () => void = observer.subscribe((): void => {})
  watchLocalRuntimeJobs(owner, client)
  await vi.waitFor((): void => expect(observer.getCurrentResult().data).toEqual([job]))
  expect(getLocalModelsJobs).toHaveBeenCalledWith(owner)
  unsubscribe()
})

it('coalesces one trailing read while the production cache is still fresh', async (): Promise<void> => {
  clients.push(queryClient)
  const owner = { connectionId: 'A', profile: 'work' }
  vi.mocked(getLocalModelsJobs).mockResolvedValue({ jobs: [] })
  watchLocalRuntimeJobs(owner)
  await vi.waitFor((): void => expect(queryClient.getQueryData(localModelsKey(owner, 'jobs'))).toEqual([]))

  let release: (value: { jobs: LocalRuntimeJob[] }) => void = (): void => {}

  const pending: Promise<{ jobs: LocalRuntimeJob[] }> = new Promise((resolve): void => {
    release = resolve
  })

  vi.mocked(getLocalModelsJobs).mockReturnValueOnce(pending)
  watchLocalRuntimeJobs(owner)
  watchLocalRuntimeJobs(owner)
  watchLocalRuntimeJobs(owner)
  await Promise.resolve()
  expect(getLocalModelsJobs).toHaveBeenCalledTimes(2)
  release({ jobs: [] })
  await vi.waitFor((): void => expect(getLocalModelsJobs).toHaveBeenCalledTimes(3))
})
