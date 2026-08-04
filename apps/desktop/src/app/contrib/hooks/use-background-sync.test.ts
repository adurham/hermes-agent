import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import {
  $attentionSessionIds,
  $stalledSessionIds,
  $workingSessionIds,
  clearAllSessionStates,
  publishSessionState,
  SESSION_WATCHDOG_TIMEOUT_MS
} from '@/store/session-states'

import { rehydrateLiveSessionStatuses, resetLiveRuntimeTracking } from './use-background-sync'

describe('rehydrateLiveSessionStatuses', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    clearAllSessionStates()
    resetLiveRuntimeTracking()
  })

  it('restores running sessions after reconnect without opening them', () => {
    const now = 1_800_000_000_000

    rehydrateLiveSessionStatuses(
      {
        sessions: [
          {
            id: 'runtime-overnight',
            last_active: (now - SESSION_WATCHDOG_TIMEOUT_MS - 1_000) / 1000,
            session_key: 'overnight-exam-learning',
            status: 'working'
          },
          {
            id: 'runtime-cleanup',
            last_active: now / 1000,
            session_key: 'temporary-file-cleanup',
            status: 'working'
          }
        ]
      },
      now
    )

    expect($workingSessionIds.get()).toEqual(['overnight-exam-learning', 'temporary-file-cleanup'])
    expect($stalledSessionIds.get()).toEqual(['overnight-exam-learning'])
    expect($attentionSessionIds.get()).toEqual([])
  })

  it('restores a waiting turn as working and needing attention', () => {
    rehydrateLiveSessionStatuses({
      sessions: [{ id: 'runtime-needs-user', session_key: 'needs-user', status: 'waiting' }]
    })

    expect($workingSessionIds.get()).toEqual(['needs-user'])
    expect($attentionSessionIds.get()).toEqual(['needs-user'])
    expect($stalledSessionIds.get()).toEqual([])
  })

  it('ignores idle, starting, and malformed live-session rows', () => {
    rehydrateLiveSessionStatuses({
      sessions: [
        { id: 'runtime-idle', session_key: 'idle-session', status: 'idle' },
        { id: 'runtime-starting', session_key: 'starting-session', status: 'starting' },
        { id: 'runtime-malformed', status: 'working' }
      ]
    })

    expect($workingSessionIds.get()).toEqual([])
    expect($attentionSessionIds.get()).toEqual([])
    expect($stalledSessionIds.get()).toEqual([])
  })

  it('keeps a busy runtime visible through brief, isolated absences from the snapshot', () => {
    const now = 1_800_000_000_000

    publishSessionState('runtime-flaky', {
      ...createClientSessionState('flaky-session'),
      busy: true
    })

    // The snapshot has never reported this runtime live, so its absence is not
    // a completion signal — the optimistic local busy state must survive.
    rehydrateLiveSessionStatuses({ sessions: [] }, now)
    expect($workingSessionIds.get()).toEqual(['flaky-session'])

    rehydrateLiveSessionStatuses(
      { sessions: [{ id: 'runtime-flaky', session_key: 'flaky-session', status: 'working' }] },
      now + 1_500
    )
    expect($workingSessionIds.get()).toEqual(['flaky-session'])
  })

  it('settles a busy runtime the first time it vanishes after the snapshot reported it live', () => {
    const now = 1_800_000_000_000

    // Optimistically-published local busy state the snapshot has never
    // reported: empty polls must NOT settle it (optimistic-send protection).
    publishSessionState('runtime-stuck', {
      ...createClientSessionState('stuck-session'),
      busy: true,
      needsInput: false
    })

    rehydrateLiveSessionStatuses({ sessions: [] }, now)
    rehydrateLiveSessionStatuses({ sessions: [] }, now + 1_500)
    expect($workingSessionIds.get()).toEqual(['stuck-session'])

    // Once the snapshot has reported the runtime live, absence on the very
    // next poll is a completion signal — settled busy→idle immediately.
    rehydrateLiveSessionStatuses(
      { sessions: [{ id: 'runtime-stuck', session_key: 'stuck-session', status: 'working' }] },
      now + 3_000
    )
    expect($workingSessionIds.get()).toEqual(['stuck-session'])

    rehydrateLiveSessionStatuses({ sessions: [] }, now + 4_500)

    expect($workingSessionIds.get()).toEqual([])
    expect($stalledSessionIds.get()).toEqual([])
  })

  it('never touches a runtime the renderer already marks idle', () => {
    publishSessionState('runtime-idle-local', {
      ...createClientSessionState('idle-local-session'),
      busy: false
    })

    rehydrateLiveSessionStatuses({ sessions: [] }, 1_800_000_000_000)
    rehydrateLiveSessionStatuses({ sessions: [] }, 1_800_000_020_000)

    expect($workingSessionIds.get()).toEqual([])
  })
})
