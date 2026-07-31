"""Unit coverage for the background-review aux-model selector + routed digest.

Covers the two behaviors this change adds:
  • _resolve_review_runtime — auto/same-model → not routed (main model, warm
    cache); a configured different model → routed with resolved credentials.
  • _digest_history — compact replay used ONLY on the routed path (recent tail
    verbatim + a digest of older turns), preserving role alternation.

Pure-function / config-driven; no live model calls.
"""
from typing import Any
from unittest.mock import patch

from agent import background_review as br


def _msg(role, content, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# ---------------------------------------------------------------------------
# _resolve_review_runtime — the aux-model selector
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, provider="openai-codex", model="gpt-5.5"):
        self.provider = provider
        self.model = model
        self._credential_pool: Any = None
        self.request_overrides = {}
        self.max_tokens: int | None = None

    def _current_main_runtime(self):
        return {
            "api_key": "parent-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_app_server",
        }


def test_routing_auto_inherits_parent_and_downgrades_codex_app_server():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {"provider": "auto", "model": ""}}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"
    assert rt["model"] == "gpt-5.5"
    assert rt["api_mode"] == "codex_responses"  # downgraded so agent-loop tools dispatch


def test_routing_to_different_model_marks_routed_and_resolves_credentials():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    fake_rp = {
        "provider": "openrouter", "api_key": "or-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
        "credential_pool": "routed-pool",
        "request_overrides": {"extra_body": {"store": False}},
        "max_output_tokens": 2048,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "openrouter"
    assert rt["model"] == "google/gemini-3-flash-preview"
    assert rt["api_key"] == "or-key"
    assert rt["credential_pool"] == "routed-pool"
    assert rt["request_overrides"] == {"extra_body": {"store": False}}
    assert rt["max_tokens"] == 2048


def test_unrouted_runtime_keeps_parent_pool_and_overrides():
    agent = _FakeAgent()
    agent._credential_pool = "parent-pool"
    agent.request_overrides = {"service_tier": "priority"}
    agent.max_tokens = 4096
    with patch("hermes_cli.config.load_config", return_value={}):
        rt = br._resolve_review_runtime(agent)
    assert rt["credential_pool"] == "parent-pool"
    assert rt["request_overrides"] == {"service_tier": "priority"}
    assert rt["max_tokens"] == 4096


def test_routing_same_model_as_parent_is_not_routed():
    agent = _FakeAgent(provider="openrouter", model="anthropic/claude-opus-4.8")
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "anthropic/claude-opus-4.8",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False  # same model/provider → keep full-replay path


def test_routing_resolution_failure_falls_back_to_parent():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=RuntimeError("boom")):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"


# ---------------------------------------------------------------------------
# _resolve_review_runtime — PROVIDER-FIRST schema (2026-07-31 fix)
#
# Regression for a real bug: the pre-fix implementation read
# ``auxiliary.background_review`` directly off the raw config dict, which
# only understands the legacy TASK-FIRST schema. A PROVIDER-FIRST config
# (top-level keys are provider ids + "defaults" -- e.g. auxiliary.exo:,
# auxiliary.anthropic: -- the schema curator/compression/vision already
# support via _resolve_task_provider_model) was silently ignored: the naive
# read always saw an empty background_review block and fell through to
# "parent", so a user's per-provider override had NO EFFECT no matter how
# it was configured. Fixed by delegating to the same
# _resolve_task_provider_model("background_review") every other auxiliary
# task already uses.
# ---------------------------------------------------------------------------

def test_provider_first_schema_routes_when_main_provider_matches_block():
    """auxiliary.exo.background_review (provider-first) must route the
    review fork off-cluster when the active main provider is exo -- this
    is the exact scenario that was previously silently ignored."""
    agent = _FakeAgent(provider="exo", model="mlx-community/DeepSeek-V4-Flash")
    cfg = {"auxiliary": {
        "exo": {"provider": "ollama-cloud", "default": "gemma4:31b"},
    }}
    fake_rp = {
        "provider": "ollama-cloud", "api_key": None,
        "base_url": None, "api_mode": "chat_completions",
        "credential_pool": None, "request_overrides": {},
        "max_output_tokens": None,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("agent.auxiliary_client._read_main_provider", return_value="exo"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "ollama-cloud"
    assert rt["model"] == "gemma4:31b"


def test_provider_first_schema_falls_through_to_parent_for_unmatched_provider():
    """A provider-first config with an exo block, but main provider is
    something else entirely (no matching block) -- must fall through to
    parent (auto), not crash or misroute."""
    agent = _FakeAgent(provider="anthropic", model="claude-sonnet-5")
    cfg = {"auxiliary": {
        "exo": {"provider": "ollama-cloud", "default": "gemma4:31b"},
    }}
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("agent.auxiliary_client._read_main_provider", return_value="anthropic"):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "anthropic"
    assert rt["model"] == "claude-sonnet-5"


def test_provider_first_schema_task_pin_overrides_provider_block():
    """A top-level auxiliary.background_review pin (explicit routing) must
    still win over provider-first flattening, per
    _aux_flatten_provider_first's documented "task pin" precedence --
    proves the fix didn't silently drop that override path."""
    agent = _FakeAgent(provider="exo", model="mlx-community/DeepSeek-V4-Flash")
    cfg = {"auxiliary": {
        "exo": {"provider": "ollama-cloud", "default": "gemma4:31b"},
        "background_review": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    }}
    fake_rp = {
        "provider": "anthropic", "api_key": None,
        "base_url": None, "api_mode": "chat_completions",
        "credential_pool": None, "request_overrides": {},
        "max_output_tokens": None,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("agent.auxiliary_client._read_main_provider", return_value="exo"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "anthropic"
    assert rt["model"] == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# _digest_history — routed-path compact replay
# ---------------------------------------------------------------------------

def test_digest_under_tail_returns_full():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    assert br._digest_history(msgs, tail=24) == msgs


def test_digest_collapses_old_keeps_tail_verbatim():
    msgs = []
    for i in range(60):
        msgs.append(_msg("user", f"u{i} " + "x" * 50))
        msgs.append(_msg("assistant", f"a{i} " + "y" * 50))
    out = br._digest_history(msgs, tail=10)
    # First message is the synthetic digest (user role → alternation preserved).
    assert out[0]["role"] == "user"
    assert out[0]["content"].startswith("[Earlier conversation digest")
    # Recent tail preserved verbatim.
    assert out[-1] == msgs[-1]
    assert len(out) == 11  # 1 digest + 10 tail


def test_digest_does_not_open_tail_on_a_tool_message():
    msgs = []
    for i in range(40):
        msgs.append(_msg("user", "u" + "x" * 50))
        msgs.append(_msg("assistant", "", tool_calls=[
            {"function": {"name": "terminal", "arguments": "{}"}}]))
        msgs.append({"role": "tool", "content": "result " + "w" * 50})
    out = br._digest_history(msgs, tail=2)
    # The verbatim tail (after the digest) must not begin on a bare tool message.
    assert out[1]["role"] != "tool"


def test_digest_records_tool_names_in_arc():
    old = [
        _msg("user", "do the thing"),
        _msg("assistant", "", tool_calls=[
            {"function": {"name": "skill_view", "arguments": "{}"}},
            {"function": {"name": "patch", "arguments": "{}"}}]),
    ]
    msgs = old + [_msg("user", f"tail{i}") for i in range(30)]
    out = br._digest_history(msgs, tail=10)
    digest = out[0]["content"]
    assert "USER: do the thing" in digest
    assert "tools: skill_view, patch" in digest
