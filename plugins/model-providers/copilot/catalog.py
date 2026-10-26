"""Copilot catalog and model-metadata hooks (lane L4): hosts, models.dev id, live context windows."""

from __future__ import annotations

# Copilot business/enterprise accounts are served from ``api.<plan>.githubcopilot.com``.
HOSTNAMES = ("githubcopilot.com",)
# GitHub Models (models.github.ai, and the ~8K-capped free tier at models.inference.ai.azure.com)
# share the Copilot catalog but take plain GitHub tokens.
METADATA_HOSTNAMES = ("models.github.ai", "models.inference.ai.azure.com")
MODELS_DEV_ID = "github-copilot"


class CopilotCatalogMixin:
    """Catalog/metadata hooks for the Copilot profiles."""

    def live_context_length(self, model: str, *, api_key: str = "", base_url: str = "") -> int | None:
        from hermes_cli.models import get_copilot_model_context

        return get_copilot_model_context(model, api_key=api_key)

    def normalize_model_id(self, model: str, *, api_key: str = "") -> str | None:
        from hermes_cli.models import normalize_copilot_model_id

        return normalize_copilot_model_id(model, api_key=api_key or None)
