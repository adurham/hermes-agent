"""Messaging-session reads after the conversation moved on (RT-18, TUI-19 on messaging rows, #134889).

A messaging lane's control card (goal, loop, heartbeat) follows the conversation through compression,
and an old conversation opened read-only for a refused delete is not a live session.
"""
import pytest

from tests.gateway.test_authority_cold_route_recovery import _cold, _compressed_lane, _ref, _superseded


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))


@pytest.mark.asyncio
async def test_the_control_card_of_a_compressed_messaging_session_reads_its_current_segment(tmp_path):
    from hermes_cli.goals import GoalState
    from gateway.session_ancillary import control_snapshot
    _compressed_lane(tmp_path)
    cold = await _cold(tmp_path)
    cold.authority.authorize(cold.actor, _ref('lane-root'), 'session:read')
    # Compression moves automation state to the continuation and archives the root's copy.
    cold.authority.db.set_meta('goal:lane-root', GoalState('ROOT_GOAL', status='cleared').to_json())
    cold.authority.db.set_meta('goal:lane-tip', GoalState('TIP_GOAL').to_json())

    snapshot = control_snapshot(cold.authority, _ref('lane-root'))

    assert snapshot['goal']['title'] == 'TIP_GOAL'


@pytest.mark.asyncio
async def test_an_unviewed_superseded_mount_is_not_listed_live(tmp_path):
    """A refused delete of an old conversation leaves its read-only mount behind with no viewer."""
    from gateway.session_controls import AuthorityConnection
    cold, _successor = await _superseded(tmp_path)
    connection = AuthorityConnection(cold.authority, object(), {
        'provider': 'local', 'user_id': 'viewer', 'profile_id': 'fixture', 'instance_id': 'cold',
        'capabilities': ['session:read']})
    resumed = await connection.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 'old'}})
    assert cold.authority.sessions['old'].superseded, resumed
    listed = await connection.dispatch({'id': 2, 'method': 'session.list', 'params': {}})
    assert 'old' in {row['id'] for row in listed['result']['sessions']}, 'a viewed historic mount stays listed'

    await connection.dispatch({'id': 3, 'method': 'session.detach', 'params': {
        'session_id': 'old', 'subscription_id': resumed['result']['subscription_id']}})
    listed = await connection.dispatch({'id': 4, 'method': 'session.list', 'params': {}})
    active = await connection.dispatch({'id': 5, 'method': 'session.active_list', 'params': {}})

    assert 'old' not in {row['id'] for row in listed['result']['sessions']}
    assert 'old' not in {row.get('id') or row.get('session_id') for row in active['result']['sessions']}
