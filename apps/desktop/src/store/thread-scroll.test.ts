import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  $threadJumpButtonVisibleBySession,
  $threadMessagesBelowBySession,
  $threadScrolledUpBySession,
  isOwnThreadScroll,
  onScrollToBottomRequest,
  publishThreadAtBottom,
  publishThreadMessagesBelow,
  requestScrollToBottom,
  resetPublishedThreadScroll,
  resetThreadScroll,
  setThreadAtBottom,
  threadScrollStateFromMetrics
} from './thread-scroll'

afterEach(() => {
  resetThreadScroll('session-a')
  resetThreadScroll('session-b')
})

describe('publishThreadAtBottom', () => {
  it('lets the visible pane flash the jump pill when the thread leaves the bottom', () => {
    publishThreadAtBottom(false, { paneVisible: true, sessionId: 'session-a' })

    expect(Boolean($threadJumpButtonVisibleBySession.get()['session-a'])).toBe(true)
    expect(Boolean($threadScrolledUpBySession.get()['session-a'])).toBe(true)
  })

  it('lets a stalled reader un-dim the composer while the jump pill stays', () => {
    publishThreadAtBottom(false, { paneVisible: true, sessionId: 'session-a' }, false)

    expect(Boolean($threadJumpButtonVisibleBySession.get()['session-a'])).toBe(true)
    expect(Boolean($threadScrolledUpBySession.get()['session-a'])).toBe(false)
  })

  it('ignores stick-to-bottom misses from a hidden keep-alive pane', () => {
    setThreadAtBottom(true, 'session-a')

    publishThreadAtBottom(false, { paneVisible: false, sessionId: 'session-a' })

    expect(Boolean($threadJumpButtonVisibleBySession.get()['session-a'])).toBe(false)
    expect(Boolean($threadScrolledUpBySession.get()['session-a'])).toBe(false)
  })

  it("keeps the visible pane's scrolled-up chrome when a hidden pane publishes", () => {
    publishThreadAtBottom(false, { paneVisible: true, sessionId: 'session-a' })

    publishThreadAtBottom(true, { paneVisible: false, sessionId: 'session-a' })

    expect(Boolean($threadJumpButtonVisibleBySession.get()['session-a'])).toBe(true)
    expect(Boolean($threadScrolledUpBySession.get()['session-a'])).toBe(true)
  })
})

describe('resetPublishedThreadScroll', () => {
  it('resets only the unmounting session, including its message count', () => {
    for (const sessionId of ['session-a', 'session-b']) {
      publishThreadAtBottom(false, { paneVisible: true, sessionId })
      publishThreadMessagesBelow(7, { paneVisible: true, sessionId })
    }

    resetPublishedThreadScroll({ paneVisible: true, sessionId: 'session-a' })

    expect($threadJumpButtonVisibleBySession.get()['session-a']).toBeUndefined()
    expect($threadScrolledUpBySession.get()['session-a']).toBeUndefined()
    expect($threadMessagesBelowBySession.get()['session-a']).toBeUndefined()
    expect($threadJumpButtonVisibleBySession.get()['session-b']).toBe(true)
    expect($threadScrolledUpBySession.get()['session-b']).toBe(true)
    expect($threadMessagesBelowBySession.get()['session-b']).toBe(7)
  })

  it('preserves mirror references on no-op ticks, hidden publications, and missing identities', () => {
    publishThreadAtBottom(false, { paneVisible: true, sessionId: 'session-a' })
    publishThreadMessagesBelow(7, { paneVisible: true, sessionId: 'session-a' })
    const flags = $threadScrolledUpBySession.get()
    const jump = $threadJumpButtonVisibleBySession.get()
    const counts = $threadMessagesBelowBySession.get()

    publishThreadAtBottom(false, { paneVisible: true, sessionId: 'session-a' })
    publishThreadMessagesBelow(7, { paneVisible: true, sessionId: 'session-a' })
    publishThreadMessagesBelow(0, { paneVisible: false, sessionId: 'session-a' })
    resetPublishedThreadScroll({ paneVisible: false, sessionId: 'session-a' })
    publishThreadAtBottom(false, { paneVisible: true, sessionId: null })
    publishThreadMessagesBelow(12, { paneVisible: true, sessionId: null })
    resetThreadScroll(null)

    expect($threadScrolledUpBySession.get()).toBe(flags)
    expect($threadJumpButtonVisibleBySession.get()).toBe(jump)
    expect($threadMessagesBelowBySession.get()).toBe(counts)
  })

  it('clears the jump pill when the visible pane unmounts', () => {
    setThreadAtBottom(false, 'session-a')

    resetPublishedThreadScroll({ paneVisible: true, sessionId: 'session-a' })

    expect(Boolean($threadJumpButtonVisibleBySession.get()['session-a'])).toBe(false)
    expect(Boolean($threadScrolledUpBySession.get()['session-a'])).toBe(false)
  })

  it('does not clear the visible pane when a hidden list unmounts', () => {
    setThreadAtBottom(false, 'session-a')

    resetPublishedThreadScroll({ paneVisible: false, sessionId: 'session-a' })

    expect(Boolean($threadJumpButtonVisibleBySession.get()['session-a'])).toBe(true)
    expect(Boolean($threadScrolledUpBySession.get()['session-a'])).toBe(true)
  })
})

