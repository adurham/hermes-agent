import { extendRefreshPageToOverlap, graftRefreshedTailOntoBackfill, olderPageReader } from '@/app/chat/transcript-backfill'
import { getLatestSessionMessages, type ProfileScope } from '@/hermes'
import { type ChatMessage, toChatMessages } from '@/lib/chat-messages'
import type { SessionMessagesResponse, SessionResumeResult } from '@/types/hermes'

import { reconcileDurableHistory } from './utils'

export interface GraftedTranscriptPage {
  page: SessionMessagesResponse
  /** The REST page grafted onto the view's older backfilled pages. */
  transcript: ChatMessage[]
  /** That transcript reconciled with the view's local rows. */
  snapshot: ChatMessage[]
}

/** Graft a newest-tail REST page onto the view: older pages kept, local tail reconciled. */
export async function graftTranscriptPage(
  page: SessionMessagesResponse,
  previousMessages: ChatMessage[],
  storedSessionId: string,
  scope: ProfileScope
): Promise<GraftedTranscriptPage> {
  // A long turn can push every rendered row off the newest page; read older
  // pages until they overlap so the graft keeps earlier history.
  const tail = await extendRefreshPageToOverlap(
    toChatMessages(page.messages),
    previousMessages,
    olderPageReader(storedSessionId, scope, page)
  )

  const transcript = graftRefreshedTailOntoBackfill(tail, previousMessages)

  return { page, transcript, snapshot: reconcileDurableHistory(transcript, previousMessages) }
}

/**
 * The cold resume reads REST concurrently with `session.resume`, so that read
 * can land BEFORE the resume rebinds this socket to the session. When the turn
 * commits in between, its tail and terminal frames go to the dropped socket:
 * the reconnect replay has already been answered, so the wire never delivers
 * them. The early page then lacks the committed reply, and the view keeps its
 * local stream row, stuck at the last streamed chunk. An idle resume answer
 * is the barrier (the reply is durable once it returns), so a snapshot whose
 * tail is still a local row is read once more.
 */
async function rereadPageAfterResumeBarrier(
  resumed: Pick<SessionResumeResult, 'inflight' | 'queued'> & { running?: boolean },
  early: GraftedTranscriptPage | null,
  storedSessionId: string,
  scope: ProfileScope
): Promise<GraftedTranscriptPage | null> {
  const tail = early?.snapshot.at(-1)

  if (!early || !tail || resumed.running !== false || resumed.inflight || resumed.queued) {
    return null
  }

  if (early.transcript.some(message => message.id === tail.id)) {
    return null
  }

  const page = await getLatestSessionMessages(storedSessionId, scope).catch(() => null)

  if (!page || (early.page.session_id && page.session_id && page.session_id !== early.page.session_id)) {
    return null
  }

  return graftTranscriptPage(page, early.snapshot, storedSessionId, scope)
}

export interface ColdPrefetchState {
  localSnapshot: ChatMessage[]
  prefetchedPage: GraftedTranscriptPage | null
  prefetchedResult: SessionMessagesResponse | null
  prefetchedTranscriptMessages: ChatMessage[] | null
}

/** The cold resume's prefetch state once the resume answered: re-read past the barrier when it raced the commit. */
export async function settlePrefetchAtResumeBarrier(
  resumed: Pick<SessionResumeResult, 'inflight' | 'queued'> & { running?: boolean },
  state: ColdPrefetchState,
  storedSessionId: string,
  scope: ProfileScope
): Promise<ColdPrefetchState> {
  const reread = await rereadPageAfterResumeBarrier(resumed, state.prefetchedPage, storedSessionId, scope)

  return reread
    ? {
        localSnapshot: reread.snapshot,
        prefetchedPage: reread,
        prefetchedResult: reread.page,
        prefetchedTranscriptMessages: reread.transcript
      }
    : state
}
