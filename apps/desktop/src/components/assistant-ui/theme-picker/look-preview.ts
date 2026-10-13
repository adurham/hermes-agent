import { useEffect, useRef, useState } from 'react'

import { snapshotChatLayout } from '@/components/onboarding/first-run/assembly'
import { applyAppearance } from '@/store/appearance-apply'
import { $accentOverride, setAccentOverride } from '@/themes/accent-override'
import { NOUS_ACCENT, resolveAccentPick } from '@/themes/accents'

/** A staged pick; `value` null is the theme's own accent. */
export interface StagedLook {
  value: null | string
}

interface LiveLook {
  apply: (value: null | string, dark: boolean) => void
  snapshot: () => () => void
}

// The accent previews through the unsaved override so nothing is persisted before Continue; the theme's own
// accent previews as Nous blue because an empty override would keep the current colour.
const ACCENT: LiveLook = {
  apply: (value, dark) => setAccentOverride(resolveAccentPick(value ?? 'theme', dark)?.hex ?? NOUS_ACCENT),
  snapshot: () => {
    const override = $accentOverride.get()

    return () => setAccentOverride(override)
  }
}

const LAYOUT: LiveLook = {
  apply: value => {
    if (value) {
      applyAppearance({ layout: value })
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

/** Click previews a look live and keeps the value it replaced; a card that unmounts without `settle`
 *  (cancel, timeout, Skip setup, words in the composer) puts that value back. */
export function useLookPreview(kind: 'accent' | 'layout', dark: boolean): LookPreview {
  const live = kind === 'accent' ? ACCENT : LAYOUT
  const [staged, setStaged] = useState<null | StagedLook>(null)
  const revert = useRef<(() => void) | null>(null)
  const settled = useRef(false)

  useEffect(
    () => () => {
      if (!settled.current) {
        revert.current?.()
      }

      revert.current = null
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
      settled.current = send(staged.value)
    },
    stage: value => {
      revert.current ??= live.snapshot()
      live.apply(value, dark)
      setStaged({ value })
    },
    staged
  }
}
