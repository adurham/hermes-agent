import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { fetchPetDialogue } from '@/hermes'
import { AlertCircle, Clock, type IconComponent } from '@/lib/icons'
import { playSpeechText } from '@/lib/voice-playback'
import { $petActivity, $petInfo, $petRealState, $petTurnCompletedBeat, type PetState } from '@/store/pet'
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
 *
 * OPTIONAL LLM DIALOGUE (`auxiliary.pet_dialogue.enabled`, off by default):
 * for those same two announced beats, first tries a short cheap-model call
 * (`POST /api/pet/dialogue`) for a fresh, context-aware line — e.g.
 * referencing what the agent just did instead of a generic "all done!". On
 * any failure (disabled, timeout, network error) it falls back to the
 * static pool below, so the feature never blocks or breaks. Never used for
 * the continuous run/review rotation — that stays 100% local regardless of
 * this setting, to avoid an LLM call on a ~2.6s cadence.
 *
 * PHRASING flavors by active pet (`$petInfo.slug`): the Hatsune Miku pet gets
 * a Vocaloid-themed line set (drawing on real fan-culture terms — "producer"
 * is the actual term Miku's community uses for anyone directing her, and
 * "san-kyuu" is a long-established "39" thank-you pun — not invented
 * character traits) instead of the generic default phrasing. Both the bubble
 * text and the spoken voice lines read from the same flavored spec/completion
 * lookup (see `specsForSlug`/`completionLinesForSlug`), so picking a flavor
 * changes both surfaces at once. Any other pet keeps the original generic
 * lines.
 */

type Tone = 'error' | 'wait'

interface Spec {
  lines: string[]
  glyph?: IconComponent
  tone?: Tone
}

type SpecSet = Partial<Record<PetState, Spec>>

