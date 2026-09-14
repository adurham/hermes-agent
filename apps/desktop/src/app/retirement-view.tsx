import { type ReactElement, useState } from 'react'

import { Button } from '@/components/ui/button'
import { DialogDescription, DialogTitle } from '@/components/ui/dialog'
import type { DesktopRetirementConsent, DesktopUpdateStatus } from '@/global'
import { useI18n } from '@/i18n'
import { applyRetirement } from '@/store/updates'

export function RetirementView({
  retirement,
  onLater,
  onRetry
}: {
  retirement: NonNullable<DesktopUpdateStatus['retirement']>
  onLater: () => void
  onRetry: () => void
}): ReactElement {
  const { t } = useI18n()
  const [consented, setConsented] = useState<boolean>(false)
  const [workspaceChoice, setWorkspaceChoice] = useState<DesktopRetirementConsent['workspaceChoice'] | null>(null)
  const available: boolean = retirement.state === 'available' || retirement.state === 'cleanup-pending'

  return (
    <div className="space-y-4 p-6">
      <DialogTitle>{t.updates.retirementTitle}</DialogTitle>
      <DialogDescription>{t.updates.retirementBody}</DialogDescription>
      <p className="text-sm font-medium">
        {retirement.destination} · {retirement.version}
      </p>
      {retirement.message && (
        <p className="text-sm text-muted-foreground" role="status">
          {retirement.message}
        </p>
      )}
      {available ? (
        <div className="space-y-3 text-sm">
          <label className="flex items-start gap-2">
            <input
              checked={workspaceChoice === 'keep-stable'}
              name="retirement-workspace"
              onChange={() => setWorkspaceChoice('keep-stable')}
              type="radio"
            />
            {t.updates.retirementKeepStable}
          </label>
          <label className="flex items-start gap-2">
            <input
              checked={workspaceChoice === 'open-preview'}
              name="retirement-workspace"
              onChange={() => setWorkspaceChoice('open-preview')}
              type="radio"
            />
            {t.updates.retirementOpenPreview}
          </label>
          <label className="flex items-start gap-2">
            <input checked={consented} onChange={event => setConsented(event.currentTarget.checked)} type="checkbox" />
            {t.updates.retirementConsent}
          </label>
        </div>
      ) : (
        <p className="text-sm">
          {retirement.state === 'complete' ? t.updates.retirementComplete : t.updates.retirementBlocked}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button onClick={onLater} variant="text">
          {t.updates.maybeLater}
        </Button>
        {available ? (
          <Button
            disabled={!consented || !workspaceChoice}
            onClick={() => {
              if (workspaceChoice) {
                void applyRetirement(workspaceChoice)
              }
            }}
          >
            {t.updates.retirementAction}
          </Button>
        ) : (
          <Button onClick={onRetry}>{t.updates.tryAgain}</Button>
        )}
      </div>
    </div>
  )
}
