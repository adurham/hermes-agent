"""The classic CLI's per-turn ``prompt.submit`` fields survive canonical admission (CALLMAP W2).

Main's HermesCLI sends the model an API-only message (voice prefix, model-switch and skills-reload
notes) while persisting the clean text, names the turn's author, and runs ``/model --once`` / ``/moa``
as a one-turn override. Each is committed with the admission row as ``cli_turn_v1`` and bound to that
admission's execution only.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform

from tests.gateway.test_surface_admission import _authority


@pytest.mark.asyncio
async def test_cli_turn_fields_are_admitted_and_bound_to_their_own_turn(tmp_path, monkeypatch):
    from gateway.session_cli_turn import cli_turn
    from gateway.session_controls import AuthorityConnection
    from gateway.session_ingress import admission_author
    from hermes_state_runtime import list_session_admissions

    seen = []
    async def answer(event):
        seen.append((event.text, cli_turn(event.message_id), cli_turn('another-event'), admission_author.get()))
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
    assert seen[0] == ('full', rows['full']['payload']['cli_turn_v1'], {}, author)
    # The next admission never inherits the previous turn's fields.
    assert seen[1][:3] == ('plain', {}, {})


class _RecordingAgent:
    built: list = []

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)
        self.model = kwargs["model"]
        self.session_id = kwargs.get("session_id")
        self.tools = []
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=200_000)
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.calls = []

    def run_conversation(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return {"final_response": "ok", "messages": []}


def _gateway_runner():
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = runner._prefill_messages = None
    runner._pending_model_notes = runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._pre_agent_fallback_notice = None
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = (
        "session-model", {"provider": "custom", "api_key": "k1", "base_url": "http://session"})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.side_effect = lambda _msg, model, rt: {
        "model": model, "runtime": {k: v for k, v in rt.items() if k != "request_overrides"}}
    runner._agent_config_signature.return_value = ("sig",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    for lane in ("_is_telegram_topic_lane", "_is_discord_auto_thread_lane", "_is_relay_discord_channel_lane"):
        getattr(runner, lane).return_value = False
    return runner


def _run_turn(runner, admission_id="adm-1"):
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
        message="clean text", history=[], session_id="sid", session_key="key", user_config={},
        inbound_message_id=admission_id, AIAgent=_RecordingAgent, resolve_display_setting=lambda *_a: False,
        _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=False))
    result = TurnRunner(runner, ctx).run_sync()
    return result, ctx.agent_holder[0]


def test_the_turn_sends_api_text_persists_the_clean_text_and_runs_the_one_turn_model():
    from gateway.session_cli_turn import cli_turn_scope
    from hermes_cli.model_switch import ModelSwitchResult

    switched = ModelSwitchResult(success=True, new_model="gpt-x", target_provider="openrouter",
                                 api_key="k2", base_url="http://or", api_mode="chat_completions")
    _RecordingAgent.built = []
    runner = _gateway_runner()
    committed = {"api_text": "[voice] clean text", "model_override": {"model": "gpt-x", "provider": "openrouter"}}
    with patch("hermes_cli.model_switch.switch_model", return_value=switched) as switch, \
            cli_turn_scope("adm-1", committed):
        result, agent = _run_turn(runner)
    assert result["final_response"] == "ok"
    [(message, kwargs)] = agent.calls
    assert message == "[voice] clean text" and kwargs["persist_user_message"] == "clean text"
    assert switch.call_args.kwargs["raw_input"] == "gpt-x"
    assert switch.call_args.kwargs["explicit_provider"] == "openrouter"
    assert switch.call_args.kwargs["current_model"] == "session-model"
    built = _RecordingAgent.built[-1]
    assert (built["model"], built["provider"], built["api_key"], built["base_url"]) == (
        "gpt-x", "openrouter", "k2", "http://or")

    # The next turn (and any turn whose event is not that admission) runs the session's own route.
    with patch("hermes_cli.model_switch.switch_model") as switch, cli_turn_scope("adm-1", committed):
        _result, agent = _run_turn(runner, admission_id="adm-2")
    switch.assert_not_called()
    [(message, kwargs)] = agent.calls
    assert message == "clean text" and "persist_user_message" not in kwargs
    assert _RecordingAgent.built[-1]["model"] == "session-model"


def test_an_unresolvable_one_turn_model_fails_the_turn_without_running_it():
    from gateway.session_cli_turn import cli_turn_scope
    from hermes_cli.model_switch import ModelSwitchResult

    _RecordingAgent.built = []
    failed = ModelSwitchResult(success=False, error_message="Unknown model 'nope'")
    with patch("hermes_cli.model_switch.switch_model", return_value=failed), \
            cli_turn_scope("adm-1", {"model_override": {"model": "nope"}}):
        result, agent = _run_turn(_gateway_runner())
    assert result["failed"] is True and "Unknown model 'nope'" in result["final_response"]
    assert agent is None and _RecordingAgent.built == []
