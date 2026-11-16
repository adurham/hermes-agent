import { assistantTextPart, type ChatMessagePart, mergeFinalAssistantText } from '@/lib/chat-messages'

/** One live bubble may hold several model responses when an equal interim
 * callback is suppressed. The last tool round, not the bubble, bounds the
 * response that an interim/final frame is allowed to replace. */
export function currentResponseParts(parts: ChatMessagePart[]): ChatMessagePart[] {
  return parts.slice(parts.findLastIndex(part => part.type === 'tool-call') + 1)
}

export function mergeCurrentResponseText(parts: ChatMessagePart[], text: string, timestamp: number): ChatMessagePart[] {
  const boundary = parts.findLastIndex(part => part.type === 'tool-call') + 1

  return [...parts.slice(0, boundary), ...mergeFinalAssistantText(parts.slice(boundary), text, timestamp)]
}

/** The `end_turn` close chip (a bridge may prefix the tool name, e.g. `mcp__hermes__end_turn`). */
const isCloseChip = (part: ChatMessagePart): boolean =>
  part.type === 'tool-call' && String((part as { toolName?: unknown }).toolName ?? '').endsWith('end_turn')

/**
 * Replace the reply's streamed draft with its authoritative final text.
 *
 * `mergeFinalAssistantText` keeps every text part before the last tool call as confirmed
 * narration, which is right for a normal completion but wrong here: the turn-close contract
 * writes the `end_turn` summary over the whole final message's draft, and the draft normally
 * restates that summary.
 *
 * The close round can paint text on BOTH sides of the `end_turn` chip — prose streamed before
 * the call (the prose the harness rejected as a text-only ending is the usual source), and
 * trailing prose the model adds once the close tool returns. Replacing only the last run left
 * the other one standing, so the report read twice with different wording (recorded: rejected
 * prose above the `End Turn` chip, delivered summary below it). So every text part after the
 * last NON-close tool call is the draft: all of it is dropped and the summary takes the place
 * of the first dropped part (or lands at the end when the close round streamed no text).
 * Tools, close chips, reasoning and all narration from earlier rounds keep their places.
 */
export function replaceDraftResponseText(
  parts: ChatMessagePart[],
  text: string,
  timestamp: number
): ChatMessagePart[] {
  const boundary = parts.findLastIndex(part => part.type === 'tool-call' && !isCloseChip(part)) + 1
  const first = parts.findIndex((part, index) => index >= boundary && part.type === 'text')

  if (first < 0) {
    return [...parts, assistantTextPart(text, timestamp)]
  }

  const draft = parts[first] as { completedAt?: number; timestamp?: number }
  const finalPart = assistantTextPart(text, draft.timestamp ?? timestamp)

  if (draft.completedAt !== undefined) {
    finalPart.completedAt = draft.completedAt
  }

  return [
    ...parts.slice(0, first),
    finalPart,
    ...parts.slice(first + 1).filter(part => part.type !== 'text')
  ]
}
