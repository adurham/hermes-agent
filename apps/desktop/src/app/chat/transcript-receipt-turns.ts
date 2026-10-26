/**
 * A refreshed page that holds a whole turn the window drew live supersedes the
 * window's copy of that turn, decided by the turn's completion receipt.
 *
 * The stored-id merge (transcript-backfill `mergeOverlappingTail`) pairs rows
 * by stored id. A live turn reaches it with stored ids on only some of its rows:
 * a stopped turn binds none, a steered turn's receipt is not `complete`, and the
 * narration the stream sealed before each tool never has one. Those rows used
 * to travel as "unstored" rows beside the stored copies the page brings, so the
 * whole turn, prompt included, rendered twice (#139741), or the narration
 * re-rendered under the final answer once the duplicate final collapsed
 * (#141771).
 *
 * `message.complete.persisted_turn` names every stored row of the turn
 * (`row_ids`) and the turn's own prompt (`submission_id`, `user_row_id(s)`).
 * The page is one contiguous run of the newest stored rows, so a page that
 * holds the turn's first row and reaches its last row holds the whole turn.
 * The window's rows from that prompt to the receipt bubble are then the live
 * copy of rows the page carries, and only the page's copy is kept. A local
 * notice drawn between them (see `isLocalSystemNotice`) is no row of the turn
 * and is never dropped.
 */

import type { PersistedTurn } from '@hermes/shared'

import { transcriptRowIds } from '@/app/session/hooks/use-session-actions/pending-turn-identity'
import { type ChatMessage, isLocalSystemNotice } from '@/lib/chat-messages'

const isRowId = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value > 0

/** Index of the turn's prompt in the window, by the receipt's own identity. */
function receiptPromptIndex(window: ChatMessage[], receipt: PersistedTurn, before: number): number {
  const submission = typeof receipt.submission_id === 'string' && receipt.submission_id ? receipt.submission_id : ''
  const userRows = [receipt.user_row_id, ...(receipt.user_row_ids ?? [])].filter(isRowId)

  for (let index = before; index >= 0; index -= 1) {
    const message = window[index]

    if (message.role !== 'user') {
      continue
    }

    if (
      (submission && message.id === `user-${submission}`) ||
      (message.rowId !== undefined && userRows.includes(message.rowId))
    ) {
      // A steer row of the same turn sits after the prompt: keep walking while
      // an earlier user row still belongs to this receipt.
      const earlier = receiptPromptIndex(window, receipt, index - 1)

      return earlier >= 0 && earlier < index ? earlier : index
    }
  }

  return -1
}

/** Window indices of the receipt turn ending at `end`, or none when its identity is incomplete. */
function receiptTurnIndices(window: ChatMessage[], end: number, pageRows: Set<number>): number[] {
  const receipt = window[end].persistedTurn
  const rows = (receipt?.row_ids ?? []).filter(isRowId)

  if (!receipt || !rows.length) {
    return []
  }

  const first = Math.min(...rows)
  const last = Math.max(...rows)
  const pageLast = Math.max(...pageRows)

  if (!pageRows.has(first) || pageLast < last) {
    return []
  }

  const start = receiptPromptIndex(window, receipt, end)

  if (start < 0) {
    return []
  }

  const indices: number[] = []

  for (let index = start; index <= end; index += 1) {
    // A notice this window drew mid-turn (a /btw answer, a fallback switch) is
    // not a row of the turn: the page cannot carry it, so it stays.
    if (isLocalSystemNotice(window[index])) {
      continue
    }

    const ids = transcriptRowIds(window[index])

    // A row that carries a stored id outside the turn is not this turn's copy:
    // the pairing is wrong, so nothing is dropped.
    if (ids.some(id => id < first || id > last)) {
      return []
    }

    indices.push(index)
  }

  return indices
}

/** Drop the window rows whose turn the page holds whole, by the turn's receipt. */
export function dropReceiptTurnsThePageHolds(window: ChatMessage[], page: ChatMessage[]): ChatMessage[] {
  const pageRows = new Set(page.flatMap(transcriptRowIds))

  if (!pageRows.size) {
    return window
  }

  const dropped = new Set<number>()

  window.forEach((message, index) => {
    if (message.persistedTurn && !message.pending) {
      receiptTurnIndices(window, index, pageRows).forEach(at => dropped.add(at))
    }
  })

  return dropped.size ? window.filter((_, index) => !dropped.has(index)) : window
}

/**
 * The newest page can start inside an older tool turn. History folds that turn
 * under its first stored row, so the page's fold carries only the rows from
 * the page boundary on, under a different first row than the window's fold of
 * the same turn. Paired by first row, both folds rendered and the turn's later
 * narration and answer appeared again under the whole turn (#141771). A page
 * fold whose stored rows the window's fold all carries, while the window's
 * fold also carries rows older than the page, is that turn cut by the page
 * boundary: the window's whole fold stays and the page's partial copy goes.
 */
export function dropPageFoldsCutByTheBoundary(window: ChatMessage[], page: ChatMessage[]): ChatMessage[] {
  const pageRows = page.flatMap(transcriptRowIds)

  if (!pageRows.length) {
    return page
  }

  const pageFirst = Math.min(...pageRows)

  const wholeFolds = window
    .map(message => new Set(transcriptRowIds(message)))
    .filter(rows => [...rows].some(id => id < pageFirst))

  const cut = (message: ChatMessage) => {
    const rows = transcriptRowIds(message)

    return rows.length > 0 && wholeFolds.some(whole => rows.every(id => whole.has(id)))
  }

  return page.some(cut) ? page.filter(message => !cut(message)) : page
}
