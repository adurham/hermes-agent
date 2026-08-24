"""Unit tests for ``hermes_cli.personas`` discovery + config helpers.

Personas live under ``~/.hermes/personas/<category>/<name>.md``.  The
fake-personas fixture builds that exact layout in a tmp dir, then routes
discovery to it via the ``personas_path=`` arg.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hermes_cli import personas


# ── Frontmatter parser ────────────────────────────────────────────────────


def test_strip_frontmatter_drops_yaml_block():
    text = textwrap.dedent("""
        ---
        name: foo
        description: bar
        ---

        # Body

        Content.
    """).lstrip()
    body = personas._strip_frontmatter(text)
    assert body.startswith("# Body")
    assert "name: foo" not in body


def test_strip_frontmatter_passes_through_when_missing():
    text = "# No Frontmatter\n\nJust body."
    assert personas._strip_frontmatter(text) == text


def test_strip_frontmatter_handles_unclosed_block():
    text = "---\nname: incomplete\nbody\n"
    assert personas._strip_frontmatter(text) == text


def test_parse_frontmatter_simple_keys():
    text = textwrap.dedent("""
        ---
        name: researcher
        description: Investigates patterns
        ---

        body
    """).lstrip()
    meta = personas._parse_frontmatter(text)
    assert meta["name"] == "researcher"
    assert meta["description"] == "Investigates patterns"


def test_parse_frontmatter_strips_quotes():
    text = textwrap.dedent("""
        ---
        name: "quoted-name"
        description: 'single-quoted description'
        ---
        body
    """).lstrip()
    meta = personas._parse_frontmatter(text)
    assert meta["name"] == "quoted-name"
    assert meta["description"] == "single-quoted description"


def test_parse_frontmatter_joins_continuation_lines():
    text = textwrap.dedent("""
        ---
        name: foo
        description: line one
          continued on line two
        ---
        body
    """).lstrip()
    meta = personas._parse_frontmatter(text)
    assert meta["description"] == "line one continued on line two"


def test_parse_frontmatter_missing_returns_empty():
    assert personas._parse_frontmatter("# No frontmatter\nbody") == {}


# ── Discovery ─────────────────────────────────────────────────────────────


@pytest.fixture
def fake_personas(tmp_path: Path) -> Path:
    """Build a personas tree: <root>/<category>/<name>.md and root .md files."""
    # Root-level persona (category="general").
    (tmp_path / "researcher.md").write_text(
        textwrap.dedent("""
            ---
            name: researcher
            description: Investigates patterns
            ---

            # Researcher
            Body content.
        """).lstrip(),
        encoding="utf-8",
    )
    # Subdir persona (category="swarm").
    swarm = tmp_path / "swarm"
    swarm.mkdir()
    (swarm / "coordinator.md").write_text(
        textwrap.dedent("""
            ---
            name: coordinator
            description: Coordinates swarm topology
            ---

            # Coordinator
        """).lstrip(),
        encoding="utf-8",
    )
    # README at root — should be filtered by _NON_AGENT_BASENAMES.
    (tmp_path / "README.md").write_text("# README\n", encoding="utf-8")
    return tmp_path


def test_discover_returns_filtered_personas(fake_personas: Path):
    found = personas.discover_personas(fake_personas)
    names = sorted(p.name for p in found)
    assert names == ["coordinator", "researcher"]


def test_discover_assigns_categories(fake_personas: Path):
    found = personas.discover_personas(fake_personas)
    by_name = {p.name: p for p in found}
    assert by_name["researcher"].category == "general"  # at root
    assert by_name["coordinator"].category == "swarm"   # under swarm/


def test_discover_returns_empty_for_missing_path(tmp_path: Path):
    missing = tmp_path / "nope"
    assert personas.discover_personas(missing) == []


def test_load_prompt_strips_frontmatter(fake_personas: Path):
    found = personas.discover_personas(fake_personas)
    researcher = next(p for p in found if p.name == "researcher")
    body = researcher.load_prompt()
    assert body.startswith("# Researcher")
    assert "name:" not in body
    assert body.strip() != ""


def test_group_by_category_preserves_within_group_order(fake_personas: Path):
    found = personas.discover_personas(fake_personas)
    groups = personas.group_by_category(found)
    assert sorted(groups.keys()) == ["general", "swarm"]
    assert [p.name for p in groups["general"]] == ["researcher"]
    assert [p.name for p in groups["swarm"]] == ["coordinator"]


def test_lookup_agent_via_discovery(fake_personas: Path, monkeypatch):
    monkeypatch.setattr(personas, "get_personas_path", lambda: fake_personas)
    p = personas.lookup_agent("researcher")
    assert p is not None
    assert p.name == "researcher"
    assert personas.lookup_agent("ghost") is None
    assert personas.lookup_agent("") is None


# ── sync_from_ruflo ───────────────────────────────────────────────────────


@pytest.fixture
def fake_ruflo(tmp_path: Path) -> Path:
    """Minimal ruflo-shaped tree (.claude/agents/...) for sync_from_ruflo."""
    a1 = tmp_path / ".claude" / "agents"
    a1.mkdir(parents=True)
    (a1 / "researcher.md").write_text(
        "---\nname: researcher\n---\n# Researcher\n", encoding="utf-8"
    )
    sub = a1 / "swarm"
    sub.mkdir()
    (sub / "coordinator.md").write_text(
        "---\nname: coordinator\n---\n# Coordinator\n", encoding="utf-8"
    )
    # Should be filtered out by sync (cloud-integration category).
    fn = a1 / "flow-nexus"
    fn.mkdir()
    (fn / "auth.md").write_text("---\nname: auth\n---\n# Auth\n", encoding="utf-8")
    # Legacy v2 tree — filtered.
    legacy = tmp_path / "v2" / ".claude" / "agents"
    legacy.mkdir(parents=True)
    (legacy / "old.md").write_text("---\nname: old\n---\n# Old\n", encoding="utf-8")
    return tmp_path


def test_sync_copies_filtered_personas(fake_ruflo: Path, tmp_path: Path):
    dst = tmp_path / "personas-out"
    copied, skipped = personas.sync_from_ruflo(fake_ruflo, dest=dst)
    assert copied == 2  # researcher + coordinator; flow-nexus and v2 filtered
    assert skipped == 0
    assert (dst / "general" / "researcher.md").is_file()
    assert (dst / "swarm" / "coordinator.md").is_file()


def test_sync_skips_existing_when_no_overwrite(fake_ruflo: Path, tmp_path: Path):
    dst = tmp_path / "personas-out"
    personas.sync_from_ruflo(fake_ruflo, dest=dst)  # first sync
    copied, skipped = personas.sync_from_ruflo(fake_ruflo, dest=dst)  # second
    assert copied == 0
    assert skipped == 2


def test_sync_overwrites_when_requested(fake_ruflo: Path, tmp_path: Path):
    dst = tmp_path / "personas-out"
    personas.sync_from_ruflo(fake_ruflo, dest=dst)
    # Modify the dest copy, then re-sync with overwrite to verify it gets reset.
    target = dst / "general" / "researcher.md"
    target.write_text("LOCAL EDIT", encoding="utf-8")
    copied, _ = personas.sync_from_ruflo(fake_ruflo, dest=dst, overwrite=True)
    assert copied == 2
    assert "name: researcher" in target.read_text(encoding="utf-8")


def test_sync_missing_root_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        personas.sync_from_ruflo(tmp_path / "nope", dest=tmp_path / "out")


# ── Role-model map (config-backed) ────────────────────────────────────────
#
# These tests stub the load/save plumbing so they don't touch the real
# ~/.hermes/config.yaml.


def test_get_role_model_map_empty_when_no_delegation(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    assert personas.get_role_model_map() == {}


def test_get_role_model_map_reads_delegation_section(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {
                    "researcher": "claude-haiku-4-5",
                    "architect": "claude-sonnet-4-6",
                }
            }
        },
    )
    m = personas.get_role_model_map()
    assert m == {
        "researcher": "claude-haiku-4-5",
        "architect": "claude-sonnet-4-6",
    }


def test_get_role_model_map_filters_non_string_values(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {
                    "researcher": "claude-haiku-4-5",
                    "bogus": 42,         # non-string value — drop
                    "blank": "   ",      # whitespace-only — drop
                    "good": "claude-opus-4-7",
                }
            }
        },
    )
    m = personas.get_role_model_map()
    assert m == {
        "researcher": "claude-haiku-4-5",
        "good": "claude-opus-4-7",
    }


def test_set_role_model_writes_through(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert personas.set_role_model("researcher", "claude-haiku-4-5") is True
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "researcher:" in written
    assert "claude-haiku-4-5" in written


def test_set_role_model_clears_when_model_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {"researcher": "claude-haiku-4-5"}
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "delegation:\n  model_by_role:\n    researcher: claude-haiku-4-5\n",
        encoding="utf-8",
    )
    assert personas.set_role_model("researcher", None) is True
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "researcher" not in written


def test_lookup_model_for_role_returns_none_when_unset(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model_by_role": {"researcher": "claude-haiku-4-5"}}},
    )
    assert personas.lookup_model_for_role("researcher") == "claude-haiku-4-5"
    assert personas.lookup_model_for_role("unset_role") is None
    assert personas.lookup_model_for_role("") is None
    assert personas.lookup_model_for_role(None) is None


# ── Provider-aware role entries (dict-form model_by_role) ─────────────────
#
# A model_by_role entry may be a bare model string (historical shape) or a
# dict carrying the provider that model must run on.  The three read
# accessors are views over the same normalized entry map, so the contracts
# below are about how they must agree with each other.


def _cfg(monkeypatch, by_role: dict):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model_by_role": by_role}},
    )


def test_bare_string_entry_has_model_but_no_provider(monkeypatch):
    _cfg(monkeypatch, {"coder": "claude-opus-5"})
    assert personas.get_role_entry_map() == {"coder": {"model": "claude-opus-5"}}
    assert personas.get_role_model_map() == {"coder": "claude-opus-5"}
    assert "coder" not in personas.get_role_provider_map()
    assert personas.lookup_provider_for_role("coder") is None


def test_dict_entry_splits_into_model_and_provider(monkeypatch):
    _cfg(
        monkeypatch,
        {"jr-coder": {"model": "qwen3-coder:480b-cloud", "provider": "ollama-cloud"}},
    )
    assert personas.get_role_entry_map()["jr-coder"] == {
        "model": "qwen3-coder:480b-cloud",
        "provider": "ollama-cloud",
    }
    # get_role_model_map stays a role -> model-string map: dict entries flatten.
    assert personas.get_role_model_map() == {"jr-coder": "qwen3-coder:480b-cloud"}
    assert personas.get_role_provider_map() == {"jr-coder": "ollama-cloud"}
    assert personas.lookup_model_for_role("jr-coder") == "qwen3-coder:480b-cloud"
    assert personas.lookup_provider_for_role("jr-coder") == "ollama-cloud"


def test_mixed_bare_and_dict_entries_resolve_independently(monkeypatch):
    _cfg(
        monkeypatch,
        {
            "coder": "claude-opus-5",
            "jr-coder": {
                "model": "qwen3-coder:480b-cloud",
                "provider": "ollama-cloud",
            },
            "mid-coder": {
                "model": "deepseek-v4-flash:0731-cloud",
                "provider": "ollama-cloud",
            },
        },
    )
    models = personas.get_role_model_map()
    providers = personas.get_role_provider_map()
    # Every role resolves a model; only the dict-form ones pin a provider.
    assert set(models) == {"coder", "jr-coder", "mid-coder"}
    assert set(providers) == {"jr-coder", "mid-coder"}
    assert models["coder"] == "claude-opus-5"
    assert models["mid-coder"] == "deepseek-v4-flash:0731-cloud"
    assert providers["mid-coder"] == "ollama-cloud"


def test_provider_is_never_returned_without_its_own_model(monkeypatch):
    """A provider with no model would send the batch's model to the wrong
    endpoint — such an entry is dropped from every accessor."""
    _cfg(
        monkeypatch,
        {
            "orphan": {"provider": "ollama-cloud"},
            "blank-model": {"model": "   ", "provider": "ollama-cloud"},
            "ok": {"model": "qwen3-coder:480b-cloud", "provider": "ollama-cloud"},
        },
    )
    assert set(personas.get_role_entry_map()) == {"ok"}
    assert set(personas.get_role_model_map()) == {"ok"}
    assert set(personas.get_role_provider_map()) == {"ok"}
    assert personas.lookup_provider_for_role("orphan") is None
    assert personas.lookup_model_for_role("orphan") is None
    # Invariant: every role with a provider also has a model.
    providers = personas.get_role_provider_map()
    models = personas.get_role_model_map()
    assert set(providers).issubset(set(models))


def test_entry_map_carries_optional_credential_fields_and_strips(monkeypatch):
    _cfg(
        monkeypatch,
        {
            "custom": {
                "model": "  local-model  ",
                "provider": " custom:exo ",
                "base_url": "http://exo.local:8000/v1",
                "api_key": "sk-test",
                "api_mode": "chat_completions",
                "unknown_key": "ignored",
            }
        },
    )
    entry = personas.get_role_entry_map()["custom"]
    assert entry == {
        "model": "local-model",
        "provider": "custom:exo",
        "base_url": "http://exo.local:8000/v1",
        "api_key": "sk-test",
        "api_mode": "chat_completions",
    }
    assert "unknown_key" not in entry


def test_garbage_entries_are_dropped_without_raising(monkeypatch):
    _cfg(
        monkeypatch,
        {
            "researcher": "claude-haiku-4-5",
            "nil": None,
            "num": 42,
            "blank": "   ",
            "empty_dict": {},
            "bad_types": {"model": 7, "provider": ["x"]},
            5: "claude-opus-4-7",  # non-string key
        },
    )
    # Nothing raises, and only the one valid entry survives anywhere.
    assert personas.get_role_entry_map() == {
        "researcher": {"model": "claude-haiku-4-5"}
    }
    assert personas.get_role_model_map() == {"researcher": "claude-haiku-4-5"}
    assert personas.get_role_provider_map() == {}


def test_set_role_model_without_provider_writes_bare_string(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert personas.set_role_model("coder", "claude-opus-5") is True
    written = _read_by_role(tmp_path)
    assert written == {"coder": "claude-opus-5"}


def test_set_role_model_with_provider_writes_dict_form(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert (
        personas.set_role_model(
            "jr-coder", "qwen3-coder:480b-cloud", provider="ollama-cloud"
        )
        is True
    )
    written = _read_by_role(tmp_path)
    assert written == {
        "jr-coder": {
            "model": "qwen3-coder:480b-cloud",
            "provider": "ollama-cloud",
        }
    }


def test_set_role_model_none_deletes_dict_entry(monkeypatch, tmp_path):
    existing = {
        "jr-coder": {"model": "qwen3-coder:480b-cloud", "provider": "ollama-cloud"},
        "coder": "claude-opus-5",
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model_by_role": existing}},
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert personas.set_role_model("jr-coder", None) is True
    written = _read_by_role(tmp_path)
    assert "jr-coder" not in written
    assert written["coder"] == "claude-opus-5"


def _read_by_role(tmp_path) -> dict:
    """Read back what actually landed in ``delegation.model_by_role``."""
    import yaml

    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")) or {}
    return cfg.get("delegation", {}).get("model_by_role", {})


# ── apply_suggested_defaults ──────────────────────────────────────────────


def test_apply_suggested_defaults_fills_empties(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    applied, skipped = personas.apply_suggested_defaults()
    assert applied == len(personas.SUGGESTED_ROLE_MODELS)
    assert skipped == 0
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    # Researcher: promoted to Sonnet 2026-05-04 (multi-source scans blow
    # past Haiku's 200K context).  See SUGGESTED_ROLE_MODELS docstring.
    assert "researcher: claude-sonnet-4-6" in written
    assert "security-architect: claude-opus-4-7" in written
    # A role that's still Haiku — just to prove the test exercises both.
    assert "pii-detector: claude-haiku-4-5" in written


def test_apply_suggested_defaults_preserves_user_pins(monkeypatch, tmp_path):
    user_pin = "claude-opus-4-7"  # not the suggested default for researcher
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model_by_role": {"researcher": user_pin}}},
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"delegation:\n  model_by_role:\n    researcher: {user_pin}\n",
        encoding="utf-8",
    )
    applied, skipped = personas.apply_suggested_defaults(overwrite=False)
    assert skipped >= 1
    assert applied == len(personas.SUGGESTED_ROLE_MODELS) - 1
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert f"researcher: {user_pin}" in written


def test_apply_suggested_defaults_force_overwrites(monkeypatch, tmp_path):
    user_pin = "claude-opus-4-7"
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model_by_role": {"researcher": user_pin}}},
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"delegation:\n  model_by_role:\n    researcher: {user_pin}\n",
        encoding="utf-8",
    )
    applied, skipped = personas.apply_suggested_defaults(overwrite=True)
    assert applied == len(personas.SUGGESTED_ROLE_MODELS)
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    # Suggested default for researcher is now Sonnet (promoted 2026-05-04
    # because multi-source research scans hit Haiku's context cap).
    assert "researcher: claude-sonnet-4-6" in written
    assert f"researcher: {user_pin}" not in written


def test_apply_suggested_defaults_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    applied1, _ = personas.apply_suggested_defaults()

    map_after_first = dict(personas.SUGGESTED_ROLE_MODELS)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model_by_role": map_after_first}},
    )
    applied2, skipped2 = personas.apply_suggested_defaults()
    assert applied2 == 0
    assert skipped2 == len(personas.SUGGESTED_ROLE_MODELS)
    assert applied1 == len(personas.SUGGESTED_ROLE_MODELS)


def test_apply_suggested_defaults_preserves_dict_form_entry(monkeypatch, tmp_path):
    """A provider-pinned role must round-trip through a defaults sweep.

    Regression guard: the merge used to be built from the flattened
    role->model map, so saving it back would rewrite the user's dict entry
    as a bare string and silently drop the provider.
    """
    pinned = {"model": "qwen3-coder:480b-cloud", "provider": "ollama-cloud"}
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {
                    "jr-coder": dict(pinned),
                    "researcher": "claude-opus-4-7",
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    applied, _ = personas.apply_suggested_defaults(overwrite=False)
    assert applied > 0  # the sweep really did write the config back
    written = _read_by_role(tmp_path)
    assert written["jr-coder"] == pinned
    # The bare-string neighbour keeps its historical shape too.
    assert written["researcher"] == "claude-opus-4-7"


def test_apply_suggested_defaults_skips_dict_entry_matching_suggestion(
    monkeypatch, tmp_path
):
    """The 'already equals the suggestion' skip compares the entry's model."""
    role, suggested = next(iter(personas.SUGGESTED_ROLE_MODELS.items()))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {
                    role: {"model": suggested, "provider": "ollama-cloud"}
                }
            }
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    applied, _ = personas.apply_suggested_defaults(overwrite=True)
    # Every other role is applied; the matching dict entry is skipped, and
    # its provider survives.
    assert applied == len(personas.SUGGESTED_ROLE_MODELS) - 1
    written = _read_by_role(tmp_path)
    assert written[role] == {"model": suggested, "provider": "ollama-cloud"}


