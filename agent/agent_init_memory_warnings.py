"""Startup warnings for a configured memory provider that will not run this session."""

from __future__ import annotations

import logging

logger = logging.getLogger("run_agent")


# Deduped: the gateway builds a fresh AIAgent per message, so it would warn every turn.
_warned_unavailable_providers: set[tuple[str, str]] = set()


def _unavailable_warning_key(name: str) -> tuple[str, str]:
    """Once per profile home and provider: one multiplexed gateway/Desktop backend serves several
    profiles, and each one running without its memory must be told."""
    from hermes_constants import get_hermes_home, hermes_home_key
    return hermes_home_key(get_hermes_home()), name


def _warn_once(name: str, message: str, say=None) -> None:
    """Log *message* and hand it to the user's sink once per home and provider."""
    key = _unavailable_warning_key(name)
    if key in _warned_unavailable_providers:
        return
    _warned_unavailable_providers.add(key)
    logger.warning(message)
    if say is not None:
        say(message)


def _warn_memory_provider_unavailable(name: str, reason: str = "", say=None) -> None:
    """Warn once per home and provider that a configured memory provider is unavailable.

    ``is_available()`` is a side-effect-free hot-path check and can't log itself; without this
    the provider is silently dropped. ``reason`` (the provider's ``unavailable_reason()`` hint)
    can only reach the user here, so it is appended when present. *say* is the agent's
    user-facing sink: a log line alone leaves the user running without memory unaware.
    """
    _warn_once(name, (
        f"⚠ Memory provider {name!r} is selected but reports unavailable — external memory "
        "is disabled for this session (built-in memory still works). Check the "
        "provider's credentials/config with 'hermes memory status'. Note: "
        "systemd/gateway services do not inherit ~/.hermes/.env automatically; set "
        f"any required variables in the service environment.{f' {reason}' if reason else ''}"
    ), say=say)
