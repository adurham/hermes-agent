import { useState, type ReactElement } from 'react'

import { Button } from '@/components/ui/button'
import { DialogDescription, DialogTitle } from '@/components/ui/dialog'
import type { DesktopUpdateStatus } from '@/global'
import { useI18n } from '@/i18n'
import { applyRetirement } from '@/store/updates'

export function RetirementView({ retirement, onLater, onRetry }: {
  retirement: NonNullable<DesktopUpdateStatus['retirement']>
  onLater: () => void
  onRetry: () => void
}): ReactElement {
  const { t } = useI18n()
  const [consented, setConsented] = useState<boolean>(false)
  const available: boolean = retirement.state === 'available' || retirement.state === 'cleanup-pending'
  return (
    <div className="space-y-4 p-6">
      <DialogTitle>{t.updates.retirementTitle}</DialogTitle>
      <DialogDescription>{t.updates.retirementBody}</DialogDescription>
      <p className="text-sm font-medium">{retirement.destination} · {retirement.version}</p>
      {retirement.message && <p role="status" className="text-sm text-muted-foreground">{retirement.message}</p>}
      {available ? (
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={consented} onChange={event => setConsented(event.currentTarget.checked)} />
          {t.updates.retirementConsent}
        </label>
      ) : <p className="text-sm">{retirement.state === 'complete' ? t.updates.retirementComplete : t.updates.retirementBlocked}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="text" onClick={onLater}>{t.updates.maybeLater}</Button>
        {available
          ? <Button disabled={!consented} onClick={() => void applyRetirement()}>{t.updates.retirementAction}</Button>
          : <Button onClick={onRetry}>{t.updates.tryAgain}</Button>}
      </div>
    </div>
  )
}
