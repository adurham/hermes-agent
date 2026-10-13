import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { sessionParkedCardWaiting } from '@/store/parked-card-answers'

/** Typed text answers a waiting card through Send; an empty draft still offers Stop, card or not. */
export function useCardWaitingSubmit(sessionId: null | string, busy: boolean, hasPayload: boolean) {
  const cardWaiting = useStore(useMemo(() => sessionParkedCardWaiting(sessionId), [sessionId]))

  return { canSubmit: busy || hasPayload, cardWaiting }
}
