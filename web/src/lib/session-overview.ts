/**
 * Selection for the Sessions page "Recent Sessions" overview card.
 *
 * The card used to be built with ``overviewSessions.filter((s) => !s.is_active)``,
 * which hid every *currently running* session — the one thing a "what is my
 * agent doing right now" panel most needs to show. A session is ``is_active``
 * when its row has no ``ended_at`` and it was active within the last 5
 * minutes, so an attached terminal CLI (``hermes`` in tmux over SSH), a live
 * desktop chat, or an in-flight cron run were all systematically invisible
 * on the Overview tab while they were live, and only appeared once they had
 * gone idle for 5 minutes. Users reasonably read that as "my session is
 * missing from the dashboard".
 *
 * Live sessions are now surfaced *first* (that is the interesting state),
 * with recently finished ones filling the remainder of the card. Relative
 * order inside each group is preserved, so the server's ordering (newest
 * activity first) still decides ties.
 */

export interface OverviewSessionLike {
  id: string;
  is_active?: boolean;
}

/**
 * Pick the sessions shown in the overview card: live ones first, then the
 * rest, capped at ``limit``.
 */
export function selectOverviewSessions<T extends OverviewSessionLike>(
  sessions: readonly T[],
  limit = 5,
): T[] {
  const live: T[] = [];
  const idle: T[] = [];
  for (const session of sessions) {
    (session.is_active ? live : idle).push(session);
  }
  return [...live, ...idle].slice(0, Math.max(0, limit));
}
