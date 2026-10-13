import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { registerLayoutPresets } from '@/app/contrib/layout-presets'
import { group } from '@/components/pane-shell/tree/model'
import { applyLayoutPreset } from '@/components/pane-shell/tree/presets'
import { $activePresetId } from '@/components/pane-shell/tree/store'
import { applyAppearance } from '@/store/appearance-apply'
import { $interfaceMode, setInterfaceMode } from '@/store/interface-mode'
import { $sidebarOpen, setSidebarOpen } from '@/store/layout'
import { $chatOnboardingSolo, $introView } from '@/store/onboarding-intro'

import { openDemoLayout, snapshotChatLayout } from './assembly'
import { openIntro, skipIntro } from './intro'

const size = vi.fn()

beforeAll(() => {
  registerLayoutPresets()
})

beforeEach(() => {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { chatOnboarding: { size }, guestOnboardingEnabled: true }
  })
  $introView.set('off')
  openDemoLayout()
  openIntro(false)
  size.mockClear()
})

describe('first-run layout beat', () => {
  it('keeps the intro through a preview and grows the window when a layout is committed', () => {
    setInterfaceMode('advanced')
    applyLayoutPreset('picked', group(['workspace']))
    expect($introView.get()).toBe('intro')
    expect(size).not.toHaveBeenCalled()

    expect(applyAppearance({ layout: 'sidebar-left' }).success).toBe(true)

    expect(size).toHaveBeenCalledWith('normal')
    expect($chatOnboardingSolo.get()).toBe(false)
    expect($introView.get()).toBe('ended')
    expect($interfaceMode.get()).toBe('simple')
    expect($activePresetId.get()).toBe('sidebar-left')
  })

  it('keeps the intro while a preview opens the sidebar itself', () => {
    const undo = snapshotChatLayout()
    setSidebarOpen(!$sidebarOpen.get())
    undo.revert()

    expect($introView.get()).toBe('intro')
  })

  it('saves a previewed layout only when Continue keeps it', () => {
    skipIntro()
    const writes = vi.spyOn(window.localStorage, 'setItem')

    const cancelled = snapshotChatLayout()
    applyLayoutPreset('picked', group(['workspace']))
    cancelled.revert()
    expect(writes).not.toHaveBeenCalled()

    const kept = snapshotChatLayout()
    applyLayoutPreset('picked', group(['workspace']))
    kept.keep()
    expect(writes).toHaveBeenCalled()
    writes.mockRestore()
  })

  it('sizes the window normal on Skip setup', () => {
    skipIntro()

    expect(size).toHaveBeenCalledWith('normal')
    expect($chatOnboardingSolo.get()).toBe(false)
  })
})
