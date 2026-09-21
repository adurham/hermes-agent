"""Regression test: provider-scoped omission of the single-space reasoning_content pad on exo.

The exo inference server runs a prefix-cache optimization (\"Fix B\") that is
defeated when the client injects a one-character space pad into the
``reasoning_content`` field of assistant messages that carried no reasoning.
When that message is re-fed on the next turn, the pad lands at the FIRST
position of the re-fed region, so the longest-common-prefix is 0 even though
the entire remaining output is byte-identical. One byte forfeits an entire
turn's cache reuse.

Live evidence against the real exo server (identical requests, only the prior
assistant message's ``reasoning_content`` differing):

* key ABSENT   -> HTTP 200, prompt_tokens=353
* key = \"\"     -> HTTP 200, prompt_tokens=353 (renders byte-identically to absent;
  token-id lists are equal)
* key = \" \"    -> HTTP 200, prompt_tokens=354

The (absent)-vs-(' ') difference is EXACTLY one inserted token (id 223, a
single space) at index 294; removing it from (c) yields (a) exactly. The
server's own prefix-cache accounting reported cached_tokens=351 for \"\" (a hit
against the absent-key prefix) but cached_tokens=0 for \" \" — the pad
demonstrably destroys reuse. Both omitting the key AND sending \"\" are safe and
equivalent on exo; we prefer OMITTING the key.

Scope: the change is ADDITIVE and provider-scoped to the exo backend (the
provider identity the client already resolves as ``agent.provider == \"exo\"``).
It is NOT a global config flag, NOT a blocklist, and defaults SAFE: every other
provider keeps today's behavior byte-for-byte. In particular ollama-cloud
(which, like exo, runs a DeepSeek model and is a require-side thinking mode)
MUST keep emitting the \" \" pad, and strict/unknown providers (anthropic,
custom) MUST keep stripping the key exactly as they do today.

Notes on \"byte-for-byte unchanged from today's behavior\": today's real code
ships no reasoning_content key for anthropic or an unknown/custom provider
(their ``_needs_thinking_reasoning_pad()`` is False, so the policy strips the
field). This change does NOT add a pad there — doing so would itself be a
behavior change. The non-exo behavior this change locks in is exactly what runs
today.

Refs: exo prefix-cache Fix B LCP defeat (cached_tokens 351 vs 0).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from run_agent import AIAgent


def _make_agent(provider: str = "", model: str = "", base_url: str = "") -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    agent._thinking_pad_cache = None
    agent._reasoning_echo_flag = False
    return agent


_EXO = ("exo", "deepseek-ai/DeepSeek-V4-Flash-0731", "http://192.168.86.201:52415/v1")
_ANTHROPIC = ("anthropic", "claude-sonnet-5", "https://api.anthropic.com")
_OLLAMA_CLOUD = ("ollama-cloud", "deepseek-v4-flash", "https://ollama.com")
_UNKNOWN = ("custom-provider", "my-local-model", "https://unknown.example/v1")

_ATTR_ABSENT = object()


def _sdk_tool_call(call_id: str = "c1", name: str = "terminal", arguments: str = "{}"):
    """Minimal SDK-shaped tool_call object that satisfies the builder's iteration."""
    return SimpleNamespace(
        id=call_id,
        call_id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
        extra_content=None,
    )


def _build_sdk_message(reasoning_content=_ATTR_ABSENT, tool_calls=None):
    """SDK-shaped assistant message; ``reasoning_content`` defaults to absent."""
    kwargs: dict = {"content": "", "reasoning": None}
    if reasoning_content is not _ATTR_ABSENT:
        kwargs["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        kwargs["tool_calls"] = tool_calls
    return SimpleNamespace(
        content="",
        reasoning=None,
        reasoning_content=kwargs.get("reasoning_content"),
        reasoning_details=None,
        codex_reasoning_items=None,
        codex_message_items=None,
        anthropic_content_blocks=None,
        tool_calls=tool_calls,
    )


# ---------------------------------------------------------------------------
# Acceptance 1 & 2 — exo: omit the pad when empty/absent, emit verbatim when present
# ---------------------------------------------------------------------------


class TestExoOmitsPadOnBuildPath:
    """_build_assistant_message must not pin a \" \" pad for exo on empty reasoning."""

    def test_exo_empty_reasoning_build_omits_key(self) -> None:
        agent = _make_agent(*_EXO)
        built = agent._build_assistant_message(
            _build_sdk_message(tool_calls=[_sdk_tool_call()]), "tool_calls"
        )
        assert "reasoning_content" not in built

    def test_exo_absent_reasoning_build_omits_key(self) -> None:
        agent = _make_agent(*_EXO)
        built = agent._build_assistant_message(
            _build_sdk_message(reasoning_content=None, tool_calls=[_sdk_tool_call()]),
            "tool_calls",
        )
        assert "reasoning_content" not in built

    def test_exo_non_empty_reasoning_build_verbatim(self) -> None:
        agent = _make_agent(*_EXO)
        built = agent._build_assistant_message(
            _build_sdk_message(
                reasoning_content=" actual chain of thought ", tool_calls=[_sdk_tool_call()]
            ),
            "tool_calls",
        )
        assert built["reasoning_content"] == " actual chain of thought "


class TestExoOmitsPadOnReplayPath:
    """copy_reasoning_content_for_api must drop the pad for exo on empty reasoning."""

    def test_exo_empty_reasoning_copy_omits_key(self) -> None:
        agent = _make_agent(*_EXO)
        api_msg: dict = {"role": "assistant", "content": "hi"}
        agent._copy_reasoning_content_for_api(
            {
                "role": "assistant",
                "content": "hi",
                "reasoning_content": " ",
            },
            api_msg,
        )
        assert "reasoning_content" not in api_msg

    def test_exo_space_pad_copy_omitted(self) -> None:
        agent = _make_agent(*_EXO)
        api_msg: dict = {"role": "assistant", "content": "hi"}
        agent._copy_reasoning_content_for_api(
            {"role": "assistant", "content": "hi", "reasoning_content": " "},
            api_msg,
        )
        assert "reasoning_content" not in api_msg

    def test_exo_non_empty_reasoning_copy_verbatim(self) -> None:
        agent = _make_agent(*_EXO)
        api_msg: dict = {"role": "assistant", "content": "hi"}
        agent._copy_reasoning_content_for_api(
            {"role": "assistant", "content": "hi", "reasoning_content": " real chain "},
            api_msg,
        )
        assert api_msg["reasoning_content"] == " real chain "


class TestExoOmitsPadOnReapplyPath:
    """reapply_reasoning_echo_for_provider must not re-add the pad for exo."""

    def test_exo_reapply_does_not_repad(self) -> None:
        from agent.agent_runtime_helpers import reapply_reasoning_echo_for_provider

        agent = _make_agent(*_EXO)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
        ]
        changed = reapply_reasoning_echo_for_provider(agent, msgs)
        assert changed == 0
        assert "reasoning_content" not in msgs[2]


