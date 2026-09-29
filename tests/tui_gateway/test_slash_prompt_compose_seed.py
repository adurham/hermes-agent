"""Worker-routed slash commands that queue a next-turn prompt must actually send it.

The persistent slash worker runs ``HermesCLI.process_command`` headlessly. Commands
like ``/prompt``/``/compose`` (and ``/blueprint``) park the composed prompt on the
one-shot ``_pending_agent_seed`` for the interactive REPL loop to run — but the worker
has no REPL loop, so the seed was silently dropped: Desktop/TUI ``/prompt`` returned
``(no output)`` and the composed prompt vanished (#107800). The worker must harvest the
seed and the gateway must route it back as a ``{type: "send"}`` dispatch, which both the
Desktop and TUI clients already handle.
"""

from __future__ import annotations

import threading

from tui_gateway import server


class _FakeCLI:
    """Records the command; simulates /prompt parking the composed text on the seed."""
    console = None

    def __init__(self, seed=None):
        self._pending_agent_seed = seed
        self.commands = []

    def process_command(self, cmd):
        self.commands.append(cmd)


def _live_session():
    return {"session_key": "seed-key", "running": False, "history": [],
            "history_lock": threading.Lock(), "agent": None, "cwd": ""}


def test_run_harvests_pending_agent_seed():
    from tui_gateway import slash_worker

    cli = _FakeCLI(seed="Composed in $EDITOR")
    result = slash_worker._run(cli, "/prompt")
    assert result["output"] == ""
    assert result["seed"] == "Composed in $EDITOR"
    # one-shot: the seed is consumed, a second command must not re-send it
    assert cli._pending_agent_seed is None
    result2 = slash_worker._run(cli, "/status")
    assert result2["seed"] == ""


def test_run_without_seed_returns_empty_seed():
    from tui_gateway import slash_worker

    result = slash_worker._run(_FakeCLI(), "/status")
    assert result == {"output": "", "seed": ""} or (result["output"] and result["seed"] == "")


def test_empty_command_returns_empty_output_and_seed():
    from tui_gateway import slash_worker

    result = slash_worker._run(_FakeCLI(), "")
    assert result == {"output": "", "seed": ""}


def test_slash_exec_routes_seed_as_send_dispatch(monkeypatch):
    """slash.exec must return {type: "send", message: <seed>} when the worker harvested one."""
    sid = "seed-sid"
    session = _live_session()
    server._sessions[sid] = session

    class _SeedingWorker:
        def __init__(self, *a, **kw):
            pass

        def run(self, command):
            return ("", "Composed in $EDITOR")

        def close(self):
            pass

    monkeypatch.setattr(server, "_SlashWorker", _SeedingWorker)
    try:
        resp = server._methods["slash.exec"](1, {"session_id": sid, "command": "/prompt"})
    finally:
        server._sessions.pop(sid, None)

    assert "error" not in resp, resp
    assert resp["result"]["type"] == "send"
    assert resp["result"]["message"] == "Composed in $EDITOR"


def test_slash_exec_plain_output_unchanged_when_no_seed(monkeypatch):
    sid = "seed-sid-plain"
    session = _live_session()
    server._sessions[sid] = session

    class _PlainWorker:
        def __init__(self, *a, **kw):
            pass

        def run(self, command):
            return ("worker says hi", "")

        def close(self):
            pass

    monkeypatch.setattr(server, "_SlashWorker", _PlainWorker)
    try:
        resp = server._methods["slash.exec"](1, {"session_id": sid, "command": "/journey"})
    finally:
        server._sessions.pop(sid, None)

    assert "error" not in resp, resp
    assert resp["result"]["output"] == "worker says hi"
    assert "type" not in resp["result"]
