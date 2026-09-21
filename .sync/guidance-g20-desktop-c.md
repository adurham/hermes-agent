# Group g20-desktop-c — FORK.md guidance

Files: apps/desktop/src/components/pane-shell/tree/renderer/tree-group.tsx,
apps/desktop/src/components/pet/use-pet-roam.test.tsx,
apps/desktop/src/components/ui/pane-tab.tsx,
apps/desktop/src/store/native-notifications.test.ts, apps/desktop/src/store/projects.ts,
apps/desktop/src/store/prompts.test.ts, apps/desktop/src/store/prompts.ts,
apps/desktop/src/store/session-states.ts, apps/desktop/src/store/subagents.ts

## apps/desktop/src/components/pane-shell/tree/renderer/tree-group.tsx — dead
## DoubleTapContext call-site argument
Same defect class as groups g18/g19: a prior sync's merge left a stale call
`tabDrag?.()` with 4 arguments after upstream reduced the signature to 3 args (dropping
the `DoubleTapContext` parameter). If you see a call site here passing MORE arguments
than the current (post-merge) function signature declares, reduce the call to match —
don't just resolve the marker textually, verify arg count against the actual
callee signature in this same file or its import target.

## apps/desktop/src/store/session-states.ts
Related to `apps/desktop/src/app/session/hooks/use-session-actions/index.ts` (group
g19) — if this store also tracks liveness fields (`busy`, `awaitingResponse`,
`turnLive`, `adoptedRunningTurn`, `turnStartedAt`), apply the same rule: a terminal
transport event arriving mid-hydration must remain authoritative; don't let a
post-hydration reconcile silently overwrite live state with a stale hydration snapshot.

## No specific prior guidance found for
apps/desktop/src/components/pet/use-pet-roam.test.tsx,
apps/desktop/src/components/ui/pane-tab.tsx,
apps/desktop/src/store/native-notifications.test.ts, apps/desktop/src/store/projects.ts,
apps/desktop/src/store/prompts.test.ts, apps/desktop/src/store/prompts.ts,
apps/desktop/src/store/subagents.ts — use the common-guidance default. Flag
design-collision picks explicitly; these will need new FORK.md entries.
