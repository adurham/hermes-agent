import { useEffect } from 'react'

import { $workspaceMode } from '@/components/pane-shell/workspace-scope'
import { $showAllProfiles, setShowAllProfiles } from '@/store/profile'

/**
 * Bot Mode turns the unified Sessions list on: opening a bot chat publishes
 * `keepAllProfilesScope`, so the sidebar shows every profile's sessions instead
 * of collapsing onto the bot's own backend (a bot chat is hidden from the
 * Sessions list, which would otherwise look like the sessions vanished).
 *
 * That flip belongs to the Bot Mode snapshot, not to the user's choice — so
 * leaving Bot Mode must put the Sessions sidebar back on the gateway the user
 * was on. Otherwise the `BOTS → SESSIONS` tab switch leaves the sidebar
 * showing every gateway's sessions at once, with the profile pill still naming
 * the chosen one.
 *
 * Capture ONCE on the way in: Bot Mode re-publishes its scope for every newly
 * selected bot, and re-capturing after our own flip would restore `true`.
 * Returns the unsubscribe for the caller's effect cleanup.
 */
export function restoreSessionsScopeAfterBots(
  read: () => boolean = () => $showAllProfiles.get(),
  write: (value: boolean) => void = setShowAllProfiles
): () => void {
  let captured: boolean | null = null

  return $workspaceMode.listen((mode, previousMode) => {
    if (mode === 'bots') {
      if (previousMode !== 'bots' && captured === null) {
        captured = read()
      }

      return
    }

    if (previousMode === 'bots' && captured !== null) {
      write(captured)
      captured = null
    }
  })
}

/** Keep the Sessions sidebar's profile scope across Bot Mode visits. Mounted
 *  once by the shell wiring, beside the other session/sidebar listeners. */
export function useSessionsScopeAfterBots(): void {
  useEffect(() => restoreSessionsScopeAfterBots(), [])
}
