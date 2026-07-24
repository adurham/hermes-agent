import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { AlertCircle, Clock, type IconComponent } from '@/lib/icons'
import { playSpeechText } from '@/lib/voice-playback'
import { $petActivity, $petRealState, $petTurnCompletedBeat, type PetState } from '@/store/pet'
import { $petVoiceEnabled, $petVoiceProvider } from '@/store/pet-voice'

/**
 * Speech bubble + status glyph for the popped-out pet overlay — the
 * "notification" half of the mascot. It externalizes what the agent is doing
 * (Codex-style) so a glance at the desktop pet replaces switching back to the
 * window. The in-window pet doesn't show it (the app itself is the surface);
 * only the overlay renders it.
 *
 * Text is derived from `$petRealState` (agent activity only) rather than the
 * sprite-facing `$petState`, which also carries the roam loop's own walk/hop
 * pose — reading that would show a "working…" bubble for a pet that's simply
 * strolling while idle. The bubble is shown only when there's something
 * worth saying (working / reviewing / a transient done/error beat / waiting
 * on the user) and is hidden at plain idle or while merely wandering.
 *
 * VOICE (opt-in, `display.pet.voice_enabled`) is deliberately narrower than
 * the bubble TEXT: it only announces two things — the agent needs you
 * (`waiting`) and a turn just finished (`$petTurnCompletedBeat`) — never the
 * continuous "working…"/"thinking…" chatter the bubble shows while a turn
 * runs. That distinction is why voice can't just watch the bubble's own
 * `specKey`/`line` state: `run`/`review` intentionally never speak.
 */

type Tone = 'error' | 'wait'

interface Spec {
  lines: string[]
  glyph?: IconComponent
  tone?: Tone
}

// Phrasings per mood, picked at random (no immediate repeat) for a bit of life.
// Keep them short — the bubble is tiny and never wraps.
const SPECS: Partial<Record<PetState, Spec>> = {
  run: {
    lines: [
      'working…',
      'on it…',
      'crunching…',
      'tinkering…',
      'cooking…',
      'in the weeds…',
      'wiring it up…',
      'making moves…',
      'heads down…',
      'hammering away…'
    ]
  },
  review: {
    lines: [
      'thinking…',
      'reading…',
      'reviewing…',
      'pondering…',
      'connecting dots…',
      'sizing it up…',
      'tracing it…',
      'mulling…',
      'scheming…',
      'hmm…'
    ]
  },
  failed: {
    glyph: AlertCircle,
    lines: ['hit a snag', 'welp', 'that broke', 'oof', 'snagged'],
    tone: 'error'
  },
  waiting: {
    glyph: Clock,
    lines: ['your turn', 'all yours', 'over to you', 'ball’s in your court', 'awaiting orders'],
    tone: 'wait'
  }
}

const TONE_COLOR: Record<Tone, string> = {
  error: 'var(--ui-red)',
  wait: 'var(--ui-yellow)'
}

// Spoken only, never shown as bubble text — the jump/celebrate pose already
// carries the "done" visual, so a text bubble would fight the heart-puff
// animation. Picked the same random-no-repeat way as the bubble lines.
const COMPLETION_LINES = ['all done!', 'finished!', 'wrapped up!', 'done and done!', 'all set!']

// Random pick that avoids repeating the line we're already showing.
function pick(lines: string[], prev: string): string {
  if (lines.length <= 1) {
    return lines[0] ?? ''
  }

  let next = prev

  while (next === prev) {
    next = lines[Math.floor(Math.random() * lines.length)]
  }

  return next
}

