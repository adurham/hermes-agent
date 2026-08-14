import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useMemo, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { ProfileGlyph } from '@/components/ui/profile-glyph'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { displayPath } from '@/lib/display-path'
import { useStoreSelector } from '@/lib/use-session-slice'
import { setWorkspaceNodeOpen } from '@/store/layout'
import { notifyError } from '@/store/notifications'
import { newSessionInProfile, selectProfile } from '@/store/profile'
import { switchBranchInRepo } from '@/store/projects'
import { $sessionProfilesUsage } from '@/store/session'
import { $sidebarSessionRankIds } from '@/store/sidebar-sort'

import { SidebarGroupRow, SidebarRowLead, SidebarRowLink, SidebarRowStack } from '../chrome'
import { orderByIds, rankSessions } from '../order'
import { SortableGroup } from '../reorderable-list'

import { PROJECT_PREVIEW_COUNT, SIDEBAR_GROUP_PAGE, useWorkspaceNodeOpen } from './model'
import type { SidebarSessionGroup } from './workspace-groups'
import {
  WorkspaceAddButton,
  WorkspaceContextMenu,
  WorkspaceHeader,
  WorkspaceMenu,
  WorkspaceShowMoreButton
} from './workspace-header'

interface SidebarWorkspaceGroupProps {
  group: SidebarSessionGroup
  renderRows: (
    sessions: SessionInfo[],
    draggable?: boolean,
    sortData?: Record<string, unknown>,
    preserveOrder?: boolean
  ) => React.ReactNode
  onNewSession?: (path: null | string) => void
  // When set (linked worktree rows), shows a remove affordance that runs a real
  // `git worktree remove`.
  onRemove?: () => void
  // Stored session ids with a turn currently running. When the group is
  // collapsed, any overlap rolls up into a single pulsing dot on the header —
  // the per-row dot + arc-border are already hidden while collapsed (they only
  // render inside `renderRows`, which a collapsed group never calls), so this
  // is the one place that state stays visible instead of vanishing on toggle.
  workingSessionIdSet?: Set<string>
  // Drag-to-reorder THIS lane among its siblings (wired by the parent's own
  // SortableGroup via useSortableBindings — see SortableWorkspaceGroup in
  // entered-content.tsx). The actual DndContext + dispatcher live on the
  // parent (RepoFlatSection) — see that file's header comment for why.
  reorderable?: boolean
  dragging?: boolean
  dragHandleProps?: React.HTMLAttributes<HTMLElement>
  // Whether sessions inside this lane may drag-to-reorder at all. The actual
  // persisted order and the DndContext handling the drop both live on the
  // parent (RepoFlatSection); this component only renders the (bare, no
  // DndContext of its own) SortableGroup wrapper and tags each row's sortable
  // binding with {type: 'session', laneId} via renderRows' sortData param.
  sessionsReorderable?: boolean
  // A manual per-lane drag order (when set) wins over the backend's default
  // (recency) session order.
  laneSessionOrder?: string[]
  ref?: React.Ref<HTMLDivElement>
  style?: React.CSSProperties
}

