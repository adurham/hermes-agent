"""The classic CLI's ``/queue`` over the gateway's durable FIFO (refs #134889, N5).

On main a prompt queued while a turn ran sat in the CLI's ``_pending_input``: no
``session_admissions`` row, so an owner restart or a disconnect lost it. Over the session authority
``/queue <prompt>`` typed while a turn runs is an ordinary queued admission (``prompt.submit
{queued: true}``, the same path Ink and Desktop queue busy input on): the owner commits it at accept,
keeps it across a restart and runs it once, in FIFO order, after the current turn.

The CLI keeps no copy to resend. It remembers only which admissions it queued
(``SessionState.queued_inputs``) so the turn the owner runs for one is painted like a typed turn
(``gateway_tools_notify.queue_admitted_turn`` adopts it), and ``/queue list|edit|rm|clear`` read and
change the owner's queue (``prompt.receipt`` / ``prompt.replace_queued`` / ``prompt.cancel``).
"""
from __future__ import annotations

import logging
import uuid

from hermes_cli.gateway_client import GatewayClientError

logger = logging.getLogger(__name__)


def _state(cli, store):
    return store.connection.state(cli.session_id)


def submit_queued(cli, store, text: str) -> str:
    """Admit ``text`` behind the running turn in the owner's FIFO; returns its admission id."""
    receipt = store.call("prompt.submit", session_id=cli.session_id, submission_id=uuid.uuid4().hex,
                         text=text, queued=True)
    admission = (receipt or {}).get("admission_id")
    if not admission:
        raise GatewayClientError("the gateway did not admit the queued prompt")
    state = _state(cli, store)
    state.queued_inputs[admission] = text
    state.queued_at[admission] = state.pending_seq
    return admission


def queued_rows(cli, store) -> list[tuple[str, str]]:
    """``[(admission_id, text)]`` still queued in the owner's FIFO, in run order.

    This viewer's own admissions are read back with ``prompt.receipt`` (one it finds started or settled
    is left for the idle tick to paint); other viewers' queued rows come from the last pending list the owner published.
    """
    state = _state(cli, store)
    rows: dict[str, tuple[int, str]] = {}
    for row in state.pending or ():
        if isinstance(row, dict) and row.get("status") == "queued" and row.get("admission_id"):
            rows[row["admission_id"]] = (int(row.get("sequence") or 0), str(row.get("text") or ""))
    for admission, text in list(state.queued_inputs.items()):
        try:
            receipt = store.call("prompt.receipt", session_id=cli.session_id, admission_id=admission)
        except GatewayClientError as exc:
            logger.debug("prompt.receipt %s failed: %s", admission, exc)
            rows.setdefault(admission, (1 << 62, text))  # unread: still shown, never dropped
            continue
        if receipt.get("status") == "queued":
            rows[admission] = (int(receipt.get("sequence") or 0), text)
        else:
            # Started or settled: no longer queued, but its turn may not be painted yet. The idle tick
            # paints it (``gateway_tools_notify.recover_queued``); listing never erases that.
            rows.pop(admission, None)
            state.missed.add(admission)
    return [(admission, text) for admission, (_, text) in sorted(rows.items(), key=lambda kv: kv[1][0])]


def published_queue(cli, store) -> list[str]:
    """The texts the owner last published as queued, plus this viewer's admissions submitted after that
    list (no RPC: the dock repaints on every tick).

    The owner's published list is authoritative: an admission it no longer lists (cancelled, replaced or
    steered by another client, or started) is not shown as queued, even while ``queued_inputs`` still
    owes its turn a paint."""
    state = _state(cli, store)
    for gone in [admission for admission in state.queued_at if admission not in state.queued_inputs]:
        state.queued_at.pop(gone, None)
    rows = [row for row in state.pending or () if isinstance(row, dict)]
    known = {row.get("admission_id") for row in rows}
    fresh = [text for admission, text in state.queued_inputs.items()
             if admission not in known and state.queued_at.get(admission, -1) >= state.pending_seq]
    return [str(row.get("text") or "") for row in rows if row.get("status") == "queued"] + fresh


def cancel(cli, store, admission: str) -> bool:
    """Retire one queued admission; True when it no longer runs."""
    receipt = store.call("prompt.cancel", session_id=cli.session_id, admission_id=admission)
    _state(cli, store).queued_inputs.pop(admission, None)
    return (receipt or {}).get("status") == "terminal"


def replace(cli, store, admission: str, text: str) -> str | None:
    """``prompt.replace_queued``: the edited prompt is a new admission at the back of the FIFO."""
    receipt = store.call("prompt.replace_queued", session_id=cli.session_id, admission_id=admission,
                         submission_id=uuid.uuid4().hex, text=text)
    state = _state(cli, store)
    state.queued_inputs.pop(admission, None)
    new = (receipt or {}).get("admission_id")
    if new:
        state.queued_inputs[new] = text
        state.queued_at[new] = state.pending_seq
    return new
