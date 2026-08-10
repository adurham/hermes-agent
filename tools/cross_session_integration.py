"""Transport B integration glue — the seam between ``cross_session_transport``
and the live processes that must heartbeat, drain, and inject.

Design: ``docs/design/cross-session-messaging.md`` ("Delivery mechanics") and
``docs/design/local-agent-messaging.md`` (Transport B sections).

Why this module exists rather than inlining the logic into ``cli.py`` /
``run_agent.py``: both of those are multi-thousand-line god-files that cannot
be imported in a unit test without dragging in the whole agent. Keeping the
glue here means the call sites stay one-liners and the actual behaviour —
especially the *framing* invariant — is directly testable.

The single invariant this module exists to protect:

    **Nothing here ever hands a raw body to an injection sink.**

Every injection path reads ``DrainedMessage.framed_body``, which
``cross_session_transport._frame()`` produced via
``build_agent_message_marker()``. The raw ``body`` column is never read
outside the transport module. A bare body reaching a transcript is
indistinguishable from operator-authored input and defeats the entire
untrusted-content framing contract.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, List, Optional

from tools.agent_messaging_contract import (
    CROSS_SESSION_HEARTBEAT_SECONDS,
    SessionOrigin,
)
from tools import cross_session_transport as _tb

logger = logging.getLogger(__name__)


# How often the idle poll runs the cheap housekeeping sweep (reap stale
# registry rows + expire held messages). NOT specified by the design doc.
# The idle tick fires every 0.1s; running two DB writes at 10Hz forever would
# be gratuitous for operations whose inputs move on a 3600s horizon, so this
# is throttled to once a minute — the same cadence as the heartbeat, which is
# the fastest rate at which any of the reaped state can actually change.
MAINTENANCE_INTERVAL_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def session_origin() -> SessionOrigin:
    """This process's session origin.

    Delegates to Transport A's classifier when available so the two transports
    can never disagree about what kind of session this is; falls back to the
    same env-flag rules if Transport A is not installed.
    """
    try:
        from tools.agent_messaging_tools import session_origin as _origin

        return _origin()
    except Exception:
        pass
    from utils import env_var_enabled

    if env_var_enabled("HERMES_GATEWAY_SESSION"):
        return SessionOrigin.GATEWAY
    if env_var_enabled("HERMES_CRON_SESSION"):
        return SessionOrigin.CRON
    if env_var_enabled("HERMES_ACP_SESSION"):
        return SessionOrigin.ACP
    return SessionOrigin.CLI


def session_display_name(agent: Any) -> str:
    """Registry ``name`` for this session.

    Per the design doc: the ``/rename`` title when the session has one,
    otherwise the cwd folder name. Never empty — ``heartbeat_registry``
    refuses a blank name, which would silently drop this session out of the
    registry entirely.
    """
    session_id = getattr(agent, "session_id", "") or ""
    db = getattr(agent, "_session_db", None)
    getter = getattr(db, "get_session_title", None)
    if callable(getter) and session_id:
        try:
            title = getter(session_id)
            if title and str(title).strip():
                return str(title).strip()
        except Exception:
            pass
    try:
        return os.path.basename(os.getcwd()) or "hermes"
    except Exception:
        return "hermes"


def is_subagent(agent: Any) -> bool:
    """Transport B is sessions-only; a delegated child must never register."""
    sid = getattr(agent, "_subagent_id", None)
    return isinstance(sid, str) and bool(sid)


# ---------------------------------------------------------------------------
# Startup + heartbeat
# ---------------------------------------------------------------------------


def register_session_participant_for(agent: Any, cli: Any = None) -> bool:
    """Make ``agent``'s session addressable as a Transport A recipient.

    Why this lives here rather than at the ``cli.py`` call site: building the
    ``Participant`` requires the contract types and the origin classifier,
    and ``cli.py`` is a god-file we keep free of that knowledge. Call sites
    stay one-liners (module docstring's stated reason for existing).

    Without this, ``in_process_lookup`` can never find a top-level session,
    so EVERY ``send_to_parent`` from a background subagent falls through to
    Transport B — held for human approval, or refused outright on a
    gateway/cron origin — even when sender and recipient are the same
    process. Registration is additive and idempotent, so calling it on every
    idle tick and at every subagent spawn is safe and covers ``session_id``
    reassignment without ever removing an alias an in-flight subagent may
    still be keyed to.

    Returns True when a registration was attempted.
    """
    if is_subagent(agent):
        return False
    session_id = getattr(agent, "session_id", "") or ""
    if not session_id:
        return False
    try:
        from tools.agent_messaging_contract import Participant, ParticipantKind
        from tools.agent_messaging_transport_a import register_session_participant

        register_session_participant(
            Participant(
                participant_id=session_id,
                kind=ParticipantKind.SESSION,
                owner_session_id=session_id,
                session_origin=session_origin(),
            ),
            agent=agent,
            cli=cli,
        )
        return True
    except Exception as exc:  # never break startup/spawn over messaging
        logger.debug("in-process participant registration failed: %s", exc)
        return False


def install_transport() -> None:
    """Plug Transport B into ``resolve_transport``'s fan-out. Idempotent.

    Called once at session/process startup. ``register_lookup`` guards its own
    re-entry, so calling this on every startup path is safe.
    """
    try:
        _tb.register_lookup()
    except Exception as exc:  # never break startup over messaging
        logger.debug("cross_session lookup registration failed: %s", exc)


def heartbeat_if_due(agent: Any, *, now: Optional[float] = None) -> bool:
    """Upsert this session's registry row, at most once per heartbeat window.

    Piggybacks on the caller's existing turn-boundary activity hook rather
    than spawning a second heartbeat thread. Rate-limited here (not in the
    transport) so the transport stays a pure data layer.

    Returns True only when a row was actually written.
    """
    if is_subagent(agent):
        return False
    session_id = getattr(agent, "session_id", "") or ""
    if not session_id:
        return False

    ts = float(now if now is not None else time.time())
    last = getattr(agent, "_cross_session_last_heartbeat", 0.0) or 0.0
    if (ts - last) < CROSS_SESSION_HEARTBEAT_SECONDS:
        return False
    try:
        setattr(agent, "_cross_session_last_heartbeat", ts)
    except Exception:
        pass

    try:
        cwd = os.getcwd()
    except Exception:
        cwd = None
    return _tb.heartbeat_registry(
        session_id=session_id,
        name=session_display_name(agent),
        cwd=cwd,
        platform="cli",
        session_origin=session_origin(),
        now=ts,
    )


_last_maintenance = 0.0


def maintenance_tick(*, now: Optional[float] = None, force: bool = False) -> bool:
    """Reap dead registry rows and expire stale held messages.

    Throttled to ``MAINTENANCE_INTERVAL_SECONDS`` because the idle poll that
    calls it fires at 10Hz. Both underlying operations are idempotent, so the
    throttle is a cost decision, not a correctness one.
    """
    global _last_maintenance
    ts = float(now if now is not None else time.time())
    if not force and (ts - _last_maintenance) < MAINTENANCE_INTERVAL_SECONDS:
        return False
    _last_maintenance = ts
    try:
        _tb.reap_stale_registry(now=ts)
        _tb.expire_held_messages(now=ts)
    except Exception as exc:
        logger.debug("cross_session maintenance failed: %s", exc)
        return False
    return True


def _reset_maintenance_for_tests() -> None:
    global _last_maintenance
    _last_maintenance = 0.0


# ---------------------------------------------------------------------------
# Delivery — idle path (cli.py's process_loop) and mid-turn path
# ---------------------------------------------------------------------------


def _drain(
    session_id: str,
    *,
    on_held: Optional[Callable[[str], None]],
    now: Optional[float],
) -> List[_tb.DrainedMessage]:
    try:
        return _tb.drain_inbox(
            session_id=session_id,
            session_origin=session_origin(),
            on_held=on_held,
            now=now,
        )
    except Exception as exc:
        logger.debug("cross_session drain failed: %s", exc)
        return []


def drain_to_idle_injection(
    *,
    session_id: str,
    inject: Callable[[str], None],
    on_held: Optional[Callable[[str], None]] = None,
    turn_state: Optional[_tb.TurnMessageState] = None,
    now: Optional[float] = None,
) -> int:
    """Idle-recipient delivery: claim, then inject each message's FRAMED body.

    ``inject`` is ``cli.py``'s ``self._pending_input.put`` — the same sink the
    already-shipped background-process completion path uses, picked up on the
    next 0.1s tick and started as a fresh turn exactly like a typed prompt.

    Note precisely what is passed to ``inject``: ``msg.framed_body``. The raw
    ``body`` is not reachable from a ``DrainedMessage`` at all, so this call
    site cannot regress into injecting an unwrapped message.

    Returns the number of messages injected.
    """
    if not session_id:
        return 0
    count = 0
    for msg in _drain(session_id, on_held=on_held, now=now):
        if turn_state is not None:
            turn_state.record_delivered(msg.hop_count)
        try:
            inject(msg.framed_body)
            count += 1
        except Exception as exc:
            # At-most-once, stated deliberately: the row is already claimed.
            logger.debug("cross_session injection failed: %s", exc)
    return count


def drain_into_pending_steer(
    agent: Any,
    *,
    on_held: Optional[Callable[[str], None]] = None,
    now: Optional[float] = None,
) -> int:
    """Mid-turn delivery for an ACTIVE session.

    The design doc's mid-turn mechanic is "tail-append onto the existing
    tool_result-bearing user message at the same checkpoint already used for
    interrupt-checking". That checkpoint is
    ``agent/conversation_loop.py``'s pre-API-call block: it reads
    ``agent._interrupt_requested``, then drains ``agent._pending_steer`` and
    appends it onto the last ``role="tool"`` message — exactly the tail-append
    the doc describes, already implemented, already provider-correct, and
    already handling the "no tool call in flight" case by putting the text
    back for the next batch.

    So Transport B does NOT reimplement the tail-append. It feeds
    ``_pending_steer`` immediately before that drain, which also gives parity
    with Transport A's active-recipient path (Transport A delivers via the
    same ``steer()`` mechanic).

    The appended text is ``framed_body`` — the steer marker the loop adds is
    an additional outer wrapper, never a replacement for the cross-agent one.
    """
    session_id = getattr(agent, "session_id", "") or ""
    if not session_id or is_subagent(agent):
        return 0

    messages = _drain(session_id, on_held=on_held, now=now)
    if not messages:
        return 0

    turn_state = getattr(agent, "_cross_session_turn_state", None)
    if turn_state is None:
        turn_state = _tb.TurnMessageState()
        try:
            setattr(agent, "_cross_session_turn_state", turn_state)
        except Exception:
            turn_state = None

    chunks = []
    for msg in messages:
        if turn_state is not None:
            turn_state.record_delivered(msg.hop_count)
        chunks.append(msg.framed_body)
    text = "\n".join(chunks)

    lock = getattr(agent, "_pending_steer_lock", None)
    if lock is not None:
        with lock:
            existing = getattr(agent, "_pending_steer", None)
            agent._pending_steer = (existing + "\n" + text) if existing else text
    else:
        existing = getattr(agent, "_pending_steer", None)
        agent._pending_steer = (existing + "\n" + text) if existing else text
    return len(messages)


__all__ = [
    "MAINTENANCE_INTERVAL_SECONDS",
    "drain_into_pending_steer",
    "drain_to_idle_injection",
    "heartbeat_if_due",
    "install_transport",
    "is_subagent",
    "maintenance_tick",
    "register_session_participant_for",
    "session_display_name",
    "session_origin",
]
