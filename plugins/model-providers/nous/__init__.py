"""Nous Portal provider profile."""

from typing import Any, Optional

from agent.portal_tags import get_affinity_scope, get_conversation_context, nous_portal_tags
from agent.transports.codex import _cache_scope_from_session_id
from providers import register_provider
from providers.base import ProviderProfile


class NousProfile(ProviderProfile):
    """Nous Portal — product tags, reasoning with Nous-specific omission."""

    def resolve_aux_model(self, *, vision: bool = False) -> str:
        """Portal's tier-aware ``/api/nous/recommended-models`` pick (cached, offline-safe)."""
        try:
            from hermes_cli.models import get_nous_recommended_aux_model

            return get_nous_recommended_aux_model(vision=vision) or ""
        except Exception:
            return ""

    def build_extra_body(
        self, *, session_id: str | None = None, data_use: frozenset[str] = frozenset(), **context,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"tags": nous_portal_tags(session_id=session_id)}
        # Top-level session_id = sticky routing key, so Anthropic-style cache
        # breakpoints stay warm on one upstream instance. Resolved like the
        # ``conversation=`` tag: declared scope, then the ambient lineage ROOT
        # (covers aux call sites that pass no session_id), then the explicit argument.
        sticky_key = _cache_scope_from_session_id(get_affinity_scope() or get_conversation_context() or session_id)
        if sticky_key:
            body["session_id"] = sticky_key
        # The Portal routes centrally and 400s on caller routing fields, except data_collection and
        # zdr: sent only when an outgoing connector result requires them (``data_use``), never from config.
        if data_use:
            body["provider"] = {"data_collection": "deny", **({"zdr": True} if "zero_retention" in data_use else {})}
        return body

    @staticmethod
    def _cannot_disable_reasoning(model: str | None) -> bool:
        """True when ``reasoning: {enabled: false}`` would 400 on *model*. Cache-only catalog
        lookup; unknown/cold (warmer kicked) and no-reasoning routes both answer True (omit > 400)."""
        try:
            from hermes_cli.models_reasoning_caps import nous_model_reasoning_capabilities, warm_nous_reasoning_caps_async

            caps = nous_model_reasoning_capabilities(model)
            if caps is None:
                warm_nous_reasoning_caps_async()
                return True
        except Exception:
            return True
        return not caps.get("supports_reasoning") or bool(caps.get("mandatory"))

    @staticmethod
    def _wire_effort_for_model(effort: str, model: str | None) -> str:
        """FORK: clamp a requested effort onto the vocabulary the Portal's upstream
        actually accepts, in the same order the direct provider profiles do it:
        the catalog's advertised ``supported_efforts`` first, then the model
        family's documented vocabulary (DeepSeek V4, GLM 5.2/5.3), then the generic
        OpenAI-compat wire. The Portal relays ``reasoning`` verbatim and upstreams
        reject unknown levels — a Hermes-internal ``ultra``/``xhigh`` arriving on a
        GLM/DeepSeek route is silently dropped and the model falls back to the
        server's default thinking tier (#140239, #96838, #140686). ``none`` and the
        unset case pass through untouched (explicit disable, never upgraded)."""
        requested = (effort or "").strip().lower()
        if not requested or requested == "none":
            return requested
        from agent import reasoning_effort as re_

        supported: Optional[tuple[str, ...]] = None
        overrides: Optional[dict[str, str]] = None
        try:
            from hermes_cli.models_reasoning_caps import nous_model_reasoning_capabilities

            catalog_efforts = (nous_model_reasoning_capabilities(model) or {}).get("supported_efforts")
        except Exception:
            catalog_efforts = None
        if isinstance(catalog_efforts, list) and catalog_efforts:
            supported = tuple(dict.fromkeys(str(e).strip().lower() for e in catalog_efforts if str(e).strip()))
        if supported is None:
            m = (model or "").strip().lower()
            if "deepseek" in m:
                supported, overrides = re_.DEEPSEEK_V4_EFFORTS, re_.DEEPSEEK_V4_OVERRIDES
            elif "glm" in m:
                glm53 = ("glm-5.3", "glm-5-3", "glm-5p3")
                if any(t in m for t in glm53):
                    supported, overrides = re_.GLM53_EFFORTS, re_.GLM53_OVERRIDES
                else:
                    supported, overrides = re_.GLM52_EFFORTS, re_.GLM52_OVERRIDES
            else:
                supported = re_.OPENAI_COMPAT_WIRE_EFFORTS
        return re_.clamp_effort(requested, supported, overrides) or requested

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, supports_reasoning: bool = False,
        model: str | None = None, **context,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Pass the full reasoning_config, disable included (the Portal honors it;
        omitting it means the upstream default, thinking ON for V4-class models).
        FORK: the effort level is clamped to the route's real vocabulary via
        ``_wire_effort_for_model`` — everything else ships untouched."""
        if not supports_reasoning:
            return {}, {}
        if reasoning_config is None:
            return {"reasoning": {"enabled": True, "effort": self._wire_effort_for_model("medium", model)}}, {}
        rc = dict(reasoning_config)
        if rc.get("enabled") is False and self._cannot_disable_reasoning(model):
            return {}, {}
        if rc.get("effort"):
            rc["effort"] = self._wire_effort_for_model(str(rc["effort"]), model)
        return {"reasoning": rc}, {}


nous = NousProfile(
    name="nous", aliases=("nous-portal", "nousresearch"), env_vars=("NOUS_API_KEY",),
    display_name="Nous Research", description="Nous Research — Hermes model family",
    signup_url="https://nousresearch.com/", fallback_models=("hermes-3-405b", "hermes-3-70b"),
    base_url="https://inference-api.nousresearch.com/v1", auth_type="oauth_device_code",
)

register_provider(nous)
