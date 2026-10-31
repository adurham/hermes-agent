import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { group, split } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $sessionTiles } from '@/store/session-states'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'
import { MAX_STREAM_FLUSH_GAP_MS, STREAM_DELTA_FLUSH_MS } from './utils'

// A primary chat in `workspace` plus one session tile, because that is the
// shape the measurement came from: several streaming sessions, only some of
// whose transcripts are on screen.
const PRIMARY = 'runtime-primary'
const TILE_RUNTIME = 'runtime-tile'
const TILE_STORED = 'stored-tile'
const TILE_PANE = `session-tile:${TILE_STORED}`

type UpdateSessionState = (
  sessionId: string,
  updater: (state: ClientSessionState) => ClientSessionState,
  storedSessionId?: string | null
) => ClientSessionState

let stream: MessageStreamHarness
let states: Map<string, ClientSessionState>
let updateSessionState: ReturnType<typeof vi.fn<UpdateSessionState>>
// performance.now is a manual clock: the flush floor arithmetic reads it while
// the timers are fake, so every step advances both.
let now = 0

const tick = async (ms: number) => {
  now += ms

  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** One zone: the primary chat and the tile stacked as tabs, `active` in front. */
const stackedZone = (active: string) =>
  group(['workspace', TILE_PANE], {
    active,
    id: 'grp-main'
  })

/** Two zones side by side — both chats on screen at once. */
const splitZones = () =>
  split(
    'row',
    [
      group(['workspace'], { active: 'workspace', id: 'grp-main' }),
      group([TILE_PANE], { active: TILE_PANE, id: 'grp-tile' })
    ],
    [1, 1],
    'split-main'
  )

describe('useMessageStream flush floor by pane visibility', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    now = 10_000
    states = new Map()
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1)
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)

    $sessionTiles.set([{ runtimeId: TILE_RUNTIME, storedSessionId: TILE_STORED }])
    $layoutTree.set(stackedZone('workspace'))
    updateSessionState = vi.fn<UpdateSessionState>((sessionId, updater) => {
      const next = updater(states.get(sessionId) ?? createClientSessionState())
      states.set(sessionId, next)

      return next
    })
  })

  afterEach(() => {
    cleanup()
    $sessionTiles.set([])
    $layoutTree.set(null)
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('keeps the visible chat on the 33ms cadence', async () => {
    stream = renderMessageStream(PRIMARY, { states, updateSessionState })

    act(() => stream.appendDelta(PRIMARY, 'a'))
    await tick(0)

    expect(stream.text(PRIMARY)).toBe('a')

    act(() => stream.appendDelta(PRIMARY, 'b'))
    await tick(STREAM_DELTA_FLUSH_MS - 1)

    expect(stream.text(PRIMARY)).toBe('a')

    await tick(1)

    expect(stream.text(PRIMARY)).toBe('ab')
  })

  it('parks a tile that is behind another tab at the hidden floor, while the visible chat keeps its cadence', async () => {
    stream = renderMessageStream(PRIMARY, { states, updateSessionState })

    // First paint of a fresh queue is immediate; the cadence after it is what
    // the off-screen floor governs.
    act(() => stream.appendDelta(TILE_RUNTIME, 'x'))
    await tick(0)

    expect(stream.text(TILE_RUNTIME)).toBe('x')

    act(() => stream.appendDelta(TILE_RUNTIME, 'y'))
    // Two visible-cadence frames pass with the primary also streaming.
    act(() => stream.appendDelta(PRIMARY, 'p'))
    await tick(STREAM_DELTA_FLUSH_MS)

    expect(stream.text(PRIMARY)).toBe('p')
    expect(stream.text(TILE_RUNTIME)).toBe('x')

    act(() => stream.appendDelta(PRIMARY, 'q'))
    await tick(MAX_STREAM_FLUSH_GAP_MS)

    // The parked tile catches up with EVERYTHING it buffered, in order.
    expect(stream.text(TILE_RUNTIME)).toBe('xy')
  })

  it('flushes a revealed tile immediately instead of waiting out its budget', async () => {
    stream = renderMessageStream(PRIMARY, { states, updateSessionState })

    act(() => stream.appendDelta(TILE_RUNTIME, 'x'))
    await tick(0)

    act(() => stream.appendDelta(TILE_RUNTIME, 'y'))
    expect(stream.text(TILE_RUNTIME)).toBe('x')

    // The user clicks the tile's tab: it is now the zone's active pane.
    act(() => $layoutTree.set(stackedZone(TILE_PANE)))

    expect(stream.text(TILE_RUNTIME)).toBe('xy')
  })

  it('leaves a side-by-side tile at the visible cadence', async () => {
    $layoutTree.set(splitZones())
    stream = renderMessageStream(PRIMARY, { states, updateSessionState })

    act(() => stream.appendDelta(TILE_RUNTIME, 'x'))
    await tick(0)

    act(() => stream.appendDelta(TILE_RUNTIME, 'y'))
    await tick(STREAM_DELTA_FLUSH_MS - 1)

    expect(stream.text(TILE_RUNTIME)).toBe('x')

    await tick(1)

    expect(stream.text(TILE_RUNTIME)).toBe('xy')
  })

  it('flushes every parked session when the window regains focus', async () => {
    stream = renderMessageStream(PRIMARY, { states, updateSessionState })

    act(() => stream.appendDelta(TILE_RUNTIME, 'x'))
    await tick(0)

    act(() => stream.appendDelta(TILE_RUNTIME, 'y'))
    expect(stream.text(TILE_RUNTIME)).toBe('x')

    act(() => window.dispatchEvent(new Event('focus')))

    expect(stream.text(TILE_RUNTIME)).toBe('xy')
  })

  it('flushes a session with no pane in the layout tree (unknown) at the visible cadence', async () => {
    // A session streaming only into its cache — a secondary window, a plain
    // route, a closed tile's runtime: no pane claims it, so it must not be
    // throttled on a guess.
    $sessionTiles.set([])
    stream = renderMessageStream(PRIMARY, { states, updateSessionState })

    act(() => stream.appendDelta('runtime-detached', 'x'))
    await tick(0)

    act(() => stream.appendDelta('runtime-detached', 'y'))
    await tick(STREAM_DELTA_FLUSH_MS)

    expect(stream.text('runtime-detached')).toBe('xy')
  })
})
