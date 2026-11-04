import type { GatewayEventPayload } from '@/lib/chat-messages'
import { normalizePersonalityValue } from '@/lib/chat-runtime'
import { isTodoToolName } from '@/lib/todos'

import type { ClientSessionState } from '../../../types'

type SessionRuntimeStatePatch = Partial<
  Pick<
    ClientSessionState,
    | 'branch'
    | 'cwd'
    | 'fast'
    | 'model'
    | 'personality'
    | 'provider'
    | 'reasoningEffort'
    | 'reasoningEffortPending'
    | 'reasoningEffortWire'
    | 'serviceTier'
    | 'yolo'
  >
>

export function sessionInfoStatePatch(payload: GatewayEventPayload | undefined): SessionRuntimeStatePatch {
  const patch: SessionRuntimeStatePatch = {}

  if (typeof payload?.model === 'string') {
    patch.model = payload.model || ''
  }

  if (typeof payload?.provider === 'string') {
    patch.provider = payload.provider || ''
  }

  if (typeof payload?.cwd === 'string') {
    patch.cwd = payload.cwd
  }

  if (typeof payload?.branch === 'string') {
    patch.branch = payload.branch
  }

  if (typeof payload?.personality === 'string') {
    patch.personality = normalizePersonalityValue(payload.personality)
  }

  if (typeof payload?.reasoning_effort === 'string') {
    patch.reasoningEffort = payload.reasoning_effort
    patch.reasoningEffortPending = false
  }

  if (typeof payload?.reasoning_effort_wire === 'string') {
    patch.reasoningEffortWire = payload.reasoning_effort_wire
  }

  if (typeof payload?.service_tier === 'string') {
    patch.serviceTier = payload.service_tier
  }

  if (typeof payload?.fast === 'boolean') {
    patch.fast = payload.fast
  }

  if (typeof payload?.yolo === 'boolean') {
    patch.yolo = payload.yolo
  }

  return patch
}

export function hasSessionInfoStatePatch(patch: SessionRuntimeStatePatch): boolean {
  return Object.keys(patch).length > 0
}

/** Keep the runtime-state object when a heartbeat only restates cached fields.
 *  `$sessionStates` feeds every mounted session surface, so an equivalent spread
 *  re-renders them all at the heartbeat cadence. */
export function applySessionInfoStatePatch(
  state: ClientSessionState,
  patch: SessionRuntimeStatePatch
): ClientSessionState {
  if (
    (patch.branch === undefined || patch.branch === state.branch) &&
    (patch.cwd === undefined || patch.cwd === state.cwd) &&
    (patch.fast === undefined || patch.fast === state.fast) &&
    (patch.model === undefined || patch.model === state.model) &&
    (patch.personality === undefined || patch.personality === state.personality) &&
    (patch.provider === undefined || patch.provider === state.provider) &&
    (patch.reasoningEffort === undefined || patch.reasoningEffort === state.reasoningEffort) &&
    // The wire level can change alone: an optimistic paint already holds the new
    // model/effort, so skipping here kept the previous route's clamp ("Medium→Max") forever.
    (patch.reasoningEffortWire === undefined || patch.reasoningEffortWire === state.reasoningEffortWire) &&
    (patch.reasoningEffortPending === undefined ||
      patch.reasoningEffortPending === Boolean(state.reasoningEffortPending)) &&
    (patch.serviceTier === undefined || patch.serviceTier === state.serviceTier) &&
    (patch.yolo === undefined || patch.yolo === state.yolo)
  ) {
    return state
  }

  return { ...state, ...patch }
}

// Minimum gap between two assistant-text flushes during a stream. Was 16ms
// Streamed text is committed at 10 Hz rather than 30 Hz. The old 33ms floor let
// ~2 tokens batch per commit at 60 tok/sec, but every commit is a React
// re-render plus a Streamdown re-parse of the growing last block, and each one
// also wakes WindowServer to composite the tile — which is what actually costs
// watts on a laptop. Nobody reads text faster than ~10 Hz, and measured on an
// M4 Max the streaming path (renderer + WindowServer) was ~1.5-2 W with five
// sessions live. 100ms cuts the commit count ~3x for no perceptible change.
// See `scripts/profile-typing-lag.md` for the earlier 33ms measurement work.
export const STREAM_DELTA_FLUSH_MS = 100