export function SidebarWorkspaceGroup({
  group,
  renderRows,
  onNewSession,
  onRemove,
  workingSessionIdSet,
  reorderable = false,
  dragging = false,
  dragHandleProps,
  sessionsReorderable = false,
  laneSessionOrder,
  ref,
  style
}: SidebarWorkspaceGroupProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const isProfileGroup = group.mode === 'profile'
  // Totals for the whole profile, not the loaded page — a selector so a refresh
  // that leaves this profile's spend unchanged doesn't repaint its header.
  const usage = useStoreSelector($sessionProfilesUsage, all => all[group.id])
  const rankIds = useStore($sidebarSessionRankIds)
  // Empty worktree/branch lanes start collapsed — they only show a "No sessions
  // yet" placeholder, so defaulting them open just adds noise. Profile lanes and
  // lanes that already hold sessions default open.
  const defaultOpen = isProfileGroup || group.sessions.length > 0
  const [open, toggleOpen] = useWorkspaceNodeOpen(group.id, defaultOpen)
  const [visibleCount, setVisibleCount] = useState(SIDEBAR_GROUP_PAGE)

  // A manual per-lane drag order (when set) wins over the backend's default
  // (recency) order — same pattern as the flat Recents list, just scoped to
  // this one lane instead of the whole sidebar. Otherwise fall back to
  // whatever the active sort key ranks (`rankSessions`).
  const sessions = useMemo(
    () =>
      laneSessionOrder?.length
        ? orderByIds(group.sessions, session => session.id, laneSessionOrder)
        : rankSessions(group.sessions, rankIds),
    [group.sessions, laneSessionOrder, rankIds]
  )
  // A profile previews the same handful a project does, and clicking its label
  // is how you see the rest. Workspace groups page within what's loaded.
  const visibleSessions = sessions.slice(0, isProfileGroup ? PROJECT_PREVIEW_COUNT : visibleCount)
  const hiddenCount = isProfileGroup ? 0 : sessions.length - visibleSessions.length
  const nextCount = Math.min(SIDEBAR_GROUP_PAGE, hiddenCount)

  // Sessions only drag-to-reorder when there's more than one visible AND the
  // parent has enabled it for this view — branch/fork rows (rendered with a
  // tree stem) never participate (mirrors the flat list's own exclusion).
  const sessionsSortable = sessionsReorderable && visibleSessions.length > 1

  // Only matters while collapsed — an expanded group already shows the real
  // per-row indicator, so the aggregate would be a second, redundant cue.
  const workingWhileCollapsed =
    !open && Boolean(workingSessionIdSet?.size) && group.sessions.some(session => workingSessionIdSet!.has(session.id))

  // Leading glyph: profile color dot, a home mark for the repo's primary
  // checkout (labeled by its live branch), or a branch/kanban mark otherwise.
  const leadingIcon = group.color ? (
    <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: group.color }} />
  ) : (
    <Codicon
      className="shrink-0 text-(--ui-text-tertiary)"
      name={group.isKanban ? 'checklist' : group.isHome ? 'home' : 'git-branch'}
      size="0.75rem"
    />
  )

  const handleNewSession = async () => {
    // Reveal the lane the new session targets — an empty worktree/branch lane
    // starts collapsed, so without this the session lands in a folder the user
    // can't see. Stable across the lane's default flipping open once populated.
    setWorkspaceNodeOpen(group.id, true)

    if (isProfileGroup) {
      newSessionInProfile(group.id)

      return
    }

    if (!onNewSession) {
      return
    }

    // Main-checkout lanes are branch-labeled views over the same repo root path.
    // Clicking "+" on `main` should open on `main`, not whatever branch the root
    // currently sits on (`test0`, etc.), so explicitly switch first.
    if (group.isMain && group.path && group.label) {
      try {
        await switchBranchInRepo(group.path, group.label)
      } catch (err) {
        notifyError(err, t.statusStack.coding.switchFailed(group.label))

        return
      }
    }

    onNewSession(group.path)
  }

  const rows =
    visibleSessions.length === 0 ? (
      <div className="min-h-7 pl-2 text-[0.75rem] leading-7 text-(--ui-text-quaternary)">{s.noSessions}</div>
    ) : (
      renderRows(
        visibleSessions,
        sessionsSortable,
        sessionsSortable ? { laneId: group.id, type: 'session' } : undefined,
        Boolean(laneSessionOrder?.length)
      )
    )

  // Profile groups start a fresh session in that profile but keep the
  // all-profiles browse view; workspace groups seed the new session's cwd.
  // Main checkout lanes are branch-targeted.
  const addButton = (onNewSession || isProfileGroup) && (
    <WorkspaceAddButton label={s.newSessionIn(group.label)} onClick={() => void handleNewSession()} />
  )

  return (
    <SidebarRowStack className={dragging ? 'relative z-10 bg-(--ui-sidebar-surface-background)' : undefined} ref={ref} style={style}>
      {isProfileGroup ? (
        // A profile heads its sessions the way a project does, so it takes the
        // project row's shape rather than the tree caption the lanes below use.
        <SidebarGroupRow
          actions={addButton}
          // Clicking a profile scopes the sidebar to it, the way clicking a
          // project enters that project. Capitalized to sit level with the
          // project labels it alternates with (`Home`, and whatever the user
          // named theirs) — profile keys are stored lowercase.
          label={
            <SidebarRowLink
              aria-label={t.profiles.switchToProfile(group.label)}
              labelClassName="capitalize hover:text-foreground hover:underline"
              onClick={() => selectProfile(group.id)}
            >
              {group.label}
            </SidebarRowLink>
          }
          lead={
            <SidebarRowLead>
              {/* Fills the lead cell like a project's icon does: the glyph's own
                  16px would sit 2px proud of the 14px column. */}
              <ProfileGlyph
                className="size-full"
                color={group.color ?? null}
                isDefault={group.id === 'default'}
                name={group.label}
              />
            </SidebarRowLead>
          }
          toggle={{ ariaLabel: s.projects.toggle(group.label, !open), onToggle: toggleOpen, open }}
          totals={{ costUsd: usage?.cost_usd ?? 0, tokens: usage?.tokens ?? 0 }}
        />
      ) : (
        <WorkspaceContextMenu onRemove={onRemove} path={group.path}>
          <WorkspaceHeader
            action={
              (onNewSession || onRemove) && (
                <div className="flex items-center">
                  {addButton}
                  {onRemove && <WorkspaceMenu onRemove={onRemove} path={group.path} />}
                </div>
              )
            }
            dragAriaLabel={s.projects.reorder(group.label)}
            dragging={dragging}
            dragHandleProps={dragHandleProps}
            icon={leadingIcon}
            label={group.label}
            onToggle={toggleOpen}
            open={open}
            reorderable={reorderable}
            title={group.path ? displayPath(group.path) : undefined}
            workingWhileCollapsed={workingWhileCollapsed}
          />
        </WorkspaceContextMenu>
      )}
      {open && (
        <>
          {sessionsSortable ? (
            <SortableGroup ids={visibleSessions.map(session => session.id)}>{rows}</SortableGroup>
          ) : (
            rows
          )}
          {hiddenCount > 0 && (
            <WorkspaceShowMoreButton
              count={nextCount}
              label={group.label}
              onClick={() => setVisibleCount(count => count + SIDEBAR_GROUP_PAGE)}
            />
          )}
        </>
      )}
    </SidebarRowStack>
  )
}
