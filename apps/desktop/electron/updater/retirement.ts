import { setTimeout as delay } from 'node:timers/promises'

import {
  assertRetirementReady,
  canonicalRetirementPath,
  insideRetirementRoot,
  retirementDigest,
  type RetirementJournal,
  type RetirementRecord,
  type RetirementRequest,
  type RetirementStateSnapshot,
  validateRetirementRequest
} from './retirement-state'

export interface RetirementNativeAdapter {
  install(request: RetirementRequest, journal: RetirementJournal): Promise<void>
  verifyDestination(request: RetirementRequest): Promise<void>
  activate(request: RetirementRequest, journal: RetirementJournal): Promise<void>
  removePreview(request: RetirementRequest, journal: RetirementJournal): Promise<void>
  isPreviewRemoved(request: RetirementRequest): Promise<boolean>
}

export interface RetirementDependencies {
  native: RetirementNativeAdapter
  /** Revalidate the authority's immutable receiver pin. */
  assertAdmitted(request: RetirementRequest): Promise<void>
  /** Quiesced private snapshot plus preservation of any removal-scoped state. */
  prepareState(request: RetirementRequest, journal: RetirementJournal): Promise<RetirementStateSnapshot>
  /** Use lifecycle ownership. Do not kill a supervised or unrelated backend. */
  assertSourceQuiescent(request: RetirementRequest): Promise<void>
  /** Re-probe the current destination instance and selected backend on every cleanup retry. */
  assertDestinationReady(record: RetirementRecord): Promise<void>
}

export type RetirementOutcome =
  | { status: 'awaiting-destination'; forwardOnly: boolean }
  | { status: 'cleanup-pending'; error: string; forwardOnly: true }
  | { status: 'complete'; forwardOnly: true }

export interface RetirementReadinessProbe {
  home: string
  profile: () => string
  connectionId: () => string
  rendererMounted: () => boolean
  /** Must exercise the selected authenticated backend, not its public health endpoint. */
  backend: () => Promise<void>
}

export async function verifyRetirementReadiness(record: RetirementRecord, probe: RetirementReadinessProbe): Promise<void> {
  if (probe.home !== record.state.selectedHome || probe.profile() !== record.request.selection.profile ||
      probe.connectionId() !== record.request.selection.connectionId) {
    throw new Error('The destination is not using the consented workspace and connection')
  }
  if (!probe.rendererMounted()) { throw new Error('Waiting for the stable renderer to mount') }
  await probe.backend()
}

/** One bounded recovery loop covers both backend startup and lagging native release. */
export async function awaitRetirementCleanup(complete: () => Promise<RetirementOutcome>): Promise<RetirementOutcome> {
  const deadline: number = Date.now() + 120_000
  let message: string = 'Stable did not become ready'
  while (Date.now() < deadline) {
    try {
      const result: RetirementOutcome = await complete()
      if (result.status !== 'cleanup-pending') { return result }
      message = result.error
    } catch (error) { message = error instanceof Error ? error.message : String(error) }
    await delay(1000)
  }
  return { status: 'cleanup-pending', forwardOnly: true, error: message }
}

