"""Exo cluster provider profile.

Exo is a distributed inference system running across multiple Mac Studio
nodes. It exposes an OpenAI-compatible chat completions endpoint with
custom extra_body fields (use_prefix_cache, service_tier).

FORK: this profile is also the per-provider GATE for the reasoning controls.
``exo`` is a first-class named provider whose profile the transport reaches on
its profile path, and the base ``ProviderProfile.build_api_kwargs_extras``
returns nothing — so a resolved reasoning effort (parent ``/reasoning`` level,
``agent.reasoning_effort_by_model``, or a delegated child's
``delegation.reasoning_effort_by_role``) was silently dropped before the wire:
the request body carried only ``model``/``messages``/``max_tokens``. Verified
against the live engine by reading
``exo/src/exo/api/types/api.py`` (``ChatCompletionRequest.reasoning_effort`` +
``enable_thinking``) and
``exo/src/exo/shared/types/text_generation.py`` (``ReasoningEffort`` Literal and
``resolve_reasoning_params``): the knob is a TOP-LEVEL ``reasoning_effort``
string, and ``enable_thinking`` is an exo-only body extension. Keeping both in
THIS profile is what stops a strict OpenAI-compatible backend (or another
provider's wire) from ever seeing a field it can reject.
"""

from typing import Any

from agent.reasoning_effort import EXO_WIRE_EFFORTS, clamp_effort
from providers import register_provider
from providers.base import ProviderProfile


class ExoProfile(ProviderProfile):
    """Exo cluster: top-level ``reasoning_effort`` + the ``enable_thinking`` extension."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Map Hermes reasoning controls onto exo's chat-completions body.

        Returns ``(extra_body_additions, top_level_kwargs)``.

        * an enabled graded effort → top-level ``reasoning_effort``, clamped onto
          ``EXO_WIRE_EFFORTS`` (``none``..``xhigh``; Hermes-internal ``ultra`` and
          the OpenAI ``max`` both resolve to ``xhigh``, exo's model-max tier);
        * thinking off (``enabled: false`` or ``effort: none``) → the ``none``
          level plus ``enable_thinking: false`` in ``extra_body`` — the explicit
          switch, spelled the same way exo's own ``resolve_reasoning_params``
          derives it. ``enable_thinking`` is NOT an OpenAI SDK keyword, so it can
          only travel inside ``extra_body`` (the body is merged verbatim);
        * unset, enabled-with-no-level, or an unrecognised level → emit nothing
          and let the endpoint's own per-model default apply (never invent, never
          forward a level this wire would 422 on).

        ``supports_reasoning`` is deliberately NOT consulted: the core
        ``_supports_reasoning_extra_body`` allowlist does not know this host, so
        the transport always passes ``False`` here and gating on it would make
        this method a permanent no-op.
        """
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}
        if not isinstance(reasoning_config, dict):
            return extra_body, top_level
        effort = str(reasoning_config.get("effort") or "").strip().lower()
        if effort == "none" or reasoning_config.get("enabled", True) is False:
            top_level["reasoning_effort"] = "none"
            extra_body["enable_thinking"] = False
            return extra_body, top_level
        if not effort:
            return extra_body, top_level
        clamped = clamp_effort(effort, EXO_WIRE_EFFORTS)
        if clamped in EXO_WIRE_EFFORTS:
            top_level["reasoning_effort"] = clamped
        return extra_body, top_level


exo = ExoProfile(
    name="exo",
    aliases=("exo-cluster", "exo_cluster"),
    env_vars=(),  # No fixed API key — uses the cluster's own auth
    base_url="",  # User-configured in providers.exo.base_url
    api_mode="chat_completions",
    supports_vision=False,
    default_max_tokens=65536,
)

register_provider(exo)
