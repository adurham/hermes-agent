"""FORK: the ``Conversation started:`` block rides the user turn, not the system prompt.

On a longest-common-prefix KV cache (the user's exo cluster) a date inside the system
prompt changes daily and forces a re-prefill of everything after it — the rest of the
prompt plus the whole tools array — on every cross-day session.  The block now rides the
per-turn injected context appended to the user message (the first inherent divergence
between sessions); the identity trailer (Model/Provider/Platform) stays in the prompt.
"""

from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import patch

import pytest

from agent.system_prompt import (
    _timestamp_line,
    build_system_prompt,
    conversation_date_block,
)
from agent.turn_context import compose_multimodal_context_part, compose_user_api_content
from run_agent import AIAgent
from tests.agent.test_api_content_sidecar import _FakeAgent, _build


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _real_agent(session_id: str) -> AIAgent:
    return AIAgent(
        api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
        provider="openrouter", platform="cli", quiet_mode=True, skip_context_files=True,
        skip_memory=True, session_id=session_id,
    )


def test_system_prompt_is_date_free_and_byte_stable_across_days(monkeypatch):
    """Sessions born on different days, built on different days, send identical system
    prompts — and the identity trailer is still the prompt's final identity paragraph."""
    monkeypatch.setattr(
        "agent.coding_context.build_coding_workspace_block",
        lambda cwd=None: "Workspace (snapshot at session start):\n- Root: /pinned",
    )
    from agent.surface_switch import identity_line_value

    prompts = []
    for sid, now in (("20261012_090000_a", datetime(2026, 10, 12, 9)),
                     ("20261013_090000_b", datetime(2026, 10, 13, 9))):
        with patch("hermes_time.now", return_value=now):
            prompts.append(build_system_prompt(_real_agent(sid)))
    assert prompts[0] == prompts[1]
    assert "Conversation started:" not in prompts[0]
    assert "Today's date" not in prompts[0]
    assert identity_line_value(prompts[0], "Model") == "test/model"
    assert identity_line_value(prompts[0], "Provider") == "openrouter"
    assert identity_line_value(prompts[0], "Platform") == "cli"


def test_date_block_is_the_unchanged_head_of_timestamp_line():
    """Format-preserving move: the injected bytes are exactly what the system prompt used
    to carry before the identity trailer."""
    agent = types.SimpleNamespace(
        session_id="20260101_120000_x", session_start=None, pass_session_id=False,
        model="m", provider="p", platform="cli", _bot_chat_timeless_prompt=False,
    )
    with patch("hermes_time.now", return_value=datetime(2026, 1, 2, 9)):
        block, full = conversation_date_block(agent), _timestamp_line(agent)
    assert block.startswith("Conversation started: Thursday, January 01, 2026")
    assert "Today's date (as of the last context rebuild): Friday, January 02, 2026" in block
    assert full == block + "\nModel: m\nProvider: p\nPlatform: cli"


def test_first_turn_api_content_ends_with_the_date_block_and_is_byte_stable():
    agent = _FakeAgent()
    with patch("hermes_cli.plugins.invoke_hook", return_value=[{"context": "PLUGIN-CTX"}]):
        ctx = _build(agent)
    block = conversation_date_block(agent)
    assert block.startswith("Conversation started:")
    msg = ctx.messages[ctx.current_turn_user_idx]
    assert msg["content"] == "hello"  # durable content stays clean
    # Appended LAST, after the plugin context, separated by a blank line.
    assert msg["api_content"] == "hello\n\nPLUGIN-CTX\n\n" + block
    # Every consumer composes the same bytes: the sidecar stamp, a live re-compose
    # (API-builder fallback) and a second compose within the turn.
    first = compose_user_api_content("hello", ctx.ext_prefetch_cache, ctx.plugin_user_context)
    second = compose_user_api_content("hello", ctx.ext_prefetch_cache, ctx.plugin_user_context)
    assert first == second == msg["api_content"]
    assert first.endswith("\n\n" + block)
    assert compose_multimodal_context_part(ctx.ext_prefetch_cache, ctx.plugin_user_context).endswith(block)


