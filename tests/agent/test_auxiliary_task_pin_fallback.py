"""Tests for the 3-tier auxiliary.<provider>.<task> fallback feature.

Design (see the module docstring in agent/auxiliary_client.py, "Per-task
pinned fallback (3-tier resolution)"):

  TIER 1 — the existing explicit ``auxiliary.<provider>.<task>`` pin
    (e.g. ``{model: gemma4:31b, provider: ollama-cloud}``). Unchanged
    behavior when it works.
  TIER 2 — a NEW ``fallback: {model, provider, ...}`` sub-key on that same
    pin, engaged only when tier 1 fails with a RETRYABLE error (rate limit,
    quota, timeout, connection — the same classifiers the pre-existing
    ``fallback_chain`` mechanism already uses). Implemented as a one-entry
    shorthand for ``fallback_chain`` (see
    ``_apply_singular_aux_fallback_shorthand``), so it reuses that
    mechanism's chain-walk, credential isolation, and logging verbatim.
  TIER 3 — the PRE-EXISTING auto-resolution safety net
    (``_try_main_agent_model_fallback`` for an explicit-provider pin,
    which is Step 1 of ``_resolve_auto()``: the user's live main
    provider + model). No new code — this is what already ran after a
    configured fallback_chain was exhausted before this feature shipped.

Behavior contracts covered:
  (a) tier1 success never touches tier2/tier3
  (b) tier1 retryable failure engages tier2 with ITS OWN isolated creds
  (c) tier1 non-retryable failure does NOT engage tier2 (loud fail)
  (d) tier1+tier2 both retryable-failing falls through to tier3
  (e) tier3 itself failing is a normal terminal failure (no new raise)
  (f) no ``fallback`` key => byte-identical to pre-feature behavior
  (g) a bare ``"auto"`` string entry is unaffected
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent import auxiliary_client as ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyResponse:
    def __init__(self, text: str = "ok"):
        self.choices = [MagicMock(message=MagicMock(content=text))]


def _rate_limit_error(msg: str = "Rate limit exceeded, try again in 60 seconds"):
    err = Exception(msg)
    err.status_code = 429
    return err


def _bad_request_error(msg: str = "Invalid request: malformed schema"):
    """A representative NON-retryable error (400, no rate-limit/payment/
    connection/auth signature) — should never trigger tier 2/3 escalation."""
    err = Exception(msg)
    err.status_code = 400
    return err


# ---------------------------------------------------------------------------
# Unit level: _apply_singular_aux_fallback_shorthand
# ---------------------------------------------------------------------------


class TestSingularFallbackShorthandNormalization:
    def test_shorthand_normalizes_to_one_entry_chain(self):
        cfg = {
            "provider": "ollama-cloud",
            "model": "gemma4:31b",
            "fallback": {"model": "claude-haiku-4-5-20251001", "provider": "anthropic"},
        }
        out = ac._apply_singular_aux_fallback_shorthand(cfg)
        assert out["fallback_chain"] == [
            {
                "model": "claude-haiku-4-5-20251001",
                "provider": "anthropic",
                "_aux_fallback_shorthand": True,
            }
        ]
        # Original dict must not be mutated in place.
        assert "fallback_chain" not in cfg

    def test_explicit_fallback_chain_wins_over_shorthand(self):
        """(more specific config wins) — a task with BOTH keys ignores the
        singular shorthand entirely; the existing list form is untouched."""
        existing_chain = [{"provider": "openrouter", "model": "some/model"}]
        cfg = {
            "provider": "ollama-cloud",
            "model": "gemma4:31b",
            "fallback_chain": existing_chain,
            "fallback": {"model": "claude-haiku-4-5-20251001", "provider": "anthropic"},
        }
        out = ac._apply_singular_aux_fallback_shorthand(cfg)
        assert out["fallback_chain"] == existing_chain
        assert out is cfg  # no-op path returns the same object

    def test_no_fallback_key_is_a_no_op(self):
        """(f) No fallback sub-key => byte-identical to pre-feature behavior."""
        cfg = {"provider": "ollama-cloud", "model": "gemma4:31b"}
        out = ac._apply_singular_aux_fallback_shorthand(cfg)
        assert out == {"provider": "ollama-cloud", "model": "gemma4:31b"}
        assert "fallback_chain" not in out

    def test_fallback_with_no_model_is_dropped(self):
        cfg = {
            "provider": "ollama-cloud",
            "model": "gemma4:31b",
            "fallback": {"provider": "anthropic"},  # no model — unusable
        }
        out = ac._apply_singular_aux_fallback_shorthand(cfg)
        assert "fallback_chain" not in out

    def test_bare_auto_string_entry_survives_unaffected(self):
        """(g) A bare 'auto' string task-config is not a dict at all by the
        time it reaches this helper in real usage (task-first schema stores
        the raw value), but _get_auxiliary_task_config always coerces
        non-dict entries to {} before calling this helper — verify that
        empty-dict path is untouched."""
        out = ac._apply_singular_aux_fallback_shorthand({})
        assert out == {}


class TestGetAuxiliaryTaskConfigAppliesShorthand:
    """E2E through _get_auxiliary_task_config with a real temp HERMES_HOME
    config.yaml — task-first schema (the shape the user's 17 tasks use)."""

    @pytest.fixture
    def hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        try:
            from hermes_cli import config as _cfg
            if hasattr(_cfg, "_LOAD_CONFIG_CACHE"):
                _cfg._LOAD_CONFIG_CACHE.clear()
        except Exception:
            pass
        yield tmp_path

    def test_task_first_pin_with_fallback_reaches_flat_config(self, hermes_home):
        (hermes_home / "config.yaml").write_text(
            "model:\n  provider: anthropic\n  model: claude-opus-4-8\n"
            "auxiliary:\n"
            "  vision:\n"
            "    provider: ollama-cloud\n"
            "    model: gemma4:31b\n"
            "    fallback:\n"
            "      model: claude-haiku-4-5-20251001\n"
            "      provider: anthropic\n"
        )
        cfg = ac._get_auxiliary_task_config("vision")
        assert cfg["provider"] == "ollama-cloud"
        assert cfg["model"] == "gemma4:31b"
        assert cfg["fallback_chain"] == [
            {
                "model": "claude-haiku-4-5-20251001",
                "provider": "anthropic",
                "_aux_fallback_shorthand": True,
            }
        ]

    def test_task_first_pin_without_fallback_is_unaffected(self, hermes_home):
        """(f) byte-identical to pre-feature behavior with no fallback key."""
        (hermes_home / "config.yaml").write_text(
            "model:\n  provider: anthropic\n  model: claude-opus-4-8\n"
            "auxiliary:\n"
            "  vision:\n"
            "    provider: ollama-cloud\n"
            "    model: gemma4:31b\n"
        )
        cfg = ac._get_auxiliary_task_config("vision")
        assert cfg["provider"] == "ollama-cloud"
        assert cfg["model"] == "gemma4:31b"
        assert "fallback_chain" not in cfg
        assert "fallback" not in cfg

    def test_bare_auto_string_task_is_unaffected(self, hermes_home):
        """(g) A bare 'auto' string entry (or absent) resolves through the
        pre-existing path with no fallback machinery touched."""
        (hermes_home / "config.yaml").write_text(
            "model:\n  provider: anthropic\n  model: claude-opus-4-8\n"
            "auxiliary:\n"
            "  vision: auto\n"
        )
        cfg = ac._get_auxiliary_task_config("vision")
        assert "fallback_chain" not in cfg
        assert "fallback" not in cfg


# ---------------------------------------------------------------------------
# Behavior contracts (a)-(e): the call_llm() runtime path
# ---------------------------------------------------------------------------


class TestThreeTierRuntimeActivation:
    """Exercises the real call_llm() retry/fallback ladder, mocking only the
    provider-client construction boundary (resolve_provider_client /
    _get_cached_client) — never the classifiers or the chain-walk."""

    def _task_config(self, with_fallback=True):
        cfg = {"provider": "ollama-cloud", "model": "gemma4:31b"}
        if with_fallback:
            cfg["fallback"] = {
                "model": "claude-haiku-4-5-20251001",
                "provider": "anthropic",
            }
        return ac._apply_singular_aux_fallback_shorthand(cfg)

    def test_a_tier1_success_never_touches_tier2_or_tier3(self, monkeypatch):
        primary_client = MagicMock()
        primary_client.chat.completions.create.return_value = _DummyResponse("tier1 ok")

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=self._task_config()), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._try_main_agent_model_fallback") as tier3_spy, \
             patch("agent.auxiliary_client.resolve_provider_client") as tier2_resolve_spy:
            result = ac.call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "describe this"}],
            )

        assert result.choices[0].message.content == "tier1 ok"
        tier3_spy.assert_not_called()
        tier2_resolve_spy.assert_not_called()

    def test_b_tier1_retryable_failure_engages_tier2_with_isolated_creds(self, monkeypatch):
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _rate_limit_error()

        tier2_client = MagicMock()
        tier2_client.base_url = "https://api.anthropic.com"
        tier2_client.chat.completions.create.return_value = _DummyResponse("tier2 ok")

        captured_resolve_args = []

        def _fake_resolve(entry):
            captured_resolve_args.append(entry)
            return tier2_client, entry.get("model")

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=self._task_config()), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry",
                   side_effect=lambda entry: _fake_resolve(entry)), \
             patch("agent.auxiliary_client._try_main_agent_model_fallback") as tier3_spy:
            result = ac.call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "describe this"}],
            )

        assert result.choices[0].message.content == "tier2 ok"
        # Tier 2 was resolved from its OWN entry — never merged with the
        # primary's ollama-cloud/gemma4:31b fields.
        assert len(captured_resolve_args) == 1
        entry = captured_resolve_args[0]
        assert entry["provider"] == "anthropic"
        assert entry["model"] == "claude-haiku-4-5-20251001"
        assert "ollama-cloud" not in str(entry.values())
        assert "gemma4:31b" not in str(entry.values())
        # Tier 2 succeeded — tier 3 must never be consulted.
        tier3_spy.assert_not_called()

    def test_c_tier1_non_retryable_failure_does_not_engage_tier2(self, monkeypatch):
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _bad_request_error()

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=self._task_config()), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry") as tier2_spy, \
             patch("agent.auxiliary_client._try_main_agent_model_fallback") as tier3_spy:
            with pytest.raises(Exception, match="malformed schema"):
                ac.call_llm(
                    task="title_generation",
                    messages=[{"role": "user", "content": "describe this"}],
                )

        tier2_spy.assert_not_called()
        tier3_spy.assert_not_called()

    def test_d_tier1_and_tier2_both_retryable_falls_through_to_tier3(self, monkeypatch):
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _rate_limit_error()

        tier2_client = MagicMock()
        tier2_client.base_url = "https://api.anthropic.com"
        tier2_client.chat.completions.create.side_effect = _rate_limit_error(
            "Rate limit exceeded on fallback too"
        )

        tier3_client = MagicMock()
        tier3_client.chat.completions.create.return_value = _DummyResponse("tier3 ok (main model)")

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=self._task_config()), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry",
                   return_value=(tier2_client, "claude-haiku-4-5-20251001")), \
             patch("agent.auxiliary_client._try_main_agent_model_fallback",
                   return_value=(tier3_client, "claude-opus-4-8", "main-agent(anthropic)")) as tier3_spy:
            result = ac.call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "describe this"}],
            )

        assert result.choices[0].message.content == "tier3 ok (main model)"
        tier3_spy.assert_called_once()

    def test_e_tier3_itself_failing_is_a_normal_terminal_failure(self, monkeypatch):
        """No new raise mechanism — tier3 exhaustion re-raises the ORIGINAL
        (tier1) error, exactly like the pre-existing fallback_chain ladder
        already does when every layer is exhausted."""
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _rate_limit_error(
            "Rate limit exceeded on primary"
        )

        tier2_client = MagicMock()
        tier2_client.base_url = "https://api.anthropic.com"
        tier2_client.chat.completions.create.side_effect = _rate_limit_error(
            "Rate limit exceeded on fallback"
        )

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=self._task_config()), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry",
                   return_value=(tier2_client, "claude-haiku-4-5-20251001")), \
             patch("agent.auxiliary_client._try_main_agent_model_fallback",
                   return_value=(None, None, "")):
            with pytest.raises(Exception, match="Rate limit exceeded on primary"):
                ac.call_llm(
                    task="title_generation",
                    messages=[{"role": "user", "content": "describe this"}],
                )

    def test_e2_tier3_own_exception_surfaces_when_tier3_has_a_client(self, monkeypatch):
        """(e) variant: when tier 3 DOES resolve a client but ITS call also
        raises, that raised exception (tier3's own) is what propagates —
        not a synthesized error, and no infinite chain (only one hop past
        tier 2, matching the model_by_role precedent)."""
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _rate_limit_error(
            "Rate limit exceeded on primary"
        )

        tier2_client = MagicMock()
        tier2_client.base_url = "https://api.anthropic.com"
        tier2_client.chat.completions.create.side_effect = _rate_limit_error(
            "Rate limit exceeded on fallback"
        )

        tier3_client = MagicMock()
        tier3_client.chat.completions.create.side_effect = ValueError(
            "tier3 malformed response"
        )

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=self._task_config()), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry",
                   return_value=(tier2_client, "claude-haiku-4-5-20251001")), \
             patch("agent.auxiliary_client._try_main_agent_model_fallback",
                   return_value=(tier3_client, "claude-opus-4-8", "main-agent(anthropic)")) as tier3_spy:
            with pytest.raises(ValueError, match="tier3 malformed response"):
                ac.call_llm(
                    task="title_generation",
                    messages=[{"role": "user", "content": "describe this"}],
                )

        tier3_spy.assert_called_once()

    def test_f_no_fallback_key_behaves_like_pre_feature_ladder(self, monkeypatch):
        """(f) A pin with NO fallback sub-key: tier1 fails, straight to
        tier3 (main-agent-model), exactly as before this feature existed —
        no tier2 attempted, no new code path touched."""
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _rate_limit_error()

        tier3_client = MagicMock()
        tier3_client.chat.completions.create.return_value = _DummyResponse("main model ok")

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=self._task_config(with_fallback=False)), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry") as tier2_spy, \
             patch("agent.auxiliary_client._try_main_agent_model_fallback",
                   return_value=(tier3_client, "claude-opus-4-8", "main-agent(anthropic)")) as tier3_spy:
            result = ac.call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "describe this"}],
            )

        assert result.choices[0].message.content == "main model ok"
        tier2_spy.assert_not_called()
        tier3_spy.assert_called_once()


