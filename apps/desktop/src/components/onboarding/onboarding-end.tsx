import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { SETTINGS_ROUTE } from '@/app/routes'
import { appIsQuiet, SETTLE_MIN_MS, SETTLE_SPREAD_MS, TICK_MS } from '@/components/tips/use-tip-rotation'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  preventCloseButtonAutoFocus
} from '@/components/ui/dialog'
import { useI18n } from '@/i18n'
import { readKey, writeKey } from '@/lib/storage'
import { activeGatewayConnectionId } from '@/store/gateway'
import { readLocalSetupEligibility } from '@/store/local-setup-offer'
import { $onboardingSettleFrom } from '@/store/onboarding-run'
import { requestRoute } from '@/store/recovery-requests'
import type { LocalCatalogModel } from '@/types/hermes'

/** Install-level: the local-model offer opens at most once per install, and a dismiss is final. */
export const LOCAL_MODEL_OFFERED_KEY = 'hermes.desktop.firstRun.localModelOffered.v1'
/** When the offer's settle wait started, so a restart before it opens resumes the wait. */
const LOCAL_MODEL_PENDING_KEY = 'hermes.desktop.firstRun.localModelPendingFrom.v1'

function settleStart(settleFrom: null | number): null | number {
  if (settleFrom !== null) {
    writeKey(LOCAL_MODEL_PENDING_KEY, readKey(LOCAL_MODEL_PENDING_KEY) ?? String(settleFrom))
  }

  const pending = Number(readKey(LOCAL_MODEL_PENDING_KEY))

  return Number.isFinite(pending) && pending > 0 ? pending : null
}

/**
 * The local-model offer after first-run onboarding: armed when a run ends or the user first leaves
 * the onboarding chat, then shown at the first quiet moment once a settle window has passed.
 */
export function OnboardingEnd() {
  const { t } = useI18n()
  const settleFrom = useStore($onboardingSettleFrom)
  const [offer, setOffer] = useState<LocalCatalogModel | null>(null)

  useEffect(() => {
    const from = settleStart(settleFrom)

    if (from === null || readKey(LOCAL_MODEL_OFFERED_KEY)) {
      return
    }

    const settledAt = from + SETTLE_MIN_MS + Math.random() * SETTLE_SPREAD_MS
    let lastTypedAt = 0
    let checking = false
    let cancelled = false

    const noteTyping = () => {
      lastTypedAt = Date.now()
    }

    const tick = async () => {
      if (checking || Date.now() < settledAt || readKey(LOCAL_MODEL_OFFERED_KEY) || !appIsQuiet(lastTypedAt)) {
        return
      }

      checking = true
      const connection = activeGatewayConnectionId()
      const { fit } = await readLocalSetupEligibility()
      checking = false

      // The read is async: the moment may have stopped being quiet, or the window moved to another
      // connection, while it ran.
      const stale = cancelled || activeGatewayConnectionId() !== connection

      if (stale || !fit || readKey(LOCAL_MODEL_OFFERED_KEY) || !appIsQuiet(lastTypedAt)) {
        return
      }

      writeKey(LOCAL_MODEL_OFFERED_KEY, '1')
      window.clearInterval(timer)
      setOffer(fit.model)
    }

    const timer = window.setInterval(() => void tick(), TICK_MS)
    window.addEventListener('keydown', noteTyping, true)

    return () => {
      cancelled = true
      window.clearInterval(timer)
      window.removeEventListener('keydown', noteTyping, true)
    }
  }, [settleFrom])

  const copy = t.firstRun.localModel

  return (
    <Dialog onOpenChange={open => !open && setOffer(null)} open={offer !== null}>
      {offer && (
        <DialogContent onOpenAutoFocus={preventCloseButtonAutoFocus}>
          <DialogHeader>
            <DialogTitle>{copy.title}</DialogTitle>
            <DialogDescription>{copy.text(offer.display_name, offer.size_label)}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={() => setOffer(null)} size="sm" type="button" variant="text">
              {copy.notNow}
            </Button>
            <Button
              onClick={() => {
                setOffer(null)
                requestRoute(`${SETTINGS_ROUTE}?tab=providers&pview=local`)
              }}
              type="button"
            >
              {copy.setUp}
            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  )
}
