"""The CLI spells out auto-resume when a delegate_task goes to the background.

A top-level ``delegate_task`` returns a handle immediately and runs the subagent
in the background; the result re-enters the conversation as a fresh turn when it
finishes. ``_on_tool_complete`` prints a one-line, no-spinner reassurance at
dispatch so the idle prompt doesn't read as "nothing happened".
"""

import json

import cli
from cli import HermesCLI


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._pending_edit_snapshots = {}
    return cli_obj


def _capture(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(cli, "_cprint", lambda text: printed.append(text))
    return printed


def test_background_dispatch_prints_resume_notice(monkeypatch):
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({"status": "dispatched", "mode": "background", "count": 1})
    cli_obj._on_tool_complete("tc1", "delegate_task", {"goal": "x"}, result)

    joined = "\n".join(printed)
    assert "resume" in joined.lower()
    assert "it finishes" in joined


def test_background_batch_dispatch_pluralizes(monkeypatch):
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({"status": "dispatched", "mode": "background", "count": 3})
    cli_obj._on_tool_complete("tc2", "delegate_task", {"tasks": []}, result)

    joined = "\n".join(printed)
    assert "3 tasks" in joined
    assert "they finish" in joined


def test_synchronous_delegate_result_prints_no_notice(monkeypatch):
    """A non-background result (e.g. the stateless sync fallback) must not claim
    a background dispatch."""
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({"results": [{"status": "completed", "summary": "done"}]})
    cli_obj._on_tool_complete("tc3", "delegate_task", {"goal": "x"}, result)

    assert not any("resume" in p.lower() for p in printed)


def test_non_delegate_tool_prints_no_notice(monkeypatch):
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    cli_obj._on_tool_complete("tc4", "read_file", {"path": "a"}, '{"ok": true}')

    assert not any("resume" in p.lower() for p in printed)


def test_background_dispatch_with_id_surfaces_stop_hint(monkeypatch):
    """When the dispatch payload carries a delegation_id, the notice must
    surface it plus a copy-pasteable /stop <id> cancel hint — otherwise the
    id only ever appeared once, in a line that scrolls out of view."""
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({
        "status": "dispatched", "mode": "background", "count": 1,
        "delegation_id": "deleg_abc123",
    })
    cli_obj._on_tool_complete("tc5", "delegate_task", {"goal": "x"}, result)

    joined = "\n".join(printed)
    assert "deleg_abc123" in joined
    assert "/stop deleg_abc123" in joined


def test_background_dispatch_without_id_omits_stop_hint(monkeypatch):
    """Older/malformed payloads without a delegation_id must not print a
    broken '/stop ' hint with a dangling empty id."""
    cli_obj = _make_cli()
    printed = _capture(monkeypatch)

    result = json.dumps({"status": "dispatched", "mode": "background", "count": 1})
    cli_obj._on_tool_complete("tc6", "delegate_task", {"goal": "x"}, result)

    joined = "\n".join(printed)
    assert "/stop" not in joined

