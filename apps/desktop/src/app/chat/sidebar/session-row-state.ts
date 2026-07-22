import type * as React from 'react'

export type SessionDotState = 'background' | 'idle' | 'needs-input' | 'stalled' | 'unread' | 'working'

interface SessionRowState {
  hasBackground: boolean
  isStalled: boolean
  isUnread: boolean
  isWorking: boolean
  needsInput: boolean
}

/** Resolve the sidebar dot's mutually-exclusive display state by priority. */
export function sessionDotState({
  hasBackground,
  isStalled,
  isUnread,
  isWorking,
  needsInput
}: SessionRowState): SessionDotState {
  if (needsInput) {
    return 'needs-input'
  }

  if (isWorking) {
    return isStalled ? 'stalled' : 'working'
  }

  if (hasBackground) {
    return 'background'
  }

  return isUnread ? 'unread' : 'idle'
}

/** A quiet turn is still authoritatively running. Keep the unmistakable row
 * arc until the gateway reports completion; only a blocking prompt suppresses
 * it in favour of the needs-input treatment. */
export function sessionShowsRunningArc({
  isWorking,
  needsInput
}: Pick<SessionRowState, 'isWorking' | 'needsInput'>): boolean {
  return isWorking && !needsInput
}

/**
 * Split dnd-kit's combined dragHandleProps ({...attributes, ...listeners})
 * into the POINTER activator (onPointerDown) and everything else (the
 * KEYBOARD activator's onKeyDown plus attributes — role/tabIndex/aria-*).
 *
 * A reorderable session row wants dragging to start from anywhere across the
 * dot+label cluster (not just a dedicated grabber icon), but that wide area
 * is rendered `display: contents` so it doesn't disturb the row's flex/gap
 * layout — and `display: contents` strips an element from the accessibility
 * tree, so it can never be dnd-kit's KeyboardSensor activator (which needs a
 * real, focusable, `role="button"` node to Tab onto). Pointer events bubble
 * through a display:contents wrapper fine, so only the pointer half moves to
 * the wide wrapper; the keyboard half stays on the small, genuinely focusable
 * grab handle so Tab + Space + Arrow reordering keeps working.
 */
export function splitDragHandleProps(dragHandleProps: undefined | Record<string, unknown>): {
  keyboardProps: Record<string, unknown>
  pointerDown: ((event: React.PointerEvent) => void) | undefined
} {
  const { onPointerDown, ...keyboardProps } = dragHandleProps ?? {}

  return { keyboardProps, pointerDown: onPointerDown as ((event: React.PointerEvent) => void) | undefined }
}
