import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MicRecording } from './use-mic-recorder'

import { useVoiceRecorder } from './use-voice-recorder'

// #105955 review: the mic-open warm-up must act as a READINESS BARRIER before
// transcription — `stop()` awaits the warm-up fired at start, so a cold local
// model load settles inside the lease request's own budget instead of eating
// the transcription request's decode deadline. Without the barrier, a 190 s
// cold load behind a 3 s recording exhausted the 180 s transcription floor
// before decoding began.

// The real recorder flips `recording` in start/stop; dictate() routes on it.
let recording = false
const micHandle = {
  cancel: vi.fn(),
  start: vi.fn(async () => {
    recording = true
  }),
  stop: vi.fn<() => Promise<MicRecording | null>>(async () => {
    recording = false

    return { audio: new Blob(), durationMs: 500, heardSpeech: true }
  })
}

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({ handle: micHandle, level: 0, get recording() { return recording } })
}))

const syncSttLeaseSpy = vi.fn(async () => undefined)

vi.mock('@/lib/stt-lease', () => ({
  syncSttLease: (...args: unknown[]) => syncSttLeaseSpy(...(args as [])),
  VOICE_INPUT_LEASE: 'desktop:voice-input:test'
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/desktop-metrics', () => ({
  recordFeatureUse: vi.fn()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          noSpeechDetected: 'no speech',
          recordingFailed: 'recording failed',
          transcriptionFailed: 'transcription failed',
          transcriptionUnavailable: 'unavailable',
          tryRecordingAgain: 'try again',
          unavailable: 'unavailable'
        }
      }
    }
  })
}))

vi.mock('../scope', () => ({
  useComposerScope: () => ({ connectionId: undefined, profile: undefined })
}))

describe('useVoiceRecorder STT readiness barrier', () => {
  beforeEach(() => {
    cleanup()
    recording = false
    micHandle.start.mockClear()
    micHandle.stop.mockReset()
    micHandle.stop.mockImplementation(async () => {
      recording = false

      return { audio: new Blob(), durationMs: 500, heardSpeech: true }
    })
    syncSttLeaseSpy.mockReset()
    syncSttLeaseSpy.mockImplementation(async () => undefined)
  })

  afterEach(() => {
    cleanup()
  })

  function renderRecorder(onTranscribeAudio: (audio: Blob) => Promise<string>) {
    return renderHook(() =>
      useVoiceRecorder({
        focusInput: () => undefined,
        maxRecordingSeconds: 30,
        onTranscript: () => undefined,
        onTranscribeAudio
      })
    )
  }

  it('does not submit the clip to transcription while the mic-open warm-up is still settling', async () => {
    // Model the cold load: the acquire stays on the wire after the mic opens.
    let settleWarmup!: () => void
    syncSttLeaseSpy.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          settleWarmup = resolve
        })
    )

    const transcribe = vi.fn(async () => 'hello world')
    const hook = renderRecorder(transcribe)

    // Mic opens: recording starts without waiting on the warm-up.
    await act(async () => {
      hook.result.current.dictate()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(micHandle.start).toHaveBeenCalledTimes(1)
    expect(syncSttLeaseSpy).toHaveBeenCalledWith('desktop:voice-input:test', true, {
      connectionId: undefined,
      profile: undefined
    })

    // User stops after a short clip — but the cold load is still in flight.
    let stopping: Promise<void> | undefined
    await act(async () => {
      stopping = hook.result.current.dictate() ?? undefined
      await Promise.resolve()
      await Promise.resolve()
    })

    // The barrier holds: the transcription request has not gone out yet,
    // so the load wait is not inside the transcribe request's budget.
    expect(transcribe).not.toHaveBeenCalled()

    settleWarmup()
    await act(async () => {
      await stopping
    })

    // Readiness established → the clip is submitted and transcribes.
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1))
    expect(await transcribe.mock.results[0].value).toBe('hello world')
  })

  it('transcribes without extra delay once the warm-up has already settled', async () => {
    syncSttLeaseSpy.mockImplementation(async () => undefined)

    const transcribe = vi.fn(async () => 'hello world')
    const hook = renderRecorder(transcribe)

    await act(async () => {
      hook.result.current.dictate()
    })
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      hook.result.current.dictate() // stop
    })

    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1))
  })

  it('releases the lease to the composer scope owner after the transcript settles', async () => {
    const transcribe = vi.fn(async () => 'hello world')
    const hook = renderRecorder(transcribe)

    await act(async () => {
      hook.result.current.dictate()
    })
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      hook.result.current.dictate() // stop
    })
    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1))

    // The release also carries the captured owner, not a re-read of ambient
    // selection at settle time.
    const releaseCall = syncSttLeaseSpy.mock.calls.find(([, active]) => active === false)
    expect(releaseCall).toEqual(['desktop:voice-input:test', false, { connectionId: undefined, profile: undefined }])
  })
})
