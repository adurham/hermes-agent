"""Tests for agent/anthropic_adapter.py — Anthropic Messages API adapter."""

import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from agent.prompt_caching import apply_anthropic_cache_control
from agent.anthropic_adapter import (
    _is_azure_anthropic_endpoint,
    _is_oauth_token,
    _refresh_oauth_token,
    _to_plain_data,
    _write_claude_code_credentials,
    build_anthropic_client,
    build_anthropic_bedrock_client,
    build_anthropic_kwargs,
    convert_messages_to_anthropic,
    convert_tools_to_anthropic,
    is_claude_code_token_valid,
    normalize_model_name,
    read_claude_code_credentials,
    resolve_anthropic_token,
    run_oauth_setup_token,
)
from agent.transports import get_transport


@pytest.fixture(autouse=True)
def _isolate_credential_sources(monkeypatch):
    """Block real macOS Keychain access from leaking into credential tests.

    On a developer machine with Claude Code logged in,
    ``_read_claude_code_credentials_from_keychain()`` returns the real
    sk-ant-oat01 token regardless of how ``Path.home()`` or env vars are
    monkeypatched — and any test that broadly patches ``subprocess.run``
    additionally trips a TypeError when the keychain helper tries to
    json.loads a MagicMock. Short-circuit the helper to None for every
    test in this file; tests that specifically need to exercise keychain
    behavior can re-patch it explicitly.
    """
    monkeypatch.setattr(
        "agent.anthropic_adapter._read_claude_code_credentials_from_keychain",
        lambda: None,
    )


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


class TestIsOAuthToken:
    def test_setup_token(self):
        assert _is_oauth_token("sk-ant-oat01-abcdef1234567890") is True

    def test_api_key(self):
        assert _is_oauth_token("sk-ant-api03-abcdef1234567890") is False





class TestBuildAnthropicClient:
    def test_setup_token_uses_auth_token(self):
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client("sk-ant-oat01-" + "x" * 60)
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert "auth_token" in kwargs
            betas = kwargs["default_headers"]["anthropic-beta"]
            assert "oauth-2025-04-20" in betas
            assert "claude-code-20250219" in betas
            assert "interleaved-thinking-2025-05-14" in betas
            assert "fine-grained-tool-streaming-2025-05-14" in betas
            # Default: 1M-context beta stays IN for OAuth so 1M-capable
            # subscriptions keep full context. The reactive recovery path
            # in run_agent.py flips it off only after a subscription
            # actually rejects the beta.
            assert "context-1m-2025-08-07" in betas
            assert "api_key" not in kwargs


    def test_api_key_uses_api_key(self):
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client("sk-ant-api03-something")
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert kwargs["api_key"] == "sk-ant-api03-something"
            assert "auth_token" not in kwargs
            # API key auth should still get common betas
            betas = kwargs["default_headers"]["anthropic-beta"]
            assert "interleaved-thinking-2025-05-14" in betas
            assert "context-1m-2025-08-07" in betas
            assert "oauth-2025-04-20" not in betas  # OAuth-only beta NOT present
            assert "claude-code-20250219" not in betas  # OAuth-only beta NOT present

    def test_custom_base_url(self):
        # Custom (non-Anthropic, non-Azure) base_urls do NOT get the
        # context-1m beta — conservative default avoids the "long context
        # beta is not yet available" rejection from third-party providers
        # that mimic Anthropic's surface. Set base_url to an Anthropic /
        # Azure host (or unset it) to opt back in.
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client("sk-ant-api03-x", base_url="https://custom.api.com")
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert kwargs["base_url"] == "https://custom.api.com"
            assert kwargs["default_headers"] == {
                "anthropic-beta": "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14,extended-cache-ttl-2025-04-11,redact-thinking-2026-02-12,context-management-2025-06-27,prompt-caching-scope-2026-01-05,effort-2025-11-24"
            }





    def test_opencode_endpoint_gets_attribution_headers(self):
        """OpenCode identifies clients by request headers, like OpenRouter.

        The OpenAI-wire paths get HTTP-Referer / X-Title / User-Agent from
        profile.default_headers. The Anthropic Messages route builds its
        client here and must merge the same set.
        """
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client(
                "sk-opencode-secret",
                base_url="https://opencode.ai/zen/go/v1",
            )
            kwargs = mock_sdk.Anthropic.call_args[1]
            headers = kwargs["default_headers"]
            assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
            assert headers["X-Title"] == "Hermes Agent"
            assert headers["User-Agent"].startswith("HermesAgent/")
            # Auth branch is unchanged: x-api-key via api_key, betas kept.
            assert kwargs["api_key"] == "sk-opencode-secret"
            assert "anthropic-beta" in headers

    def test_minimax_anthropic_endpoint_uses_bearer_auth_for_regular_api_keys(self):
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client(
                "minimax-secret-123",
                base_url="https://api.minimax.io/anthropic",
            )
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert kwargs["auth_token"] == "minimax-secret-123"
            assert "api_key" not in kwargs
            assert kwargs["default_headers"] == {
                "anthropic-beta": "interleaved-thinking-2025-05-14"
            }


    def test_extended_cache_ttl_beta_present_for_anthropic_endpoints(self):
        """Without extended-cache-ttl-2025-04-11, the ttl field on
        cache_control markers (e.g. ``{"type": "ephemeral", "ttl": "1h"}``)
        is silently ignored and Anthropic falls back to the 5-minute
        default. The hermes prompt-caching layer emits ttl markers when
        ``prompt_caching.cache_ttl: 1h`` is configured — the beta must be
        on the wire or that config is a no-op."""
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client("***")
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert (
                "extended-cache-ttl-2025-04-11"
                in kwargs["default_headers"]["anthropic-beta"]
            )

    def test_extended_cache_ttl_beta_stripped_for_minimax_bearer(self):
        """Bearer-auth endpoints host their own models and don't honor
        Anthropic-namespaced betas; the extended-cache-ttl beta must be
        stripped along with the other Anthropic-only betas."""
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client(
                "minimax-cn-secret-123",
                base_url="https://api.minimaxi.com/anthropic",
            )
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert (
                "extended-cache-ttl-2025-04-11"
                not in kwargs["default_headers"]["anthropic-beta"]
            )

    def test_azure_foundry_anthropic_endpoint_uses_bearer_auth(self):
        """Azure AI Foundry's /anthropic endpoint requires Authorization: Bearer.

        Regression test for #26970: without this, builds set api_key (x-api-key)
        and the endpoint returns HTTP 401. Also verifies that Azure retains the
        1M-context beta even though it now matches `_requires_bearer_auth`.
        """
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client(
                "azure-foundry-secret-123",
                base_url="https://my-resource.openai.azure.com/anthropic",
            )
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert kwargs["auth_token"] == "azure-foundry-secret-123"
            assert "api_key" not in kwargs
            # Azure endpoints still get the api-version query param plumbing.
            assert kwargs.get("default_query") == {"api-version": "2025-04-15"}
            # Azure keeps the 1M-context beta (it's not MiniMax).
            betas = kwargs["default_headers"]["anthropic-beta"]
            assert "context-1m-2025-08-07" in betas

    def test_palantir_foundry_anthropic_endpoint_uses_bearer_auth(self):
        """Palantir Foundry's LLM proxy requires Authorization: Bearer.

        Regression test for PR #36043: Palantir's
        ``<org>.palantirfoundry.com/api/v2/llm/proxy/anthropic`` endpoint
        rejects x-api-key with 401 — the SDK must be built with auth_token.
        """
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client(
                "foundry-secret-123",
                base_url="https://acme.palantirfoundry.com/api/v2/llm/proxy/anthropic",
            )
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert kwargs["auth_token"] == "foundry-secret-123"
            assert "api_key" not in kwargs


    def test_disables_sdk_retries_for_api_key(self):
        """#26293: the SDK's default max_retries=2 ignores Retry-After and
        double-retries inside hermes's outer loop. We delegate retry entirely
        to the outer loop, so the client must be built with max_retries=0."""
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            build_anthropic_client("sk-ant-api03-something")
            kwargs = mock_sdk.Anthropic.call_args[1]
            assert kwargs["max_retries"] == 0




class TestReadClaudeCodeCredentials:
    @pytest.fixture(autouse=True)
    def no_keychain(self, monkeypatch):
        monkeypatch.setattr(
            "agent.anthropic_adapter._read_claude_code_credentials_from_keychain",
            lambda: None,
        )

    def test_reads_valid_credentials(self, tmp_path, monkeypatch):
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-token",
                "refreshToken": "sk-ant-oat01-refresh",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        creds = read_claude_code_credentials()
        assert creds is not None
        assert creds["accessToken"] == "sk-ant-oat01-token"
        assert creds["refreshToken"] == "sk-ant-oat01-refresh"
        assert creds["source"] == "claude_code_credentials_file"

    def test_ignores_primary_api_key_for_native_anthropic_resolution(self, tmp_path, monkeypatch):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"primaryApiKey": "sk-ant-api03-primary"}))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        creds = read_claude_code_credentials()
        assert creds is None





class TestIsClaudeCodeTokenValid:
    def test_valid_token(self):
        creds = {"accessToken": "tok", "expiresAt": int(time.time() * 1000) + 3600_000}
        assert is_claude_code_token_valid(creds) is True

    def test_expired_token(self):
        creds = {"accessToken": "tok", "expiresAt": int(time.time() * 1000) - 3600_000}
        assert is_claude_code_token_valid(creds) is False

    def test_no_expiry_but_has_token(self):
        creds = {"accessToken": "tok", "expiresAt": 0}
        assert is_claude_code_token_valid(creds) is True


