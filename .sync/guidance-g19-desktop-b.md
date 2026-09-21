# Group g19-desktop-b — FORK.md guidance

Files: apps/desktop/src/app/contrib/controller.tsx, apps/desktop/src/app/contrib/wiring.tsx,
apps/desktop/src/app/pet-overlay/pet-overlay-app.tsx,
apps/desktop/src/app/session/hooks/use-session-actions.test.tsx,
apps/desktop/src/app/session/hooks/use-session-actions/index.ts,
apps/desktop/src/components/chat/status-section.tsx

## apps/desktop/src/app/contrib/controller.tsx — dead DoubleTapContext parameter
Same defect class as `session-drag.ts` in group g18: a prior sync's merge left a dead
`double: DoubleTapContext | undefined` parameter after upstream retired that type
(replaced by `stripVisible` / `strip-visibility.ts`). Drop it from signatures/call sites
if present and unresolvable. `grep -rn DoubleTapContext apps/desktop/src/` should return
zero hits repo-wide once ALL desktop groups are done — if you still see it in THIS file
after resolving, that's a real leftover, not a false positive.

## apps/desktop/src/app/session/hooks/use-session-actions/index.ts — CRITICAL, known
## regression pattern (liveness fields vs post-hydration reconcile)
A PRIOR sync's merge duplicated the fork's liveness fields (`busy`, `awaitingResponse`,
`turnLive`, `adoptedRunningTurn`, `turnStartedAt`) INTO upstream's post-hydration
reconcile block. This is WRONG: the post-hydration reconcile block must NOT touch
liveness fields, because a terminal transport event arriving mid-hydration must stay
authoritative over a stale hydration snapshot. If you see liveness fields being
set/reset inside a reconcile-after-hydration function, STRIP them back out of that
block. The fork's `inflight.started_at` real-start-time preference belongs in the
PRE-hydration liveness block, not the post-hydration reconcile block — verify it's
positioned there.

## apps/desktop/src/app/session/hooks/use-session-actions.test.tsx — known regression
## pattern (stubbing the wrong RPC surface)
A PRIOR sync saw upstream change tile-resume routing from an ambient `requestGateway`
call to a profile-scoped router (`requestForSessionProfile` / `requestGatewayForProfile`).
Fork-authored tests that stub only the AMBIENT socket silently get `undefined` back from
the resume RPC (a false-negative/false-positive test, not a crash). If this test file
stubs a resume/session-action RPC, verify it stubs whatever function the CURRENT
(post-merge) source code actually calls — grep the corresponding source file
(`use-session-actions/index.ts` in this same group) for the exact function name used at
the resume call site, and make sure the test mocks that exact name.

## No specific prior guidance found for
apps/desktop/src/app/contrib/wiring.tsx, apps/desktop/src/app/pet-overlay/
pet-overlay-app.tsx, apps/desktop/src/components/chat/status-section.tsx — use the
common-guidance default. Flag design-collision picks explicitly; these will need new
FORK.md entries.
