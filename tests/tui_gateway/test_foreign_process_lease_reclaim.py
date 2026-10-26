"""An orphan sweep must never reclaim a lease its process still holds.

``tui_gateway.server`` started a 5-minute orphan reaper thread at IMPORT time, and its
vouching set (``session_lifecycle._own_live_lease_ids``) is the TUI backend's own
``_sessions`` map.  The v0.22.5 sync put ``tui_gateway.server`` on the messaging
gateway's ordinary RPC paths (``gateway/session_complete_path.py``,
``gateway/session_ancillary.py``, ``gateway/session_tui_reads.py``), so the gateway
process imported the module, started that reaper and — with an empty ``_sessions`` —
reclaimed **its own** active-session leases (``gateway/run_busy.py::_claim_active_session_slot``,
surface ``gateway:<platform>``) a few seconds after each claim.

A lease is the registry's proof that one backend owns a session.  Dropping the
gateway's own lease makes the session look unowned while its turn is still running:
another backend may claim the id, the gateway's next turn is then refused
``SESSION_NOT_OWNED`` (and the queued message behind it never runs), and a viewer's
finalize sees "no other runtime owns this session" and ends the durable row mid-turn
— which fences the turn ``unknown`` and pauses the session's FIFO.

Live proof of the leak: "Reclaimed N orphaned active-session lease(s)" was 0/day
before the sync and 12-78/day after it (see FORK.md, v0.22.5 sync entry).
"""

from __future__ import annotations

import gc
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import active_sessions
from hermes_cli.active_sessions import (
    active_session_registry_snapshot,
    register_lease_source,
    try_acquire_active_session,
)
from tui_gateway import server

# A real local session key: ``build_session_key``'s shape for a gateway-hosted chat.
_GATEWAY_KEY = "agent:main:local:dm:local-0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def _entries_for(session_id: str, home=None) -> list[dict]:
    return [e for e in active_session_registry_snapshot(home) if e.get("session_id") == session_id]


def _claim_gateway_lease(session_id: str = _GATEWAY_KEY):
    """Claim exactly as ``gateway/run_busy.py::_claim_active_session_slot`` does."""
    lease, message = try_acquire_active_session(
        session_id=session_id,
        surface="gateway:local",
        config={},
        metadata={"live_session_id": session_id},
    )
    assert lease is not None and message is None, message
    return lease


class _Runner:
    """Stand-in for GatewayRunner: the lease-source hook ``active_sessions`` consults."""

    def __init__(self, lease=None):
        self.lease = lease

    def live_active_session_lease_ids(self):
        return {self.lease.lease_id} if self.lease is not None else set()


def test_reaper_keeps_a_lease_the_messaging_gateway_still_holds(monkeypatch):
    """The regression: the TUI idle reaper ate the gateway's own live lease."""
    monkeypatch.setattr(active_sessions, "_SELF_ORPHAN_GRACE_SECONDS", 0.0)
    lease = _claim_gateway_lease()
    runner = _Runner(lease)
    register_lease_source(runner)
    assert [e["lease_id"] for e in _entries_for(_GATEWAY_KEY)] == [lease.lease_id]

    server._reap_idle_sessions()

    assert [e["lease_id"] for e in _entries_for(_GATEWAY_KEY)] == [lease.lease_id], (
        "the orphan sweep reclaimed a lease the messaging gateway still holds in this process"
    )
    lease.release()
    assert [e["lease_id"] for e in _entries_for(_GATEWAY_KEY)] == []


def test_held_lease_ids_vouches_for_registered_backends_only():
    """A process's live leases come from the backends that hold them, not one session map."""
    lease = _claim_gateway_lease()
    runner = _Runner(lease)
    register_lease_source(runner)
    try:
        assert active_sessions.held_lease_ids() == {lease.lease_id}
        assert lease.lease_id in server._own_live_lease_ids()
        runner.lease = None  # the turn ended: the backend reports nothing
        assert active_sessions.held_lease_ids() == set()
        assert lease.lease_id not in server._own_live_lease_ids()
    finally:
        lease.release()


