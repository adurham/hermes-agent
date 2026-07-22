import { closestCenter, DndContext, type DragEndEvent, type useSensors } from '@dnd-kit/core'
import { arrayMove } from '@dnd-kit/sortable'
import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import type { HermesGitWorktree } from '@/global'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import {
  $dismissedWorktreeIds,
  $sidebarLaneSessionOrderIds,
  $sidebarWorkspaceOrderIds,
  dismissWorktree,
  pruneSidebarLaneSessionOrderIds,
  setSidebarLaneSessionOrderIds,
  setSidebarWorkspaceOrderIds
} from '@/store/layout'
import { notifyError } from '@/store/notifications'
import { removeWorktreePath } from '@/store/projects'

import { SidebarRowStack } from '../chrome'
import { mergeReorderedSubset, orderByIds } from '../order'
import { reorderAutoScroll, SortableGroup, useSortableBindings } from '../reorderable-list'

import { useWorkspaceNodeOpen } from './model'
import { SidebarWorkspaceGroup } from './workspace-group'
import {
  mergeRepoWorktreeGroups,
  overlayRepoLanes,
  type SidebarProjectTree,
  type SidebarSessionGroup,
  type SidebarWorkspaceTree
} from './workspace-groups'
import { WorkspaceAddButton, WorkspaceHeader } from './workspace-header'

// Draggable items inside a repo's subtree are tagged with `data` so the ONE
// shared onDragEnd dispatcher (below) can tell a lane drop from a session drop
// without needing two separate DndContexts (dnd-kit does not support nesting
// DndContext providers — the outer one's sensors capture pointer events first
// and an inner list's drag handles silently stop reordering; see
// reorderable-list.tsx's header comment).
type DragItemData = { type: 'lane' } | { laneId: string; type: 'session' }

// The entered project's body. Main-checkout sessions render directly — no
// redundant repo/branch header (the breadcrumb already names the project). Only
// linked worktrees nest, shown by branch. Multi-folder projects keep per-repo
// headers so the folders stay distinguishable.
export function EnteredProjectContent({
  project,
  renderRows,
  onNewSession,
  repoWorktrees,
  liveSessions,
  removedSessionIds,
  workingSessionIdSet,
  dndSensors
}: {
  project: SidebarProjectTree
  renderRows: (sessions: SessionInfo[], draggable?: boolean, sortData?: Record<string, unknown>) => React.ReactNode
  onNewSession?: (path: null | string) => void
  repoWorktrees?: Record<string, HermesGitWorktree[]>
  liveSessions?: SessionInfo[]
  removedSessionIds?: ReadonlySet<string>
  workingSessionIdSet?: Set<string>
  dndSensors?: ReturnType<typeof useSensors>
}) {
  if (!project.repos.length) {
    return null
  }

  const single = project.repos.length === 1

  return (
    <>
      {project.repos.map(repo => (
        <RepoFlatSection
          discoveredWorktrees={repo.path ? repoWorktrees?.[repo.path] : undefined}
          dndSensors={dndSensors}
          key={repo.id}
          liveSessions={liveSessions}
          onNewSession={onNewSession}
          removedSessionIds={removedSessionIds}
          renderRows={renderRows}
          repo={repo}
          showHeader={!single}
          workingSessionIdSet={workingSessionIdSet}
        />
      ))}
    </>
  )
}

