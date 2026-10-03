"""Regression: the compaction TRIGGER must not charge stale assistant reasoning on
aggregator re-export routes whose provider accounting ignores those bytes.

The send-side echo policy (``needs_reasoning_echo`` / ``apply_reasoning_content_policy``)
attaches stored ``reasoning_content`` to every assistant turn on the deepseek family —
matched by the bare MODEL SUBSTRING, so ``ollama-cloud`` serving ``deepseek-v4.1-flash``
qualifies. But the ollama.com backend does not count those replayed bytes in
``prompt_tokens``: on the live route (session 20261003_092105_ada7ef) the replayed stale
thinking alone estimated ~950K tokens while the provider reported ~528,988 real prompt
tokens for the same request. Charging it made the TRIGGER (1,385,064) run ~2.6x the
provider's real prompt and fired compaction far too early on reasoning-heavy sessions.

``stale_thinking_reaches_wire`` — the ESTIMATOR-facing predicate both the trigger and the
tail walk share — now excludes the aggregator host, so the trigger estimates
newest-turn-only there (same size class as the walk, so no infinite-compaction loop)
while the SEND-side wire shape is unchanged and genuine deepseek-native / openrouter
routes keep the full charge.
"""

from types import SimpleNamespace

from agent.context_compressor import (
    ContextCompressor,
    _estimate_msg_budget_tokens,
    _last_assistant_index,
)
from agent.conversation_compression_manual import estimate_request_tokens
from agent.message_sanitization import (
    matches_reasoning_echo_family,
    needs_reasoning_echo,
    stale_thinking_reaches_wire,
)
from agent.model_metadata import estimate_messages_tokens_rough
from utils import base_url_host_matches


STALE_THINKING = "considering the next move carefully... " * 200  # ~2K tok


