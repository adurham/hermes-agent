import { getQueuedPrompts, type QueuedPromptEntry, writeSessionQueue } from './composer-queue'

const STORAGE_KEY = 'hermes.desktop.pendingSubmissions.v1'
interface PendingSubmission {
  id: string
  text: string
  displayText?: string
  status?: string
}
type Journal = Record<string, Record<string, PendingSubmission>>

const readJournal = (): Journal => {
  try {
    return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '{}') as Journal
  } catch {
    return {}
  }
}

// Not an outbox: an uncertain accepted input must never be replayed automatically.
export function trackPendingSubmission(key: string, entry: PendingSubmission): void {
  const journal = readJournal()
  journal[key] = { ...journal[key], [entry.id]: entry }
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(journal))
}

// Array.isArray narrows `unknown` to `any[]`; the helpers keep that wire shape.
type RawReceipts = any[]

// An admission only moves forward: queued -> started -> (unknown after an owner restart) ->
// retired (gone from the pending set: terminal). Snapshots can arrive out of order (a resume
// result racing the live fanout, a replayed session.info), so a receipt weaker than the strongest
// state already observed for its admission is stale and never repaints the queue.
const STATUS_RANK: Record<string, number> = { queued: 0, started: 1, unknown: 2, retired: 3 }
// Retired identities are remembered per session only as long as a late snapshot can matter.
const RETIRED_MEMORY = 200

const isStale = (raw: { admission_id: string; status: string }, known: Record<string, PendingSubmission>) =>
  (STATUS_RANK[raw.status] ?? 0) < (STATUS_RANK[known[raw.admission_id]?.status ?? ''] ?? -1)

function collectReceipts(value: RawReceipts, known: Record<string, PendingSubmission>) {
  const receipts = new Map<string, PendingSubmission>()
  const admissionByInput = new Map<string, string>()

  for (const raw of value) {
    if (!raw || typeof raw.admission_id !== 'string' || !['queued', 'started', 'unknown'].includes(raw.status) ||
        isStale(raw, known)) {
      continue
    }

    const id = raw.admission_id

    if (typeof raw.input_id === 'string') {
      admissionByInput.set(raw.input_id, id)
    }

    receipts.set(id, {
      ...known[id],
      id,
      text: typeof raw.user === 'string' ? raw.user : (known[id]?.text ?? ''),
      status: raw.status
    })
  }

  return { receipts, admissionByInput }
}

function projectQueue(
  current: QueuedPromptEntry[],
  receipts: Map<string, PendingSubmission>,
  admissionByInput: Map<string, string>
): QueuedPromptEntry[] {
  const next: QueuedPromptEntry[] = []

  for (const entry of current) {
    const receipt = receipts.get(admissionByInput.get(entry.id) ?? entry.id)

    if (receipt) {
      if (receipt.status !== 'started') {
        next.push({ ...entry, id: receipt.id, serverStatus: receipt.status })
      }

      receipts.delete(receipt.id)
    } else if (!entry.serverStatus) {
      next.push(entry)
    }
  }

  for (const receipt of receipts.values()) {
    if (receipt.status !== 'started') {
      next.push({
        id: receipt.id,
        text: receipt.text,
        displayText: receipt.displayText,
        attachments: [],
        queuedAt: Date.now(),
        serverStatus: receipt.status
      })
    }
  }

  return next
}

function updateKnownReceipts(known: Record<string, PendingSubmission>, value: RawReceipts): void {
  // Only observed server records may be retired by their later absence. The retirement is kept
  // (text dropped) so a stale snapshot that still lists the admission cannot resurrect its card.
  for (const [id, entry] of Object.entries(known)) {
    if (entry.status && entry.status !== 'retired' && !value.some(raw => raw?.admission_id === id)) {
      known[id] = { id, text: '', status: 'retired' }
    }
  }

  const retired = Object.keys(known).filter(id => known[id].status === 'retired')

  for (const id of retired.slice(0, Math.max(0, retired.length - RETIRED_MEMORY))) {
    delete known[id]
  }

  for (const raw of value) {
    if (typeof raw?.admission_id === 'string' && !isStale(raw, known)) {
      known[raw.admission_id] = {
        ...known[raw.admission_id],
        id: raw.admission_id,
        text: raw.user ?? known[raw.admission_id]?.text ?? '',
        status: raw.status
      }
    }
  }
}

export function reconcilePendingSubmissions(key: string, value: unknown): void {
  if (!Array.isArray(value)) {
    return
  }

  const journal = readJournal()
  const known = journal[key] ?? {}
  const { receipts, admissionByInput } = collectReceipts(value, known)

  const current = getQueuedPrompts(key)
  const next = projectQueue(current, receipts, admissionByInput)

  updateKnownReceipts(known, value)

  journal[key] = known
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(journal))

  if (JSON.stringify(current) !== JSON.stringify(next)) {
    writeSessionQueue(key, next)
  }
}
