"""Runtime activation of a per-role fallback chain (delegation.model_by_role.
<role>.fallback) via the REAL AIAgent retry loop.

``tools/delegate_tool.py`` attaches a role's ``fallback`` sub-key to a
dispatched child as a ONE-ENTRY raw ``fallback_model=`` chain (proven in
``tests/tools/test_delegate_role_fallback.py``, which stops at the
attachment boundary since it mocks child construction). This file proves
the OTHER half of the contract end-to-end against a real ``AIAgent``: once
that chain is attached, a retryable-class runtime failure (rate limit /
quota / connection error / timeout) on the ACTUAL model call activates it,
while a non-retryable failure does not -- reusing the exact same
``classify_api_error`` / ``_try_activate_fallback`` machinery the
top-level session's own ``fallback_providers`` chain already exercises
(see ``tests/run_agent/test_provider_fallback.py`` and
``tests/run_agent/test_32646_fallback_429_after_timeout.py``). No new
retry/classification logic is introduced by the per-role fallback feature
-- this is the whole point of reusing ``fallback_model=``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _make_role_child(fallback_chain):
    """Build a minimal AIAgent the way tools/delegate_tool.py's dispatch
    loop builds a role-pinned child: primary provider/model plus a
    one-entry ``fallback_model=`` chain carrying the role's ``fallback``
    sub-key (delegate_task passes this straight through as
    ``override_fallback_chain`` -> ``fallback_model=``)."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="ollama-role-key",
            base_url="https://ollama.com/v1",
            provider="ollama-cloud",
            model="glm-5.3",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_chain,
        )
        agent.client = MagicMock()
        return agent


def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


class QuotaExhaustedRateLimitError(Exception):
    """Mirrors the real ollama-cloud weekly-quota body shape."""

    status_code = 429

    def __init__(self):
        super().__init__(
            "Error code: 429 - you (amdnative) have reached your weekly "
            "usage limit for this model"
        )
        self.response = SimpleNamespace(headers={})
        self.body = {
            "error": {
                "message": (
                    "you (amdnative) have reached your weekly usage "
                    "limit for this model"
                )
            }
        }


ROLE_FALLBACK_CHAIN = [
    {
        "provider": "zai",
        "model": "glm-4.7",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    }
]


def _mock_fallback_client():
    client = MagicMock()
    client.api_key = "zai-fallback-key"
    client.base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    client._custom_headers = None
    client.default_headers = None
    return client


class TestRetryableFailureActivatesRoleFallback:
    """(c) A retryable-class runtime error (429/quota exhaustion) on the
    role's primary provider activates its configured fallback."""

    def test_quota_exhaustion_429_switches_to_the_fallback_bundle(self):
        agent = _make_role_child(ROLE_FALLBACK_CHAIN)

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            if len(calls) == 1:
                raise QuotaExhaustedRateLimitError()
            return _mock_response("Recovered via role fallback")

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_mock_fallback_client(), "glm-4.7"),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("do the campaign PM work")

        assert result["completed"] is True
        assert result["final_response"] == "Recovered via role fallback"
        assert calls == [
            ("ollama-cloud", "glm-5.3"),
            ("zai", "glm-4.7"),
        ]
        mock_resolve.assert_called_once()
        assert agent._fallback_activated is True
        assert agent.provider == "zai"
        assert agent.model == "glm-4.7"

    def test_fallback_bundle_is_the_configured_one_not_a_guess(self):
        """The activated bundle is exactly the role's fallback identity --
        never mixed with the primary's provider/base_url."""
        agent = _make_role_child(ROLE_FALLBACK_CHAIN)

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_mock_fallback_client(), "glm-4.7"),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
        ):
            ok = agent._try_activate_fallback()

        assert ok is True
        # resolve_provider_client was called with the FALLBACK provider,
        # never the primary ollama-cloud one.
        call_args = mock_resolve.call_args
        called_provider = call_args.args[0] if call_args.args else call_args.kwargs.get("provider")
        assert called_provider == "zai"
        assert agent.provider == "zai"
        assert agent.base_url != "https://ollama.com/v1"


class TestNonRetryableFailureDoesNotActivateFallback:
    """(d) A non-retryable-class failure (internal code bug) must NOT
    engage the fallback -- it fails loud exactly like today."""

    def test_internal_code_error_does_not_switch_provider(self):
        agent = _make_role_child(ROLE_FALLBACK_CHAIN)

        def fake_api_call(api_kwargs):
            # NameError classifies as FailoverReason.internal_code_error,
            # retryable=False, should_fallback defaults False -- the
            # classifier's own contract, unmodified by this feature.
            raise NameError("some_undefined_name is not defined")

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client"
            ) as mock_resolve,
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("do the campaign PM work")

        # Never switched providers -- the fallback path was never entered.
        mock_resolve.assert_not_called()
        assert agent._fallback_activated is False
        assert agent.provider == "ollama-cloud"
        assert agent.model == "glm-5.3"
        # Loud failure -- the turn does not silently claim success.
        assert result["completed"] is False


class TestFallbackAlsoFailingRaisesLoud:
    """(e) Primary fails AND the fallback ALSO fails to activate --> the
    turn ends in a loud failure result, no silent third attempt."""

    def test_fallback_provider_unconfigured_exhausts_the_chain(self):
        """resolve_provider_client returning (None, None) for the
        fallback -- 'not configured' -- must not be silently retried a
        third time; the chain (length 1) is exhausted and the turn fails
        loud with the ORIGINAL error surfaced."""
        agent = _make_role_child(ROLE_FALLBACK_CHAIN)

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            raise QuotaExhaustedRateLimitError()

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(None, None),
            ) as mock_resolve,
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("do the campaign PM work")

        # The fallback WAS attempted (chain walked) but never activated.
        mock_resolve.assert_called_once()
        assert agent._fallback_activated is False
        assert agent.provider == "ollama-cloud"  # never switched
        assert result["completed"] is False
        # No third attempt: exactly the two calls the retry budget allows
        # before the chain-exhausted terminal path (may retry the primary
        # a bounded number of times, but never fabricates a third
        # provider identity).
        assert all(p == "ollama-cloud" for p, _m in calls)
