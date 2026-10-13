import { atom, computed, type ReadableAtom } from 'nanostores'

/** Answers a card parked on `sessionId` with composer text: null when no card of this kind takes it,
 *  else a promise of whether the answer reached the backend. */
export type ParkedCardAnswer = (sessionId: string, text: string) => null | Promise<boolean>

/** One card kind: its typed-answer handler, whether a card of it waits on a session, and the store that
 *  predicate reads (watched so the composer redraws when a card parks or settles). */
export interface ParkedCardKind {
  $requests: ReadableAtom<object>
  answer: ParkedCardAnswer
  waiting: (sessionId: string) => boolean
}

const kinds = new Map<string, ParkedCardKind>()

// Bumped when a kind registers, unregisters or its store changes, so per-session views re-ask every kind.
const $revision = atom(0)
const bump = () => $revision.set($revision.get() + 1)

/** Register a card kind under `id`. Returns the unregister function. */
export function registerParkedCardAnswer(id: string, kind: ParkedCardKind): () => void {
  kinds.set(id, kind)
  const unlisten = kind.$requests.listen(bump)
  bump()

  return () => {
    unlisten()

    if (kinds.get(id) === kind) {
      kinds.delete(id)
      bump()
    }
  }
}

/**
 * Offer composer text to every card waiting on `sessionId`, in registration order. True when one
 * took it as its answer: the composer then neither sends nor steers the text, and `onFailed` runs
 * if that answer never reaches the backend, so the composer can give the words back.
 */
export function answerParkedCard(sessionId: string, text: string, onFailed: () => void = () => {}): boolean {
  for (const kind of kinds.values()) {
    const sent = kind.answer(sessionId, text)

    if (sent) {
      void sent
        .then(delivered => {
          if (!delivered) {
            onFailed()
          }
        })
        .catch(onFailed)

      return true
    }
  }

  return false
}

/** Whether any card waits on `sessionId`, so typed text would answer it. */
export function parkedCardWaiting(sessionId: string): boolean {
  return [...kinds.values()].some(kind => kind.waiting(sessionId))
}

/** `parkedCardWaiting` for one session, as a store the composer can subscribe to. */
export const sessionParkedCardWaiting = (sessionId: null | string) =>
  computed($revision, () => (sessionId ? parkedCardWaiting(sessionId) : false))
