import type * as React from 'react'

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
