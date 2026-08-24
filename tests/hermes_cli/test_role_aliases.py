"""Role aliases — a role name that is a pure synonym for another role.

``hermes_cli.personas.ROLE_ALIASES`` maps an alias role to the canonical role
it resolves to for EVERY role-keyed surface: model, provider (and the rest of
the endpoint credential bundle), reasoning effort, and persona prompt.

``sr-coder -> coder`` is the motivating entry. The Jr/Mid/Sr coder tiering
names its cheap tiers explicitly (``jr-coder``/``mid-coder``) while the top
tier kept the bare historical name ``coder``; ``sr-coder`` makes the triad
nameable at a dispatch site without forking ``coder``'s configuration.

These are behavior contracts on the ALIAS RELATION — "an alias resolves to
whatever its target resolves to" — not snapshots of any particular model
string. That is deliberate: a test asserting ``sr-coder == "claude-opus-5"``
would still pass if the alias silently broke and both sides happened to be
retargeted, and would fail spuriously the moment the user retargets ``coder``.
The value-level check is kept separately and pinned to a fixture config.

The regression guard that ``coder`` itself is completely unaffected is the
other half of the contract: this feature is purely additive.
"""

import pytest

from hermes_cli import personas


ALIAS = "sr-coder"
CANONICAL = "coder"


