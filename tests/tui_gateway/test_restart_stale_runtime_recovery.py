"""Gateway restart → stale runtime id → resume-by-stored-id recovery contract.

The 2026-09-05 incident: an ansible-driven `systemctl restart
hermes-gateway.service` sent a bare SIGTERM (no planned-stop marker), the
gateway drained and exited, and the new process booted with an empty
in-memory `_sessions` dict. Desktop tabs that had been live against the old
process kept their OLD runtime session ids. Every session-scoped RPC they
sent (prompt.submit first among them) hit `_sess_nowait`'s hard 4001
"session not found" — the gateway's only way to say "this runtime id is not
in memory".

The recovery contract this pins (server half):

1. A session-scoped RPC against a runtime id the restarted process has never
   heard of returns 4001 "session not found" — the terminal verdict the
   client's `withSessionNotFoundResume` recovery keys on.
2. `session.resume` with the STORED (durable) session id — the id that
   lives in state.db and survives the restart — mints a fresh runtime id
   and registers it in `_sessions`.
3. The fresh runtime id is immediately usable by the same session-scoped
   RPCs that 4001'd a moment ago.

The client half (resume-on-4001 + single retry) is covered by the desktop
suite (`use-prompt-actions/single-flight-resume.test.ts`,
`use-prompt-actions/index.test.tsx`); this file pins the server half so a
future server change cannot silently break the contract the client
recovery depends on.
"""

from __future__ import annotations

import pytest

from tui_gateway import server


class _StoredDB:
    """Minimal SessionDB stand-in holding one durable session row.

    Implements only the surface `session.resume` touches on the cold path
    (deferred build, `omit_messages=True`): lookup, lineage resolution,
    reopen, and the conversation reads.
    """

    def __init__(self, stored_id: str):
        self.stored_id = stored_id
        self.reopened: list[str] = []

    def get_session(self, target):
        if target == self.stored_id:
            return {"id": self.stored_id, "message_count": 2}
        return None

    def get_session_by_title(self, _title):
        return None

    def resolve_resume_session_id(self, target):
        return target

    def reopen_session(self, target):
        self.reopened.append(target)

    def get_messages_as_conversation(self, _target, **_kwargs):
        return [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

    def get_resume_conversations(self, _target):
        return (
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )

    def get_ancestor_display_prefix(self, _target):
        return []


@pytest.fixture()
def restart_server(monkeypatch, tmp_path):
    """tui_gateway.server with a stored session and hermetic agent machinery.

    Mirrors the test_session_resume_db_ownership fixture conventions: the
    resume path must not touch the real agent/secret/HERMES_HOME machinery.
    """
    stored_id = "20260905_034859_6cb58e"
    db = _StoredDB(stored_id)

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _key: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(server, "_resolve_session_source", lambda explicit: explicit or "desktop")

    known = set(server._sessions)
    yield server, stored_id, db
    with server._sessions_lock:
        for sid in [s for s in server._sessions if s not in known]:
            server._sessions.pop(sid, None)


def _rpc(srv, method, **params):
    return srv.handle_request({"id": "r1", "method": method, "params": params})


def test_stale_runtime_id_4001s_after_restart(restart_server):
    """A runtime id from a previous gateway process is a hard 4001.

    This is the exact incident shape: the desktop tab held runtime id
    `351212a5` (minted by the pre-restart process); the restarted process's
    `_sessions` dict has never heard of it. `_sess_nowait` must answer 4001
    "session not found" — the terminal verdict the client recovery keys on —
    not a transient/retryable error.
    """
    srv, _stored_id, _db = restart_server
    assert "351212a5" not in srv._sessions  # restarted process: empty memory

    resp = _rpc(srv, "prompt.submit", session_id="351212a5", text="hello")

    assert resp["error"]["code"] == 4001
    assert "session not found" in resp["error"]["message"]


def test_resume_by_stored_id_mints_usable_fresh_runtime(restart_server):
    """session.resume on the STORED id recovers the session after a restart.

    The stored (durable) id survives the restart in state.db. Resuming it
    must mint a fresh runtime id, register it in `_sessions`, and make it
    immediately usable by the same session-scoped RPC that 4001'd against
    the stale id — the exact sequence the desktop's
    `withSessionNotFoundResume` performs on a 4001.
    """
    srv, stored_id, db = restart_server

    resp = _rpc(
        srv,
        "session.resume",
        session_id=stored_id,
        source="desktop",
        omit_messages=True,
    )

    assert "error" not in resp, resp
    fresh_id = resp["result"]["session_id"]
    assert fresh_id and fresh_id != stored_id
    assert fresh_id in srv._sessions
    assert db.reopened == [stored_id]

    # The fresh runtime is immediately usable: a session-scoped RPC against
    # it resolves instead of 4001-ing. (prompt.submit would then run the
    # agent; resolving the session record is the contract under test.)
    session, err = srv._sess_nowait({"session_id": fresh_id}, "r2")
    assert err is None
    assert session is not None
