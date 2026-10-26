"""Ask provider profiles about an endpoint: the named provider first, then any profile serving the host.

Endpoint-keyed ProviderProfile hooks (``client_headers``, ``request_headers``, ``max_tokens_param``)
receive the request's ``base_url`` and answer None / ``{}`` for endpoints that are not theirs, so a
``custom`` route pointed at a plugin provider's host still gets that provider's wire quirks. Only
profiles that override the hook are consulted: the base implementations are no-ops, and hosted
(``plugins.isolation: host``) proxies define only the hooks their plugin overrides.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


def _overrides(profile: Any, hook: str) -> bool:
    return getattr(type(profile), hook, None) is not getattr(ProviderProfile, hook)


def route_profiles(hook: str, provider: str = "") -> list[Any]:
    """Profiles overriding *hook*: the one registered for *provider* first, then every other."""
    from providers import get_provider_profile, list_providers

    named = get_provider_profile(provider) if provider else None
    ordered = [named] if named is not None and _overrides(named, hook) else []
    ordered.extend(p for p in list_providers() if p is not named and _overrides(p, hook))
    return ordered


def route_answer(hook: str, *, provider: str = "", **kwargs: Any) -> Any:
    """First truthy answer of *hook* across ``route_profiles``; None when no profile claims the endpoint."""
    for profile in route_profiles(hook, provider):
        try:
            answer = getattr(profile, hook)(**kwargs)
        except Exception:  # plugin code: a broken hook degrades to generic behaviour, never fails the request
            logger.debug("provider %s %s hook failed", getattr(profile, "name", "?"), hook, exc_info=True)
            continue
        if answer:
            return answer
    return None
