"""Resolve the provider profile that claims an endpoint URL (``ProviderProfile.serves_endpoint``).

Endpoint wire quirks belong to the backend, not to the configured provider name: a ``custom`` entry
pointed at a provider's host gets that host's quirks, and a provider entry pointed at a relay does not.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def endpoint_profile(base_url: Any) -> Any:
    """The registered profile whose ``serves_endpoint`` claims *base_url*, else None (generic wire).

    Lazy and per-home: it walks ``providers.list_providers()`` on each call, so a user plugin
    discovered later in the process (or bound to another profile home) is honoured. Fails open.
    """
    url = str(base_url or "").strip()
    if not url:
        return None
    try:
        from providers import list_providers

        return next((p for p in list_providers() if p.serves_endpoint(url)), None)
    except Exception:  # plugin-owned hook (possibly hosted over IPC): a broken plugin degrades to the generic wire
        logger.debug("endpoint profile lookup failed for %s", url, exc_info=True)
        return None
