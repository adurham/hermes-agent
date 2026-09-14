import { randomBytes, timingSafeEqual } from 'node:crypto'

import { recordRetirementReady } from './retirement'
import {
  canonicalRetirementPath,
  type RetirementCorrelation,
  type RetirementJournal,
  type RetirementReady,
  type RetirementRecord,
  type RetirementRequest,
  type RetirementSelection,
  signRetirementReady
} from './retirement-state'

export interface RetirementExistingSelection {
  home: string
  profile: string
  connectionId: string
  operatorOverride: boolean
}

export interface RetirementReceiverDependencies {
  /** Check process.execPath, native signature, baked commit and stable update owner. */
  verifyRunningDestination(request: RetirementRequest): Promise<string>
  /** Real destination readers on the disposable snapshot, before live migrations. */
  assertCompatibleSnapshot(record: RetirementRecord): Promise<void>
  readSelection(): Promise<RetirementExistingSelection | null>
  /** CAS through the boot/connection writers. Must be idempotent for request.id. Never copy userData. */
  adoptSelection(record: RetirementRecord, previous: RetirementExistingSelection | null): Promise<void>
  /** Real renderer and selected local/remote backend; finish reauthentication before resolving. */
  assertReady(record: RetirementRecord): Promise<void>
}

const receiverInstance: string = randomBytes(32).toString('hex')

export function retirementArgument(id: string, token: string): string {
  if (!/^[a-f0-9]{32}$/.test(id) || !/^[a-f0-9]{64}$/.test(token)) {
    throw new Error('Invalid retirement correlation')
  }

  return `--hermes-retirement=${id}.${token}`
}

/** Parse only our argument. The journal root is local policy, never a launch argument. */
export function parseRetirementArgument(argv: readonly string[]): RetirementCorrelation | null {
  const values: string[] = argv.filter((arg: string): boolean => arg.startsWith('--hermes-retirement='))

  if (!values.length) {
    return null
  }

  const match: RegExpExecArray | null = /^--hermes-retirement=([a-f0-9]{32})\.([a-f0-9]{64})$/.exec(values[0])

  if (values.length !== 1 || !match) {
    throw new Error('Invalid retirement activation argument')
  }

  return { id: match[1], token: match[2] }
}

function assertToken(record: RetirementRecord, token: string): void {
  if (
    !/^[a-f0-9]{64}$/.test(token) ||
    !timingSafeEqual(Buffer.from(token, 'hex'), Buffer.from(record.request.token, 'hex'))
  ) {
    throw new Error('Retirement correlation mismatch')
  }
}

function sameSelection(current: RetirementExistingSelection, selection: RetirementSelection): boolean {
  return (
    current.home === selection.home &&
    current.profile === selection.profile &&
    current.connectionId === selection.connectionId
  )
}

/** Invoke before normal home selection, and on the existing stable second-instance path. */
export async function receiveRetirement(
  journal: RetirementJournal,
  token: string,
  deps: RetirementReceiverDependencies
): Promise<RetirementSelection> {
  return journal.exclusive(async (): Promise<RetirementSelection> => {
    const record: RetirementRecord = await journal.read()
    assertToken(record, token)

    if (record.stage !== 'destination-installed' && record.stage !== 'destination-ready') {
      throw new Error('Retirement is not awaiting this destination')
    }

    await journal.assertPaths(record.request)
    await deps.verifyRunningDestination(record.request)

    if (record.request.selection.connectionId !== 'local' && !record.request.selection.reauthenticate) {
      throw new Error(
        'Reauthentication required: encrypted connection credentials cannot transfer between applications'
      )
    }

    if (record.receiverApplied) {
      const active: RetirementExistingSelection | null = await deps.readSelection()

      if (active && !sameSelection(active, record.request.selection)) {
        throw new Error('Stable workspace changed after retirement adoption; explicit forward recovery is required')
      }

      return record.request.selection
    }

    await deps.assertCompatibleSnapshot(record)
    const current: RetirementExistingSelection | null = await deps.readSelection()

    if (current) {
      current.home = await canonicalRetirementPath(current.home)

      if (!sameSelection(current, record.request.selection)) {
        const consent: RetirementExistingSelection | undefined = record.request.selection.conflictConsent

        if (
          current.operatorOverride ||
          !consent ||
          !sameSelection(current, { ...consent, choice: 'open-preview', reauthenticate: false })
        ) {
          throw new Error('Stable workspace conflict: explicit selection consent is required')
        }
      }
    }

    if (!record.receiverStarted) {
      record.previousSelection = current
      record.receiverStarted = true
      await journal.write(record)
    }

    await deps.adoptSelection(record, record.previousSelection)
    record.receiverApplied = true
    await journal.write(record)

    return record.request.selection
  })
}

/** Readiness is authored only by the verified destination, never by the installer. */
export async function completeRetirementReception(
  journal: RetirementJournal,
  token: string,
  deps: RetirementReceiverDependencies
): Promise<RetirementReady> {
  const record: RetirementRecord = await journal.read()
  assertToken(record, token)

  if (!record.receiverApplied || record.stage !== 'destination-installed') {
    throw new Error('Retirement state has not been adopted')
  }

  const executable: string = await deps.verifyRunningDestination(record.request)
  await deps.assertReady(record)

  const ready: RetirementReady = signRetirementReady(record.request, {
    requestDigest: record.requestDigest,
    id: record.request.id,
    home: record.state.selectedHome,
    profile: record.request.selection.profile,
    connectionId: record.request.selection.connectionId,
    executable,
    commit: record.request.destination.commit,
    pid: process.pid,
    instance: receiverInstance
  })

  await recordRetirementReady(journal, ready)

  return ready
}
