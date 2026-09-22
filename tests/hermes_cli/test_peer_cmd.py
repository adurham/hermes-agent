"""Tests for ``hermes peer`` — cross-machine bot-to-bot DMs."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from agent.turn_author import TURN_AUTHOR_ENV
from hermes_cli.subcommands import peer as peer_cmd


# ── target parsing ───────────────────────────────────────────────────────────


def test_parse_target_bare_peer():
    assert peer_cmd._parse_target("spark") == ("spark", None)


def test_parse_target_peer_and_profile():
    assert peer_cmd._parse_target("spark/researcher") == ("spark", "researcher")


def test_parse_target_rejects_empty():
    with pytest.raises(ValueError):
        peer_cmd._parse_target("")


def test_parse_target_rejects_bad_profile():
    with pytest.raises(ValueError):
        peer_cmd._parse_target("spark/../etc")


# ── url scoping ──────────────────────────────────────────────────────────────


def test_base_url_bare_and_profile():
    peer = {"url": "http://spark.lan:8377/"}
    assert peer_cmd._base_url(peer, None) == "http://spark.lan:8377"
    assert peer_cmd._base_url(peer, "researcher") == "http://spark.lan:8377/p/researcher"


# ── registry round-trip (isolated config) ────────────────────────────────────


def test_add_list_remove_roundtrip(monkeypatch, capsys):
    store = {}

    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: dict(store))

    def fake_save(peers):
        store.clear()
        store.update(peers)

    monkeypatch.setattr(peer_cmd, "_save_peers", fake_save)
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k" * 20)

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(peer_action="add", name="spark", url="http://spark.lan:8377", key="", note="")
    )
    assert rc == 0
    assert store["spark"]["url"] == "http://spark.lan:8377"

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="list"))
    assert rc == 0
    assert "spark" in capsys.readouterr().out

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="remove", name="spark"))
    assert rc == 0
    assert "spark" not in store


def test_add_rejects_bad_name_and_url(monkeypatch):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {})
    monkeypatch.setattr(peer_cmd, "_save_peers", lambda peers: None)

    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="add", name="Bad Name!", url="http://x", key="", note="")) == 2
    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="add", name="ok", url="ftp://x", key="", note="")) == 2


def test_dm_unknown_peer_and_missing_key(monkeypatch):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")

    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="nope", message="hi", json=False)) == 1
    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="hi", json=False)) == 1


# ── live HTTP dm flow (real loopback server, fake peer gateway) ──────────────


class _FakePeer(BaseHTTPRequestHandler):
    sessions: list = []
    chats: list = []
    chat_bodies: list = []
    runs: list = []
    run_idempotency_keys: list = []
    auth_seen: list = []

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path == "/v1/capabilities":
            return self._json(
                {
                    "features": {
                        "runs_idempotency": {
                            "supported": True,
                            "durable": True,
                            "retention_seconds": 86400,
                        }
                    }
                }
            )
        if self.path == "/v1/runs/run_1":
            return self._json({
                "object": "hermes.run",
                "run_id": "run_1",
                "status": "completed",
                "session_id": "bc_existing",
                "output": "async reply from the other machine",
            })
        if self.path.startswith("/api/sessions"):
            data = [{"id": s, "title": "Bot Chat"} for s in type(self).sessions]
            return self._json({"object": "list", "data": data})
        return self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/sessions":
            type(self).sessions.append("bc_1")
            # REAL api_server create shape: the row is wrapped under "session"
            # (verified live Aug 2026 — a flat fake hid a parser bug).
            return self._json({"object": "hermes.session", "session": {"id": "bc_1", "title": body.get("title")}}, 201)

        if self.path.startswith("/api/sessions/") and self.path.endswith("/chat"):
            type(self).chats.append(body.get("message"))
            type(self).chat_bodies.append(body)
            return self._json({
                "object": "hermes.session.chat.completion",
                "session_id": "bc_1",
                "message": {
                    "role": "assistant",
                    "content": "reply from the other machine",
                },
            })

        if self.path == "/v1/runs":
            type(self).runs.append(body)
            type(self).run_idempotency_keys.append(
                self.headers.get("Idempotency-Key", "")
            )
            return self._json(
                {"run_id": "run_1", "status": "started", "replayed": False},
                202,
            )

        if self.path == "/v1/runs/run_1/stop":
            return self._json({"run_id": "run_1", "status": "stopping"})

        return self._json({"error": {"message": "not found"}}, 404)

    def log_message(self, *args):  # noqa: D102 — silence test server logging
        pass


@pytest.fixture()
def fake_peer_server():
    _FakePeer.sessions = []
    _FakePeer.chats = []
    _FakePeer.chat_bodies = []
    _FakePeer.runs = []
    _FakePeer.run_idempotency_keys = []
    _FakePeer.auth_seen = []
    server = HTTPServer(("127.0.0.1", 0), _FakePeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_dm_creates_bot_chat_then_chats(monkeypatch, capsys, fake_peer_server):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="dm",
            target="spark",
            message="Message from 🤖 dixie (@dixie): disk status?",
            json=False,
        )
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "reply from the other machine" in out
    # One Bot Chat was created (none existed), then the chat turn ran on it.
    assert _FakePeer.sessions == ["bc_1"]
    assert _FakePeer.chats == ["Message from 🤖 dixie (@dixie): disk status?"]
    # Every request carried the peer key.
    assert all(a == "Bearer secret-key-123456" for a in _FakePeer.auth_seen)


def test_dm_reuses_existing_bot_chat(monkeypatch, capsys, fake_peer_server):
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == "reply from the other machine"
    # No new session was created — the existing canonical chat was reused.
    assert _FakePeer.sessions == ["bc_existing"]


# ── per-turn author (HERMES_TURN_AUTHOR set by the message_agent runner) ─────


AUTHOR = {"id": "bot:dixie", "name": "dixie", "is_bot": True}


def _peer_spark(monkeypatch, url, author_env):
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")
    if author_env is None:
        monkeypatch.delenv(TURN_AUTHOR_ENV, raising=False)
    else:
        monkeypatch.setenv(TURN_AUTHOR_ENV, json.dumps(author_env))


@pytest.mark.parametrize("author_env, expected_body", [
    ({**AUTHOR, "x": 1}, {"message": "ping", "author": AUTHOR}),
    (None, {"message": "ping"}),
], ids=["author from env", "no env"])
def test_dm_body_carries_author_only_from_env(monkeypatch, fake_peer_server, author_env, expected_body):
    _peer_spark(monkeypatch, fake_peer_server, author_env)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True))

    assert rc == 0
    assert _FakePeer.chat_bodies == [expected_body]


def test_run_body_carries_author_from_env(monkeypatch, capsys, fake_peer_server):
    _peer_spark(monkeypatch, fake_peer_server, AUTHOR)

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="run", target="spark", message="long task", idempotency_key="ticket-1", json=True))

    assert rc == 0
    assert _FakePeer.runs == [{"input": "long task", "session_id": "bc_existing", "author": AUTHOR}]


# ── hidden canonical Bot Chat (issue #91583) ─────────────────────────────────


class _HiddenBotChatPeer(_FakePeer):
    """A NEW-style peer: its Bot Chat exists but is HIDDEN (Bot Mode hides
    canonical chats), so it only appears in the listing when the client
    sends the exact-title + include_hidden lookup."""

    hidden_sessions: list = []
    get_queries: list = []

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path.startswith("/api/sessions"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            type(self).get_queries.append(query)
            data = [{"id": s, "title": "Bot Chat", "hidden": False} for s in type(self).sessions]
            if query.get("title", [""])[0] == "Bot Chat" and query.get("include_hidden", ["0"])[0] in ("1", "true"):
                data += [{"id": s, "title": "Bot Chat", "hidden": True} for s in type(self).hidden_sessions]
            return self._json({"object": "list", "data": data})
        return self._json({"error": {"message": "not found"}}, 404)


class _OldHiddenBotChatPeer(_FakePeer):
    """An OLD peer: ignores title/include_hidden, its hidden Bot Chat is
    invisible in every listing, and the duplicate create trips the DB's
    UNIQUE(title) guard with the real api_server 400 shape."""

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path.startswith("/api/sessions"):
            return self._json({"object": "list", "data": []})
        return self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/api/sessions":
            return self._json(
                {"error": {"message": "Title already in use by session hidden_bc_1", "code": "invalid_title"}},
                400,
            )
        return self._json({"error": {"message": "not found"}}, 404)


@pytest.fixture()
def hidden_peer_server():
    _HiddenBotChatPeer.sessions = []
    _HiddenBotChatPeer.hidden_sessions = ["bc_hidden"]
    _HiddenBotChatPeer.chats = []
    _HiddenBotChatPeer.auth_seen = []
    _HiddenBotChatPeer.get_queries = []
    server = HTTPServer(("127.0.0.1", 0), _HiddenBotChatPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def old_hidden_peer_server():
    _OldHiddenBotChatPeer.sessions = []
    _OldHiddenBotChatPeer.chats = []
    _OldHiddenBotChatPeer.auth_seen = []
    server = HTTPServer(("127.0.0.1", 0), _OldHiddenBotChatPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_find_bot_chat_sends_hidden_aware_lookup(hidden_peer_server):
    """The lookup carries title + include_hidden so a hidden canonical row resolves."""
    found = peer_cmd._find_bot_chat(hidden_peer_server, "secret-key-123456")
    assert found == "bc_hidden"
    query = _HiddenBotChatPeer.get_queries[-1]
    assert query.get("title") == ["Bot Chat"]
    assert query.get("include_hidden") == ["1"]


def test_dm_resolves_hidden_bot_chat_without_duplicate_create(monkeypatch, capsys, hidden_peer_server):
    """Regression for issue #91583: hidden canonical Bot Chat must be reused,
    never re-created (the peer's UNIQUE(title) guard rejects the duplicate)."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": hidden_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == "reply from the other machine"
    # No create was attempted: the hidden canonical chat resolved directly.
    assert _HiddenBotChatPeer.sessions == []
    assert _HiddenBotChatPeer.chats == ["ping"]


def test_dm_older_peer_hidden_duplicate_gives_clear_error(monkeypatch, capsys, old_hidden_peer_server):
    """Against an older peer that can't expose hidden sessions, the UNIQUE(title)
    rejection must surface a diagnosable error naming the hidden canonical chat."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": old_hidden_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=False))

    assert rc == 1
    err = capsys.readouterr().err
    assert "hidden" in err
    assert "Bot Chat" in err
    assert "Title already in use" in err


def test_dm_older_peer_with_visible_bot_chat_still_works(monkeypatch, capsys, fake_peer_server):
    """Backward compat: an older peer ignores the new query params and returns
    the plain visible listing — a visible Bot Chat must still resolve."""
    _FakePeer.sessions = ["bc_visible"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True))

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["reply"] == "reply from the other machine"
    assert _FakePeer.sessions == ["bc_visible"]


def test_run_starts_async_turn_with_canonical_session_and_idempotency(
    monkeypatch, capsys, fake_peer_server
):
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(
        peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}}
    )
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="run",
            target="spark",
            message="long task",
            idempotency_key="ticket-123",
            json=True,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "peer": "spark",
        "profile": None,
        "session_id": "bc_existing",
        "run_id": "run_1",
        "status": "started",
        "idempotency_key": "ticket-123",
        "replayed": False,
    }
    assert _FakePeer.runs == [{"input": "long task", "session_id": "bc_existing"}]
    assert _FakePeer.run_idempotency_keys == ["ticket-123"]


def test_status_reads_async_run_output(monkeypatch, capsys, fake_peer_server):
    monkeypatch.setattr(
        peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}}
    )
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="status",
            target="spark",
            run_id="run_1",
            json=True,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["output"] == "async reply from the other machine"


def test_stop_requests_exact_async_run(monkeypatch, capsys, fake_peer_server):
    monkeypatch.setattr(
        peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}}
    )
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="stop",
            target="spark",
            run_id="run_1",
            json=True,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run_1"
    assert payload["status"] == "stopping"


# ── cross-origin redirect must not carry the peer's Bearer key ──────────────


class _AttackerOrigin(BaseHTTPRequestHandler):
    """A second real HTTP server standing in for an attacker-controlled host
    a compromised/MITM'd peer could redirect a ``hermes peer dm`` request to."""

    auth_seen: list = []

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization"))
        body = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102 — silence test server logging
        pass


class _RedirectingPeer(BaseHTTPRequestHandler):
    """A "peer" that 302-redirects every request to a different origin —
    the shape of a compromised peer or a LAN MITM answering ``hermes peer
    add``'s registered URL."""

    redirect_target: str = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", type(self).redirect_target + self.path)
        self.end_headers()

    def log_message(self, *args):  # noqa: D102 — silence test server logging
        pass


def test_request_strips_bearer_key_across_redirect_origin():
    """``_request`` must not forward the peer's Authorization: Bearer key to
    a different origin a redirect points at (compromised peer / LAN MITM) —
    the exact class of leak ``open_credentialed_url`` exists to close."""
    _AttackerOrigin.auth_seen = []
    attacker = HTTPServer(("127.0.0.1", 0), _AttackerOrigin)
    attacker_thread = threading.Thread(target=attacker.serve_forever, daemon=True)
    attacker_thread.start()

    _RedirectingPeer.redirect_target = f"http://127.0.0.1:{attacker.server_port}"
    peer = HTTPServer(("127.0.0.1", 0), _RedirectingPeer)
    peer_thread = threading.Thread(target=peer.serve_forever, daemon=True)
    peer_thread.start()

    try:
        # The attacker origin answers with a well-formed (empty) listing, so
        # the redirect completes successfully — the request itself is not
        # the point of this test, only whether the Bearer key rode along.
        result = peer_cmd._request(f"http://127.0.0.1:{peer.server_port}/api/sessions", "top-secret-peer-key")
        assert result == {"object": "list", "data": []}
    finally:
        peer.shutdown()
        peer_thread.join(timeout=5)
        attacker.shutdown()
        attacker_thread.join(timeout=5)

    assert _AttackerOrigin.auth_seen, "redirect target was never reached"
    assert all(header is None for header in _AttackerOrigin.auth_seen), (
        f"peer's Bearer key leaked to the redirect target: {_AttackerOrigin.auth_seen}"
    )

# ═══════════════════════════════════════════════════════════════════════════
# Fork-only capabilities ported forward from the retired ``hermes submit``
# (see FORK.md). Three groups: SSE --tail streaming, the widened credential
# resolution chain, and the session/--no-session behavior.
# ═══════════════════════════════════════════════════════════════════════════


# ── capability 1: SSE --tail streaming ──────────────────────────────────────


def _sse(*events) -> bytes:
    """Encode events the way api_server's ``_sse_frame`` does, keepalives and all."""
    out = b""
    for event in events:
        if isinstance(event, bytes):
            out += event  # a raw comment line, e.g. b": keepalive\n\n"
        else:
            out += f"data: {json.dumps(event)}\n\n".encode()
    return out


class _StreamingRunPeer(_FakePeer):
    """A peer whose ``/v1/runs`` starts a run and whose ``/events`` streams it
    to completion — an in-flight run observed live, not a canned final status."""

    # Frames pushed, in order, once the client attaches to the feed.
    frames: bytes = b""
    events_requests: list = []
    events_accept: list = []

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path.endswith("/events"):
            type(self).events_requests.append(self.path)
            type(self).events_accept.append(self.headers.get("Accept", ""))
            # No Content-Length: the stream ends at EOF, exactly like aiohttp's
            # StreamResponse closing after the terminal event.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(type(self).frames)
            self.wfile.flush()
            return
        return super().do_GET()


@pytest.fixture()
def streaming_peer_server():
    def _start(frames: bytes, sessions=None):
        _StreamingRunPeer.sessions = list(sessions or ["bc_existing"])
        _StreamingRunPeer.chats = []
        _StreamingRunPeer.runs = []
        _StreamingRunPeer.run_idempotency_keys = []
        _StreamingRunPeer.auth_seen = []
        _StreamingRunPeer.events_requests = []
        _StreamingRunPeer.events_accept = []
        _StreamingRunPeer.frames = frames
        server = HTTPServer(("127.0.0.1", 0), _StreamingRunPeer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    started: list = []
    try:
        yield _start
    finally:
        for server, thread in started:
            server.shutdown()
            thread.join(timeout=5)


_RUNNING_STREAM = _sse(
    {"event": "run.started", "run_id": "run_1", "timestamp": 1.0},
    {"event": "message.delta", "run_id": "run_1", "delta": "work"},
    b": keepalive\n\n",
    {"event": "message.delta", "run_id": "run_1", "delta": "ing..."},
    {"event": "tool.start", "run_id": "run_1", "preview": "terminal: df -h"},
    {"event": "run.completed", "run_id": "run_1", "output": "disk is fine"},
)


def test_run_tail_streams_in_flight_run_to_completion(monkeypatch, capsys, streaming_peer_server):
    """``peer run --tail`` must follow a live run's SSE feed until it ends.

    The port-forward of ``hermes submit --tail`` (submit.py:127-170), rebased on
    peer.py's run_id model and its urllib/open_credentialed_url transport.
    """
    url = streaming_peer_server(_RUNNING_STREAM)
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="run", target="spark", message="disk status?", idempotency_key="t-1",
        no_session=False, tail=True, quiet=False, json=False, file=None, instructions=None,
        url=None, api_key=None))

    assert rc == 0
    out = capsys.readouterr().out
    # The run was started first, then the feed was consumed to its terminal event.
    assert "run_1: started" in out
    assert _StreamingRunPeer.events_requests == ["/v1/runs/run_1/events"]
    # Deltas render inline (a live transcript), lifecycle events are labelled.
    assert "working..." in out
    assert "[tool.start]" in out
    assert "[run.completed]: disk is fine" in out
    # SSE comment frames (`: keepalive`) are consumed, never printed as content.
    assert "keepalive" not in out
    assert _StreamingRunPeer.events_accept == ["text/event-stream"]


