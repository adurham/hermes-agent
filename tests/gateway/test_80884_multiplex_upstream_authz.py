"""Regression tests for issue #80884.

Under ``gateway.multiplex_profiles=true``, inbound events from an adapter
that declares ``authorization_is_upstream=True`` (e.g. A2A, Relay) can be
routed to a SECONDARY (non-primary) profile — via ``profile_routes`` stamping
``source.profile``, or because the source is namespaced to a secondary
profile at build time. Before this fix, ``_adapter_authorization_is_upstream``
resolved the adapter purely by ``(platform, source.profile)``. When the
secondary profile had no adapter of that platform registered — which is the
common case for adapters like A2A whose ONE process-level http.server serves
all profiles from the primary registry — the lookup returned ``None`` and the
event dropped through to the env-allowlist default-deny.

The fix consults the source's actual in-process transport adapter (retained
by ``build_source`` via ``_transport_adapter_ref``) first, so any inbound
event that reached the gateway through an upstream-authorized transport is
honored regardless of which profile the source is stamped for. This is
generic gateway multiplexer behavior, not A2A-specific — Relay, A2A, and any
future ``authorization_is_upstream=True`` platform benefit.
"""

from __future__ import annotations

import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource

# A2A is a plugin platform — created on demand via _missing_
PLATFORM_A2A = Platform("a2a")


class _FakeAdapter:
    """Minimal adapter shell — SimpleNamespace can't be weakref'd."""

    def __init__(self, *, authorization_is_upstream: bool, enforces_own_access_policy: bool = False):
        self.authorization_is_upstream = authorization_is_upstream
        self.enforces_own_access_policy = enforces_own_access_policy
        self.send = AsyncMock()


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "A2A_ALLOWED_USERS",
        "RELAY_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "A2A_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_multiplex_runner(monkeypatch):
    """Runner with one A2A adapter registered ONLY in the primary map.

    Mimics production: A2A binds a single stdlib http.server in the primary
    profile's adapter registry and multiplexes routed traffic to secondary
    profiles at request time. Secondary profiles have NO A2A entry in
    ``_profile_adapters``.
    """
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    a2a_adapter = _FakeAdapter(
        
        authorization_is_upstream=True,
        enforces_own_access_policy=False,
    )
    runner.adapters = {PLATFORM_A2A: a2a_adapter}
    # Secondary profile 'coder' is registered but has NO A2A adapter of its
    # own — the primary process-level A2A server multiplexes to it.
    runner._profile_adapters = {"coder": {}}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner._active_profile_name = lambda: "default"
    return runner, a2a_adapter


def _a2a_source(*, profile, transport_adapter=None) -> SessionSource:
    src = SessionSource(
        platform=PLATFORM_A2A,
        user_id="peer-alice",
        chat_id="ctx-1",
        user_name="peer-alice",
        chat_type="dm",
        profile=profile,
    )
    if transport_adapter is not None:
        src._transport_adapter_ref = weakref.ref(transport_adapter)
    return src


def test_a2a_secondary_profile_upstream_authz_honored(monkeypatch):
    """A2A routed to a secondary profile with no per-profile adapter passes.

    This is the exact #80884 shape: the source is stamped with the secondary
    profile ('coder'), but only the primary registry has the A2A adapter.
    The transport adapter is the authoritative source of the
    ``authorization_is_upstream`` capability.
    """
    runner, a2a_adapter = _make_multiplex_runner(monkeypatch)
    src = _a2a_source(profile="coder", transport_adapter=a2a_adapter)
    assert runner._is_user_authorized(src) is True


def test_a2a_primary_profile_still_authorized(monkeypatch):
    """Mirror of the pre-existing primary-profile behavior — no regression."""
    runner, a2a_adapter = _make_multiplex_runner(monkeypatch)
    src = _a2a_source(profile=None, transport_adapter=a2a_adapter)
    assert runner._is_user_authorized(src) is True


def test_secondary_profile_without_transport_provenance_falls_back(monkeypatch):
    """Restored/hand-built sources with no transport ref use profile lookup.

    When the source has no ``_transport_adapter_ref`` (e.g. rehydrated from
    disk), we still fall back to ``(platform, profile)`` — and if that
    profile registers the same upstream-authorized platform, it is honored.
    """
    runner, a2a_adapter = _make_multiplex_runner(monkeypatch)
    # Register the same upstream-authorized adapter under the secondary too.
    runner._profile_adapters["coder"][PLATFORM_A2A] = a2a_adapter
    src = _a2a_source(profile="coder", transport_adapter=None)
    assert runner._is_user_authorized(src) is True


def test_secondary_profile_transport_not_upstream_still_denies(monkeypatch):
    """Guards against fail-open: a non-upstream transport must NOT authorize.

    An adapter registered in a secondary profile that does NOT declare
    ``authorization_is_upstream`` and has no env allowlist must remain
    default-denied even when it is the source's transport adapter.
    """
    _clear_auth_env(monkeypatch)
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    non_upstream_adapter = _FakeAdapter(
        
        authorization_is_upstream=False,
        enforces_own_access_policy=False,
    )
    runner.adapters = {}
    runner._profile_adapters = {"coder": {Platform.DISCORD: non_upstream_adapter}}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner._active_profile_name = lambda: "default"

    src = SessionSource(
        platform=Platform.DISCORD,
        user_id="123",
        chat_id="456",
        user_name="someone",
        chat_type="dm",
        profile="coder",
    )
    src._transport_adapter_ref = weakref.ref(non_upstream_adapter)
    assert runner._is_user_authorized(src) is False