export function PetBubble() {
  const state = useStore($petRealState)
  const activity = useStore($petActivity)
  const turnCompletedBeat = useStore($petTurnCompletedBeat)
  const voiceEnabled = useStore($petVoiceEnabled)
  const voiceProvider = useStore($petVoiceProvider)
  const [line, setLine] = useState('')
  // Previous `specKey` — lets the "waiting" voice announcement fire once per
  // TRANSITION into waiting (not on every render while it holds), including a
  // second wait beat after an intervening run/review/idle stretch.
  const prevSpecKeyRef = useRef<null | PetState>(null)

  // Finish beats are carried by the sprite/mail icon; idle only speaks up when
  // it's actually the user's turn. Everything else maps to a mood spec.
  const specKey: null | PetState =
    state in SPECS ? state : state === 'idle' && activity.awaitingInput ? 'waiting' : null

  const rotating = specKey === 'run' || specKey === 'review'

  // Pick a fresh line on every mood change, then keep rotating (random, no
  // repeat) only while the agent is actively working/thinking. Bubble TEXT is
  // purely visual and unaffected by the voice toggle below.
  useEffect(() => {
    const spec = specKey ? SPECS[specKey] : null

    if (!spec) {
      setLine('')

      return undefined
    }

    setLine(prev => pick(spec.lines, prev))

    if (!rotating || spec.lines.length <= 1) {
      return undefined
    }

    const id = window.setInterval(() => setLine(prev => pick(spec.lines, prev)), 2600)

    return () => window.clearInterval(id)
  }, [specKey, rotating])

  // Voice announces "needs you" — fires once per transition INTO `waiting`
  // (an approval/clarify/sudo prompt or plain end-of-turn idle blocking on
  // the user), never the continuous "working…"/"thinking…" bubble chatter,
  // which would be constant narration over anything long-running.
  useEffect(() => {
    const previous = prevSpecKeyRef.current
    prevSpecKeyRef.current = specKey

    if (!voiceEnabled || specKey !== 'waiting' || previous === 'waiting') {
      return
    }

    const toSpeak = pick(SPECS.waiting!.lines, '')

    void playSpeechText(toSpeak, { provider: voiceProvider || undefined, source: 'pet' }).catch(() => {
      // Cosmetic feature — a TTS hiccup shouldn't surface an error toast.
    })
  }, [specKey, voiceEnabled, voiceProvider])

  // Voice announces "turn finished" — keyed off a dedicated nonce
  // (`$petTurnCompletedBeat`, bumped only by the gateway's real completion
  // event), NOT the shared `celebrate`/`jump` pose, which also fires when the
  // user just pets/clicks the mascot. Skips the initial mount (beat === 0)
  // so opening the app never announces a stale "done" from a previous turn.
  const mountedTurnBeatRef = useRef(turnCompletedBeat)
  useEffect(() => {
    if (!voiceEnabled || turnCompletedBeat === mountedTurnBeatRef.current) {
      return
    }

    mountedTurnBeatRef.current = turnCompletedBeat

    const toSpeak = pick(COMPLETION_LINES, '')

    void playSpeechText(toSpeak, { provider: voiceProvider || undefined, source: 'pet' }).catch(() => {
      // Cosmetic feature — a TTS hiccup shouldn't surface an error toast.
    })
  }, [turnCompletedBeat, voiceEnabled, voiceProvider])

  const spec = specKey ? SPECS[specKey] : null

  if (!spec) {
    return null
  }

  const Glyph = spec.glyph
  const text = line || spec.lines[0]
  const hasText = Boolean(text)

  return (
    <div
      style={{
        alignItems: 'center',
        // Solid, theme-driven surface (the prior --ui-bg-card mixes in
        // `transparent`, so the bubble was see-through).
        background: 'var(--ui-bg-elevated)',
        border: '1px solid var(--ui-stroke-secondary)',
        borderRadius: hasText ? 10 : 999,
        boxShadow: '0 4px 14px rgba(0,0,0,0.22)',
        color: 'var(--foreground)',
        display: 'inline-flex',
        fontSize: 11,
        fontWeight: 500,
        gap: hasText ? 5 : 0,
        lineHeight: 1,
        // Glyph-only bubbles collapse to a tight, symmetric badge.
        padding: hasText ? '5px 8px' : 5,
        pointerEvents: 'none',
        whiteSpace: 'nowrap'
      }}
    >
      {Glyph && (
        <span style={{ display: 'inline-flex' }}>
          <Glyph style={{ color: spec.tone ? TONE_COLOR[spec.tone] : 'currentColor', height: 13, width: 13 }} />
        </span>
      )}
      {text}
    </div>
  )
}
