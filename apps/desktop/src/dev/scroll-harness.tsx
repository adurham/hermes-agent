/**
 * DEV-ONLY scroll harness (never imported by `index.html`, so it is not in the shipped renderer).
 *
 * Mounts the real `Thread` in the real tab topology — one visible pane per key, the others kept
 * MOUNTED and hidden exactly the way `pane-shell/tree/renderer/tree-group.tsx` hides them
 * (`visibility: hidden` + `data-pane-hidden`) — so a headless Chromium can drive wheel scrolling
 * and tab switches against real layout, the real `use-stick-to-bottom`, and the real stores.
 *
 *   npx vite --host 127.0.0.1 --port 5174        # from apps/desktop
 *   node scripts/diag-scroll-tabs.mjs            # headless driver, no Electron, no window
 *
 * Hooks the driver uses:
 *   [data-tab="a|b|c"]        switch the active tab
 *   #grow / #grow-20          append turns to the ACTIVE session (mimics streaming growth)
 *   [data-key="a|b|c"]        pane layer (hidden ones carry data-pane-hidden)
 *   [data-slot="aui_thread-viewport"]   the transcript scroller inside each pane
 *   localStorage[hermes.desktop.threadScroll.v1.profile.default]   the saved positions
 */

import '../styles.css'

import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { StrictMode, useCallback, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { Thread } from '@/components/assistant-ui/thread'
import { TranscriptWindowProvider } from '@/components/assistant-ui/thread/transcript-window'
import { PaneLifecycleContext, PaneVisibleContext } from '@/components/pane-shell/pane-visibility'

const KEYS = ['a', 'b', 'c'] as const
const CREATED_AT = new Date('2026-08-01T00:00:00.000Z')
/** The store's whole session vs the runtime window that materializes the newest turns. */
const MODEL_TOTAL = 40
const MODEL_PAGE = 8
const MODEL_WINDOW: Record<string, number> = { a: 8, b: 8, c: 6 }
const PARAGRAPH = Array.from({ length: 6 }, (_, line) => `line ${line + 1} of padding text for the transcript row`).join('\n')

function turns(key: string, count: number): ThreadMessage[] {
  return Array.from({ length: count }, (_, index) => [
    {
      id: `u-${key}-${index}`,
      role: 'user',
      content: [{ type: 'text', text: `message ${index} in ${key}\n${PARAGRAPH}` }],
      attachments: [],
      createdAt: CREATED_AT,
      metadata: { custom: {} }
    } as ThreadMessage,
    {
      id: `a-${key}-${index}`,
      role: 'assistant',
      content: [{ type: 'text', text: `response ${index} in ${key}\n${PARAGRAPH}\n${PARAGRAPH}` }],
      status: { type: 'complete', reason: 'stop' },
      createdAt: CREATED_AT,
      metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
    } as ThreadMessage
  ]).flat()
}

function Pane({
  clamp,
  expandWindow,
  isRunning,
  messages,
  olderAvailable,
  sessionKey,
  visible
}: {
  clamp: boolean
  expandWindow: (beforePrepend?: () => void) => void | Promise<boolean>
  isRunning: boolean
  messages: ThreadMessage[]
  olderAvailable: boolean
  sessionKey: string
  visible: boolean
}) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({ isRunning, messages, onNew: async () => {} })
  const pullOlder = useCallback((beforePrepend?: () => void) => expandWindow(beforePrepend), [expandWindow])
  const window = useMemo(() => ({ expandWindow: pullOlder, olderAvailable }), [pullOlder, olderAvailable])

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TranscriptWindowProvider value={window}>
        <PaneLifecycleContext.Provider value={visible ? 'visible' : 'hot-hidden'}>
          <PaneVisibleContext.Provider value={visible}>
            <Thread clampToComposer={clamp} scrollProfile="default" sessionId={sessionKey} sessionKey={sessionKey} />
          </PaneVisibleContext.Provider>
        </PaneLifecycleContext.Provider>
      </TranscriptWindowProvider>
    </AssistantRuntimeProvider>
  )
}

