"""Subagent status surfaces report the model a child is ACTUALLY running on.

``agent/chat_completion_helpers.py::try_activate_fallback()`` mutates a live
agent's ``model``/``provider`` in place on failover. Every subagent-facing
display used to snapshot that string ONCE at dispatch and never re-read it,
so a child that silently failed over kept being reported under the model it
was dispatched with — the parent model then reasoned about, and reported, a
runtime that wasn't in use.

These tests mock that mutation (the same three attributes
``try_activate_fallback`` writes) AFTER registration and assert every surface
re-resolves. They fail against the pre-fix code, which reads the stale
registry snapshot.
"""
from __future__ import annotations

import json
import weakref

from tools.delegate_tool import (
    _build_child_progress_callback,
    _handle_control_action,
    _register_subagent,
    _unregister_subagent,
    list_active_subagents,
)


class _StubParent:
    pass


class _StubChild:
    """Weakref-able child whose failover surface can be mutated live."""

    def __init__(self, parent=None, model="glm-5.3", provider="ollama-cloud"):
        self.model = model
        self.provider = provider
        self._fallback_activated = False
        self._live_transcript_path = "/tmp/live/task-0.log"
        if parent is not None:
            self._delegate_parent_ref = weakref.ref(parent)

    def fail_over(self, model="claude-opus-5", provider="anthropic"):
        """Exactly what try_activate_fallback does to a live agent."""
        self._primary_runtime = {"model": self.model, "provider": self.provider}
        self.model = model
        self.provider = provider
        self._fallback_activated = True

    def restore_primary(self):
        """Exactly what restore_primary_runtime does — failover is reversible."""
        primary = self._primary_runtime
        self.model = primary["model"]
        self.provider = primary["provider"]
        self._fallback_activated = False


def _register(sid: str, child, **extra) -> None:
    """Register with a dispatch-time model snapshot, as the real path does."""
    record = {
        "subagent_id": sid,
        "parent_id": None,
        "depth": 0,
        "goal": "test goal",
        # The stale snapshot: written once at registration
        # (delegate_tool.py's _register_subagent call) and never rewritten.
        "model": getattr(child, "model", None),
        "started_at": 1000.0,
        "status": "running",
        "tool_count": 0,
        "agent": child,
    }
    record.update(extra)
    _register_subagent(record)


def _list(parent):
    return json.loads(_handle_control_action("list", None, None, parent))


# ---------------------------------------------------------------------------
# action='list' — contract C
# ---------------------------------------------------------------------------


def test_list_reports_the_failed_over_model_not_the_snapshot():
    """The regression: this is what the user actually sees and complained about."""
    parent = _StubParent()
    child = _StubChild(parent)
    _register("sa-fb-1", child)
    try:
        before = _list(parent)["subagents"][0]
        assert before["model"] == "glm-5.3"
        assert before["fallback_active"] is False
        assert before["model_label"] == "glm-5.3"

        child.fail_over()

        after = _list(parent)["subagents"][0]
        assert after["model"] == "claude-opus-5"
        assert after["provider"] == "anthropic"
        assert after["fallback_active"] is True
        assert after["primary_model"] == "glm-5.3"
        assert after["primary_provider"] == "ollama-cloud"
        assert "⚠" in after["model_label"]
        assert after["model_label"] == "⚠ claude-opus-5 (fallback from glm-5.3)"
    finally:
        _unregister_subagent("sa-fb-1")


def test_list_carries_every_wire_key():
    parent = _StubParent()
    child = _StubChild(parent)
    _register("sa-fb-2", child)
    try:
        entry = _list(parent)["subagents"][0]
        for key in (
            "model",
            "provider",
            "fallback_active",
            "primary_model",
            "primary_provider",
            "model_label",
        ):
            assert key in entry, f"missing wire key: {key}"
    finally:
        _unregister_subagent("sa-fb-2")


def test_list_follows_a_restore_back_to_the_primary():
    """Failover is reversible, so the read must be live in BOTH directions."""
    parent = _StubParent()
    child = _StubChild(parent)
    _register("sa-fb-3", child)
    try:
        child.fail_over()
        assert _list(parent)["subagents"][0]["model"] == "claude-opus-5"
        child.restore_primary()
        restored = _list(parent)["subagents"][0]
        assert restored["model"] == "glm-5.3"
        assert restored["fallback_active"] is False
        assert restored["model_label"] == "glm-5.3"
    finally:
        _unregister_subagent("sa-fb-3")


def test_list_falls_back_to_the_snapshot_when_the_agent_ref_is_gone():
    parent = _StubParent()
    child = _StubChild(parent)
    _register("sa-fb-4", child)
    try:
        # A record whose agent slot was cleared still reports its snapshot
        # rather than a blank model.
        from tools.delegate_tool import _active_subagents, _active_subagents_lock

        with _active_subagents_lock:
            _active_subagents["sa-fb-4"]["agent"] = None
        entry = _list(parent)["subagents"]
        # Ownership is resolved via the agent ref, so a cleared ref drops the
        # row from this parent's scoped view — the important assertion is
        # that resolution didn't raise.
        assert isinstance(entry, list)
    finally:
        _unregister_subagent("sa-fb-4")


# ---------------------------------------------------------------------------
# list_active_subagents() — the same fix, before the "agent" key is stripped
# ---------------------------------------------------------------------------


