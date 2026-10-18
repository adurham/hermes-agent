"""RT-37 (#140385): a Desktop prompt on a session whose gateway owner is running a turn (a Bot Chat
delivery) is admitted into the OWNER's durable FIFO and runs after that turn, exactly once. It is not
refused ``SESSION_NOT_OWNED``, nothing runs in the legacy backend, and the Desktop gets a queued
receipt, then the owner's start and completion for that admission.

Real: the SessionDB, the gateway turn's started admission (``admit_session_input`` +
``claim_session_input``), the legacy ``prompt.submit`` and its ledger gate, and the owner
(``SessionAuthority`` + ``AuthorityConnection``: attach, admission, claim stamp, settlement and
publication). Replaced: only the owner's discovery and socket dial (``owner_forward.discover_owner`` /
``connect_owner``), by an in-process client speaking to the same ``AuthorityConnection``."""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, begin_runtime_epoch, claim_session_input, list_session_admissions,
    recover_session_inputs, settle_session_input,
)
from tui_gateway import server
from tests.waiting import wait_until


class _OwnerClient:
    """``GatewayClient``'s surface (``rpc`` + ``events``) over a real ``AuthorityConnection``."""

    def __init__(self, authority):
        from gateway.session_controls import AuthorityConnection
        self.loop, self.events = asyncio.get_running_loop(), asyncio.Queue()
        self.conn = AuthorityConnection(authority, self, {"user_id": "desktop"})

    def write(self, frame):  # the owner's fanout to this socket
        self.loop.call_soon_threadsafe(self.events.put_nowait, frame)
        return True

    def close(self):
        return None

    async def rpc(self, method, **params):
        reply = await self.conn.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in reply:
            from hermes_cli.gateway_client import rpc_error
            raise rpc_error(reply["error"])
        return reply["result"]


def _legacy(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    for name in ("_schedule_agent_build", "_schedule_session_cap_enforcement", "_register_session_cwd",
                 "_start_agent_build"):
        monkeypatch.setattr(server, name, lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *_a: False)
    turns, emitted = [], []
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a: turns.append(a[1]))
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)) or True)
    created = server.handle_request({"id": "c", "method": "session.create", "params": {"source": "desktop"}})
    return created["result"]["session_id"], created["result"]["stored_session_id"], turns, emitted


def _owner(monkeypatch, db, key, epoch):
    from gateway.session_authority import LiveSession, SessionAuthority
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id="owned", instance_id="gateway",
                                 db=db, epoch=epoch)
    authority.sessions[key] = LiveSession(SimpleNamespace(platform=None, user_id="desktop"), "route")
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)  # the test drives the FIFO
    try:
        from tui_gateway import owner_forward
    except ImportError:  # main: no forwarder; the submit is refused and the assertions below fail
        return authority

    @contextlib.asynccontextmanager
    async def connect(_home, endpoint):
        assert endpoint == "ready-owner"
        yield _OwnerClient(authority)
    monkeypatch.setattr(owner_forward, "discover_owner", lambda _home: "ready-owner")
    monkeypatch.setattr(owner_forward, "connect_owner", connect)
    return authority