# ---------------------------------------------------------------------------
# Acceptance 3 — non-exo require-side provider (ollama-cloud) STILL emits " "
# ---------------------------------------------------------------------------


class TestOllamaCloudKeepsPad:
    """ollama-cloud (a DeepSeek require-side thinking mode) must keep the pad."""

    def test_ollama_cloud_empty_reasoning_build_keeps_pad(self) -> None:
        agent = _make_agent(*_OLLAMA_CLOUD)
        built = agent._build_assistant_message(
            _build_sdk_message(tool_calls=[_sdk_tool_call()]), "tool_calls"
        )
        assert built["reasoning_content"] == " "

    def test_ollama_cloud_empty_reasoning_copy_keeps_pad(self) -> None:
        agent = _make_agent(*_OLLAMA_CLOUD)
        api_msg: dict = {"role": "assistant", "content": "hi"}
        agent._copy_reasoning_content_for_api(
            {"role": "assistant", "content": "hi"}, api_msg
        )
        assert api_msg["reasoning_content"] == " "

    def test_ollama_cloud_non_empty_reasoning_unchanged(self) -> None:
        agent = _make_agent(*_OLLAMA_CLOUD)
        api_msg: dict = {"role": "assistant", "content": "hi"}
        agent._copy_reasoning_content_for_api(
            {"role": "assistant", "content": "hi", "reasoning_content": " real chain "},
            api_msg,
        )
        assert api_msg["reasoning_content"] == " real chain "


# ---------------------------------------------------------------------------
# Acceptance 4 — unknown/unrecognized provider fails SAFE (unchanged from today)
# ---------------------------------------------------------------------------


class TestStrictAndUnknownProvidersUnchanged:
    """anthroopic and unknown providers keep stripping (today's behavior)."""

    @pytest.mark.parametrize(
        "provider,model,base_url",
        [_ANTHROPIC, _UNKNOWN],
    )
    def test_strict_unknown_empty_reasoning_key_absent(self, provider, model, base_url) -> None:
        agent = _make_agent(provider, model, base_url)
        api_msg: dict = {"role": "assistant", "content": "hi"}
        agent._copy_reasoning_content_for_api(
            {"role": "assistant", "content": "hi", "reasoning_content": " "}, api_msg
        )
        assert "reasoning_content" not in api_msg

    @pytest.mark.parametrize(
        "provider,model,base_url",
        [_ANTHROPIC, _UNKNOWN],
    )
    def test_strict_unknown_build_no_pad(self, provider, model, base_url) -> None:
        agent = _make_agent(provider, model, base_url)
        built = agent._build_assistant_message(
            _build_sdk_message(tool_calls=[_sdk_tool_call()]), "tool_calls"
        )
        assert "reasoning_content" not in built

    @pytest.mark.parametrize(
        "provider,model,base_url",
        [_ANTHROPIC, _UNKNOWN],
    )
    def test_strict_unknown_non_empty_reasoning_unchanged(
        self, provider, model, base_url
    ) -> None:
        agent = _make_agent(provider, model, base_url)
        api_msg: dict = {"role": "assistant", "content": "hi"}
        agent._copy_reasoning_content_for_api(
            {"role": "assistant", "content": "hi", "reasoning_content": " real chain "},
            api_msg,
        )
        assert "reasoning_content" not in api_msg