def test_tail_action_attaches_to_existing_run(monkeypatch, capsys, streaming_peer_server):
    """``hermes peer tail <target> <run_id>`` replaces ``submit --tail-run``:
    attach to an already-started run without submitting anything."""
    url = streaming_peer_server(_RUNNING_STREAM)
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="tail", target="spark", run_id="run_1", json=False, url=None, api_key=None))

    assert rc == 0
    assert "[run.completed]: disk is fine" in capsys.readouterr().out
    # Nothing was submitted — this is a pure attach.
    assert _StreamingRunPeer.runs == []


def test_tail_returns_one_on_failed_run(monkeypatch, capsys, streaming_peer_server):
    """A server-side failure must set a non-zero exit code (submit.py's rc=1 rule)."""
    url = streaming_peer_server(_sse(
        {"event": "message.delta", "run_id": "run_1", "delta": "trying"},
        {"event": "run.failed", "run_id": "run_1", "error": "provider auth failed"},
    ))
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="tail", target="spark", run_id="run_1", json=False, url=None, api_key=None))

    assert rc == 1
    assert "[run.failed]: provider auth failed" in capsys.readouterr().out


def test_tail_json_emits_raw_event_payloads(monkeypatch, capsys, streaming_peer_server):
    """``--json`` reproduces submit.py's machine-readable one-JSON-per-line feed."""
    url = streaming_peer_server(_RUNNING_STREAM)
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="tail", target="spark", run_id="run_1", json=True, url=None, api_key=None))

    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [event["event"] for event in lines] == [
        "run.started", "message.delta", "message.delta", "tool.start", "run.completed"]


