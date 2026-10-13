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


def test_a_finite_run_creates_mains_oneshot_session_so_bare_c_finds_it(store, monkeypatch):
    """Main labels a `chat -q` / `-Q` session ``oneshot`` (agent.session_source.session_source_for), which
    `hermes -c` / `--resume latest` count as CLI history; created as ``cli`` the owner stores the
    transport's ``local`` and the next bare `-c` finds nothing."""
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE_EXPLICIT", raising=False)
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    db, client = store
    db.open_session(cli(), {})
    assert client.calls[-1][1]["source"] == "oneshot"
    # An explicit `--source tool` still wins, and an interactive session stays `cli`.
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")
    monkeypatch.setenv("HERMES_SESSION_SOURCE_EXPLICIT", "1")
    GatewaySessionStore(db.connection).open_session(cli(), {})
    assert client.calls[-1][1]["source"] == "tool"
    for name in ("HERMES_SINGLE_QUERY_SESSION", "HERMES_SESSION_SOURCE", "HERMES_SESSION_SOURCE_EXPLICIT"):
        monkeypatch.delenv(name)
    GatewaySessionStore(db.connection).open_session(cli(), {})
    assert client.calls[-1][1]["source"] == "cli"


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


def test_reader_opens_once_the_gateway_created_state_db(store, tmp_path, monkeypatch):
    """The startup maintenance probe runs before the gateway exists; that miss is not latched."""
    db, _ = store
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    assert db.get_session("x") is None and db._reader is None
    from hermes_state import SessionDB
    SessionDB(db_path=tmp_path / "state.db").close()
    assert db.get_session("x") is None and db._reader is not None


def test_banner_session_label_is_mains_eight_characters_for_a_canonical_id():
    from hermes_cli.gateway_session_store import session_label
    assert session_label("local-ab12cd34" + "0" * 56) == "ab12cd34"
    assert session_label("20261012_134501_abcdef") == "20261012_134501_abcdef"
    assert session_label("local-abc") == "local-abc"
    assert session_label(None) is None


def test_cli_startup_notices_render_from_the_snapshot(store, monkeypatch):
    """Main's tool-availability warning and browser notice print from the gateway snapshot (the
    gateway resolved the session's tools); the CLI process enumerates nothing."""
    from types import SimpleNamespace

    from hermes_cli.cli_info_mixin import CLIInfoMixin
    from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
    db, _ = store
    db.open_session(cli("local-old", resumed=True))
    db.current["info"].update(tool_availability_warnings=["[yellow]⚠ terminal off[/]"],
                              browser_backend_notice="browser-harness is missing")
    printed = []
    host = SimpleNamespace(_session_db=db, _console_print=lambda *a: printed.append(a[0] if a else ""),
                           _show_browser_backend_notice=lambda: printed.append("LOCAL"))
    CLITuiRuntimeMixin._show_startup_browser_notice(host)
    assert printed == ["[yellow]⚠ browser-harness is missing[/yellow]"]
    monkeypatch.setattr("cli._build_compact_banner", lambda: "compact", raising=False)
    banner = SimpleNamespace(**vars(host), console=SimpleNamespace(clear=lambda: None), compact=True,
                             session_id="local-old", _show_status=lambda: None, agent=None,
                             _show_tool_availability_warnings=lambda: printed.append("LOCAL-WARN"))
    banner._print_gateway_tool_warnings = lambda lines: CLIInfoMixin._print_gateway_tool_warnings(banner, lines)
    printed.clear()
    CLIInfoMixin.show_banner(banner)
    assert "[yellow]⚠ terminal off[/]" in printed and "LOCAL-WARN" not in printed