// Floor used while the window is VISIBLE but unfocused. Nothing on screen is
// being read at that moment, and every flush is a React commit plus a
// Streamdown re-parse of the growing last block, so 30fps of text growth buys
// nothing a background window can show. 120ms still moves the text ~8x/s, and
// the focus/visibility handlers flush the queue immediately on the way back in,
// so returning to the window never reveals a stalled reply. Chromium only
// throttles an occluded page, so without this a backgrounded-but-visible window
// keeps paying the display-cadence price for text nobody is watching.
export const STREAM_DELTA_FLUSH_UNFOCUSED_MS = 200

// Which floor applies right now. A real blur/focus latch alone is not enough —
// a missed `focus` event (system dialog, IME handoff, focus-follows-mouse WM)
// would leave a focused user's stream at 8fps for the rest of the session — so
// the latch is required AND the live `hasFocus()` check must agree. That
// conjunction is self-healing in both directions: a stuck latch is overridden
// the moment the document really is focused, and a spurious "not focused"
// report (app launching behind another window, devtools handoff, jsdom) can
// never slow the stream on its own because no blur was ever observed.
// The focus and visibilitychange handlers in index.ts flush the queue
// immediately on the way back, so returning never reveals a stalled reply.
let windowUnfocused = false

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('blur', () => {
    windowUnfocused = true
  })
  window.addEventListener('focus', () => {
    windowUnfocused = false
  })
}

export function streamDeltaFlushFloorMs(): number {
  const reallyUnfocused =
    windowUnfocused && typeof document !== 'undefined' && !document.hasFocus()

  return reallyUnfocused ? STREAM_DELTA_FLUSH_UNFOCUSED_MS : STREAM_DELTA_FLUSH_MS
}

// Ceiling for the ADAPTIVE flush gap (see scheduleDeltaFlush). Under heavy
// multi-stream load the gap stretches to 3x the measured flush cost so the
// main thread stays responsive to input; this cap guarantees streaming text
// still visibly updates at least ~4x per second no matter the load.
export const MAX_STREAM_FLUSH_GAP_MS = 250

// Floor for a session whose chat surface is NOT on screen — an inactive
// keep-alive tab in a zone, a minimized zone, a collapsed floating pane, or a
// tile sitting behind another tab. Those surfaces stay MOUNTED on purpose
// (`visibility: hidden`, see pane-shell/pane-visibility.ts) so scroll state
// survives a tab round-trip, which means they still take every flush: a store
// write, a React commit, and a Streamdown re-parse of the growing last block.
// None of that is readable at 30fps, so an off-screen session rides the same
// ~4/s cadence as the adaptive cap instead — the reveal, focus and
// visibilitychange handlers flush its queue the moment it is looked at again.
// Deliberately the SAME number as the adaptive cap: "text nobody can see" is
// never faster than "text the user can see under maximum load".
export const STREAM_DELTA_FLUSH_HIDDEN_PANE_MS = MAX_STREAM_FLUSH_GAP_MS

/** Whether the pane holding a session is on screen. `unknown` = no layout-tree
 *  pane renders this session at all (secondary windows, plain routes, a
 *  session that is only streaming into its cache), which must keep the
 *  window-based floor: a surface we cannot locate must never be slowed. */
export type StreamPaneVisibility = 'hidden' | 'unknown' | 'visible'

/** Which floor applies to ONE session, given its pane's visibility.
 *  The visible/unfocused floor is a lower bound, never a substitute: a hidden
 *  pane of an unfocused window still flushes at the parked cadence. */
export function streamDeltaFlushFloorForVisibility(
  visibility: StreamPaneVisibility,
  windowFloorMs: number = streamDeltaFlushFloorMs()
): number {
  return visibility === 'hidden' ? Math.max(windowFloorMs, STREAM_DELTA_FLUSH_HIDDEN_PANE_MS) : windowFloorMs
}

