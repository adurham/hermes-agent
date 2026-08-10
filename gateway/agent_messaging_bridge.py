"""Transport A (in-process agent messaging) bridge for the gateway.

Companion to ``cli.py``'s registration call (commit 9f6a4da9). That fix
wired ``register_session_participant_for()`` into ``cli.py``'s idle tick and
``delegate_tool.py``'s subagent-spawn site, but ``gateway/run.py`` never
called it. Without a Transport A registration, a gateway session's
``background=true`` subagent calling ``send_to_parent`` fell through
``resolve_transport()`` to Transport B — whose GATEWAY-origin inbound policy
is ``POLICY_REFUSE``, not merely held-for-approval. That path fails 100% of
the time.

Transport A's ``register_session_participant()`` expects, per session:

- ``agent``: the live ``AIAgent`` (used for the mid-turn ``steer()`` branch).
- ``cli``: any object exposing ``_agent_running`` (bool-ish) and a
  ``_pending_input`` attribute with a synchronous ``.put(text)`` (used for
  the idle branch — ``cli.py``'s own ``_pending_input`` queue is a
  ``queue.Queue``-shaped next-turn-injection sink).

The gateway has no single long-lived per-session object shaped like that —
sessions are represented by ``GatewayRunner._sessions[session_key]``
(``SessionState``), and delivery to an idle recipient is a queue-based
poll+push into a platform adapter (``_async_delegation_watcher`` ->
``_deliver_completion_notification`` -> ``_inject_watch_notification`` ->
``adapter.handle_message()``), not a literal ``queue.Queue.put()``.

Rather than force the gateway into CLI's exact ``_pending_input.put()``
shape, this module provides a *thin adapter* that satisfies the same
duck-typed contract by delegating to the gateway's own real mechanism:

- ``_agent_running`` reflects ``session_key in self._running_agents``
  (``GatewayRunner._is_session_running()``).
- ``_pending_input.put(marked_text)`` schedules
  ``GatewayRunner._deliver_completion_notification()`` on the gateway's
  event loop (``GatewayRunner._gateway_loop``) via
  ``asyncio.run_coroutine_threadsafe`` — the exact same injection path
  already used to deliver background-process/async-delegation completions,
  just fed a Transport A payload instead of a process-completion summary.

No new delivery machinery: this is glue over the two mechanisms that
already exist and are already tested (``_running_agents`` / the completion
watcher's injection path).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Synthetic event "type" used for Transport A deliveries injected through
# ``_deliver_completion_notification``. Deliberately NOT "async_delegation"
# or "completion" — those types carry extra durable-claim / dedup logic
# (``tools.async_delegation`` claim/ack, ``_completion_delivery_identity``)
# that does not apply here: Transport A messages are ephemeral, in-process,
# and have no durable producer row to claim or acknowledge.
_EVENT_TYPE = "agent_message_transport_a"


class _GatewayPendingInputSink:
    """Duck-typed stand-in for ``cli.py``'s ``self._pending_input`` queue.

    Transport A's idle-delivery branch (``_append_idle_atomically`` in
    ``tools/agent_messaging_transport_a.py``) calls ``pending_input.put(marked)``
    synchronously — it does not await anything, because on the CLI side
    that's a plain ``queue.Queue``. Subagent code calls into Transport A from
    a worker thread (delegate_tool's background executor), never from the
    gateway's own asyncio event loop thread, so this ``put()`` schedules the
    real async injection onto the gateway loop via
    ``run_coroutine_threadsafe`` and returns immediately — fire-and-forget,
    matching the at-most-once delivery semantics ``drain_to_idle_injection``
    already documents for the cross-process transport's idle path.
    """

    __slots__ = ("_runner", "_session_key")

    def __init__(self, runner: Any, session_key: str) -> None:
        self._runner = runner
        self._session_key = session_key

    def put(self, marked_text: str) -> None:
        loop = getattr(self._runner, "_gateway_loop", None)
        if loop is None or not loop.is_running():
            logger.warning(
                "Transport A gateway delivery dropped for session %s: "
                "no live gateway event loop to inject onto.",
                self._session_key,
            )
            return
        evt = {"type": _EVENT_TYPE, "session_key": self._session_key}
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._runner._deliver_completion_notification(marked_text, evt),
                loop,
            )
        except Exception:
            logger.warning(
                "Transport A gateway delivery scheduling failed for session %s",
                self._session_key,
                exc_info=True,
            )
            return

        def _log_outcome(fut: "asyncio.Future") -> None:
            try:
                result = fut.result()
            except Exception:
                logger.warning(
                    "Transport A gateway delivery injection errored for "
                    "session %s",
                    self._session_key,
                    exc_info=True,
                )
                return
            if result is not True:
                logger.warning(
                    "Transport A gateway delivery for session %s did not "
                    "reach adapter acceptance (result=%r)",
                    self._session_key,
                    result,
                )

        future.add_done_callback(_log_outcome)


class GatewaySessionAgentSink:
    """The ``cli``-shaped object registered for a gateway session.

    Exposes exactly the two attributes ``tools/agent_messaging_transport_a.py``
    reads off a registered participant's ``cli`` reference:
    ``_agent_running`` and ``_pending_input``. Both are computed against the
    gateway's real per-session state rather than duplicated here.
    """

    __slots__ = ("_runner", "_session_key", "_pending_input")

    def __init__(self, runner: Any, session_key: str) -> None:
        self._runner = runner
        self._session_key = session_key
        self._pending_input = _GatewayPendingInputSink(runner, session_key)

    @property
    def _agent_running(self) -> bool:
        try:
            return bool(self._runner._is_session_running(self._session_key))
        except Exception:
            return False


def register_gateway_session_participant(runner: Any, session_key: str, agent: Any) -> str:
    """Register ``agent``'s owning gateway session as a Transport A participant.

    Call this whenever a gateway session's ``AIAgent`` becomes the live
    turn agent (``GatewayRunner``'s ``track_agent()``, mirroring ``cli.py``'s
    idle-tick call and ``delegate_tool``'s spawn-site call). Additive and
    idempotent (see ``register_session_participant``'s docstring) — safe to
    call on every turn, which also keeps the stored ``agent`` reference
    fresh across gateway's per-turn ``AIAgent`` reconstruction.

    Returns the ``participant_id`` (== ``agent.session_id`` at the moment of
    registration) when a registration was attempted, or ``""`` on failure.
    Callers MUST persist this return value (``SessionState.conversation.
    transport_a_participant_id`` — see that field's docstring) and pass it
    back to ``unregister_gateway_session_participant`` at the matching
    conversation boundary, rather than trying to re-derive it later from
    whatever agent object happens to still be reachable at that point: a
    session split (in-place compaction changes ``agent.session_id`` without
    rotating the gateway's ``session_key``) can leave the live agent's
    ``session_id`` different from the id actually registered here, and
    ``TurnState.clear()`` already nulls ``state.turn.agent`` at the end of
    every turn — well before most conversation boundaries fire. Never
    raises — a messaging-registration failure must never block a gateway
    turn.
    """
    if not session_key or agent is None:
        return ""
    try:
        from tools.cross_session_integration import register_session_participant_for

        if not register_session_participant_for(
            agent, cli=GatewaySessionAgentSink(runner, session_key)
        ):
            return ""
        return getattr(agent, "session_id", "") or ""
    except Exception:
        logger.debug(
            "gateway Transport A participant registration failed for %s",
            session_key,
            exc_info=True,
        )
        return ""


def unregister_gateway_session_participant(participant_id: Optional[str]) -> None:
    """Drop a gateway session's Transport A registration at a conversation boundary.

    UNLIKE the CLI fix (which deliberately never unregisters, because CLI
    processes are short-lived and an in-flight subagent's ``send_to_parent``
    must keep resolving for the rest of the process's life), gateway
    sessions are long-running: the SAME process serves potentially thousands
    of sessions over its lifetime, so a registration that is never removed
    is an unbounded ``_session_participants`` leak (one dict entry, plus a
    strong ``AIAgent`` reference, per session ever seen).

    Called from ``GatewayRunner._clear_conversation_scope()`` — the single
    funnel every true conversation boundary (session expiry, /new, /resume,
    auto-reset) already routes through, so this fires at exactly the same
    points ``_CONVERSATION_SCOPED_STATE`` is cleared and nowhere else. It
    deliberately does NOT fire on idle agent-cache eviction (``_evict_cached_agent``
    called standalone, e.g. after /model) since that is not a conversation
    boundary — the session can resume and its subagents (if genuinely
    in-flight, mid-turn) still need to reach it.

    ``participant_id`` is the exact id ``register_gateway_session_participant``
    returned at registration time (persisted in ``SessionState.conversation.
    transport_a_participant_id``) — NOT re-derived from a live ``AIAgent``
    object here. See that field's docstring in ``gateway/session_state.py``
    for why re-deriving it at teardown time is unsound.
    """
    if not participant_id:
        return
    try:
        from tools.agent_messaging_transport_a import unregister_session_participant

        unregister_session_participant(participant_id)
    except Exception:
        logger.debug(
            "gateway Transport A participant unregistration failed for %s",
            participant_id,
            exc_info=True,
        )


__all__ = [
    "GatewaySessionAgentSink",
    "register_gateway_session_participant",
    "unregister_gateway_session_participant",
]