function RepoFlatSection({
  repo,
  showHeader,
  renderRows,
  onNewSession,
  discoveredWorktrees,
  liveSessions,
  removedSessionIds,
  workingSessionIdSet,
  dndSensors
}: {
  repo: SidebarWorkspaceTree
  showHeader: boolean
  renderRows: (sessions: SessionInfo[], draggable?: boolean, sortData?: Record<string, unknown>) => React.ReactNode
  onNewSession?: (path: null | string) => void
  discoveredWorktrees?: HermesGitWorktree[]
  liveSessions?: SessionInfo[]
  removedSessionIds?: ReadonlySet<string>
  workingSessionIdSet?: Set<string>
  dndSensors?: ReturnType<typeof useSensors>
}) {
  const { t } = useI18n()
  const s = t.sidebar
  const [open, toggleOpen] = useWorkspaceNodeOpen(repo.id)
  const dismissedWorktrees = useStore($dismissedWorktreeIds)
  // The lane drag-order is a SHARED atom across every repo in the sidebar (it
  // mirrors $sidebarWorkspaceParentOrderIds' shape) — scope reads/writes to
  // this repo's own lane ids via mergeReorderedSubset so reordering repo A's
  // lanes never clobbers repo B's saved order.
  const workspaceOrderIds = useStore($sidebarWorkspaceOrderIds)
  const laneSessionOrders = useStore($sidebarLaneSessionOrderIds)

  // The repo's session lanes already come fully built from the backend; this
  // only injects empty VISUAL lanes from a live `git worktree list`.
  const mergedGroups = useMemo(() => mergeRepoWorktreeGroups(repo, discoveredWorktrees), [repo, discoveredWorktrees])

  // Optimistic placement runs against the MERGED lane set (backend + visual
  // git-worktree lanes) so out-of-tree/sibling worktrees — which exist as visual
  // lanes before the snapshot carries their sessions — get the new row. The
  // overlay drops lanes it empties, so re-merge to restore still-real worktrees.
  const overlaidGroups = useMemo(() => {
    if (!(liveSessions?.length || removedSessionIds?.size)) {
      return mergedGroups
    }

    const { groups } = overlayRepoLanes({ ...repo, groups: mergedGroups }, liveSessions ?? [], removedSessionIds)

    return mergeRepoWorktreeGroups({ id: repo.id, path: repo.path, groups }, discoveredWorktrees)
  }, [repo, mergedGroups, discoveredWorktrees, liveSessions, removedSessionIds])

  const discoveredWorktreePaths = useMemo(
    () =>
      new Set(
        (discoveredWorktrees ?? [])
          .map(worktree => worktree.path?.trim())
          .filter((path): path is string => Boolean(path))
      ),
    [discoveredWorktrees]
  )

  // Main lanes are always visible; linked worktrees can be user-dismissed.
  // A live `git worktree list` hit wins over an old dismissal: if git says the
  // worktree exists again (or still exists after "hide from sidebar"), surface it.
  const defaultOrdered = overlaidGroups.filter(
    group =>
      group.isMain || !dismissedWorktrees.includes(group.id) || (group.path && discoveredWorktreePaths.has(group.path))
  )

  // Apply THIS repo's manual lane drag-order over the default (recency) order.
  // orderByIds no-ops when the repo has no saved order, or when the persisted
  // ids don't intersect this repo's lanes (another repo's saved order).
  const laneIdSet = useMemo(() => new Set(defaultOrdered.map(group => group.id)), [defaultOrdered])

  const repoLaneOrder = useMemo(
    () => workspaceOrderIds.filter(id => laneIdSet.has(id)),
    [workspaceOrderIds, laneIdSet]
  )

  const ordered = repoLaneOrder.length
    ? orderByIds(defaultOrdered, group => group.id, repoLaneOrder)
    : defaultOrdered

  // Drop stale per-lane session orders once the live lane set is known —
  // repo deletion / worktree removal / branch rename shouldn't leave orphaned
  // entries growing the persisted map forever.
  useEffect(() => {
    pruneSidebarLaneSessionOrderIds(laneIdSet)
  }, [laneIdSet])

  const repoCount = ordered.reduce((sum, group) => sum + group.sessions.length, 0)

  // Removal asks how: actually `git worktree remove` it, or just hide the lane
  // and leave the worktree on disk. A dirty worktree escalates to a force prompt
  // instead of erroring (those changes are usually throwaway).
  const [removeTarget, setRemoveTarget] = useState<null | SidebarSessionGroup>(null)
  const [forceTarget, setForceTarget] = useState<null | SidebarSessionGroup>(null)

  const removeViaGit = async (group: SidebarSessionGroup, force = false) => {
    if (!repo.path || !group.path) {
      return
    }

    try {
      await removeWorktreePath(repo.path, group.path, { force })
      dismissWorktree(group.id)
    } catch (err) {
      // git refuses a non-force remove on a dirty/locked worktree — offer force
      // rather than dead-ending on an error toast.
      if (!force && /force|modified|untracked|dirty|locked|contains/i.test(String((err as Error)?.message ?? ''))) {
        setForceTarget(group)
      } else {
        notifyError(err, s.projects.removeWorktreeFailed)
      }
    }
  }

  // Lanes drag-to-reorder among their siblings (main/branches/worktrees) once
  // there's more than one to order — a lone lane has nothing to reorder against.
  const lanesSortable = ordered.length > 1
  const laneIds = useMemo(() => ordered.map(group => group.id), [ordered])

  // A lane's EFFECTIVE session order (manual drag-order applied over the
  // backend's default) — the same computation SidebarWorkspaceGroup does
  // internally, but needed here too so the dispatcher below can resolve
  // from/to indices without reading each lane's private render state.
  const effectiveLaneSessionIds = (lane: SidebarSessionGroup): string[] => {
    const manual = laneSessionOrders[lane.id]

    return manual?.length
      ? orderByIds(lane.sessions, session => session.id, manual).map(session => session.id)
      : lane.sessions.map(session => session.id)
  }

  // ONE DndContext for the whole repo subtree — lanes AND every lane's
  // sessions all drag under it (sibling/nested SortableContexts, dnd-kit's
  // documented "multiple containers" pattern). A single dispatcher branches on
  // each dragged item's tagged `data.type` to decide which array to reorder.
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

    const activeData = active.data.current as DragItemData | undefined
    const overData = over.data.current as DragItemData | undefined

    if (activeData?.type === 'lane' && overData?.type === 'lane') {
      const from = laneIds.indexOf(String(active.id))
      const to = laneIds.indexOf(String(over.id))

      if (from >= 0 && to >= 0) {
        const newLaneOrder = arrayMove(laneIds, from, to)

        setSidebarWorkspaceOrderIds(mergeReorderedSubset(workspaceOrderIds, [...laneIdSet], newLaneOrder))
      }

      return
    }

    // Sessions only reorder WITHIN their own lane — a session dropped onto a
    // different lane's slot (mismatched laneId) or onto a lane header
    // (mismatched type) is a no-op, never an implicit move between lanes.
    if (activeData?.type === 'session' && overData?.type === 'session' && activeData.laneId === overData.laneId) {
      const lane = ordered.find(group => group.id === activeData.laneId)

      if (!lane) {
        return
      }

      const sessionIds = effectiveLaneSessionIds(lane)
      const from = sessionIds.indexOf(String(active.id))
      const to = sessionIds.indexOf(String(over.id))

      if (from >= 0 && to >= 0) {
        setSidebarLaneSessionOrderIds(lane.id, arrayMove(sessionIds, from, to))
      }
    }
  }

  const renderGroup = (group: SidebarSessionGroup, sortableLane: boolean) => {
    const groupProps = {
      group,
      laneSessionOrder: laneSessionOrders[group.id],
      // The kanban bucket is read-only: it aggregates many task worktrees, so
      // "new session here" and "remove worktree" have no single target.
      onNewSession: group.isKanban ? undefined : onNewSession,
      onRemove: group.isMain || group.isKanban ? undefined : () => setRemoveTarget(group),
      renderRows,
      sessionsReorderable: true,
      workingSessionIdSet
    }

    return sortableLane ? (
      <SortableWorkspaceGroup key={group.id} {...groupProps} />
    ) : (
      <SidebarWorkspaceGroup key={group.id} {...groupProps} />
    )
  }

  const laneRows = ordered.map(group => renderGroup(group, lanesSortable))

  const body = ordered.length ? (
    <DndContext
      autoScroll={reorderAutoScroll}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
      sensors={dndSensors}
    >
      <SortableGroup ids={laneIds}>{laneRows}</SortableGroup>
    </DndContext>
  ) : (
    <>{laneRows}</>
  )

  // Both removal prompts share the shape (hide-from-sidebar + cancel + a
  // destructive action); only the copy and the destructive handler differ.
  const worktreeDialog = (
    target: null | SidebarSessionGroup,
    setTarget: (next: null | SidebarSessionGroup) => void,
    description: string,
    destructiveLabel: string,
    onDestructive: (group: SidebarSessionGroup) => void
  ) => (
    <Dialog onOpenChange={isOpen => !isOpen && setTarget(null)} open={Boolean(target)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`${s.projects.removeWorktree} "${target?.label ?? ''}"?`}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button onClick={() => setTarget(null)} variant="ghost">
            {t.common.cancel}
          </Button>
          <Button
            onClick={() => {
              if (target) {
                dismissWorktree(target.id)
              }

              setTarget(null)
            }}
            variant="secondary"
          >
            {s.projects.removeFromSidebar}
          </Button>
          <Button
            onClick={() => {
              setTarget(null)

              if (target) {
                onDestructive(target)
              }
            }}
            variant="destructive"
          >
            {destructiveLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )

  const removeDialog = (
    <>
      {worktreeDialog(
        removeTarget,
        setRemoveTarget,
        s.projects.removeWorktreeConfirm,
        s.projects.removeWorktree,
        group => void removeViaGit(group)
      )}
      {worktreeDialog(
        forceTarget,
        setForceTarget,
        s.projects.removeWorktreeDirty,
        s.projects.forceRemove,
        group => void removeViaGit(group, true)
      )}
    </>
  )

  if (!showHeader) {
    return (
      <>
        {body}
        {removeDialog}
      </>
    )
  }

  return (
    <SidebarRowStack>
      <WorkspaceHeader
        action={
          onNewSession && (
            <WorkspaceAddButton label={s.newSessionIn(repo.label)} onClick={() => onNewSession(repo.path)} />
          )
        }
        count={repoCount}
        emphasis
        icon={<Codicon className="shrink-0 text-(--ui-text-tertiary)" name="repo" size="0.75rem" />}
        label={repo.label}
        onToggle={toggleOpen}
        open={open}
        title={repo.path ?? undefined}
      />
      {open && <SidebarRowStack className="pl-2.5">{body}</SidebarRowStack>}
      {removeDialog}
    </SidebarRowStack>
  )
}

function SortableWorkspaceGroup(props: React.ComponentProps<typeof SidebarWorkspaceGroup>) {
  return <SidebarWorkspaceGroup {...props} {...useSortableBindings(props.group.id, { type: 'lane' })} />
}
