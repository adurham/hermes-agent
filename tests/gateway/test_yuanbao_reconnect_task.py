"""schedule_reconnect's fire-and-forget task: a failure that escapes the reconnect loop is
logged with its traceback instead of surfacing only as "Task exception was never retrieved"."""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from gateway.platforms.yuanbao import ConnectionManager, YuanbaoAdapter


def _make_manager() -> ConnectionManager:
    adapter = MagicMock(spec=YuanbaoAdapter)
    adapter.name = "yuanbao"
    adapter._running = True
    return ConnectionManager(adapter)


@pytest.mark.asyncio
async def test_escaping_reconnect_failure_is_logged(caplog):
    cm = _make_manager()
    attempted = asyncio.Event()

    async def _boom():
        attempted.set()
        raise RuntimeError("reconnect blew up")

    with patch.object(cm, "_do_reconnect", _boom), caplog.at_level(logging.ERROR, logger="gateway.platforms.yuanbao"):
        cm.schedule_reconnect()
        await asyncio.wait_for(attempted.wait(), timeout=5)
        for _ in range(3):  # let the task finish and its done-callback run
            await asyncio.sleep(0)

    failures = [r for r in caplog.records if "Reconnect task failed" in r.getMessage()]
    assert len(failures) == 1
    assert isinstance(failures[0].exc_info[1], RuntimeError)
    assert cm._reconnecting is False  # the guard still resets, so the next drop can reconnect


@pytest.mark.asyncio
async def test_reconnect_not_scheduled_while_one_is_running():
    cm = _make_manager()
    release = asyncio.Event()
    calls = 0

    async def _slow():
        nonlocal calls
        calls += 1
        await release.wait()
        return True

    with patch.object(cm, "_do_reconnect", _slow):
        cm.schedule_reconnect()
        await asyncio.sleep(0)
        cm.schedule_reconnect()  # _reconnecting is set: no second task
        release.set()
        for _ in range(3):
            await asyncio.sleep(0)
    assert calls == 1
