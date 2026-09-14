import { randomBytes, timingSafeEqual } from 'node:crypto'

import {
  canonicalRetirementPath,
  type RetirementCorrelation,
  retirementDigest,
  type RetirementJournal,
  type RetirementReady,
  type RetirementRecord,
  type RetirementRequest,
  type RetirementSelection,
  type RetirementWorkspaceChoice,
  signRetirementReady
} from './retirement-state'

export interface RetirementExistingSelection {
  home: string
  profile: string
  connectionId: string
  operatorOverride: boolean
  configured?: boolean
}

export interface RetirementWorkspaceConflict {
  current: RetirementExistingSelection
  requested: RetirementSelection
  canOpenPreview: boolean
}

export interface RetirementReceiverDependencies {
  /** The destination is already using this workspace; it cannot change homes in place. */
  runningSelection?: () => Pick<RetirementExistingSelection, 'home' | 'profile' | 'connectionId'>
  confirmWorkspaceConflict?: (conflict: RetirementWorkspaceConflict) => Promise<RetirementWorkspaceChoice | null>
  /** Check process.execPath, native signature, baked commit and stable update owner. */
  verifyRunningDestination(request: RetirementRequest): Promise<string>
  /** Real destination readers on the disposable snapshot, before live migrations. */
  assertCompatibleSnapshot(record: RetirementRecord): Promise<void>
  prepareConnection?: (selection: RetirementSelection) => RetirementSelection
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

function sameSelection(
  current: Pick<RetirementSelection, 'home' | 'profile' | 'connectionId'>,
  selection: Pick<RetirementSelection, 'home' | 'profile' | 'connectionId'>
): boolean {
  return (
    current.home === selection.home &&
    current.profile === selection.profile &&
    current.connectionId === selection.connectionId
  )
}

async function readCurrentSelection(deps: RetirementReceiverDependencies): Promise<RetirementExistingSelection | null> {
  const current: RetirementExistingSelection | null = await deps.readSelection()

  return current ? { ...current, home: await canonicalRetirementPath(current.home) } : null
}

async function consentToWorkspace(
  record: RetirementRecord,
  current: RetirementExistingSelection | null,
  deps: RetirementReceiverDependencies
): Promise<void> {
  if (!current || current.configured === false) {
    if (record.request.selection.choice === 'keep-stable') {
      throw new Error('There is no configured stable workspace to keep')
    }

    return
  }

  if (sameSelection(current, record.request.selection)) {
    return
  }

  const consent: RetirementSelection['conflictConsent'] = record.request.selection.conflictConsent

  const consentMatches: boolean = Boolean(
    consent && sameSelection(current, consent) && consent.operatorOverride === current.operatorOverride
  )

  if (record.receiverStarted && !consentMatches) {
    if (interruptedAdoption(record, current)) { return }
    throw new Error('Stable workspace changed after retirement admission; explicit forward recovery is required')
  }

  const canOpenPreview: boolean = !deps.runningSelection && !current.operatorOverride
  let choice: RetirementWorkspaceChoice = record.request.selection.choice

  if (!consentMatches) {
    if (!deps.confirmWorkspaceConflict) {
      throw new Error('Stable workspace conflict: explicit selection consent is required')
    }

    const confirmed: RetirementWorkspaceChoice | null = await deps.confirmWorkspaceConflict({
      current: { ...current },
      requested: { ...record.request.selection },
      canOpenPreview
    })

    if (!confirmed) {
      throw new Error('Workspace migration cancelled; preview preserved')
    }

    const latest: RetirementExistingSelection | null = await readCurrentSelection(deps)

    if (!latest || !sameSelection(current, latest) || latest.operatorOverride !== current.operatorOverride) {
      throw new Error('Stable workspace changed while awaiting consent; review the migration again')
    }

    choice = confirmed
  }

  if (choice === 'open-preview' && !canOpenPreview) {
    throw new Error(
      'Close stable and remove any explicit home override before opening the preview workspace, then retry. Both workspaces are preserved.'
    )
  }

  const selected: Pick<RetirementSelection, 'home' | 'profile' | 'connectionId'> =
    choice === 'keep-stable' ? current : record.request.selection

  record.request.selection = { ...record.request.selection, ...selected, choice, conflictConsent: { ...current } }
  record.state.selectedHome = selected.home
  record.requestDigest = retirementDigest(record.request)
}

function interruptedAdoption(record: RetirementRecord, current: RetirementExistingSelection): boolean {
  const previous = record.previousSelection
  // Home and connection have different normal writers. A crash between them
  // may leave exactly this transaction's old/new pair, never a third choice.
  return Boolean(previous && current.operatorOverride === previous.operatorOverride &&
    (current.home === previous.home || current.home === record.request.selection.home) &&
    (current.profile === previous.profile || current.profile === record.request.selection.profile) &&
    (current.connectionId === previous.connectionId || current.connectionId === record.request.selection.connectionId))
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

    if (
      record.stage !== 'destination-installed' &&
      record.stage !== 'destination-ready' &&
      record.stage !== 'preview-removed'
    ) {
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
      const active: RetirementExistingSelection | null = await readCurrentSelection(deps)

      if (active && !sameSelection(active, record.request.selection)) {
        throw new Error('Stable workspace changed after retirement adoption; explicit forward recovery is required')
      }

      return record.request.selection
    }

    await deps.assertCompatibleSnapshot(record)
    if (!record.receiverStarted && record.request.selection.choice === 'open-preview' && deps.prepareConnection) {
      record.request.selection = deps.prepareConnection(record.request.selection)
      record.requestDigest = retirementDigest(record.request)
    }
    const current: RetirementExistingSelection | null = await readCurrentSelection(deps)
    await consentToWorkspace(record, current, deps)
    await journal.assertPaths(record.request)

    if (!record.receiverStarted) {
      record.previousSelection = current
      record.receiverStarted = true
      await journal.write(record)
    }

    if (record.request.selection.choice === 'open-preview') {
      await deps.adoptSelection(record, record.previousSelection)
    }

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

  await journal.exclusive(async (): Promise<void> => {
    const current: RetirementRecord = await journal.read()
    if (current.stage !== 'destination-installed' || !current.receiverStarted || current.requestDigest !== record.requestDigest) {
      throw new Error('Receiver admission changed before readiness')
    }
    current.ready = ready
    current.stage = 'destination-ready'
    await journal.write(current)
  })

  return ready
}
