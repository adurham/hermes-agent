import type { useSensors } from '@dnd-kit/core'
import { closestCenter, DndContext, type DragEndEvent } from '@dnd-kit/core'
import { arrayMove, SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import type * as React from 'react'

// Sidebar reordering is a strictly vertical list. The dragged item's transform
// is rendered Y-only in useSortableBindings (no x, no scale); this just stops
// dnd-kit's auto-scroll from dragging the rail — or the window — sideways when
// the pointer nears an edge, killing the horizontal "drag to valhalla".
export const reorderAutoScroll = { threshold: { x: 0, y: 0.2 } }

// A single, standalone reorderable list — it owns its own DndContext, so it is
// safe to drop anywhere PROVIDED it is not itself nested inside another
// ReorderableList/DndContext (Recents, Pinned, the project overview list).
//
// dnd-kit does NOT support nesting DndContext providers: when two are
// ancestor/descendant, the OUTER one's sensors capture pointer events first,
// so the inner list's drag handles silently stop reordering (the event either
// no-ops or gets picked up by an unrelated outer gesture). For a lane that
// itself contains draggable sessions (repos -> lanes -> sessions, all
// reorderable), do NOT nest a second ReorderableList inside one of these —
// build ONE DndContext for the whole subtree instead, with sibling/nested
// SortableContexts (see `SortableGroup` below) and a single onDragEnd
// dispatcher that branches on each item's `data.type`. See
// projects/entered-content.tsx's RepoFlatSection for the reference pattern.
export function ReorderableList({
  children,
  ids,
  onReorder,
  sensors
}: {
  children: React.ReactNode
  ids: string[]
  onReorder: (ids: string[]) => void
  sensors?: ReturnType<typeof useSensors>
}) {
  const handleDragEnd = ({ activatorEvent, active, over }: DragEndEvent) => {
    // dnd-kit only restores focus for keyboard drags; after a pointer drop the
    // browser leaves :focus on the grab handle, which keeps a focus-within
    // grabber/affordance reveal stuck "on". Drop that focus so the row returns
    // to its resting state once the pointer moves away.
    if (!(activatorEvent instanceof KeyboardEvent)) {
      ;(document.activeElement as HTMLElement | null)?.blur()
    }

    if (!over || active.id === over.id) {
      return
    }

    const from = ids.indexOf(String(active.id))
    const to = ids.indexOf(String(over.id))

    if (from >= 0 && to >= 0) {
      onReorder(arrayMove(ids, from, to))
    }
  }

  return (
    <DndContext
      autoScroll={reorderAutoScroll}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
      sensors={sensors}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  )
}

// A BARE SortableContext — no DndContext of its own. Use this (never a second
// ReorderableList) to nest an independently-orderable group of items inside a
// subtree that already has an ancestor DndContext. Sibling/nested
// SortableContexts under ONE shared DndContext is dnd-kit's documented
// "multiple containers" pattern; multiple DndContexts is not.
export function SortableGroup({ children, ids }: { children: React.ReactNode; ids: string[] }) {
  return (
    <SortableContext items={ids} strategy={verticalListSortingStrategy}>
      {children}
    </SortableContext>
  )
}

export function useSortableBindings(id: string, data?: Record<string, unknown>) {
  const { attributes, isDragging, listeners, setNodeRef, transform, transition } = useSortable({ data, id })

  return {
    dragging: isDragging,
    dragHandleProps: { ...attributes, ...listeners },
    ref: setNodeRef,
    reorderable: true as const,
    style: {
      // Uniform vertical list: only ever translate on Y. Ignoring x and the
      // scaleX/scaleY that CSS.Transform.toString would emit keeps a dragged
      // group/row from drifting sideways or morphing its size mid-drag.
      transform: transform ? `translate3d(0px, ${transform.y}px, 0)` : undefined,
      transition: isDragging ? undefined : transition,
      willChange: isDragging ? 'transform' : undefined
    }
  }
}
