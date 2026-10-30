import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, render, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneLifecycleContext, PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import { rescopeConnectionScopedStores } from '@/lib/connection-scoped'
import { setActiveProfile } from '@/store/profile'
import { getThreadScrollPosition } from '@/store/thread-scroll'

import { stubThreadEnvironment, stubThreadViewportSize } from '../test-utils'

import { TranscriptWindowProvider } from './transcript-window'

import { Thread } from '.'

stubThreadEnvironment()
stubThreadViewportSize()

const SCROLL_H = 5000
const CLIENT_H = 600
const VIEWPORT_SLOT = 'aui_thread-viewport'

let scrollHeightValue = SCROLL_H

Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
  configurable: true,
  get() {
    return scrollHeightValue
  }
})
Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
  configurable: true,
  get() {
    return CLIENT_H
  }
})

beforeEach(() => {
  scrollHeightValue = SCROLL_H
  window.localStorage.clear()
  setActiveProfile('default')
  rescopeConnectionScopedStores(null)
})

const createdAt = new Date('2026-08-01T00:00:00.000Z')

function sessionMessages(key: string, turns = 4): ThreadMessage[] {
  return Array.from({ length: turns }, (_, index) => [
    {
      id: `u-${key}-${index}`,
      role: 'user',
      content: [{ type: 'text', text: `message ${index} in ${key}` }],
      attachments: [],
      createdAt,
      metadata: { custom: {} }
    } as ThreadMessage,
    {
      id: `a-${key}-${index}`,
      role: 'assistant',
      content: [{ type: 'text', text: `response ${index} in ${key}` }],
      status: { type: 'complete', reason: 'stop' },
      createdAt,
      metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
    } as ThreadMessage
  ]).flat()
}

function Pane({ sessionKey, visible }: { sessionKey: string; visible: boolean }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    isRunning: false,
    messages: sessionMessages(sessionKey),
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TranscriptWindowProvider value={{ olderAvailable: false, expandWindow: () => {} }}>
        <PaneLifecycleContext.Provider value={visible ? 'visible' : 'hot-hidden'}>
          <PaneVisibleContext.Provider value={visible}>
            <Thread scrollProfile="default" sessionId={null} sessionKey={sessionKey} />
          </PaneVisibleContext.Provider>
        </PaneLifecycleContext.Provider>
      </TranscriptWindowProvider>
    </AssistantRuntimeProvider>
  )
}

/** A tab zone exactly as the app builds one: every pane stays MOUNTED and the
 *  inactive ones are hidden with `visibility: hidden` (data-pane-hidden), which
 *  deliberately keeps their layout box so scroll positions can round-trip. */
function zone(active: string, keys: string[] = ['a', 'b']) {
  return (
    <div data-chat-surface="">
      {keys.map(key => (
        <div
          className={key === active ? '' : 'pointer-events-none invisible opacity-0'}
          data-pane-hidden={key === active ? undefined : ''}
          key={key}
        >
          <Pane sessionKey={key} visible={key === active} />
        </div>
      ))}
    </div>
  )
}

function viewports(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(`[data-slot="${VIEWPORT_SLOT}"]`)]
}

async function settleScroll(ticks = 5) {
  await act(async () => {
    for (let tick = 0; tick < ticks; tick += 1) {
      await new Promise<void>(resolve => window.setTimeout(resolve, 0))
    }
  })
}

/** What a reader does: wheel up, land at an offset, let the DOM report it. */
function scrollUpTo(vp: HTMLElement, fromBottom: number) {
  const top = scrollHeightValue - CLIENT_H - fromBottom

  act(() => {
    vp.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -160 }))
    vp.scrollTop = top
    vp.dispatchEvent(new Event('scroll'))
  })

  return top
}

