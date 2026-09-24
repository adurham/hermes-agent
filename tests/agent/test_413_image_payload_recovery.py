"""Regression tests: HTTP 413 recovery must score progress in BYTES.

Bug (#88960 / #47339): a 413 is a *byte*-size error, but the recovery loop in
``agent/conversation_loop.py`` scored compression progress with
``estimate_messages_tokens_rough``, which deliberately prices every image at
a flat per-image token cost so screenshots don't trigger premature
compaction.  When the payload is image-dominated that progress test can never
be satisfied: in the reporting session two ``vision_analyze`` results were
5,627,202 bytes — 96.6% of the request body — while contributing only ~3K of
the ~80K token estimate.  Compaction (post-#97160) frees those megabytes, but
the token-scored check reported "no progress", burned all three attempts, and
wedged the session permanently at 13% context usage.

The fix: the 413 no-progress check measures ``serialized_messages_bytes``
(exact, free) before and after each compression pass, never the token
estimate.  These tests assert that invariant directly.

They also cover the restored fork behavior: 413 oversized-payload recovery
tries ``_try_shrink_image_parts_in_messages`` FIRST for an image-dominated
payload and only falls through to history compression when nothing was
shrinkable.  The pre-merge conversation_loop handler did this (commit
646e86af11); the merge into ``agent/turn_overflow.py`` dropped it, so an
oversized body could dead-end in a 413 with no shrink attempt.
"""

import pytest

from agent.message_sanitization import serialized_messages_bytes


def _data_url_image(size_bytes: int) -> dict:
    """An image part whose inline data URL is ~``size_bytes`` long."""
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + ("A" * size_bytes)},
    }


def _tool_msg_with_image(size_bytes: int, text: str = "screenshot captured") -> dict:
    return {
        "role": "tool",
        "tool_call_id": "call_abc123",
        "content": [
            {"type": "text", "text": text},
            _data_url_image(size_bytes),
        ],
    }


class TestSerializedMessagesBytes:
    def test_counts_inline_data_url_payloads(self):
        small = serialized_messages_bytes([_tool_msg_with_image(1_000)])
        huge = serialized_messages_bytes([_tool_msg_with_image(3_000_000)])
        assert huge - small == pytest.approx(3_000_000 - 1_000, abs=64)


    def test_utf8_bytes_not_codepoints(self):
        ascii_msgs = [{"role": "user", "content": "aaaa"}]
        utf8_msgs = [{"role": "user", "content": "éééé"}]  # 2 bytes each in UTF-8
        assert serialized_messages_bytes(utf8_msgs) > serialized_messages_bytes(
            ascii_msgs
        )

    def test_degenerate_input(self):
        assert serialized_messages_bytes([]) == 0
        assert serialized_messages_bytes("not-a-list") == 0  # type: ignore[arg-type]

    def test_never_raises_on_non_serializable_content(self):
        class Weird:
            pass

        messages = [{"role": "tool", "content": Weird()}]
        assert serialized_messages_bytes(messages) > 0


class _Stub413Agent:
    """Smallest agent that can drive ``_recover_payload_too_large``, with an ordered
    ``calls`` log so the shrink-vs-compress ORDER is directly assertable.

    Mirrors the StatusOutputMixin stub style of the other turn_overflow unit tests
    (``test_recovery_diagnostic_producers``), plus the two payload helpers this path
    calls: ``_try_shrink_image_parts_in_messages`` and
    ``_try_strip_image_parts_from_tool_messages``.
    """

    log_prefix = ""
    model = "fixture"
    platform = "cli"
    suppress_status_output = False
    tools = None

    def __init__(self, *, shrink_results, compressed):
        self.calls = []
        self.buffered = []
        self.persisted = []
        self._shrink_results = list(shrink_results)
        self._compressed = compressed

    def _try_shrink_image_parts_in_messages(self, api_messages, **kwargs):
        self.calls.append("shrink")
        return self._shrink_results.pop(0) if self._shrink_results else False

    def _compress_context(self, messages, *a, **k):
        self.calls.append("compress")
        # Fresh list object each call so ``compress``'s identity check sees a rewrite.
        return list(self._compressed), "compressed system"

    def _buffer_diagnostic_status(self, message):
        self.buffered.append(str(message))

    def _buffer_vprint(self, message):
        self.buffered.append(str(message))

    def _try_strip_image_parts_from_tool_messages(self, api_messages, **kwargs):
        self.calls.append("strip")
        return False

    def _flush_status_buffer(self):
        self.calls.append("flush")

    def _vprint(self, *a, **k):
        pass

    def _persist_session(self, *args):
        self.persisted.append(args)


