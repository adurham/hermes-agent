import { normalizeWs } from './parts'
import type { ChatMessage, ChatMessagePart } from './types'

/**
 * One reply renders once per turn, whichever paths delivered it.
 *
 * A turn's reply reaches the renderer more than once: as live stream events,
 * as the committed row the backend persists before each tool round, and again
 * through every history read (re-attach to a running turn, reload, switch-back,
 * reconnect replay, the in-flight journal). Each merge path used to decide on
 * its own whether two bubbles were the same reply, and every path that decided
 * wrong painted the reply twice while `state.db` held one row. This fold is the
 * one place that decides, at the render boundary every surface shares, so a
 * missed merge can no longer reach the screen.
 *
 * Identity is the content a bubble shows, compared in order: its text, its tool
 * calls (name + id + arguments) and its other parts. Reasoning is ignored. A
 * turn spans the assistant and system rows between two visible user rows.
 *
 * - A bubble whose content another bubble of the turn already shows in order is
 *   the same reply delivered twice. The committed copy (rowId) wins over a
 *   live copy, then the copy carrying the turn's footer (not interim), then the
 *   earlier one.
 * - A bubble that starts with content an earlier bubble shows, including at
 *   least one tool call, is a live segment re-drawn after a re-attach: that
 *   leading run is dropped and the new remainder stays (#127288).
 *
 * Text alone never trims a bubble that adds anything new: equal commentary in
 * two tool rounds is authored twice and stays, matching hydration
 * (`dedupeRepeatedTextInParts`). Render-only; the store keeps every row.
 */

type Atom =
  | { kind: 'other'; key: string }
  | { kind: 'text'; key: string }
  | { args: string; id: string; kind: 'tool'; name: string }

interface Bubble {
  atoms: Atom[]
  /** Index into `parts` of the part each atom came from. */
  partIndex: number[]
}

const PART_METADATA = new Set([
  'completedAt',
  'interrupted',
  'mediaSource',
  'sourceRowId',
  'storedResultToolName',
  'timestamp',
  'toolResultMetadata',
  'unpairedStoredToolResult'
])

const DUP_SUFFIX_RE = /-dup-\d+$/

function stableArgs(part: Extract<ChatMessagePart, { type: 'tool-call' }>): string {
  if (part.args && typeof part.args === 'object' && Object.keys(part.args).length) {
    return JSON.stringify(part.args, Object.keys(part.args).sort())
  }

  const text = typeof part.argsText === 'string' ? part.argsText.trim() : ''

  if (!text) {
    return ''
  }

  try {
    const parsed = JSON.parse(text)

    return parsed && typeof parsed === 'object' ? JSON.stringify(parsed, Object.keys(parsed).sort()) : text
  } catch {
    return text
  }
}

function atomOf(part: ChatMessagePart): Atom | null {
  if (part.type === 'reasoning') {
    return null
  }

  if (part.type === 'text') {
    const key = normalizeWs(part.mediaSource ?? part.text)

    return key ? { kind: 'text', key } : null
  }

  if (part.type === 'tool-call') {
    return {
      args: stableArgs(part),
      id: (part.toolCallId ?? '').replace(DUP_SUFFIX_RE, ''),
      kind: 'tool',
      name: part.toolName ?? ''
    }
  }

  const body = Object.fromEntries(Object.entries(part).filter(([key]) => !PART_METADATA.has(key)))

  return { kind: 'other', key: JSON.stringify(body) }
}

function sameAtom(a: Atom, b: Atom): boolean {
  if (a.kind !== b.kind) {
    return false
  }

  if (a.kind === 'tool' && b.kind === 'tool') {
    // A live tool part can carry no arguments yet; ids are per turn.
    return a.name === b.name && a.id === b.id && (!a.args || !b.args || a.args === b.args)
  }

  return (a as { key: string }).key === (b as { key: string }).key
}

const bubbleCache = new WeakMap<ChatMessagePart[], Bubble>()

function bubbleOf(message: ChatMessage): Bubble {
  const cached = bubbleCache.get(message.parts)

  if (cached) {
    return cached
  }

  const atoms: Atom[] = []
  const partIndex: number[] = []

  message.parts.forEach((part, index) => {
    const atom = atomOf(part)

    if (atom) {
      atoms.push(atom)
      partIndex.push(index)
    }
  })

  const bubble = { atoms, partIndex }
  bubbleCache.set(message.parts, bubble)

  return bubble
}

/** `inner` appears in `outer` in order (a subsequence). */
function covers(outer: Atom[], inner: Atom[]): boolean {
  let at = 0

  for (const atom of outer) {
    if (at < inner.length && sameAtom(atom, inner[at])) {
      at += 1
    }
  }

  return at === inner.length
}

