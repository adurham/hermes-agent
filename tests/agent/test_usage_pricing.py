from types import SimpleNamespace

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    get_pricing_entry,
    normalize_usage,
)


def test_normalize_usage_anthropic_keeps_cache_buckets_separate():
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=2000,
        cache_creation_input_tokens=400,
    )

    normalized = normalize_usage(usage, provider="anthropic", api_mode="anthropic_messages")

    assert normalized.input_tokens == 1000
    assert normalized.output_tokens == 500
    assert normalized.cache_read_tokens == 2000
    assert normalized.cache_write_tokens == 400
    assert normalized.prompt_tokens == 3400


def test_normalize_usage_openai_subtracts_cached_prompt_tokens():
    usage = SimpleNamespace(
        prompt_tokens=3000,
        completion_tokens=700,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1800),
    )

    normalized = normalize_usage(usage, provider="openai", api_mode="chat_completions")

    assert normalized.input_tokens == 1200
    assert normalized.cache_read_tokens == 1800
    assert normalized.output_tokens == 700


def test_normalize_usage_openai_reads_top_level_anthropic_cache_fields():
    """Some OpenAI-compatible proxies (OpenRouter, Vercel AI Gateway, Cline) expose
    Anthropic-style cache token counts at the top level of the usage object when
    routing Claude models, instead of nesting them in prompt_tokens_details.

    Regression guard for the bug fixed in cline/cline#10266 — before this fix,
    the chat-completions branch of normalize_usage() only read
    prompt_tokens_details.cache_write_tokens and completely missed the
    cache_creation_input_tokens case, so cache writes showed as 0 and reflected
    inputTokens were overstated by the cache-write amount.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=500),
        cache_creation_input_tokens=300,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    # Expected: cache read from prompt_tokens_details.cached_tokens (preferred),
    # cache write from top-level cache_creation_input_tokens (fallback).
    assert normalized.cache_read_tokens == 500
    assert normalized.cache_write_tokens == 300
    # input_tokens = prompt_total - cache_read - cache_write = 1000 - 500 - 300 = 200
    assert normalized.input_tokens == 200
    assert normalized.output_tokens == 200


def test_normalize_usage_openai_reads_top_level_cache_read_when_details_missing():
    """Some proxies expose only top-level Anthropic-style fields with no
    prompt_tokens_details object. Regression guard for cline/cline#10266.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=300,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 500
    assert normalized.cache_write_tokens == 300
    assert normalized.input_tokens == 200


