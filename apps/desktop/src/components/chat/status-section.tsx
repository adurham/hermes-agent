import { useStore } from '@nanostores/react'
import { type ReactNode, type PointerEvent as ReactPointerEvent, useRef, useState } from 'react'

import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { cn } from '@/lib/utils'
import { $paneHeightOverride, setPaneHeightOverride } from '@/store/panes'

// Drag clamp for a resizable section's body: never smaller than ~2-3 rows,
// never taller than most of the viewport (the stack itself still scrolls as
// a whole past 40vh — see composer status stack's outer max-h).
const RESIZE_MIN_PX = 96
const RESIZE_MAX_VH = 0.6

interface StatusSectionProps {
  /** Optional right-aligned actions (text links / micro buttons). Pass
   *  `Button` with `size="micro"` + `variant="text"` or `"link"`. */
  accessory?: ReactNode
  children: ReactNode
  defaultCollapsed?: boolean
  /** Optional glyph between the caret and the label (e.g. a `Codicon`). */
  icon?: ReactNode
  label: ReactNode
  /** Pane-store key that turns on a drag handle below the body, letting the
   *  user resize this section's scrollable height. Persisted through the same
   *  pane-state store the shell's split panes use. Omit for sections that
   *  should just hug their content (the default). */
  resizeId?: string
}

/**
 * One collapsible group inside the composer status stack. Pure chrome — header
 * (caret + label) + body — styled to match the queue exactly so every status
 * (queue, subagents, background) reads as one piece. The stack supplies the
 * outer card and the dividers between groups; this owns only its own collapse
 * and (when `resizeId` is given) its own drag-to-resize height.
 */
export function StatusSection({
  accessory,
  children,
  defaultCollapsed = true,
  icon,
  label,
  resizeId
}: StatusSectionProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  const override = useStore($paneHeightOverride(resizeId ?? ''))
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const [dragging, setDragging] = useState(false)

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!resizeId) {
      return
    }

    event.preventDefault()
    const startY = event.clientY
    const startHeight = override ?? bodyRef.current?.getBoundingClientRect().height ?? RESIZE_MIN_PX
    const max = Math.round(window.innerHeight * RESIZE_MAX_VH)
    setDragging(true)

    const onMove = (move: globalThis.PointerEvent) => {
      setPaneHeightOverride(
        resizeId,
        Math.min(max, Math.max(RESIZE_MIN_PX, Math.round(startHeight + (move.clientY - startY))))
      )
    }

    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      setDragging(false)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp, { once: true })
  }

  return (
    <div>
      <div className="flex items-center gap-1 pr-1">
        <button
          className="flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1 text-left text-xs font-normal text-muted-foreground/92 transition-colors hover:text-foreground/90"
          onClick={() => setCollapsed(open => !open)}
          type="button"
        >
          <DisclosureCaret className="shrink-0" open={!collapsed} size="1em" />
          {icon && <span className="flex shrink-0 items-center">{icon}</span>}
          <span className="truncate">{label}</span>
        </button>
        {accessory && <div className="flex shrink-0 items-center gap-1">{accessory}</div>}
      </div>
      {!collapsed && (
        <div
          className={cn('px-1 pb-0.5', resizeId && 'overflow-y-auto')}
          ref={bodyRef}
          style={resizeId && override !== undefined ? { height: override } : undefined}
        >
          {children}
        </div>
      )}
      {!collapsed && resizeId && (
        <div
          className="group/sash relative -my-1 h-2 cursor-row-resize touch-none"
          onDoubleClick={() => setPaneHeightOverride(resizeId, undefined)}
          onPointerDown={startDrag}
        >
          <div
            className={cn(
              'absolute inset-x-0 top-1/2 h-px -translate-y-1/2 transition-colors',
              dragging ? 'bg-(--ui-stroke-secondary)' : 'group-hover/sash:bg-(--ui-stroke-secondary)'
            )}
          />
        </div>
      )}
    </div>
  )
}
