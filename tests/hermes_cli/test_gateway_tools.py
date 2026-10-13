"""Main's process dock, status-bar ``⚙ N`` and ``/stop`` read the gateway session's processes.

Rewired, ``terminal(background=true)`` runs in the gateway, so the CLI process's own
``tools.process_registry`` is empty. ``hermes_cli.gateway_tools`` answers main's registry calls from
``process.list`` / ``process.kill`` / ``process.stop`` (scripted fake gateway, no live gateway).
"""
import asyncio
import time
from contextlib import asynccontextmanager

import pytest

from hermes_cli import gateway_tools
from hermes_cli.gateway_agent import GatewayAgent
from hermes_cli.gateway_connection import GatewayConnection

SID = "local-abc"


class FakeClient:
    def __init__(self):
        self.events = asyncio.Queue()
        self.calls = []
        now = time.time()
        self.processes = [
            {"session_id": "proc_1", "command": "sleep 300", "status": "running", "exit_code": None,
             "started_epoch": now - 5, "exited_at": None, "completion_reason": None,
             "output_tail": "tick 1\ntick 2\n"},
            {"session_id": "proc_2", "command": "echo hi", "status": "exited", "exit_code": 0,
             "started_epoch": now - 9, "exited_at": now - 3, "completion_reason": "exited",
             "output_tail": "hi\n"},
        ]

    async def rpc(self, method, *, _timeout=None, **params):
        self.calls.append((method, params))
        if method == "process.list":
            return {"processes": self.processes}
        if method == "process.stop":
            for row in self.processes:
                if row["status"] == "running":
                    row.update(status="exited", exited_at=time.time(), completion_reason="killed", exit_code=-15)
            return {"killed": 1, "delegations": 0}
        if method == "process.kill":
            return {"status": "killed", "session_id": params["process_id"]}
        return {}


@pytest.fixture
def gateway():
    client = FakeClient()

    @asynccontextmanager
    async def connect():
        yield client
    conn = GatewayConnection(connect=connect)
    conn.start(5)
    conn.state(SID)
    agent = GatewayAgent(connection=conn, session_id=SID)
    gateway_tools.bind(agent)
    yield agent, client
    conn.close()
    gateway_tools._view = None


def test_dock_rows_come_from_the_gateway_session(gateway):
    from hermes_cli import cli_process_dock as dock
    _agent, client = gateway
    rows = dock.process_rows()
    assert [(r["id"], r["status"]) for r in rows] == [("proc_1", "running"), ("proc_2", "done")]
    assert rows[0]["command"] == "sleep 300" and rows[0]["detail"] == "tick 2" and rows[0]["elapsed"] >= 4
    assert rows[1]["exit_code"] == 0 and rows[1]["since_exit"] >= 2
    assert ("process.list", {"session_id": SID}) in client.calls
    assert dock.process_tail("proc_1") == "tick 1\ntick 2\n"


def test_status_bar_count_reads_the_cached_gateway_count(gateway):
    _agent, client = gateway
    gateway_tools.process_registry().list_sessions()
    polls = len(client.calls)
    assert gateway_tools.process_registry().count_running() == 1
    assert len(client.calls) == polls  # the render thread never waits on an RPC


def test_stop_kills_the_session_processes_through_the_gateway(gateway, capsys):
    from hermes_cli.cli_commands_session_tools import CLICommandsSessionToolsMixin
    _agent, client = gateway
    CLICommandsSessionToolsMixin._handle_stop_command(object())
    out = capsys.readouterr().out
    assert "Stopping 1 background process(es)..." in out and "Stopped 1 process(es)." in out
    assert any(method == "process.stop" for method, _ in client.calls)


def test_dock_stop_kills_one_process_through_the_gateway(gateway):
    from hermes_cli import cli_process_dock as dock
    _agent, client = gateway
    dock.process_rows()
    assert dock.kill("proc_1")["status"] == "killed"
    assert ("process.kill", {"session_id": SID, "process_id": "proc_1"}) in client.calls