def test_date_block_rides_alone_when_nothing_else_is_injected():
    agent = _FakeAgent()
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent)
    msg = ctx.messages[ctx.current_turn_user_idx]
    assert msg["api_content"] == "hello\n\n" + conversation_date_block(agent)


def test_multimodal_turn_carries_the_date_block_as_its_text_part():
    agent = _FakeAgent()
    blocks = [{"type": "image_url", "image_url": {"url": "data:img"}}]
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent, user_message=blocks, summarize_user_message_for_log=lambda _m: "[image]")
    content = ctx.messages[ctx.current_turn_user_idx]["content"]
    assert content[-1] == {"type": "text", "text": conversation_date_block(agent)}


def test_not_resent_once_history_carries_the_same_bytes():
    """Later turns of the same day do not repeat the block; the turn that first carried
    it replays its own stamped bytes, so the conversation prefix never changes."""
    agent = _FakeAgent()
    block = conversation_date_block(agent)
    history = [
        {"role": "user", "content": "hi", "api_content": "hi\n\n" + block},
        {"role": "assistant", "content": "hello"},
    ]
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent, conversation_history=history)
    assert "api_content" not in ctx.messages[ctx.current_turn_user_idx]


def test_resent_when_the_rendered_block_changes():
    """A new day changes the block (the 'Today's date' line), so the model is told."""
    agent = _FakeAgent()
    history = [
        {"role": "user", "content": "hi", "api_content": "hi\n\nConversation started: Monday, January 05, 2026"},
        {"role": "assistant", "content": "hello"},
    ]
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent, conversation_history=history)
    msg = ctx.messages[ctx.current_turn_user_idx]
    assert msg["api_content"] == "hello\n\n" + conversation_date_block(agent)


def test_not_duplicated_on_a_legacy_stored_prompt_that_still_embeds_it():
    """A resumed pre-fork session keeps its stored prompt (with the date) byte-for-byte;
    the turn must not repeat the same date on top of it."""
    agent = _FakeAgent()
    agent._cached_system_prompt = "SYSTEM\n\n" + conversation_date_block(agent) + "\nModel: test/model"
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent)
    assert "api_content" not in ctx.messages[ctx.current_turn_user_idx]


def test_timeless_bot_chat_injects_nothing():
    agent = _FakeAgent()
    agent._bot_chat_timeless_prompt = True
    assert conversation_date_block(agent) == ""
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent)
    assert "api_content" not in ctx.messages[ctx.current_turn_user_idx]


def test_render_failure_fails_open():
    agent = _FakeAgent()
    with (
        patch("hermes_cli.plugins.invoke_hook", return_value=[{"context": "PLUGIN-CTX"}]),
        patch("agent.system_prompt.conversation_date_block", side_effect=RuntimeError("clock")),
    ):
        ctx = _build(agent)
    assert ctx.messages[ctx.current_turn_user_idx]["api_content"] == "hello\n\nPLUGIN-CTX"


def test_legacy_plugin_frame_neighbor_still_restores():
    """Persisted prompts from before the date move put the date block right after the
    plugin-sections frame; those must keep restoring their frozen sections."""
    from agent.system_prompt import _restore_plugin_prompt_sections
    from hermes_cli.plugins import RenderedPluginSystemPromptSection, format_system_prompt_sections

    framed = format_system_prompt_sections([RenderedPluginSystemPromptSection(
        id="example.rules", content="rules", position="after_memory", plugin="p")])
    for neighbor in ("Conversation started: Monday, January 05, 2026", "Active Hermes profile: default."):
        restored = _restore_plugin_prompt_sections(f"BODY\n\n{framed}\n\n{neighbor}")
        assert [(s.id, s.content) for s in restored] == [("example.rules", "rules")]
    assert _restore_plugin_prompt_sections(f"BODY\n\n{framed}\n\nUser prose") == ()