describe('requestScrollToBottom', () => {
  it('routes a scroll request only to its session', () => {
    const sessionA = vi.fn()
    const sessionB = vi.fn()
    const stopA = onScrollToBottomRequest(sessionA, 'session-a')
    const stopB = onScrollToBottomRequest(sessionB, 'session-b')

    requestScrollToBottom('session-b')

    expect(sessionA).not.toHaveBeenCalled()
    expect(sessionB).toHaveBeenCalledOnce()
    stopA()
    stopB()
  })

  it("does not let a late unmount clear a newer session's handler", () => {
    const first = vi.fn()
    const second = vi.fn()
    const stopFirst = onScrollToBottomRequest(first, 'session-a')
    const stopSecond = onScrollToBottomRequest(second, 'session-a')

    stopFirst()
    requestScrollToBottom('session-a')

    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledOnce()
    stopSecond()
  })
})

describe('isOwnThreadScroll', () => {
  const base = { heightChanged: true, loadSettled: false, ownScrollTop: 206, scrollTop: 192, targetTop: 192 }

  it('treats the layout clamping under an in-flight restore as the app\'s own scroll', () => {
    // The live trace shape: the app applied 206 against a 969px transcript, a same-frame resize
    // clamped the browser to 192 (= the new bottom of 955), and the stale ownScrollTop made the
    // exact-match test call it foreign — cancelling the restore and leaving the reader mid-flight.
    expect(isOwnThreadScroll(base)).toBe(true)
    expect(isOwnThreadScroll({ ...base, scrollTop: 192, ownScrollTop: 192, targetTop: 206 })).toBe(true)
    expect(isOwnThreadScroll({ ...base, ownScrollTop: null, scrollTop: 200, targetTop: 210 })).toBe(true)
  })

  it('still hands over to a real scroll while the restore is landing', () => {
    // Same landing, but nothing resized: this is the reader, not the layout.
    expect(isOwnThreadScroll({ ...base, heightChanged: false, scrollTop: 200 })).toBe(false)
    // Or resized, but the position is nowhere near the target.
    expect(isOwnThreadScroll({ ...base, scrollTop: 100, targetTop: 192 })).toBe(false)
  })

  it('hands over to any deviation once the restore has settled', () => {
    expect(isOwnThreadScroll({ ...base, loadSettled: true, scrollTop: 100, targetTop: 192 })).toBe(false)
  })
})

describe('threadScrollStateFromMetrics', () => {
  const metrics = (fromBottom: number) => ({ clientHeight: 762, scrollHeight: 5000, scrollTop: 5000 - 762 - fromBottom })

  it('records the follow loop\'s one-frame lag as the bottom, not a reading position', () => {
    // The lag values seen on live traces while a tool card re-renders under a reader who is following
    // the tail. Recorded as offsets, they came back as a deliberate position just outside the follow
    // zone, so the follow disarmed and the transcript walked away under the reader.
    for (const lag of [0, 1, 16.9, 24.18, 32, 41, 80.09, 96]) {
      expect(threadScrollStateFromMetrics(metrics(lag))).toEqual({ kind: 'bottom' })
    }
  })

  it('still records a position the reader actually stopped at', () => {
    for (const parked of [97, 281, 800, 1500]) {
      expect(threadScrollStateFromMetrics(metrics(parked))).toEqual({ fromBottom: parked, kind: 'offset' })
    }
  })
})
