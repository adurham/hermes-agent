"""WeCom 846609 (subscription lost): the stale-req_id purge is owned by the adapter until it runs."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig


@pytest.mark.asyncio
async def test_subscription_lost_purge_is_tracked_then_clears_req_ids():
    from plugins.platforms.wecom.adapter import STREAM_NOT_SUBSCRIBED_ERRCODE, WeComAdapter

    adapter = WeComAdapter(PlatformConfig(enabled=True))
    adapter._last_chat_req_ids["chat-1"] = "req-old"
    adapter._reply_req_ids["msg-1"] = "req-old"
    adapter._send_request = AsyncMock(
        return_value={"errcode": STREAM_NOT_SUBSCRIBED_ERRCODE, "errmsg": "not subscribed"})

    result = await adapter._send_inner("chat-1", "hi", force_proactive=True)

    assert result.success is False
    pending = [task for task in adapter._background_tasks if not task.done()]
    assert len(pending) == 1, "purge task must be held by the adapter, not left dangling"
    await asyncio.gather(*pending)
    assert not adapter._background_tasks
    assert not adapter._last_chat_req_ids and not adapter._reply_req_ids
