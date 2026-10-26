"""Copilot catalog and model-metadata hooks (lane L4): hosts, models.dev id, live context windows."""

from __future__ import annotations

# Copilot business/enterprise accounts are served from ``api.<plan>.githubcopilot.com``; GitHub Models
# (models.github.ai) speaks the same wire (Responses quirks, reasoning payloads, x-initiator billing).
# Per-site differences (carriers, editor headers: githubcopilot.com only) live in the hooks, keyed on base_url.
HOSTNAMES = ("githubcopilot.com", "models.github.ai")
# The ~8K-capped GitHub Models free tier shares the Copilot catalog only (model metadata lookups).
METADATA_HOSTNAMES = ("models.inference.ai.azure.com",)
MODELS_DEV_ID = "github-copilot"


class CopilotCatalogMixin:
    """Catalog/metadata hooks for the Copilot profiles."""

    def live_context_length(self, model: str, *, api_key: str = "", base_url: str = "") -> int | None:
        from hermes_cli.models import get_copilot_model_context

        return get_copilot_model_context(model, api_key=api_key)

    def normalize_model_id(self, model: str, *, api_key: str = "") -> str | None:
        from hermes_cli.models import normalize_copilot_model_id

        return normalize_copilot_model_id(model, api_key=api_key or None)