def test_orphan_sweep_still_reclaims_a_genuinely_leaked_lease(monkeypatch):
    """The safety net stays: a lease no live owner reports is reclaimed."""
    monkeypatch.setattr(active_sessions, "_SELF_ORPHAN_GRACE_SECONDS", 0.0)
    key = "agent:main:local:dm:local-leaked-lease-0000000000000000000000000000"
    lease, message = try_acquire_active_session(
        session_id=key, surface="tui", config={}, metadata={"live_session_id": key},
    )
    assert lease is not None and message is None, message
    del lease
    gc.collect()

    server._reap_idle_sessions()

    assert _entries_for(key) == [], "a lease no live backend reports must still be reclaimed"


def test_a_broken_lease_source_fails_closed_and_skips_the_sweep(monkeypatch):
    """A source that raises cannot prove what it holds: the sweep must keep EVERY lease.

    Dropping the broken source (and vouching only for the others) would make its live leases look
    orphaned and reclaim them — the very bug the lease-source registry exists to prevent.
    """
    monkeypatch.setattr(active_sessions, "_SELF_ORPHAN_GRACE_SECONDS", 0.0)
    held_by_broken = _claim_gateway_lease()
    other_key = "agent:main:local:dm:local-other-backend-000000000000000000000000"
    held_by_runner = _claim_gateway_lease(other_key)

    class _Broken:
        def live_active_session_lease_ids(self):
            raise RuntimeError("boom")

    # Both owners must stay referenced: the registry is weak, so a retired backend stops vouching.
    broken, runner = _Broken(), _Runner(held_by_runner)
    register_lease_source(broken)
    register_lease_source(runner)
    try:
        assert active_sessions.held_lease_ids() is None
        assert server._own_live_lease_ids() is None
        assert active_sessions.release_orphaned_leases(None) == 0

        server._reap_idle_sessions()

        assert [e["lease_id"] for e in _entries_for(_GATEWAY_KEY)] == [held_by_broken.lease_id], (
            "a lease source that failed must not have its live leases reclaimed")
        assert [e["lease_id"] for e in _entries_for(other_key)] == [held_by_runner.lease_id]
    finally:
        held_by_broken.release()
        held_by_runner.release()


def test_gateway_runner_reports_its_live_turn_leases():
    """The gateway's hook: a live turn's slot is vouched for; a released one is not."""
    from gateway.run import GatewayRunner
    from gateway.session_state import SessionState

    lease = _claim_gateway_lease()
    runner = GatewayRunner.__new__(GatewayRunner)
    state = SessionState()
    state.turn.lease = lease
    runner._sessions = {"agent:main:local:dm:local-x": state}
    try:
        assert runner.live_active_session_lease_ids() == {lease.lease_id}
        state.turn.lease = None
        assert runner.live_active_session_lease_ids() == set()
    finally:
        lease.release()


def test_gateway_lease_snapshot_survives_a_concurrent_session_insert():
    """The sweep reads the runner's map on the reaper thread while the asyncio loop inserts sessions.

    Iterating the live ``dict.values()`` view raised "dictionary changed size during iteration";
    under the fail-closed registry that skips the sweep, but the runner's own leases must still be
    reported correctly. The insert is forced mid-iteration (deterministic, no thread timing).
    """
    from gateway.run import GatewayRunner
    from gateway.session_state import SessionState

    lease = _claim_gateway_lease()
    runner = GatewayRunner.__new__(GatewayRunner)
    sessions: dict = {}

    class _InsertingState:
        """Reading its turn simulates the loop admitting a new session at that instant."""

        @property
        def turn(self):
            sessions.setdefault("agent:main:local:dm:local-late", SessionState())
            return SessionState().turn

    live = SessionState()
    live.turn.lease = lease
    sessions["agent:main:local:dm:local-a"] = _InsertingState()
    sessions["agent:main:local:dm:local-b"] = live
    runner._sessions = sessions
    try:
        assert runner.live_active_session_lease_ids() == {lease.lease_id}
    finally:
        lease.release()


def test_importing_the_server_module_starts_no_reaper_thread():
    """Import must not start a background reaper: the gateway imports this module for helpers."""
    # The module installs a stdout proxy, so the probe reports on the real stderr instead.
    probe = (
        "import sys, tui_gateway.server as s;"
        "sys.stderr.write('REAPER=%r\\n' % (getattr(s, '_idle_reaper_thread', 'missing'),))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=420,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "REAPER=None" in done.stderr, (
        "importing tui_gateway.server must not start the idle reaper "
        f"(stderr tail: {done.stderr[-800:]!r})"
    )


