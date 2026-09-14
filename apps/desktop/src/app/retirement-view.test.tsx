import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { Dialog, DialogContent } from '@/components/ui/dialog'
import type { DesktopUpdateStatus } from '@/global'
import { I18nProvider } from '@/i18n'
import { en } from '@/i18n/en'
import { $updateStatus, resetUpdateApplyState } from '@/store/updates'

import { RetirementView } from './retirement-view'

afterEach((): void => {
  cleanup()
  resetUpdateApplyState()
  $updateStatus.set(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
})

test.each(['keep-stable', 'open-preview'] as const)(
  'migration requires a workspace choice and forwards %s through the real store',
  async (choice): Promise<void> => {
    const retire = vi.fn().mockResolvedValue({ ok: true, handedOff: true })
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { updates: { retire } } })

    const retirement: NonNullable<DesktopUpdateStatus['retirement']> = {
      state: 'available',
      destination: 'stable',
      version: '1.0.0'
    }

    $updateStatus.set({ supported: true, retirement })
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <Dialog open>
          <DialogContent>
            <RetirementView onLater={(): void => {}} onRetry={(): void => {}} retirement={retirement} />
          </DialogContent>
        </Dialog>
      </I18nProvider>
    )
    const action: HTMLElement = screen.getByRole('button', { name: en.updates.retirementAction })
    fireEvent.click(screen.getByRole('checkbox'))
    expect(action.hasAttribute('disabled')).toBe(true)
    fireEvent.click(
      screen.getByRole('radio', {
        name: choice === 'keep-stable' ? 'Keep stable’s existing workspace' : 'Open this preview workspace in stable'
      })
    )
    fireEvent.click(action)
    await waitFor((): void => {
      expect(retire).toHaveBeenCalledWith({ installStable: true, removePreview: true, workspaceChoice: choice })
    })
  }
)
