"""The classic CLI's ``/queue`` goes into the gateway's durable FIFO (refs #134889, N5).

On main ``/queue <prompt>`` typed while a turn runs only ``put`` the text on the CLI's own
``_pending_input``: no ``session_admissions`` row, so an owner restart (or the N2 disconnect) lost or
failed it. Now the CLI submits it as an ordinary queued admission (``prompt.submit``), the way Ink and
Desktop queue busy input, and ``/queue list`` / ``rm`` / ``clear`` read and change the gateway's queue.

Real owners (``initialize_session_authority`` again on the same state.db is the restart), real
``AuthorityConnection.dispatch``, real ``GatewayConnection`` and ``GatewaySessionStore``. The one stub:
the first owner's drain is held (``_schedule`` no-op), which is what a running turn does to the FIFO.
"""
import asyncio
import queue
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.gateway_client import rpc_error
from hermes_cli.gateway_connection import GatewayConnection
from hermes_cli.gateway_session_store import GatewaySessionStore

TEXT = "N5 queued while the turn runs"


class _Life:
    def __init__(self, connection, loop):
        self.connection, self.loop = connection, loop
        self.events = asyncio.Queue()

    async def rpc(self, method, *, _timeout=None, **params):
        frame = {'id': 1, 'method': method, 'params': params}
        reply = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self.connection.dispatch(frame), self.loop))
        if 'error' in reply:
            raise rpc_error(reply['error'])
        return reply['result']


class _Owners:
    async def start(self, tmp_path, monkeypatch):
        from gateway import run
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from gateway.session_controls import AuthorityConnection
        from gateway.session_local import create_local_session
        monkeypatch.setattr(run, '_load_gateway_config', lambda *a, **k: {'platform_toolsets': {'cli': []}})
        self.profile, self.loop, self.lives = str(tmp_path), asyncio.get_running_loop(), []
        self.store = SessionStore(tmp_path / 'sessions', GatewayConfig())
        first = await self.boot('first')
        first._schedule = lambda ref: None  # a turn is running: the FIFO does not drain
        actor = AuthorityConnection(first, object(), {'user_id': 'owner'}).actor
        self.sid = create_local_session(first, actor, {'cwd': self.profile, 'model': 'm', 'toolsets': [],
                                                       'request_id': 'n5'}).session_id
        return self

    async def boot(self, instance_id):
        from gateway.session_authority import initialize_session_authority
        owner = SimpleNamespace(session_store=self.store, _session_db=self.store._db, adapters={}, _draining=False,
                                _cached_agent_for=lambda route: None, _evict_cached_agent=lambda route: None)
        owner._adapter_for_source = lambda source: owner.adapters.get(source.platform)
        self.current = await initialize_session_authority(owner, profile_id=self.profile, instance_id=instance_id)
        return self.current

    async def _open(self):
        from gateway.session_controls import AuthorityConnection
        self.lives.append(_Life(AuthorityConnection(self.current, object(), {'user_id': 'owner'}), self.loop))
        return self.lives[-1]

    @asynccontextmanager
    async def dial(self):
        yield await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._open(), self.loop))

    def rows(self):
        from hermes_state_runtime import list_session_admissions
        return [row for row in list_session_admissions(self.store._db, session_id=self.sid, pending_only=False)
                if row['payload'].get('text') == TEXT]

    async def close(self, conn):
        await asyncio.to_thread(conn.close)
        for life in self.lives:
            await life.connection.close()
        self.store._db.close()


def _busy_cli(store, sid):
    from cli import HermesCLI
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = sid
    cli._pending_input = queue.Queue()
    cli._pending_resume_sessions = None
    cli._session_db = store
    cli._agent_running = True
    return cli


def _command(cli, text):
    with patch("cli._cprint") as printed:
        assert cli.process_command(text) is True
    return " ".join(str(call.args[0]) for call in printed.call_args_list)


@pytest.mark.asyncio
async def test_queue_while_busy_is_a_durable_admission_that_survives_an_owner_restart(tmp_path, monkeypatch):
    owners = await _Owners().start(tmp_path, monkeypatch)
    conn = GatewayConnection(connect=owners.dial)
    try:
        store = GatewaySessionStore(conn)
        await asyncio.to_thread(store.attach, owners.sid)
        cli = _busy_cli(store, owners.sid)
        await asyncio.to_thread(_command, cli, f"/queue {TEXT}")
        # Not held client-side: one queued admission in state.db, nothing for the CLI to send again.
        assert cli._pending_input.empty()
        assert [row['status'] for row in owners.rows()] == ['queued']
        listed = await asyncio.to_thread(_command, cli, "/queue list")
        assert TEXT in listed

        await owners.boot('restarted')  # the owner restarts; state.db keeps the FIFO
        assert [row['status'] for row in owners.rows()] == ['queued']
        assert len({row['admission_id'] for row in owners.rows()}) == 1  # exactly once, never re-submitted
        # The restarted owner lists it to a viewer that attaches now: it is the next turn it runs.
        fresh = GatewayConnection(connect=owners.dial)
        try:
            snapshot = await asyncio.to_thread(GatewaySessionStore(fresh).attach, owners.sid)
            assert [(row['text'], row['status']) for row in snapshot['pending']] == [(TEXT, 'queued')]
        finally:
            await asyncio.to_thread(fresh.close)
    finally:
        await owners.close(conn)


