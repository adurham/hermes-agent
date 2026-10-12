// The canonical owner names a session by its durable id (`local-` + 64 hex). The banner shows
// main's short 8-character label; the full id stays in state for every RPC.
const CANONICAL_LOCAL_ID = /^local-([0-9a-f]{8})[0-9a-f]{56}$/

export const sessionLabel = (sid: null | string | undefined): null | string | undefined =>
  sid ? (CANONICAL_LOCAL_ID.exec(sid)?.[1] ?? sid) : sid
