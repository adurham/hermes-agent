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

/**
 * Replace the reply's streamed draft with its authoritative final text.
 *
 * `mergeFinalAssistantText` keeps every text part before the last tool call as confirmed
 * narration, which is right for a normal completion but wrong here: the turn-close contract
 * writes the `end_turn` summary over the whole final message's draft, and the draft normally
 * restates that summary (the recorded turn showed the report twice — streamed draft above the
 * `End Turn` chip, delivered summary below it). So the draft — the LAST contiguous run of text
 * parts, which may sit before the closing tool chip — is dropped, while tools, reasoning and
 * any earlier sealed narration keep their places.
 */
export function replaceDraftResponseText(
  parts: ChatMessagePart[],
  text: string,
  timestamp: number
): ChatMessagePart[] {
  const lastText = parts.findLastIndex(part => part.type === 'text')

  if (lastText < 0) {
    return [...parts, assistantTextPart(text, timestamp)]
  }

  let start = lastText

  while (start > 0 && parts[start - 1].type === 'text') {
    start -= 1
  }

  const draft = parts[start] as { completedAt?: number; timestamp?: number } | undefined
  const finalPart = assistantTextPart(text, draft?.timestamp ?? timestamp)

  if (draft?.completedAt !== undefined) {
    finalPart.completedAt = draft.completedAt
  }

  return [...parts.slice(0, start), finalPart, ...parts.slice(lastText + 1)]
}
