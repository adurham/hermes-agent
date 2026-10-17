"""BlueBubbles webhook read receipts run as owned tasks, not dangling ones (RUF006)."""
import asyncio
import json

import pytest

from gateway.config import PlatformConfig


class _Request:
    def __init__(self, payload):
        self.query = {"password": "secret"}
        self.headers = {}
        self._body = json.dumps(payload).encode("utf-8")

    async def read(self):
        return self._body


@pytest.mark.asyncio
async def test_read_receipt_task_is_held_until_done(monkeypatch):
    from gateway.platforms.bluebubbles import BlueBubblesAdapter

    adapter = BlueBubblesAdapter(PlatformConfig(enabled=True, extra={
        "server_url": "http://localhost:1234", "password": "secret", "send_read_receipts": True}))
    release = asyncio.Event()
    marked = []

    async def fake_mark_read(chat_id):
        await release.wait()
        marked.append(chat_id)
        return True

    async def fake_handle_message(event):
        return None

    monkeypatch.setattr(adapter, "mark_read", fake_mark_read)
    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    response = await adapter._handle_webhook(_Request({
        "type": "new-message",
        "data": {"guid": "msg-1", "text": "hi", "chatGuid": "iMessage;-;user@example.com",
                 "handle": {"address": "user@example.com"}},
    }))

    assert response.status == 200
    pending = [t for t in adapter._read_receipt_tasks if not t.done()]
    assert len(pending) == 1
    release.set()
    await pending[0]  # resumes after the earlier-registered discard callback has run
    assert marked == ["iMessage;-;user@example.com"]
    assert not adapter._read_receipt_tasks