class TestResolveAnthropicToken:
    def _assert_not_called(*_args, **_kwargs):
        raise AssertionError("should not be called when API key is present")

    def test_prefers_oauth_token_over_api_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-mykey")
        monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat01-mytoken")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        assert resolve_anthropic_token() == "sk-ant-oat01-mytoken"

    def test_does_not_resolve_primary_api_key_as_native_anthropic_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        (tmp_path / ".claude.json").write_text(json.dumps({"primaryApiKey": "sk-ant-api03-primary"}))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        assert resolve_anthropic_token() is None

    def test_falls_back_to_api_key_when_no_oauth_sources_exist(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant...ykey")
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        assert resolve_anthropic_token() == "sk-ant...ykey"

    def test_api_key_wins_over_auto_discovered_claude_code_credentials(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant...ykey")
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "cc-auto-token",
                "refreshToken": "refresh",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        assert resolve_anthropic_token() == "sk-ant...ykey"

    def test_api_key_path_does_not_read_auto_discovered_credentials(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant...ykey")
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials",
            self._assert_not_called,
        )

        assert resolve_anthropic_token() == "sk-ant...ykey"

    def test_falls_back_to_claude_code_credentials(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "cc-auto-token",
                "refreshToken": "refresh",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        assert resolve_anthropic_token() == "cc-auto-token"

    def test_falls_back_to_anthropic_credential_pool_oauth(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        # Isolate source #5 (credential_pool): ensure source #4 (Claude Code
        # creds, incl. the macOS keychain read which Path.home does not cover)
        # returns nothing, mirroring a Hermes-PKCE-only setup.
        monkeypatch.setattr("agent.anthropic_adapter.read_claude_code_credentials", lambda: None)

        pool_entry = SimpleNamespace(
            auth_type="oauth",
            access_token="pool-oauth-token",
        )
        pool = SimpleNamespace(
            _available_entries=lambda **_kwargs: ([pool_entry], []),
        )
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

        assert resolve_anthropic_token() == "pool-oauth-token"

    def test_api_key_wins_over_anthropic_credential_pool_oauth(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant...ykey")
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials",
            self._assert_not_called,
        )
        monkeypatch.setattr(
            "agent.credential_pool.load_pool",
            self._assert_not_called,
        )

        assert resolve_anthropic_token() == "sk-ant...ykey"

    def test_pool_entry_with_null_access_token_does_not_crash(self, monkeypatch, tmp_path):
        """A persisted OAuth entry with access_token=None must not crash the
        resolver (None.strip() would escape the helper's try/excepts and take
        down the whole resolver incl. the ANTHROPIC_API_KEY fallback). It should
        be skipped and the api-key fallback (source #3) should win."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant...ykey")
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr("agent.anthropic_adapter.read_claude_code_credentials", lambda: None)

        broken_entry = SimpleNamespace(auth_type="oauth", access_token=None)
        pool = SimpleNamespace(
            _available_entries=lambda **_kwargs: ([broken_entry], []),
        )
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

        # Must fall through to source #3 (ANTHROPIC_API_KEY), not raise.
        assert resolve_anthropic_token() == "sk-ant...ykey"

    def test_pool_api_key_only_entry_is_not_returned_as_token(self, monkeypatch, tmp_path):
        """resolve_anthropic_token() returns an OAuth bearer token; a pool entry
        whose auth_type is api_key (not oauth) must NOT be returned from the pool
        path — those are consumed via the aux client's _pool_runtime_api_key
        lane, a different resolution concern."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr("agent.anthropic_adapter.read_claude_code_credentials", lambda: None)

        api_key_entry = SimpleNamespace(auth_type="api_key", access_token="sk-pool-apikey")
        pool = SimpleNamespace(
            _available_entries=lambda **_kwargs: ([api_key_entry], []),
        )
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

        # No OAuth entry and no other source → None (the api_key entry is ignored here).
        assert resolve_anthropic_token() is None


    def test_pool_resolution_is_read_only(self, monkeypatch, tmp_path):
        """The resolver must enumerate the pool read-only — clear_expired and
        refresh must both be False so a bare resolve never writes auth.json or
        triggers a network refresh from diagnostic call sites (#50108 MED)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr("agent.anthropic_adapter.read_claude_code_credentials", lambda: None)

        captured = {}
        pool_entry = SimpleNamespace(auth_type="oauth", access_token="pool-oauth-token")

        def _available_entries(**kwargs):
            captured.update(kwargs)
            return ([pool_entry], [])

        pool = SimpleNamespace(_available_entries=_available_entries)
        monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

        assert resolve_anthropic_token() == "pool-oauth-token"
        assert captured == {"clear_expired": False, "refresh": False}

    def test_prefers_refreshable_claude_code_credentials_over_static_anthropic_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat01-static-fake-token")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "cc-auto-token",
                "refreshToken": "refresh-token",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        assert resolve_anthropic_token() == "cc-auto-token"

    def test_keeps_static_anthropic_token_when_only_non_refreshable_claude_key_exists(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat01-static-fake-token")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"primaryApiKey": "sk-ant-api03-managed-fake-key"}))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        assert resolve_anthropic_token() == "sk-ant-oat01-static-fake-token"

    def test_pin_anthropic_token_config_makes_static_token_win(self, monkeypatch, tmp_path):
        """agent.pin_anthropic_token: true inverts the default preference —
        the static env token wins even when a refreshable Claude Code
        credential is present. Opt-in only; default behavior (tested above)
        is unaffected when the config key is absent or false."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat01-static-fake-token")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "cc-auto-token",
                "refreshToken": "refresh-token",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"agent": {"pin_anthropic_token": True}},
        )

        assert resolve_anthropic_token() == "sk-ant-oat01-static-fake-token"

    def test_pin_anthropic_token_false_preserves_default_behavior(self, monkeypatch, tmp_path):
        """Explicit agent.pin_anthropic_token: false behaves identically to
        the key being absent — refreshable Claude Code creds still win."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat01-static-fake-token")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "cc-auto-token",
                "refreshToken": "refresh-token",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"agent": {"pin_anthropic_token": False}},
        )

        assert resolve_anthropic_token() == "cc-auto-token"


class TestRefreshOauthToken:
    def test_returns_none_without_refresh_token(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        # Neutralize live Claude Code sources (macOS Keychain + ~/.claude file)
        # so the adopt-already-refreshed branch can't short-circuit with a real
        # credential on a dev/CI machine that happens to have Claude Code creds.
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials", lambda: None
        )
        creds = {"accessToken": "expired", "refreshToken": "", "expiresAt": 0}
        assert _refresh_oauth_token(creds) is None

    def test_successful_refresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials", lambda: None
        )

        creds = {
            "accessToken": "old-token",
            "refreshToken": "refresh-123",
            "expiresAt": int(time.time() * 1000) - 3600_000,
        }

        mock_response = json.dumps({
            "access_token": "new-token-abc",
            "refresh_token": "new-refresh-456",
            "expires_in": 7200,
        }).encode()

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=MagicMock(
                read=MagicMock(return_value=mock_response)
            ))
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_ctx

            result = _refresh_oauth_token(creds)

        assert result == "new-token-abc"
        # Verify credentials were written back
        cred_file = tmp_path / ".claude" / ".credentials.json"
        assert cred_file.exists()
        written = json.loads(cred_file.read_text())
        assert written["claudeAiOauth"]["accessToken"] == "new-token-abc"
        assert written["claudeAiOauth"]["refreshToken"] == "new-refresh-456"

    def test_failed_refresh_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials", lambda: None
        )
        creds = {
            "accessToken": "old",
            "refreshToken": "refresh-123",
            "expiresAt": 0,
        }

        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            assert _refresh_oauth_token(creds) is None


class TestWriteClaudeCodeCredentials:
    def test_writes_new_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        _write_claude_code_credentials("tok", "ref", 12345)
        cred_file = tmp_path / ".claude" / ".credentials.json"
        assert cred_file.exists()
        data = json.loads(cred_file.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "tok"
        assert data["claudeAiOauth"]["refreshToken"] == "ref"
        assert data["claudeAiOauth"]["expiresAt"] == 12345

    def test_preserves_existing_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_file = cred_dir / ".credentials.json"
        cred_file.write_text(json.dumps({"otherField": "keep-me"}))
        _write_claude_code_credentials("new-tok", "new-ref", 99999)
        data = json.loads(cred_file.read_text())
        assert data["otherField"] == "keep-me"
        assert data["claudeAiOauth"]["accessToken"] == "new-tok"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits not enforced on Windows")
    def test_credentials_file_created_with_0o600(self, tmp_path, monkeypatch):
        """Refreshed Claude Code credentials must land on disk at 0o600.

        Regression for the TOCTOU race where ``write_text`` + ``replace``
        + post-write ``chmod`` left both the temp file and the destination
        briefly readable at the process umask (commonly 0o644). Mirrors
        the fix shipped in #19673 (google_oauth) and #21148 (mcp_oauth).
        """
        import stat as _stat
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)
        _write_claude_code_credentials("tok", "ref", 12345)

        cred_file = tmp_path / ".claude" / ".credentials.json"
        assert cred_file.exists()
        mode = _stat.S_IMODE(cred_file.stat().st_mode)
        assert mode == 0o600, f"creds file mode {oct(mode)} != 0o600 — TOCTOU race regressed"


class TestResolveWithRefresh:
    def test_auto_refresh_on_expired_creds(self, monkeypatch, tmp_path):
        """When cred file has expired token + refresh token, auto-refresh is attempted."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        # Set up expired creds with a refresh token
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired-tok",
                "refreshToken": "valid-refresh",
                "expiresAt": int(time.time() * 1000) - 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        # Mock refresh to succeed
        with patch("agent.anthropic_adapter._refresh_oauth_token", return_value="refreshed-token"):
            result = resolve_anthropic_token()

        assert result == "refreshed-token"

    def test_static_env_oauth_token_does_not_block_refreshable_claude_creds(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-ant-oat01-expired-env-token")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired-claude-creds-token",
                "refreshToken": "valid-refresh",
                "expiresAt": int(time.time() * 1000) - 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_adapter._refresh_oauth_token", return_value="refreshed-token"):
            result = resolve_anthropic_token()

        assert result == "refreshed-token"


class TestRunOauthSetupToken:

    def test_returns_token_from_credential_files(self, monkeypatch, tmp_path):
        """After subprocess completes, reads credentials from Claude Code files."""
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)

        # Pre-create credential files that will be found after subprocess
        cred_file = tmp_path / ".claude" / ".credentials.json"
        cred_file.parent.mkdir(parents=True)
        cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "from-cred-file",
                "refreshToken": "refresh",
                "expiresAt": int(time.time() * 1000) + 3600_000,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            token = run_oauth_setup_token()

        assert token == "from-cred-file"
        # Don't assert exact call count — the contract is "credentials flow
        # through", not "exactly one subprocess call". xdist cross-test
        # pollution (other tests shimming subprocess via plugins) has flaked
        # assert_called_once() in CI.
        assert mock_run.called


    def test_returns_none_when_no_creds_found(self, monkeypatch, tmp_path):
        """Returns None when subprocess completes but no credentials are found."""
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            token = run_oauth_setup_token()

        assert token is None



# ---------------------------------------------------------------------------
# Model name normalization
# ---------------------------------------------------------------------------


class TestNormalizeModelName:
    def test_strips_anthropic_prefix(self):
        assert normalize_model_name("anthropic/claude-sonnet-4-20250514") == "claude-sonnet-4-20250514"




    def test_preserve_dots_for_alibaba_dashscope(self):
        """Alibaba/DashScope use dots in model names (e.g. qwen3.5-plus). Fixes #1739."""
        assert normalize_model_name("qwen3.5-plus", preserve_dots=True) == "qwen3.5-plus"
        assert normalize_model_name("anthropic/qwen3.5-plus", preserve_dots=True) == "qwen3.5-plus"
        assert normalize_model_name("qwen3.5-flash", preserve_dots=True) == "qwen3.5-flash"


# ---------------------------------------------------------------------------
# Tool conversion
# ---------------------------------------------------------------------------


class TestConvertTools:
    def test_converts_openai_to_anthropic_format(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search the web",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ]
        result = convert_tools_to_anthropic(tools)
        assert len(result) == 1
        assert result[0]["name"] == "search"
        assert result[0]["description"] == "Search the web"
        assert result[0]["input_schema"]["properties"]["query"]["type"] == "string"

    def test_empty_tools(self):
        assert convert_tools_to_anthropic([]) == []
        assert convert_tools_to_anthropic(None) == []

    def test_strips_nullable_union_from_input_schema(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "run",
                    "description": "Run command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "timeout": {
                                "anyOf": [{"type": "integer"}, {"type": "null"}],
                                "default": None,
                            },
                        },
                        "required": ["command"],
                    },
                },
            }
        ]

        result = convert_tools_to_anthropic(tools)

        assert result[0]["input_schema"]["properties"]["timeout"] == {
            "type": "integer",
            "default": None,
        }
        assert result[0]["input_schema"]["required"] == ["command"]


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


