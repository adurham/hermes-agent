"""A managed worker turn (every Kanban card) is never offered ``delegate_task`` on purpose
(ef52a09a558: a child needs an owner-registered execution the worker cannot reserve). Asking
for it must explain that boundary and name the supported alternative, never answer "call it
directly" for a tool this turn cannot see (#141793)."""
import json

import pytest

import tools.delegate_tool  # registers delegate_task in the global registry
from agent import runtime_session_store
from agent.conversation_loop import _invalid_tool_name_error_content
from tools.tool_search import ToolSearchConfig, dispatch_tool_describe, resolve_underlying_call


@pytest.fixture
def worker_process(monkeypatch):
    monkeypatch.setattr(runtime_session_store, "_worker_process", True)


def test_bridge_explains_the_managed_worker_boundary_for_delegate_task(worker_process):
    worker_tools = [{"type": "function", "function": {"name": "kanban_create", "parameters": {}}}]
    _, _, call_err = resolve_underlying_call({"calls": [{"name": "delegate_task", "arguments": {}}]},
                                             {"kanban_create"})
    described = json.loads(dispatch_tool_describe({"names": ["delegate_task"]}, current_tool_defs=worker_tools,
                                                  config=ToolSearchConfig.from_raw({})))
    direct = _invalid_tool_name_error_content("delegate_task", ["kanban_create"])
    for message in (call_err or "", described["errors"]["delegate_task"], direct):
        assert "managed worker" in message and "kanban_create" in message, message
        assert "Call it directly" not in message and "does not exist" not in message, message


def test_outside_a_worker_delegate_task_keeps_the_ordinary_guidance(monkeypatch):
    monkeypatch.setattr(runtime_session_store, "_worker_process", False)
    _, _, err = resolve_underlying_call({"calls": [{"name": "delegate_task", "arguments": {}}]},
                                        {"delegate_task"})
    assert "Call it directly" in (err or "")
    assert "managed worker" not in _invalid_tool_name_error_content("delegate_task", ["todo"])
