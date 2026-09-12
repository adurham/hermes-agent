import { beforeEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from './client'
import { getLocalModelsJobs, getLocalModelsStatus, pauseLocalDownload } from './local-models'

beforeEach((): void => {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { api: vi.fn().mockResolvedValue({ jobs: [] }) }
  })
  setApiRequestConnection('foreground')
  setApiRequestProfile('default')
})

it('pins delayed reads and controls to their captured connection and profile', async (): Promise<void> => {
  const owner = { connectionId: 'background', profile: 'work' }
  await getLocalModelsStatus(owner)
  await getLocalModelsJobs(owner)
  await pauseLocalDownload('download', owner)

  for (const [request] of vi.mocked(window.hermesDesktop.api).mock.calls) {
    expect(request).toMatchObject(owner)
  }
})