def test_serving_the_first_session_starts_the_reaper(monkeypatch):
    """The backend still reaps: starting it moved from import to the first session record."""
    started: list[int] = []
    monkeypatch.setattr(server, "_ensure_idle_reaper_started", lambda: started.append(1))
    for name in ("_hydrate_session_cwd", "_register_session_cwd", "_wire_session_agent",
                 "_start_session_services", "_emit", "_schedule_mcp_late_refresh"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "all")
    monkeypatch.setattr(server, "_resolve_session_source", lambda source: source or "tui")
    monkeypatch.setattr(server, "current_transport", lambda: None)
    monkeypatch.setattr(server, "_stdio_transport", object(), raising=False)
    monkeypatch.setattr(server, "_transport_auth_user_id", lambda transport: None)
    monkeypatch.setattr(server, "_session_todo_state", lambda session: None)
    sid = "runtime-first-session"
    try:
        server._init_session(sid, "stored-first-session", None, [])
    finally:
        server._sessions.pop(sid, None)
    assert started, "serving a session must start this backend's idle reaper"


def test_session_create_over_rpc_starts_the_reaper(monkeypatch, tmp_path):
    """``session.create`` writes ``_sessions`` itself (lazy agent build) and never calls
    ``_init_session``: a Desktop/TUI backend that only opens new chats must still reap.

    Driven through the real JSON-RPC entry (``handle_request`` → contract validation → handler).
    """
    monkeypatch.setattr("hermes_cli.banner.prefetch_update_check", lambda: None)
    (tmp_path / "config.yaml").write_text("model:\n  default: claude-opus-5\n  provider: anthropic\n",
                                          encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_load_cfg", dict)
    monkeypatch.setattr(server, "_profile_home", lambda *a: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda *a: None)
    started: list[int] = []
    monkeypatch.setattr(server, "_ensure_idle_reaper_started", lambda: started.append(1))

    response = server.handle_request(
        {"jsonrpc": "2.0", "id": "r1", "method": "session.create", "params": {"cols": 80}})

    assert "error" not in response, response
    assert response["result"]["session_id"] in server._sessions
    assert started, "session.create must start this backend's idle reaper"


def test_compute_host_fallback_session_starts_the_reaper(monkeypatch):
    """The compute host's minimal fallback record (``_init_session`` raised) also serves a session."""
    from tui_gateway.compute_host import ComputeHost

    started: list[int] = []
    monkeypatch.setattr(server, "_ensure_idle_reaper_started", lambda: started.append(1))
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: object())
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda agent, db: False)

    def _broken_init(*a, **k):
        raise RuntimeError("side machinery unavailable")

    monkeypatch.setattr(server, "_init_session", _broken_init)
    sid = "s-fallback-reaper"
    host = ComputeHost(stdout=__import__("io").StringIO(), heartbeat_secs=0)
    try:
        session = host._build_server_session(
            server, {"session_key": "s-fallback-key", "cols": 80, "history": []}, sid)
        assert session is server._sessions[sid]
    finally:
        server._sessions.pop(sid, None)
        host.close()
    assert started, "the compute host's fallback session must start the idle reaper"


def test_reaper_start_is_idempotent_and_names_its_thread(monkeypatch):
    """The backend starts it explicitly, once; the thread is named for diagnosability.

    No real reaper thread may outlive this test (it would sweep other tests' state every
    ``_REAPER_SCAN_S``), so ``threading.Thread`` is replaced for the duration.
    """
    created: list = []

    class _FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target, self.name, self.daemon, self._alive = target, name, daemon, False
            created.append(self)

        def start(self):
            self._alive = True

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(server, "_idle_reaper_thread", None)
    monkeypatch.setattr(server.threading, "Thread", _FakeThread)

    server._ensure_idle_reaper_started()
    first = server._idle_reaper_thread
    server._ensure_idle_reaper_started()

    assert server._idle_reaper_thread is first
    assert len(created) == 1
    assert first is not None and first.is_alive()
    assert first.daemon is True
    assert first.name == server._IDLE_REAPER_THREAD_NAME

    first._alive = False  # a dead reaper is restarted, not left dead
    server._ensure_idle_reaper_started()
    assert len(created) == 2 and server._idle_reaper_thread is created[1]