/** Length of the longest prefix of `atoms` found as a contiguous run in `earlier`. */
function coveredPrefix(earlier: Atom[], atoms: Atom[]): number {
  let best = 0

  for (let start = 0; start < earlier.length; start += 1) {
    let length = 0

    while (
      length < atoms.length &&
      start + length < earlier.length &&
      sameAtom(earlier[start + length], atoms[length])
    ) {
      length += 1
    }

    best = Math.max(best, length)
  }

  return best
}

// Branch alternatives (regenerate) share content by design and render one at a time.
function isReplyBubble(message: ChatMessage): boolean {
  return message.role === 'assistant' && !message.hidden && !message.error && !message.branchGroupId
}

/** Prefer the committed copy, then the copy that carries the turn's footer. */
function rank(message: ChatMessage): number {
  return (message.rowId === undefined ? 0 : 2) + (message.interim ? 0 : 1)
}

interface TurnFold {
  dropped: Set<ChatMessage>
  trimmed: Map<ChatMessage, number>
}

function foldTurn(turn: ChatMessage[], fold: TurnFold): void {
  const bubbles = turn.map(bubbleOf)

  for (let i = 0; i < turn.length; i += 1) {
    if (!bubbles[i].atoms.length || fold.dropped.has(turn[i])) {
      continue
    }

    for (let j = 0; j < turn.length; j += 1) {
      if (j === i || fold.dropped.has(turn[j]) || !covers(bubbles[j].atoms, bubbles[i].atoms)) {
        continue
      }

      const otherIsEarlier = j < i
      const laterWins = otherIsEarlier ? rank(turn[i]) > rank(turn[j]) : rank(turn[j]) > rank(turn[i])

      // Between equal copies the better-ranked one stays (ties keep the
      // first). An earlier superset always wins; a later superset retires an
      // earlier subset only when it outranks it (the committed row over its
      // live copy), so a growing live bubble never hides committed history.
      const drop = covers(bubbles[i].atoms, bubbles[j].atoms)
        ? otherIsEarlier !== laterWins
        : otherIsEarlier || laterWins

      if (drop) {
        fold.dropped.add(turn[i])

        break
      }
    }
  }

  for (let i = 1; i < turn.length; i += 1) {
    const { atoms, partIndex } = bubbles[i]

    if (fold.dropped.has(turn[i]) || !atoms.length) {
      continue
    }

    let prefix = 0

    for (let j = 0; j < i; j += 1) {
      if (!fold.dropped.has(turn[j])) {
        prefix = Math.max(prefix, coveredPrefix(bubbles[j].atoms, atoms))
      }
    }

    if (prefix > 0 && prefix < atoms.length && atoms.slice(0, prefix).some(atom => atom.kind === 'tool')) {
      fold.trimmed.set(turn[i], partIndex[prefix - 1] + 1)
    }
  }
}

export type TurnReplyFoldCache = WeakMap<ChatMessage, { cut: number; message: ChatMessage }>

export function createTurnReplyFoldCache(): TurnReplyFoldCache {
  return new WeakMap()
}

/**
 * Drop the bubbles (and leading segments) of a turn that repeat what another
 * bubble of the same turn already shows. Returns `messages` itself when there
 * is nothing to fold, and stable trimmed objects across calls via `cache`.
 */
export function foldTurnReplyDuplicates(messages: ChatMessage[], cache?: TurnReplyFoldCache): ChatMessage[] {
  const fold: TurnFold = { dropped: new Set(), trimmed: new Map() }
  let turn: ChatMessage[] = []

  const flush = () => {
    if (turn.length > 1) {
      foldTurn(turn, fold)
    }

    turn = []
  }

  for (const message of messages) {
    if (message.role === 'user' && !message.hidden) {
      flush()
    } else if (isReplyBubble(message)) {
      turn.push(message)
    }
  }

  flush()

  if (!fold.dropped.size && !fold.trimmed.size) {
    return messages
  }

  const out: ChatMessage[] = []

  for (const message of messages) {
    if (fold.dropped.has(message)) {
      continue
    }

    const cut = fold.trimmed.get(message)

    if (cut === undefined) {
      out.push(message)

      continue
    }

    const cached = cache?.get(message)

    if (cached && cached.cut === cut) {
      out.push(cached.message)

      continue
    }

    const trimmed = { ...message, parts: message.parts.slice(cut) }
    cache?.set(message, { cut, message: trimmed })
    out.push(trimmed)
  }

  return out
}
