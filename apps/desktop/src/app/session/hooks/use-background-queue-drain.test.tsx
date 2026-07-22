import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $queuedPromptsBySession, enqueueQueuedPrompt, getQueuedPrompts } from '@/store/composer-queue'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'

import { useBackgroundQueueDrain } from './use-background-queue-drain'
import type { SubmitTextOptions } from './use-prompt-actions/utils'

/** Builds a validated getter over a plain stored→runtime map, mirroring
 *  use-session-state-cache's getRuntimeIdForStoredSession contract: null
 *  when unmapped. */
function runtimeGetterFrom(map: Map<string, string>): (storedSessionId: string) => null | string {
  return storedSessionId => map.get(storedSessionId) ?? null
}

function Harness({
  enabled = true,
  getRuntimeIdForStoredSession,
  selectedStoredSessionId = 'stored-session-b',
  submitText
}: {
  enabled?: boolean
  getRuntimeIdForStoredSession: (storedSessionId: string) => null | string
  selectedStoredSessionId?: string | null
  submitText: (text: string, options?: SubmitTextOptions) => Promise<boolean> | boolean
}) {
  useBackgroundQueueDrain({
    enabled,
    getRuntimeIdForStoredSession,
    selectedStoredSessionId,
    submitText
  })

  return null
}

describe('useBackgroundQueueDrain', () => {
  beforeEach(() => {
    vi.useRealTimers()
    clearAllSessionStates()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
    $queuedPromptsBySession.set({})
    clearAllSessionStates()
  })

  it('drains an idle queued prompt for a non-selected background session', async () => {
    const runtimeMap = new Map([['stored-session-a', 'rt-session-a']])
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'continue in the background', attachments: [] })
    clearAllSessionStates()

    render(<Harness getRuntimeIdForStoredSession={runtimeGetterFrom(runtimeMap)} submitText={submitText} />)

    await waitFor(() => {
      expect(submitText).toHaveBeenCalledWith('continue in the background', {
        attachments: [],
        fromQueue: true,
        sessionId: 'rt-session-a',
        storedSessionId: 'stored-session-a'
      })
    })

    await waitFor(() => expect(getQueuedPrompts('stored-session-a')).toHaveLength(0))
  })

  it('leaves the selected session queue to the mounted ChatBar drainer', async () => {
    const runtimeMap = new Map([['stored-session-a', 'rt-session-a']])
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'visible queue entry', attachments: [] })
    clearAllSessionStates()

    render(
      <Harness
        getRuntimeIdForStoredSession={runtimeGetterFrom(runtimeMap)}
        selectedStoredSessionId="stored-session-a"
        submitText={submitText}
      />
    )

    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(submitText).not.toHaveBeenCalled()
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(1)
  })

  it('does not drain a background session that is still marked working', async () => {
    const runtimeMap = new Map([['stored-session-a', 'rt-session-a']])
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'wait for current turn', attachments: [] })
    // Mark the session as working (busy) so the drain should skip it
    publishSessionState('rt-session-a', { ...createClientSessionState('stored-session-a'), busy: true })

    render(<Harness getRuntimeIdForStoredSession={runtimeGetterFrom(runtimeMap)} submitText={submitText} />)

    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(submitText).not.toHaveBeenCalled()
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(1)
  })

  it('passes a null runtime id so submitText can resume stale background sessions by stored id', async () => {
    const runtimeMap = new Map<string, string>()
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'resume then send', attachments: [] })

    render(<Harness getRuntimeIdForStoredSession={runtimeGetterFrom(runtimeMap)} submitText={submitText} />)

    await waitFor(() => {
      expect(submitText).toHaveBeenCalledWith('resume then send', {
        attachments: [],
        fromQueue: true,
        sessionId: null,
        storedSessionId: 'stored-session-a'
      })
    })
  })

  // Regression: a pooled/idle-reaped profile backend re-mints runtime ids
  // (pruneSecondaryGateways), so a stale stored→runtime mapping can point at a
  // DIFFERENT, currently-live session's runtime id. Draining with that stale
  // id would dispatch the queued prompt as a live turn against the wrong
  // session ("queued in A, landed in B"). The validated getter must reject a
  // recycled mapping and hand back null so submitText falls back to a
  // stored-id resume instead of misrouting into the live session that now
  // owns that runtime id.
  it('passes null (not a recycled runtime id) when the stored→runtime mapping is cross-wired', async () => {
    // getRuntimeIdForStoredSession simulates the validated lookup rejecting a
    // mapping whose target runtime state no longer belongs to this stored id
    // (recycled onto a different, live session).
    const getRuntimeIdForStoredSession = vi.fn((storedSessionId: string) =>
      storedSessionId === 'stored-session-a' ? null : 'rt-session-b'
    )
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'must not land in session b', attachments: [] })
    clearAllSessionStates()

    render(<Harness getRuntimeIdForStoredSession={getRuntimeIdForStoredSession} submitText={submitText} />)

    await waitFor(() => {
      expect(submitText).toHaveBeenCalledWith('must not land in session b', {
        attachments: [],
        fromQueue: true,
        sessionId: null,
        storedSessionId: 'stored-session-a'
      })
    })
  })

  it('retries a rejected background drain without waiting for another queue or busy-state change', async () => {
    vi.useFakeTimers()

    const runtimeMap = new Map([['stored-session-a', 'rt-session-a']])
    const submitText = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)

    enqueueQueuedPrompt('stored-session-a', { text: 'retry me', attachments: [] })

    render(<Harness getRuntimeIdForStoredSession={runtimeGetterFrom(runtimeMap)} submitText={submitText} />)

    await act(async () => {
      await Promise.resolve()
    })

    expect(submitText).toHaveBeenCalledTimes(1)
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(750)
      await Promise.resolve()
    })

    expect(submitText).toHaveBeenCalledTimes(2)
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(0)
  })
})
