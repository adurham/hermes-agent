"""Regression: the API server's startup ``API_SERVER_KEY`` read must stay scoped.

The v2026.9.14 merge reverted ``APIServerAdapter.__init__``'s key read from
``_get_scoped_secret("API_SERVER_KEY", "")`` back to a bare
``os.getenv("API_SERVER_KEY", "")``, reintroducing the cross-profile
credential borrow that ``gateway/platforms/_shared.py::get_scoped_secret``
(landed by de114b3af1) exists to prevent.

Under ``gateway.multiplex_profiles`` one process constructs an adapter per
served profile. ``os.environ`` holds the DEFAULT profile's credentials, so a
bare ``os.getenv`` inside a secondary profile's secret scope hands that
secondary adapter the default profile's key — profile A's bearer token then
authenticates against profile B's listener.

These tests assert the leak is CLOSED at the ``_api_key`` read itself and,
end to end, through ``_check_auth``. They are written to fail against the
bare-``os.getenv`` form: every assertion distinguishes the scoped value from
the ``os.environ`` value rather than merely checking a key is present.

Complements tests/gateway/test_api_server_multiplex_secret_scope.py, which
covers the ``_expected_api_key`` request-time path (#72041); the hole here was
the *construction-time* read that feeds ``self._api_key``.
"""

from __future__ import annotations

import pytest

from agent import secret_scope as ss
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile

DEFAULT_PROFILE_KEY = "default-profile-key-aaaaaaaaaaaaaaaa"
WORKER_PROFILE_KEY = "worker-profile-key-bbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def _adapter_under_scope(scope: dict | None) -> APIServerAdapter:
    """Construct an adapter the way the multiplexer does: inside the served
    profile's secret scope (or unscoped, for the default profile)."""
    if scope is None:
        return APIServerAdapter(PlatformConfig(enabled=True))
    tok = ss.set_secret_scope(scope)
    try:
        return APIServerAdapter(PlatformConfig(enabled=True))
    finally:
        ss.reset_secret_scope(tok)


def _bearer(token: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        headers={"Authorization": f"Bearer {token}"},
        remote="127.0.0.1",
        transport=None,
        method="GET",
        path_qs="/v1/models",
    )


class TestStartupKeyReadIsProfileScoped:
    def test_secondary_profile_does_not_borrow_default_key_from_environ(
        self, monkeypatch
    ):
        """The core leak: environ holds the DEFAULT profile's key while the
        adapter is constructed inside the WORKER profile's scope."""
        monkeypatch.setenv("API_SERVER_KEY", DEFAULT_PROFILE_KEY)
        ss.set_multiplex_active(True)

        adapter = _adapter_under_scope({"API_SERVER_KEY": WORKER_PROFILE_KEY})

        assert adapter._api_key == WORKER_PROFILE_KEY
        assert adapter._api_key != DEFAULT_PROFILE_KEY

    def test_two_profiles_get_two_distinct_keys(self, monkeypatch):
        """Two scopes in one process must not converge on the environ value."""
        monkeypatch.setenv("API_SERVER_KEY", DEFAULT_PROFILE_KEY)
        ss.set_multiplex_active(True)

        worker = _adapter_under_scope({"API_SERVER_KEY": WORKER_PROFILE_KEY})
        other_key = "third-profile-key-cccccccccccccccc"
        other = _adapter_under_scope({"API_SERVER_KEY": other_key})

        assert worker._api_key == WORKER_PROFILE_KEY
        assert other._api_key == other_key
        assert worker._api_key != other._api_key

    def test_scoped_miss_returns_empty_not_environ_value(self, monkeypatch):
        """A scope without the key must fail closed, never borrow environ."""
        monkeypatch.setenv("API_SERVER_KEY", DEFAULT_PROFILE_KEY)
        ss.set_multiplex_active(True)

        adapter = _adapter_under_scope({"SOME_OTHER_KEY": "x"})

        assert adapter._api_key == ""

    def test_default_profile_unscoped_under_multiplex_still_reads_environ(
        self, monkeypatch
    ):
        """The DEFAULT profile constructs unscoped under multiplexing; a bare
        get_secret would raise UnscopedSecretError. environ is its OWN value."""
        monkeypatch.setenv("API_SERVER_KEY", DEFAULT_PROFILE_KEY)
        ss.set_multiplex_active(True)

        adapter = _adapter_under_scope(None)

        assert adapter._api_key == DEFAULT_PROFILE_KEY

    def test_single_profile_legacy_environ_read_unchanged(self, monkeypatch):
        """Multiplex off, no scope: the legacy read keeps working."""
        monkeypatch.setenv("API_SERVER_KEY", DEFAULT_PROFILE_KEY)

        adapter = _adapter_under_scope(None)

        assert adapter._api_key == DEFAULT_PROFILE_KEY

    def test_explicit_extra_key_still_wins(self, monkeypatch):
        """An explicit platforms.api_server.key overrides both paths."""
        monkeypatch.setenv("API_SERVER_KEY", DEFAULT_PROFILE_KEY)
        ss.set_multiplex_active(True)
        explicit = "explicit-config-key-dddddddddddddddd"

        tok = ss.set_secret_scope({"API_SERVER_KEY": WORKER_PROFILE_KEY})
        try:
            adapter = APIServerAdapter(
                PlatformConfig(enabled=True, extra={"key": explicit})
            )
        finally:
            ss.reset_secret_scope(tok)

        assert adapter._api_key == explicit


class TestAuthRejectsCrossProfileToken:
    def test_default_profiles_token_is_rejected_by_worker_adapter(
        self, monkeypatch
    ):
        """End-to-end proof the leak is closed: a worker-profile adapter must
        reject the DEFAULT profile's bearer token and accept only its own."""
        monkeypatch.setenv("API_SERVER_KEY", DEFAULT_PROFILE_KEY)
        ss.set_multiplex_active(True)

        adapter = _adapter_under_scope({"API_SERVER_KEY": WORKER_PROFILE_KEY})

        # Requests arrive on this adapter's own listener (no /p/<profile>/
        # prefix), so _expected_api_key() returns self._api_key — exactly the
        # value the reverted read poisoned.
        tok = _api_request_profile.set(None)
        try:
            assert adapter._check_auth(_bearer(WORKER_PROFILE_KEY)) is None

            leaked = adapter._check_auth(_bearer(DEFAULT_PROFILE_KEY))
            assert leaked is not None, (
                "cross-profile credential borrow: the worker adapter accepted "
                "the default profile's API_SERVER_KEY"
            )
            assert leaked.status == 401
        finally:
            _api_request_profile.reset(tok)
