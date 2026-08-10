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

Scope of the listing, per design decision (b) in local-agent-messaging.md:
**subagents are invisible cross-process.** ``list_agents`` returns top-level
sessions only. A caller reaches another session's subagents by messaging that
session, which decides for itself whether to relay in-process. A session's own
in-process subagents are discoverable through ``list_active_subagents``, which
already exists — duplicating them here would blur the addressing model the
decision deliberately kept simple.

Never given to a subagent in any mode (design doc Question 4).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from tools.agent_messaging_contract import (
    TOOL_NAME_LIST_AGENTS,
    TOOLSET_NAME,
)
from tools.cross_session_transport import list_registered_sessions
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _age_label(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def list_agents(*, agent: Any = None, **_kw) -> str:
    """List other live Hermes sessions reachable via ``send_agent_message``."""
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

    if is_subagent(agent):
        # Defense in depth — the toolset gate should already prevent this.
        return tool_error("list_agents is not available to subagents.")

    session_id = getattr(agent, "session_id", "") or ""
    try:
        sessions = list_registered_sessions(exclude_session_id=session_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("list_agents failed", exc_info=True)
        return tool_error(f"could not read the session registry: {exc}")

    if not sessions:
        return (
            "No other live Hermes sessions are registered in this profile. "
            "Sessions appear here only while running and heartbeating; a "
            "session that exited is removed."
        )

    lines = ["Live Hermes sessions you can message (send_agent_message):"]
    for rec in sessions:
        origin = rec.session_origin.value if rec.session_origin else "unknown"
        lines.append(
            f"- {rec.name} (id: {rec.session_id}, origin: {origin}, "
            f"cwd: {rec.cwd or 'n/a'}, last active: {_age_label(rec.heartbeat_age)})"
        )
    lines.append(
        "Address a recipient by name, or by id when two sessions share a name "
        "(an ambiguous name is refused rather than guessed). Subagents are not "
        "listed: message the session that owns them."
    )
    return "\n".join(lines)


LIST_AGENTS_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME_LIST_AGENTS,
    "description": (
        "List the other live Hermes sessions on this machine that you can "
        "reach with send_agent_message. Returns each session's name, id, "
        "origin, working directory, and how recently it was active. Only "
        "top-level sessions are listed — another session's subagents are not "
        "directly addressable; message that session instead."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


registry.register(
    name=TOOL_NAME_LIST_AGENTS,
    toolset=TOOLSET_NAME,
    schema=LIST_AGENTS_SCHEMA,
    handler=lambda args, **kw: list_agents(
        agent=kw.get("agent") or kw.get("parent_agent"),
    ),
    description="List other live Hermes sessions you can message.",
    emoji="📇",
)