def test_tail_missing_run_id_is_a_usage_error(monkeypatch):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k")

    assert peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="tail", target="spark", run_id="  ", json=False,
        url=None, api_key=None)) == 2


def test_tail_reports_rejected_stream(monkeypatch, capsys, fake_peer_server):
    """The plain fake peer 404s ``/events``; that must be a clean rc=1, not a traceback."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="tail", target="spark", run_id="run_1", json=False, url=None, api_key=None))

    assert rc == 1
    assert "spark" in capsys.readouterr().err


def test_tail_request_strips_bearer_key_across_redirect_origin():
    """The tail stream goes through the same ``open_credentialed_url`` policy as
    every other peer request, so a redirecting peer cannot harvest the key.
    Sibling of test_request_strips_bearer_key_across_redirect_origin."""
    _AttackerOrigin.auth_seen = []
    attacker = HTTPServer(("127.0.0.1", 0), _AttackerOrigin)
    attacker_thread = threading.Thread(target=attacker.serve_forever, daemon=True)
    attacker_thread.start()

    _RedirectingPeer.redirect_target = f"http://127.0.0.1:{attacker.server_port}"
    peer = HTTPServer(("127.0.0.1", 0), _RedirectingPeer)
    peer_thread = threading.Thread(target=peer.serve_forever, daemon=True)
    peer_thread.start()

    try:
        peer_cmd._tail_run_events(
            "spark", f"http://127.0.0.1:{peer.server_port}", "top-secret-peer-key", "run_1")
    finally:
        peer.shutdown()
        peer_thread.join(timeout=5)
        attacker.shutdown()
        attacker_thread.join(timeout=5)

    assert _AttackerOrigin.auth_seen, "redirect target was never reached"
    assert all(header is None for header in _AttackerOrigin.auth_seen), (
        f"peer's Bearer key leaked to the tail redirect target: {_AttackerOrigin.auth_seen}")


# ── capability 2: widened credential resolution ─────────────────────────────
#
# Ported from tests/hermes_cli/test_submit.py's _resolve_target precedence
# block. Upstream's registered-peer path must keep resolving exactly as before;
# the fork only ADDS ways to reach an unregistered gateway.


def _resolve_args(**kw):
    defaults = {"url": None, "api_key": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture()
def no_gateway_env(monkeypatch):
    """No ambient gateway credentials, and an empty ~/.hermes/.env."""
    for name in ("HERMES_GATEWAY_URL", "HERMES_GATEWAY_API_KEY", "API_SERVER_KEY",
                 "HERMES_PEER_DEFAULT_KEY", "HERMES_PEER_SPARK_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda name, default="": "")
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {})


def test_resolve_registered_peer_path_is_unchanged(monkeypatch, no_gateway_env):
    """Upstream's path: bot_peers entry + HERMES_PEER_<NAME>_KEY, nothing else."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://spark.lan:8377"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "registered-key")

    name, profile, peer, key = peer_cmd._resolve_peer_target("spark", _resolve_args())

    assert (name, profile, key) == ("spark", None, "registered-key")
    assert peer["url"] == "http://spark.lan:8377"
    assert peer["hermes_source"] == "bot_peers.spark"