export async function prepareRetirement(
  journal: RetirementJournal,
  request: RetirementRequest,
  deps: RetirementDependencies
): Promise<RetirementRecord> {
  validateRetirementRequest(request)

  if (request.id !== journal.id) {
    throw new Error('Retirement transaction ID mismatch')
  }

  await deps.assertAdmitted(request)

  return journal.exclusive(async (): Promise<RetirementRecord> => {
    let existing: RetirementRecord | null = null
    try {
      existing = await journal.read()

      if (existing.requestDigest !== retirementDigest(request)) {
        throw new Error('Transaction already binds another request')
      }

      if (existing.receiverStarted) { return existing }
    } catch (error) {
      if (!(error instanceof Error) || !('code' in error) || error.code !== 'ENOENT') {
        throw error
      }
    }

    await deps.assertSourceQuiescent(request)
    const state: RetirementStateSnapshot = await deps.prepareState(request, journal)

    const snapshot: string = await canonicalRetirementPath(state.snapshotHome)

    for (const live of [
      request.source.home,
      request.selection.home,
      request.source.appPath,
      request.destination.appPath
    ]) {
      const canonical: string = await canonicalRetirementPath(live)

      if (insideRetirementRoot(canonical, snapshot) || insideRetirementRoot(snapshot, canonical)) {
        throw new Error('Compatibility snapshot must be separate from live state and application roots')
      }
    }

    if (state.selectedHome !== request.selection.home) {
      throw new Error('Preserved home must be consented before preparation')
    }

    await journal.assertPaths(request)

    const record: RetirementRecord = {
      request,
      requestDigest: retirementDigest(request),
      stage: existing?.stage ?? 'prepared',
      state,
      receiverStarted: false,
      receiverApplied: false,
      previousSelection: null,
      ready: null
    }

    await journal.write(record)

    return record
  })
}

/** Source stops after activation. Stable owns cleanup, outside the source package. */
export async function resumeRetirement(
  journal: RetirementJournal,
  deps: Omit<RetirementDependencies, 'prepareState'>,
  caller: 'source' | 'destination'
): Promise<RetirementOutcome> {
  const outcome: RetirementOutcome = await journal.exclusive(async (): Promise<RetirementOutcome> => {
    const record: RetirementRecord = await journal.read()

    if (record.stage === 'complete') {
      return { status: 'complete', forwardOnly: true }
    }

    await journal.assertPaths(record.request)
    await deps.assertAdmitted(record.request)

    if (record.stage === 'prepared') {
      if (caller !== 'source') {
        throw new Error('Destination cannot install itself during retirement')
      }

      await deps.native.install(record.request, journal)
      await deps.native.verifyDestination(record.request)
      record.stage = 'destination-installed'
      await journal.write(record)
    }

    if (record.stage === 'destination-installed') {
      await deps.native.verifyDestination(record.request)

      return { status: 'awaiting-destination', forwardOnly: record.receiverStarted }
    }

    if (caller !== 'destination') {
      return { status: 'awaiting-destination', forwardOnly: true }
    }

    try {
      if (!record.ready) {
        throw new Error('Destination readiness is required before cleanup')
      }

      assertRetirementReady(record, record.ready)
      await deps.native.verifyDestination(record.request)
      await deps.assertDestinationReady(record)
      await deps.assertSourceQuiescent(record.request)

      if (record.stage === 'destination-ready') {
        if (!(await deps.native.isPreviewRemoved(record.request))) {
          await deps.native.removePreview(record.request, journal)
        }

        if (!(await deps.native.isPreviewRemoved(record.request))) {
          throw new Error('Native preview removal is still pending')
        }

        record.stage = 'preview-removed'
        await journal.write(record)
      }

      record.stage = 'complete'
      await journal.write(record)

      return { status: 'complete', forwardOnly: true }
    } catch (error) {
      return {
        status: 'cleanup-pending',
        error: error instanceof Error ? error.message : String(error),
        forwardOnly: true
      }
    }
  })

  if (caller === 'source' && outcome.status === 'awaiting-destination') {
    const record: RetirementRecord = await journal.read()

    if (record.stage === 'destination-installed') {
      await deps.native.activate(record.request, journal)
    }
  }

  return outcome
}

export async function assertNativeRetirementRemoval(
  journal: RetirementJournal,
  request: RetirementRequest
): Promise<void> {
  const record: RetirementRecord = await journal.read()

  if (record.stage !== 'destination-ready' || record.requestDigest !== retirementDigest(request) || !record.ready) {
    throw new Error('Native removal requires the correlated destination-ready transaction')
  }

  assertRetirementReady(record, record.ready)
}