def _cfg(monkeypatch, by_role: dict, reasoning: dict | None = None):
    """Point the config readers at an in-memory delegation block."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": by_role,
                "reasoning_effort_by_role": reasoning or {},
            }
        },
    )


# ---------------------------------------------------------------------------
# The alias table + resolver
# ---------------------------------------------------------------------------


class TestResolveRoleAlias:
    def test_sr_coder_is_registered_as_a_synonym_for_coder(self):
        assert personas.ROLE_ALIASES[ALIAS] == CANONICAL
        assert personas.resolve_role_alias(ALIAS) == CANONICAL

    def test_non_alias_roles_resolve_to_none(self):
        """Only registered aliases resolve; everything else is left alone."""
        assert personas.resolve_role_alias(CANONICAL) is None
        assert personas.resolve_role_alias("researcher") is None
        assert personas.resolve_role_alias("jr-coder") is None

    def test_empty_and_none_roles_resolve_to_none(self):
        assert personas.resolve_role_alias(None) is None
        assert personas.resolve_role_alias("") is None
        assert personas.resolve_role_alias("   ") is None

    def test_surrounding_whitespace_is_tolerated(self):
        assert personas.resolve_role_alias(f"  {ALIAS}  ") == CANONICAL

    def test_aliases_do_not_chain(self):
        """One hop only — an alias target is never itself re-resolved.

        Guards against a future entry accidentally forming a cycle or a
        surprising two-hop resolution.
        """
        for alias, target in personas.ROLE_ALIASES.items():
            assert personas.resolve_role_alias(target) is None, (
                f"{alias!r} -> {target!r} -> {personas.resolve_role_alias(target)!r} "
                "is a chained alias"
            )

    def test_no_alias_shadows_a_real_curated_role(self):
        """An alias name must not collide with a curated persona role."""
        for alias in personas.ROLE_ALIASES:
            assert alias not in personas.SUGGESTED_ROLE_MODELS


# ---------------------------------------------------------------------------
# Model / provider resolution through the alias
# ---------------------------------------------------------------------------


class TestAliasResolvesLikeCanonical:
    def test_alias_resolves_to_the_same_model_as_its_target(self, monkeypatch):
        """The core invariant, stated as a relation rather than a literal."""
        _cfg(monkeypatch, {CANONICAL: "claude-opus-5"})

        assert personas.lookup_model_for_role(ALIAS) == personas.lookup_model_for_role(
            CANONICAL
        )
        assert personas.lookup_role_entry(ALIAS) == personas.lookup_role_entry(
            CANONICAL
        )

    def test_alias_resolves_to_opus_5_against_the_users_config(self, monkeypatch):
        """Value-level proof, pinned to a fixture mirroring the live config."""
        _cfg(monkeypatch, {CANONICAL: "claude-opus-5", "reviewer": "claude-opus-5"})

        assert personas.lookup_model_for_role(ALIAS) == "claude-opus-5"
        # No provider override: a bare-string entry inherits the batch provider.
        assert personas.lookup_provider_for_role(ALIAS) is None

    def test_alias_relation_holds_when_target_is_retargeted(self, monkeypatch):
        """Alias tracks its target — it does not fork the value.

        This is the whole reason for an alias over a duplicated config entry:
        a copied entry would silently drift the moment ``coder`` changes.
        """
        for model in ("claude-opus-5", "claude-sonnet-4-6", "some-future-model"):
            _cfg(monkeypatch, {CANONICAL: model})
            assert personas.lookup_model_for_role(ALIAS) == model

    def test_alias_inherits_the_whole_credential_bundle(self, monkeypatch):
        """Model AND provider travel together — never a mix of the two."""
        _cfg(
            monkeypatch,
            {
                CANONICAL: {
                    "model": "qwen3-coder:480b-cloud",
                    "provider": "ollama-cloud",
                    "base_url": "https://ollama.com/v1",
                }
            },
        )

        assert personas.lookup_role_entry(ALIAS) == personas.lookup_role_entry(
            CANONICAL
        )
        assert personas.lookup_model_for_role(ALIAS) == "qwen3-coder:480b-cloud"
        assert personas.lookup_provider_for_role(ALIAS) == "ollama-cloud"

    def test_alias_resolves_nothing_when_target_is_unconfigured(self, monkeypatch):
        """No config, no resolution — the alias invents nothing."""
        _cfg(monkeypatch, {"researcher": "claude-haiku-4-5"})

        assert personas.lookup_model_for_role(ALIAS) is None
        assert personas.lookup_provider_for_role(ALIAS) is None
        assert personas.lookup_role_entry(ALIAS) == {}

    def test_explicit_alias_entry_overrides_the_aliased_value(self, monkeypatch):
        """An alias is a FALLBACK, never an override.

        A user who later wants the tiers to genuinely diverge just configures
        ``sr-coder`` directly.
        """
        _cfg(
            monkeypatch,
            {CANONICAL: "claude-opus-5", ALIAS: "claude-sonnet-4-6"},
        )

        assert personas.lookup_model_for_role(ALIAS) == "claude-sonnet-4-6"
        # ...and the target keeps its own value.
        assert personas.lookup_model_for_role(CANONICAL) == "claude-opus-5"


class TestAliasReasoningEffort:
    def test_alias_inherits_the_targets_reasoning_effort(self, monkeypatch):
        _cfg(
            monkeypatch,
            {CANONICAL: "claude-opus-5"},
            reasoning={"orchestrator": "max", CANONICAL: "high"},
        )

        assert personas.lookup_reasoning_for_role(
            ALIAS
        ) == personas.lookup_reasoning_for_role(CANONICAL)
        assert personas.lookup_reasoning_for_role(ALIAS) == "high"

    def test_explicit_alias_effort_overrides(self, monkeypatch):
        _cfg(
            monkeypatch,
            {CANONICAL: "claude-opus-5"},
            reasoning={CANONICAL: "high", ALIAS: "max"},
        )

        assert personas.lookup_reasoning_for_role(ALIAS) == "max"
        assert personas.lookup_reasoning_for_role(CANONICAL) == "high"

    def test_no_effort_configured_resolves_to_none(self, monkeypatch):
        _cfg(monkeypatch, {CANONICAL: "claude-opus-5"}, reasoning={})
        assert personas.lookup_reasoning_for_role(ALIAS) is None


# ---------------------------------------------------------------------------
# Regression guard — 'coder' must be completely unaffected
# ---------------------------------------------------------------------------


class TestCanonicalRoleUnaffected:
    def test_coder_resolves_exactly_as_before(self, monkeypatch):
        _cfg(
            monkeypatch,
            {CANONICAL: "claude-opus-5", "reviewer": "claude-opus-5"},
            reasoning={CANONICAL: "high"},
        )

        assert personas.lookup_model_for_role(CANONICAL) == "claude-opus-5"
        assert personas.lookup_provider_for_role(CANONICAL) is None
        assert personas.lookup_reasoning_for_role(CANONICAL) == "high"
        assert personas.lookup_role_entry(CANONICAL) == {"model": "claude-opus-5"}

    def test_coder_is_not_an_alias(self, monkeypatch):
        """``coder`` must never resolve THROUGH the alias table."""
        assert CANONICAL not in personas.ROLE_ALIASES
        assert personas.resolve_role_alias(CANONICAL) is None

    def test_raw_config_maps_contain_no_alias_keys(self, monkeypatch):
        """The config readers stay a faithful view of config.yaml.

        Alias resolution is a lookup-time fallback, NOT injected into the
        maps — otherwise the CLI role picker would display a role the user
        never configured and could persist a duplicate entry on save.
        """
        _cfg(
            monkeypatch,
            {CANONICAL: "claude-opus-5"},
            reasoning={CANONICAL: "high"},
        )

        assert set(personas.get_role_model_map()) == {CANONICAL}
        assert set(personas.get_role_entry_map()) == {CANONICAL}
        assert set(personas.get_role_reasoning_map()) == {CANONICAL}
        assert ALIAS not in personas.get_role_model_map()
        assert ALIAS not in personas.get_role_entry_map()

    def test_unrelated_roles_are_untouched(self, monkeypatch):
        _cfg(
            monkeypatch,
            {
                CANONICAL: "claude-opus-5",
                "jr-coder": {
                    "model": "qwen3-coder:480b-cloud",
                    "provider": "ollama-cloud",
                },
                "researcher": "claude-haiku-4-5",
            },
        )

        assert personas.lookup_model_for_role("researcher") == "claude-haiku-4-5"
        assert personas.lookup_model_for_role("jr-coder") == "qwen3-coder:480b-cloud"
        assert personas.lookup_provider_for_role("jr-coder") == "ollama-cloud"
        # An unknown role still resolves to nothing.
        assert personas.lookup_model_for_role("no-such-role") is None


# ---------------------------------------------------------------------------
# Public surface — the legacy shim must re-export the new names
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_alias_api_is_exported_from_personas(self):
        for name in ("ROLE_ALIASES", "resolve_role_alias", "lookup_role_entry"):
            assert name in personas.__all__

    def test_alias_api_is_reexported_through_the_ruflo_shim(self):
        from hermes_cli import ruflo_agents

        assert ruflo_agents.ROLE_ALIASES is personas.ROLE_ALIASES
        assert ruflo_agents.resolve_role_alias is personas.resolve_role_alias
        assert ruflo_agents.lookup_role_entry is personas.lookup_role_entry
        for name in ("ROLE_ALIASES", "resolve_role_alias", "lookup_role_entry"):
            assert name in ruflo_agents.__all__


# ---------------------------------------------------------------------------
# Persona prompt lookup
# ---------------------------------------------------------------------------


class TestAliasPersonaLookup:
    @staticmethod
    def _persona_dir(tmp_path, names):
        for name in names:
            (tmp_path / f"{name}.md").write_text(
                f"---\nname: {name}\n---\n\nYou are the {name} persona.\n",
                encoding="utf-8",
            )
        return tmp_path

    def test_alias_falls_back_to_the_targets_persona(self, tmp_path, monkeypatch):
        """An alias with no .md of its own inherits its target's prompt."""
        path = self._persona_dir(tmp_path, [CANONICAL])
        monkeypatch.setattr(personas, "get_personas_path", lambda *a, **k: path)

        found = personas.lookup_agent(ALIAS)
        assert found is not None
        assert found.name == CANONICAL
        # Same persona the canonical role resolves to.
        canonical = personas.lookup_agent(CANONICAL)
        assert canonical is not None
        assert found.name == canonical.name

    def test_alias_with_its_own_persona_file_wins(self, tmp_path, monkeypatch):
        path = self._persona_dir(tmp_path, [CANONICAL, ALIAS])
        monkeypatch.setattr(personas, "get_personas_path", lambda *a, **k: path)

        found = personas.lookup_agent(ALIAS)
        assert found is not None
        assert found.name == ALIAS

    def test_unknown_role_still_returns_none(self, tmp_path, monkeypatch):
        path = self._persona_dir(tmp_path, [CANONICAL])
        monkeypatch.setattr(personas, "get_personas_path", lambda *a, **k: path)

        assert personas.lookup_agent("no-such-persona") is None

    def test_canonical_persona_lookup_unaffected(self, tmp_path, monkeypatch):
        path = self._persona_dir(tmp_path, [CANONICAL])
        monkeypatch.setattr(personas, "get_personas_path", lambda *a, **k: path)

        found = personas.lookup_agent(CANONICAL)
        assert found is not None
        assert found.name == CANONICAL
