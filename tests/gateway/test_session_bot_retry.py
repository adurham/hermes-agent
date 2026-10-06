"""The owner retries a transiently failed Bot DM delivery exactly once, never unknown execution.

``gateway/session_bot.py`` admits a Bot Chat delivery into the authority FIFO. When that admission
SETTLES ``failed`` with a committed result whose error classifies transient
(``tools.bot_failure_reasons.result_retry_action``), one retry is admitted under the derived
identity ``bot:<id>:retry``; the delivery's receipt follows it. A repeated ``deliver`` of the same
id or an owner restart (``recover_bot_deliveries``) re-reads that admission instead of minting
another, and an unknown/non-transient admission is never replayed.
"""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway.session_contract import Principal
from hermes_state_runtime import list_session_admissions

KEY = 'c' * 32


@pytest_asyncio.fixture
async def bot(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_local import create_local_session
    from gateway.session_local_title import title_new_session
    from hermes_state import SessionDB

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / 'state.db')
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='fixture')
    owner = Principal('uid:1000', 'default', frozenset({'session:create', 'session:read', 'session:submit'}), 'native')
    chat = create_local_session(authority, owner, {'request_id': 'bot', 'source': 'gui', 'cwd': str(tmp_path),
                                                    'model': 'fixture', 'toolsets': []})
    title_new_session(authority, chat, 'Bot Chat')
    errors = []

    async def execute(authority, ref, row):
        error = errors.pop(0) if errors else None
        if error is None:
            return 'pong'
        authority.pending_results[row['admission_id']] = {
            'result': {'final_response': '', 'messages': [], 'failed': True, 'error': error}, 'usage': {}}
        return ''

    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    try:
        yield SimpleNamespace(authority=authority, chat=chat.session_id, errors=errors, home=tmp_path,
                              connection=SimpleNamespace(authority=authority, actor=owner))
    finally:
        store._db.close()


def _admissions(bot):
    return list_session_admissions(bot.authority.db, session_id=bot.chat, pending_only=False)


async def _settled(bot, count):
    from gateway.session_bot import _result
    from tools.bot_live_delivery import _read, _root
    for _ in range(200):
        await asyncio.sleep(0.02)
        rows = _admissions(bot)
        if len(rows) >= count and all(r['status'] == 'terminal' for r in rows):
            await asyncio.sleep(0.05)  # let the receipt task observe the settle
            return _result(bot.authority, _read(_root(bot.home) / f'{KEY}.json'))
    raise AssertionError(f'admissions never settled: {_admissions(bot)}')


@pytest.mark.asyncio
async def test_transient_failure_retries_exactly_once_and_the_receipt_follows_the_retry(bot):
    from gateway.session_bot import deliver, recover_bot_deliveries
    params = dict(id=KEY, profile='default', message='ping')
    bot.errors[:] = ['Error code: 429 - rate limit exceeded']
    first = await deliver(bot.connection, params)
    receipt = await _settled(bot, 2)
    assert receipt['status'] == 'settled' and receipt['reply'] == 'pong', receipt
    assert receipt['admission_id'] == first['admission_id'] and receipt['retry_admission_id']
    assert [r['request_id'] for r in _admissions(bot)] == ['bot:' + KEY, 'bot:' + KEY + ':retry']
    # A repeated delivery of the same id and an owner restart re-read the one retry; never a third.
    again = await deliver(bot.connection, params)
    assert again['status'] == 'settled' and again['reply'] == 'pong'
    await recover_bot_deliveries(bot.authority)
    await asyncio.sleep(0.05)
    assert len(_admissions(bot)) == 2


@pytest.mark.asyncio
async def test_retry_that_fails_again_and_non_transient_failures_are_never_replayed(bot):
    from gateway.session_bot import deliver, recover_bot_deliveries
    # The retry itself fails transiently: the sender sees that failure, no second retry.
    bot.errors[:] = ['HTTP 503 server error', 'HTTP 503 server error']
    await deliver(bot.connection, dict(id=KEY, profile='default', message='ping'))
    receipt = await _settled(bot, 2)
    assert receipt['status'] == 'failed' and receipt['retry_admission_id']
    await deliver(bot.connection, dict(id=KEY, profile='default', message='ping'))
    await recover_bot_deliveries(bot.authority)
    await asyncio.sleep(0.1)
    assert len(_admissions(bot)) == 2
    # Auth failures do not classify transient: settled failed, never retried.
    other = 'd' * 32
    bot.errors[:] = ['Error code: 401 - invalid api key']
    await deliver(bot.connection, dict(id=other, profile='default', message='auth'))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if all(r['status'] == 'terminal' for r in _admissions(bot)) and len(_admissions(bot)) == 3:
            break
    await recover_bot_deliveries(bot.authority)
    await asyncio.sleep(0.1)
    assert [r['request_id'] for r in _admissions(bot)][2:] == ['bot:' + other]
