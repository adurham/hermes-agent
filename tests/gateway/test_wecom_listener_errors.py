"""WeCom listen loop: a dropped socket is a one-line warning, a bug in frame handling keeps its traceback."""

import logging
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "level", "has_traceback"),
    [
        (RuntimeError("WeCom websocket closed"), logging.WARNING, False),
        (ConnectionResetError("peer reset"), logging.WARNING, False),
        (KeyError("body"), logging.ERROR, True),
    ],
)
async def test_listen_loop_logs_traceback_only_for_unexpected_errors(monkeypatch, caplog, error, level, has_traceback):
    from plugins.platforms.wecom import adapter as wecom_module

    adapter = wecom_module.WeComAdapter(PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"}))
    monkeypatch.setattr(wecom_module, "RECONNECT_BACKOFF", [0])
    adapter._running = True
    reads = iter([error])

    async def _read_once_then_stop():
        if (exc := next(reads, None)) is not None:
            raise exc
        adapter._running = False  # second read after the reconnect: end the loop

    adapter._read_events = AsyncMock(side_effect=_read_once_then_stop)
    adapter._open_connection = AsyncMock()

    with caplog.at_level(logging.DEBUG, logger=wecom_module.logger.name):
        await adapter._listen_loop()

    records = [r for r in caplog.records if r.name == wecom_module.logger.name and r.levelno >= logging.WARNING]
    assert [r.levelno for r in records] == [level]
    assert (records[0].exc_info is not None) is has_traceback
    adapter._open_connection.assert_awaited_once()