def test_resolve_registered_peer_missing_key_still_hard_fails(monkeypatch, no_gateway_env):
    """A registered peer must NOT silently borrow the gateway-wide key chain —
    that would weaken the registry's per-peer credential guarantee."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://spark.lan:8377"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "unrelated-gateway-key")

    with pytest.raises(PermissionError) as exc:
        peer_cmd._resolve_peer_target("spark", _resolve_args())
    assert "hermes peer add spark" in str(exc.value)


def test_resolve_unknown_peer_still_hard_fails(monkeypatch, no_gateway_env):
    """A typo must never be silently redirected to the default gateway."""
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://the-default-gateway:1")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "k")

    with pytest.raises(LookupError) as exc:
        peer_cmd._resolve_peer_target("sprk", _resolve_args())
    assert "No peer named 'sprk'" in str(exc.value)


def test_resolve_url_flag_reaches_unregistered_gateway(monkeypatch, no_gateway_env):
    """``--url`` + ``--api-key``: submit.py's flag path, no registry entry needed."""
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")

    name, _, peer, key = peer_cmd._resolve_peer_target(
        "ad-hoc", _resolve_args(url="http://from-flag:8000/", api_key="flag-key"))

    assert (name, key) == ("ad-hoc", "flag-key")
    assert peer["url"] == "http://from-flag:8000"
    assert peer["hermes_source"] == "--url"