// Default phrasing — used by every pet except a specifically-flavored slug
// below. Picked at random (no immediate repeat) for a bit of life. Keep them
// short — the bubble is tiny and never wraps.
const DEFAULT_SPECS: SpecSet = {
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

// Spoken only, never shown as bubble text — the jump/celebrate pose already
// carries the "done" visual, so a text bubble would fight the heart-puff
// animation. Picked the same random-no-repeat way as the bubble lines.
const DEFAULT_COMPLETION_LINES = ['all done!', 'finished!', 'wrapped up!', 'done and done!', 'all set!']

// Hatsune Miku flavor: idol/performer-persona phrasing, not a studio-engineer
// narrating her own signal chain. "Producer" (プロデューサー / "P") is the
// real term her community uses for whoever's directing her — genuine, not a
// Hermes invention — but it's direct address (she's speaking TO you), so it's
// reserved for `waiting`/completion where she's actually addressing the user,
// not `run`/`review` where she's narrating her own action. "39" ("san-kyuu" ≈
// "thank you") is a long-established fan pun, most visibly in her "Miku's
// Day" (3/9) tradition — kept as the anchor completion line. Kept short for
// the same bubble-width constraint as the default set.
const MIKU_SPECS: SpecSet = {
  run: {
    lines: [
      'here we go~',
      'showtime prep…',
      "let's roll!",
      'cueing up…',
      'warming up…',
      'on it!',
      'diving in!',
      'getting groovy…',
      'tuning it up…',
      'composing…'
    ]
  },
  review: {
    lines: [
      'hmm, gimme a sec…',
      'humming it over…',
      'listening close…',
      'thinking it through…',
      'checking the notes…',
      'in tune with it…',
      'rehearsing…',
      'sound-checking…',
      'piecing it together…',
      'one sec…'
    ]
  },
  failed: {
    glyph: AlertCircle,
    lines: ['off-key, oops', 'system hiccup', 'oof, rewind', 'needs a retake', 'ah, glitchy'],
    tone: 'error'
  },
  waiting: {
    glyph: Clock,
    lines: ['your solo, producer', 'stage is yours', 'awaiting cue', 'over to you, producer', 'standing by'],
    tone: 'wait'
  }
}

const MIKU_COMPLETION_LINES = [
  'san-kyuu for waiting!',
  'take a bow!',
  'encore-ready!',
  "that's a wrap!",
  'Yay!... done!'
]

// Slugs whose active pet gets the Vocaloid-flavored phrasing above. Petdex
// slugs are lowercase-hyphenated (see hermes_cli/config.py's
// `display.pet.slug` default of "hatsune-miku"); matched case-insensitively
// as a light guard against a hand-edited config using different casing.
const MIKU_SLUGS = new Set(['hatsune-miku', 'miku', 'hatsunemiku'])

function specsForSlug(slug: string | undefined): SpecSet {
  return slug && MIKU_SLUGS.has(slug.toLowerCase()) ? MIKU_SPECS : DEFAULT_SPECS
}

function completionLinesForSlug(slug: string | undefined): string[] {
  return slug && MIKU_SLUGS.has(slug.toLowerCase()) ? MIKU_COMPLETION_LINES : DEFAULT_COMPLETION_LINES
}

const TONE_COLOR: Record<Tone, string> = {
  error: 'var(--ui-red)',
  wait: 'var(--ui-yellow)'
}

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

/**
 * Speak one of the two ANNOUNCED beats. Tries the optional LLM-generated
 * line first (`fetchPetDialogue`); on ANY rejection — disabled (404),
 * timeout, network error, empty response — falls back to a random pick from
 * the static pool, so the feature degrades gracefully rather than going
 * silent. Never awaited by the caller; fire-and-forget with its own
 * catch-all, same as the rest of this component's TTS calls.
 */
function speakAnnouncedBeat(params: {
  beat: 'completed' | 'waiting'
  context: string
  fallbackLines: string[]
  petSlug: string
  voiceProvider: string
}): void {
  const { beat, context, fallbackLines, petSlug, voiceProvider } = params

  const speak = (text: string) =>
    playSpeechText(text, { provider: voiceProvider || undefined, source: 'pet' }).catch(() => {
      // Cosmetic feature — a TTS hiccup shouldn't surface an error toast.
    })

  void fetchPetDialogue({ beat, context, petSlug })
    .then(response => {
      const llmLine = response.ok ? response.line.trim() : ''

      void speak(llmLine || pick(fallbackLines, ''))
    })
    .catch(() => {
      // Disabled (404), timeout, or network error — all treated the same:
      // fall back to the static pool so the feature never goes silent.
      void speak(pick(fallbackLines, ''))
    })
}

export function PetBubble() {
  const state = useStore($petRealState)
  const activity = useStore($petActivity)
  const petSlug = useStore($petInfo).slug
  const turnCompletedBeat = useStore($petTurnCompletedBeat)
  const voiceEnabled = useStore($petVoiceEnabled)
  const voiceProvider = useStore($petVoiceProvider)
  const [line, setLine] = useState('')
  // Previous `specKey` — lets the "waiting" voice announcement fire once per
  // TRANSITION into waiting (not on every render while it holds), including a
  // second wait beat after an intervening run/review/idle stretch.
  const prevSpecKeyRef = useRef<null | PetState>(null)

  const specs = specsForSlug(petSlug)

  // Finish beats are carried by the sprite/mail icon; idle only speaks up when
  // it's actually the user's turn. Everything else maps to a mood spec.
  const specKey: null | PetState =
    state in specs ? state : state === 'idle' && activity.awaitingInput ? 'waiting' : null

  const rotating = specKey === 'run' || specKey === 'review'

  // Pick a fresh line on every mood change, then keep rotating (random, no
  // repeat) only while the agent is actively working/thinking. Bubble TEXT is
  // purely visual and unaffected by the voice toggle below.
  useEffect(() => {
    const spec = specKey ? specs[specKey] : null

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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `specs` is derived from `petSlug` each render; re-keying on it here would restart the interval on every unrelated re-render.
  }, [specKey, rotating, petSlug])

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

    speakAnnouncedBeat({
      beat: 'waiting',
      // No task-specific context passed for `waiting` — the line is about
      // the STATE (it's your turn), not about which specific prompt is
      // blocking, so there's little payoff versus the added complexity of
      // extracting prompt text from three different prompt-store shapes.
      context: '',
      fallbackLines: specs.waiting!.lines,
      petSlug: petSlug ?? '',
      voiceProvider
    })
  }, [specKey, voiceEnabled, voiceProvider, specs, petSlug])

  // Voice announces "turn finished" — keyed off a dedicated nonce
  // (`$petTurnCompletedBeat.seq`, bumped only by the gateway's real
  // completion event), NOT the shared `celebrate`/`jump` pose, which also
  // fires when the user just pets/clicks the mascot. Skips the initial
  // mount (seq === 0) so opening the app never announces a stale "done"
  // from a previous turn. `.context` (the assistant's final reply text)
  // feeds the optional LLM line so it can reference what actually happened.
  const mountedTurnSeqRef = useRef(turnCompletedBeat.seq)
  useEffect(() => {
    if (!voiceEnabled || turnCompletedBeat.seq === mountedTurnSeqRef.current) {
      return
    }

    mountedTurnSeqRef.current = turnCompletedBeat.seq

    speakAnnouncedBeat({
      beat: 'completed',
      context: turnCompletedBeat.context,
      fallbackLines: completionLinesForSlug(petSlug),
      petSlug: petSlug ?? '',
      voiceProvider
    })
  }, [turnCompletedBeat, voiceEnabled, voiceProvider, petSlug])

  const spec = specKey ? specs[specKey] : null

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