class TestConvertMessages:









    def test_strips_tool_use_when_result_not_immediately_adjacent(self):
        """A tool_use whose result appears LATER but not in the immediately
        following user message must be stripped (adjacency, #52145).

        The old logic matched tool_result ids globally across the whole
        transcript, so it would wrongly KEEP such a tool_use; Anthropic then
        400s because the result does not follow the tool_use turn. The adjacency
        rewrite only honors a result in the next user message.
        """
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc_late", "function": {"name": "search", "arguments": "{}"}},
                ],
            },
            {"role": "user", "content": "actually, something else"},
            {"role": "assistant", "content": "sure"},
            {"role": "tool", "tool_call_id": "tc_late", "content": "late result"},
        ]
        _, result = convert_messages_to_anthropic(messages)
        for m in result:
            if m["role"] == "assistant" and isinstance(m["content"], list):
                assert all(b.get("type") != "tool_use" for b in m["content"]), (
                    "non-adjacent tool_use should have been stripped"
                )
        for m in result:
            if m["role"] == "user" and isinstance(m["content"], list):
                assert all(b.get("type") != "tool_result" for b in m["content"]), (
                    "orphaned late tool_result should have been stripped"
                )


    def test_system_with_cache_control(self):
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "System prompt", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {"role": "user", "content": "Hi"},
        ]
        system, result = convert_messages_to_anthropic(messages)
        # When cache_control is present, system should be a list of blocks
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}


    def test_assistant_cache_control_blocks_are_preserved(self):
        messages = apply_anthropic_cache_control([
            {"role": "system", "content": "System prompt"},
            {"role": "assistant", "content": "Hello from assistant"},
        ])

        _, result = convert_messages_to_anthropic(messages)
        assistant_msg = next(m for m in result if m["role"] == "assistant")
        assistant_blocks = assistant_msg["content"]

        assert assistant_blocks[0]["type"] == "text"
        assert assistant_blocks[0]["text"] == "Hello from assistant"
        assert assistant_blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_assistant_tool_use_cache_control_is_preserved(self):
        messages = apply_anthropic_cache_control([
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Run the tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc_1", "function": {"name": "test_tool", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
        ], native_anthropic=True)

        _, result = convert_messages_to_anthropic(messages)
        assistant_msg = [m for m in result if m["role"] == "assistant"][0]
        tool_use = assistant_msg["content"][-1]

        assert tool_use["type"] == "tool_use"
        assert tool_use["id"] == "tc_1"
        assert tool_use["cache_control"] == {"type": "ephemeral"}

    def test_ordered_replay_keeps_cache_control_from_nonempty_content(self):
        """An assistant turn that interleaves signed thinking with a tool_use
        AND has preamble text carries its cache_control INSIDE ``content``
        (apply_anthropic_cache_control marks the last content block, not the
        top level). The ordered-replay branch rebuilds the message from
        ``anthropic_content_blocks`` alone, so without harvesting that marker
        the breakpoint is dropped -- and it is *burned*, because
        _can_carry_marker already spent a budget slot on this message.

        #56195 covers the blank-content shape; this is the non-empty one, which
        is what a Claude thinking+tools turn normally looks like.
        """
        preamble = "I will read a.py now."
        messages = apply_anthropic_cache_control([
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Read a.py"},
            {
                "role": "assistant",
                "content": preamble,
                "anthropic_content_blocks": [
                    {"type": "thinking", "thinking": "Need a tool.", "signature": "sig_1"},
                    {"type": "text", "text": preamble},
                    {"type": "tool_use", "id": "tc_1", "name": "test_tool", "input": {}},
                ],
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "test_tool", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "contents"},
        ])

        _system, converted = convert_messages_to_anthropic(messages)
        assistant = next(m for m in converted if m.get("role") == "assistant")
        marked = [
            b for b in assistant["content"]
            if isinstance(b, dict) and b.get("cache_control")
        ]
        assert marked, (
            "the assistant cache breakpoint was dropped by the ordered-replay "
            "path and the budget slot is burned"
        )
        # The signed thinking block must still lead the replayed message.
        assert assistant["content"][0]["type"] == "thinking"

    def test_ordered_replay_tool_use_cache_control_is_preserved(self):
        messages = apply_anthropic_cache_control([
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Run the tool"},
            {
                "role": "assistant",
                "content": "",
                "anthropic_content_blocks": [
                    {
                        "type": "thinking",
                        "thinking": "Need a tool.",
                        "signature": "sig_1",
                    },
                    {
                        "type": "tool_use",
                        "id": "tc_1",
                        "name": "test_tool",
                        "input": {"query": "raw"},
                    },
                ],
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "function": {
                            "name": "test_tool",
                            "arguments": '{"query":"redacted"}',
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
        ], native_anthropic=True)

        _, result = convert_messages_to_anthropic(messages)
        assistant_msg = [m for m in result if m["role"] == "assistant"][0]
        thinking, tool_use = assistant_msg["content"]

        assert thinking["type"] == "thinking"
        assert "cache_control" not in thinking
        assert tool_use["type"] == "tool_use"
        assert tool_use["id"] == "tc_1"
        assert tool_use["input"] == {"query": "redacted"}
        assert tool_use["cache_control"] == {"type": "ephemeral"}

    def test_tool_cache_control_is_preserved_on_tool_result_block(self):
        messages = apply_anthropic_cache_control([
            {"role": "system", "content": "System prompt"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc_1", "function": {"name": "test_tool", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
        ], native_anthropic=True)

        _, result = convert_messages_to_anthropic(messages)
        user_msg = next(
            m for m in result
            if m["role"] == "user"
            and isinstance(m["content"], list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        )
        tool_block = user_msg["content"][0]

        assert tool_block["type"] == "tool_result"
        assert tool_block["tool_use_id"] == "tc_1"
        assert tool_block["content"] == "result"
        assert tool_block["cache_control"] == {"type": "ephemeral"}




    def test_anthropic_content_blocks_replayed_verbatim(self):
        """When the assistant turn carries the original Anthropic content
        array, it's replayed in original block order.

        Recomposing from reasoning_details + tool_calls would emit
        ``[thinking_A, thinking_B, tool_use_1, tool_use_2]`` regardless of
        original ordering. Anthropic signs each thinking block against its
        position; ``clear_thinking_20251015`` rejects any reordering with
        HTTP 400. The verbatim path keeps positions untouched.
        """
        original_blocks = [
            {"type": "thinking", "thinking": "step 1", "signature": "sig_A"},
            {"type": "tool_use", "id": "tu_1", "name": "lookup", "input": {"q": "x"}},
            {"type": "thinking", "thinking": "step 2", "signature": "sig_B"},
            {"type": "tool_use", "id": "tu_2", "name": "lookup", "input": {"q": "y"}},
        ]
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "anthropic_content_blocks": original_blocks,
                # reasoning_details + tool_calls would normally co-exist;
                # the verbatim path must ignore them in favor of the
                # captured array.
                "reasoning_details": [
                    {"type": "thinking", "thinking": "step 1", "signature": "sig_A"},
                    {"type": "thinking", "thinking": "step 2", "signature": "sig_B"},
                ],
                "tool_calls": [
                    {"id": "tu_1", "function": {"name": "lookup", "arguments": '{"q": "x"}'}},
                    {"id": "tu_2", "function": {"name": "lookup", "arguments": '{"q": "y"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "x result"},
            {"role": "tool", "tool_call_id": "tu_2", "content": "y result"},
        ]

        _, result = convert_messages_to_anthropic(messages)
        assistant_blocks = next(msg for msg in result if msg["role"] == "assistant")["content"]

        # Original order preserved (interleaved thinking among tool_uses)
        assert [b["type"] for b in assistant_blocks] == [
            "thinking",
            "tool_use",
            "thinking",
            "tool_use",
        ]
        assert assistant_blocks[0]["signature"] == "sig_A"
        assert assistant_blocks[2]["signature"] == "sig_B"
        # tool_use blocks intact (id + input round-trip)
        assert assistant_blocks[1]["id"] == "tu_1"
        assert assistant_blocks[1]["input"] == {"q": "x"}
        assert assistant_blocks[3]["id"] == "tu_2"

    def test_anthropic_content_blocks_deepcopied_not_aliased(self):
        """Replayed array must be a deep copy — downstream mutation (e.g.
        cache_control stripping at the bottom of convert_messages_to_anthropic)
        must not leak back to the stored message."""
        original_blocks = [
            {"type": "thinking", "thinking": "x", "signature": "sig"},
            {"type": "text", "text": "hello"},
        ]
        stored = {
            "role": "assistant",
            "content": "",
            "anthropic_content_blocks": original_blocks,
        }
        messages = [{"role": "user", "content": "hi"}, stored]

        _, result = convert_messages_to_anthropic(messages)
        # Mutate the result to confirm independence
        result[1]["content"][0]["thinking"] = "MUTATED"
        assert original_blocks[0]["thinking"] == "x"
        assert stored["anthropic_content_blocks"][0]["thinking"] == "x"

    def test_decomposition_path_still_runs_when_verbatim_absent(self):
        """Sanity check: the existing recomposition logic is unchanged when
        ``anthropic_content_blocks`` is not on the message."""
        messages = [
            {
                "role": "assistant",
                "content": "Hello",
                "reasoning_details": [
                    {"type": "thinking", "thinking": "thought", "signature": "sig"},
                ],
            },
        ]
        _, result = convert_messages_to_anthropic(messages)
        # A leading "(empty)" user turn is prepended (Anthropic requires
        # messages[0] to be role=user); the assistant turn follows it.
        assistant = next(m for m in result if m.get("role") == "assistant")
        blocks = assistant["content"]
        assert blocks[0]["type"] == "thinking"
        assert blocks[0]["signature"] == "sig"
        assert blocks[1]["type"] == "text"
        assert blocks[1]["text"] == "Hello"

    def test_converts_data_url_image_to_anthropic_image_block(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,ZmFrZQ=="},
                    },
                ],
            }
        ]

        _, result = convert_messages_to_anthropic(messages)
        blocks = result[0]["content"]
        assert blocks[0] == {"type": "text", "text": "Describe this image"}
        assert blocks[1] == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "ZmFrZQ==",
            },
        }

    def test_converts_remote_image_url_to_anthropic_image_block(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/cat.png"},
                    },
                ],
            }
        ]

        _, result = convert_messages_to_anthropic(messages)
        blocks = result[0]["content"]
        assert blocks[1] == {
            "type": "image",
            "source": {
                "type": "url",
                "url": "https://example.com/cat.png",
            },
        }

    def test_empty_cached_assistant_tool_turn_converts_without_empty_text_block(self):
        messages = apply_anthropic_cache_control([
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Find the skill"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc_1", "function": {"name": "skill_view", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
        ])

        _, result = convert_messages_to_anthropic(messages)

        assistant_turn = next(msg for msg in result if msg["role"] == "assistant")
        assistant_blocks = assistant_turn["content"]

        assert all(not (b.get("type") == "text" and b.get("text") == "") for b in assistant_blocks)
        assert any(b.get("type") == "tool_use" for b in assistant_blocks)

    def test_empty_user_message_string_gets_placeholder(self):
        """Empty user message strings should get '(empty message)' placeholder.

        Anthropic rejects requests with empty user message content.
        Regression test for #3143 — Discord @mention-only messages.
        """
        messages = [
            {"role": "user", "content": ""},
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "(empty message)"




    def test_leading_assistant_after_compaction_gets_user_turn_prepended(self):
        """The adapter backstops compactors that emit a leading assistant summary."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "assistant", "content": "[Context compaction summary] earlier work…"},
            {"role": "user", "content": "continue"},
        ]

        system, result = convert_messages_to_anthropic(messages)

        assert system == "You are helpful."
        assert result[0]["role"] == "user"
        assert result[0]["content"] == [{"type": "text", "text": "(empty)"}]
        assert result[1]["role"] == "assistant"
        assert any(
            m["role"] == "assistant" and "Context compaction summary" in str(m["content"])
            for m in result
        )





# ---------------------------------------------------------------------------
# Build kwargs
# ---------------------------------------------------------------------------


class TestBuildAnthropicKwargs:


    def test_oauth_path_does_not_double_prefix_mcp_tools(self):
        """OAuth path normalizes every tool name to double-underscore ``mcp__``.

        FORK NOTE (2026-06-22 upstream sync): the fork previously passed tool
        names through bare on the OAuth wire. Converged to upstream's GH-25255
        ``mcp__`` normalization this sync (live Max-subscription billing
        verification: a single-underscore ``mcp_`` name flips the request to
        the extra-usage lane; ``mcp__`` is accepted). ``normalize_response``
        reverses the wire name back to the registered name so dispatch still
        resolves originals.

        The invariant: NOTHING reaches the OAuth wire with a single-underscore
        ``mcp_`` prefix, and an already-``mcp__`` name is never double-prefixed.
        """
        tools = [
            # Single-underscore MCP-server names must be promoted to ``mcp__``
            # (the gap the bare->mcp__ constant swap left open).
            {"type": "function", "function": {"name": "slack_slack_search_public", "description": "x"}},
            {"type": "function", "function": {"name": "jira_jira_search_issues", "description": "x"}},
        ]
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            tools=tools,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
        )
        names = [t["name"] for t in kwargs["tools"]]
        # Bare names land on the double-underscore form.
        assert "mcp__slack_slack_search_public" in names, names
        assert "mcp__jira_jira_search_issues" in names, names
        # The core invariant: NOTHING single-underscore reaches the wire, and
        # no name is double-prefixed (``mcp__mcp__``).
        for n in names:
            assert not (n.startswith("mcp_") and not n.startswith("mcp__")), (
                f"single-underscore mcp_ reached the OAuth wire: {names}"
            )
            assert not n.startswith("mcp__mcp__"), f"tool name double-prefixed: {names}"

    def test_fast_mode_oauth_default_keeps_context_1m_beta(self, monkeypatch):
        """OAuth fast-mode requests carry context-1m-2025-08-07 when the
        small-prompt 1M-tier gate is disabled (or when the prompt is large
        enough to need 1M context).

        Default behavior changed 2026-05-06: small prompts (<150K tokens
        estimate) now strip context-1m to avoid the slower 1M-tier queue
        — see the gate at the bottom of build_anthropic_kwargs. Disable
        with HERMES_CONTEXT_1M_THRESHOLD_TOKENS=0 for this test so it
        still asserts the underlying default-betas wiring works.
        """
        monkeypatch.setenv("HERMES_CONTEXT_1M_THRESHOLD_TOKENS", "0")
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
            fast_mode=True,
        )
        betas = kwargs["betas"]
        assert "fast-mode-2026-02-01" in betas
        assert "oauth-2025-04-20" in betas
        assert "context-1m-2025-08-07" in betas

    def test_gate_default_off_keeps_context_1m_beta(self, monkeypatch):
        """The 1M-tier gate is DISABLED by default (threshold=0) because
        running context can exceed the request body size when there's a
        large cached prefix. With the gate off, small-prompt requests
        keep context-1m-2025-08-07 (via the client-level default header)."""
        monkeypatch.delenv("HERMES_CONTEXT_1M_THRESHOLD_TOKENS", raising=False)
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
        )
        # No per-request override should have been emitted by the gate.
        # (Other code paths — fast_mode, server-side tools — may still
        # set extra_headers, but for this minimal request none of those
        # apply, so extra_headers should be absent.)
        assert "extra_headers" not in kwargs

    def test_small_prompt_strips_context_1m_when_gate_enabled(self, monkeypatch):
        """When the gate is opt-in via env, small-prompt requests strip
        context-1m-2025-08-07 from the per-request beta header."""
        monkeypatch.setenv("HERMES_CONTEXT_1M_THRESHOLD_TOKENS", "150000")
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
        )
        betas = kwargs.get("betas") or []
        assert "context-1m-2025-08-07" not in betas
        assert "interleaved-thinking-2025-05-14" in betas
        assert "oauth-2025-04-20" in betas

    def test_large_prompt_keeps_context_1m_beta_with_gate_enabled(self, monkeypatch):
        """Prompts above the threshold keep the 1M beta even when the
        gate is enabled — they need it."""
        monkeypatch.setenv("HERMES_CONTEXT_1M_THRESHOLD_TOKENS", "150000")
        # Build a prompt > 150K tokens (~600K chars). Char/4 estimate is
        # what the gate uses, so a 700K-char user message exceeds it.
        big = "x" * 700_000
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": big}],
            tools=None,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
        )
        # Large prompts don't get a per-request override; they keep the
        # client-level betas (which include context-1m). Either no
        # extra_headers, OR extra_headers with the beta still listed.
        eh = kwargs.get("extra_headers")
        if eh:
            assert "context-1m-2025-08-07" in eh.get("anthropic-beta", "")

    def test_gate_disabled_via_env_keeps_context_1m_beta(self, monkeypatch):
        """HERMES_CONTEXT_1M_THRESHOLD_TOKENS=0 disables the gate entirely."""
        monkeypatch.setenv("HERMES_CONTEXT_1M_THRESHOLD_TOKENS", "0")
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
        )
        eh = kwargs.get("extra_headers")
        if eh:
            assert "context-1m-2025-08-07" in eh.get("anthropic-beta", "")

    def test_fast_mode_oauth_drop_context_1m_beta_strips_only_1m(self):
        """drop_context_1m_beta=True strips context-1m from fast-mode
        extra_headers while preserving every other OAuth + fast-mode beta."""
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
            fast_mode=True,
            drop_context_1m_beta=True,
        )
        betas = kwargs["betas"]
        assert "context-1m-2025-08-07" not in betas
        assert "fast-mode-2026-02-01" in betas
        assert "oauth-2025-04-20" in betas
        assert "claude-code-20250219" in betas
        assert "interleaved-thinking-2025-05-14" in betas

    def test_reasoning_config_maps_to_manual_thinking_for_pre_4_6_models(self):
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "think hard"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "high"},
        )
        assert kwargs["thinking"]["type"] == "enabled"
        assert kwargs["thinking"]["budget_tokens"] == 16000
        assert kwargs["temperature"] == 1
        assert kwargs["max_tokens"] >= 16000 + 4096
        assert "output_config" not in kwargs

    def test_reasoning_config_maps_to_adaptive_thinking_for_4_6_models(self):
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "think hard"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "high"},
        )
        # Adaptive thinking with no ``display`` field — matches Claude Code's
        # wire shape (Opus 4.7 default is "omitted"; setting "summarized"
        # adds a summary-generation pass that magnifies internal-thinking
        # latency). Per HERMES_THINKING_DISPLAY env var to opt back in.
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "high"}
        assert "budget_tokens" not in kwargs["thinking"]
        assert "display" not in kwargs["thinking"]
        assert "temperature" not in kwargs
        assert kwargs["max_tokens"] == 4096

    def test_thinking_display_env_override(self, monkeypatch):
        """HERMES_THINKING_DISPLAY=summarized opts back into the previous
        behaviour for users who prefer the visible thinking summary even
        at the latency cost."""
        monkeypatch.setenv("HERMES_THINKING_DISPLAY", "summarized")
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "xhigh"},
        )
        assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}

    def test_thinking_display_env_invalid_ignored(self, monkeypatch):
        """An unknown HERMES_THINKING_DISPLAY value is ignored — defaults
        to omitted (no display field)."""
        monkeypatch.setenv("HERMES_THINKING_DISPLAY", "garbage-value")
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "xhigh"},
        )
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert "display" not in kwargs["thinking"]

    def test_reasoning_config_downgrades_xhigh_to_high_for_4_6_models(self):
        # Opus 4.7 added "xhigh" as a distinct effort level (low/medium/high/
        # xhigh/max). Sonnet/Opus 4.6 reject xhigh with a 400; Sonnet 4.6
        # and Haiku 4.5 also reject "max" (Opus-tier only). Per Claude Code's
        # disassembled binary (`return"xhigh";return"high"`), the right
        # fallback is "high" — which works on every adaptive-thinking model.
        # Updated 2026-05-05 (commit b8dea73373) from the previous
        # xhigh→max alias that 400'd on Sonnet/Haiku.
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "think harder"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "xhigh"},
        )
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "high"}

    def test_reasoning_config_preserves_xhigh_for_4_7_models(self):
        # On 4.7+ xhigh is a real level and the recommended default for
        # coding/agentic work — keep it distinct from max.
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "think harder"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "xhigh"},
        )
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "xhigh"}


    def test_reasoning_config_maps_max_effort_for_4_7_models(self):
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "maximum reasoning please"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True, "effort": "max"},
        )
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "max"}


    def test_supports_fast_mode_predicate(self):
        """Fast mode is Opus 4.6 only — Opus 4.7 and others must be excluded.

        For Opus 4.8 the fast variant is a separate model ID
        (anthropic/claude-opus-4.8-fast) routed through the normal model
        field, NOT via the ``speed: "fast"`` request parameter. So
        ``_supports_fast_mode`` (which gates the parameter) must stay
        False for both opus-4-8 and opus-4-8-fast.
        """
        from agent.anthropic_adapter import _supports_fast_mode
        assert _supports_fast_mode("claude-opus-4-6") is True
        assert _supports_fast_mode("anthropic/claude-opus-4-6") is True
        assert _supports_fast_mode("claude-opus-4-7") is False
        assert _supports_fast_mode("claude-opus-4-8") is False
        assert _supports_fast_mode("claude-opus-4-8-fast") is False
        assert _supports_fast_mode("claude-sonnet-4-6") is False
        assert _supports_fast_mode("claude-haiku-4-5") is False
        assert _supports_fast_mode("") is False

    def test_fable_class_models_route_as_adaptive_thinking(self):
        """Invariant: unknown/new Claude models default to the modern (4.7+)
        contract — adaptive thinking, xhigh-capable, sampling-params-forbidden —
        without any per-model code change. Named models (claude-fable-5) and
        hypothetical future ones must all classify modern; only the explicit
        legacy list stays on the manual path.
        """
        from agent.anthropic_adapter import (
            _supports_adaptive_thinking,
            _supports_xhigh_effort,
            _forbids_sampling_params,
            _get_anthropic_max_output,
        )
        # New / unknown Claude models → modern contract by default.
        for m in (
            "claude-fable-5",
            "anthropic/claude-fable-5",
            "claude-saga-2",            # hypothetical future named model
            "anthropic/claude-opus-9",  # hypothetical future numbered model
        ):
            assert _supports_adaptive_thinking(m) is True, m
            assert _supports_xhigh_effort(m) is True, m
            assert _forbids_sampling_params(m) is True, m
        # 1M-context reasoning model → highest output ceiling.
        assert _get_anthropic_max_output("anthropic/claude-fable-5") == 128_000



    def test_non_claude_anthropic_models_use_manual_path(self):
        """Non-Claude Anthropic-Messages models (minimax, qwen3, glm) must not
        be misclassified as adaptive by the default-to-modern rule. Kimi is
        the deliberate exception — see test_kimi_family_uses_adaptive_path."""
        from agent.anthropic_adapter import (
            _supports_adaptive_thinking,
            _supports_xhigh_effort,
            _forbids_sampling_params,
        )
        for m in ("minimax-m2", "qwen3-max", "glm-4.6"):
            assert _supports_adaptive_thinking(m) is False, m
            assert _supports_xhigh_effort(m) is False, m
            assert _forbids_sampling_params(m) is False, m


    def test_bare_k3_coding_plan_slug_is_kimi_family(self):
        """Kimi Coding Plan serves K3 as the bare slug ``k3`` — it must be
        classified as Kimi family (adaptive thinking) even on proxied
        endpoints where only the model name is available. Lookalike
        non-Kimi names must NOT match the exact-slug rule."""
        from agent.anthropic_adapter import (
            _model_name_is_kimi_family,
            _supports_adaptive_thinking,
        )
        for m in ("k3", "K3", "moonshotai/k3", "k3.1-preview", "k3-turbo"):
            assert _model_name_is_kimi_family(m) is True, m
        assert _supports_adaptive_thinking("k3") is True
        # Prefix-lookalikes without a separator must not be swept in.
        for m in ("k30", "k3000-chat", "keras-3"):
            assert _model_name_is_kimi_family(m) is False, m

    def test_fast_mode_omitted_for_unsupported_model(self):
        """fast_mode=True on Opus 4.7 must NOT inject speed=fast (API 400s)."""
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            reasoning_config=None,
            fast_mode=True,
        )
        # extra_body either absent or doesn't carry "speed"
        assert kwargs.get("speed") != "fast"
        # No fast-mode beta header should be added either
        assert "fast-mode-2026-02-01" not in (kwargs.get("betas") or [])

    def test_fast_mode_still_applied_on_opus_46(self):
        """Regression guard — fast mode must still work on Opus 4.6."""
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            reasoning_config=None,
            fast_mode=True,
        )
        assert kwargs.get("speed") == "fast"
        assert "fast-mode-2026-02-01" in kwargs["betas"]



    def test_default_max_tokens_opus_4_6(self):
        # 4.6+ models cap at 16K to mirror Claude Code's main chat path
        # (commit b8dea73373, 2026-05-05). Override per-call via the
        # max_tokens kwarg when a longer output is needed.
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=None,
            reasoning_config=None,
        )
        assert kwargs["max_tokens"] == 16_000

    def test_default_max_tokens_sonnet_4_6(self):
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=None,
            reasoning_config=None,
        )
        assert kwargs["max_tokens"] == 16_000

    def test_default_max_tokens_date_stamped_model(self):
        """Date-stamped model IDs should resolve via substring match."""
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-5-20250929",
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=None,
            reasoning_config=None,
        )
        assert kwargs["max_tokens"] == 16_000




    def test_context_length_clamp(self):
        """max_tokens should be clamped to context_length if it's smaller.

        Today the model output cap (16K for 4.6+) is below typical
        context_length values, so clamp doesn't usually kick in. Use an
        older model with a larger native limit to actually exercise it.
        """
        kwargs = build_anthropic_kwargs(
            model="claude-3-7-sonnet",  # 128K output
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=None,
            reasoning_config=None,
            context_length=50000,
        )
        assert kwargs["max_tokens"] == 49999  # context_length - 1

    def test_context_length_no_clamp_when_larger(self):
        """No clamping when context_length exceeds output limit."""
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-6",  # 16K output (post-2026-05-05)
            messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            max_tokens=None,
            reasoning_config=None,
            context_length=200000,
        )
        assert kwargs["max_tokens"] == 16_000

    # ── Stale tool_use scrubbing ────────────────────────────────────────
    #
    # Anthropic's API returns ``invalid_request_error: Tool reference 'X'
    # not found in available tools`` when the message history contains a
    # ``tool_use`` whose name isn't in the current ``tools`` array.
    # Triggering scenarios in the wild (errors.log 2026-05-07/08):
    #   * MCP server reconnect failures — e.g. a custom MCP server's
    #     ``*_get_prompt``, ``salesforce_get_prompt``,
    #     ``StackOverflowTeams_create_QA``, or similar tools disappearing
    #     from the tool list when their server fails to reconnect
    #   * Toolset switches mid-session — ``clarify`` dropped after
    #     ``/toolsets remove`` while tool_use blocks linger in history.
    # build_anthropic_kwargs now scrubs these via
    # ``_strip_unknown_tool_blocks`` after tool conversion.

    def test_strips_unknown_tool_use_when_tool_missing_from_current_list(self):
        """tool_use for a name not in the current tools array gets rewritten
        to a text breadcrumb instead of the tool_use block (which would
        otherwise be rejected by the API)."""
        messages = [
            {"role": "user", "content": "do the thing"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_clarify_1",
                        "function": {
                            "name": "clarify",
                            "arguments": '{"question": "which?"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_clarify_1", "content": "user picked option A"},
            {"role": "user", "content": "now go"},
        ]
        # Current toolset does NOT include `clarify` (e.g. dropped via
        # /toolsets remove, or this is a leaf subagent with no UI tools).
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages,
            tools=[
                {"type": "function", "function": {"name": "read_file", "description": "x"}},
            ],
            max_tokens=4096,
            reasoning_config=None,
        )
        # No tool_use / tool_result for `clarify` should remain in the
        # outgoing messages.
        for m in kwargs["messages"]:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                assert not (
                    b.get("type") == "tool_use" and b.get("name") == "clarify"
                ), "stale tool_use must not reach the wire"
                assert not (
                    b.get("type") == "tool_result" and b.get("tool_use_id") == "tc_clarify_1"
                ), "stale tool_result must not reach the wire"
        # And a breadcrumb text block should mention clarify so the model
        # still has context for what happened.
        joined = ""
        for m in kwargs["messages"]:
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        joined += b.get("text", "")
        assert "clarify" in joined, (
            "expected a breadcrumb mentioning the dropped tool name"
        )

    def test_keeps_known_tool_use_intact(self):
        """Sanity: tool_use for tools that ARE in the live list is untouched."""
        messages = [
            {"role": "user", "content": "search please"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_read_1",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/x"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_read_1", "content": "file contents"},
        ]
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages,
            tools=[
                {"type": "function", "function": {"name": "read_file", "description": "x"}},
            ],
            max_tokens=4096,
            reasoning_config=None,
        )
        # tool_use block should still be present with its original name.
        found_tool_use = False
        for m in kwargs["messages"]:
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "read_file":
                        found_tool_use = True
        assert found_tool_use, "live tool_use block should not be rewritten"

    def test_strips_unknown_tool_use_with_no_tools_at_all(self):
        """When the current call has no tools whatsoever, ALL tool_use
        blocks in history are stale by definition.  This used to 400
        when the model history mentioned any tool — now they collapse
        to text breadcrumbs."""
        messages = [
            {"role": "user", "content": "earlier"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_x",
                        "function": {"name": "salesforce_get_prompt", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_x", "content": "stale"},
            {"role": "user", "content": "now"},
        ]
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages,
            tools=None,
            max_tokens=4096,
            reasoning_config=None,
        )
        assert "tools" not in kwargs
        for m in kwargs["messages"]:
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        assert b.get("type") not in ("tool_use", "tool_result"), (
                            "every tool_use/result must be stripped when tools=None"
                        )

    def test_oauth_cc_aliased_historical_tool_use_is_not_stripped(self):
        """On the OAuth path, ``terminal`` (hermes name) is aliased to
        ``Bash`` (CC canonical name) downstream. Message history from
        prior OAuth turns therefore carries ``tool_use(name="Bash")``
        even though the live tool list this turn has ``terminal``.

        ``_strip_unknown_tool_blocks`` runs BEFORE the alias replacement,
        so without expanding the allowlist to include CC alias targets,
        every historical ``Bash`` call gets rewritten to a "[Previous
        tool call: Bash(...) — tool no longer available in this turn.]"
        breadcrumb — silently dropping a perfectly live tool from the
        model's context.

        Regression observed in interactive use immediately after the
        partial-strip ordering fix landed: hermes ate a ``Bash`` call
        with ``split -l 150 /tmp/homelab_audit.txt ...`` even though
        Bash was clearly available in the current toolset.
        """
        messages = [
            {"role": "user", "content": "split this audit"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_bash_cc_1",
                        "function": {
                            # CC canonical name — what the model emitted
                            # last turn after the alias-on-the-wire swap.
                            "name": "Bash",
                            "arguments": '{"command": "split -l 150 /tmp/audit.txt /tmp/x_"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc_bash_cc_1", "content": "ok"},
            {"role": "user", "content": "now list them"},
        ]
        # Hermes-side tool list — ``terminal`` is the live hermes tool
        # whose canonical CC alias is ``Bash``.
        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=messages,
            tools=[
                {"type": "function", "function": {"name": "terminal", "description": "x"}},
            ],
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=True,
        )
        # The historical Bash tool_use must survive — no "tool no longer
        # available" breadcrumb should have replaced it.
        found_bash_tool_use = False
        breadcrumbs: list[str] = []
        for m in kwargs["messages"]:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Bash":
                    found_bash_tool_use = True
                if b.get("type") == "text":
                    txt = b.get("text", "")
                    if "tool no longer available" in txt:
                        breadcrumbs.append(txt)
        assert found_bash_tool_use, (
            "live Bash tool_use was incorrectly stripped on OAuth path; "
            f"breadcrumbs found: {breadcrumbs!r}"
        )
        assert not breadcrumbs, (
            f"no 'tool no longer available' breadcrumbs should be emitted for "
            f"tools that survive via CC aliasing; got: {breadcrumbs!r}"
        )

    def test_surviving_tool_result_leads_user_message_after_partial_strip(self):
        """When an assistant turn has tool_use blocks for a mix of live and
        stale tools, the live tool_use survives and the stale ones become
        text breadcrumbs.  The matching user message must still place the
        surviving tool_result BEFORE the breadcrumbs — otherwise Anthropic
        400s with `tool_use` ids were found without `tool_result` blocks
        immediately after.  Regression for the dump captured at
        request_dump_20260510_172029_36d2cf.json (msg[4] had a leading
        text breadcrumb ahead of a surviving tool_result)."""
        messages = [
            {"role": "user", "content": "do many things"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    # Two stale Bash calls bracket one live skill_view call.
                    {
                        "id": "tc_bash_1",
                        "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                    },
                    {
                        "id": "tc_skill_1",
                        "function": {"name": "skill_view", "arguments": '{"name": "x"}'},
                    },
                    {
                        "id": "tc_bash_2",
                        "function": {"name": "Bash", "arguments": '{"command": "pwd"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "tc_bash_1", "content": "out 1"},
            {"role": "tool", "tool_call_id": "tc_skill_1", "content": "skill output"},
            {"role": "tool", "tool_call_id": "tc_bash_2", "content": "out 2"},
            {"role": "user", "content": "continue"},
        ]
        # skill_view is the only live tool in this turn.
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages,
            tools=[
                {"type": "function", "function": {"name": "skill_view", "description": "x"}},
            ],
            max_tokens=4096,
            reasoning_config=None,
        )
        # Find the user message that holds tool_result blocks.
        target = None
        for m in kwargs["messages"]:
            content = m.get("content")
            if m.get("role") != "user" or not isinstance(content, list):
                continue
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                target = content
                break
        assert target is not None, "expected a user message carrying tool_result blocks"
        # The very first block must be the surviving tool_result — text
        # breadcrumbs for the stripped Bash results must come after.
        first = target[0]
        assert isinstance(first, dict) and first.get("type") == "tool_result", (
            f"first block must be tool_result, got: {first!r}"
        )
        assert first.get("tool_use_id") == "tc_skill_1"


# ---------------------------------------------------------------------------
# Model output limit lookup
# ---------------------------------------------------------------------------


class TestGetAnthropicMaxOutput:
    # 4.6+ models cap at 16K to mirror Claude Code's main chat path
    # (commit b8dea73373, 2026-05-05). The cap matters for billing/scheduling
    # signals on the API side; callers can override via max_tokens kwarg
    # when they actually need long outputs.
    def test_opus_4_6(self):
        from agent.anthropic_adapter import _get_anthropic_max_output
        assert _get_anthropic_max_output("claude-opus-4-6") == 16_000

    def test_opus_4_6_variant(self):
        from agent.anthropic_adapter import _get_anthropic_max_output
        assert _get_anthropic_max_output("claude-opus-4-6:1m:fast") == 16_000

    def test_sonnet_4_6(self):
        from agent.anthropic_adapter import _get_anthropic_max_output
        assert _get_anthropic_max_output("claude-sonnet-4-6") == 16_000







# ---------------------------------------------------------------------------
# _to_plain_data hardening
# ---------------------------------------------------------------------------


class TestToPlainData:




    def test_deep_nesting_is_capped(self):
        deep = "leaf"
        for _ in range(25):
            deep = {"nested": deep}
        result = _to_plain_data(deep)
        assert isinstance(result, dict)

    def test_plain_values_pass_through(self):
        assert _to_plain_data("hello") == "hello"
        assert _to_plain_data(42) == 42
        assert _to_plain_data(None) is None

    def test_object_with_dunder_dict(self):
        obj = SimpleNamespace(type="thinking", thinking="reason", signature="sig")
        result = _to_plain_data(obj)
        assert result == {"type": "thinking", "thinking": "reason", "signature": "sig"}


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------


class TestNormalizeResponse:
    def _make_response(self, content_blocks, stop_reason="end_turn"):
        resp = SimpleNamespace()
        resp.content = content_blocks
        resp.stop_reason = stop_reason
        resp.usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        return resp


    def test_tool_use_response(self):
        blocks = [
            SimpleNamespace(type="text", text="Searching..."),
            SimpleNamespace(
                type="tool_use",
                id="tc_1",
                name="search",
                input={"query": "test"},
            ),
        ]
        nr = get_transport("anthropic_messages").normalize_response(
            self._make_response(blocks, "tool_use")
        )
        assert nr.content == "Searching..."
        assert nr.finish_reason == "tool_calls"
        assert len(nr.tool_calls) == 1
        assert nr.tool_calls[0].name == "search"
        assert json.loads(nr.tool_calls[0].arguments) == {"query": "test"}

    def test_thinking_response(self):
        blocks = [
            SimpleNamespace(type="thinking", thinking="Let me reason about this..."),
            SimpleNamespace(type="text", text="The answer is 42."),
        ]
        nr = get_transport("anthropic_messages").normalize_response(self._make_response(blocks))
        assert nr.content == "The answer is 42."
        assert nr.reasoning == "Let me reason about this..."
        assert nr.provider_data["reasoning_details"] == [{"type": "thinking", "thinking": "Let me reason about this..."}]


    def test_captures_full_content_blocks_in_original_order(self):
        """Verbatim block array round-trips through provider_data so subsequent
        turns can replay it in original position. Required for
        interleaved-thinking-2025-05-14 + clear_thinking_20251015 strict
        validation — recomposing from reasoning_details + tool_calls would
        reorder thinking blocks among tool_uses and break signatures."""
        blocks = [
            SimpleNamespace(type="thinking", thinking="step 1", signature="sig_A"),
            SimpleNamespace(
                type="tool_use", id="tu_1", name="lookup", input={"q": "x"}
            ),
            SimpleNamespace(type="thinking", thinking="step 2", signature="sig_B"),
            SimpleNamespace(
                type="tool_use", id="tu_2", name="lookup", input={"q": "y"}
            ),
        ]
        nr = get_transport("anthropic_messages").normalize_response(
            self._make_response(blocks, "tool_use")
        )
        captured = nr.provider_data.get("anthropic_content_blocks")
        assert captured is not None, "anthropic_content_blocks must be populated"
        assert nr.anthropic_content_blocks is captured
        # Original ordering preserved (thinking interleaved with tool_use)
        assert [b["type"] for b in captured] == [
            "thinking",
            "tool_use",
            "thinking",
            "tool_use",
        ]
        # Signatures intact
        assert captured[0]["signature"] == "sig_A"
        assert captured[2]["signature"] == "sig_B"

    def test_stop_reason_mapping(self):
        block = SimpleNamespace(type="text", text="x")
        nr1 = get_transport("anthropic_messages").normalize_response(
            self._make_response([block], "end_turn")
        )
        nr2 = get_transport("anthropic_messages").normalize_response(
            self._make_response([block], "tool_use")
        )
        nr3 = get_transport("anthropic_messages").normalize_response(
            self._make_response([block], "max_tokens")
        )
        assert nr1.finish_reason == "stop"
        assert nr2.finish_reason == "tool_calls"
        assert nr3.finish_reason == "length"




# ---------------------------------------------------------------------------
# Role alternation
# ---------------------------------------------------------------------------


class TestRoleAlternation:
    def test_merges_consecutive_user_messages(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "World"},
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert "Hello" in result[0]["content"]
        assert "World" in result[0]["content"]

    def test_preserves_proper_alternation(self):
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"},
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert len(result) == 3
        assert [m["role"] for m in result] == ["user", "assistant", "user"]


# ---------------------------------------------------------------------------
# Thinking block signature management
# ---------------------------------------------------------------------------


class TestThinkingBlockSignatureManagement:
    """Tests for the thinking block handling strategy:
    strip from old turns, preserve latest signed, downgrade unsigned."""




    def test_redacted_thinking_with_data_preserved(self):
        """Redacted thinking with 'data' field is kept on last turn."""
        messages = [
            {
                "role": "assistant",
                "content": "Response.",
                "reasoning_details": [
                    {"type": "redacted_thinking", "data": "opaque_signature_data"},
                ],
            },
        ]
        _, result = convert_messages_to_anthropic(messages)
        blocks = next(m for m in result if m["role"] == "assistant")["content"]
        redacted = [b for b in blocks if b.get("type") == "redacted_thinking"]
        assert len(redacted) == 1
        assert redacted[0]["data"] == "opaque_signature_data"

    def test_redacted_thinking_without_data_dropped(self):
        """Redacted thinking without 'data' is dropped — can't be validated."""
        messages = [
            {
                "role": "assistant",
                "content": "Response.",
                "reasoning_details": [
                    {"type": "redacted_thinking"},
                    # No 'data' field
                ],
            },
        ]
        _, result = convert_messages_to_anthropic(messages)
        blocks = result[0]["content"]
        assert not any(b.get("type") == "redacted_thinking" for b in blocks)

    def test_cache_control_stripped_from_thinking_blocks(self):
        """cache_control markers are removed from thinking/redacted_thinking blocks."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc_1", "function": {"name": "t", "arguments": "{}"}},
                ],
                "reasoning_details": [
                    {
                        "type": "thinking",
                        "thinking": "Reasoning.",
                        "signature": "sig_1",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
        ]
        _, result = convert_messages_to_anthropic(messages)
        assistant = next(m for m in result if m["role"] == "assistant")
        for block in assistant["content"]:
            if block.get("type") in {"thinking", "redacted_thinking"}:
                assert "cache_control" not in block



    def test_multi_turn_conversation_preserves_only_last(self):
        """Full multi-turn conversation: only last assistant keeps thinking."""
        messages = [
            {"role": "user", "content": "Question 1"},
            {
                "role": "assistant",
                "content": "Answer 1",
                "reasoning_details": [
                    {"type": "thinking", "thinking": "Thought 1", "signature": "sig_1"},
                ],
            },
            {"role": "user", "content": "Question 2"},
            {
                "role": "assistant",
                "content": "Answer 2",
                "reasoning_details": [
                    {"type": "thinking", "thinking": "Thought 2", "signature": "sig_2"},
                ],
            },
            {"role": "user", "content": "Question 3"},
            {
                "role": "assistant",
                "content": "Answer 3",
                "reasoning_details": [
                    {"type": "thinking", "thinking": "Thought 3", "signature": "sig_3"},
                ],
            },
        ]
        _, result = convert_messages_to_anthropic(messages)

        assistants = [m for m in result if m["role"] == "assistant"]
        assert len(assistants) == 3

        # First two: no thinking blocks
        for a in assistants[:2]:
            assert not any(
                b.get("type") in {"thinking", "redacted_thinking"}
                for b in a["content"]
                if isinstance(b, dict)
            )

        # Last one: thinking preserved
        last_thinking = [
            b for b in assistants[2]["content"]
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        assert len(last_thinking) == 1
        assert last_thinking[0]["signature"] == "sig_3"




# ---------------------------------------------------------------------------
# Tool choice
# ---------------------------------------------------------------------------


class TestToolChoice:
    _DUMMY_TOOL = [
        {
            "type": "function",
            "function": {
                "name": "test",
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def test_auto_tool_choice(self):
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "Hi"}],
            tools=self._DUMMY_TOOL,
            max_tokens=4096,
            reasoning_config=None,
            tool_choice="auto",
        )
        # Anthropic treats absent tool_choice as "auto" — omit to match
        # Claude Code's wire shape (verified by mitmdump capture 2026-05-06).
        assert "tool_choice" not in kwargs


    def test_specific_tool_choice(self):
        kwargs = build_anthropic_kwargs(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "Hi"}],
            tools=self._DUMMY_TOOL,
            max_tokens=4096,
            reasoning_config=None,
            tool_choice="search",
        )
        assert kwargs["tool_choice"] == {"type": "tool", "name": "search"}



# ---------------------------------------------------------------------------
# max_tokens resolver — openclaw/openclaw#66664 port
# ---------------------------------------------------------------------------

from agent.anthropic_adapter import (
    _resolve_positive_anthropic_max_tokens,
    _resolve_anthropic_messages_max_tokens,
)


class TestResolvePositiveMaxTokens:
    """Unit tests for the positive-int resolver helper."""


    def test_zero_returns_none(self):
        assert _resolve_positive_anthropic_max_tokens(0) is None





    def test_nan_returns_none(self):
        assert _resolve_positive_anthropic_max_tokens(float("nan")) is None


    def test_bool_true_returns_none(self):
        # True is an int subclass but semantically never a real max_tokens value
        assert _resolve_positive_anthropic_max_tokens(True) is None
        assert _resolve_positive_anthropic_max_tokens(False) is None




class TestResolveMessagesMaxTokens:
    """Integration tests for the full Messages resolver."""

    def test_positive_requested_wins(self):
        assert _resolve_anthropic_messages_max_tokens(
            8192, "claude-opus-4-6"
        ) == 8192





    def test_sub_one_float_falls_back(self):
        # 0.5 floors to 0 -> not positive -> falls back to model ceiling
        result = _resolve_anthropic_messages_max_tokens(0.5, "claude-opus-4-6")
        assert result > 0
        assert result != 0


# ---------------------------------------------------------------------------
# convert_tools_to_anthropic — tool dedup at API boundary
# ---------------------------------------------------------------------------

class TestConvertToolsToAnthropicDedup:
    """convert_tools_to_anthropic must deduplicate tool names.

    Anthropic rejects requests with duplicate tool names.  This guard converts
    a hard failure into a warning log.  See:
    https://github.com/NousResearch/hermes-agent/issues/18478
    """

    def _make_openai_tool(self, name: str) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Tool {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }


    def test_duplicate_tool_names_are_deduplicated(self):
        """RED test — must fail until dedup guard is added."""
        tools = [
            self._make_openai_tool("lcm_grep"),
            self._make_openai_tool("lcm_describe"),
            self._make_openai_tool("lcm_grep"),  # duplicate
            self._make_openai_tool("lcm_expand"),
            self._make_openai_tool("lcm_describe"),  # duplicate
        ]
        result = convert_tools_to_anthropic(tools)
        names = [t["name"] for t in result]
        assert len(names) == len(set(names)), (
            f"Duplicate tool names found: {names}"
        )
        assert len(result) == 3  # lcm_grep, lcm_describe, lcm_expand


    def test_none_tools_returns_empty(self):
        assert convert_tools_to_anthropic(None) == []


# ---------------------------------------------------------------------------
# Round-trip regression: response → store → replay preserves block order
# ---------------------------------------------------------------------------
#
# Pre-2026-05-07 hermes recomposed assistant turns from
# reasoning_details + content + tool_calls in a fixed order
# [thinking..., server_tools, text, tool_use...].  When Anthropic returned
# blocks in a different order — typical under interleaved-thinking-2025-05-14
# with multi-step tool use, e.g. [thinking_A, tool_use_1, thinking_B, tool_use_2] —
# the rebuild collapsed them to [thinking_A, thinking_B, tool_use_1, tool_use_2].
#
# Until 2026-05-06 Anthropic accepted the reordered shape silently.  That
# day's wire-format change activated context_management.clear_thinking_20251015
# (keep:"all"), which validates each thinking block stays in its original
# position across turns.  The reorder started returning HTTP 400
# "thinking ... cannot be modified".
#
# This class wires together the full path that broke — transport →
# stored msg dict → rebuild — and asserts position is preserved end-to-end.
# It would have caught the bug before commit.


class TestThinkingBlockOrderRoundTrip:
    """The path from API response back to API request must preserve block
    position byte-identically.  Anthropic signs each thinking block against
    its position in the response; clear_thinking_20251015 enforces it."""

    def _make_response(self, content_blocks, stop_reason="tool_use"):
        resp = SimpleNamespace()
        resp.content = content_blocks
        resp.stop_reason = stop_reason
        resp.usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        return resp

    def _build_stored_assistant_msg(self, normalized):
        """Mirror what run_agent._build_assistant_message produces for the
        downstream adapter.  Captures the same fields the real builder
        attaches (content, reasoning, reasoning_content, reasoning_details,
        anthropic_content_blocks, tool_calls)."""
        msg = {
            "role": "assistant",
            "content": normalized.content or "",
            "finish_reason": normalized.finish_reason,
        }
        if normalized.reasoning:
            msg["reasoning"] = normalized.reasoning
            msg["reasoning_content"] = normalized.reasoning
        if normalized.reasoning_details:
            msg["reasoning_details"] = normalized.reasoning_details
        if normalized.anthropic_content_blocks:
            msg["anthropic_content_blocks"] = normalized.anthropic_content_blocks
        if normalized.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in normalized.tool_calls
            ]
        return msg

    def test_interleaved_thinking_position_preserved_through_round_trip(self):
        """Original: [thinking_A, tool_use_1, thinking_B, tool_use_2].
        After replay: same exact order.  The pre-fix recomposition path
        produced [thinking_A, thinking_B, tool_use_1, tool_use_2]."""
        original_response_blocks = [
            SimpleNamespace(
                type="thinking", thinking="plan: lookup x", signature="sig_A"
            ),
            SimpleNamespace(
                type="tool_use", id="tu_1", name="lookup", input={"q": "x"}
            ),
            SimpleNamespace(
                type="thinking", thinking="now lookup y", signature="sig_B"
            ),
            SimpleNamespace(
                type="tool_use", id="tu_2", name="lookup", input={"q": "y"}
            ),
        ]
        nr = get_transport("anthropic_messages").normalize_response(
            self._make_response(original_response_blocks)
        )

        stored = self._build_stored_assistant_msg(nr)
        # Conversation is: user → assistant (the turn we care about) →
        # tool results for both tool_uses.  This is the shape that
        # triggered the API rejection — a tool_use continuation re-sending
        # the assistant turn.
        api_messages = [
            {"role": "user", "content": "find x and y"},
            stored,
            {"role": "tool", "tool_call_id": "tu_1", "content": "x=1"},
            {"role": "tool", "tool_call_id": "tu_2", "content": "y=2"},
        ]

        _, converted = convert_messages_to_anthropic(api_messages)
        assistant_blocks = next(
            m for m in converted if m["role"] == "assistant"
        )["content"]

        # Assertion the pre-fix code would have failed: original interleaved
        # ordering preserved verbatim.
        assert [b["type"] for b in assistant_blocks] == [
            "thinking",
            "tool_use",
            "thinking",
            "tool_use",
        ]
        # Signatures still attached to the right blocks
        assert assistant_blocks[0]["signature"] == "sig_A"
        assert assistant_blocks[2]["signature"] == "sig_B"
        # Tool_use ids and inputs still associated with the right blocks
        assert assistant_blocks[1]["id"] == "tu_1"
        assert assistant_blocks[1]["input"] == {"q": "x"}
        assert assistant_blocks[3]["id"] == "tu_2"
        assert assistant_blocks[3]["input"] == {"q": "y"}

    def test_thinking_text_tool_use_position_preserved(self):
        """Three-part response [thinking, text, tool_use] — the common
        single-tool case.  Position must round-trip just like the
        interleaved case."""
        blocks = [
            SimpleNamespace(type="thinking", thinking="reasoning", signature="sig"),
            SimpleNamespace(type="text", text="Looking that up..."),
            SimpleNamespace(
                type="tool_use", id="tu_1", name="lookup", input={"q": "x"}
            ),
        ]
        nr = get_transport("anthropic_messages").normalize_response(
            self._make_response(blocks)
        )
        stored = self._build_stored_assistant_msg(nr)

        _, converted = convert_messages_to_anthropic(
            [
                {"role": "user", "content": "find x"},
                stored,
                {"role": "tool", "tool_call_id": "tu_1", "content": "x=1"},
            ]
        )
        assistant_blocks = next(
            m for m in converted if m["role"] == "assistant"
        )["content"]

        assert [b["type"] for b in assistant_blocks] == [
            "thinking",
            "text",
            "tool_use",
        ]
        assert assistant_blocks[0]["signature"] == "sig"
        assert assistant_blocks[1]["text"] == "Looking that up..."
        assert assistant_blocks[2]["id"] == "tu_1"

    def test_text_block_strips_parsed_output_on_replay(self):
        """Anthropic's response BetaTextBlock carries ``parsed_output``
        (structured output result) — a field the input validator rejects
        with HTTP 400 "Extra inputs are not permitted".  Replay must
        strip it.  Real failure: req_011CaoaYqmZD7qFyGjEtmR1E."""
        captured_blocks = [
            {
                "type": "text",
                "text": '{"answer": 42}',
                "parsed_output": {"answer": 42},  # response-only
                "citations": None,
            },
        ]
        stored = {
            "role": "assistant",
            "content": "",
            "anthropic_content_blocks": captured_blocks,
        }
        _, converted = convert_messages_to_anthropic(
            [{"role": "user", "content": "?"}, stored]
        )
        block = next(m for m in converted if m["role"] == "assistant")["content"][0]
        assert block["type"] == "text"
        assert block["text"] == '{"answer": 42}'
        assert "parsed_output" not in block

    def test_unknown_response_only_fields_stripped_per_block_type(self):
        """Defense in depth: every known block type drops fields that
        aren't in the input-allowed set, regardless of where they came
        from."""
        captured_blocks = [
            {
                "type": "thinking",
                "thinking": "...",
                "signature": "sig",
                "_internal_id": "should_not_round_trip",  # not in input allowlist
            },
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "lookup",
                "input": {"q": "x"},
                "stop_reason": "end_turn",  # response-only stop signal
            },
        ]
        stored = {
            "role": "assistant",
            "content": "",
            "anthropic_content_blocks": captured_blocks,
        }
        # Pair the tool_use with a tool_result so the orphan stripper
        # at line ~2180 doesn't drop it before we can inspect it.
        _, converted = convert_messages_to_anthropic(
            [
                {"role": "user", "content": "?"},
                stored,
                {"role": "tool", "tool_call_id": "tu_1", "content": "x=1"},
            ]
        )
        blocks = next(m for m in converted if m["role"] == "assistant")["content"]
        assert "_internal_id" not in blocks[0]
        assert blocks[0]["signature"] == "sig"
        assert "stop_reason" not in blocks[1]
        assert blocks[1]["id"] == "tu_1"
        assert blocks[1]["input"] == {"q": "x"}

    def test_redacted_thinking_block_position_preserved(self):
        """redact-thinking-2026-02-12 emits redacted_thinking blocks with
        a ``data`` field instead of plaintext thinking + signature.  These
        must also round-trip in original position."""
        blocks = [
            SimpleNamespace(
                type="redacted_thinking", data="encrypted_payload_A"
            ),
            SimpleNamespace(
                type="tool_use", id="tu_1", name="lookup", input={"q": "x"}
            ),
            SimpleNamespace(
                type="redacted_thinking", data="encrypted_payload_B"
            ),
            SimpleNamespace(type="text", text="result"),
        ]
        nr = get_transport("anthropic_messages").normalize_response(
            self._make_response(blocks)
        )
        stored = self._build_stored_assistant_msg(nr)

        _, converted = convert_messages_to_anthropic(
            [
                {"role": "user", "content": "go"},
                stored,
                {"role": "tool", "tool_call_id": "tu_1", "content": "x=1"},
            ]
        )
        assistant_blocks = next(
            m for m in converted if m["role"] == "assistant"
        )["content"]

        assert [b["type"] for b in assistant_blocks] == [
            "redacted_thinking",
            "tool_use",
            "redacted_thinking",
            "text",
        ]
        assert assistant_blocks[0]["data"] == "encrypted_payload_A"
        assert assistant_blocks[2]["data"] == "encrypted_payload_B"


# ---------------------------------------------------------------------------
# FORK regression: sticky tool_search activation must prevent
# _strip_unknown_tool_blocks from firing for the bridge tool names across
# consecutive turns of the same conversation. See tools/tool_search.py
# assemble_tool_defs docstring + tests/tools/test_tool_search.py
# TestStickyActivation for the full bug writeup.
# ---------------------------------------------------------------------------


class TestStickyActivationPreservesToolHistory:
    def _turn_messages(self, tool_name: str, turn: int) -> list:
        """A minimal assistant tool_use + tool_result pair, as they'd appear
        in stored conversation history after turn ``turn``."""
        return [
            {"role": "user", "content": f"do thing {turn}"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"tu_{turn}",
                        "name": tool_name,
                        "input": {"query": "x"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"tu_{turn}",
                        "content": "ok",
                    }
                ],
            },
        ]

    def test_bridge_tool_history_survives_when_sticky_keeps_bridge_present(self):
        """Simulate 2 turns where turn 1 uses ``tool_search`` while bridge
        tools are active, and turn 2's live tools array (post sticky-fix)
        still includes the bridge names even though the underlying
        deferrable total dropped under threshold. Across both turns,
        _strip_unknown_tool_blocks must never rewrite the tool_search
        tool_use/tool_result into breadcrumbs, because the sticky flag kept
        it in ``available_tool_names``.
        """
        from agent.anthropic_adapter import _strip_unknown_tool_blocks
        from tools.tool_search import BRIDGE_TOOL_NAMES

        history = []
        history += self._turn_messages("tool_search", 1)

        # Turn 1: bridge tools were active (sticky not yet needed).
        available_turn1 = {"terminal", "read_file"} | BRIDGE_TOOL_NAMES
        result_turn1 = _strip_unknown_tool_blocks(history, available_turn1)
        assistant_blocks_1 = next(
            m for m in result_turn1 if m.get("role") == "assistant"
        )["content"]
        assert assistant_blocks_1[0]["type"] == "tool_use"
        assert assistant_blocks_1[0]["name"] == "tool_search"

        # Turn 2: caller's assembled tools array — WITH the sticky fix,
        # bridge names remain present (sticky_active=True held activation
        # open) even though the live deferrable total shrank under
        # threshold. History from turn 1 plus a new tool_search call.
        history += self._turn_messages("tool_search", 2)
        available_turn2 = {"terminal", "read_file"} | BRIDGE_TOOL_NAMES
        result_turn2 = _strip_unknown_tool_blocks(history, available_turn2)

        assistant_msgs = [m for m in result_turn2 if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 2
        for msg in assistant_msgs:
            block = msg["content"][0]
            assert block["type"] == "tool_use", (
                "sticky activation should have kept tool_search in "
                "available_tool_names, so its history must NOT be rewritten "
                "into a breadcrumb"
            )
            assert block["name"] == "tool_search"

        # No breadcrumb text should appear anywhere.
        for msg in result_turn2:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        assert "no longer available in this turn" not in block.get("text", "")

    def test_without_sticky_bridge_tool_history_gets_rewritten(self):
        """Baseline/contrast: WITHOUT the sticky fix (bridge names absent
        from turn 2's tools array because the live total dropped under
        threshold), _strip_unknown_tool_blocks correctly (and, pre-fix,
        harmfully) rewrites the turn-1 tool_search call into a breadcrumb —
        this is the exact corruption the sticky fix prevents.
        """
        from agent.anthropic_adapter import _strip_unknown_tool_blocks

        history = self._turn_messages("tool_search", 1)

        # Turn 2: bridge tools vanished from the wire array (the flap bug).
        available_turn2 = {"terminal", "read_file"}
        result = _strip_unknown_tool_blocks(history, available_turn2)

        assistant_block = next(
            m for m in result if m.get("role") == "assistant"
        )["content"][0]
        assert assistant_block["type"] == "text"
        assert "tool_search" in assistant_block["text"]
        assert "no longer available in this turn" in assistant_block["text"]


class TestCreateAnthropicMessageBetaRouting:
    """Regression: beta-only kwargs must route through .beta.messages when
    the client supports it, and be stripped when it doesn't.

    The fork's Claude-Code-mimicry path attaches typed body kwargs
    (context_management, output_config, speed, betas) that only exist on
    client.beta.messages.* (Anthropic SDK 0.100+). The plain .messages.*
    namespace rejects them with TypeError.
    """

    BETA_KWARGS = {"context_management", "output_config", "speed", "betas"}

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _make_client(*, has_beta_messages: bool):
        """Build a mock Anthropic client with or without .beta.messages."""
        class _Messages:
            def stream(self, **kwargs):
                return _StreamContextManager(kwargs)
            def create(self, **kwargs):
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="ok")],
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                )

        class _BetaMessages:
            def stream(self, **kwargs):
                return _StreamContextManager(kwargs)
            def create(self, **kwargs):
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="ok")],
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                )

        if has_beta_messages:
            return SimpleNamespace(
                messages=_Messages(),
                beta=SimpleNamespace(messages=_BetaMessages()),
            )
        return SimpleNamespace(messages=_Messages())

    # ── create_anthropic_message tests ─────────────────────────────────

    def test_create_with_beta_messages_routes_through_beta(self):
        """When client has .beta.messages, stream() is called on .beta.messages."""
        from agent.anthropic_adapter import create_anthropic_message

        client = self._make_client(has_beta_messages=True)
        kwargs = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        for k in self.BETA_KWARGS:
            kwargs[k] = "dummy"

        # Monkey-patch .beta.messages.stream to capture the call
        captured = {}
        original_stream = client.beta.messages.stream
        def _capture_stream(**kw):
            captured["namespace"] = "beta"
            captured["kwargs"] = kw
            return _StreamContextManager(kw)
        client.beta.messages.stream = _capture_stream

        create_anthropic_message(client, kwargs)

        assert captured.get("namespace") == "beta", \
            "Should route through .beta.messages when available"
        for k in self.BETA_KWARGS:
            assert k in captured.get("kwargs", {}), \
                f"Beta-only kwarg '{k}' should be preserved on .beta.messages"

    def test_create_without_beta_messages_strips_beta_kwargs(self):
        """When client lacks .beta.messages, beta-only kwargs are stripped."""
        from agent.anthropic_adapter import create_anthropic_message

        client = self._make_client(has_beta_messages=False)
        kwargs = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        for k in self.BETA_KWARGS:
            kwargs[k] = "dummy"

        captured = {}
        original_stream = client.messages.stream
        def _capture_stream(**kw):
            captured["kwargs"] = kw
            return _StreamContextManager(kw)
        client.messages.stream = _capture_stream

        create_anthropic_message(client, kwargs)

        for k in self.BETA_KWARGS:
            assert k not in captured.get("kwargs", {}), \
                f"Beta-only kwarg '{k}' should be stripped on .messages"

    def test_create_without_beta_messages_preserves_normal_kwargs(self):
        """Non-beta kwargs pass through unchanged when .beta.messages is absent."""
        from agent.anthropic_adapter import create_anthropic_message

        client = self._make_client(has_beta_messages=False)
        kwargs = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        captured = {}
        def _capture_stream(**kw):
            captured["kwargs"] = kw
            return _StreamContextManager(kw)
        client.messages.stream = _capture_stream

        create_anthropic_message(client, kwargs)

        assert captured["kwargs"].get("model") == "claude-sonnet-5"
        assert captured["kwargs"].get("max_tokens") == 1000
        assert captured["kwargs"].get("temperature") == 0.7


class _StreamContextManager:
    """Mimics Anthropic SDK's stream context manager."""
    def __init__(self, kwargs):
        self._kwargs = kwargs
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )
class TestBlankTextBlockFiltering:
    """Regression tests for blank text block filtering in _convert_assistant_message.

    Bedrock and strict Anthropic-compatible endpoints reject text blocks where
    "text" is empty or whitespace-only with HTTP 400. Both the normal list-
    content path and the ordered-replay fast path must drop such blocks while
    preserving tool_use and other block types, and must relocate (not lose)
    any cache_control marker attached to the dropped block.
    """

    def _convert(self, message):
        from agent.anthropic_adapter import _convert_assistant_message
        return _convert_assistant_message(message)



    def test_normal_path_filters_none_text_block_without_crashing(self):
        """Regression (review of #63228): text=None must not raise
        AttributeError. _convert_content_part_to_anthropic() can preserve
        None from an invalid upstream input text block -- a bare .strip()
        on blk.get("text", "") crashes because .get() only substitutes the
        default when the key is ABSENT, not when it's present with value None."""
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": None},
                {"type": "tool_use", "id": "call_none", "name": "web_search",
                 "input": {"query": "test"}},
            ],
        }
        result = self._convert(msg)  # must not raise
        blocks = result["content"]
        text_blocks = [b for b in blocks if b.get("type") == "text"]
        tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        assert len(text_blocks) == 0, f"None text block not filtered: {text_blocks}"
        assert len(tool_blocks) == 1



    def test_normal_path_relocates_cache_control_from_dropped_block(self):
        """Regression (review of #63228): prompt_caching.py's _apply_cache_marker
        sets cache_control directly on content[-1] for list content. If that
        last part is blank text, dropping it must relocate the marker to the
        surviving last cacheable block (here: the tool_use), not lose it."""
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll look that up."},
                {"type": "tool_use", "id": "call_cache", "name": "web_search",
                 "input": {"query": "test"}},
                {"type": "text", "text": "", "cache_control": {"type": "ephemeral"}},
            ],
        }
        result = self._convert(msg)
        blocks = result["content"]
        assert not any(b.get("type") == "text" and not b.get("text", "").strip() for b in blocks), (
            "Blank text block must be dropped"
        )
        cacheable_with_marker = [b for b in blocks if isinstance(b.get("cache_control"), dict)]
        assert len(cacheable_with_marker) == 1, (
            f"cache_control marker must survive on exactly one surviving block: {blocks}"
        )
        assert cacheable_with_marker[0]["type"] == "tool_use", (
            f"Marker must relocate to the new last cacheable block: {blocks}"
        )


    def test_replay_path_relocates_cache_control_from_dropped_block(self):
        """Same cache_control-relocation guarantee on the ordered-replay path:
        a blank text block carrying cache_control (e.g. a stored, previously
        cache-marked turn where prompt_caching later becomes blank on replay)
        must not silently lose the breakpoint when dropped."""
        from agent.anthropic_adapter import _convert_assistant_message
        msg = {
            "role": "assistant",
            "content": "",
            "anthropic_content_blocks": [
                {"type": "tool_use", "id": "call_5", "name": "web_search",
                 "input": {"query": "test"}},
                {"type": "text", "text": "  ", "cache_control": {"type": "ephemeral"}},
            ],
            "tool_calls": [
                {
                    "id": "call_5",
                    "function": {"name": "web_search",
                                 "arguments": '{"query": "test"}'},
                }
            ],
        }
        result = _convert_assistant_message(msg)
        blocks = result["content"]
        assert not any(b.get("type") == "text" for b in blocks), "Blank replay text must be dropped"
        cacheable_with_marker = [b for b in blocks if isinstance(b.get("cache_control"), dict)]
        assert len(cacheable_with_marker) == 1
        assert cacheable_with_marker[0]["type"] == "tool_use"


