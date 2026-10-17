"""Per-task phase of ``hermes_cli.config.convert_auxiliary_to_provider_first``.

Folds one legacy *task-first* ``auxiliary.<task>`` block into the accumulators the
facade later collapses into provider-first blocks. Constants late-import from the
facade so patches on ``hermes_cli.config`` keep applying.
"""

from __future__ import annotations


def accumulate_aux_task(
    task: str,
    tcfg: dict,
    provider_models: dict,
    provider_routing: dict,
    defaults_block: dict,
) -> None:
    """Record *task*'s primary pin, fallbacks and settings into the accumulators."""
    from hermes_cli.config import _AUX_ROUTING_KEYS, _AUX_SETTING_KEYS

    def _block(prov: str) -> dict:
        return provider_models.setdefault(prov, {})

    prov = str(tcfg.get("provider", "")).strip()
    model = str(tcfg.get("model", "")).strip()
    base_url = str(tcfg.get("base_url", "")).strip()
    api_key = str(tcfg.get("api_key", "")).strip()
    api_mode = str(tcfg.get("api_mode", "")).strip()

    # Primary pin → provider block (skip 'auto'/empty: resolver default).
    if prov and prov != "auto":
        # Normalise custom:exo → exo block name for readability.
        block_name = prov.split(":", 1)[1] if prov.startswith("custom:") else prov
        if model:
            _block(block_name)[task] = model
        routing = provider_routing.setdefault(block_name, {})
        # Record block-level routing once (consistent across tasks by design).
        if prov.startswith("custom:") or base_url:
            routing.setdefault("provider", prov)
        if base_url:
            routing.setdefault("base_url", base_url)
        if api_key:
            routing.setdefault("api_key", api_key)
        if api_mode:
            routing.setdefault("api_mode", api_mode)
    elif model:
        # Explicit model on an auto/empty provider: provider-first has no
        # "auto" block, so carry the pin through the shared ``defaults``
        # block (the resolver merges defaults first, then block/task
        # overrides) instead of silently dropping it.
        defaults_block.setdefault(task, {})["model"] = model

    # Provider-scoped fallbacks → each provider's block.
    fbm = tcfg.get("fallback_models")
    if isinstance(fbm, dict):
        for fb_prov, fb_model in fbm.items():
            fb_prov = str(fb_prov).strip().lower()
            fb_model = str(fb_model).strip()
            if fb_prov and fb_model:
                _block(fb_prov)[task] = fb_model
    # Legacy scalar fallback_model → anthropic (old resolver assumption).
    fb_scalar = str(tcfg.get("fallback_model", "")).strip()
    if fb_scalar:
        _block("anthropic").setdefault(task, fb_scalar)

    # Per-task settings → defaults block.
    settings = {
        k: v for k, v in tcfg.items()
        if k in _AUX_SETTING_KEYS and k not in _AUX_ROUTING_KEYS
    }
    if settings:
        defaults_block.setdefault(task, {}).update(settings)
