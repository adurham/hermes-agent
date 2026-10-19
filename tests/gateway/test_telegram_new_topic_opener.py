"""Client-created Telegram DM topic: thread-less opener adopts the new topic (#136567).

Telegram for Android 13.0.x sends the opener of a topic created from "All messages" without
``message_thread_id``; only the preceding ``forum_topic_created`` service message names the topic.
"""

import sys
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType
from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()
sys.modules.pop("plugins.platforms.telegram.adapter", None)
import plugins.platforms.telegram.telegram_topic_routing as routing
from plugins.platforms.telegram.adapter import TelegramAdapter

CHAT, USER, NEW_TOPIC = 111, 42, 600


def _msg(message_id, *, thread_id=None, created=False, user_id=USER, chat_id=CHAT, text="hi"):
    chat = SimpleNamespace(id=chat_id, type="private", title=None, full_name="T")
    return SimpleNamespace(
        chat=chat, from_user=SimpleNamespace(id=user_id, full_name="T", is_bot=False),
        text=None if created else text, message_thread_id=thread_id, is_topic_message=thread_id is not None,
        message_id=message_id, reply_to_message=None, date=None,
        forum_topic_created=SimpleNamespace(name="New topic") if created else None)


@pytest.fixture
def adapter(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(routing.time, "monotonic", lambda: clock[0])
    a = TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={"allow_from": [str(USER)]}))
    a.clock = clock
    return a


async def _create(adapter, message_id, **kw):
    await adapter._handle_forum_topic_created(
        SimpleNamespace(effective_message=_msg(message_id, thread_id=NEW_TOPIC, created=True, **kw)), None)


def _thread(adapter, msg):
    return adapter._build_message_event(msg, MessageType.TEXT).source.thread_id


@pytest.mark.asyncio
async def test_thread_less_messages_after_creation_land_in_the_new_topic(adapter):
    await _create(adapter, 9379)
    # Not creation+1: private-chat ids are one sequence shared by every chat of the bot.
    assert _thread(adapter, _msg(9382)) == str(NEW_TOPIC)
    adapter.clock[0] += 0.4
    assert _thread(adapter, _msg(9383, text="second chunk of a split text")) == str(NEW_TOPIC)
    # A message the client stamped ends the follow; a later lobby message stays lobby.
    assert _thread(adapter, _msg(9384, thread_id=NEW_TOPIC)) == str(NEW_TOPIC)
    assert _thread(adapter, _msg(9385)) is None
    assert f"{CHAT}:New topic" not in adapter._dm_topics  # must not block session-title auto-rename


@pytest.mark.asyncio
async def test_creation_is_not_adopted_late_or_by_others(adapter):
    await _create(adapter, 10)
    assert _thread(adapter, _msg(11, user_id=7)) is None  # another sender
    assert _thread(adapter, _msg(12, chat_id=222)) is None  # another chat
    adapter.clock[0] += routing.TelegramTopicRoutingMixin._NEW_TOPIC_FOLLOW_SECONDS + 1
    assert _thread(adapter, _msg(13)) is None  # a lobby message well after the creation
    await _create(adapter, 20, user_id=7)  # creation by a user the adapter would reject
    assert not adapter._pending_created_topics