def test_resolve_url_flag_beats_registered_entry(monkeypatch, no_gateway_env):
    """An explicit --url overrides a registered peer's URL for this one call
    (submit.py: flag beats every other source)."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://registered:1"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "registered-key")

    _, _, peer, key = peer_cmd._resolve_peer_target("spark", _resolve_args(url="http://from-flag:2"))

    assert peer["url"] == "http://from-flag:2"
    # Still the registered peer's own key — only the URL was overridden.
    assert key == "registered-key"


def test_resolve_url_flag_rejects_non_http(monkeypatch, no_gateway_env):
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k")
    with pytest.raises(ValueError):
        peer_cmd._resolve_peer_target("ad-hoc", _resolve_args(url="ftp://nope"))


def test_resolve_default_target_uses_builtin_gateway(monkeypatch, no_gateway_env):
    """``default`` with nothing set → submit.py's DEFAULT_GATEWAY_URL."""
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    monkeypatch.setenv("API_SERVER_KEY", "from-api-server-key")

    name, _, peer, key = peer_cmd._resolve_peer_target("default", _resolve_args())

    assert name == "default"
    assert peer["url"] == peer_cmd.DEFAULT_GATEWAY_URL
    assert "default" in peer["hermes_source"]
    assert key == "from-api-server-key"


