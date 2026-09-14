import { setTimeout as delay } from 'node:timers/promises'

import { finishRetirementStartup, type RetirementReception } from './retirement-host'
import type { RetirementRecord } from './retirement-state'
import type { RetirementOutcome } from './retirement'

export interface RetirementReadinessProbe {
  home: string
  profile: () => string
  isLocal: () => boolean
  rendererMounted: () => boolean
  backend: () => Promise<void>
}

/** A loaded document alone is not readiness: require the mounted UI and selected backend. */
export async function verifyRetirementReadiness(record: RetirementRecord, probe: RetirementReadinessProbe): Promise<void> {
  if (probe.home !== record.state.selectedHome || probe.profile() !== record.request.selection.profile || !probe.isLocal()) {
    throw new Error('The destination is not using the consented local workspace')
  }
  if (!probe.rendererMounted()) { throw new Error('Waiting for the stable renderer to mount') }
  await probe.backend()
}

export async function completeDesktopRetirement(reception: RetirementReception, probe: RetirementReadinessProbe): Promise<RetirementOutcome> {
  const deadline: number = Date.now() + 120_000
  let lastError: Error = new Error('Stable did not become ready')
  while (Date.now() < deadline) {
    try {
      await verifyRetirementReadiness(await reception.journal.read(), probe)
      // Source quit and final native release can lag renderer readiness. The
      // same bounded retry also resumes a previous cleanup-pending receipt.
      const result: RetirementOutcome = await finishRetirementStartup(reception,
        (record: RetirementRecord): Promise<void> => verifyRetirementReadiness(record, probe))
      if (result.status !== 'cleanup-pending') { return result }
      lastError = new Error(result.error)
    } catch (error) { lastError = error instanceof Error ? error : new Error(String(error)) }
    await delay(1000)
  }
  return { status: 'cleanup-pending', forwardOnly: true, error: lastError.message }
}