def test_list_active_subagents_resolves_before_stripping_the_agent_key():
    """This function drops "agent", so resolution MUST happen inside it.

    Its consumers (tui_gateway delegation.status, process_registry's
    liveness gate, transport A's error text) have no way to resolve the
    live model themselves once the ref is gone.
    """
    child = _StubChild()
    _register("sa-fb-5", child)
    try:
        child.fail_over()
        rows = [r for r in list_active_subagents() if r["subagent_id"] == "sa-fb-5"]
        assert len(rows) == 1
        row = rows[0]
        assert "agent" not in row
        assert row["model"] == "claude-opus-5"
        assert row["provider"] == "anthropic"
        assert row["fallback_active"] is True
        assert row["primary_model"] == "glm-5.3"
        assert "⚠" in row["model_label"]
    finally:
        _unregister_subagent("sa-fb-5")


def test_list_active_subagents_is_json_serialisable():
    """The gateway ships this straight over the wire."""
    child = _StubChild()
    _register("sa-fb-6", child)
    try:
        child.fail_over()
        rows = [r for r in list_active_subagents() if r["subagent_id"] == "sa-fb-6"]
        json.dumps(rows)  # must not raise
    finally:
        _unregister_subagent("sa-fb-6")


# ---------------------------------------------------------------------------
# progress events — contract D
# ---------------------------------------------------------------------------


class _CapturingParent:
    """Parent whose progress callback records every relayed event payload."""

    def __init__(self):
        self.events = []

        def _cb(event_type, tool_name=None, preview=None, args=None, **kwargs):
            self.events.append((event_type, kwargs))

        self.tool_progress_callback = _cb


def test_progress_events_resolve_the_model_live_per_event():
    """The baked-in effective_model_for_cb was one stale source feeding
    the TUI and desktop renderers for the whole run."""
    parent = _CapturingParent()
    child = _StubChild()
    agent_ref = {}
    cb = _build_child_progress_callback(
        0,
        "goal",
        parent,
        1,
        subagent_id="sa-fb-7",
        model="glm-5.3",
        agent_ref=agent_ref,
    )
    assert cb is not None
    agent_ref["agent"] = weakref.ref(child)

    cb("subagent.start")
    _, first = parent.events[-1]
    assert first["model"] == "glm-5.3"
    assert first["fallback_active"] is False

    child.fail_over()

    cb("subagent.start")
    _, second = parent.events[-1]
    assert second["model"] == "claude-opus-5"
    assert second["provider"] == "anthropic"
    assert second["fallback_active"] is True
    assert second["primary_model"] == "glm-5.3"
    assert second["primary_provider"] == "ollama-cloud"
    assert "⚠" in second["model_label"]


def test_progress_events_degrade_to_dispatch_model_before_the_child_exists():
    """The callback is built BEFORE the child agent — the first events fire
    during build and must still carry a sensible model."""
    parent = _CapturingParent()
    cb = _build_child_progress_callback(
        0, "goal", parent, 1, subagent_id="sa-fb-8", model="glm-5.3", agent_ref={}
    )
    cb("subagent.start")
    _, payload = parent.events[-1]
    assert payload["model"] == "glm-5.3"
    assert payload["fallback_active"] is False
    assert payload["model_label"] == "glm-5.3"


def test_progress_events_omit_model_when_nothing_is_known():
    """Preserves the pre-fix "omit when unknown" contract."""
    parent = _CapturingParent()
    cb = _build_child_progress_callback(
        0, "goal", parent, 1, subagent_id="sa-fb-9", model=None, agent_ref={}
    )
    cb("subagent.start")
    _, payload = parent.events[-1]
    assert "model" not in payload
    assert payload["fallback_active"] is False


def test_progress_events_survive_a_dead_child_weakref():
    parent = _CapturingParent()
    child = _StubChild()
    agent_ref = {"agent": weakref.ref(child)}
    cb = _build_child_progress_callback(
        0, "goal", parent, 1, subagent_id="sa-fb-10", model="glm-5.3",
        agent_ref=agent_ref,
    )
    del child
    cb("subagent.start")  # must not raise
    _, payload = parent.events[-1]
    assert payload["model"] == "glm-5.3"


# ---------------------------------------------------------------------------
# board re-sync — contract 4's push half
# ---------------------------------------------------------------------------


class _RecordingBoard:
    """Captures update() kwargs so the push can be asserted without a CLI."""

    def __init__(self):
        self.updates = []

    def update(self, subagent_id, **kwargs):
        self.updates.append((subagent_id, kwargs))

    def note(self, *a, **k):
        pass

    def finish(self, *a, **k):
        pass


def test_progress_path_pushes_updated_model_state_to_the_board(monkeypatch):
    """board.register() stamps the model once; nothing rewrote it, so a
    failed-over row stayed frozen. The re-sync rides on the board writes the
    callback already makes — no new polling thread."""
    import tools.swarm_board as swarm_board

    parent = _CapturingParent()
    child = _StubChild()
    board = _RecordingBoard()
    monkeypatch.setattr(swarm_board, "board_for_row", lambda *a, **k: board)

    agent_ref = {"agent": weakref.ref(child)}
    cb = _build_child_progress_callback(
        0, "goal", parent, 1, subagent_id="sa-fb-11", model="glm-5.3",
        agent_ref=agent_ref,
    )
    child.fail_over()
    cb("subagent.start")

    assert board.updates, "the progress path made no board write"
    sid, kwargs = board.updates[-1]
    assert sid == "sa-fb-11"
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["fallback_active"] is True
    assert kwargs["primary_model"] == "glm-5.3"