describe('tab switch scroll memory', () => {
  it('records the reader’s own offset when a warm switch swaps the transcript', async () => {
    const { container, rerender } = render(<Pane sessionKey="a" visible />)

    await settleScroll()
    const vp = viewports(container)[0]!
    scrollUpTo(vp, 800)
    await settleScroll()

    rerender(<Pane sessionKey="b" visible />)
    await settleScroll()

    expect(getThreadScrollPosition('a')).toEqual({ fromBottom: 800, kind: 'offset' })
  })

  it('remembers a kept-alive tab’s offset across a round trip', async () => {
    const { container, rerender } = render(zone('a'))

    await settleScroll()
    const readingTop = scrollUpTo(viewports(container)[0]!, 800)
    await settleScroll()

    rerender(zone('b'))
    await settleScroll()

    expect(getThreadScrollPosition('a')).toEqual({ fromBottom: 800, kind: 'offset' })

    rerender(zone('a'))
    await settleScroll()

    await waitFor(() => expect(viewports(container)[0]!.scrollTop).toBe(readingTop))
  })

  it('remembers each tab’s own offset through a multi-tab bounce, and lands a never-read tab at the bottom', async () => {
    const keys = ['a', 'b', 'c', 'd', 'e']
    const { container, rerender } = render(zone('a', keys))

    await settleScroll()
    const topA = scrollUpTo(viewports(container)[0]!, 800)
    await settleScroll()

    rerender(zone('b', keys))
    await settleScroll()
    const topB = scrollUpTo(viewports(container)[1]!, 1500)
    await settleScroll()

    // c has never been read: it must sit at the bottom, never inherit b's offset.
    rerender(zone('c', keys))
    await settleScroll()
    expect(viewports(container)[2]!.scrollTop).toBe(SCROLL_H - CLIENT_H)

    rerender(zone('a', keys))
    await settleScroll()
    expect(viewports(container)[0]!.scrollTop).toBe(topA)

    rerender(zone('b', keys))
    await settleScroll()
    expect(viewports(container)[1]!.scrollTop).toBe(topB)

    expect(getThreadScrollPosition('a')).toEqual({ fromBottom: 800, kind: 'offset' })
    expect(getThreadScrollPosition('b')).toEqual({ fromBottom: 1500, kind: 'offset' })
  })

  it('keeps a tab’s offset when the reader bounces back before the restore settles', async () => {
    const keys = ['a', 'b']
    const { container, rerender } = render(zone('a', keys))

    await settleScroll()
    const topA = scrollUpTo(viewports(container)[0]!, 800)
    await settleScroll()

    rerender(zone('b', keys))
    // One tick only: b's restore is still in flight when the reader leaves again.
    await act(async () => {
      await new Promise<void>(resolve => window.setTimeout(resolve, 0))
    })
    rerender(zone('a', keys))
    await settleScroll()

    expect(viewports(container)[0]!.scrollTop).toBe(topA)
    expect(getThreadScrollPosition('a')).toEqual({ fromBottom: 800, kind: 'offset' })
  })

  it('does not strand a settled reader at the top when the transcript collapses under them', async () => {
    const previousObserver = globalThis.ResizeObserver
    const observers = new Set<{ callback: ResizeObserverCallback; targets: Set<Element> }>()

    vi.stubGlobal(
      'ResizeObserver',
      class {
        targets = new Set<Element>()
        constructor(public callback: ResizeObserverCallback) {
          observers.add(this)
        }
        observe(target: Element) {
          this.targets.add(target)
        }
        unobserve(target: Element) {
          this.targets.delete(target)
        }
        disconnect() {
          this.targets.clear()
        }
      }
    )

    const deliverContentResize = (height: number) => {
      for (const observer of observers) {
        const targets = [...observer.targets].filter(el => el.getAttribute('data-slot') === 'aui_thread-content')

        if (targets.length) {
          observer.callback(
            targets.map(target => ({ target, contentRect: { height } })) as ResizeObserverEntry[],
            observer as unknown as ResizeObserver
          )
        }
      }
    }

    const { container, unmount } = render(<Pane sessionKey="a" visible />)

    try {
      const vp = viewports(container)[0]!
      await settleScroll(10)

      scrollUpTo(vp, 800)
      await settleScroll(10)
      expect(vp.scrollTop).toBe(SCROLL_H - CLIENT_H - 800)

      // The transcript window collapses under the reader — the app prunes the
      // render budget for a tab it just left, so the mounted rows shrink far
      // below the reading offset.
      scrollHeightValue = SCROLL_H - 3000
      act(() => deliverContentResize(scrollHeightValue))
      await settleScroll(10)

      // eslint-disable-next-line no-console -- evidence for the investigation
      console.error(`[tab-scroll-memory] top after collapse = ${vp.scrollTop} of ${SCROLL_H - 3000 - CLIENT_H}`)

      expect(vp.scrollTop).toBeGreaterThan(0)
    } finally {
      scrollHeightValue = SCROLL_H
      unmount()
      vi.stubGlobal('ResizeObserver', previousObserver)
    }
  })
})