class TestAllBlankFallbackAndNonStringText:
    """Regression tests for the two bugs found in independent review of
    #68633 (GPT-5.6-sol-xhigh in Codex, egilewski):

    1. `effective = blocks or content` fell back to the RAW, unfiltered
       `content` when every block was filtered out as blank -- restoring
       exactly the invalid (blank/whitespace) payload the filter exists to
       remove, for any message where blank content is the ONLY content
       (no surviving tool_use/text/thinking block).
    2. The normal-path blank-text check used `(blk.get("text") or "").strip()`,
       which is not type-safe for a truthy NON-string, non-None text value
       (e.g. an int) -- `or` doesn't substitute for a truthy value, so
       `(7 or "").strip()` still raises AttributeError.
    """

    def _convert(self, message):
        from agent.anthropic_adapter import _convert_assistant_message
        return _convert_assistant_message(message)



    def test_sole_cache_marked_blank_block_relocates_marker_to_placeholder(self):
        """A message whose ONLY content is a blank text block that also
        carries cache_control: the marker must not be silently dropped just
        because there's nothing else to relocate it onto -- it must land on
        the (empty) placeholder that replaces the dropped block."""
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "", "cache_control": {"type": "ephemeral"}}],
        }
        result = self._convert(msg)
        blocks = result["content"]
        assert blocks == [
            {"type": "text", "text": "(empty)", "cache_control": {"type": "ephemeral"}}
        ], f"cache_control must relocate onto the (empty) placeholder: {blocks}"


    def test_non_string_truthy_text_treated_as_invalid_not_crash(self):
        """Regression: text=7 (a truthy int, not None) must not reach
        .strip() and raise AttributeError -- it must be treated the same as
        blank/invalid text and dropped."""
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": 7},
                {"type": "tool_use", "id": "call_int", "name": "web_search",
                 "input": {"query": "test"}},
            ],
        }
        result = self._convert(msg)  # must not raise
        blocks = result["content"]
        text_blocks = [b for b in blocks if b.get("type") == "text"]
        tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        assert len(text_blocks) == 0, f"Non-string text value must be dropped, not kept: {text_blocks}"
        assert len(tool_blocks) == 1


    def test_dict_valued_text_treated_as_invalid_not_crash(self):
        """Another truthy non-string shape (dict) must also be safely dropped."""
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": {"nested": "garbage"}}],
            "tool_calls": [
                {"id": "call_d", "function": {"name": "web_search",
                                               "arguments": '{"query": "test"}'}},
            ],
        }
        result = self._convert(msg)  # must not raise
        blocks = result["content"]
        assert not any(b.get("type") == "text" for b in blocks)


