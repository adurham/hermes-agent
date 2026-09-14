import type { ChannelBuild } from './channel-protocol'
import type { ChannelResolver, ChannelRetirement, ChannelTarget } from './channel'
import type { UpdaterApplyResultWire, UpdaterStatusWire, UpdaterStrategy } from './index'

export interface ChannelRetirementStatus {
  state: 'available' | 'waiting' | 'incompatible' | 'conflict' | 'migrating' | 'cleanup-pending' | 'complete'
  destination: string
  version: string
  message?: string
}
export interface ChannelRetirementCallbacks {
  check: (retirement: ChannelRetirement) => Promise<Pick<ChannelRetirementStatus, 'state' | 'message'>>
  apply: (retirement: ChannelRetirement) => Promise<UpdaterApplyResultWire>
}
export interface ChannelStrategyDeps {
  resolver: Pick<ChannelResolver, 'resolve'>
  build: ChannelBuild
  mechanism: 'electron-updater' | 'app-installer'
  nativeFactory: (target: ChannelTarget) => UpdaterStrategy
  retirement?: ChannelRetirementCallbacks
}
interface NativeSelection { kind: 'native'; strategy: UpdaterStrategy; available: boolean }
interface RetirementSelection { kind: 'retirement'; value: ChannelRetirement; state: ChannelRetirementStatus['state'] }
type Selection = NativeSelection | RetirementSelection | { kind: 'empty' }

/** One selection owns a check/apply operation; background checks cannot retarget it. */
export class ChannelStrategy implements UpdaterStrategy {
  readonly mechanism: ChannelStrategyDeps['mechanism']
  private selection: Selection | null = null
  private busy = false

  constructor(private readonly deps: ChannelStrategyDeps) { this.mechanism = deps.mechanism }

  private enter(): void {
    if (this.busy) { throw new Error('An update operation is already in progress.') }
    this.busy = true
  }

  async check(): Promise<UpdaterStatusWire> {
    this.enter()
    try { return await this.select() } finally { this.busy = false }
  }

  private async select(): Promise<UpdaterStatusWire> {
    // Failed reads invalidate prior availability, never leave a stale install action.
    this.selection = null
    const result = await this.deps.resolver.resolve()
    const base: UpdaterStatusWire = {
      supported: true, mechanism: this.mechanism, channel: this.deps.build.channel,
      currentVersion: this.deps.build.version, fetchedAt: Date.now()
    }
    if (result.kind === 'empty') {
      this.selection = { kind: 'empty' }
      return { ...base, updateAvailable: false, reason: 'no-build-published' }
    }
    if (result.kind === 'retirement') {
      const status = this.deps.retirement ? await this.deps.retirement.check(result.retirement) :
        { state: 'waiting' as const, message: 'The retirement receiver is not available in this installation.' }
      this.selection = { kind: 'retirement', value: result.retirement, state: status.state }
      return {
        ...base, retirement: { ...status, destination: result.retirement.target.channel.name, version: result.retirement.target.manifest.request.version }
      }
    }
    const target = result.target
    if (target.manifest.request.sequence <= this.deps.build.sequence) {
      this.selection = { kind: 'empty' }
      return { ...base, updateAvailable: false }
    }
    const strategy = this.deps.nativeFactory(target)
    const status = await strategy.check()
    if (status.error || status.updateAvailable === undefined) { throw new Error(status.error || 'Native update availability unknown') }
    this.selection = { kind: 'native', strategy, available: status.updateAvailable }
    return { ...status, ...base, latestTag: `v${target.manifest.request.version}`, targetSha: target.manifest.request.commit }
  }

  async apply(): Promise<UpdaterApplyResultWire> {
    this.enter()
    try {
      if (!this.selection) { await this.select() }
      const selected = this.selection
      if (selected?.kind === 'retirement') { return { ok: false, error: 'Retirement requires explicit migration consent.' } }
      if (selected?.kind === 'native' && selected.available) { return await selected.strategy.apply() }
      return { ok: true, mechanism: this.mechanism }
    } finally { this.busy = false }
  }

  /** Only the explicit migration action may call this method. */
  async applyRetirement(): Promise<UpdaterApplyResultWire> {
    this.enter()
    try {
      if (this.selection?.kind !== 'retirement' || !this.deps.retirement) {
        return { ok: false, error: 'Check the retirement offer before accepting migration.' }
      }
      if (this.selection.state !== 'available' && this.selection.state !== 'cleanup-pending') {
        return { ok: false, error: 'Retirement is blocked; resolve the reported issue and retry.' }
      }
      return await this.deps.retirement.apply(this.selection.value)
    } finally { this.busy = false }
  }
}
