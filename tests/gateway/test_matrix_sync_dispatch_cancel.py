"""A sync-dispatch handler that ends cancelled is reported like any other failed handler.

``gather(return_exceptions=True)`` hands back CancelledError (a BaseException, not an
Exception) as a result; it used to slip past the failure check without a trace.
"""
import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig


def _make_adapter():
    from plugins.platforms.matrix.adapter import MatrixAdapter
    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:example.org",
        },
    )
    return MatrixAdapter(config)


@pytest.mark.asyncio
async def test_cancelled_handler_is_logged_and_siblings_still_run(caplog):
    adapter = _make_adapter()
    ran = {"ok": False}

    async def _cancelled():
        raise asyncio.CancelledError("handler cancelled")

    async def _ok():
        ran["ok"] = True

    client = MagicMock()
    client.handle_sync = MagicMock(return_value=[_cancelled(), _ok()])
    adapter._client = client

    with caplog.at_level(logging.WARNING, logger="plugins.platforms.matrix.adapter"):
        await adapter._dispatch_sync({"next_batch": "s1"})

    assert ran["ok"] is True
    assert any("event handler failed during sync dispatch" in r.getMessage() for r in caplog.records)
