import { noteActiveTreeGroup, revealTreePane } from '@/components/pane-shell/tree/store'
import { freshDraftScope, requestComposerDraftSync, takeSessionDraft } from '@/store/composer'
import { $queuedPromptsBySession } from '@/store/composer-queue'
import { $activeSessionId, $selectedStoredSessionId, $sessions, sessionMatchesStoredId } from '@/store/session'
import { $sessionStates, $sessionTiles, focusOpenSession, type SessionTile } from '@/store/session-states'

import { mainChatOccupied } from '../open-session'
import { $workspaceIsPage } from '../routes'

const hasQueued = (...keys: (string | undefined)[]): boolean => {
  const queues = $queuedPromptsBySession.get()

  return keys.some(key => Boolean(key && queues[key]?.length))
}

// A composer stashes its text on a debounce, so flush the live editor before
// reading: a word typed a moment ago still counts as touched.
const draftIsEmpty = (target: string, scope: string): boolean => {
  requestComposerDraftSync('flush', target)
  const draft = takeSessionDraft(scope)

  return !draft.text.trim() && draft.attachments.length === 0
}

/**
 * A Sessions tab nobody has used: no listed row, a bound runtime whose state
 * reads zero messages and idle, nothing typed, nothing queued. A tab whose
 * state is not known yet (restored, still resuming, failed) is NOT untouched:
 * unknown never reads as empty.
 */
export function isUntouchedDraftTile(tile: SessionTile): boolean {
  if ((tile.workspaceMode ?? 'sessions') !== 'sessions' || tile.error || !tile.runtimeId) {
    return false
  }

  const state = $sessionStates.get()[tile.runtimeId]

  return (
    Boolean(state) &&
    state.messages.length === 0 &&
    !state.busy &&
    !$sessions.get().some(session => sessionMatchesStoredId(session, tile.storedSessionId)) &&
    draftIsEmpty(`tile:${tile.storedSessionId}`, tile.storedSessionId) &&
    !hasQueued(tile.storedSessionId, tile.runtimeId)
  )
}

/** Main holds a blank new chat: no session yet, no page over it, nothing typed. */
function mainIsUntouchedDraft(): boolean {
  return (
    !mainChatOccupied($activeSessionId.get(), $selectedStoredSessionId.get()) &&
    !$workspaceIsPage.get() &&
    draftIsEmpty('main', freshDraftScope())
  )
}

/**
 * The tab-strip "+" / ⌘T opens a new session as a tab. When this window
 * already shows a blank one, front it instead of minting another identical
 * "New session" tab, so repeated clicks land on the one pending draft (#145594).
 * Returns true when a draft was fronted and the caller must not create one.
 */
export function frontUntouchedDraftTab(): boolean {
  if (mainIsUntouchedDraft()) {
    revealTreePane('workspace')
    noteActiveTreeGroup(null)

    return true
  }

  const tile = $sessionTiles.get().find(isUntouchedDraftTile)

  return Boolean(tile && focusOpenSession(tile.storedSessionId) === 'tile')
}
