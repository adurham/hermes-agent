"""The restart that ``_on_socket_mode_task_done`` schedules is held and its failure logged.

A bare ``loop.create_task`` keeps only a weak reference, so the reconnect could be
garbage-collected mid-run, and an exception from it surfaced only as asyncio's
"Task exception was never retrieved" at collection time.
"""

import asyncio
import gc
import logging
import sys
from unittest.mock import MagicMock

import pytest


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler",
         slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod

_slack_mod.SLACK_AVAILABLE = True

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


def _exited_socket_task(adapter):
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = None
    adapter._socket_mode_task = task
    return task


@pytest.fixture()
def adapter():
    a = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake-token"))
    a._running = True
    return a


@pytest.mark.asyncio
async def test_scheduled_restart_survives_gc_and_runs(adapter):
    started = asyncio.Event()
    release = asyncio.Event()
    reasons: list[str] = []

    async def _restart(reason: str) -> None:
        reasons.append(reason)
        started.set()
        await release.wait()

    adapter._restart_socket_mode = _restart
    adapter._on_socket_mode_task_done(_exited_socket_task(adapter))
    await started.wait()
    gc.collect()

    assert len(adapter._socket_restart_tasks) == 1
    release.set()
    await asyncio.gather(*adapter._socket_restart_tasks)
    await asyncio.sleep(0)
    assert reasons == ["socket task exited"]
    assert adapter._socket_restart_tasks == set()


@pytest.mark.asyncio
async def test_failed_restart_is_logged(adapter, caplog):
    async def _restart(reason: str) -> None:
        raise RuntimeError("boom")

    adapter._restart_socket_mode = _restart
    adapter._on_socket_mode_task_done(_exited_socket_task(adapter))
    (task,) = adapter._socket_restart_tasks
    with caplog.at_level(logging.WARNING, logger=_slack_mod.logger.name):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert adapter._socket_restart_tasks == set()
    assert any("Socket Mode restart failed" in r.getMessage() for r in caplog.records)
