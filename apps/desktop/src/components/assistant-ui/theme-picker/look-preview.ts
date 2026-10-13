import { useEffect, useRef, useState } from 'react'

import { type PreviewUndo, snapshotChatLayout } from '@/components/onboarding/first-run/assembly'
import { applyDesktopLayoutPreset } from '@/store/pane-focus'
import { $accentOverride, setAccentOverride } from '@/themes/accent-override'
import { NOUS_ACCENT, resolveAccentPick } from '@/themes/accents'

/** A staged pick; `value` null is the theme's own accent. */
export interface StagedLook {
  value: null | string
}

interface LiveLook {
  apply: (value: null | string, dark: boolean) => void
  snapshot: () => PreviewUndo
}

// The accent previews through the unsaved override so nothing is persisted before Continue; the theme's own
// accent previews as Nous blue because an empty override would keep the current colour.
const ACCENT: LiveLook = {
  apply: (value, dark) => setAccentOverride(resolveAccentPick(value ?? 'theme', dark)?.hex ?? NOUS_ACCENT),
  snapshot: () => {
    const override = $accentOverride.get()

    return { keep: send => send(), revert: () => setAccentOverride(override) }
  }
}

// A layout previews as an arrangement in the current mode; Continue applies it for real (switching
// Simple/Advanced when the preset needs it), so a cancelled preview never touches either mode's saved layout.
const LAYOUT: LiveLook = {
  apply: value => {
    if (value) {
      applyDesktopLayoutPreset(value)
    }
  },
  snapshot: snapshotChatLayout
}

export interface LookPreview {
  /** Answer with the staged pick; leaving keeps it when `send` returns true. */
  commit: (send: (value: null | string) => boolean) => void
  stage: (value: null | string) => void
  staged: null | StagedLook
}

/** Click previews a look live; a card that unmounts without Continue (cancel, timeout, Skip setup,
 *  words in the composer) puts back the value from before the preview. */
export function useLookPreview(kind: 'accent' | 'layout', dark: boolean): LookPreview {
  const live = kind === 'accent' ? ACCENT : LAYOUT
  const [staged, setStaged] = useState<null | StagedLook>(null)
  // The value from before this card's first preview.
  const undo = useRef<null | PreviewUndo>(null)
  const settled = useRef(false)

  useEffect(
    () => () => {
      if (!settled.current) {
        undo.current?.revert()
      }
    },
    []
  )

  return {
    // Settled before sending: the answer can unmount the card before `send` returns.
    commit: send => {
      if (!staged) {
        return
      }

      settled.current = true
      settled.current = undo.current ? undo.current.keep(() => send(staged.value)) : send(staged.value)

      // A failed send keeps the card, so its preview goes back on screen.
      if (!settled.current && undo.current) {
        live.apply(staged.value, dark)
      }
    },
    stage: value => {
      undo.current ??= live.snapshot()
      live.apply(value, dark)
      setStaged({ value })
    },
    staged
  }
}
