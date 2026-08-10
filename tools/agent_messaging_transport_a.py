"""Transport A — in-process local agent messaging.

Design: ``docs/design/local-agent-messaging.md`` (APPROVED). This module
implements ONLY the in-process (same-Python-process) half of local agent
messaging: a top-level session talking to its own ``delegate_task``
subagent tree, and a ``background=true`` subagent talking back to its
parent. Transport B (cross-process, ``state.db``-backed) is a separate
module registering its own lookup against the same shared contract.

Everything shared between the two transports — the participant model, the
``resolve_transport`` fan-out seam, the mandatory untrusted-content
marker, the size caps, the delivery-outcome types, the tool names — lives
in ``tools/agent_messaging_contract.py`` and is imported, never
redefined, here.

Two properties of this module are correctness requirements, not
implementation details:

1. **Every delivered message is marker-wrapped.** Both delivery branches
   (active recipient via ``steer()``, idle recipient via the
   ``_pending_input`` next-turn injection) go through
   ``_deliver_marked()``, which is the ONLY function in this module that
   touches a recipient's transcript, and which constructs its payload
   exclusively via ``build_agent_message_marker()``. The idle branch must
   never inject a bare body — that would be indistinguishable from
   operator-authored input (design doc: "Blocker caught by the final
   sign-off review").
2. **Cap enforcement is atomic.** The coalesced-pending-cap check and the
   append happen inside ONE lock acquisition, never as a check-then-
   separately-locked-append (design doc: "Cap-locking" decision).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from tools.agent_messaging_contract import (
    COALESCED_PENDING_CAP_BYTES,
    DeliveryOutcome,
    MessageTooLargeError,
    Participant,
    ParticipantKind,
    RecipientQueueFullError,
    SendResult,
    SessionOrigin,
    TransportKind,
    build_agent_message_marker,
    check_message_size,
    register_transport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process participant registry
# ---------------------------------------------------------------------------
# Subagents are NOT tracked here — they already live in delegate_tool's
# ``_active_subagents`` dict, which is the existing, already-thread-locked
# in-process registry the design doc says to extend rather than duplicate.
# This registry holds only top-level *session* participants (the delivery
# target for a background subagent messaging its parent), which have no
# equivalent existing home.

_session_lock = threading.Lock()
_session_participants: Dict[str, Dict[str, Any]] = {}

# Fallback lock for agent stubs built via ``object.__new__`` that never ran
# ``AIAgent.__init__`` and therefore have no ``_pending_steer_lock``. Mirrors
# the same defensive branch ``AIAgent.steer()`` itself already carries.
_FALLBACK_STEER_LOCK = threading.Lock()


def register_session_participant(
    participant: Participant,
    *,
    agent: Any = None,
    cli: Any = None,
) -> None:
    """Register a top-level session as an addressable in-process participant.

    ``agent`` is the session's ``AIAgent``; ``cli`` is the object owning the
    ``_agent_running`` flag and ``_pending_input`` queue (the CLI session).
    Either may be None in tests or headless hosts — delivery degrades to
    whichever branch is actually available.

    Registration is ADDITIVE and idempotent. ``cli.py`` reassigns
    ``self.session_id`` in several places (resume, rename-triggered
    compression-tip switch, ...), and a background subagent spawned before
    such a switch still carries the OLD id in its ``owner_session_id``. So a
    session registers under every id it has ever held and old aliases are
    never removed during the process lifetime — unregistering one mid-flight
    would break exactly the ``send_to_parent`` path this registry exists to
    serve.

    Re-registering an existing id refreshes the stored ``agent``/``cli``
    references (they are rebuilt on e.g. credential-churn agent reinit) while
    PRESERVING ``pending_bytes``. Resetting that counter would silently defeat
    the coalesced-cap accounting in ``_append_idle_atomically``. A None
    ``agent``/``cli`` never clobbers an already-known reference, so a caller
    that only has one of the two (the delegate spawn path) can register
    safely.
    """
    if participant.kind is not ParticipantKind.SESSION:
        raise ValueError("register_session_participant expects a SESSION participant")
    with _session_lock:
        existing = _session_participants.get(participant.participant_id)
        if existing is None:
            _session_participants[participant.participant_id] = {
                "participant": participant,
                "agent": agent,
                "cli": cli,
                "pending_bytes": 0,
            }
            return
        existing["participant"] = participant
        if agent is not None:
            existing["agent"] = agent
        if cli is not None:
            existing["cli"] = cli


def unregister_session_participant(participant_id: str) -> None:
    with _session_lock:
        _session_participants.pop(participant_id, None)


def _reset_for_tests() -> None:
    """Test-only: drop all registered session participants."""
    with _session_lock:
        _session_participants.clear()


# ---------------------------------------------------------------------------
# Transport resolution (registered into the shared fan-out seam)
# ---------------------------------------------------------------------------


def _subagent_record(recipient_id: str) -> Optional[Dict[str, Any]]:
    """Look up a live subagent record in delegate_tool's existing registry.

    Imported lazily: ``delegate_tool`` imports plenty of heavy machinery and
    this module is imported at tool-registration time.
    """
    try:
        from tools.delegate_tool import _active_subagents, _active_subagents_lock
    except Exception:  # pragma: no cover - import cycle / partial init
        return None
    with _active_subagents_lock:
        return _active_subagents.get(recipient_id)


def in_process_lookup(sender: Participant, recipient_id: str) -> Optional[Participant]:
    """Resolve ``recipient_id`` to an in-process participant, or None.

    Contract (``register_transport_lookup``): returns None — never raises —
    for anything it does not recognise, so ``resolve_transport``'s fan-out
    falls through to Transport B.

    In-process means "shares the sender's ``owner_session_id``". A subagent
    belonging to a different session's tree is not reachable in-process even
    though its record happens to live in this process's registry, because
    ``_active_subagents`` is process-global while ownership is per-session.
    """
    try:
        if not recipient_id:
            return None

        record = _subagent_record(recipient_id)
        if record is not None:
            owner = record.get("owner_session_id") or sender.owner_session_id
            if owner != sender.owner_session_id:
                return None
            return Participant(
                participant_id=recipient_id,
                kind=ParticipantKind.SUBAGENT,
                owner_session_id=owner,
                parent_participant_id=record.get("parent_id"),
            )

        with _session_lock:
            entry = _session_participants.get(recipient_id)
            participant = entry.get("participant") if entry else None
        if participant is None:
            return None
        if participant.owner_session_id != sender.owner_session_id:
            return None
        return participant
    except Exception:  # pragma: no cover - lookup must never raise
        logger.debug("in_process_lookup(%s) failed", recipient_id, exc_info=True)
        return None


def _in_process_send(
    sender: Participant, recipient: Participant, body: str
) -> SendResult:
    """``TransportSendFn`` adapter for Transport A.

    A thin shim over ``send_in_process`` — this transport's send semantics
    need no translation, it only needs to be *discoverable* through the
    contract's registry instead of being hardcoded in the tool layer.
    ``send_in_process`` already raises only ``MessageTooLargeError`` /
    ``RecipientQueueFullError``, which is exactly the seam's exception
    contract, so nothing is caught or rewrapped here.
    """
    return send_in_process(sender=sender, recipient=recipient, body=body)


register_transport(TransportKind.IN_PROCESS, in_process_lookup, _in_process_send)


# ---------------------------------------------------------------------------
# Delivery — the ONLY path that touches a recipient's transcript
# ---------------------------------------------------------------------------


def _append_steer_atomically(agent: Any, marked: str) -> None:
    """Cap-check and append to ``agent._pending_steer`` in ONE critical section.

    This deliberately does not call ``AIAgent.steer()``: steer() acquires the
    lock itself, so a cap check outside it would be the exact check-then-act
    TOCTOU race the design's cap-locking decision rejects. Instead we take
    the same lock steer() uses and do the read-check-write inside it, which
    also keeps the human ``/steer`` UX free of this feature's size limits.
    """
    lock = getattr(agent, "_pending_steer_lock", None) or _FALLBACK_STEER_LOCK
    with lock:
        existing = getattr(agent, "_pending_steer", None) or ""
        combined = (existing + "\n" + marked) if existing else marked
        if len(combined.encode("utf-8")) > COALESCED_PENDING_CAP_BYTES:
            raise RecipientQueueFullError(
                f"recipient has {len(existing.encode('utf-8'))} unread bytes pending; "
                f"delivering this message would exceed the "
                f"{COALESCED_PENDING_CAP_BYTES}-byte coalesced cap — retry after it "
                f"reaches its next tool-batch boundary."
            )
        agent._pending_steer = combined


def _append_idle_atomically(entry: Dict[str, Any], marked: str) -> None:
    """Cap-check and enqueue onto the idle recipient's ``_pending_input``.

    Same atomicity requirement as the active branch: the byte accounting and
    the ``put()`` happen under one acquisition of ``_session_lock``.
    """
    cli = entry.get("cli")
    pending_input = getattr(cli, "_pending_input", None)
    if pending_input is None:
        raise RecipientQueueFullError(
            "recipient session is idle but exposes no _pending_input queue; "
            "cannot deliver."
        )
    size = len(marked.encode("utf-8"))
    with _session_lock:
        current = int(entry.get("pending_bytes", 0) or 0)
        if current + size > COALESCED_PENDING_CAP_BYTES:
            raise RecipientQueueFullError(
                f"recipient has {current} unread bytes pending; delivering this "
                f"message would exceed the {COALESCED_PENDING_CAP_BYTES}-byte "
                f"coalesced cap — retry after it processes its queued input."
            )
        pending_input.put(marked)
        entry["pending_bytes"] = current + size


def note_idle_delivery_consumed(participant_id: str, nbytes: int) -> None:
    """Release byte accounting once an idle-injected message is consumed."""
    with _session_lock:
        entry = _session_participants.get(participant_id)
        if entry is None:
            return
        entry["pending_bytes"] = max(0, int(entry.get("pending_bytes", 0) or 0) - nbytes)


def _recipient_is_active(entry: Dict[str, Any]) -> bool:
    """Is the recipient session's own turn currently running?

    Mirrors ``cli.py``'s ``/steer`` command (``canonical == "steer"``), which
    branches on ``self._agent_running`` for exactly this reason: calling
    ``steer()`` while no turn is running stashes text nothing will ever
    drain.
    """
    cli = entry.get("cli")
    return bool(getattr(cli, "_agent_running", False)) and entry.get("agent") is not None


def _deliver_marked(
    *,
    sender: Participant,
    recipient: Participant,
    body: str,
) -> SendResult:
    """Wrap and deliver. The single choke point for BOTH delivery branches.

    Every message Transport A delivers passes through here, and the marker is
    built before either branch is selected — there is no code path in this
    module that can put an unwrapped body in front of a recipient.
    """
    marked = build_agent_message_marker(
        sender_participant_id=sender.participant_id,
        sender_kind=sender.kind,
        sender_origin=sender.session_origin,
        body=body,
    )

    if recipient.kind is ParticipantKind.SUBAGENT:
        record = _subagent_record(recipient.participant_id)
        agent = record.get("agent") if record else None
        if agent is None:
            return SendResult(
                outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
                detail=_not_found_detail(recipient.participant_id),
            )
        _append_steer_atomically(agent, marked)
        return SendResult(
            outcome=DeliveryOutcome.QUEUED_EPHEMERAL,
            detail=(
                f"queued for subagent {recipient.participant_id}; it arrives at that "
                f"subagent's next tool-batch boundary."
            ),
        )

    with _session_lock:
        entry = _session_participants.get(recipient.participant_id)
    if entry is None:
        return SendResult(
            outcome=DeliveryOutcome.RECIPIENT_NOT_FOUND,
            detail=_not_found_detail(recipient.participant_id),
        )

    if _recipient_is_active(entry):
        _append_steer_atomically(entry["agent"], marked)
        detail = (
            f"queued for session {recipient.participant_id} (mid-turn); it arrives "
            f"at that session's next tool-batch boundary."
        )
    else:
        _append_idle_atomically(entry, marked)
        detail = (
            f"queued for session {recipient.participant_id} (idle); it starts a "
            f"fresh turn there."
        )
    return SendResult(outcome=DeliveryOutcome.QUEUED_EPHEMERAL, detail=detail)


def _not_found_detail(recipient_id: str) -> str:
    """Not-found error text, including the currently-valid targets.

    Design doc Question 2 resolution: include the live subagent IDs + goals so
    a mistyped or hallucinated ID gives the model a self-correcting feedback
    loop rather than a dead end.
    """
    base = (
        f"no active session or subagent matches '{recipient_id}' — it may have "
        f"already exited, or the ID may be incorrect."
    )
    try:
        from tools.delegate_tool import list_active_subagents

        live = list_active_subagents()
    except Exception:  # pragma: no cover
        live = []
    if not live:
        return base + " There are no active subagents right now."
    listed = ", ".join(
        f"{r.get('subagent_id')} (goal: {str(r.get('goal') or '')[:60]})" for r in live
    )
    return base + f" Currently active subagents: {listed}."


def send_in_process(
    *,
    sender: Participant,
    recipient: Participant,
    body: str,
) -> SendResult:
    """Public Transport A send entry point.

    Raises ``MessageTooLargeError``/``RecipientQueueFullError`` for the caller
    (the messaging tool) to surface as a synchronous tool error — never
    truncates, never silently drops.
    """
    check_message_size(body)
    return _deliver_marked(sender=sender, recipient=recipient, body=body)


__all__ = [
    "MessageTooLargeError",
    "RecipientQueueFullError",
    "SessionOrigin",
    "in_process_lookup",
    "note_idle_delivery_consumed",
    "register_session_participant",
    "send_in_process",
    "unregister_session_participant",
]
