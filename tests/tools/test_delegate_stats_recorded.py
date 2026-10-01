"""A completed delegate_task child must leave a record in delegation_stats.json.

Regression: the record() call was lost when delegate_tool.py was split into siblings,
so the per-role usage log silently stopped growing.
"""
from types import SimpleNamespace

from hermes_cli import delegation_stats
from tools.delegate_tool_child_run import _record_delegation_stat


def test_record_delegation_stat_writes_a_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATION_STATS_DISABLED", raising=False)
    child = SimpleNamespace(max_iterations=10)
    entry = {
        "agent_type": "sr-coder", "model": "claude-opus-5-5", "status": "completed",
        "exit_reason": "completed", "duration_seconds": 12.5, "api_calls": 10,
        "tokens": {"input": 100, "output": 50}, "cost_usd": 0.25,
    }

    _record_delegation_stat(child, entry)

    rows = delegation_stats.load_all()
    assert len(rows) == 1
    assert rows[0].role == "sr-coder"
    assert rows[0].model == "claude-opus-5-5"
    assert rows[0].hit_max_iter is True
    assert rows[0].input_tokens == 100


def test_record_delegation_stat_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _record_delegation_stat(object(), {"tokens": "not-a-dict", "api_calls": object()})


def test_run_single_child_leaves_a_stats_row(tmp_path, monkeypatch):
    """The real completion path (not just the helper) must persist a record."""
    from unittest.mock import MagicMock

    from tools.delegate_tool import _run_single_child

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DELEGATION_STATS_DISABLED", raising=False)
    child = MagicMock()
    child._credential_pool = None
    child._delegate_agent_type = "reviewer"
    child.max_iterations = 50
    child.run_conversation.return_value = {
        "final_response": "done", "completed": True, "interrupted": False,
        "api_calls": 3, "messages": [],
    }
    parent = MagicMock()
    parent._delegate_depth = 0

    result = _run_single_child(task_index=0, goal="review it", child=child, parent_agent=parent)

    assert result["status"] == "completed"
    rows = delegation_stats.load_all()
    assert [r.role for r in rows] == ["reviewer"]
    assert rows[0].api_calls == 3