@pytest.mark.asyncio
async def test_queue_rm_and_clear_retire_the_gateway_admissions(tmp_path, monkeypatch):
    owners = await _Owners().start(tmp_path, monkeypatch)
    conn = GatewayConnection(connect=owners.dial)
    try:
        store = GatewaySessionStore(conn)
        await asyncio.to_thread(store.attach, owners.sid)
        cli = _busy_cli(store, owners.sid)
        await asyncio.to_thread(_command, cli, f"/queue {TEXT}")
        await asyncio.to_thread(_command, cli, "/queue second item")
        await asyncio.to_thread(_command, cli, "/queue rm 1")
        assert [row['status'] for row in owners.rows()] == ['terminal']
        listed = await asyncio.to_thread(_command, cli, "/queue list")
        assert "second item" in listed and TEXT not in listed
        await asyncio.to_thread(_command, cli, "/queue clear")
        assert "empty" in (await asyncio.to_thread(_command, cli, "/queue list")).lower()
    finally:
        await owners.close(conn)


def test_the_owner_claim_of_a_queued_prompt_runs_as_a_typed_turn_without_a_second_submit():
    """When the owner starts the queued admission, the idle CLI paints it as the user's turn and
    ``run_conversation`` pumps that admission (no new ``prompt.submit``: it runs exactly once)."""
    from hermes_cli.gateway_agent import GatewayAgent
    from tests.hermes_cli.test_gateway_agent import SID, frame
    from tests.hermes_cli.test_gateway_tools_notify import _connected, _idle_cli, _play
    conn, client = _connected()
    try:
        cli = _idle_cli(conn)
        conn.state(SID).queued_inputs["adm-q"] = TEXT
        _play(conn, client, [frame("message.start", {}, admission="adm-q"),
                             frame("message.delta", {"text": "ran"}, admission="adm-q"),
                             frame("message.complete", {"text": "ran", "status": "complete"}, admission="adm-q")])
        cli._drain_process_notifications("cli-idle")
        item = cli._pending_input.get_nowait()
        assert str(item) == TEXT and type(item).__name__ == "_SeededQueryMessage"  # literal, never a command
        assert conn.state(SID).queued_inputs == {}
        cli._tui_unwrap_input(item)  # the input loop takes it: its admission is armed by id
        agent = GatewayAgent(connection=conn, session_id=SID)
        agent._pending_cli_user_message = {"role": "user", "content": TEXT}
        result = agent.run_conversation(TEXT, conversation_history=[])
        assert "prompt.submit" not in [method for method, _ in client.calls]
        assert result["final_response"] == "ran"
    finally:
        conn.close()


def test_an_adopted_queued_prompt_never_submits_a_second_admission():
    """Review N5-F3: the adopted item is matched to its admission by id, not by text. A queued text the
    input path's cleanup rewrites (a leaked CPR, a bracketed-paste marker) must still be pumped as the
    admission the owner is running, never sent again as a new ``prompt.submit``."""
    from cli import _strip_leaked_bracketed_paste_wrappers, _strip_leaked_terminal_responses_with_meta
    from hermes_cli.gateway_agent import GatewayAgent
    from tests.hermes_cli.test_gateway_agent import SID, frame
    from tests.hermes_cli.test_gateway_tools_notify import _connected, _idle_cli, _play
    text = "compare cursor log ^[[12;40R with the expected rows [200~"
    conn, client = _connected()
    try:
        cli = _idle_cli(conn)
        conn.state(SID).queued_inputs["adm-q"] = text
        _play(conn, client, [frame("message.start", {}, admission="adm-q"),
                             frame("message.complete", {"text": "ran", "status": "complete"}, admission="adm-q")])
        cli._drain_process_notifications("cli-idle")
        client.script = [frame("message.complete", {"text": "again", "status": "complete"}, admission="adm-1")]
        item = cli._pending_input.get_nowait()
        user_input, _, seeded = cli._tui_unwrap_input(item)
        assert seeded and user_input == text
        # Even if a sanitizer still rewrote it (chat() strips lone surrogates), identity decides.
        user_input = _strip_leaked_bracketed_paste_wrappers(user_input)
        user_input, _ = _strip_leaked_terminal_responses_with_meta(user_input)
        agent = GatewayAgent(connection=conn, session_id=SID)
        agent._pending_cli_user_message = {"role": "user", "content": user_input}
        result = agent.run_conversation(user_input, conversation_history=[])
        assert "prompt.submit" not in [m for m, _ in client.calls], "second admission submitted"
        assert result["final_response"] == "ran"
    finally:
        conn.close()


def test_a_typed_turn_with_the_same_text_is_never_taken_for_the_adopted_admission():
    """Identity cuts both ways: a typed input that happens to equal an adopted queued text, run before the
    adopted item was unwrapped, is its own new submit."""
    from hermes_cli.gateway_agent import GatewayAgent
    from tests.hermes_cli.test_gateway_agent import SID, frame
    from tests.hermes_cli.test_gateway_tools_notify import _connected, _idle_cli, _play
    conn, client = _connected()
    try:
        cli = _idle_cli(conn)
        conn.state(SID).queued_inputs["adm-q"] = TEXT
        _play(conn, client, [frame("message.start", {}, admission="adm-q")])
        cli._drain_process_notifications("cli-idle")
        client.script = [frame("message.complete", {"text": "typed", "status": "complete"}, admission="adm-1")]
        agent = GatewayAgent(connection=conn, session_id=SID)
        agent._pending_cli_user_message = {"role": "user", "content": TEXT}
        agent.run_conversation(TEXT, conversation_history=[])
        assert "prompt.submit" in [m for m, _ in client.calls]
    finally:
        conn.close()
