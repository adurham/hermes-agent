"""Tests for the fork's exo provider profile.

Verifies the exo provider profile registers correctly and exposes
the expected attributes for the distributed inference cluster.

Note: plugins.model_providers.exo may not be importable directly
from the test runner (plugins dir not in sys.path), so we use
importlib to load it.
"""

import importlib.util
import os
import sys

import pytest


@pytest.fixture(scope="module")
def exo_provider():
    """Load the exo provider module and return its profile."""
    # Find the module path relative to the repo root
    test_dir = os.path.dirname(os.path.abspath(__file__))
    # tests/plugins/model_providers/ -> go up 3 levels to repo root
    repo_root = os.path.normpath(os.path.join(test_dir, "..", "..", ".."))
    module_path = os.path.join(
        repo_root, "plugins", "model-providers", "exo", "__init__.py"
    )

    spec = importlib.util.spec_from_file_location(
        "plugins.model_providers.exo", module_path,
        submodule_search_locations=[]
    )
    mod = importlib.util.module_from_spec(spec)
    # Add the repo root to sys.path so 'providers' can be imported
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    spec.loader.exec_module(mod)
    return mod.exo


class TestExoProviderProfile:
    """Tests for the exo provider profile plugin."""

    def test_provider_registered(self, exo_provider):
        """Provider can be looked up by name after registration."""
        from providers import get_provider_profile
        provider = get_provider_profile("exo")
        assert provider is not None
        assert provider.name == "exo"

    def test_provider_attributes(self, exo_provider):
        """Provider has correct identity and capability attributes."""
        assert exo_provider.name == "exo"
        assert exo_provider.aliases == ("exo-cluster", "exo_cluster")
        assert exo_provider.api_mode == "chat_completions"
        assert exo_provider.supports_vision is False
        assert exo_provider.default_max_tokens == 65536

    def test_provider_no_env_vars(self, exo_provider):
        """Exo has no fixed API key — env_vars is empty."""
        assert exo_provider.env_vars == ()
        assert len(exo_provider.env_vars) == 0

    def test_provider_base_url_empty(self, exo_provider):
        """Base URL is user-configured, not hardcoded."""
        assert exo_provider.base_url == ""

    def test_provider_alias_exo_cluster(self, exo_provider):
        """'exo-cluster' alias resolves to the same provider."""
        from providers import get_provider_profile
        provider = get_provider_profile("exo-cluster")
        assert provider is not None
        assert provider.name == "exo"

    def test_provider_alias_exo_cluster_underscore(self, exo_provider):
        """'exo_cluster' alias resolves to the same provider."""
        from providers import get_provider_profile
        provider = get_provider_profile("exo_cluster")
        assert provider is not None
        assert provider.name == "exo"

    def test_provider_register_idempotent(self, exo_provider):
        """Re-registering the same provider doesn't raise."""
        from providers import register_provider
        register_provider(exo_provider)  # should not raise