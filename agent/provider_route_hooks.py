"""Ask provider profiles about an endpoint: the named provider first, then the profile serving the host.

Endpoint-keyed ProviderProfile hooks (``client_headers``, ``request_headers``, ``max_tokens_param``,
``refresh_runtime_credentials``) belong to the provider by name AND to the backend by host: a
``custom`` route pointed at a plugin provider's host still gets that provider's quirks. The host
side is ``providers.profile_for_host`` (the profile's declared ``hostnames``), core's one
endpoint->profile lookup. Only profiles that override the hook are consulted: the base
implementations are no-ops, and hosted (``plugins.isolation: host``) proxies define only the hooks
their plugin overrides.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# Per-request hooks documented pure in providers/base.py: one answer per argument set, so a hosted
# plugin pays one IPC round trip per distinct request shape instead of one per API call.
_PURE_HOOKS = frozenset({"request_headers", "max_tokens_param"})
_PURE_MEMO: dict[tuple, Any] = {}
_PURE_MEMO_MAX = 512


def _overrides(profile: Any, hook: str) -> bool:
    return getattr(type(profile), hook, None) is not getattr(ProviderProfile, hook)


def route_profiles(hook: str, provider: str = "", base_url: str = "") -> list[Any]:
    """Profiles overriding *hook*: the one registered for *provider*, then the one serving *base_url*'s host."""
    from providers import get_provider_profile, profile_for_host

    named = get_provider_profile(provider) if provider else None
    ordered = [named] if named is not None and _overrides(named, hook) else []
    hosted = profile_for_host(base_url) if base_url else None
    if hosted is not None and hosted is not named and _overrides(hosted, hook):
        ordered.append(hosted)
    return ordered


def _ask(profile: Any, hook: str, kwargs: dict[str, Any]) -> Any:
    if hook not in _PURE_HOOKS:
        return getattr(profile, hook)(**kwargs)
    from providers import registry_generation

    key = (registry_generation(), id(profile), type(profile), hook, tuple(sorted(kwargs.items())))
    if key not in _PURE_MEMO:
        if len(_PURE_MEMO) >= _PURE_MEMO_MAX:
            _PURE_MEMO.clear()
        _PURE_MEMO[key] = getattr(profile, hook)(**kwargs)
    answer = _PURE_MEMO[key]
    return dict(answer) if isinstance(answer, dict) else answer


def route_answer(hook: str, *, provider: str = "", **kwargs: Any) -> Any:
    """First truthy answer of *hook* across ``route_profiles``; None when no profile claims the endpoint."""
    for profile in route_profiles(hook, provider, str(kwargs.get("base_url") or "")):
        try:
            answer = _ask(profile, hook, kwargs)
        except Exception:  # plugin code: a broken hook degrades to generic behaviour, never fails the request
            logger.debug("provider %s %s hook failed", getattr(profile, "name", "?"), hook, exc_info=True)
            continue
        if answer:
            return answer
    return None
