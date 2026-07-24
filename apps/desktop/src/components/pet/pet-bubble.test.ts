import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { _resetBeatLineCacheForTests, speakAnnouncedBeat as SpeakAnnouncedBeat } from './pet-bubble'

// Mocked before import so the module under test picks up the mocks.
const fetchPetDialogueMock = vi.fn()
const playSpeechTextMock = vi.fn().mockResolvedValue(true)

vi.mock('@/hermes', () => ({
  fetchPetDialogue: (...args: unknown[]) => fetchPetDialogueMock(...args)
}))

vi.mock('@/lib/voice-playback', () => ({
  playSpeechText: (...args: unknown[]) => playSpeechTextMock(...args)
}))

// pet-bubble.tsx pulls in nanostores atoms + React JSX — none of it runs
// during import for a plain function-level test, but keep the mocks minimal
// and targeted rather than reaching further into unrelated stores.
vi.mock('@/store/pet', () => ({
  $petActivity: { get: () => ({}) },
  $petInfo: { get: () => ({}) },
  $petRealState: { get: () => 'idle' },
  $petTurnCompletedBeat: { get: () => ({ context: '', seq: 0 }) }
}))

vi.mock('@/store/pet-voice', () => ({
  $petVoiceEnabled: { get: () => false },
  $petVoiceProvider: { get: () => '' }
}))

describe('speakAnnouncedBeat', () => {
  let speakAnnouncedBeat: typeof SpeakAnnouncedBeat
  let resetCache: typeof _resetBeatLineCacheForTests

  beforeEach(async () => {
    vi.resetModules()
    fetchPetDialogueMock.mockReset()
    playSpeechTextMock.mockClear()
    ;({ speakAnnouncedBeat, _resetBeatLineCacheForTests: resetCache } = await import('./pet-bubble'))
    resetCache()
  })

  afterEach(() => {
    resetCache()
  })

  const call = (overrides: Partial<Parameters<typeof speakAnnouncedBeat>[0]> = {}) =>
    speakAnnouncedBeat({
      beat: 'completed',
      context: '',
      fallbackLines: ['fallback one', 'fallback two'],
      petSlug: 'hatsune-miku',
      voiceProvider: '',
      ...overrides
    })

  it('speaks a static fallback line instantly when nothing is cached yet, never waiting on the LLM call', () => {
    fetchPetDialogueMock.mockReturnValue(new Promise(() => {})) // never resolves

    call()

    expect(playSpeechTextMock).toHaveBeenCalledTimes(1)
    const [spokenText] = playSpeechTextMock.mock.calls[0]
    expect(['fallback one', 'fallback two']).toContain(spokenText)
  })

  it('speaks the cached LLM line from a PRIOR beat instantly, not the current one still in flight', async () => {
    // First beat: static fallback speaks immediately, LLM resolves after.
    fetchPetDialogueMock.mockResolvedValueOnce({ ok: true, line: 'first llm line' })
    call({ context: 'task A' })
    await Promise.resolve()
    await Promise.resolve()

    // Second beat: should speak the line generated for the FIRST beat,
    // instantly — not wait for a second LLM call.
    fetchPetDialogueMock.mockReturnValue(new Promise(() => {})) // never resolves this time
    call({ context: 'task B' })

    expect(playSpeechTextMock).toHaveBeenCalledTimes(2)
    expect(playSpeechTextMock.mock.calls[1][0]).toBe('first llm line')
  })

  it('never speaks the LLM result directly for the SAME beat call — only caches it for next time', async () => {
    fetchPetDialogueMock.mockResolvedValueOnce({ ok: true, line: 'brand new line' })

    call()
    // The synchronous speak() call already fired with a fallback line before
    // the promise below resolves.
    expect(playSpeechTextMock).toHaveBeenCalledTimes(1)
    expect(playSpeechTextMock.mock.calls[0][0]).not.toBe('brand new line')

    await Promise.resolve()
    await Promise.resolve()

    // Still only ever spoken once for this call — the resolved LLM line just
    // sits in the cache for the NEXT beat.
    expect(playSpeechTextMock).toHaveBeenCalledTimes(1)
  })

  it('does not regress the cache when an OLDER in-flight fetch resolves after a NEWER one', async () => {
    // Beat 1 launches a slow fetch that resolves last.
    let resolveSlow: (value: { ok: true; line: string }) => void = () => {}
    fetchPetDialogueMock.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveSlow = resolve
        })
    )
    call({ context: 'slow, launched first' })

    // Beat 2 launches a fast fetch that resolves before beat 1's slow one.
    fetchPetDialogueMock.mockResolvedValueOnce({ ok: true, line: 'fast line (newer)' })
    call({ context: 'fast, launched second' })
    await Promise.resolve()
    await Promise.resolve()

    // Beat 3 should speak the FAST (newer) line, since it's the freshest
    // committed result so far.
    fetchPetDialogueMock.mockReturnValue(new Promise(() => {}))
    call({ context: 'beat 3' })
    expect(playSpeechTextMock.mock.calls[2][0]).toBe('fast line (newer)')

    // Now the OLD slow fetch (launched before the fast one) finally resolves.
    // It must NOT overwrite the cache with its stale result.
    resolveSlow({ ok: true, line: 'slow line (older, arrives late)' })
    await Promise.resolve()
    await Promise.resolve()

    fetchPetDialogueMock.mockReturnValue(new Promise(() => {}))
    call({ context: 'beat 4' })
    expect(playSpeechTextMock.mock.calls[3][0]).toBe('fast line (newer)')
  })

  it('leaves the cache untouched when fetchPetDialogue rejects, falling back to the static pool', async () => {
    fetchPetDialogueMock.mockRejectedValueOnce(new Error('network error'))
    call()
    await Promise.resolve()
    await Promise.resolve()

    fetchPetDialogueMock.mockReturnValue(new Promise(() => {}))
    call()

    const [, secondCallArgs] = playSpeechTextMock.mock.calls
    expect(['fallback one', 'fallback two']).toContain(secondCallArgs[0])
  })
})