def test_suggested_role_models_only_uses_known_models():
    """Sanity: every suggested model is one of the three curated choices."""
    valid = {"claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"}
    bad = {
        role: model
        for role, model in personas.SUGGESTED_ROLE_MODELS.items()
        if model not in valid
    }
    assert not bad, f"Unknown model in defaults: {bad}"


# ── Back-compat shim ──────────────────────────────────────────────────────


def test_ruflo_agents_shim_reexports():
    """The legacy ``hermes_cli.ruflo_agents`` shim re-exports everything we
    need for the old import paths to keep working without churn."""
    from hermes_cli import ruflo_agents

    # Public API
    assert ruflo_agents.Persona is personas.Persona
    assert ruflo_agents.RufloAgent is personas.Persona
    assert ruflo_agents.SUGGESTED_ROLE_MODELS is personas.SUGGESTED_ROLE_MODELS
    assert ruflo_agents.discover_ruflo_agents is personas.discover_ruflo_agents
    assert ruflo_agents.lookup_agent is personas.lookup_agent
    assert ruflo_agents.get_role_model_map is personas.get_role_model_map
    assert ruflo_agents.get_role_entry_map is personas.get_role_entry_map
    assert ruflo_agents.get_role_provider_map is personas.get_role_provider_map
    assert ruflo_agents.set_role_model is personas.set_role_model
    assert ruflo_agents.lookup_model_for_role is personas.lookup_model_for_role
    assert ruflo_agents.lookup_provider_for_role is personas.lookup_provider_for_role
    assert ruflo_agents.apply_suggested_defaults is personas.apply_suggested_defaults
    # Private helpers re-exported for older test imports
    assert ruflo_agents._parse_frontmatter is personas._parse_frontmatter
    assert ruflo_agents._strip_frontmatter is personas._strip_frontmatter