# ---------------------------------------------------------------------------
# Visibility: distinct log line when tier 2 (shorthand) engages
# ---------------------------------------------------------------------------


class TestFallbackEngagedVisibility:
    def test_shorthand_engagement_logs_distinct_notice(self, monkeypatch, caplog):
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _rate_limit_error()

        tier2_client = MagicMock()
        tier2_client.base_url = "https://api.anthropic.com"
        tier2_client.chat.completions.create.return_value = _DummyResponse("tier2 ok")

        task_config = ac._apply_singular_aux_fallback_shorthand({
            "provider": "ollama-cloud",
            "model": "gemma4:31b",
            "fallback": {"model": "claude-haiku-4-5-20251001", "provider": "anthropic"},
        })

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=task_config), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry",
                   return_value=(tier2_client, "claude-haiku-4-5-20251001")), \
             caplog.at_level("WARNING", logger="agent.auxiliary_client"):
            ac.call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "describe this"}],
            )

        assert any(
            "Auxiliary fallback engaged for task" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_multi_entry_fallback_chain_does_not_emit_shorthand_notice(self, monkeypatch, caplog):
        """The pre-existing multi-entry fallback_chain feature's log format
        must be unchanged — the new WARNING notice is exclusive to the
        singular shorthand path (marked via the private entry flag)."""
        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _rate_limit_error()

        chain_client = MagicMock()
        chain_client.base_url = "https://api.anthropic.com"
        chain_client.chat.completions.create.return_value = _DummyResponse("chain ok")

        task_config = {
            "provider": "ollama-cloud",
            "model": "gemma4:31b",
            "fallback_chain": [
                {"model": "claude-haiku-4-5-20251001", "provider": "anthropic"}
            ],
        }

        with patch("agent.auxiliary_client._get_auxiliary_task_config",
                   return_value=task_config), \
             patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "gemma4:31b")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("ollama-cloud", "gemma4:31b", None, None, None)), \
             patch("agent.auxiliary_client._resolve_fallback_entry",
                   return_value=(chain_client, "claude-haiku-4-5-20251001")), \
             caplog.at_level("WARNING", logger="agent.auxiliary_client"):
            ac.call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "describe this"}],
            )

        assert not any(
            "Auxiliary fallback engaged for task" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]
