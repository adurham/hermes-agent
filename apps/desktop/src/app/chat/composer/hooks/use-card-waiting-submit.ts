import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { sessionParkedCardWaiting } from '@/store/parked-card-answers'

/** A waiting card turns the stop button into send, which has nothing to send while the draft is empty. */
export function useCardWaitingSubmit(sessionId: null | string, busy: boolean, hasPayload: boolean) {
  const cardWaiting = useStore(useMemo(() => sessionParkedCardWaiting(sessionId), [sessionId]))

  return { canSubmit: (busy && !cardWaiting) || hasPayload, cardWaiting }
}
