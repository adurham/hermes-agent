"""End-to-end Transport A lifecycle for a GATEWAY-origin session.

Companion to ``test_transport_a_gateway_registration.py``, which exercises
``gateway/agent_messaging_bridge.py``'s two helpers in isolation. This file
drives the REAL gateway control flow instead:

    real ``GatewayRunner._run_agent_track_agent()``   (registration)
      -> real ``send_to_parent()`` tool                (delivery via Transport A)
      -> real ``GatewayRunner._clear_conversation_scope()``  (unregistration)
      -> real ``send_to_parent()`` again               (falls back to Transport B)

and asserts the DURABLE side effect at each step (the registry dict, the
recipient's ``_pending_steer`` buffer), never just the tool's return string.

The last step is the user-visible bug this wiring exists to prevent: with a
real Transport B row present for the same session, an unregistered
gateway-origin recipient resolves CROSS_PROCESS_DB, whose inbound policy for
``SessionOrigin.GATEWAY`` is ``POLICY_REFUSE`` — the subagent's message is
rejected outright, not merely held for approval.

Both call sites were silently dropped by the v2026.9.14 upstream merge, which
split ``gateway/run.py`` into ``run_turn.py``/``run_agent_cache.py`` without
carrying the fork's registration/unregistration blocks over.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import tools.agent_messaging_transport_a as transport_a
from tools.agent_messaging_contract import (
    SessionOrigin,
    TransportKind,
    _reset_transport_lookups_for_tests,
    register_transport,
    resolve_transport,
)

GATEWAY_SESSION_ID = "agent:main:telegram:dm:e2e-1"
SUBAGENT_ID = "sa-e2e-child-1"


# ---------------------------------------------------------------------------
# Fixtures — real state.db under a temp HERMES_HOME (Transport B is a durable
# store; mocking it would hide the exact fallthrough this file asserts).
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    import tools.cross_session_transport as cst

    monkeypatch.setattr(cst, "get_hermes_home", lambda: tmp_path)
    # Pin the per-origin defaults rather than whatever the developer's real
    # config.yaml says, so GATEWAY -> POLICY_REFUSE is the contract under test.
    monkeypatch.setattr(
        cst,
        "resolve_inbound_policy",
        lambda *, session_origin=None: cst._DEFAULT_INBOUND_BY_ORIGIN.get(
            session_origin or SessionOrigin.CLI, cst.POLICY_HOLD
        ),
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_transports(home):
    """Both transports are process-global; isolate and register both.

    Transport B must be registered too — without it the post-teardown send
    would resolve NOT_FOUND rather than exercising the real fallthrough.
    """
    import tools.cross_session_transport as cst

    transport_a._reset_for_tests()
    _reset_transport_lookups_for_tests()
    cst._lookup_registered = False  # module-level idempotency guard
    register_transport(
        TransportKind.IN_PROCESS, transport_a.in_process_lookup, transport_a._in_process_send
    )
    cst.register_lookup()
    yield
    transport_a._reset_for_tests()
    _reset_transport_lookups_for_tests()
    cst._lookup_registered = False


@pytest.fixture()
def gateway_origin(monkeypatch):
    """Make this process classify as a gateway session, as the real gateway does."""
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_ACP_SESSION", raising=False)


# ---------------------------------------------------------------------------
# Doubles: only the objects the gateway itself would hand these call sites.
# The GatewayRunner, its SessionState machinery, the bridge, the transports and
# the send_to_parent tool are all REAL.
# ---------------------------------------------------------------------------


def _parent_agent():
    """A top-level gateway session's AIAgent, from Transport A's point of view.

    Transport A's mid-turn delivery branch appends to ``_pending_steer`` under
    ``_pending_steer_lock`` — that buffer IS the durable side effect asserted
    below.
    """
    import threading

    return SimpleNamespace(
        session_id=GATEWAY_SESSION_ID,
        _pending_steer=None,
        _pending_steer_lock=threading.Lock(),
    )


def _subagent(owner_session_id: str = GATEWAY_SESSION_ID):
    """A background=true delegated child of the gateway session above."""
    return SimpleNamespace(
        session_id="child-session",
        _subagent_id=SUBAGENT_ID,
        _delegate_owner_session_id=owner_session_id,
        _parent_subagent_id=None,
    )


def _runner():
    """A real GatewayRunner with only the attributes these two paths read."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._draining = False
    runner._sessions = {}
    return runner


def _turn_ctx(runner, agent, session_key=GATEWAY_SESSION_ID, *, run_generation=None):
    """Build the ctx a real turn would carry.

    ``run_generation`` defaults to claiming a fresh one through the runner's own
    ``_begin_session_run_generation()``, exactly as ``_run_agent`` does — the
    track hook's staleness check compares against that counter and skips the
    whole promotion (registration included) when it doesn't match.
    """
    from gateway.turn_context import TurnContext

    if run_generation is None:
        run_generation = runner._begin_session_run_generation(session_key)
    return TurnContext(
        session_key=session_key,
        run_generation=run_generation,
        agent_holder=[agent],
    )


