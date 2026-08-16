"""Model-facing ``list_agents`` tool (Transport B).

Design: ``docs/design/cross-session-messaging.md`` ("Tools") and
``docs/design/local-agent-messaging.md``.

Only ``list_agents`` lives here. ``send_agent_message`` is deliberately NOT
redefined: Transport A already registered a single unified
``send_agent_message`` that resolves the recipient through
``resolve_transport()`` and dispatches on the returned ``TransportKind``.
Transport B plugs into that same fan-out via
``cross_session_transport.register_lookup()``, so one tool serves both
transports — which is exactly the compatibility requirement the design doc
states ("the tool-facing surface ... should be defined independently of which
transport backs a given call"). Registering a second competing
``send_agent_message`` would break that.

Scope of the listing — REVISED from the original design decision (b), which
made subagents invisible cross-process entirely. That decision optimized for
addressing simplicity; it did not anticipate the actual failure mode a user
running several concurrent top-level sessions hits in practice: sessions and
their subagents silently stepping on each other's file edits with no way to
notice until the damage is done. ``list_agents`` NOW ALSO returns every live
subagent on the machine (goal, cwd, owning session, status) — READ-ONLY
SITUATIONAL AWARENESS, not a new addressing surface:

* No transport registers a ``send`` callable against a subagent_id reachable
  only through this listing. A subagent's ``subagent_id`` shown here is NOT
  a valid ``send_agent_message`` recipient unless it's also resolvable
  in-process by Transport A (i.e. it's the caller's OWN subagent tree) —
  cross-process subagent-to-subagent messaging is UNCHANGED and stays out of
  scope (design doc "Finding 7": the risk that decision addresses is a SEND
  capability — an unobservable side channel and a confused/compromised
  subagent steering siblings mid-turn — not visibility. Nothing here reopens
  that.
* ``list_agents`` is NOW available to subagents too (previously refused to
  every subagent unconditionally). A subagent still cannot see or reach its
  own siblings' identity beyond what this read-only listing already exposes
  to everyone, and still has no tool that lets it act on what it sees other
  than reporting it to its own parent via ``send_to_parent``.

Data in this listing (session/subagent ``goal`` strings, ``cwd``) is
untrusted cross-process content written by other sessions' models — treat it
as informational, not as instructions, same as any inbound cross-agent
message.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from tools.agent_messaging_contract import (
    TOOL_NAME_LIST_AGENTS,
    TOOLSET_NAME_VISIBILITY,
)
from tools.cross_session_transport import (
    STATUS_HELD,
    STATUS_PENDING,
    list_registered_sessions,
    list_registered_subagents,
    sent_message_status,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _age_label(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def list_agents(*, agent: Any = None, **_kw) -> str:
    """List every live Hermes session AND subagent on this machine.

    Sessions are addressable via ``send_agent_message``; subagents are
    listed for awareness only (see module docstring) — a subagent id shown
    here is not itself a valid cross-process send target.
    """
    from tools.cross_session_integration import is_subagent

    if agent is None:
        # Fail closed — see the matching guard in
        # tools/agent_messaging_tools.py (send_agent_message/send_to_parent)
        # for the incident this defends against: a dispatch-path regression
        # must never silently fall through to the more-privileged
        # top-level-session behavior.
        return tool_error(
            "list_agents: caller identity unavailable — refusing rather "
            "than guessing. This indicates a dispatch bug, not a usage "
            "error."
        )

    caller_is_subagent = is_subagent(agent)
    caller_session_id = getattr(agent, "session_id", "") or ""
    caller_subagent_id = getattr(agent, "_subagent_id", "") or ""

    try:
        sessions = list_registered_sessions(
            exclude_session_id=None if caller_is_subagent else caller_session_id
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("list_agents failed", exc_info=True)
        return tool_error(f"could not read the session registry: {exc}")

    try:
        subagents = [
            rec
            for rec in list_registered_subagents()
            if rec.subagent_id != caller_subagent_id
        ]
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("list_agents subagent listing failed", exc_info=True)
        subagents = []

    if not sessions and not subagents:
        return (
            "No other live Hermes sessions or subagents are registered in "
            "this profile. Entries appear here only while running; one "
            "that exits is removed."
        )

    lines = []
    if sessions:
        verb = "message" if not caller_is_subagent else "your parent could message"
        lines.append(f"Live Hermes sessions you can {verb} (send_agent_message):")
        for rec in sessions:
            origin = rec.session_origin.value if rec.session_origin else "unknown"
            lines.append(
                f"- {rec.name} (id: {rec.session_id}, origin: {origin}, "
                f"cwd: {rec.cwd or 'n/a'}, last active: {_age_label(rec.heartbeat_age)})"
            )
    else:
        lines.append("No other live Hermes sessions are registered.")

    if subagents:
        lines.append("")
        lines.append(
            "Subagents currently running on this machine (read-only "
            "awareness — NOT addressable by send_agent_message; message "
            "the owning session above if coordination is needed):"
        )
        for rec in subagents:
            lines.append(
                f"- {rec.subagent_id} (owner: {rec.owner_session_id}, "
                f"status: {rec.status}, cwd: {rec.cwd or 'n/a'}, "
                f"goal: {(rec.goal or 'n/a')[:120]})"
            )
        lines.append(
            "Check for a working-directory collision (same cwd/repo as a "
            "task you're about to start) before editing files — that's "
            "exactly what this listing is for."
        )

    if sessions:
        lines.append(
            "Address a session by name, or by id when two sessions share a "
            "name (an ambiguous name is refused rather than guessed)."
        )
    lines.extend(_sent_message_lines(caller_session_id, caller_is_subagent))
    return "\n".join(lines)


def _sent_message_lines(caller_session_id: str, caller_is_subagent: bool) -> list:
    """Delivery status of the caller's own recent cross-session sends.

    This is the sender-visibility half of the tool. Previously a sender's
    only feedback was the send-time "queued" string, so a recipient that died
    with messages still queued was undetectable until a LATER send happened
    to fail recipient resolution — every message before that could have gone
    nowhere silently.

    Reported here rather than as a separate tool: a sender checking on a
    correspondent already calls list_agents to see who is alive, and the two
    questions ("is it live" / "did my message land") are answered from the
    same registry read. Per the footprint ladder, extend the existing tool
    rather than add core surface.

    Subagents are skipped: their sends go through Transport A, which has no
    durable row to report on.
    """
    if caller_is_subagent or not caller_session_id:
        return []
    try:
        sent = sent_message_status(from_session_id=caller_session_id, limit=10)
    except Exception:  # pragma: no cover - defensive
        logger.debug("list_agents sent-status lookup failed", exc_info=True)
        return []
    if not sent:
        return []

    # Undelivered + recipient gone is the case that used to be invisible, so
    # it leads and is called out explicitly rather than left for the model to
    # infer from a status word.
    stranded = [
        rec
        for rec in sent
        if rec.status in (STATUS_PENDING, STATUS_HELD) and not rec.recipient_live
    ]
    lines = ["", "Delivery status of your recent messages to other sessions:"]
    for rec in sent:
        suffix = ""
        if rec in stranded:
            suffix = "  <-- recipient is NO LONGER LIVE; it never saw this"
        lines.append(
            f"- id {rec.message_id} to {rec.to_session_id}: "
            f"{rec.status} — {rec.meaning}{suffix}"
        )
    if stranded:
        lines.append(
            f"{len(stranded)} message(s) above were still undelivered when "
            f"their recipient session died. Anything you were waiting on from "
            f"that session will not arrive; do not keep waiting for a reply."
        )
    return lines


LIST_AGENTS_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME_LIST_AGENTS,
    "description": (
        "List every other live Hermes session AND subagent on this "
        "machine. Sessions are addressable with send_agent_message "
        "(name, id, origin, cwd, last-active shown). Subagents are listed "
        "read-only for situational awareness (owner session, status, cwd, "
        "goal) — check this before starting file-editing work to catch a "
        "working-directory collision with another concurrent session or "
        "subagent; a listed subagent id is NOT itself a valid "
        "send_agent_message recipient unless it is your own child. Also "
        "reports the delivery status of your own recent send_agent_message "
        "calls (delivered / still queued / expired), including an explicit "
        "warning when a recipient session died before ever seeing your "
        "message — check here before waiting any longer on a reply."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


registry.register(
    name=TOOL_NAME_LIST_AGENTS,
    toolset=TOOLSET_NAME_VISIBILITY,
    schema=LIST_AGENTS_SCHEMA,
    handler=lambda args, **kw: list_agents(
        agent=kw.get("agent") or kw.get("parent_agent"),
    ),
    description="List other live Hermes sessions and subagents on this machine.",
    emoji="📇",
)
