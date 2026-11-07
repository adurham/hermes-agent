/**
 * `BOTS → SESSIONS` must not carry Bot Mode's unified session list back into
 * the Sessions sidebar: the tab switch has to land on the gateway the user
 * chose, not on every gateway at once.
 *
 * Bot Mode itself sets `$showAllProfiles` on the way in (the SDK's
 * `keepAllProfilesScope` default), so the hook captures the user's own scope
 * once on entry and writes it back on exit.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $workspaceMode } from '@/components/pane-shell/workspace-scope'
import { $showAllProfiles, setShowAllProfiles } from '@/store/profile'

import { restoreSessionsScopeAfterBots } from './use-sessions-scope-after-bots'

beforeEach(() => {
  $workspaceMode.set('sessions')
  setShowAllProfiles(false)
})

function harness(initialScope: boolean) {
  let scope = initialScope

  const write = vi.fn((value: boolean) => {
    scope = value
  })

  return {
    restore: restoreSessionsScopeAfterBots(() => scope, write),
    scope: () => scope,
    write
  }
}

describe('Sessions sidebar scope across Bot Mode', () => {
  it('returns to the chosen gateway after Bot Mode flipped the unified list on', () => {
    const test = harness(false)

    $workspaceMode.set('bots')
    // The SDK's bot-chat open (keepAllProfilesScope) flips the unified list.
    test.write(true)

    $workspaceMode.set('sessions')

    expect(test.scope()).toBe(false)
    expect(test.write).toHaveBeenLastCalledWith(false)
  })

  it('keeps the unified list when that is what the user already had', () => {
    const test = harness(true)

    $workspaceMode.set('bots')
    $workspaceMode.set('sessions')

    expect(test.scope()).toBe(true)
  })

  it('captures the scope once, so a second bot selection cannot restore the flip', () => {
    const test = harness(false)

    $workspaceMode.set('bots')
    test.write(true)
    // Bot Mode re-publishes `bots` for every newly selected bot; the capture
    // must already be spent, or exit would restore our own `true`.
    $workspaceMode.set('bots')

    $workspaceMode.set('sessions')

    expect(test.scope()).toBe(false)
  })

  it('leaves the scope alone when Bot Mode was never entered', () => {
    const test = harness(false)

    $workspaceMode.set('sessions')

    expect(test.write).not.toHaveBeenCalled()
    expect(test.scope()).toBe(false)
  })

  it('stops listening on cleanup', () => {
    const test = harness(false)

    test.restore()

    $workspaceMode.set('bots')
    $workspaceMode.set('sessions')

    expect(test.write).not.toHaveBeenCalled()
  })

  it('drives the real sidebar scope store, not just the injected harness', () => {
    const restore = restoreSessionsScopeAfterBots()

    $workspaceMode.set('bots')
    setShowAllProfiles(true)
    $workspaceMode.set('sessions')

    expect($showAllProfiles.get()).toBe(false)
    restore()
  })
})
