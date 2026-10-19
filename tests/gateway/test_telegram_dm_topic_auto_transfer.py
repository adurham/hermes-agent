"""Telegram ``/topic`` mode: opt-in root-DM auto transfer (``dm_topic_auto_transfer``, #139136).

Driven through the real ``TelegramAdapter.handle_message`` ingress (topic recovery, session keying,
background processing, final send) into the real ``GatewayRunner._handle_message``; only the Bot API
(``adapter._bot``) and the model call (``runner._run_agent``) are faked.

* default (key unset): a root-DM message keeps the v0.22 lobby recovery — it is answered in the
  user's most recently bound topic (#136567);
* opted in: the message opens a fresh topic via ``createForumTopic`` and the reply lands there;
* opted in but ``createForumTopic`` fails: it falls back to lobby recovery.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from hermes_state import SessionDB
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_telegram_topic_mode import (
    _make_runner,
    _make_source,
    _seed_two_topic_bindings,
)

NEW_TOPIC_ID = 777


def _fake_bot(*, create_fails: bool = False) -> MagicMock:
    bot = MagicMock()
    if create_fails:
        bot.create_forum_topic = AsyncMock(side_effect=RuntimeError("Bad Request: chat is not a forum"))
    else:
        bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=NEW_TOPIC_ID))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9001))
    bot.send_chat_action = AsyncMock(return_value=True)
    return bot


def _rig(tmp_path, monkeypatch, *, extra: dict, create_fails: bool = False):
    import gateway.run as gateway_run

    db = SessionDB(db_path=tmp_path / "state.db")
    _seed_two_topic_bindings(db)  # topic mode on; topic "222" is the most recently bound
    # The stub session store names every topic-lane session "sess-topic"; the binding FK needs its row.
    db.create_session(session_id="sess-topic", source="telegram", user_id="208214988")
    runner = _make_runner(session_db=db)
    runner.config.platforms[Platform.TELEGRAM].extra.update(extra)
    runs: list[dict] = []

    async def fake_run_agent(*args, **kwargs):
        runs.append(kwargs)
        return {"success": True, "final_response": "agent reply", "session_id": kwargs.get("session_id"),
                "messages": []}

    runner._run_agent = AsyncMock(side_effect=fake_run_agent)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***", extra=dict(extra)))
    adapter._bot = _fake_bot(create_fails=create_fails)
    runner.adapters[Platform.TELEGRAM] = adapter
    # The production wiring: every callback GatewayRunner installs on an adapter (topic recovery and,
    # where it exists, the topic-transfer hook), with the message handler pointed at the real runner.
    runner._busy_text_mode = "interrupt"
    runner._wire_adapter_handlers(
        adapter, message_handler=runner._handle_message,
        authorization_check=lambda *_a, **_k: True, platform_event_handler=AsyncMock())
    return runner, adapter, db, runs


async def _deliver_root_dm(adapter: TelegramAdapter, text: str = "plan the migration") -> MessageEvent:
    """Plain text takes the adapter's real text-batching ingress (``_enqueue_text_event``, whose batch
    key already runs topic recovery) before ``handle_message``; commands dispatch directly."""
    event = MessageEvent(text=text, source=_make_source(thread_id=None), message_id="4242")
    if event.get_command():
        await adapter.handle_message(event)
    else:
        adapter._text_batch_delay_seconds = adapter._text_batch_split_delay_seconds = 0.0
        adapter._enqueue_text_event(event)
        for _ in range(200):
            if not adapter._pending_text_batches and adapter._session_tasks:
                break
            await asyncio.sleep(0.01)
    for _ in range(200):
        tasks = [t for t in adapter._session_tasks.values() if not t.done()]
        if not tasks and adapter._bot.send_message.await_count:
            break
        await asyncio.sleep(0.01)
    await asyncio.gather(*adapter._session_tasks.values(), return_exceptions=True)
    return event


def _reply_calls(adapter: TelegramAdapter) -> list:
    """The agent's final reply sends (not the one-off "no home channel" onboarding notice)."""
    return [c for c in adapter._bot.send_message.await_args_list if "agent reply" in str(c.kwargs.get("text"))]


@pytest.mark.asyncio
async def test_default_root_dm_keeps_lobby_recovery_to_last_bound_topic(tmp_path, monkeypatch):
    """Key unset: no topic is created; the turn and its reply go to the last bound topic (#136567)."""
    _runner, adapter, db, runs = _rig(tmp_path, monkeypatch, extra={})

    event = await _deliver_root_dm(adapter)

    adapter._bot.create_forum_topic.assert_not_awaited()
    assert event.source.thread_id == "222"
    assert len(runs) == 1
    (reply,) = _reply_calls(adapter)
    assert reply.kwargs.get("message_thread_id") == 222
    db.close()


@pytest.mark.asyncio
async def test_opt_in_root_dm_opens_new_topic_and_reply_lands_there(tmp_path, monkeypatch):
    _runner, adapter, db, runs = _rig(tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True})

    event = await _deliver_root_dm(adapter)

    adapter._bot.create_forum_topic.assert_awaited_once()
    assert adapter._bot.create_forum_topic.await_args.kwargs["chat_id"] == 208214988
    assert event.source.thread_id == str(NEW_TOPIC_ID)
    assert len(runs) == 1, "the root-DM message must run as a turn, not get the lobby reminder"
    (reply,) = _reply_calls(adapter)
    assert reply.kwargs.get("message_thread_id") == NEW_TOPIC_ID
    # The root-DM message lives outside the new topic: it can't be the reply anchor there.
    assert reply.kwargs.get("reply_to_message_id") is None
    # The new topic is a bound session lane, so the next message inside it continues this session.
    binding = db.get_telegram_topic_binding(chat_id="208214988", thread_id=str(NEW_TOPIC_ID))
    assert binding is not None and binding["user_id"] == "208214988"
    db.close()


@pytest.mark.asyncio
async def test_opt_in_topic_create_failure_falls_back_to_lobby_recovery(tmp_path, monkeypatch):
    _runner, adapter, db, runs = _rig(
        tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True}, create_fails=True)

    event = await _deliver_root_dm(adapter)

    adapter._bot.create_forum_topic.assert_awaited_once()
    assert event.source.thread_id == "222"
    assert len(runs) == 1
    (reply,) = _reply_calls(adapter)
    assert reply.kwargs.get("message_thread_id") == 222
    db.close()


@pytest.mark.asyncio
async def test_opt_in_root_dm_command_stays_in_the_lobby(tmp_path, monkeypatch):
    """``/topic`` (and every other command) typed in the root DM is a lobby command: no topic."""
    _runner, adapter, db, runs = _rig(tmp_path, monkeypatch, extra={"dm_topic_auto_transfer": True})

    await _deliver_root_dm(adapter, text="/topic help")

    adapter._bot.create_forum_topic.assert_not_awaited()
    assert runs == []
    db.close()


def test_top_level_telegram_yaml_key_reaches_extra(monkeypatch):
    """``telegram: dm_topic_auto_transfer: true`` (top-level YAML) lands in PlatformConfig.extra,
    like its neighbour ``disable_topic_auto_rename``."""
    from plugins.platforms.telegram.adapter import _apply_yaml_config

    extras = _apply_yaml_config({}, {"dm_topic_auto_transfer": True}) or {}
    assert extras.get("dm_topic_auto_transfer") is True
