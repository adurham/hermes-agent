"""A background process's completion belongs to the session that SPAWNED it.

Two sessions can hold the same gateway route key: a mid-turn compression leaves the
spawning session ``A`` ended and its continuation ``C`` live, a sibling child ``B`` of
the same conversation holds the same chat key, and every subagent of that chat inherits
the key too. Route-key equality therefore does NOT identify the owner, and a consumer
that falls back to it delivers another session's process completion into whichever
session holds the key.

The durable receipt path already proves ownership through the compression lineage
(``process_registry_results._owns_result``); the LIVE event carries no owner at all,
so the drain cannot apply the same proof. These tests pin the two-session contract.
"""

import json
import time

import pytest


def _make_db(tmp_path, monkeypatch, *, sessions):
    """A real state.db with ``sessions`` = [(id, parent_id, end_reason)]."""
    import hermes_state

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    db = hermes_state.SessionDB(db_path=db_path)
    for session_id, parent, end_reason in sessions:
        db.create_session(session_id, "local", parent_session_id=parent)
        if end_reason:
            db.end_session(session_id, end_reason=end_reason)
    db.close()
    return db_path


def _spawn_and_kill(registry, *, parent_session_id, session_key,
                    command="echo CROSS_SESSION; exec sleep 60"):
    """Spawn a real local process owned by *parent_session_id*, then kill it."""
    from gateway.session_context import scoped_current_session_id

    with scoped_current_session_id(parent_session_id):
        session = registry.spawn_local(
            command, cwd="/tmp", task_id=parent_session_id,
            owner_task_id=parent_session_id, session_key=session_key,
        )
        session.watcher_platform = "local"
        session.watcher_chat_id = "local-chat"
        session.notify_on_complete = True
    time.sleep(0.2)  # let the reader attach before the signal
    # consume_output=False is the bulk-cleanup shape (agent close / lost backend): the
    # killed process still owns a pending completion.
    registry.kill_process(session.id, source="process.kill", consume_output=False)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not _queued_completions(registry):
        time.sleep(0.05)
    assert _queued_completions(registry), "the killed process never published its completion"
    return session


def _queued_completions(registry):
    return [e for e in list(registry.completion_queue.queue) if e.get("type") == "completion"]


def test_sibling_session_does_not_consume_the_spawners_completion(tmp_path, monkeypatch):
    """SESSION B (same chat key, not the spawner) must not receive A's completion."""
    from gateway.session_context import scoped_current_session_id
    from tools.process_registry import ProcessRegistry

    chat_key = "agent:main:local:dm:local-deadbeef"
    # A spawned the process and was compressed mid-turn into C; B is a second child of
    # the same conversation and is NOT A's continuation (the tip is C).
    _make_db(tmp_path, monkeypatch, sessions=[
        ("sess-a", None, "compression"),
        ("sess-b", "sess-a", None),
        ("sess-c", "sess-a", None),
    ])
    registry = ProcessRegistry()
    session = _spawn_and_kill(registry, parent_session_id="sess-a", session_key=chat_key)

    # B drains the shared queue as itself: it holds the chat key, so today it consumes
    # the completion that names sess-a — a process it never started.
    with scoped_current_session_id("sess-b"):
        delivered = registry.drain_notifications(session_key=chat_key)

    assert delivered == [], (
        "session B consumed a completion for process %s, spawned by sess-a" % session.id
    )
    assert [e["session_id"] for e in _queued_completions(registry)] == [session.id], (
        "the completion must stay queued for its owner"
    )


def test_spawner_and_its_continuation_still_receive_the_completion(tmp_path, monkeypatch):
    """The spawner and its compression continuation DO own the completion."""
    from gateway.session_context import scoped_current_session_id
    from tools.process_registry import ProcessRegistry

    chat_key = "agent:main:local:dm:local-deadbeef"
    _make_db(tmp_path, monkeypatch, sessions=[
        ("sess-a", None, "compression"),
        ("sess-b", "sess-a", None),
        ("sess-c", "sess-a", None),
    ])
    registry = ProcessRegistry()
    session = _spawn_and_kill(registry, parent_session_id="sess-a", session_key=chat_key)

    # C is A's compression tip: the conversation's live continuation inherits the result.
    with scoped_current_session_id("sess-c"):
        delivered = registry.drain_notifications(session_key=chat_key)

    assert [evt["session_id"] for evt, _text in delivered] == [session.id], delivered
    assert "CROSS_SESSION" in delivered[0][1]
    assert registry.completion_queue.empty()


def test_receipt_is_scoped_to_the_spawner_and_its_tip(tmp_path, monkeypatch):
    """The durable receipt agrees: the tip reads it, a sibling does not."""
    from gateway.session_context import scoped_current_session_id
    from hermes_constants import get_hermes_home
    from tools.process_registry import ProcessRegistry
    from tools.process_registry_results import load_completed_results

    chat_key = "agent:main:local:dm:local-deadbeef"
    _make_db(tmp_path, monkeypatch, sessions=[
        ("sess-a", None, "compression"),
        ("sess-b", "sess-a", None),
        ("sess-c", "sess-a", None),
    ])
    registry = ProcessRegistry()
    session = _spawn_and_kill(registry, parent_session_id="sess-a", session_key=chat_key)
    receipt = get_hermes_home() / "logs" / "process-results" / f"{session.id}.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not receipt.exists():
        time.sleep(0.02)
    assert json.loads(receipt.read_text())["parent_session_id"] == "sess-a"

    with scoped_current_session_id("sess-b"):
        assert load_completed_results() == {}
    with scoped_current_session_id("sess-c"):
        assert session.id in load_completed_results()


@pytest.mark.parametrize("drainer,key", [
    ("sess-b", "agent:main:local:dm:local-deadbeef"),
    ("sess-stranger", "agent:main:local:dm:local-cafe"),
])
def test_completion_naming_its_spawner_is_never_ownerless(tmp_path, monkeypatch, drainer, key):
    """A completion that names its spawner is never consumed without proof of ownership."""
    from gateway.session_context import scoped_current_session_id
    from tools.process_registry import ProcessRegistry

    _make_db(tmp_path, monkeypatch, sessions=[
        ("sess-a", None, "compression"),
        ("sess-b", "sess-a", None),
        ("sess-c", "sess-a", None),
    ])
    registry = ProcessRegistry()
    session = _spawn_and_kill(
        registry, parent_session_id="sess-a", session_key="agent:main:local:dm:local-deadbeef")
    with scoped_current_session_id(drainer):
        delivered = registry.drain_notifications(session_key=key)
    assert delivered == [], (drainer, session.id)