def test_resolve_default_target_env_url_beats_builtin(monkeypatch, no_gateway_env):
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://env-wins:1/")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "k")

    _, _, peer, _ = peer_cmd._resolve_peer_target("default", _resolve_args())

    assert peer["url"] == "http://env-wins:1"
    assert peer["hermes_source"] == "env HERMES_GATEWAY_URL"


def test_resolve_default_target_env_url_beats_dotenv(monkeypatch, no_gateway_env):
    """submit.py's precedence: process env beats ~/.hermes/.env for the same name."""
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://env-wins:1")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "k")
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda name, default="": "http://dotenv-loses:2" if name == "HERMES_GATEWAY_URL" else "")

    _, _, peer, _ = peer_cmd._resolve_peer_target("default", _resolve_args())
    assert peer["url"] == "http://env-wins:1"


def test_resolve_default_target_reads_dotenv_url_and_key(monkeypatch, no_gateway_env):
    """Both URL and key resolve out of ~/.hermes/.env when the env is empty."""
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    dotenv = {"HERMES_GATEWAY_URL": "http://from-dotenv:3/",
              "HERMES_GATEWAY_API_KEY": "dotenv-key"}
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda name, default="": dotenv.get(name, ""))

    _, _, peer, key = peer_cmd._resolve_peer_target("default", _resolve_args())

    assert peer["url"] == "http://from-dotenv:3"
    assert peer["hermes_source"] == "~/.hermes/.env HERMES_GATEWAY_URL"
    assert key == "dotenv-key"


