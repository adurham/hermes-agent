"""Read-time resolution of an ``auxiliary.<task>`` config block.

Built-in slot defaults come from ``DEFAULT_CONFIG``; plugin-registered tasks
(``PluginContext.register_auxiliary_task``) layer their declared ``defaults``
under the user's block and may inherit another slot via ``inherit_from``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _get_auxiliary_task_config(task: str, _seen: frozenset = frozenset()) -> Dict[str, Any]:
    """Config dict for auxiliary.<task>, or {} when unavailable. Plugin-registered tasks get their
    declared defaults layered under user config (user wins); built-in defaults live in DEFAULT_CONFIG.
    A task registered with ``inherit_from`` is resolved here, at read time, over the base task's
    effective config, so it follows the base's current settings (and the active profile's config)
    until the user pins a route on the task itself. ``_seen`` guards re-registration cycles."""
    if not task:
        return {}
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except ImportError:
        return {}
    aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    if not isinstance(aux, dict):
        aux = {}
    # FORK: the provider-first schema helpers live in agent.auxiliary_client (external callers
    # import them from there). Lazy import: auxiliary_client imports this module at load time, and
    # resolving at call time keeps patches of ``agent.auxiliary_client._read_main_provider`` live.
    from agent.auxiliary_client import _apply_singular_aux_fallback_shorthand
    task_config = _own_task_config(task, aux, config)
    try:
        from hermes_cli.plugins import get_plugin_auxiliary_tasks
        for _entry in get_plugin_auxiliary_tasks():
            if _entry.get("key") == task:
                _defaults = _entry.get("defaults") or {}
                if isinstance(_defaults, dict):
                    _inherit = _entry.get("inherit_from")
                    if _inherit and task in _seen:
                        logger.warning("Auxiliary task %r has a circular inherit_from chain — "
                                       "ignoring inheritance", task)
                    if not _inherit or task in _seen:
                        return _apply_singular_aux_fallback_shorthand({**_defaults, **task_config})
                    base = _get_auxiliary_task_config(_inherit, _seen | {task})
                    return _apply_singular_aux_fallback_shorthand(
                        _layer_over_inherited({**base, **_defaults}, task_config))
                break
    except Exception:  # health: allow BLE001 -- plugin discovery must never break aux config reads
        logger.debug("plugin auxiliary task lookup failed for %r", task, exc_info=True)
    return _apply_singular_aux_fallback_shorthand(task_config)


def _own_task_config(task: str, aux: Dict[str, Any], config: Any) -> Dict[str, Any]:
    """FORK: the task's own ``auxiliary`` entry, flattened to the legacy task-first shape.

    Two ``auxiliary`` schemas are supported transparently, always returning the same flat
    ``{provider, model, base_url, …}`` dict: **provider-first** (top-level keys are provider ids +
    ``defaults``; the task's model is selected from the block matching the active main provider —
    ``agent.auxiliary_client._aux_flatten_provider_first``) and **task-first** (top-level keys are
    task names; upstream's shape). A task entry in either schema may carry a ``fallback`` sub-key,
    normalized to ``fallback_chain`` by the caller via ``_apply_singular_aux_fallback_shorthand``.
    """
    from agent.auxiliary_client import (
        _AUX_BLOCK_ROUTING_KEYS, _aux_flatten_provider_first, _aux_schema_is_provider_first,
        _aux_task_pin_is_explicit, _read_main_provider,
    )
    if _aux_schema_is_provider_first(aux):
        task_config = _aux_flatten_provider_first(task, aux, _read_main_provider(), config)
        # Fork fix (2026-07-11): a top-level ``auxiliary.<task>`` block that carries explicit
        # routing is a TASK PIN, not a provider block — honor it over the provider-first
        # flattening. Without this the pin is dead config in a provider-first schema: it is never
        # selected by ``_aux_select_provider_block`` (no main provider is named after a task), so
        # the task silently resolves to the main provider's block default. The pin's routing
        # replaces the block's routing WHOLESALE (routing keys and model dropped first) so a block
        # ``base_url`` can't leak under the pin's provider and force the downstream
        # base_url→custom coercion in ``_resolve_task_provider_model``.
        pin = aux.get(task)
        if isinstance(pin, dict) and _aux_task_pin_is_explicit(pin):
            for _routing_key in _AUX_BLOCK_ROUTING_KEYS | {"model"}:
                task_config.pop(_routing_key, None)
            task_config.update({
                _k: _v for _k, _v in pin.items()
                if _v is not None and not (isinstance(_v, str) and not _v.strip())
            })
        return task_config
    task_config = aux.get(task, {})
    return task_config if isinstance(task_config, dict) else {}


# The fields that together pick WHERE a call goes. They travel as one unit: a provider pinned on an
# inheriting task must never pick up the base's base_url/api_key (that would send one vendor's key
# to another's endpoint).
_AUX_ROUTE_KEYS = frozenset({"provider", "model", "base_url", "api_key", "api_mode", "key_env",
                             "api_key_env", "reasoning_effort"})


def _layer_over_inherited(inherited: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a task's own ``auxiliary.<task>`` block over its inherited base.

    The picker, "reset to auto" and the dashboard persist ``provider: auto`` plus ``""`` for
    model/base_url/api_key/reasoning_effort when the operator expresses no preference, so those
    placeholders mean "follow the base", not "override it with nothing". Once the operator pins a
    route (a non-auto provider, a model or a base_url) the whole route comes from the task's own
    block. Non-route keys (timeout, extra_body, ...) override per key; only ``""`` is dropped,
    never other falsy values (``reasoning_effort: false`` is an explicit choice)."""
    provider = str(user.get("provider") or "").strip().lower()
    pinned = (provider not in ("", "auto") or bool(str(user.get("model") or "").strip())
              or bool(str(user.get("base_url") or "").strip()))
    merged = {k: v for k, v in inherited.items() if not (pinned and k in _AUX_ROUTE_KEYS)}
    for key, value in user.items():
        if value == "" and not (pinned and key in _AUX_ROUTE_KEYS):
            continue
        if key == "provider" and not pinned:
            continue
        merged[key] = value
    return merged