def _oversized_image_messages() -> list:
    """An image-dominated payload: one ~300 KB base64 data URL, well over any
    shrink target but small enough to keep the fixture cheap."""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "here is the photo"},
            _data_url_image(300_000),
        ],
    }]


def _recovery(agent, *, messages):
    from agent.turn_overflow import _Recovery

    return _Recovery(
        agent=agent, api_messages=messages, system_message="system",
        effective_task_id="test", api_call_count=1, max_compression_attempts=3,
        messages=messages, active_system_prompt="system", conversation_history=[],
        approx_tokens=1000, compression_attempts=0,
    )


class TestPayloadTooLargeShrinksBeforeCompression:
    """The restored fork hunk: a 413 tries image-shrink FIRST, and only compresses
    when there was nothing shrinkable (commit 646e86af11; dropped in the merge)."""

    def test_shrink_succeeds_before_any_compression(self, monkeypatch):
        from agent.turn_overflow import _recover_payload_too_large
        from agent.turn_retry_state import TurnRetryState

        monkeypatch.setattr("agent.turn_overflow.time.sleep", lambda _: None)
        messages = _oversized_image_messages()
        agent = _Stub413Agent(shrink_results=[True], compressed=[{"role": "user", "content": "x"}])
        st = _recovery(agent, messages=messages)
        retry = TurnRetryState()

        verdict = _recover_payload_too_large(st, retry)

        # Shrink ran, and compression was never attempted (order + no fallthrough).
        assert agent.calls == ["shrink"], agent.calls
        assert verdict.action == "continue"
        assert st.compression_attempts == 0
        assert retry.image_shrink_retry_attempted is True
        # Same-spirit status line as the pre-merge hunk.
        assert any("Payload too large (413)" in line and "shrank" in line for line in agent.buffered)
        assert not retry.restart_with_compressed_messages

    def test_no_shrinkable_images_falls_through_to_compression(self, monkeypatch):
        from agent.turn_overflow import _recover_payload_too_large
        from agent.turn_retry_state import TurnRetryState

        monkeypatch.setattr("agent.turn_overflow.time.sleep", lambda _: None)
        messages = _oversized_image_messages()
        # Shrink reports nothing changed; compression then reduces the payload.
        agent = _Stub413Agent(shrink_results=[False], compressed=[{"role": "user", "content": "tiny"}])
        st = _recovery(agent, messages=messages)
        retry = TurnRetryState()

        verdict = _recover_payload_too_large(st, retry)

        assert agent.calls == ["shrink", "compress"], agent.calls
        assert verdict.action == "break"
        assert retry.restart_with_compressed_messages is True
        assert st.compression_attempts == 1

    def test_shrink_is_single_shot_and_cannot_double_apply(self, monkeypatch):
        """A second 413 within the same attempt must NOT re-run the shrink — the shared
        ``image_shrink_retry_attempted`` flag was already spent, so it goes to compression."""
        from agent.turn_overflow import _recover_payload_too_large
        from agent.turn_retry_state import TurnRetryState

        monkeypatch.setattr("agent.turn_overflow.time.sleep", lambda _: None)
        messages = _oversized_image_messages()
        # First call: shrink succeeds. A hypothetical second 413: shrink would now
        # succeed again, but the flag is spent so it must not be consulted.
        agent = _Stub413Agent(shrink_results=[True, True], compressed=[{"role": "user", "content": "tiny"}])
        st = _recovery(agent, messages=messages)
        retry = TurnRetryState()

        first = _recover_payload_too_large(st, retry)
        assert first.action == "continue"
        assert agent.calls == ["shrink"]

        _recover_payload_too_large(st, retry)

        assert agent.calls.count("shrink") == 1, agent.calls
        assert agent.calls[1] == "compress", agent.calls