def test_resolve_default_target_prefers_peer_key_env(monkeypatch, no_gateway_env):
    """HERMES_PEER_DEFAULT_KEY still wins over the gateway-wide chain, so the
    reserved name can also be registered-style keyed."""
    monkeypatch.setattr(peer_cmd, "_peer_secret",
                        lambda name: "peer-default-key" if name == "default" else "")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-wide")

    _, _, _, key = peer_cmd._resolve_peer_target("default", _resolve_args())
    assert key == "peer-default-key"


def test_resolve_gateway_api_key_beats_api_server_key(monkeypatch, no_gateway_env):
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "specific")
    monkeypatch.setenv("API_SERVER_KEY", "generic")

    _, _, _, key = peer_cmd._resolve_peer_target("default", _resolve_args())
    assert key == "specific"


def test_resolve_api_key_flag_beats_every_env_source(monkeypatch, no_gateway_env):
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "peer-env-key")
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-wide")

    _, _, _, key = peer_cmd._resolve_peer_target("default", _resolve_args(api_key="flag-key"))
    assert key == "flag-key"


def test_resolve_keyless_widened_path_names_the_whole_chain(monkeypatch, no_gateway_env):
    """Deliberate tightening vs submit.py, which allowed an unauthenticated
    request and let the gateway 401. The error names every accepted source."""
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")

    with pytest.raises(PermissionError) as exc:
        peer_cmd._resolve_peer_target("default", _resolve_args())
    message = str(exc.value)
    assert "--api-key" in message
    assert "HERMES_PEER_DEFAULT_KEY" in message
    assert "HERMES_GATEWAY_API_KEY" in message
    assert "API_SERVER_KEY" in message


def test_default_target_end_to_end_without_any_registry(monkeypatch, capsys, fake_peer_server):
    """The full `hermes submit` replacement: no bot_peers entry anywhere, the
    gateway URL + key come from the environment alone."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda name, default="": "")
    monkeypatch.setenv("HERMES_GATEWAY_URL", fake_peer_server)
    monkeypatch.setenv("HERMES_GATEWAY_API_KEY", "gateway-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="run", target="default", message="do x", idempotency_key="t-2",
        no_session=True, tail=False, quiet=False, json=True, file=None, instructions=None,
        url=None, api_key=None))

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run_1"
    assert all(a == "Bearer gateway-key-123456" for a in _FakePeer.auth_seen)


# ── capability 3: session vs --no-session ───────────────────────────────────


def _run_args(**kw):
    defaults = {
        "peer_action": "run", "target": "spark", "message": "long task",
        "idempotency_key": "t-3", "no_session": False, "tail": False, "quiet": False,
        "json": True, "file": None, "instructions": None, "url": None, "api_key": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_run_defaults_to_the_canonical_bot_chat_session(monkeypatch, capsys, fake_peer_server):
    """Upstream's forced-session behavior is the DEFAULT, unchanged: the remote
    Bot Chat is what gives a bot-to-bot exchange continuity and an inspectable
    transcript. Nothing about --no-session may alter this path."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(_run_args())

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "bc_existing"
    # session_id rode along in the POST body.
    assert _FakePeer.runs == [{"input": "long task", "session_id": "bc_existing"}]


