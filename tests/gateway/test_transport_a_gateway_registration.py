"""Regression tests for the gateway half of the Transport A registration gap
found 2026-08-10 (companion to the CLI-side fix, commit 9f6a4da9).

Before this fix, gateway/run.py never called
``register_session_participant_for()``, so a gateway-origin background
subagent's ``send_to_parent`` always fell through Transport A's
``in_process_lookup`` (empty registry) to Transport B, whose GATEWAY-origin
inbound policy is ``POLICY_REFUSE`` — an outright rejection, not merely a
held-for-approval delay.

These tests exercise ``gateway/agent_messaging_bridge.py`` directly against
the real Transport A registry (``tools.agent_messaging_transport_a``), plus
AST pins confirming the two gateway/run.py call sites (registration at
``track_agent()``, unregistration at ``_clear_conversation_scope()``) are
wired and haven't regressed.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

from tools.agent_messaging_contract import (
    Participant,
    ParticipantKind,
    SessionOrigin,
    TransportKind,
    _reset_transport_lookups_for_tests,
    resolve_transport,
)
import tools.agent_messaging_transport_a as transport_a
from gateway.agent_messaging_bridge import (
    GatewaySessionAgentSink,
    register_gateway_session_participant,
    unregister_gateway_session_participant,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_transport_a_registry():
    """Transport A's participant registry is process-global; isolate tests.

    Other test modules call ``_reset_transport_lookups_for_tests()`` on the
    shared contract registry (clearing ALL registered transport lookups, not
    just this module's participant dict) and don't always re-register
    Transport A afterward — a real cross-test-file ordering hazard, not
    specific to this file. Re-registering here defensively guarantees
    ``resolve_transport`` can find Transport A's ``in_process_lookup``
    regardless of what ran before us in the same process.
    """
    transport_a._reset_for_tests()
    _reset_transport_lookups_for_tests()
    from tools.agent_messaging_contract import register_transport

    register_transport(
        TransportKind.IN_PROCESS,
        transport_a.in_process_lookup,
        transport_a._in_process_send,
    )
    yield
    transport_a._reset_for_tests()


class _FakeRunner:
    """Minimal stand-in for GatewayRunner exposing the surface the bridge needs."""

    def __init__(self):
        self._running = set()
        self.injected = []
        self._gateway_loop = None

    def _is_session_running(self, session_key: str) -> bool:
        return session_key in self._running

    async def _deliver_completion_notification(self, synth_text, evt):
        self.injected.append((synth_text, evt))
        return True


def _fake_agent(session_id: str, *, is_subagent: bool = False):
    kwargs = {"session_id": session_id}
    if is_subagent:
        kwargs["_subagent_id"] = "sub-1"
    return SimpleNamespace(**kwargs)


def _sender_participant(owner_session_id: str) -> Participant:
    return Participant(
        participant_id="sub-child-1",
        kind=ParticipantKind.SUBAGENT,
        owner_session_id=owner_session_id,
        session_origin=SessionOrigin.GATEWAY,
    )


# ---------------------------------------------------------------------------
# Core regression: a gateway session's background subagent resolves via
# Transport A (IN_PROCESS), never Transport B's POLICY_REFUSE.
# ---------------------------------------------------------------------------


def test_registered_gateway_session_resolves_in_process():
    runner = _FakeRunner()
    session_id = "agent:main:telegram:dm:12345"
    agent = _fake_agent(session_id)

    result = register_gateway_session_participant(runner, session_id, agent)
    assert result == session_id

    sender = _sender_participant(session_id)
    resolution = resolve_transport(sender, session_id)
    assert resolution.kind is TransportKind.IN_PROCESS


def test_unregistered_gateway_session_does_not_resolve_in_process():
    """Sanity check: without registration, in_process_lookup finds nothing
    (falls through to whatever else is registered — Transport B in
    production)."""
    session_id = "agent:main:telegram:dm:99999"
    sender = _sender_participant(session_id)
    recipient = transport_a.in_process_lookup(sender, session_id)
    assert recipient is None


def test_gateway_sink_agent_running_reflects_running_agents():
    runner = _FakeRunner()
    session_key = "agent:main:discord:dm:1"
    sink = GatewaySessionAgentSink(runner, session_key)
    assert sink._agent_running is False
    runner._running.add(session_key)
    assert sink._agent_running is True


def test_gateway_sink_idle_delivery_injects_via_completion_notification():
    """The idle-recipient branch of Transport A delivery
    (``_append_idle_atomically``) calls ``cli._pending_input.put(marked)``.
    For the gateway sink, that must reach the real injection path
    (``_deliver_completion_notification``), not silently no-op.
    """
    runner = _FakeRunner()
    session_id = "agent:main:slack:dm:42"
    agent = _fake_agent(session_id)
    register_gateway_session_participant(runner, session_id, agent)

    sink = GatewaySessionAgentSink(runner, session_id)
    # Simulate a live gateway event loop by using the running loop directly.
    import asyncio

    async def _drive():
        loop = asyncio.get_running_loop()
        runner._gateway_loop = loop
        sink._pending_input.put("hello from subagent")
        # Let the scheduled coroutine run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert runner.injected
    synth_text, evt = runner.injected[0]
    assert synth_text == "hello from subagent"
    assert evt["session_key"] == session_id


# ---------------------------------------------------------------------------
# Unregistration on session-boundary eviction — no stale entries survive.
# ---------------------------------------------------------------------------


def test_unregister_drops_session_from_registry():
    runner = _FakeRunner()
    session_id = "agent:main:telegram:dm:555"
    agent = _fake_agent(session_id)
    participant_id = register_gateway_session_participant(runner, session_id, agent)
    assert participant_id == session_id

    sender = _sender_participant(session_id)
    assert transport_a.in_process_lookup(sender, session_id) is not None

    unregister_gateway_session_participant(participant_id)

    assert transport_a.in_process_lookup(sender, session_id) is None


def test_unregister_is_noop_for_empty_participant_id():
    # Must never raise when called with no id (e.g. a session that never
    # ran a turn before hitting a conversation boundary, so
    # transport_a_participant_id was never populated).
    unregister_gateway_session_participant(None)
    unregister_gateway_session_participant("")


def test_unregister_uses_registration_time_id_not_current_agent_session_id():
    """Regression for the 2026-08-10 fragile-lookup bug: unregistration must
    use the id CAPTURED AT REGISTRATION TIME, not whatever a live agent's
    ``session_id`` happens to be at teardown. A session split (in-place
    compaction changes ``agent.session_id`` without rotating the gateway's
    routing key) used to leave the old lookup chain unregistering the WRONG
    id (or finding nothing at all, since ``TurnState.clear()`` nulls
    ``turn.agent`` before most boundaries fire) -- see
    ``transport_a_participant_id``'s docstring in gateway/session_state.py.
    """
    runner = _FakeRunner()
    original_session_id = "agent:main:telegram:dm:split-before"
    agent = _fake_agent(original_session_id)
    participant_id = register_gateway_session_participant(runner, original_session_id, agent)
    assert participant_id == original_session_id

    # Simulate a session split: the SAME agent object's session_id changes
    # (compression in-place compaction) without re-registering.
    agent.session_id = "agent:main:telegram:dm:split-after"

    sender = _sender_participant(original_session_id)
    assert transport_a.in_process_lookup(sender, original_session_id) is not None

    # Unregistering by the CAPTURED id (what _clear_conversation_scope now
    # does) correctly drops the original registration even though the live
    # agent object's session_id has since diverged.
    unregister_gateway_session_participant(participant_id)
    assert transport_a.in_process_lookup(sender, original_session_id) is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_registration_is_idempotent():
    runner = _FakeRunner()
    session_id = "agent:main:telegram:dm:777"
    agent = _fake_agent(session_id)

    assert register_gateway_session_participant(runner, session_id, agent) == session_id
    assert register_gateway_session_participant(runner, session_id, agent) == session_id

    with transport_a._session_lock:
        assert len(transport_a._session_participants) == 1


def test_registration_refuses_subagents():
    runner = _FakeRunner()
    session_id = "sub-agent-id"
    agent = _fake_agent(session_id, is_subagent=True)
    assert register_gateway_session_participant(runner, session_id, agent) == ""


# ---------------------------------------------------------------------------
# Cross-process fallthrough must still work — a genuinely different
# session/process's message is NOT resolved in-process just because some
# OTHER gateway session happens to be registered.
# ---------------------------------------------------------------------------


def test_different_session_does_not_resolve_in_process():
    runner = _FakeRunner()
    registered_session = "agent:main:telegram:dm:1"
    other_session = "agent:main:telegram:dm:2"
    agent = _fake_agent(registered_session)
    register_gateway_session_participant(runner, registered_session, agent)

    sender = _sender_participant(other_session)
    assert transport_a.in_process_lookup(sender, registered_session) is None


# ---------------------------------------------------------------------------
# AST pins: confirm the two gateway/run.py call sites exist and haven't
# regressed (mirrors the repo's existing AST-pin test convention, e.g.
# test_10710_auto_reset_evicts_cached_agent.py).
# ---------------------------------------------------------------------------


def _source_of(func) -> str:
    return inspect.getsource(func)


def test_track_agent_registers_transport_a_participant():
    from gateway import run as gateway_run

    src = inspect.getsource(gateway_run)
    assert "register_gateway_session_participant" in src
    assert "from gateway.agent_messaging_bridge import" in src


def test_clear_conversation_scope_unregisters_transport_a_participant():
    from gateway.run import GatewayRunner

    src = _source_of(GatewayRunner._clear_conversation_scope)
    assert "unregister_gateway_session_participant" in src
