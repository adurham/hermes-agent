"""A grandchild's process completion must not wake the human's chat.

The defect this pins (live, 2026-10-27): session PS5 -> subagent PM -> subagent "Static census"
(grandchild). The grandchild ran a background ``gdb`` census whose completion was injected into
the human's chat as a full turn ("[IMPORTANT: Background process proc_... completed ...
Started by subagent sa-1-..."). The parent agent had nothing to act on, closed with
"No change to report.", and the human read that as the answer to their own next question.

The delivery contract that produced it is deliberate (held-then-released child completions
deliver with attribution, FORK.md v0.21.7 sync) and stays intact for DIRECT children. The nested
class is excluded: the owner's own parent subagent is its consumer (its result already lists the
completion as ``unread_completions``, and the durable receipt keeps the full output), so the
human's chat is not woken for it.
"""

import queue

import pytest

from tools.process_registry import ProcessRegistry
from tools.process_registry_notifications import (
    held_delivery_exempt, is_nested_child_notification, should_surface_notification)

HUMAN_SESSION = "local-human"
SUBAGENT_SESSION = "20261027_001245_92302e"     # the grandchild that spawned the process
PM_SESSION = "20261027_001104_02dddb"           # its parent subagent


def _child_event(*, parent_session_id, owner="sa-1-16fcedc3", held=True):
    evt = {
        "type": "completion",
        "owner_task_id": owner,
        "task_id": f"session:agent:main:local:dm:{HUMAN_SESSION}",
        "session_key": "parent-key",
        "session_id": "proc_81835a06acf5",
        "command": "gdb -q -batch -ex 'ptype /o WebCore::CSSProperty'",
        "exit_code": 0,
        "output": "struct WebCore::CSSProperty { ... }",
    }
    if parent_session_id is not None:
        evt["parent_session_id"] = parent_session_id
    if held:
        evt["_event_held_at_monotonic"] = 1.0
    return evt


@pytest.fixture()
def sessions(monkeypatch):
    """Hermetic session store: ``{session_id: row}``; unknown ids miss like a real store."""
    rows = {
        HUMAN_SESSION: {"id": HUMAN_SESSION, "source": "local", "parent_session_id": None},
        PM_SESSION: {"id": PM_SESSION, "source": "subagent", "parent_session_id": HUMAN_SESSION,
                     "model_config": {"_delegate_from": HUMAN_SESSION}},
        # A delegated child's spawner: ``spawner_lineage`` stays undecided for it (its events route
        # by the delegation path, never by lineage), exactly as the live row is stamped.
        SUBAGENT_SESSION: {"id": SUBAGENT_SESSION, "source": "subagent", "parent_session_id": PM_SESSION,
                           "model_config": {"_delegate_from": PM_SESSION}},
    }
    closed = []

    class _FakeSessionDB:
        def __init__(self, *args, **kwargs):
            pass

        def get_session(self, session_id):
            row = rows.get(session_id)
            return dict(row) if row else None

        def get_compression_tip(self, session_id):
            return session_id

        def close(self):
            closed.append(True)

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _FakeSessionDB)
    return rows


@pytest.fixture()
def registry():
    return ProcessRegistry()


@pytest.fixture()
def _quiet_registry(registry, monkeypatch):
    """A drain that never touches the real ledger and never opts into child notices."""
    monkeypatch.setattr(registry, "_completions_restored", True)
    monkeypatch.setattr(registry, "_surface_child_process_notifications", lambda: False)
    monkeypatch.setattr(registry, "completion_queue", queue.Queue())
    # No live owner: the liveness gate must not hold, so the delivery gate is what decides.
    monkeypatch.setattr("tools.process_registry.event_owner_still_running", lambda evt: False)
    return registry


def _drain(reg):
    return reg.drain_notifications(session_key="parent-key", session_id=HUMAN_SESSION)


# ── the predicate ────────────────────────────────────────────────────────────


def test_grandchild_completion_is_nested(sessions):
    assert is_nested_child_notification(_child_event(parent_session_id=SUBAGENT_SESSION)) is True


def test_direct_child_completion_is_not_nested(sessions):
    """A depth-1 child's spawner session IS the human's chat."""
    assert is_nested_child_notification(_child_event(parent_session_id=HUMAN_SESSION)) is False


def test_unknown_or_absent_spawner_fails_open(sessions):
    """A store miss must never silence a real completion."""
    assert is_nested_child_notification(_child_event(parent_session_id="gone-session")) is False
    assert is_nested_child_notification(_child_event(parent_session_id=None)) is False
    assert is_nested_child_notification({"type": "completion", "task_id": "proc_1"}) is False


def test_delegation_events_are_never_nested(sessions):
    evt = {"type": "async_delegation", "owner_task_id": "sa-1-x", "parent_session_id": SUBAGENT_SESSION}
    assert is_nested_child_notification(evt) is False
    assert held_delivery_exempt(evt) is False


def test_held_exemption_excludes_only_the_nested_class(sessions):
    nested = _child_event(parent_session_id=SUBAGENT_SESSION)
    direct = _child_event(parent_session_id=HUMAN_SESSION)
    assert held_delivery_exempt(nested) is False
    assert held_delivery_exempt(direct) is True
    assert held_delivery_exempt(_child_event(parent_session_id=HUMAN_SESSION, held=False)) is False


# ── the drain (interactive CLI / oneshot) ────────────────────────────────────


def test_drain_suppresses_a_released_grandchild_completion(sessions, _quiet_registry):
    """The live defect: held at first sight, released when the owner exited -> delivered."""
    evt = _child_event(parent_session_id=SUBAGENT_SESSION)
    _quiet_registry.completion_queue.put(evt)

    assert _drain(_quiet_registry) == []
    assert _quiet_registry.completion_queue.empty()  # dropped, never requeued (no consumer would take it)
    assert should_surface_notification(evt) is False


def test_drain_still_delivers_a_released_direct_child_completion(sessions, _quiet_registry):
    """The fork's orphan path (depth 1) keeps delivering with attribution."""
    evt = _child_event(parent_session_id=HUMAN_SESSION)
    _quiet_registry.completion_queue.put(evt)

    results = _drain(_quiet_registry)
    assert len(results) == 1
    raw, text = results[0]
    assert raw == evt
    assert "Started by subagent sa-1-16fcedc3" in text


# ── the gateway delivery-point admission ─────────────────────────────────────


@pytest.mark.asyncio
async def test_gateway_admission_refuses_a_nested_completion(sessions, monkeypatch):
    """Even a watcher that SAW the process running must not wake the chat for a grandchild."""
    from gateway.run_notifications import GatewayNotificationsMixin

    import contextlib

    runner = GatewayNotificationsMixin()
    runner._completion_event_scope = lambda evt: contextlib.nullcontext()

    evt = _child_event(parent_session_id=SUBAGENT_SESSION, held=False)
    assert await runner._released_completion_admitted(evt, seen_running=True) is False
    assert await runner._released_completion_admitted(evt, seen_running=False) is False

    direct = _child_event(parent_session_id=HUMAN_SESSION, held=False)
    assert await runner._released_completion_admitted(direct, seen_running=True) is True
