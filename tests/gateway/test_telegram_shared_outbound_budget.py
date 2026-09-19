"""Shared per-chat send+edit pacing budget (#116312).

sendMessage and editMessageText count against the same per-chat Telegram allowance; a stream that edits
too often while the assistant's own backend sends were also in flight tripped FloodWait (23 events).
A per-chat 1/s slot shared by both: a SEND waits for its slot (never dropped), an INTERIM edit is
skipped (the next edit shows the same text anyway), and the FINAL edit is never gated (the completed
answer is always delivered).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter(send_message: AsyncMock) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._rich_send_disabled = True
    adapter._bot = MagicMock()
    adapter._bot.send_message = send_message
    adapter._bot.edit_message_text = AsyncMock(return_value=MagicMock())
    return adapter


def _now() -> float:
    return asyncio.get_running_loop().time()


@pytest.mark.asyncio
async def test_interim_edit_skipped_when_slot_busy_but_final_edit_never_gated():
    """An interim edit while the shared slot is held returns success with the SAME message id and makes
    no API call; a finalize edit to the same chat still fires even though the slot is still held."""
    adapter = _adapter(AsyncMock())

    result = await adapter.edit_message("c1", "900", "part one", finalize=False)
    assert result.success is True  # consumed a slot on the first real edit

    busy = _chat_slot_remaining(adapter, "c1")
    assert busy > 0

    skipped = await adapter.edit_message("c1", "900", "part two", finalize=False)
    assert skipped.success is True and skipped.message_id == "900"
    assert adapter._bot.edit_message_text.await_count == 1

    final = await adapter.edit_message("c1", "900", "part two final", finalize=True)
    assert final.success is True
    assert adapter._bot.edit_message_text.await_count == 2  # the final edit is never gated


@pytest.mark.asyncio
async def test_skipped_interim_edit_consumes_no_slot():
    """A skipped interim edit returns success but does NOT consume the slot — the next interim edit is
    still allowed to fire."""
    adapter = _adapter(AsyncMock())
    await adapter.edit_message("c1", "900", "one", finalize=False)
    await adapter.edit_message("c1", "900", "two", finalize=False)  # skipped, no slot consumed
    await adapter.edit_message("c1", "900", "three", finalize=False)  # slot still held, skipped

    assert adapter._bot.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_send_waits_for_held_slot():
    """A send to a chat whose slot is held defers until the slot opens and then fires exactly once."""
    adapter = _adapter(AsyncMock())
    await adapter.edit_message("c1", "900", "one", finalize=False)  # hold the shared slot for c1

    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    original_sleep = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        result = await adapter.send("c1", "hello")
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert result.success is True
    assert adapter._bot.send_message.await_count == 1
    assert sleeps, "send should have awaited asyncio.sleep for the pending slot"


def _chat_slot_remaining(adapter: TelegramAdapter, chat_id: str) -> float:
    return adapter._chat_outbound_slot_remaining(chat_id)


@pytest.mark.asyncio
async def test_slot_is_per_chat():
    """The shared slot is keyed per chat: one chat's burst must not gate another chat."""
    adapter = _adapter(AsyncMock())
    await adapter.edit_message("c1", "900", "one", finalize=False)  # holds c1's slot
    assert _chat_slot_remaining(adapter, "c1") > 0

    result = await adapter.send("c2", "hello other")
    assert result.success is True
    assert adapter._bot.send_message.await_count == 1  # c2 untouched by c1's slot
    assert _chat_slot_remaining(adapter, "c2") > 0  # c2 consumed its own slot


@pytest.mark.asyncio
async def test_slot_expiry_lets_interim_edit_fire():
    """Once the budget window passes, the slot opens and an interim edit is allowed again."""
    adapter = _adapter(AsyncMock())
    await adapter.edit_message("c1", "900", "one", finalize=False)
    assert adapter._bot.edit_message_text.await_count == 1

    deadline = adapter._telegram_chat_outbound_slot_until["c1"]
    adapter._telegram_chat_outbound_slot_until["c1"] = _now() - 1.0  # expire the window
    assert deadline is not None

    result = await adapter.edit_message("c1", "900", "two", finalize=False)
    assert result.success is True
    assert adapter._bot.edit_message_text.await_count == 2  # fired again now that the slot is free