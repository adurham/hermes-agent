import { describe, expect, it } from 'vitest'

import { ALL_PROFILES } from '@/store/profile'
import type { CronJob, SessionInfo } from '@/types/hermes'

import { filterCronJobsByProfileScope, filterSessionsByProfileScope } from './profile-scope'

/** Build the smallest session row needed by the profile-scope tests. */
const row = (id: string, profile?: string): SessionInfo =>
  ({ id, message_count: 1, profile, source: 'signal', started_at: 0, title: id }) as SessionInfo

describe('filterSessionsByProfileScope', () => {
  it('keeps only rows from the selected profile', () => {
    const rows = [row('default-row', 'default'), row('work-row', 'work')]

    expect(filterSessionsByProfileScope(rows, 'work').map(session => session.id)).toEqual(['work-row'])
  })

  it('treats legacy rows without a profile as default', () => {
    const rows = [row('legacy-row'), row('work-row', 'work')]

    expect(filterSessionsByProfileScope(rows, 'default').map(session => session.id)).toEqual(['legacy-row'])
  })

  it('preserves every row in the canonical all-profiles scope', () => {
    const rows = [row('default-row', 'default'), row('work-row', 'work')]

    expect(filterSessionsByProfileScope(rows, ALL_PROFILES)).toBe(rows)
  })

  it('does not empty ALL scope when every row is one profile', () => {
    // Grouping → Profile persists ALL even with one profile. Filtering
    // against the `__all__` sentinel would empty recents and pins.
    const rows = [row('a', 'default'), row('b', 'default'), row('c', 'default')]

    expect(filterSessionsByProfileScope(rows, ALL_PROFILES)).toBe(rows)
  })
})

describe('filterCronJobsByProfileScope', () => {
  // The jobs counterpart of the session filter: `$cronJobs` holds whichever
  // scope's fetch landed last, so a unified snapshot must not render under a
  // concrete gateway pill while that scope's own refresh is in flight.
  const job = (id: string, profile?: string): CronJob => ({ enabled: true, id, profile })

  it('keeps only the selected profile’s jobs', () => {
    const jobs = [job('default-job', 'default'), job('work-job', 'work')]

    expect(filterCronJobsByProfileScope(jobs, 'work').map(j => j.id)).toEqual(['work-job'])
  })

  it('preserves every job in the canonical all-profiles scope', () => {
    const jobs = [job('default-job', 'default'), job('work-job', 'work')]

    expect(filterCronJobsByProfileScope(jobs, ALL_PROFILES)).toBe(jobs)
  })

  it('keeps an unstamped job so an older backend does not empty the section', () => {
    const jobs = [job('legacy'), job('work-job', 'work')]

    expect(filterCronJobsByProfileScope(jobs, 'work').map(j => j.id)).toEqual(['legacy', 'work-job'])
  })
})
