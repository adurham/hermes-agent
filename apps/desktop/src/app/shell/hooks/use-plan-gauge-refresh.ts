import { useEffect } from 'react'

import type { PlanGauge } from '@/types/hermes'

interface PlanGaugeRefreshOptions {
  /** Where an observation lands. The caller owns the store rule: a build that
   *  keeps one allowance per account slot records it through `notePlanGauge`;
   *  one that paints the focused session's gauge merges it into that usage.
   *  Must be STABLE (`useCallback`): it is an effect dependency, so a new arrow
   *  every render would restart the interval and read on every render. */
  applyGauge: (gauge: PlanGauge) => void
  busy: boolean
  enabled: boolean
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  sessionId: null | string
}

/** How often a visible, idle window re-reads the account's allowance.
 *
 *  The status bar used to move only on a `session.usage` tick (mid-turn) or a
 *  `message.complete`, so an open tab froze on the last number it was pushed:
 *  the account's window kept accruing elsewhere (claude.ai, another machine,
 *  another chat) and the gauge sat minutes behind until the view was refreshed.
 *  `session.stats` is the on-demand read of the same dict — one cache lookup on
 *  the backend, which itself asks for a background refresh when its snapshot is
 *  stale — so a minute is cheap. */
export const PLAN_GAUGE_REFRESH_MS = 60_000

const isViewed = () => typeof document === 'undefined' || document.visibilityState === 'visible'

/** Re-read the focused session's plan allowance while the app is on screen.
 *
 *  The allowance is an ACCOUNT fact, but it reaches the renderer per session,
 *  and every producer of it is turn-driven (`session.usage` ticks mid-turn,
 *  `message.complete`, the session hydrate on a switch). Nothing re-reads it on
 *  an idle tab, so the number only moved when the view was refreshed. Worse, the
 *  backend cannot heal that either: `_account_window_gauge` asks for a background
 *  refresh when its snapshot is stale and still answers with the stale one, so
 *  even a tick that discovers the staleness paints the old number.
 *
 *  This hook closes the gap for the surface that is on screen:
 *
 *  - reads once as soon as the focused session is idle and the gauge is shown,
 *  - re-reads on a slow interval while the window is visible (a hidden window —
 *    sleep, another space — is not worth the RPC; it catches up on return),
 *  - re-reads the moment the window comes back, and when a turn ends.
 *
 *  Mid-turn ticks already carry the gauge, so a running turn needs no extra read.
 *  A failed read is dropped: the next interval (or the next turn) is the retry. */
export function usePlanGaugeRefresh({
  applyGauge,
  busy,
  enabled,
  requestGateway,
  sessionId
}: PlanGaugeRefreshOptions): void {
  useEffect(() => {
    if (!enabled || !sessionId || busy) {
      return
    }

    let cancelled = false

    const read = () => {
      // A hidden window keeps its interval armed but skips the work; the return
      // handler below reads at once, so catching up never waits for a tick.
      if (cancelled || !isViewed()) {
        return
      }

      void requestGateway<{ plan_gauge?: PlanGauge | null }>('session.stats', { session_id: sessionId })
        .then(stats => {
          const gauge = stats?.plan_gauge

          if (cancelled || !gauge) {
            return
          }

          applyGauge(gauge)
        })
        .catch(() => {
          // An idle read that fails changes nothing on screen: the last honest
          // number stays up, and the next interval tries again.
        })
    }

    read()

    const timer = setInterval(read, PLAN_GAUGE_REFRESH_MS)

    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        read()
      }
    }

    document.addEventListener('visibilitychange', onVisible)

    return () => {
      cancelled = true
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [applyGauge, busy, enabled, requestGateway, sessionId])
}
