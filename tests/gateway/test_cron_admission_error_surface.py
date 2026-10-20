"""A message typed into a cron session fails with a renderable read-only descriptor (#141348)."""
import json
import threading
from types import SimpleNamespace

import pytest

# Import-time update recovery stats the checkout's git dir; collection runs before the home I/O guard.
import gateway.session_managed_worker
from gateway.session_authority import SessionAuthority


@pytest.mark.asyncio
async def test_typed_turn_in_cron_session_settles_with_read_only_surface(owner, monkeypatch):
    from cron import scheduler
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_contract import SessionRef
    from gateway.session_local import LocalSessionAdapter
    from gateway.session_results import prepare_result
    from gateway.session_settlement_recovery import completion_payload
    from hermes_state_runtime import admit_session_input, claim_session_input

    session_id = 'cron-session'
    owner.db.create_session(session_id, source='local')
    source = SessionSource(platform=Platform.LOCAL, chat_id=session_id, user_id='cron-user', profile='default')
    owner.sessions[session_id] = SimpleNamespace(
        source=source, review=None, event_stream=SimpleNamespace(lock=threading.RLock()), subscribers=())
    adapter = LocalSessionAdapter(owner)
    adapter.register_source(source)
    adapter.policies[session_id] = SimpleNamespace(source='cron', request_json=json.dumps(
        {'cron_job': {'id': 'job-1'}, 'extra_prompt': '', 'request_id': 'cron:job-1:fire-1'}))
    owner.runner._adapter_for_source = lambda _source: adapter
    monkeypatch.setattr(scheduler, 'run_job', lambda *a, **k: pytest.fail('a typed turn must not run the job'))
    # The person's message, not the fire's own admission.
    admit_session_input(owner.db, epoch=owner.epoch, principal_id='desktop-user', session_id=session_id,
                        request_id='typed-1', payload={'text': 'run this again now'})
    row = claim_session_input(owner.db, epoch=owner.epoch, session_id=session_id)

    response, outcome = await SessionAuthority._execute_claimed(
        owner, SessionRef('default', session_id), owner.sessions[session_id], row)

    captured = owner.pending_results[row['admission_id']]
    settled_outcome, _stored = prepare_result(row, response, outcome, captured)
    assert settled_outcome == 'failed'
    complete = completion_payload(row, {'outcome': settled_outcome}, response, captured)
    assert complete['status'] == 'error'
    assert complete['error_surface'] == {'layer': 'gateway', 'code': 'cron_session_read_only', 'retryable': False}
    assert 'scheduled job' in complete['text'] and 'Start a new chat' in complete['text']
