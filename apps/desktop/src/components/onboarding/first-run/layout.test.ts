import { beforeEach, describe, expect, it, vi } from 'vitest'

import { group } from '@/components/pane-shell/tree/model'
import { applyLayoutPreset } from '@/components/pane-shell/tree/presets'
import { $activePresetId } from '@/components/pane-shell/tree/store'
import { $chatOnboardingSolo, $introView } from '@/store/onboarding-intro'

import { openDemoLayout } from './assembly'
import { openIntro, skipIntro } from './intro'

const size = vi.fn()

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
  it('grows the window to normal size on the picked layout when a layout is applied', () => {
    applyLayoutPreset('picked', group(['workspace']))

    expect(size).toHaveBeenCalledWith('normal')
    expect($chatOnboardingSolo.get()).toBe(false)
    expect($introView.get()).toBe('ended')
    expect($activePresetId.get()).toBe('picked')
  })

  it('sizes the window normal on Skip setup', () => {
    skipIntro()

    expect(size).toHaveBeenCalledWith('normal')
    expect($chatOnboardingSolo.get()).toBe(false)
  })
})
