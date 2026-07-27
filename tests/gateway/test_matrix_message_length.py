"""Tests for Matrix outbound message length configuration (#53026)."""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


def _make_adapter(**extra):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:example.org",
            **extra,
        },
    )
    return MatrixAdapter(config)


class TestMatrixMaxMessageLength:
    def test_default_limit_is_16000(self):
        adapter = _make_adapter()
        assert adapter.max_message_length == 16000
        assert adapter._split_threshold == 15900

    def test_extra_override(self):
        adapter = _make_adapter(max_message_length=12000)
        assert adapter.max_message_length == 12000
        assert adapter._split_threshold == 11900

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MATRIX_MAX_MESSAGE_LENGTH", "20000")
        adapter = _make_adapter()
        assert adapter.max_message_length == 20000

    def test_extra_beats_env(self, monkeypatch):
        monkeypatch.setenv("MATRIX_MAX_MESSAGE_LENGTH", "20000")
        adapter = _make_adapter(max_message_length=10000)
        assert adapter.max_message_length == 10000

    def test_invalid_values_fall_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MATRIX_MAX_MESSAGE_LENGTH", "not-a-number")
        adapter = _make_adapter()
        assert adapter.max_message_length == 16000

    def test_values_are_clamped(self):
        adapter = _make_adapter(max_message_length=100)
        assert adapter.max_message_length == 500
        adapter = _make_adapter(max_message_length=999999)
        assert adapter.max_message_length == 65535

    def test_apply_yaml_config_sets_env(self, monkeypatch):
        from plugins.platforms.matrix.adapter import _apply_yaml_config

        monkeypatch.delenv("MATRIX_MAX_MESSAGE_LENGTH", raising=False)
        _apply_yaml_config({}, {"max_message_length": 12000})
        assert os.getenv("MATRIX_MAX_MESSAGE_LENGTH") == "12000"
        # _apply_yaml_config sets this env var directly via `os.environ[...] =
        # ...` (by design -- see its docstring: "everything flows through
        # env"). Clean it up with a RAW `os.environ.pop`, not another
        # `monkeypatch.delenv` call -- monkeypatch.delenv snapshots whatever
        # value is CURRENTLY present (here: "12000", the value the line
        # above just set) and restores THAT on teardown, not the value from
        # before this test started. A second monkeypatch.delenv() here would
        # therefore push an undo entry that re-sets "12000" on teardown --
        # the opposite of "clean up" -- which is exactly what leaked
        # "12000" into whichever sibling test ran next under a given
        # pytest-randomly ordering and didn't set its own value first (e.g.
        # test_default_limit_is_16000 asserting the DEFAULT 16000 saw this
        # test's leftover 12000 instead). A raw, untracked pop leaves
        # monkeypatch's undo stack holding only the TRUE original state (or
        # nothing, if it wasn't set before this test), so teardown restores
        # correctly.
        os.environ.pop("MATRIX_MAX_MESSAGE_LENGTH", None)

    def test_register_uses_default_limit(self):
        from plugins.platforms.matrix.adapter import DEFAULT_MAX_MESSAGE_LENGTH, register

        ctx = MagicMock()
        register(ctx)
        kwargs = ctx.register_platform.call_args[1]
        assert kwargs["max_message_length"] == DEFAULT_MAX_MESSAGE_LENGTH

    def test_send_uses_configured_limit(self):
        adapter = _make_adapter(max_message_length=5000)
        adapter._client = MagicMock()
        adapter._client.send_message_event = AsyncMock(return_value="evt")
        long_text = "x" * 12000

        async def _run():
            with patch.object(adapter, "truncate_message", wraps=adapter.truncate_message) as trunc:
                await adapter.send("!room:example.org", long_text)
                trunc.assert_called_once()
                assert trunc.call_args[0][1] == 5000

        asyncio.run(_run())
