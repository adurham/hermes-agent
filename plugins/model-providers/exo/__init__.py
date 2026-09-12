"""Exo cluster provider profile.

Exo is a distributed inference system running across multiple Mac Studio
nodes. It exposes an OpenAI-compatible chat completions endpoint with
custom extra_body fields (use_prefix_cache, service_tier) and native
top-level reasoning controls.

Reasoning / thinking control (DeepSeek-V4-Flash family, incl. Vision-Exp):
  - exo's chat_completions request schema accepts BOTH ``reasoning_effort``
    (Literal["none", "minimal", "low", "medium", "high", "xhigh"]) and
    ``enable_thinking`` (bool) as plain top-level fields — no extra_body
    wrapper, no vocabulary translation. Hermes' own reasoning_effort ladder
    (agent.reasoning_effort) already uses this exact vocabulary, so this is
    a straight pass-through, not a remap.
  - Server-side resolution (exo/shared/types/text_generation.py) derives
    whichever of the two fields the caller omits: reasoning_effort="none"
    implies enable_thinking=False and vice versa. Sending reasoning_effort
    alone is therefore sufficient; enable_thinking is not sent separately
    to avoid asserting a second, redundant field on the wire.
  - Only emitted when the user expressed a preference (reasoning_config is
    not None) — an unset config omits the field entirely, so the model's
    server-side default applies (currently ON per its model card) rather
    than Hermes silently forcing a level nobody asked for.
  - This checkpoint has no declared per-model reasoning restriction (unlike
    GLM's two-level GLM-5.2 ladder or Ollama's think=False-only-native
    quirk) — the full ladder is forwarded as-is, uncapped.
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class ExoProfile(ProviderProfile):
    """Exo cluster — native top-level reasoning_effort/enable_thinking passthrough."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(reasoning_config, dict):
            # No preference expressed — omit the field, let the server's own
            # default (thinking mode ON per the DSv4 model card) apply.
            return {}, {}

        top_level: dict[str, Any] = {}
        effort = (reasoning_config.get("effort") or "").strip().lower()
        enabled = reasoning_config.get("enabled", True)

        if effort == "none" or enabled is False:
            top_level["reasoning_effort"] = "none"
        elif effort:
            # exo's vocabulary is exactly agent.reasoning_effort's ladder
            # (none/minimal/low/medium/high/xhigh) — no clamp table needed.
            top_level["reasoning_effort"] = effort

        return {}, top_level


exo = ExoProfile(
    name="exo",
    aliases=("exo-cluster", "exo_cluster"),
    env_vars=(),  # No fixed API key — uses the cluster's own auth
    base_url="",  # User-configured in providers.exo.base_url
    api_mode="chat_completions",
    supports_vision=True,  # DeepSeek-V4-Flash-Vision-Exp (deployed 2026-09-11)
    default_max_tokens=65536,
)

register_provider(exo)