// How long an optimistically armed turn (busy/awaitingResponse set at submit /
// restore / edit, before the backend confirms it live) may hold off a
// session.info running=false heartbeat. Within this window a running=false is
// treated as a PRE-START report — the submit round trip hasn't finished, so
// settling on it would drop the spinner and reopen the send guard mid-flight.
// Past it, the gateway's running=false is authoritative: the turn never went
// live (rewind refused after the arm, gateway bounce, dropped submit response,
// missed terminal error event) and holding busy any longer latches the
// composer shut until app restart (#86795 — every send gets queued behind a
// turn that does not exist, and the queue drain waits on busy→false forever).
// Generous vs. the real pre-start gap: the busy-retry deadline is 6s and the
// backend flips running=true on accept, so heartbeats stop carrying false
// within a couple seconds of a healthy submit.
export const PRE_TURN_LIVE_SETTLE_GRACE_MS = 15_000

// Gateway/provider failures sometimes arrive as message.complete text instead
// of an explicit error event. Treat matches as inline assistant errors so they
// persist like real error events and don't get erased by hydrate fallback.
const COMPLETION_ERROR_PATTERNS = [
  /^API call failed after \d+ retries:/i,
  /^HTTP\s+\d{3}\b/i,
  /^(Provider|Gateway)\s+error:/i
]

export function completionErrorText(finalText: string): string | null {
  const text = finalText.trim()

  return text && COMPLETION_ERROR_PATTERNS.some(re => re.test(text)) ? text : null
}

export const SUBAGENT_EVENT_TYPES = new Set([
  'subagent.spawn_requested',
  'subagent.start',
  'subagent.thinking',
  'subagent.tool',
  'subagent.progress',
  'subagent.complete'
])

// Anonymous progress events that carry todos but no name still belong to the
// todo stream; named todo events are obviously routed there too.
export function toTodoPayload(payload: GatewayEventPayload | undefined): GatewayEventPayload | undefined {
  if (!payload) {
    return undefined
  }

  const isTodo = isTodoToolName(payload.name) || (!payload.name && Object.hasOwn(payload, 'todos'))

  return isTodo ? { ...payload, name: 'todo_list', tool_id: payload.tool_id || 'todo-live' } : undefined
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function parseMaybeRecord(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try {
      return asRecord(JSON.parse(value))
    } catch {
      return {}
    }
  }

  return asRecord(value)
}

const firstString = (...candidates: unknown[]): string => {
  for (const v of candidates) {
    if (typeof v === 'string' && v) {
      return v
    }
  }

  return ''
}

export function delegateTaskPayloads(
  payload: GatewayEventPayload | undefined,
  phase: 'running' | 'complete',
  sourceEventType?: string
): Record<string, unknown>[] {
  if (payload?.name !== 'delegate_task') {
    return []
  }

  const args = parseMaybeRecord(payload.args ?? payload.input)
  const result = parseMaybeRecord(payload.result)
  const rawTasks = Array.isArray(args.tasks) ? args.tasks : []
  const tasks = rawTasks.length ? rawTasks.map(parseMaybeRecord) : [args]
  const resultStatus = typeof result.status === 'string' ? result.status.toLowerCase() : ''
  const failedResult = Boolean(payload.error) || ['timeout', 'error', 'failed', 'failure'].includes(resultStatus)
  const status = phase === 'complete' ? (failedResult ? 'failed' : 'completed') : 'running'
  const toolId = payload.tool_id || payload.tool_call_id || payload.id || 'delegate_task'
  const progressText = firstString(payload.preview, payload.message, payload.context)

  const eventType =
    phase === 'complete'
      ? 'subagent.complete'
      : sourceEventType === 'tool.start'
        ? 'subagent.start'
        : 'subagent.progress'

  return tasks.map((task, index) => {
    const goal = firstString(task.goal, args.goal, payload.context) || 'Delegated task'
    const summary = firstString(result.summary, payload.summary, payload.message)

    return {
      depth: 0,
      duration_seconds: payload.duration_s,
      goal,
      status,
      subagent_id: `delegate-tool:${toolId}:${index}`,
      summary: summary || undefined,
      task_count: tasks.length,
      task_index: index,
      text: eventType === 'subagent.progress' ? progressText || goal : undefined,
      tool_name: eventType === 'subagent.start' ? 'delegate_task' : undefined,
      tool_preview: eventType === 'subagent.start' ? progressText : undefined,
      toolsets: Array.isArray(task.toolsets) ? task.toolsets : Array.isArray(args.toolsets) ? args.toolsets : [],
      event_type: eventType,
      output_tail:
        phase === 'complete' && summary
          ? [{ is_error: Boolean(payload.error), preview: summary, tool: 'delegate_task' }]
          : undefined
    }
  })
}