function Harness() {
  const [active, setActive] = useState<string>('a')
  const [clamp, setClamp] = useState(false)
  const [counts, setCounts] = useState<Record<string, number>>({ a: MODEL_TOTAL, b: 10, c: 6 })
  // Mounted window per session, exactly the app's shape: the store holds the whole session, the
  // runtime window materializes the newest `mounted` turns, and it is rebuilt at the tail when the
  // session is re-entered.
  const [mounted, setMounted] = useState<Record<string, number>>(MODEL_WINDOW)
  const [streaming, setStreaming] = useState(false)

  const totals = counts

  // A hidden pane's transcript is PRUNED in the real app (its runtime window collapses to a short
  // tail — the live trace shows a hidden pane around 989px where the revealed one is 4385px). That
  // prune-and-regrow on reveal is the case this harness has to model, because a reader parked at the
  // bottom must still be at the bottom once the full transcript lays back out.
  const PRUNED_ROWS = 2

  const messages = useMemo(
    () =>
      Object.fromEntries(
        KEYS.map(key => {
          const window = mounted[key] ?? 1
          const rows = key === active ? window : Math.min(window, PRUNED_ROWS)

          return [key, turns(key, totals[key] ?? 0).slice(-Math.max(1, rows))]
        })
      ) as Record<string, ThreadMessage[]>,
    [active, mounted, totals]
  )

  const grow = (by: number) => setCounts(current => ({ ...current, [active]: (current[active] ?? 0) + by }))

  const expand = useCallback((key: string) => {
    setMounted(current => ({ ...current, [key]: Math.min(MODEL_TOTAL, (current[key] ?? 0) + MODEL_PAGE) }))
  }, [])

  // A page that never commits (the remote-page failure the app's own contract allows): the older
  // rows arrive but the caller's beforePrepend never fires, so the reader is never anchored.
  const failPages = useRef(false)

  const expanders = useMemo(() => {
    const map: Record<string, (beforePrepend?: () => void) => Promise<boolean>> = {}

    for (const key of KEYS) {
      // The real `expandWindow` calls `beforePrepend` when the older page is about to commit (the
      // caller anchors the on-screen row) and resolves `true` when a page actually landed. The
      // parked-offset regrow branches on both.
      map[key] = async (beforePrepend?: () => void) => {
        if (failPages.current) {
          return true
        }

        beforePrepend?.()
        expand(key)

        return true
      }
    }

    return map
  }, [expand])

  // Switching tabs does not shrink the runtime window — a kept-alive pane comes back at whatever it
  // had (the real trace: a revealed pane at 4385px that was 989px while hidden, the prune being the
  // hidden-render rule below). Only a cold re-adopt rebuilds the window at the tail.
  const selectTab = (key: string) => {
    setActive(key)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div id="tabs" style={{ borderBottom: '1px solid #333', display: 'flex', flexWrap: 'wrap', gap: 4, padding: 6 }}>
        {KEYS.map(key => (
          <button
            data-tab={key}
            key={key}
            onClick={() => selectTab(key)}
            style={{ opacity: key === active ? 1 : 0.55, padding: '4px 10px' }}
          >
            {key.toUpperCase()}
          </button>
        ))}
        <button id="grow" onClick={() => grow(1)} style={{ marginLeft: 'auto', padding: '4px 10px' }}>
          grow active
        </button>
        <button id="grow-20" onClick={() => grow(20)} style={{ padding: '4px 10px' }}>
          grow 20
        </button>
        <button id="stream" onClick={() => setStreaming(value => !value)} style={{ padding: '4px 10px' }}>
          toggle run
        </button>
        <button id="clamp" onClick={() => setClamp(value => !value)} style={{ padding: '4px 10px' }}>
          toggle clamp
        </button>
        <button id="deep" onClick={() => expand(active)} style={{ padding: '4px 10px' }}>
          load older page
        </button>
        <button
          id="fail-pages"
          onClick={() => {
            failPages.current = !failPages.current
          }}
          style={{ padding: '4px 10px' }}
        >
          fail pages
        </button>
      </div>
      <div id="zone" style={{ flex: 1, minHeight: 0, position: 'relative' }}>
        {KEYS.map(key => (
          <div
            data-key={key}
            data-pane-hidden={key === active ? undefined : ''}
            key={key}
            style={{
              inset: 0,
              opacity: key === active ? 1 : 0,
              overflow: 'auto',
              position: 'absolute',
              visibility: key === active ? 'visible' : 'hidden'
            }}
          >
            <Pane
              clamp={clamp}
              expandWindow={expanders[key]!}
              isRunning={streaming && key === active}
              messages={messages[key] ?? []}
              olderAvailable={(totals[key] ?? 0) > (mounted[key] ?? 0)}
              sessionKey={key}
              visible={key === active}
            />
          </div>
        ))}
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Harness />
  </StrictMode>
)
