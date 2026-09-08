// updater/external.ts — the steward-owned strategy.
//
// The stamp declares 'external' when the package owner handles updates
// without an in-app check or apply action.

import type { UpdaterApplyResultWire, UpdaterStatusWire } from './index'

export const EXTERNAL_UNSUPPORTED_MESSAGE =
  'Updates are managed by the package owner outside this app.'

export class ExternalStrategy {
  readonly mechanism = 'external' as const
  readonly supported = false

  async check(): Promise<UpdaterStatusWire> {
    return {
      supported: false,
      mechanism: 'external',
      reason: 'bundled-not-appinstaller',
      message: EXTERNAL_UNSUPPORTED_MESSAGE,
      fetchedAt: Date.now()
    }
  }

  async apply(): Promise<UpdaterApplyResultWire> {
    return { ok: true, manual: true, bundled: true, mechanism: 'external' }
  }
}
