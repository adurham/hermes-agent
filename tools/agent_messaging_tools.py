"""Model-facing tools for local agent messaging (Transport A).

Design: ``docs/design/local-agent-messaging.md``.

Two distinctly-named tools, not one role-conditional schema (design doc:
"Tool schema bifurcation"):

- ``send_agent_message(recipient, body)`` — parent/session callers. Takes
  a recipient. NEVER given to a subagent in any mode.
- ``send_to_parent(body)`` — subagent callers. No recipient parameter; a
  subagent has exactly one valid target (Finding 7), so discovery is dead
  schema weight and ``list_agents`` is not given to subagents at all.

Gating, per the design's Question 4 resolution and the final sign-off's
wording fix:

- ``send_to_parent`` is in a subagent's toolset ONLY when the delegation
  is ``background=true``. A synchronous subagent gets neither tool — its
  parent's thread is blocked inside the batch polling loop and cannot act
  on anything until the batch returns.
- ``send_agent_message`` is not registered at all for gateway-origin
  sessions (design doc: "Sender-permission-mode threat model"), killing
  the untrusted-external-chat-user-as-sender injection path at the source.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from tools.agent_messaging_contract import (
    TOOL_NAME_SEND_AGENT_MESSAGE,
    TOOL_NAME_SEND_TO_PARENT,
    TOOLSET_NAME,
    DeliveryOutcome,
    MessageTooLargeError,
    Participant,
    ParticipantKind,
    RecipientQueueFullError,
    SendResult,
    SessionOrigin,
    TransportKind,
    resolve_transport,
)
# Imported for its import-time side effect: Transport A registers its lookup
# and send callable into the contract's transport registry at module import.
# This module is imported at tool-registration time, so this is what makes
# in-process recipients resolvable. Transport B registers itself separately,
# via cross_session_integration -> cross_session_transport.register_lookup().
import tools.agent_messaging_transport_a  # noqa: F401
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sender-permission gate
# ---------------------------------------------------------------------------


def session_origin() -> SessionOrigin:
    """Classify how this session was created.

    Uses the same ``HERMES_GATEWAY_SESSION`` env flag the codebase already
    uses to distinguish gateway sessions elsewhere (tools/approval.py,
    tools/terminal_tool.py, tools/cronjob_tools.py) rather than inventing a
    new origin concept for this feature.
    """
    from utils import env_var_enabled

    if env_var_enabled("HERMES_GATEWAY_SESSION"):
        return SessionOrigin.GATEWAY
    if env_var_enabled("HERMES_CRON_SESSION"):
        return SessionOrigin.CRON
    if env_var_enabled("HERMES_ACP_SESSION"):
        return SessionOrigin.ACP
    return SessionOrigin.CLI


def check_send_agent_message_available() -> bool:
    """check_fn for ``send_agent_message``: never registered for gateway origin.

    Per the design doc's unconditional decision, a gateway-origin session
    loses ``send_agent_message`` entirely — including the ability to reach
    its own in-process subagents. The gate is deliberately not conditional
    on whether the target is cross-process.

    What IS retained: a ``background=true`` subagent this gateway session
    delegates to still gets ``send_to_parent`` (a separate tool, ungated by
    session origin) to message back to it.
    """
    return session_origin() is not SessionOrigin.GATEWAY


def subagent_messaging_toolsets(*, background: bool) -> list:
    """Toolsets a delegated child should gain for messaging.

    Mode is the primary gate because it tracks actual capability — can the
    parent even receive right now — rather than a judgment call about
    whether a given role finds messaging useful.
    """
    return [TOOLSET_NAME] if background else []


# ---------------------------------------------------------------------------
# Caller identity
# ---------------------------------------------------------------------------


def _caller_participant(agent: Any) -> Participant:
    """Build the sending participant from the calling agent.

    A subagent carries ``_subagent_id``; anything else is a top-level
    session. Origin is stamped here, from the process environment — never
    self-reported by the sender — so it cannot be forged by a message body.
    """
    subagent_id = getattr(agent, "_subagent_id", None)
    if isinstance(subagent_id, str) and subagent_id:
        return Participant(
            participant_id=subagent_id,
            kind=ParticipantKind.SUBAGENT,
            owner_session_id=getattr(agent, "_delegate_owner_session_id", "") or "",
            parent_participant_id=getattr(agent, "_parent_subagent_id", None),
        )
    session_id = getattr(agent, "session_id", "") or ""
    return Participant(
        participant_id=session_id,
        kind=ParticipantKind.SESSION,
        owner_session_id=session_id,
        session_origin=session_origin(),
    )


def _format_result(result: SendResult) -> str:
    if result.outcome is DeliveryOutcome.RECIPIENT_NOT_FOUND:
        return tool_error(result.detail)
    if result.outcome is DeliveryOutcome.HELD:
        # A held message is NOT queued for delivery — it is queued for a
        # HUMAN, and is never delivered unless approved. Reusing the generic
        # "queued" note here would tell the model the opposite of the truth.
        note = (
            "Note: held means the recipient's inbound policy requires human "
            "approval before this is delivered at all — it may never be "
            "delivered if it is not approved."
        )
    else:
        note = (
            "Note: queued means accepted for delivery, not that the recipient "
            "has read or acted on it."
        )
    return f"{result.outcome.value}: {result.detail}\n{note}"


def _send(sender: Participant, recipient_id: str, body: str) -> str:
    if not (body or "").strip():
        return tool_error("message body is empty.")
    if not (recipient_id or "").strip():
        return tool_error("recipient is required.")

    resolution = resolve_transport(sender, recipient_id)
    if resolution.kind is TransportKind.NOT_FOUND or resolution.participant is None:
        from tools.agent_messaging_transport_a import _not_found_detail

        return tool_error(_not_found_detail(recipient_id))
    if resolution.send is None:
        # Defensive: a transport registered a lookup but no send callable, so
        # the recipient is discoverable but unreachable. Both shipped
        # transports register through register_transport(), which pairs them.
        logger.debug(
            "transport %s resolved '%s' but registered no send callable",
            resolution.kind.value,
            recipient_id,
        )
        return tool_error(
            f"'{recipient_id}' resolved to a transport with no send path "
            f"registered; the message was not sent."
        )

    try:
        result = resolution.send(sender, resolution.participant, body)
    except MessageTooLargeError as exc:
        return tool_error(str(exc))
    except RecipientQueueFullError as exc:
        return tool_error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("send_agent_message failed", exc_info=True)
        return tool_error(f"delivery failed: {exc}")
    return _format_result(result)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def send_agent_message(
    recipient: str = "", body: str = "", *, agent: Any = None, **_kw
) -> str:
    """Parent/session-side send. Requires an explicit recipient."""
    sender = _caller_participant(agent)
    if sender.kind is ParticipantKind.SUBAGENT:
        # Defense in depth — the toolset gate should already prevent this.
        return tool_error(
            "send_agent_message is not available to subagents; use "
            f"{TOOL_NAME_SEND_TO_PARENT} to reach your parent."
        )
    return _send(sender, recipient, body)


def send_to_parent(body: str = "", *, agent: Any = None, **_kw) -> str:
    """Subagent-side send. Target is implicitly the parent — no recipient."""
    sender = _caller_participant(agent)
    if sender.kind is not ParticipantKind.SUBAGENT:
        return tool_error(
            f"{TOOL_NAME_SEND_TO_PARENT} is only callable by a subagent."
        )
    parent_id = sender.parent_participant_id or sender.owner_session_id
    if not parent_id:
        return tool_error("this subagent has no addressable parent.")
    return _send(sender, parent_id, body)


SEND_AGENT_MESSAGE_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME_SEND_AGENT_MESSAGE,
    "description": (
        "Send a short coordination message to one of your own running "
        "subagents. Success means QUEUED, not delivered or acted on — the "
        "message arrives at the recipient's next tool-batch boundary, and a "
        "message that arrives too late can still bounce back to you as an "
        "unprocessed_messages note on that subagent's completion. This is a "
        "coordination channel, not a data-transfer channel: write large "
        "content to a file and send the path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": (
                    "The subagent_id to deliver to, as returned in "
                    "delegate_task's result for each spawned child."
                ),
            },
            "body": {"type": "string", "description": "The message text."},
        },
        "required": ["recipient", "body"],
    },
}

SEND_TO_PARENT_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME_SEND_TO_PARENT,
    "description": (
        "Send a short progress or coordination message to the parent that "
        "delegated this task to you. There is no recipient to choose — your "
        "parent is the only valid target. Success means QUEUED, not read. "
        "Use this for something your parent needs mid-task; anything that can "
        "wait belongs in your final summary instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "The message text."},
        },
        "required": ["body"],
    },
}


registry.register(
    name=TOOL_NAME_SEND_AGENT_MESSAGE,
    toolset=TOOLSET_NAME,
    schema=SEND_AGENT_MESSAGE_SCHEMA,
    handler=lambda args, **kw: send_agent_message(
        recipient=args.get("recipient", ""),
        body=args.get("body", ""),
        agent=kw.get("agent") or kw.get("parent_agent"),
    ),
    check_fn=check_send_agent_message_available,
    description="Message one of your running subagents.",
    emoji="📨",
)

registry.register(
    name=TOOL_NAME_SEND_TO_PARENT,
    toolset=TOOLSET_NAME,
    schema=SEND_TO_PARENT_SCHEMA,
    handler=lambda args, **kw: send_to_parent(
        body=args.get("body", ""),
        agent=kw.get("agent") or kw.get("parent_agent"),
    ),
    description="Message the parent that delegated this task to you.",
    emoji="📤",
)