def _register_transport_b_row(session_id=GATEWAY_SESSION_ID):
    """Give Transport B a live registry row for the SAME session id.

    Without this the post-teardown send resolves NOT_FOUND and the test would
    silently stop proving anything about the refusal path.
    """
    import tools.cross_session_transport as cst

    assert cst.heartbeat_registry(
        session_id=session_id,
        name="gateway-session",
        cwd="/tmp/repo",
        platform="telegram",
        session_origin=SessionOrigin.GATEWAY,
        now=time.time(),
    )


def _send_to_parent(child):
    from tools.agent_messaging_tools import send_to_parent

    return send_to_parent(body="progress from the child", agent=child)


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


def test_gateway_turn_registers_session_as_transport_a_participant(gateway_origin):
    """Driving the REAL turn-loop hook must make the session in-process addressable."""
    runner, agent = _runner(), _parent_agent()
    asyncio.run(runner._run_agent_track_agent(_turn_ctx(runner, agent)))

    # Durable side effect 1: the participant is in Transport A's registry.
    with transport_a._session_lock:
        assert GATEWAY_SESSION_ID in transport_a._session_participants
    # Durable side effect 2: the id is persisted for the matching teardown.
    state = runner._peek_session_state(GATEWAY_SESSION_ID)
    assert state.conversation.transport_a_participant_id == GATEWAY_SESSION_ID
    # And the turn slot is still set (the pre-existing behavior of this hook).
    assert state.turn.agent is agent


def test_send_to_parent_resolves_in_process_after_a_real_turn_starts(gateway_origin):
    runner, agent = _runner(), _parent_agent()
    asyncio.run(runner._run_agent_track_agent(_turn_ctx(runner, agent)))

    from tools.agent_messaging_tools import _caller_participant

    resolution = resolve_transport(_caller_participant(_subagent()), GATEWAY_SESSION_ID)
    assert resolution.kind is TransportKind.IN_PROCESS


def test_send_to_parent_delivers_via_transport_a_during_a_live_turn(gateway_origin):
    """The user-visible payoff: the child's message reaches the parent's buffer."""
    runner, agent = _runner(), _parent_agent()
    asyncio.run(runner._run_agent_track_agent(_turn_ctx(runner, agent)))
    _register_transport_b_row()  # present but must NOT be the one that serves this

    result = _send_to_parent(_subagent())

    # Assert the DURABLE side effect, not the return string: Transport A's
    # mid-turn branch appended the marked message to the parent's steer buffer.
    assert agent._pending_steer, "message never reached the parent agent"
    assert "progress from the child" in agent._pending_steer
    assert SUBAGENT_ID in agent._pending_steer
    # ...and the tool reported queued, not refused.
    assert "does not accept incoming agent messages" not in result
    assert "inbound policy: refuse" not in result
    assert "queued for session" in result


def test_clear_conversation_scope_unregisters_and_send_falls_through_to_refusal(
    gateway_origin,
):
    """Teardown drops the registration; the same send is then REFUSED by Transport B.

    This is both halves of the regression in one test: it pins the
    unregistration call site AND documents exactly what the missing
    registration used to cost a gateway user.
    """
    runner, agent = _runner(), _parent_agent()
    asyncio.run(runner._run_agent_track_agent(_turn_ctx(runner, agent)))
    _register_transport_b_row()

    runner._clear_conversation_scope(GATEWAY_SESSION_ID, reason="test_boundary")

    # Durable side effect: no stale entry survives the boundary (a long-running
    # gateway process would otherwise leak one dict entry + one AIAgent ref per
    # session it has ever served).
    with transport_a._session_lock:
        assert GATEWAY_SESSION_ID not in transport_a._session_participants

    from tools.agent_messaging_tools import _caller_participant

    resolution = resolve_transport(_caller_participant(_subagent()), GATEWAY_SESSION_ID)
    assert resolution.kind is TransportKind.CROSS_PROCESS_DB

    steer_before = agent._pending_steer
    result = _send_to_parent(_subagent())
    assert "inbound policy: refuse" in result
    assert agent._pending_steer == steer_before  # nothing was delivered


def test_registration_survives_repeated_turns_and_is_idempotent(gateway_origin):
    """The hook fires on EVERY turn; that must not multiply registry entries."""
    runner, agent = _runner(), _parent_agent()
    for _ in range(3):
        asyncio.run(runner._run_agent_track_agent(_turn_ctx(runner, agent)))
    with transport_a._session_lock:
        assert len(transport_a._session_participants) == 1


def test_stale_run_generation_does_not_register(gateway_origin):
    """A superseded run must not claim the slot — or the Transport A registration.

    Guards against restoring the registration ABOVE the generation check, which
    would let a stale run overwrite a live successor's stored agent reference.
    """
    runner, agent = _runner(), _parent_agent()
    # Claim generation 1, then bump the live counter past it: this turn is now
    # a superseded run (/stop or /new landed while it was spinning up).
    ctx = _turn_ctx(runner, agent, run_generation=1)
    runner._session_state(GATEWAY_SESSION_ID).persistent.run_generation = 9
    asyncio.run(runner._run_agent_track_agent(ctx))

    with transport_a._session_lock:
        assert GATEWAY_SESSION_ID not in transport_a._session_participants
    state = runner._peek_session_state(GATEWAY_SESSION_ID)
    assert state.conversation.transport_a_participant_id == ""
