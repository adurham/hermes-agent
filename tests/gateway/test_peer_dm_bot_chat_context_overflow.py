"""A peer DM that overflows the LOCAL Bot Chat's context auto-resets it, and the follow-up is answered.

``hermes peer dm`` lands in the peer's Bot Chat, a ``source='local'`` session the authority FIFO owns
(not an API conversation, so the API_SERVER exemption from the compression-exhaustion reset never
applies). The reset ran INSIDE the overflowing turn, while that turn's own admission was still
``started``: ``reset_local_target`` refused it (``session_busy``), the exception became a failed turn
that persisted the DM a second time, and the Bot Chat kept its oversized transcript, so every later
DM overflowed too (HTTP 502 forever). Real daemon (``python -m gateway.run``), real api_server door,
real SessionDB and authority; only the model is a loopback window-capped stub.
"""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon

KEY = 'lc23-peer-dm-overflow-key-0123456789'
CAP = 30000  # request bytes the stub's "model" accepts


class WindowCappedModel(BaseHTTPRequestHandler):
    """Main (tool-bearing or not) requests over CAP bytes get a numberless 400 context overflow."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        if len(raw) > CAP:
            self.server.rejected += 1
            payload = json.dumps({'error': {'message': "This model's maximum context length was exceeded. "
                                                       'Please reduce the length of the messages.',
                                            'type': 'invalid_request_error',
                                            'code': 'context_length_exceeded'}}).encode()
            return self._send(400, payload, 'application/json')
        text = next((m.get('content', '') for m in reversed(body.get('messages', [])) if m['role'] == 'user'), '')
        message = {'role': 'assistant', 'content': 'ACK ' + str(text)[:40]}
        payload = json.dumps({'id': 'local', 'model': body.get('model'), 'choices': [
            {'index': 0, 'message': message, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode()
        kind = 'application/json'
        if body.get('stream'):
            payload = ('data: ' + json.dumps({'id': 'local', 'choices': [
                {'index': 0, 'delta': message, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        self._send(200, payload, kind)

    def _send(self, status, payload, kind):
        try:
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _get(port, path):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Authorization': f'Bearer {KEY}'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _post(port, path, body, timeout=180):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode(), method='POST',
                                 headers={'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b'{}')


@pytest.fixture(scope='module')
def overflow_run(tmp_path_factory):
    """Two DMs into a fresh headless Bot Chat: an oversized one, then a short follow-up."""
    tmp_path = tmp_path_factory.mktemp('lc23')
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    model = ThreadingHTTPServer(('127.0.0.1', 0), WindowCappedModel)
    model.rejected = 0
    threading.Thread(target=model.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{model.server_port}/v1'
    api_port = _free_port()
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'fake-model', 'base_url': url, 'context_length': 65536},
        'agent': {'api_max_retries': 1},
        'approvals': {'mode': 'off'},
        'compression': {'protect_last_n': 2},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False},
        'auxiliary': {'title_generation': {'enabled': False},
                      'compression': {'provider': 'custom', 'model': 'fake-model', 'base_url': url}},
        'platforms': {'api_server': {'enabled': True, 'extra': {'host': '127.0.0.1', 'port': api_port, 'key': KEY}}},
        'platform_toolsets': {'cli': [], 'api_server': []}}))
    env = child_env() | dict(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
                             OPENAI_API_KEY='loopback-only', PYTHONUNBUFFERED='1')
    big = 'MKBIG ' + 'lorem ipsum dolor sit amet ' * 12000  # the live repro's DM (capped to 64 KB at the door)
    try:
        with daemon(root, home, env, barrier=False):
            status, created = _post(api_port, '/api/sessions', {'title': 'Bot Chat', 'source': 'bot_peer_dm'})
            assert status == 201, created
            sid = created['session']['id']
            first = _post(api_port, f'/api/sessions/{sid}/chat', {'message': big})
            # ``hermes peer dm`` resolves the Bot Chat by exact title before every DM (``_find_bot_chat``).
            found = _get(api_port, '/api/sessions?limit=200&title=Bot+Chat&include_hidden=1')['data']
            follow = _post(api_port, f'/api/sessions/{sid}/chat', {'message': 'short follow-up MKFOLLOW'})
    finally:
        model.shutdown()
    log = (home / 'restart.log').read_text(errors='replace')
    for name in ('gateway.log', 'errors.log'):
        if (home / 'logs' / name).exists():
            log += (home / 'logs' / name).read_text(errors='replace')
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        rows = db.execute("SELECT session_id, role, substr(coalesce(content,''),1,24) FROM messages ORDER BY id").fetchall()
        bot_chat = db.execute('SELECT end_reason FROM sessions WHERE id=?', (sid,)).fetchone()
    return SimpleNamespace(sid=sid, found=[row['id'] for row in found], first=first, follow=follow, log=log, rows=rows, rejected=model.rejected,
                           bot_chat_end=bot_chat[0])


def _stored(run, marker):
    return sum(1 for _, role, text in run.rows if role == 'user' and text.startswith(marker))


@pytest.mark.platforms('linux')
def test_peer_dm_overflow_resets_the_local_bot_chat_and_answers_the_follow_up(overflow_run):
    run = overflow_run
    assert run.rejected >= 1, 'the first DM never overflowed'
    assert 'session_busy' not in run.log, [line for line in run.log.splitlines() if 'session_busy' in line][:5]
    # The overflow is answered with its typed reason, and the Bot Chat was reset after that turn settled.
    assert run.first[0] == 502 and run.first[1]['error']['code'] == 'context_overflow', run.first
    assert run.bot_chat_end == 'session_reset', run.rows
    # The client's title lookup still finds the reset Bot Chat (no title-collision create).
    assert run.found == [run.sid], run.found
    # The follow-up runs on the fresh transcript, is answered, and is stored once.
    assert run.follow[0] == 200, (run.follow, run.rows)
    assert run.follow[1]['message']['content'].startswith('ACK short follow-up'), run.follow
    assert _stored(run, 'short follow-up') == 1, run.rows


@pytest.mark.platforms('linux')
@pytest.mark.xfail(strict=True, reason=(
    "The overflowing DM's first attempt fails transiently, is closed with the durable failed-turn "
    "boundary (#107070), and the owner's one Bot Chat retry (session_bot._maybe_retry) runs a fresh "
    "turn after that boundary, so it appends the DM a second time. Adopting the closed row needs an "
    "owner-side adoption seam that the retry declines by design."))
def test_overflowing_peer_dm_is_stored_once(overflow_run):
    assert _stored(overflow_run, 'MKBIG') == 1, overflow_run.rows
