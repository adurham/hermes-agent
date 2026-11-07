import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'
import type { CronJob, SessionInfo } from '@/types/hermes'

/**
 * Sessions visible in one sidebar profile scope.
 *
 * ALL (`__all__`) returns the caller's list unchanged — including the
 * single-profile case, where Grouping → Profile persists that sentinel
 * while grouped rendering stays off. Never filter against `__all__`.
 */
export function filterSessionsByProfileScope(sessions: SessionInfo[], profileScope: string): SessionInfo[] {
  if (profileScope === ALL_PROFILES) {
    return sessions
  }

  const scope = normalizeProfileKey(profileScope)

  return sessions.filter(session => normalizeProfileKey(session.profile) === scope)
}

/**
 * Cron jobs visible in one sidebar profile scope — the jobs counterpart of
 * {@link filterSessionsByProfileScope}.
 *
 * `$cronJobs` is one atom fed by whatever scope fetched last, so a snapshot
 * taken under "All profiles" (or under the gateway the user just left) sits in
 * it until the chosen scope's own refresh lands — and, when that refresh is
 * skipped or fails, until the next poll. Sessions are already filtered against
 * the scope at render time; the jobs list was not, so it showed other
 * profiles' jobs under a pill that named the chosen gateway.
 *
 * The backend stamps `profile` on every job, including the unified
 * `?profile=all` list (`hermes_cli/web_server_cron.py::_annotate_cron_job`).
 * A job with no stamp comes from a backend that predates it — keep it rather
 * than emptying the section against an older gateway.
 */
export function filterCronJobsByProfileScope(jobs: CronJob[], profileScope: string): CronJob[] {
  if (profileScope === ALL_PROFILES) {
    return jobs
  }

  const scope = normalizeProfileKey(profileScope)

  return jobs.filter(job => !job.profile || normalizeProfileKey(job.profile) === scope)
}
