import { afterEach, expect, test, vi } from 'vitest'

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); localStorage.clear() })

// The native file journal of one origin, shared by every renderer load. Removal fails like
// ENOSPC/EIO while `failing` is set; reads keep working.
function nativeJournal() {
  let file = '{}'
  const state = { failing: false }

  const native = {
    read: async () => file,
    update: async (key: string, entry: string | null) => {
      if (state.failing) { throw new Error('ENOSPC: no space left on device') }
      const journal = JSON.parse(file)

      if (entry === null) { delete journal[key] } else { journal[key] = JSON.parse(entry) }
      file = JSON.stringify(journal)
    }
  }

  return { native, state, file: () => JSON.parse(file) }
}

test('an admitted identity whose journal removal failed stays spent across a reload', async () => {
  const journal = nativeJournal()
  vi.stubGlobal('hermesDesktop', { preparedSubmissions: journal.native })
  const intent = JSON.stringify(['local::default', 'stored', 'same text', [], null, false, null])
  const entry = { id: 'admitted-id', text: 'same text', attachments: [], params: { submission_id: 'admitted-id' }, owner: { connectionId: 'local', profile: 'default' } }

  const first = await import('./prepared-submissions')
  await first.writePreparedSubmission(intent, entry)
  journal.state.failing = true
  await expect(first.removePreparedSubmission(intent, 'admitted-id')).rejects.toThrow('ENOSPC')
  expect(journal.file()[intent]?.id).toBe('admitted-id')

  // Renderer reload: module memory is gone, the file entry is not.
  vi.resetModules()
  const reloaded = await import('./prepared-submissions')
  expect(await reloaded.adoptPreparedSubmission(intent)).toBeUndefined()
  expect(await reloaded.readPreparedSubmission(intent)).toBeUndefined()

  // Storage recovers: the stale file entry is retired instead of lingering.
  journal.state.failing = false
  await reloaded.listPreparedImageDrafts('stored', 'local::default')
  expect(journal.file()[intent]).toBeUndefined()
})