class TestReplayAllBlankFallback:
    """Regression for the final open review point on #68633 (egilewski):

    ``_relocated_replay_cache_control`` was applied only inside ``if
    replayed:``. For ``anthropic_content_blocks`` containing only a blank
    cache-marked text block, ``replayed`` became empty, the function fell
    through to the main path's ``(empty)`` fallback, and the marker was
    lost. A signed-thinking block plus the blank marked text also returned
    without any relocated marker (thinking is not a cacheable carrier).
    The replay branch now resolves a cacheable ``(empty)`` placeholder when
    no cacheable block survives the blank filter.
    """

    def _convert(self, message):
        from agent.anthropic_adapter import _convert_assistant_message
        return _convert_assistant_message(message)

    def test_sole_blank_marked_replay_block_keeps_marker_on_placeholder(self):
        msg = {
            "role": "assistant",
            "content": "",
            "anthropic_content_blocks": [
                {"type": "text", "text": " ", "cache_control": {"type": "ephemeral"}},
            ],
        }
        result = self._convert(msg)
        assert result["content"] == [
            {"type": "text", "text": "(empty)", "cache_control": {"type": "ephemeral"}}
        ], result["content"]

    def test_thinking_plus_blank_marked_text_keeps_thinking_and_marker(self):
        msg = {
            "role": "assistant",
            "content": "",
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": "reasoning", "signature": "sig-A"},
                {"type": "text", "text": "  ", "cache_control": {"type": "ephemeral"}},
            ],
        }
        result = self._convert(msg)
        blocks = result["content"]
        assert blocks[0] == {"type": "thinking", "thinking": "reasoning", "signature": "sig-A"}
        marked = [b for b in blocks if isinstance(b.get("cache_control"), dict)]
        assert len(marked) == 1 and marked[0]["type"] == "text"
        assert marked[0]["text"].strip(), "placeholder must be non-whitespace"

    def test_thinking_plus_blank_unmarked_text_gets_schema_valid_placeholder(self):
        """Even without a cache marker, dropping the only text block from a
        thinking-only replay must leave schema-valid content."""
        msg = {
            "role": "assistant",
            "content": "",
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": "reasoning", "signature": "sig-B"},
                {"type": "text", "text": "\n"},
            ],
        }
        result = self._convert(msg)
        texts = [b for b in result["content"] if b.get("type") == "text"]
        assert texts == [{"type": "text", "text": "(empty)"}]


