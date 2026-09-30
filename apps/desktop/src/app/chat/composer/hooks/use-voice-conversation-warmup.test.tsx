import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { BargeMonitorCallbacks } from '@/lib/voice-barge-in'
import type { MicRecording } from './use-mic-recorder'

import { useVoiceConversation } from './use-voice-conversation'

// #105955 review: the warm-up fired when the conversation starts listening
// is a READINESS BARRIER before transcription — handleTurn() awaits it before
// handing the clip to `onTranscribeAudio`, so a cold local model load settles
// inside the lease request's own budget instead of eating the transcription
// request's decode deadline (190 s cold load + 3 s clip vs a 180 s floor).

const micHandle = {
  cancel: vi.fn(),
  start: vi.fn(async () => undefined),
  stop: vi.fn<() => Promise<MicRecording | null>>(async () => null)
}

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({ handle: micHandle, level: 0, recording: false })
}))

const syncSttLeaseSpy = vi.fn(async () => undefined)

vi.mock('@/lib/stt-lease', () => ({
  syncSttLease: (...args: unknown[]) => syncSttLeaseSpy(...(args as [])),
  VOICE_INPUT_LEASE: 'desktop:voice-input:test'
}))

vi.mock('@/lib/voice-barge-in', () => ({
  monitorSpeechDuringPlayback: () => () => undefined
}))

vi.mock('@/lib/voice-playback', () => ({
  markVoicePlaybackInterrupted: vi.fn(),
  playSpeechText: vi.fn(async () => true),
  startSpeechStream: vi.fn(async () => null),
  stopVoicePlayback: vi.fn(),
  takeVoicePlaybackInterrupted: vi.fn(() => true)
}))

vi.mock('@/lib/thinking-sound', () => ({
  startThinkingSound: vi.fn(),
  stopThinkingSound: vi.fn()
}))

vi.mock('@/lib/speech-text', () => ({
  IncrementalSpeechSentenceBuffer: class {
    append() { return [] }
    flush() { return [] }
  }
}))

vi.mock('@/lib/voice-stop-word', () => ({ isVoiceStopCommand: () => false }))
vi.mock('@/lib/voice-tts-echo', () => ({ isTtsEcho: () => false }))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/voice-playback', () => {
  const { atom } = require('nanostores') as never as { atom: (v: unknown) => unknown }

  return { $voicePlayback: atom({ sequence: 0, status: 'idle' }) }
})

vi.mock('@/store/voice-prefs', async () => {
  const { atom } = await import('nanostores')

  return {
    $autoSpeakReplies: atom(false),
    $bargeInThresholdMultiplier: atom(1),
    $voiceSilenceMs: atom(1250)
  }
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          configureSpeechToText: 'configure STT',
          couldNotStartSession: 'could not start',
          microphoneFailed: 'mic failed',
          playbackFailed: 'playback failed',
          recordingFailed: 'recording failed',
          transcriptionFailed: 'transcription failed',
          unavailable: 'unavailable'
        }
      }
    }
  })
}))

vi.mock('../scope', () => ({
  useComposerScope: () => ({ $messages: { get: () => [] }, connectionId: undefined, profile: undefined })
}))

describe('useVoiceConversation STT readiness barrier', () => {
  beforeEach(() => {
    cleanup()
    micHandle.start.mockClear()
    micHandle.cancel.mockClear()
    micHandle.stop.mockReset()
    micHandle.stop.mockImplementation(async () => ({ audio: new Blob(), durationMs: 900, heardSpeech: true }))
    syncSttLeaseSpy.mockReset()
    syncSttLeaseSpy.mockImplementation(async () => undefined)
  })

  afterEach(() => {
    cleanup()
  })

  function renderConversation(onTranscribeAudio: (audio: Blob) => Promise<string>) {
    const hook = renderHook(() =>
      useVoiceConversation({
        busy: false,
        consumePendingResponse: () => undefined,
        enabled: true,
        onSubmit: () => undefined,
        onTranscribeAudio,
        pendingResponse: () => null
      })
    )

    return hook
  }

  it('waits for the listening warm-up before transcribing the captured utterance', async () => {
    // Cold load in flight: the acquire stays open past the silence stop.
    let settleWarmup!: () => void
    syncSttLeaseSpy.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          settleWarmup = resolve
        })
    )

    const transcribe = vi.fn(async () => 'hello world')
    const hook = renderConversation(transcribe)

    // Listening starts: the mic opens without waiting on the warm-up.
    await act(async () => {
      await hook.result.current.start()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(micHandle.start).toHaveBeenCalledTimes(1)
    expect(syncSttLeaseSpy).toHaveBeenCalledWith('desktop:voice-input:test', true, {
      connectionId: undefined,
      profile: undefined
    })

    // The VAD silence callback fires a turn while the cold load is in flight.
    let turnPromise: Promise<void> | undefined
    await act(async () => {
      micHandle.stop.mockImplementation(async () => ({ audio: new Blob(), durationMs: 900, heardSpeech: true }))
      turnPromise = Promise.resolve(hook.result.current.stopTurn())
      await Promise.resolve()
      await Promise.resolve()
    })

    // The barrier holds: transcription has not started inside the load wait.
    expect(transcribe).not.toHaveBeenCalled()

    settleWarmup()
    await act(async () => {
      await turnPromise
    })

    await waitFor(() => expect(transcribe).toHaveBeenCalledTimes(1))
    expect(await transcribe.mock.results[0].value).toBe('hello world')
  })
})
