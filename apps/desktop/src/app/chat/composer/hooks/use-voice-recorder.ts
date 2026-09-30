import { useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { syncSttLease, VOICE_INPUT_LEASE } from '@/lib/stt-lease'
import { recordFeatureUse } from '@/store/desktop-metrics'
import { notify, notifyError } from '@/store/notifications'
import { useComposerScope } from '../scope'

import type { VoiceActivityState, VoiceStatus } from '../types'

import { useMicRecorder } from './use-mic-recorder'

interface VoiceRecorderOptions {
  maxRecordingSeconds: number
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  focusInput: () => void
  onTranscript: (text: string) => void
}

/** Settles when the STT warm-up issued at mic-open resolves (or never ran). */
type WarmupBarrier = null | Promise<void>

export function useVoiceRecorder({
  maxRecordingSeconds,
  onTranscribeAudio,
  focusInput,
  onTranscript
}: VoiceRecorderOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level, recording } = useMicRecorder(voiceCopy)
  // The scope's session owner (a Bot tile's own connection + profile) pins
  // the STT lease to the backend that will transcribe this composer's audio.
  const { connectionId: ownerConnectionId, profile: ownerProfile } = useComposerScope()
  const ownerRef = useRef({ connectionId: ownerConnectionId, profile: ownerProfile })
  ownerRef.current = { connectionId: ownerConnectionId, profile: ownerProfile }
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startedAtRef = useRef(0)
  const intervalRef = useRef<number | null>(null)
  const timeoutRef = useRef<number | null>(null)
  // Readiness barrier for the warm-up fired at mic-open (#105955 review):
  // transcription must not start while the cold model load is still on the
  // wire — the load would eat the transcription request's own decode budget.
  // stop() awaits it (bounded by the lease request's 180 s timeout) so the
  // transcribe POST starts only once warm-up settled (warm, noop, or failed —
  // a failed warm-up proceeds to transcription, which re-loads server-side).
  const warmupBarrierRef = useRef<WarmupBarrier>(null)

  const clearTimers = () => {
    if (intervalRef.current) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    if (timeoutRef.current) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }

  useEffect(() => () => clearTimers(), [])

  const stop = async () => {
    clearTimers()
    const result = await handle.stop()

    if (!result) {
      setVoiceStatus('idle')

      return
    }

    if (!onTranscribeAudio) {
      setVoiceStatus('idle')

      return
    }

    setVoiceStatus('transcribing')

    try {
      // Readiness first: let the mic-open warm-up settle before handing the
      // clip to transcription, so a cold model load no longer runs inside the
      // transcription request's timeout (#105955). syncSttLease never rejects,
      // and its budget is the lease request's own 180 s timeout — recording
      // and stop() were never bounded tighter than that for local STT.
      await warmupBarrierRef.current
      const transcript = (await onTranscribeAudio(result.audio)).trim()

      if (!transcript) {
        notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
      } else {
        onTranscript(transcript)
      }
    } catch (error) {
      notifyError(error, voiceCopy.transcriptionFailed)
    } finally {
      setVoiceStatus('idle')
      // The transcript settled (or failed): this session no longer needs the
      // engine held. The backend keeps the shared model resident regardless.
      void syncSttLease(VOICE_INPUT_LEASE, false, ownerRef.current)
      focusInput()
    }
  }

  const start = async () => {
    if (!onTranscribeAudio) {
      notify({ kind: 'warning', title: voiceCopy.unavailable, message: voiceCopy.transcriptionUnavailable })

      return
    }

    try {
      await handle.start({ onError: error => notifyError(error, voiceCopy.recordingFailed) })
      // The mic is open, so a transcript is coming: warm the backend's STT
      // engine now so a cold local model loads while the user is still
      // speaking instead of inside the transcription timeout (#105955).
      // Fire-and-forget for the MIC UX — but keep the promise: stop() awaits
      // it as the readiness barrier before transcription (see above). Pinned
      // to this composer's owner so a mid-flight gateway/profile switch
      // cannot misroute the eventual release; re-warms if a prior warm-up
      // failed or the idle watcher evicted the model.
      warmupBarrierRef.current = syncSttLease(VOICE_INPUT_LEASE, true, ownerRef.current)
      startedAtRef.current = Date.now()
      setElapsedSeconds(0)
      setVoiceStatus('recording')
      recordFeatureUse('voice_dictation')
      intervalRef.current = window.setInterval(() => setElapsedSeconds((Date.now() - startedAtRef.current) / 1000), 250)
      const cap = Math.max(1, Math.min(Math.trunc(maxRecordingSeconds), 600))
      timeoutRef.current = window.setTimeout(() => void stop(), cap * 1000)
    } catch (error) {
      setVoiceStatus('idle')
      notifyError(error, voiceCopy.recordingFailed)
    }
  }

  const dictate = () => {
    if (recording) {
      void stop()
    } else if (voiceStatus === 'idle') {
      void start()
    }
  }

  const voiceActivityState: VoiceActivityState = {
    elapsedSeconds,
    level,
    status: voiceStatus
  }

  return { dictate, voiceActivityState, voiceStatus }
}
