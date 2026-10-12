"""The classic CLI's per-turn ``prompt.submit`` fields survive canonical admission (CALLMAP W2).

Main's HermesCLI sends the model an API-only message (voice prefix, model-switch and skills-reload
notes) while persisting the clean text, names the turn's author, and runs ``/model --once`` / ``/moa``
as a one-turn override. Each is committed with the admission row as ``cli_turn_v1`` and bound to that
admission's execution only.
"""
import pytest

from tests.gateway.test_surface_admission import _authority


@pytest.mark.asyncio
async def test_cli_turn_fields_are_admitted_and_bound_to_their_own_turn(tmp_path, monkeypatch):
    from gateway.session_cli_turn import cli_turn
    from gateway.session_controls import AuthorityConnection
    from gateway.session_ingress import admission_author
    from hermes_state_runtime import list_session_admissions

    seen = []
    async def answer(event):
        seen.append((event.text, cli_turn(), admission_author.get()))
        return 'ok'
    authority = await _authority(tmp_path, monkeypatch, answer)
    connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    author = {'id': 'cli:alice', 'name': 'Alice', 'is_bot': False}
    try:
        await connection.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
        for rid, extra in (
                ('full', {'turn_author': author, 'api_text': '[voice] hi',
                          'model_override': {'model': ' gpt-x ', 'provider': 'openrouter'}}),
                ('plain', {})):
            reply = await connection.dispatch({'id': 2, 'method': 'prompt.submit', 'params': {
                'session_id': 's', 'submission_id': rid, 'text': rid, **extra}})
            assert reply.get('result', {}).get('status') == 'queued', reply
            await authority.sessions['s'].task
        for rid, bad in (('no-model', {'model_override': {'provider': 'x'}}),
                         ('extra-key', {'model_override': {'model': 'm', 'base_url': 'http://x'}}),
                         ('bad-text', {'api_text': 7}),
                         ('bad-author', {'turn_author': 'not json'})):
            refused = await connection.dispatch({'id': 3, 'method': 'prompt.submit', 'params': {
                'session_id': 's', 'submission_id': rid, 'text': 'x', **bad}})
            assert refused['error']['message'] == 'invalid_params', (rid, refused)
    finally:
        await connection.close()

    rows = {row['request_id']: row for row in list_session_admissions(authority.db, session_id='s', pending_only=False)}
    assert rows['full']['payload']['cli_turn_v1'] == {
        'turn_author': author, 'api_text': '[voice] hi',
        'model_override': {'model': 'gpt-x', 'provider': 'openrouter'}}
    assert 'cli_turn_v1' not in rows['plain']['payload']
    assert not {'no-model', 'extra-key', 'bad-text', 'bad-author'} & set(rows)
    assert seen[0][0] == 'full' and seen[0][1] == rows['full']['payload']['cli_turn_v1'] and seen[0][2] == author
    # The next admission never inherits the previous turn's fields.
    assert seen[1][0] == 'plain' and seen[1][1] == {}