def _reasoning_heavy_session(n_turns: int = 40) -> list:
    """Transcript whose bulk is stale reasoning replay (the #84371 shape)."""
    msgs: list = [{"role": "system", "content": "You are Hermes."}]
    msgs.append({"role": "user", "content": "do the big task"})
    for i in range(n_turns):
        msgs.append(
            {
                "role": "assistant",
                "content": f"step {i}",
                "reasoning_content": STALE_THINKING,
                "tool_calls": [
                    {"id": f"c{i}", "type": "function",
                     "function": {"name": "t", "arguments": "{}"}}
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"r{i}"})
    return msgs


class TestAggregatorRouteExclusion:
    """The estimator predicate excludes the aggregator host; the send-side does not."""

    OLLAMA = ("", "ollama-cloud", "deepseek-v4.1-flash", "https://ollama.com/v1")
    NATIVE = ("", "deepseek", "deepseek-reasoner", "https://api.deepseek.com")
    OPENROUTER = ("", "openrouter", "deepseek/deepseek-v3", "https://openrouter.ai")

    def test_ollama_route_excluded_from_estimate_charge(self):
        assert stale_thinking_reaches_wire(*self.OLLAMA) is False

    def test_ollama_route_still_matches_the_send_side_echo(self):
        # The SEND-side policy must keep echoing — the predicate split is estimate-only.
        assert needs_reasoning_echo(*self.OLLAMA[1:]) is True
        assert matches_reasoning_echo_family(
            "deepseek", "ollama-cloud", "deepseek-v4.1-flash", "https://ollama.com/v1"
        ) is True

    def test_native_deepseek_and_openrouter_still_charge_stale_thinking(self):
        assert stale_thinking_reaches_wire(*self.NATIVE) is True
        assert stale_thinking_reaches_wire(*self.OPENROUTER) is True

    def test_codex_responses_still_never_charges(self):
        assert stale_thinking_reaches_wire(
            "codex_responses", "deepseek", "deepseek-v4-flash", ""
        ) is False

    def test_host_exclusion_is_host_scoped_not_substring(self):
        # ``domain in base_url`` would let these through; base_url_host_matches must not.
        assert base_url_host_matches("https://ollama.com/v1", "ollama.com") is True
        assert base_url_host_matches("https://evil.com/ollama.com", "ollama.com") is False
        assert base_url_host_matches("https://ollama.com.evil/v1", "ollama.com") is False
        assert stale_thinking_reaches_wire(
            "", "ollama-cloud", "deepseek-v4.1-flash", "https://evil.com/ollama.com"
        ) is True


class TestTriggerWalkLockstep:
    """On the aggregator route the trigger and walk must land in the same size class."""

    def test_aggregator_route_trigger_matches_walk(self):
        msgs = _reasoning_heavy_session()
        charge = stale_thinking_reaches_wire(
            "", "ollama-cloud", "deepseek-v4.1-flash", "https://ollama.com/v1"
        )
        assert charge is False
        newest = _last_assistant_index(msgs)
        trigger = estimate_messages_tokens_rough(msgs, charge_stale_thinking=charge)
        walk = sum(
            _estimate_msg_budget_tokens(m, charge or i == newest)
            for i, m in enumerate(msgs)
        )
        # Same size class (the pre-fix full charge was >3x the walk).
        assert trigger <= walk * 2 and walk <= trigger * 2

    def test_aggregator_route_no_longer_full_charges(self):
        msgs = _reasoning_heavy_session()
        full = estimate_messages_tokens_rough(msgs, charge_stale_thinking=True)
        route = estimate_messages_tokens_rough(
            msgs,
            charge_stale_thinking=stale_thinking_reaches_wire(
                "", "ollama-cloud", "deepseek-v4.1-flash", "https://ollama.com/v1"
            ),
        )
        # The stale thinking dominates, so excluding it must cut the figure hard.
        assert route < full / 2

    def test_newest_turn_thinking_still_charged(self):
        msgs = _reasoning_heavy_session(n_turns=2)
        stripped = estimate_messages_tokens_rough(msgs, charge_stale_thinking=False)
        no_thinking = estimate_messages_tokens_rough(
            [{k: v for k, v in m.items() if k not in ("reasoning", "reasoning_content")}
             for m in msgs]
        )
        assert stripped > no_thinking


class TestCompressorRoutePredicate:
    def test_compressor_walk_route_matches_trigger_on_aggregator(self):
        cc = ContextCompressor(
            model="deepseek-v4.1-flash", provider="ollama-cloud", api_mode="",
            base_url="https://ollama.com/v1", quiet_mode=True, config_context_length=200_000,
        )
        assert cc._stale_thinking_on_wire() is False

    def test_compressor_walk_route_still_true_for_native_deepseek(self):
        cc = ContextCompressor(
            model="deepseek-reasoner", provider="deepseek", api_mode="",
            base_url="https://api.deepseek.com", quiet_mode=True, config_context_length=200_000,
        )
        assert cc._stale_thinking_on_wire() is True


class TestManualCompressEstimateRouteAware:
    """``conversation_compression_manual.estimate_request_tokens`` feeds the manual ``/compress``
    before/after figures and the ``approx_tokens`` handed to ``_compress_context`` (which drives
    ``request_exceeds_window`` / ``current_estimated_tokens``). It must consult the route like the
    other estimator call sites, not full-charge stale reasoning on an aggregator route that the
    backend's accounting ignores."""

    @staticmethod
    def _agent_stub(provider, model, base_url):
        # The estimator reads only these route facts + the cached prompt + tools (see
        # _agent_stale_thinking_on_wire -> message_sanitization.stale_thinking_reaches_wire).
        return SimpleNamespace(
            api_mode="", provider=provider, model=model, base_url=base_url,
            _cached_system_prompt="You are Hermes.", tools=None,
        )

    def test_estimate_request_tokens_is_route_aware(self):
        msgs = _reasoning_heavy_session()
        ollama = self._agent_stub("ollama-cloud", "deepseek-v4.1-flash", "https://ollama.com/v1")
        native = self._agent_stub("deepseek", "deepseek-reasoner", "https://api.deepseek.com")

        # Reference: same system+tools, with the charge pinned per route.
        def reference(charge):
            from agent.model_metadata import estimate_request_tokens_rough

            return estimate_request_tokens_rough(
                msgs, system_prompt="You are Hermes.", tools=None, charge_stale_thinking=charge)

        ollama_est = estimate_request_tokens(ollama, msgs)
        native_est = estimate_request_tokens(native, msgs)
        no_charge = reference(False)
        full_charge = reference(True)

        # Aggregator route: must NOT charge the replayed stale reasoning — lands in the same
        # size class as the uncharged reference and well below the route-ignorant full charge.
        assert ollama_est <= no_charge * 2 and no_charge <= ollama_est * 2
        assert ollama_est < full_charge / 2

        # Native deepseek: MUST still charge it — same size class as the fully charged reference.
        assert native_est <= full_charge * 2 and full_charge <= native_est * 2
        assert native_est > no_charge * 1.5
