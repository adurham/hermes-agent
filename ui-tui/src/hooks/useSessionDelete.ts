import type { SessionListRow } from '@hermes/shared/gateway-events'
import { useCallback, useRef, useState } from 'react'

import { mutateCanonical } from '../app/slash/canonicalSessionControls.js'
import type { GatewayClient } from '../gatewayClient.js'
import type { SessionDeleteResponse } from '../gatewayTypes.js'
import { messages } from '../i18n/runtime.js'
import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'

/** The picker's history delete: one request at a time, and a refused delete hands back the viewer
 *  attachment its revision read made. Settles through `onDeleted` or `onError`. */
export function useSessionDelete(
  gw: GatewayClient,
  onDeleted: (id: string) => void,
  onError: (message: string) => void
) {
  const [deleting, setDeleting] = useState(false)
  // Set synchronously (one stdin chunk can carry the confirming key and a click before a re-render);
  // the state guard covers a settle until the updated list renders.
  const deletingRef = useRef(false)

  const settle = () => {
    deletingRef.current = false
    setDeleting(false)
  }

  const performDelete = useCallback(
    (target: SessionListRow) => {
      if (deletingRef.current) {
        return
      }

      deletingRef.current = true
      setDeleting(true)
      onError('')
      // The shared gateway has no `session.delete`: its delete is the revision-fenced
      // `session.mutate operation=delete` (the whole lineage), as the classic CLI sends it.
      let readSubscription: string | undefined

      const deleted = gw.isCanonical
        ? mutateCanonical(gw, target.id, 'delete', {}, undefined, snapshot => {
            readSubscription = snapshot.subscription_id
          }).then(mutation => Boolean(mutation?.result))
        : gw
            .request<SessionDeleteResponse>('session.delete', { session_id: target.id })
            .then(raw => asRpcResult<SessionDeleteResponse>(raw)?.deleted === target.id)

      deleted
        .then(ok => {
          if (ok) {
            onDeleted(target.id)
          } else {
            onError(messages().pickers.common.invalidResponse(gw.isCanonical ? 'session.mutate' : 'session.delete'))
          }

          settle()
        })
        .catch((e: unknown) => {
          // The revision read attached this view to a session it was not showing; a refused
          // delete hands it back closed so it does not reappear in the live section.
          if (readSubscription) {
            void gw
              .request('session.detach', {
                session_id: target.id,
                subscription_id: readSubscription,
                end_reason: 'tui_close'
              })
              .catch(() => undefined)
          }

          onError(rpcErrorMessage(e))
          settle()
        })
    },
    [gw, onDeleted, onError]
  )

  const pending = useCallback(() => deleting || deletingRef.current, [deleting])

  return { deletePending: pending, deleting, performDelete }
}
