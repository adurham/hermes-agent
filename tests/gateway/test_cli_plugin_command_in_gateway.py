"""A classic CLI plugin command runs in the gateway and its ``ctx.call_mcp`` reaches the gateway's MCP
registry (#134889 CLI-13, PLG-7).

A real daemon (``gateway.run``) loads a user plugin whose ``/probe-mcp-status`` calls ``probe-mcp``, a
fake server in the daemon's own MCP registry (tests/gateway/fixtures/plugin_mcp_daemon.py). The CLI's
dispatch reads the daemon's ``commands.catalog`` and sends ``slash.exec`` over its websocket, so the
command goes through ``gateway/session_commands.py`` -> ``_hm_run_plugin_handler``.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import signal
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, rpc, websocket
from tests.waiting import wait_until

_PLUGIN = '''import json
import os


def register(ctx):
    def status(arg):
        reply = ctx.call_mcp("probe-mcp", "echo", {"text": arg})
        return "PLUGIN_PID=%d MCP=%s" % (os.getpid(), json.dumps(reply, default=str))
    ctx.register_command("probe-mcp-status", status, description="Ask the fake MCP server")
    ctx.register_command("probe_under", lambda arg: "EXACT_UNDERSCORE", description="Exact name")
    ctx.register_command("probe-under", lambda arg: "HYPHEN_ALIAS", description="Telegram alias form")
    ctx.register_command("probe_only", lambda arg: "UNDERSCORE_ONLY", description="No hyphen twin")

    def boom(arg):
        raise RuntimeError("probe handler exploded")
    ctx.register_command("probe-boom", boom, description="Always fails")
'''
# One command per gateway life: A serves /alpha, the restarted B only /beta.
_LIFE = '''import os


def register(ctx):
    ctx.register_command("NAME", lambda arg: "RAN_NAME PID=%d" % os.getpid(), description="NAME")
'''


@contextmanager
def _plugin_home(tmp_path, source):
    """``(root, home, env)``: a profile home with plugin ``probe-cmd`` and a loopback model."""
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    plugin = home / 'plugins' / 'probe-cmd'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text("name: probe-cmd\nversion: '1.0'\n")
    (plugin / '__init__.py').write_text(source)
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'plugin-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []},
        'plugins': {'enabled': ['probe-cmd'], 'entries': {'probe-cmd': {'mcp_allowlist': ['probe-mcp']}}},
    }))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1',
               PYTHONDONTWRITEBYTECODE='1')
    try:
        yield root, home, env
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)


def _no_local_plugins(monkeypatch):
    def no_local_plugin(*_a, **_k):
        pytest.fail('the CLI process loaded or ran plugin code')
    for name in ('get_plugin_command_handler', 'discover_plugins', 'get_plugin_commands'):
        monkeypatch.setattr(f'hermes_cli.plugins.{name}', no_local_plugin)


class _Wire:
    """The CLI connection's ``call`` over the daemon's real websocket, on one loop thread."""
    closed = final = False
    epoch = 1

    def __init__(self, home, desc):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.calls = []

        async def _open():
            return await websocket(home, desc)
        self.ws = self._run(_open())

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(60)

    def call(self, method, **params):
        from hermes_cli.gateway_client import GatewayClientError
        self.calls.append(method)
        reply = self._run(rpc(self.ws, method, **params))
        if 'error' in reply:
            raise GatewayClientError(str(reply['error'].get('message')))
        return reply['result']

    def call_tagged(self, method, **params):
        return self.call(method, **params), self.epoch

    def close(self):
        self._run(self.ws.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


@pytest.mark.platforms("linux")
def test_cli_plugin_command_runs_in_the_gateway_and_reaches_its_mcp_registry(tmp_path, monkeypatch, capsys):
    import cli as cli_mod
    from cli import HermesCLI
    _no_local_plugins(monkeypatch)
    monkeypatch.setattr(cli_mod, '_ensure_skill_commands', dict)
    monkeypatch.setattr(cli_mod, 'get_skill_bundles', dict)
    work = tmp_path / 'work'
    work.mkdir()
    with _plugin_home(tmp_path, _PLUGIN) as (root, home, env):
        with daemon(root, home, env, barrier=True, fixture='plugin_mcp_daemon.py') as (proc, desc):
            wire = _Wire(home, desc)
            try:
                sid = wire.call('session.create', request_id='plugin-cli', source='cli', toolsets=[],
                                cwd=str(work))['session_id']
                monkeypatch.setattr('hermes_cli.gateway_side.connection_for', lambda _cli: wire)
                c = HermesCLI.__new__(HermesCLI)
                c.config, c.session_id = {}, sid
                capsys.readouterr()
                assert c._process_unregistered_slash('/probe-mcp-status Deep Check',
                                                     '/probe-mcp-status deep check') is True
                out = capsys.readouterr().out
                calls = list(wire.calls)
                typed_outs = {}
                for typed in ('/probe_under', '/probe_only', '/probe-boom'):
                    assert c._process_unregistered_slash(typed, typed) is True
                    typed_outs[typed] = capsys.readouterr().out
            finally:
                wire.close()
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=20)
    assert calls[-2:] == ['commands.catalog', 'slash.exec'], calls
    assert 'EXACT_UNDERSCORE' in typed_outs['/probe_under'], typed_outs
    assert 'UNDERSCORE_ONLY' in typed_outs['/probe_only'], typed_outs
    # main's local path printed a failing handler's error; the gateway path must not swallow it
    assert 'Plugin command error: probe handler exploded' in typed_outs['/probe-boom'], typed_outs
    assert proc.pid != os.getpid()
    assert f'PLUGIN_PID={proc.pid} ' in out, out  # the handler ran in the gateway process
    mcp = json.loads(json.loads(out.split('MCP=', 1)[1].strip())['result'])
    assert mcp == {'tool': 'echo', 'arguments': {'text': 'Deep Check'}, 'registry_pid': proc.pid}, out


def _dial_running(home):
    """The production dial pinned to an owner already running (``connect_gateway(endpoint)``), so a
    redial while none is up fails instead of starting one the test does not own."""
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway
    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    @asynccontextmanager
    async def dial():
        found = await asyncio.to_thread(discover_gateway_endpoint, home)
        if found.state != 'ready' or found.endpoint is None:
            raise GatewayClientError(f'no gateway is serving this home ({found.state})')
        async with connect_gateway(found.endpoint) as client:
            yield client
    return dial


@pytest.mark.platforms("linux")
def test_a_redial_to_a_restarted_gateway_and_slash_new_reread_the_plugin_commands(tmp_path, monkeypatch, capsys):
    """The classic CLI's own GatewayConnection and GatewaySessionStore across a real owner restart:
    gateway A serves /alpha, B (same home and socket) only /beta. A cached /alpha never runs: while no
    gateway answers it is a named refusal (no same-named skill stands in), on B it is no plugin
    command. /new through the gateway session store re-reads B's catalog for the new session."""
    import cli as cli_mod
    from cli import HermesCLI
    from hermes_cli.gateway_connection import GatewayConnection
    from hermes_cli.gateway_session_store import GatewaySessionStore
    _no_local_plugins(monkeypatch)
    skills, ran_skill = {'/alpha': {'name': 'alpha'}}, []
    monkeypatch.setattr(cli_mod, '_ensure_skill_commands', lambda: dict(skills))
    monkeypatch.setattr(cli_mod, 'get_skill_bundles', dict)
    monkeypatch.setattr(HermesCLI, '_run_skill_slash_command', lambda self, *a: ran_skill.append(a[0]))
    with _plugin_home(tmp_path, _LIFE.replace('NAME', 'alpha')) as (root, home, env):
        monkeypatch.setenv('HERMES_HOME', str(home))
        conn = GatewayConnection(connect=_dial_running(home))
        sent, real = [], conn.call_tagged

        def spy(method, **params):
            sent.append((method, params.get('session_id')))
            return real(method, **params)
        monkeypatch.setattr(conn, 'call_tagged', spy)
        c = HermesCLI()
        c.agent, c._session_db, c._gateway_session_open = None, GatewaySessionStore(conn), False

        def typed(text):
            del sent[:]
            assert c._process_unregistered_slash(text, text) is True
            return capsys.readouterr().out, [m for m, _ in sent]
        try:
            with daemon(root, home, env, barrier=True, fixture='plugin_mcp_daemon.py') as (a, _desc):
                c._ensure_gateway_session()  # the CLI's startup session.create on A
                first = c.session_id
                capsys.readouterr()
                on_a = typed('/alpha')
                a.send_signal(signal.SIGINT)
                a.wait(timeout=20)
            wait_until(lambda: conn.closed, timeout=20, what='the CLI connection to see gateway A hang up')
            gone = typed('/alpha')
            (home / 'plugins' / 'probe-cmd' / '__init__.py').write_text(_LIFE.replace('NAME', 'beta'))
            skills.clear()
            with daemon(root, home, env, barrier=False) as (b, _desc):
                on_b = typed('/beta')
                epoch_b = conn.epoch
                stale = typed('/alpha')
                c.new_session(silent=True)
                capsys.readouterr()
                del sent[:]
                assert c._process_unregistered_slash('/beta', '/beta') is True
                after_new, new_calls = capsys.readouterr().out, list(sent)
                b.send_signal(signal.SIGINT)
                b.wait(timeout=20)
        finally:
            conn.close()
    assert on_a[1] == ['commands.catalog', 'slash.exec'] and f'RAN_alpha PID={a.pid}' in on_a[0], on_a
    # A hung up and nothing serves the home: the cached /alpha is not sent and the skill does not run
    assert 'slash.exec' not in gone[1] and "command list is unavailable" in gone[0], gone
    assert 'nothing was run' in gone[0] and ran_skill == [], (gone, ran_skill)
    assert epoch_b == 2 and on_b[1] == ['commands.catalog', 'slash.exec'], (epoch_b, on_b)
    assert f'RAN_beta PID={b.pid}' in on_b[0] and b.pid != a.pid, on_b
    # B's catalog has no /alpha: one refresh on the miss, never a slash.exec, no plugin output
    assert stale[1] == ['commands.catalog'] and 'RAN_alpha' not in stale[0], stale
    assert c.session_id != first and conn.epoch == epoch_b
    assert new_calls == [('commands.catalog', None), ('slash.exec', c.session_id)], new_calls
    assert f'RAN_beta PID={b.pid}' in after_new, after_new