def _find_blank_text_blocks(messages):
    """Recursively scan a converted Anthropic message list (including
    nested tool_result content) for any text block whose text is empty or
    whitespace-only. Returns a list of (message_index, role, location,
    block_index) tuples for every violation found -- empty means the
    payload is safe to send to Anthropic."""
    violations = []
    for m_idx, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b_idx, blk in enumerate(content):
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text" and not (
                isinstance(blk.get("text"), str) and blk["text"].strip()
            ):
                violations.append((m_idx, msg.get("role"), "content", b_idx))
            if blk.get("type") == "tool_result" and isinstance(blk.get("content"), list):
                for ib_idx, iblk in enumerate(blk["content"]):
                    if (
                        isinstance(iblk, dict)
                        and iblk.get("type") == "text"
                        and not (isinstance(iblk.get("text"), str) and iblk["text"].strip())
                    ):
                        violations.append((m_idx, msg.get("role"), "tool_result", ib_idx))
    return violations


class TestFinalPayloadHasNoBlankTextBlocks:
    """End-to-end regression tests on the true final payload boundary:
    ``convert_messages_to_anthropic`` -- the last transform before
    ``build_anthropic_kwargs`` hands ``messages`` to the Anthropic SDK.

    Covers the blank-content shapes enumerated for the "text content
    blocks must contain non-whitespace text" HTTP 400 class, verifying the
    final built payload never contains a blank text block while tool_use,
    tool_result, and image content are preserved.
    """

    def test_user_message_empty_string_content(self):
        messages = [{"role": "user", "content": ""}]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        assert result[0]["content"] == "(empty message)"

    def test_user_message_whitespace_only_string_content(self):
        messages = [{"role": "user", "content": "   "}]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        assert result[0]["content"] == "(empty message)"

    def test_user_message_blank_list_content(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        assert result[0]["content"] == [{"type": "text", "text": "(empty message)"}]

    def test_user_message_mixed_blank_and_valid_text_blocks(self):
        """A blank text block sitting alongside a non-blank one must be
        dropped individually -- not left in place (the all-or-nothing bug)
        and not used as an excuse to nuke the valid sibling block."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "real question"},
                    {"type": "text", "text": "   "},
                ],
            }
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        assert result[0]["content"] == [{"type": "text", "text": "real question"}]

    def test_mixed_blank_text_plus_valid_tool_block_preserved(self):
        """Blank text next to a valid non-text block (tool_result) must
        drop only the blank text and keep the tool block intact."""
        messages = [
            {"role": "user", "content": "call a tool"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "web_search", "arguments": '{"query": "x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result text"},
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        assistant_msg = next(m for m in result if m["role"] == "assistant")
        tool_use_blocks = [b for b in assistant_msg["content"] if b.get("type") == "tool_use"]
        assert len(tool_use_blocks) == 1
        tool_result_msg = next(
            m
            for m in result
            if m["role"] == "user"
            and isinstance(m["content"], list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        )
        assert tool_result_msg is not None

    def test_assistant_tool_call_message_with_blank_content(self):
        """OpenAI-wire-shaped assistant turn: content is a blank string,
        tool_calls carries the real payload. Must not surface a blank text
        block, and the tool_use block must survive untouched."""
        messages = [
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": "   ",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "function": {"name": "web_search", "arguments": '{"query": "y"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        assistant_msg = next(m for m in result if m["role"] == "assistant")
        assert assistant_msg["content"] == [
            {"type": "tool_use", "id": "call_2", "name": "web_search", "input": {"query": "y"}}
        ]

    def test_leading_synthesized_user_turn_is_non_blank(self):
        """_ensure_leading_user_turn's synthesized filler must itself be
        non-whitespace -- regression for the literal " " placeholder bug."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "[Context compaction summary] earlier work"},
            {"role": "user", "content": "continue"},
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        assert result[0]["content"] == [{"type": "text", "text": "(empty)"}]

    def test_blank_text_nested_in_tool_result_content_is_dropped(self):
        """A blank text part nested inside a tool_result's own multimodal
        content list (e.g. alongside an image) must be scrubbed without
        losing the image."""
        messages = [
            {"role": "user", "content": "screenshot please"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_3",
                        "function": {"name": "screenshot", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_3",
                "content": [
                    {"type": "text", "text": "   "},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            },
        ]
        _, result = convert_messages_to_anthropic(messages)
        assert _find_blank_text_blocks(result) == []
        tool_result_msg = next(
            m
            for m in result
            if m["role"] == "user"
            and isinstance(m["content"], list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        )
        tool_result_block = next(
            b for b in tool_result_msg["content"] if b.get("type") == "tool_result"
        )
        image_blocks = [b for b in tool_result_block["content"] if b.get("type") == "image"]
        assert len(image_blocks) == 1
