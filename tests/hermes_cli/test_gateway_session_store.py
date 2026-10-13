"""GatewaySessionStore: main's ``_session_db`` facade over gateway verbs (scripted gateway, no live one)."""
import asyncio
import types
from contextlib import asynccontextmanager

import pytest

from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.gateway_connection import GatewayConnection
from hermes_cli.gateway_session_store import GatewaySessionStore

INFO = {"model": "fake-model", "cwd": "/w", "context_length": 128000, "enabled_toolsets": ["file"],
        "tools": {"file": ["read_file", "write_file"]}, "skills": {"devops": ["sdlc-review"]},
        "mcp_servers": [], "tool_availability": {"available_toolsets": ["file"], "unavailable_toolsets": [],
                                                 "lazy_tools": [], "disabled_tools": []}}


def snapshot(sid, revision=3):
    return {"session_id": sid, "stored_session_id": sid, "messages": [], "revision": revision,
            "execution_generation": 0, "subscription_id": "sub-" + sid, "info": dict(INFO)}


class FakeClient:
    def __init__(self, known=("local-old",)):
        self.events = asyncio.Queue()
        self.calls = []
        self.known = set(known)

    async def rpc(self, method, *, _timeout=None, **params):
        self.calls.append((method, params))
        if method == "session.create":
            self.known.add("local-new")
            return snapshot("local-new")
        if method == "session.resume":
            if params["session_id"] not in self.known:
                raise GatewayClientError("not_found")
            return snapshot(params["session_id"])
        return {"status": "ok", "detached": True}


@pytest.fixture
def store():
    client = FakeClient()

    @asynccontextmanager
    async def connect():
        yield client
    conn = GatewayConnection(connect=connect)
    conn.start(5)
    yield GatewaySessionStore(conn), client
    conn.close()


def cli(session_id="20261012_000000_abcdef", resumed=False):
    return types.SimpleNamespace(session_id=session_id, _resumed=resumed)


def test_new_session_is_created_on_the_gateway_and_its_id_adopted(store, monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    db, client = store
    sid = db.open_session(cli(), {"model": "m1", "toolsets": ["file", "web"], "provider": None})
    assert sid == "local-new"
    method, params = client.calls[0]
    assert method == "session.create"
    assert params["model"] == "m1" and params["toolsets"] == ["file", "web"] and params["source"] == "cli"
    assert "provider" not in params and params["cwd"]
    assert db.connection.state("local-new").info["model"] == "fake-model"


def test_resume_attaches_and_unknown_target_is_left_to_mains_not_found_path(store):
    db, client = store
    assert db.open_session(cli("local-old", resumed=True)) == "local-old"
    assert client.calls[-1][0] == "session.resume"
    assert db.open_session(cli("missing", resumed=True)) is None


def test_title_rename_delete_and_detach_are_gateway_verbs(store):
    db, client = store
    db.open_session(cli("local-old", resumed=True))
    db.set_session_title("local-old", "Some Title")
    mutate = [p for m, p in client.calls if m == "session.mutate"][-1]
    assert mutate["operation"] == "rename" and mutate["payload"] == {"title": "Some Title"}
    assert mutate["expected_revision"] == 3
    assert db.connection.state("local-old").title == "Some Title"
    db.end_session("local-old", "cli_close")
    assert client.calls[-1] == ("session.detach", {"session_id": "local-old", "subscription_id": "sub-local-old"})
    db.delete_session("local-old")
    assert [p for m, p in client.calls if m == "session.mutate"][-1]["operation"] == "delete"


def test_gateway_owned_writes_are_noops_and_unknown_writes_refuse(store):
    db, client = store
    before = len(client.calls)
    db.create_session("x", source="cli")
    db.reopen_session("x")
    db.append_messages_batch("x", [])
    assert db.prune_empty_ghost_sessions() == 0
    assert len(client.calls) == before
    with pytest.raises(ValueError):
        db.rewind_user_turn("x", 1)


def test_banner_inventory_comes_from_the_session_snapshot(store):
    db, _ = store
    assert db.banner_inventory() is None
    db.open_session(cli("local-old", resumed=True))
    inv = db.banner_inventory()
    assert [t["function"]["name"] for t in inv["tools"]] == ["read_file", "write_file"]
    assert inv["get_toolset_for_tool"]("read_file") == "file"
    assert inv["skills_by_category"] == {"devops": ["sdlc-review"]}
    assert inv["context_length"] == 128000 and inv["cwd"] == "/w"


def test_store_is_falsy_once_the_gateway_connection_closed(store):
    db, _ = store
    assert db
    db.connection.close()
    assert not db