def test_desktop_prompt_during_a_gateway_turn_runs_after_it_in_the_owner_fifo(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        # The gateway is running a Bot Chat delivery on this session.
        bot = admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                                  payload={"text": "a message_agent DM"})
        claimed = claim_session_input(db, epoch=epoch, session_id=key)
        assert claimed["admission_id"] == bot["admission_id"]
        authority = _owner(monkeypatch, db, key, epoch)

        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "my own question"}})

        assert "error" not in reply, reply  # main: 4090 SESSION_NOT_OWNED
        assert reply["result"]["status"] == "queued" and reply["result"]["forwarded"] is True
        assert turns == [] and db.get_messages_as_conversation(key) == []  # nothing ran here
        rows = list_session_admissions(db, session_id=key)
        assert [(r["principal_id"], r["status"]) for r in rows] == [("bot", "started"), (rows[1]["principal_id"], "queued")]
        mine = rows[1]
        assert mine["admission_id"] == reply["result"]["admission_id"] and mine["payload"]["text"] == "my own question"
        # Queued, not a failed turn: the Desktop's pending-submission card, never an ``error``.
        wait_until(lambda: any(e == "session.info" for e, *_ in emitted), timeout=10)
        pending = next(p for e, _s, p in emitted if e == "session.info")["pending_submissions"]
        assert pending == [{"admission_id": mine["admission_id"], "status": "queued", "user": "my own question",
                            "input_id": mine["request_id"]}]
        assert not any(e == "error" for e, *_ in emitted)

        # The gateway turn ends; the owner's FIFO runs the prompt next, exactly once.
        assert claim_session_input(db, epoch=epoch, session_id=key) is None  # behind the running turn
        settle_session_input(db, epoch=epoch, admission_id=bot["admission_id"], generation=claimed["generation"],
                             outcome="completed")
        row = claim_session_input(db, epoch=epoch, session_id=key)
        assert row["admission_id"] == mine["admission_id"]
        assert claim_session_input(db, epoch=epoch, session_id=key) is None
        from gateway.session_contract import SessionRef
        from gateway.session_settlement_recovery import commit_and_publish
        ref, live = SessionRef("owned", key), authority.sessions[key]
        authority._stamp_claim(ref, live, row)
        with live.event_stream.lock:
            authority.executing.pop(key, None)
        commit_and_publish(authority, live, ref, row, "THE ANSWER", "completed", None, {})

        wait_until(lambda: any(e == "message.complete" for e, *_ in emitted), timeout=10)
        relayed = [(e, p) for e, s, p in emitted if s == sid and e.startswith("message.")]
        assert relayed[0][0] == "message.start"
        assert relayed[-1] == ("message.complete", {"text": "THE ANSWER", "status": "complete"})
        assert [p["pending_submissions"] for e, _s, p in emitted if e == "session.info"][-1] == []
        assert [r["status"] for r in list_session_admissions(db, session_id=key, pending_only=False)] == [
            "terminal", "terminal"]
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_outcome_unknown_work_is_still_refused_and_never_forwarded(monkeypatch, tmp_path):
    """Only Discard resolves an ``unknown`` admission: a prompt is never queued past it."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        first = begin_runtime_epoch(db, instance_id="gateway-1")
        admit_session_input(db, epoch=first, principal_id="bot", session_id=key, request_id="dm-1",
                            payload={"text": "lost?"})
        claim_session_input(db, epoch=first, session_id=key)
        epoch = begin_runtime_epoch(db, instance_id="gateway-2")
        recover_session_inputs(db, epoch=epoch)
        _owner(monkeypatch, db, key, epoch)
        reply = server.handle_request({"id": "p", "method": "prompt.submit",
                                       "params": {"session_id": sid, "text": "hi"}})
        assert reply["error"]["code"] == 4090 and reply["error"]["data"] == {"reason": "SESSION_NOT_OWNED"}
        assert len(list_session_admissions(db, session_id=key)) == 1 and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize("case", ["owner_refused", "rewind"])
def test_a_prompt_the_owner_cannot_queue_is_refused_with_why_and_when(monkeypatch, tmp_path, case):
    """Where admission is impossible the refusal names why and says to send when the turn ends,
    with a transient reason (not SESSION_NOT_OWNED, whose card offers a new chat)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key, turns, _emitted = _legacy(monkeypatch, db)
    try:
        db.create_session(key, "test")
        epoch = begin_runtime_epoch(db, instance_id="gateway")
        admit_session_input(db, epoch=epoch, principal_id="bot", session_id=key, request_id="dm-1",
                            payload={"text": "a DM"})
        claim_session_input(db, epoch=epoch, session_id=key)
        params = {"session_id": sid, "text": "hi"}
        _owner(monkeypatch, db, key, epoch)
        if case == "owner_refused":
            from tui_gateway import owner_forward

            @contextlib.asynccontextmanager
            async def refused(_home, _endpoint):
                raise owner_forward.OwnerUnavailable("the gateway answered with another protocol")
                yield
            monkeypatch.setattr(owner_forward, "connect_owner", refused)
        else:
            params.update(truncate_before_user_ordinal=0, confirm_truncate=True)
        reply = server.handle_request({"id": "p", "method": "prompt.submit", "params": params})
        assert reply["error"]["code"] == 4090 and reply["error"]["data"] == {"reason": "SESSION_OWNER_BUSY"}
        message = reply["error"]["message"]
        assert "when the current turn ends" in message and "Nothing was sent" in message
        assert ("another protocol" in message) if case == "owner_refused" else ("rewind" in message)
        assert len(list_session_admissions(db, session_id=key)) == 1 and turns == []
    finally:
        server._sessions.pop(sid, None)
        db.close()
