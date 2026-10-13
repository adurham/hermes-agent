import { useEffect, useRef, useState } from 'react'

import { snapshotChatLayout } from '@/components/onboarding/first-run/assembly'
import { applyDesktopLayoutPreset } from '@/store/pane-focus'
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

// One undo per look while any card previews it: two open cards share the value from before the first
// preview, and only the last card to close without Continue puts it back.
/** One open card's claim on a look's undo. */
interface PreviewOwner {
  readonly card: symbol
}

interface Baseline {
  owners: Set<PreviewOwner>
  revert: () => void
}

const baselines = new Map<LiveLook, Baseline>()

function release(live: LiveLook, owner: PreviewOwner): void {
  const baseline = baselines.get(live)

  if (baseline?.owners.delete(owner) && baseline.owners.size === 0) {
    baselines.delete(live)
    baseline.revert()
  }
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
  const owner = useRef<PreviewOwner>({ card: Symbol('look-preview') }).current
  const settled = useRef(false)

  useEffect(
    () => () => {
      if (!settled.current) {
        release(live, owner)
      }
    },
    [live, owner]
  )

  return {
    // Settled before sending: the answer can unmount the card before `send` returns.
    commit: send => {
      if (!staged) {
        return
      }

      settled.current = true
      settled.current = send(staged.value)

      // The sent value is the new truth: no open card puts the old one back.
      if (settled.current) {
        baselines.delete(live)
      }
    },
    stage: value => {
      const baseline = baselines.get(live) ?? { owners: new Set<PreviewOwner>(), revert: live.snapshot() }

      baseline.owners.add(owner)
      baselines.set(live, baseline)
      live.apply(value, dark)
      setStaged({ value })
    },
    staged
  }
}