def test_run_no_session_omits_session_id_entirely(monkeypatch, capsys, fake_peer_server):
    """``--no-session`` ports submit.py's session-less POST: no session_id key at
    all, so the peer assigns the run its own session (api_server_runs.py:463,
    ``session_id = selected_session_id or run_id``)."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(_run_args(no_session=True))

    assert rc == 0
    assert _FakePeer.runs == [{"input": "long task"}]
    assert "session_id" not in _FakePeer.runs[0]
    # The run still reports whichever session the peer gave it.
    assert json.loads(capsys.readouterr().out)["session_id"] == "run_1"


def test_run_no_session_skips_bot_chat_lookup_and_create(monkeypatch, fake_peer_server):
    """The session-less path must not touch /api/sessions at all — that is the
    round-trip saving, and it is what lets a key without session-management
    rights still start a run."""
    _FakePeer.sessions = []
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    def _explode(*a, **kw):
        raise AssertionError("--no-session must not resolve or create a Bot Chat")

    monkeypatch.setattr(peer_cmd, "_ensure_bot_chat", _explode)

    assert peer_cmd.cmd_peer(_run_args(no_session=True)) == 0
    # No session was created on the peer either.
    assert _FakePeer.sessions == []


def test_run_no_session_keeps_the_idempotency_key(monkeypatch, fake_peer_server):
    """Upstream's Idempotency-Key bookkeeping survives on the session-less path
    (submit.py had no idempotency at all — this is a net gain, not a trade)."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    assert peer_cmd.cmd_peer(_run_args(no_session=True, idempotency_key="ticket-9")) == 0
    assert _FakePeer.run_idempotency_keys == ["ticket-9"]


def test_dm_is_untouched_by_no_session(monkeypatch, capsys, fake_peer_server):
    """``dm`` IS the Bot Chat feature; it has no session-less mode and must keep
    resolving the canonical chat even when the flag namespace exists."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="dm", target="spark", message="ping", json=True, file=None,
        url=None, api_key=None))

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "bc_1"
    assert _FakePeer.chats == ["ping"]


# ── remaining submit.py coverage: prompt sources, instructions, -q ──────────


def test_run_reads_message_from_file(monkeypatch, tmp_path, fake_peer_server):
    """Ports test_submit.py::test_read_prompt_reads_file onto ``--file``."""
    _FakePeer.sessions = ["bc_existing"]
    task = tmp_path / "task.md"
    task.write_text("do this from a file\n")
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    assert peer_cmd.cmd_peer(_run_args(message=None, file=str(task))) == 0
    assert _FakePeer.runs[0]["input"] == "do this from a file"


def test_run_missing_file_is_a_clean_usage_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k")

    rc = peer_cmd.cmd_peer(_run_args(message=None, file=str(tmp_path / "nope.md")))

    assert rc == 2
    assert "message file" in capsys.readouterr().err


def test_run_with_no_message_anywhere_is_a_usage_error(monkeypatch, capsys):
    """Ports test_submit.py::test_read_prompt_errors_when_no_source_and_tty."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    rc = peer_cmd.cmd_peer(_run_args(message=None))

    assert rc == 2
    assert "Message required" in capsys.readouterr().err


def test_run_passes_instructions(monkeypatch, fake_peer_server):
    """Ports test_submit.py::test_post_run_passes_instructions."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    assert peer_cmd.cmd_peer(_run_args(instructions="be terse", no_session=True)) == 0
    assert _FakePeer.runs == [{"input": "long task", "instructions": "be terse"}]


def test_run_quiet_prints_only_the_run_id(monkeypatch, capsys, fake_peer_server):
    """Ports test_submit.py::test_submit_command_quiet_prints_only_run_id, so
    RUN=$(hermes peer run default -q "...") keeps working."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(_run_args(quiet=True, json=False))

    assert rc == 0
    assert capsys.readouterr().out.strip() == "run_1"


def test_run_human_output_advertises_tail_and_status(monkeypatch, capsys, fake_peer_server):
    """submit.py printed follow-up commands; keep that affordance on the new ones."""
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    assert peer_cmd.cmd_peer(_run_args(json=False)) == 0
    out = capsys.readouterr().out
    assert "hermes peer tail spark run_1" in out
    assert "hermes peer status spark run_1" in out