def test_normalize_usage_openai_prefers_prompt_tokens_details_over_top_level():
    """When both prompt_tokens_details and top-level Anthropic fields are
    present, we prefer the OpenAI-standard nested fields. Top-level Anthropic
    fields are only a fallback when the nested ones are absent/zero.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=600, cache_write_tokens=150),
        # Intentionally different values — proving we ignore these when details exist.
        cache_read_input_tokens=999,
        cache_creation_input_tokens=999,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 600
    assert normalized.cache_write_tokens == 150


def test_openrouter_models_api_pricing_is_converted_from_per_token_to_per_million(monkeypatch):
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata",
        lambda: {
            "anthropic/claude-opus-4.6": {
                "pricing": {
                    "prompt": "0.000005",
                    "completion": "0.000025",
                    "input_cache_read": "0.0000005",
                    "input_cache_write": "0.00000625",
                }
            }
        },
    )

    entry = get_pricing_entry(
        "anthropic/claude-opus-4.6",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    )

    assert float(entry.input_cost_per_million) == 5.0
    assert float(entry.output_cost_per_million) == 25.0
    assert float(entry.cache_read_cost_per_million) == 0.5
    assert float(entry.cache_write_cost_per_million) == 6.25


def test_estimate_usage_cost_marks_subscription_routes_included():
    result = estimate_usage_cost(
        "gpt-5.3-codex",
        CanonicalUsage(input_tokens=1000, output_tokens=500),
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
    )

    assert result.status == "included"
    assert float(result.amount_usd) == 0.0


def test_estimate_usage_cost_refuses_cache_pricing_without_official_cache_rate(monkeypatch):
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_model_metadata",
        lambda: {
            "google/gemini-2.5-pro": {
                "pricing": {
                    "prompt": "0.00000125",
                    "completion": "0.00001",
                }
            }
        },
    )

    result = estimate_usage_cost(
        "google/gemini-2.5-pro",
        CanonicalUsage(input_tokens=1000, output_tokens=500, cache_read_tokens=100),
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    )

    assert result.status == "unknown"


def test_custom_endpoint_models_api_pricing_is_supported(monkeypatch):
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda base_url, api_key=None: {
            "zai-org/GLM-5-TEE": {
                "pricing": {
                    "prompt": "0.0000005",
                    "completion": "0.000002",
                }
            }
        },
    )

    entry = get_pricing_entry(
        "zai-org/GLM-5-TEE",
        provider="custom",
        base_url="https://llm.chutes.ai/v1",
        api_key="test-key",
    )

    assert float(entry.input_cost_per_million) == 0.5
    assert float(entry.output_cost_per_million) == 2.0


def test_normalize_usage_anthropic_extracts_5m_1h_cache_breakdown():
    """Anthropic /v1/messages responses (post 2026-05-03 caching beta)
    include a per-TTL breakdown under ``cache_creation``. normalize_usage
    must surface those fields so estimate_usage_cost can bill the
    different rates Anthropic charges for 5m vs 1h TTLs.
    """
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=400,
        cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=100,
            ephemeral_1h_input_tokens=300,
        ),
    )
    normalized = normalize_usage(usage, provider="anthropic", api_mode="anthropic_messages")
    assert normalized.cache_write_tokens == 400
    assert normalized.cache_write_5m_tokens == 100
    assert normalized.cache_write_1h_tokens == 300


def test_estimate_usage_cost_bills_1h_cache_write_at_higher_rate():
    """Opus 4.7 1h cache writes are $10/MTok, vs $6.25 for 5m.  Hermes
    sets ttl=1h on every request (post-2026-04-11 default) so the
    correct rate matters — $0.51 difference per 137K tokens written.
    """
    usage = CanonicalUsage(
        cache_write_tokens=137_077,
        cache_write_1h_tokens=137_077,
    )
    result = estimate_usage_cost("claude-opus-4-7", usage, provider="anthropic")
    # 137,077 * $10 / 1,000,000 = $1.37077
    assert float(result.amount_usd) == 1.37077


def test_estimate_usage_cost_falls_back_to_legacy_rate_without_breakdown():
    """Sessions written before the 5m/1h breakdown landed only have the
    aggregate cache_write_tokens count. They should bill at the legacy
    cache_write_cost_per_million ($6.25 for Opus 4.7) so historical
    session totals don't shift retroactively.
    """
    usage = CanonicalUsage(cache_write_tokens=137_077)
    result = estimate_usage_cost("claude-opus-4-7", usage, provider="anthropic")
    # 137,077 * $6.25 / 1,000,000 = $0.85673125
    assert float(result.amount_usd) == 0.85673125


def test_estimate_usage_cost_fast_mode_applies_6x_multiplier_on_opus_46():
    """Fast mode (Opus 4.6 only) charges 6x standard rates across every
    per-token category, with cache TTL multipliers stacking on top.
    1M input + 1M output @ standard = $30; @ fast mode = $180.
    """
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    standard = estimate_usage_cost("claude-opus-4-6", usage, provider="anthropic")
    fast = estimate_usage_cost("claude-opus-4-6", usage, provider="anthropic", fast_mode=True)
    assert float(standard.amount_usd) == 30.0
    assert float(fast.amount_usd) == 180.0


def test_estimate_usage_cost_fast_mode_stacks_on_cache_write_multipliers():
    """Anthropic's docs: 'Cache multipliers apply on top of fast mode
    pricing'. So 1h cache write on Opus 4.6 fast mode should be
    2x base x 6x fast = 12x base = $60/MTok.
    """
    usage = CanonicalUsage(
        cache_write_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )
    fast = estimate_usage_cost("claude-opus-4-6", usage, provider="anthropic", fast_mode=True)
    assert float(fast.amount_usd) == 60.0


def test_estimate_usage_cost_fast_mode_on_unsupported_model_warns_but_doesnt_inflate():
    """If a caller passes fast_mode=True on a model that doesn't define
    a multiplier (Opus 4.7, Sonnet, Haiku), don't silently inflate the
    cost — bill at standard rates and surface a note. Anthropic would
    400 the request anyway, but the cost calculator shouldn't make the
    failure mode worse than the upstream 400.
    """
    usage = CanonicalUsage(input_tokens=1_000_000)
    result = estimate_usage_cost("claude-opus-4-7", usage, provider="anthropic", fast_mode=True)
    # Standard rate: 1M * $5 / 1M = $5
    assert float(result.amount_usd) == 5.0
    assert any("fast_mode" in n for n in result.notes)
