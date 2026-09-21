"""Regression tests: ACP sessions register as Transport A participants.

Companion to tests/tools/test_agent_messaging_send_dispatch.py's CLI-side
coverage of commit 9f6a4da9. ACP (acp_adapter/session.py's SessionManager)
is a fully separate session host from cli.py -- its own SessionState, its
own AIAgent construction, its own session_id lifecycle -- so the CLI fix's
call sites never registered an ACP session. These tests pin the three ACP
call sites (create_session, fork_session, _restore) via the shared
_register_transport_a_participant() helper they all use, and prove an ACP
background subagent's send_to_parent resolves IN_PROCESS with zero
cross_session_inbox rows -- exactly the CLI-side test's core assertion,
now for the ACP host.
"""

from __future__ import annotations

import pytest

from tools.agent_messaging_contract import (
    Participant,
    ParticipantKind,
    SessionOrigin,
    TransportKind,
    _reset_transport_lookups_for_tests,
    resolve_transport,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so each test gets its own real state.db."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    import tools.cross_session_transport as cst

    monkeypatch.setattr(cst, "get_hermes_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def cst(home):
    import tools.cross_session_transport as module

    return module


@pytest.fixture(autouse=True)
def _clean_registry():
    """Transport registrations and session-participant state are
    process-global; isolate each test the same way
    test_agent_messaging_send_dispatch.py does.
    """
    import tools.agent_messaging_transport_a as ta
    import tools.cross_session_transport as cst_mod

    _reset_transport_lookups_for_tests()
    cst_mod._lookup_registered = False
    ta._reset_for_tests()
    yield
    _reset_transport_lookups_for_tests()
    cst_mod._lookup_registered = False
    ta._reset_for_tests()


@pytest.fixture()
def _clean_subagents():
    import tools.delegate_tool as dt

    with dt._active_subagents_lock:
        dt._active_subagents.clear()
    yield
    with dt._active_subagents_lock:
        dt._active_subagents.clear()


def _register_in_process_transport():
    import tools.agent_messaging_transport_a as ta
    from tools.agent_messaging_contract import register_transport

    register_transport(
        TransportKind.IN_PROCESS, ta.in_process_lookup, ta._in_process_send
    )
    return ta


def _spawn_subagent_record(subagent_id, owner_session_id, agent):
    import tools.delegate_tool as dt

    dt._register_subagent(
        {
            "subagent_id": subagent_id,
            "parent_id": None,
            "owner_session_id": owner_session_id,
            "agent": agent,
            "goal": "test",
            "status": "running",
        }
    )


def _inbox_rows(cst):
    with cst._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM cross_session_inbox").fetchone()[0]


def _manager(monkeypatch):
    """Build a SessionManager whose agent stub's ``session_id`` matches the
    real ACP ``session_id`` SessionManager generates -- exactly what a real
    ``AIAgent`` does (``_make_agent`` passes ``session_id=session_id`` as a
    kwarg). The stock ``agent_factory=`` hook takes no arguments, so we
    monkeypatch ``_make_agent`` itself instead to keep that invariant true
    for these tests without reimplementing SessionManager's uuid generation.
    """
    from unittest.mock import MagicMock

    from acp_adapter.session import SessionManager

    manager = SessionManager(db=None)

    real_make_agent = SessionManager._make_agent

    def _stub_make_agent(self, *, session_id, cwd, **kwargs):
        agent = MagicMock(name="MockAIAgent")
        agent.session_id = session_id
        agent.model = "stub-model"
        return agent

    monkeypatch.setattr(SessionManager, "_make_agent", _stub_make_agent)
    monkeypatch.setattr(
        "acp_adapter.session._register_task_cwd", lambda task_id, cwd: None
    )
    return manager


# ---------------------------------------------------------------------------
# create_session registers the session
# ---------------------------------------------------------------------------


def test_create_session_registers_transport_a_participant(
    home, cst, _clean_subagents, monkeypatch
):
    """An ACP session's own background subagent resolves IN_PROCESS after
    create_session() -- the core gap this patch closes.
    """
    _register_in_process_transport()
    cst.register_lookup()

    manager = _manager(monkeypatch)
    state = manager.create_session(cwd="/tmp/work")

    _spawn_subagent_record("sub-1", state.session_id, object())

    sender = Participant(
        participant_id="sub-1",
        kind=ParticipantKind.SUBAGENT,
        owner_session_id=state.session_id,
    )
    resolution = resolve_transport(sender, state.session_id)

    assert resolution.kind is TransportKind.IN_PROCESS
    assert _inbox_rows(cst) == 0


def test_fork_session_registers_transport_a_participant(
    home, cst, _clean_subagents, monkeypatch
):
    _register_in_process_transport()
    cst.register_lookup()

    manager = _manager(monkeypatch)
    original = manager.create_session(cwd="/tmp/work")

    forked = manager.fork_session(original.session_id, cwd="/tmp/work")
    assert forked is not None

    _spawn_subagent_record("sub-fork", forked.session_id, object())

    sender = Participant(
        participant_id="sub-fork",
        kind=ParticipantKind.SUBAGENT,
        owner_session_id=forked.session_id,
    )
    resolution = resolve_transport(sender, forked.session_id)

    assert resolution.kind is TransportKind.IN_PROCESS
    assert _inbox_rows(cst) == 0


def test_restore_registers_transport_a_participant(
    home, cst, _clean_subagents, monkeypatch
):
    """A session restored from the DB (process restart / reconnect) is also
    reachable in-process afterwards.
    """
    _register_in_process_transport()
    cst.register_lookup()

    manager = _manager(monkeypatch)
    created = manager.create_session(cwd="/tmp/work")
    manager.save_session(created.session_id)

    # Simulate a process restart: drop the in-memory session, force _restore.
    with manager._lock:
        manager._sessions.pop(created.session_id, None)

    restored = manager.get_session(created.session_id)
    assert restored is not None

    _spawn_subagent_record("sub-restore", restored.session_id, object())

    sender = Participant(
        participant_id="sub-restore",
        kind=ParticipantKind.SUBAGENT,
        owner_session_id=restored.session_id,
    )
    resolution = resolve_transport(sender, restored.session_id)

    assert resolution.kind is TransportKind.IN_PROCESS
    assert _inbox_rows(cst) == 0


# ---------------------------------------------------------------------------
# Idempotency across create -> fork -> restore
# ---------------------------------------------------------------------------


def test_registration_idempotent_across_create_fork_restore(home, cst, monkeypatch):
    """Repeated registration (create, then a later fork/restore touching the
    same participant id) must not duplicate entries or raise.
    """
    import tools.agent_messaging_transport_a as ta_mod

    _register_in_process_transport()
    cst.register_lookup()

    manager = _manager(monkeypatch)
    state = manager.create_session(cwd="/tmp/work")

    # Re-run the registration helper directly several times, as the idle
    # tick / repeated saves might.
    from acp_adapter.session import _register_transport_a_participant

    for _ in range(5):
        _register_transport_a_participant(state)

    with ta_mod._session_lock:
        assert state.session_id in ta_mod._session_participants
        # Only ever one entry for this id, never duplicated.
        assert (
            len([k for k in ta_mod._session_participants if k == state.session_id])
            == 1
        )


# ---------------------------------------------------------------------------
# Idle recipient sink: _CliShim._pending_input.put() lands in queued_prompts
# ---------------------------------------------------------------------------


def test_idle_delivery_lands_in_queued_prompts(
    home, cst, _clean_subagents, monkeypatch
):
    """When the ACP session is idle (is_running=False), a delivered message
    must append to SessionState.queued_prompts via the _CliShim -- the ACP
    equivalent of cli.py's _pending_input queue.
    """
    _register_in_process_transport()
    cst.register_lookup()

    manager = _manager(monkeypatch)
    state = manager.create_session(cwd="/tmp/work")
    state.is_running = False

    _spawn_subagent_record("sub-idle", state.session_id, object())

    from tools.agent_messaging_transport_a import send_in_process

    sender = Participant(
        participant_id="sub-idle",
        kind=ParticipantKind.SUBAGENT,
        owner_session_id=state.session_id,
    )
    recipient = Participant(
        participant_id=state.session_id,
        kind=ParticipantKind.SESSION,
        owner_session_id=state.session_id,
        session_origin=SessionOrigin.ACP,
    )

    send_in_process(sender=sender, recipient=recipient, body="hello parent")

    assert len(state.queued_prompts) == 1
    assert "hello parent" in state.queued_prompts[0]
    assert _inbox_rows(cst) == 0


def test_active_delivery_uses_steer_not_queued_prompts(
    home, cst, _clean_subagents, monkeypatch
):
    """When the session is mid-turn (is_running=True), delivery goes through
    the active-recipient steer() branch, not queued_prompts.
    """
    import threading

    _register_in_process_transport()
    cst.register_lookup()

    manager = _manager(monkeypatch)
    state = manager.create_session(cwd="/tmp/work")
    state.is_running = True
    # MagicMock auto-creates attributes, but _pending_steer_lock must behave
    # like a real lock for the atomic-append helper.
    state.agent._pending_steer_lock = threading.Lock()
    state.agent._pending_steer = ""

    _spawn_subagent_record("sub-active", state.session_id, object())

    from tools.agent_messaging_transport_a import send_in_process

    sender = Participant(
        participant_id="sub-active",
        kind=ParticipantKind.SUBAGENT,
        owner_session_id=state.session_id,
    )
    recipient = Participant(
        participant_id=state.session_id,
        kind=ParticipantKind.SESSION,
        owner_session_id=state.session_id,
        session_origin=SessionOrigin.ACP,
    )

    send_in_process(sender=sender, recipient=recipient, body="hello mid-turn")

    assert state.queued_prompts == []
    assert "hello mid-turn" in state.agent._pending_steer


# ---------------------------------------------------------------------------
# Cross-process fallthrough still works (don't break existing behavior)
# ---------------------------------------------------------------------------


def test_unregistered_acp_session_falls_through_to_transport_b(home, cst):
    """A genuinely cross-process ACP recipient (never registered in this
    process, e.g. a different ACP daemon) must still resolve via Transport B.
    """
    _register_in_process_transport()
    cst.register_lookup()

    assert cst.heartbeat_registry(
        session_id="acp-other-process",
        name="other-acp-session",
        session_origin=SessionOrigin.ACP,
    )

    sender = Participant(
        participant_id="acp-sender",
        kind=ParticipantKind.SESSION,
        owner_session_id="acp-sender",
        session_origin=SessionOrigin.ACP,
    )
    resolution = resolve_transport(sender, "acp-other-process")

    assert resolution.kind is TransportKind.CROSS_PROCESS_DB
