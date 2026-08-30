"""Tests for ``hermes_cli.model_tiers`` — the shared tier resolver.

These are the loud tripwires for the stale-constants bug class. The three
tier anchors (haiku/sonnet/opus) used to be hardcoded in BOTH
``persona_library.py`` and ``delegation_stats.py``; when the live config
roster moved to a new generation, ``apply_suggested_defaults`` kept writing
the old generation into the user's config and ``suggest_retunes`` silently
stopped firing. The resolver here is the single source of truth both
surfaces share, resolved at CALL TIME from the live roster.

The drift test seeds a NEXT-GENERATION roster and asserts the resolver,
``apply_suggested_defaults``, and ``suggest_retunes`` all track it — so if
anyone reintroduces a hardcoded literal, this file fails loudly at the next
generation bump.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import delegation_stats as ds
from hermes_cli import model_tiers as mt
from hermes_cli import personas


# ── family_of / normalization ─────────────────────────────────────────────


def test_family_of_recognizes_each_tier():
    assert mt.family_of("claude-haiku-4-5") == "haiku"
    assert mt.family_of("claude-sonnet-4-6") == "sonnet"
    assert mt.family_of("claude-opus-4-7") == "opus"


def test_family_of_strips_provider_prefix():
    assert mt.family_of("anthropic/claude-opus-5") == "opus"
    assert mt.family_of("anthropic/claude-sonnet-5") == "sonnet"
    assert mt.family_of("anthropic/claude-haiku-4-5-20251001") == "haiku"


def test_family_of_is_case_insensitive():
    assert mt.family_of("CLAUDE-OPUS-5") == "opus"


def test_family_of_returns_none_for_non_anthropic():
    for model in ("claude-fable-5", "qwen3-coder:480b-cloud", "gpt-5.6-sol", ""):
        assert mt.family_of(model) is None


# ── Fallback (empty roster) ───────────────────────────────────────────────


def test_resolve_falls_back_to_last_known_good_when_roster_empty(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    assert mt.resolve_tier_model("haiku") == mt.LAST_KNOWN_GOOD_TIERS["haiku"]
    assert mt.resolve_tier_model("sonnet") == mt.LAST_KNOWN_GOOD_TIERS["sonnet"]
    assert mt.resolve_tier_model("opus") == mt.LAST_KNOWN_GOOD_TIERS["opus"]


def test_resolve_falls_back_when_roster_has_no_anthropic_models(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model": "qwen3-coder:480b-cloud"}},
    )
    assert mt.resolve_tier_model("opus") == mt.LAST_KNOWN_GOOD_TIERS["opus"]


def test_resolve_unknown_tier_raises():
    with pytest.raises(ValueError):
        mt.resolve_tier_model("fable")


# ── Roster precedence ──────────────────────────────────────────────────────


def test_resolve_prefers_by_provider_over_model_by_role(monkeypatch):
    """by_provider.<p>.model wins over model_by_role and top-level model."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model": "claude-opus-4-7",
                "by_provider": {"anthropic": {"model": "claude-opus-5"}},
                "model_by_role": {"architect": "claude-opus-4-8"},
            }
        },
    )
    assert mt.resolve_tier_model("opus") == "claude-opus-5"


def test_resolve_prefers_model_by_role_primary_over_fallback(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {
                    "architect": {
                        "model": "claude-opus-5",
                        "provider": "anthropic",
                        "fallback": {"model": "claude-opus-4-8", "provider": "anthropic"},
                    }
                }
            }
        },
    )
    assert mt.resolve_tier_model("opus") == "claude-opus-5"


def test_resolve_uses_fallback_when_primary_is_other_family(monkeypatch):
    """A fallback bundle is scanned even when the primary is a different family."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {
                    "fable": {
                        "model": "claude-fable-5",
                        "provider": "anthropic",
                        "fallback": {"model": "claude-opus-6", "provider": "anthropic"},
                    }
                }
            }
        },
    )
    assert mt.resolve_tier_model("opus") == "claude-opus-6"


def test_resolve_uses_top_level_model_last(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model": "claude-sonnet-5"}},
    )
    assert mt.resolve_tier_model("sonnet") == "claude-sonnet-5"


def test_resolve_is_call_time_not_import_time(monkeypatch):
    """The resolver must re-read the live config on every call."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model": "claude-opus-4-7"}},
    )
    assert mt.resolve_tier_model("opus") == "claude-opus-4-7"
    # Change the roster mid-session; the next call must see it.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"delegation": {"model": "claude-opus-5"}},
    )
    assert mt.resolve_tier_model("opus") == "claude-opus-5"


# ── Rank maps ──────────────────────────────────────────────────────────────


def test_rank_maps_are_call_time_and_roster_current(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "delegation": {
                "model_by_role": {
                    "architect": "claude-opus-6",
                    "coder": "claude-sonnet-6",
                    "pii-detector": "claude-haiku-6-20260101",
                }
            }
        },
    )
    assert mt.tier_rank_map() == {
        "claude-haiku-6-20260101": 0,
        "claude-sonnet-6": 1,
        "claude-opus-6": 2,
    }
    assert mt.rank_tier_map() == {
        0: "claude-haiku-6-20260101",
        1: "claude-sonnet-6",
        2: "claude-opus-6",
    }


# ── DRIFT TRIPWIRE: next-generation roster ────────────────────────────────
#
# Seeds a gen-6 roster in a sandboxed HERMES_HOME and asserts the resolver,
# apply_suggested_defaults, and suggest_retunes all track it. This is the
# test that fails loudly at the next generation bump if anyone reintroduces
# hardcoded drift.


def _seed_gen6_roster(tmp_path: Path) -> None:
    """Write a gen-6 delegation roster into the sandboxed HERMES_HOME."""
    cfg = {
        "delegation": {
            "by_provider": {"anthropic": {"model": "claude-sonnet-6"}},
            "model_by_role": {
                "architect": {
                    "model": "claude-opus-6",
                    "provider": "anthropic",
                    "fallback": {"model": "claude-opus-6", "provider": "anthropic"},
                },
                "pii-detector": "claude-haiku-6-20260101",
            },
        }
    }
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def test_drift_resolver_returns_gen6_models(monkeypatch, tmp_path):
    """(i) The resolver returns the gen-6 models from a seeded roster."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_gen6_roster(tmp_path)
    assert mt.resolve_tier_model("haiku") == "claude-haiku-6-20260101"
    assert mt.resolve_tier_model("sonnet") == "claude-sonnet-6"
    assert mt.resolve_tier_model("opus") == "claude-opus-6"


def test_drift_apply_suggested_defaults_writes_gen6(monkeypatch, tmp_path):
    """(ii) apply_suggested_defaults writes gen-6 models into the config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_gen6_roster(tmp_path)
    applied, skipped = personas.apply_suggested_defaults()
    # pii-detector is pre-seeded to the gen-6 haiku model, so it's skipped
    # (already matches the resolved suggestion); every other role is applied.
    assert applied == len(personas.SUGGESTED_ROLE_MODELS) - 1
    assert skipped == 1
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    # A Haiku-tier role, a Sonnet-tier role, and an Opus-tier role all land
    # as the gen-6 model — never the stale last-known-good literal.
    assert "pii-detector: claude-haiku-6-20260101" in written
    assert "researcher: claude-sonnet-6" in written
    assert "security-architect: claude-opus-6" in written
    # And the stale gen-4 literals must NOT appear anywhere.
    assert "claude-haiku-4-5" not in written
    assert "claude-sonnet-4-6" not in written
    assert "claude-opus-4-7" not in written


def test_drift_suggest_retunes_fires_for_gen6(monkeypatch, tmp_path):
    """(iii) suggest_retunes produces promote/demote for gen-6 tier models."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_gen6_roster(tmp_path)
    # A gen-6 sonnet role that keeps hitting max_iterations → promote to opus.
    stats = [
        ds.DelegationStat(role="coder", model="claude-sonnet-6", hit_max_iter=True)
        for _ in range(5)
    ]
    aggs = ds.aggregate(stats)
    sugs = ds.suggest_retunes(aggs)
    assert len(sugs) == 1
    assert sugs[0].direction == "promote"
    assert sugs[0].suggested_model == "claude-opus-6"
    # A gen-6 opus role doing boring fast work → demote to sonnet.
    stats2 = [
        ds.DelegationStat(
            role="architect", model="claude-opus-6", status="completed", output_tokens=100
        )
        for _ in range(10)
    ]
    sugs2 = ds.suggest_retunes(ds.aggregate(stats2))
    assert len(sugs2) == 1
    assert sugs2[0].direction == "demote"
    assert sugs2[0].suggested_model == "claude-sonnet-6"


# ── Fallback consistency: empty roster behaves exactly as today ───────────


def test_fallback_apply_suggested_defaults_writes_last_known_good(
    monkeypatch, tmp_path
):
    """Empty roster → apply_suggested_defaults writes the last-known-good
    literals, exactly as the pre-refactor code did."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    applied, skipped = personas.apply_suggested_defaults()
    assert applied == len(personas.SUGGESTED_ROLE_MODELS)
    assert skipped == 0
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "researcher: claude-sonnet-4-6" in written
    assert "security-architect: claude-opus-4-7" in written
    assert "pii-detector: claude-haiku-4-5" in written


def test_fallback_suggest_retunes_uses_last_known_good(monkeypatch):
    """Empty roster → suggest_retunes still recognizes the last-known-good
    literals (the pre-refactor behavior)."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    stats = [
        ds.DelegationStat(role="coder", model="claude-sonnet-4-6", hit_max_iter=True)
        for _ in range(5)
    ]
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert len(sugs) == 1
    assert sugs[0].suggested_model == "claude-opus-4-7"


# ── LOCAL LADDER: non-Anthropic (ollama-cloud) roster ─────────────────────
#
# The user's real roster pins non-Anthropic models to the ruflo roles. These
# models are NOT in the Anthropic haiku/sonnet/opus ladder, so the old
# suggest_retunes silently skipped them (the exact bug this extension fixes).
# The local ladder ranks them from explicit role anchors in
# ``delegation.model_by_role`` (see :data:`mt.ROLE_TIER_GROUPS`), with a
# suffix-variant fallback for lighter variants like ``glm-5.3-flash``.


def _seed_local_roster(tmp_path: Path, *, pm_fallback: bool = True) -> None:
    """Write a local (ollama-cloud) delegation roster into the sandboxed home.

    Mirrors the user's real config: jr-coder gemma4:31b, mid-coder
    deepseek-v4-flash:0731, sr-coder glm-5.3 (fallback claude-opus-5), pm
    glm-5.3 (fallback claude-fable-5), reviewer glm-5.3-flash. ``pm_fallback``
    lets a test drop pm's fallback to exercise the no-fallback guard.
    """
    pm_entry: dict[str, object] = {
        "model": "glm-5.3",
        "provider": "ollama-cloud",
    }
    if pm_fallback:
        pm_entry["fallback"] = {"model": "claude-fable-5", "provider": "anthropic"}
    cfg = {
        "delegation": {
            "model_by_role": {
                "jr-coder": {"model": "gemma4:31b", "provider": "ollama-cloud"},
                "mid-coder": {
                    "model": "deepseek-v4-flash:0731",
                    "provider": "ollama-cloud",
                },
                "sr-coder": {
                    "model": "glm-5.3",
                    "provider": "ollama-cloud",
                    "fallback": {"model": "claude-opus-5", "provider": "anthropic"},
                },
                "pm": pm_entry,
                "reviewer": {"model": "glm-5.3-flash", "provider": "ollama-cloud"},
            }
        }
    }
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _low_success_stats(role: str, model: str, n: int, n_completed: int) -> list:
    """Build ``n`` stats for (role, model) with ``n_completed`` successes."""
    stats = []
    for i in range(n):
        status = "completed" if i < n_completed else "failed"
        stats.append(ds.DelegationStat(role=role, model=model, status=status))
    return stats


def test_local_ladder_ranks_from_role_anchors(monkeypatch, tmp_path):
    """(i) local_ladder resolves ranks from the role anchors + suffix variant."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_roster(tmp_path)
    ladder = mt.local_ladder()
    assert ladder["gemma4:31b"] == 0
    assert ladder["deepseek-v4-flash:0731"] == 1
    assert ladder["glm-5.3"] == 2
    # reviewer's glm-5.3-flash is a suffix variant of glm-5.3 → rank 1.
    assert ladder["glm-5.3-flash"] == 1


def test_local_escalate_pm_glm53_to_fallback(monkeypatch, tmp_path):
    """(ii) pm@glm-5.3 low-success → ESCALATE to pm's configured fallback.

    This is the exact case that silently skipped before (glm-5.3 is not in
    the Anthropic ladder). glm-5.3 is the top of the local ladder, so there
    is no in-ladder promote target — it escalates to claude-fable-5.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_roster(tmp_path)
    stats = _low_success_stats("pm", "glm-5.3", n=7, n_completed=4)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert len(sugs) == 1
    assert sugs[0].direction == "escalate"
    assert sugs[0].suggested_model == "claude-fable-5"
    assert "configured fallback for role 'pm' is claude-fable-5" in sugs[0].reason


def test_local_promote_mid_coder_deepseek_to_glm53(monkeypatch, tmp_path):
    """(iii) mid-coder@deepseek-v4-flash:0731 low-success → PROMOTE to glm-5.3.

    deepseek-v4-flash:0731 is rank 1 in the local ladder; its in-ladder
    neighbor at rank 2 is glm-5.3.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_roster(tmp_path)
    stats = _low_success_stats("mid-coder", "deepseek-v4-flash:0731", n=5, n_completed=3)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert len(sugs) == 1
    assert sugs[0].direction == "promote"
    assert sugs[0].suggested_model == "glm-5.3"


def test_local_promote_reviewer_flash_variant(monkeypatch, tmp_path):
    """(iv) reviewer@glm-5.3-flash resolves at rank 1 (suffix heuristic) and
    promotes to the local rank-2 model (glm-5.3)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_roster(tmp_path)
    stats = _low_success_stats("reviewer", "glm-5.3-flash", n=5, n_completed=3)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert len(sugs) == 1
    assert sugs[0].direction == "promote"
    assert sugs[0].suggested_model == "glm-5.3"


def test_local_escalate_sr_coder_uses_own_fallback(monkeypatch, tmp_path):
    """(v) sr-coder@glm-5.3 escalates to ITS OWN fallback (claude-opus-5),
    never pm's (claude-fable-5)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_roster(tmp_path)
    stats = _low_success_stats("sr-coder", "glm-5.3", n=5, n_completed=3)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert len(sugs) == 1
    assert sugs[0].direction == "escalate"
    assert sugs[0].suggested_model == "claude-opus-5"
    assert "configured fallback for role 'sr-coder' is claude-opus-5" in sugs[0].reason


def test_local_top_of_ladder_no_fallback_no_keyerror(monkeypatch, tmp_path):
    """(vi) A top-of-local-ladder model with NO configured fallback that
    fails the promote rules produces no promote and no escalate — and never
    raises a KeyError (the old code's ``rank_tier[rank + 1]`` would have)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_roster(tmp_path, pm_fallback=False)
    # pm@glm-5.3 is top of the local ladder with no fallback → no escalate,
    # no promote. It may demote per existing rules (it won't here: low
    # success, not cheap_and_clean / expensive_for_size).
    stats = _low_success_stats("pm", "glm-5.3", n=5, n_completed=3)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert all(s.direction != "promote" for s in sugs)
    assert all(s.direction != "escalate" for s in sugs)


def test_local_escalate_guard_requires_primary_model(monkeypatch, tmp_path):
    """(vii) Escalation only fires when agg.model equals the role's PRIMARY
    model. A coincidental same-model different-role must not escalate."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_roster(tmp_path)
    # A role NOT pinned to glm-5.3 as its primary (e.g. reviewer, whose
    # primary is glm-5.3-flash) running glm-5.3 must NOT escalate to
    # reviewer's fallback (reviewer has none anyway) — and must not raise.
    stats = _low_success_stats("reviewer", "glm-5.3", n=5, n_completed=3)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert all(s.direction != "escalate" for s in sugs)


# ── LADDER PURITY: an Anthropic primary must never join the local ladder ──
#
# The live config pins pm to claude-opus-5 (an Anthropic model). Before the
# fix, local_ladder() admitted that primary at rank 2 and — being inserted
# after glm-5.3-flash — stole the rank-2 slot in local_ladder_models(), so
# suggest_retunes() promoted mid-coder@deepseek-v4-flash:0731 to
# claude-opus-5 (a cross-ladder jump) instead of the local rank-2 model.
# These tests pin the invariant: Anthropic models belong exclusively to the
# Anthropic ladder (tier_rank_map()).


def _seed_anthropic_pm_roster(tmp_path: Path) -> None:
    """Mirror the live config: pm pinned to claude-opus-5 (Anthropic),
    sr-coder pinned to glm-5.3-flash (local)."""
    cfg = {
        "delegation": {
            "model_by_role": {
                "jr-coder": {"model": "gemma4:31b", "provider": "ollama-cloud"},
                "mid-coder": {
                    "model": "deepseek-v4-flash:0731",
                    "provider": "ollama-cloud",
                },
                "sr-coder": {
                    "model": "glm-5.3-flash",
                    "provider": "ollama-cloud",
                },
                "pm": {"model": "claude-opus-5", "provider": "anthropic"},
            }
        }
    }
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def test_local_ladder_excludes_anthropic_primary(monkeypatch, tmp_path):
    """(i) An Anthropic anchor primary never joins the local ladder; the
    local rank-2 slot stays with the local model (glm-5.3-flash)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_anthropic_pm_roster(tmp_path)
    ladder = mt.local_ladder()
    assert "claude-opus-5" not in ladder
    assert ladder["glm-5.3-flash"] == 2
    # And the rank-2 slot in the inverse map is the local model, not opus.
    assert mt.local_ladder_models()[2] == "glm-5.3-flash"


def test_local_promote_mid_coder_to_local_rank2_not_anthropic(
    monkeypatch, tmp_path
):
    """(ii) mid-coder@deepseek-v4-flash:0731 promotes to the LOCAL rank-2
    model (glm-5.3-flash), never the Anthropic claude-opus-5."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_anthropic_pm_roster(tmp_path)
    stats = _low_success_stats("mid-coder", "deepseek-v4-flash:0731", n=5, n_completed=3)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    assert len(sugs) == 1
    assert sugs[0].direction == "promote"
    assert sugs[0].suggested_model == "glm-5.3-flash"


def test_anthropic_primary_resolves_to_anthropic_ladder_no_cross_ladder(
    monkeypatch, tmp_path
):
    """(iii) pm@claude-opus-5 resolves to the ANTHROPIC ladder (top rank,
    no in-ladder promote target) and produces NO cross-ladder suggestion —
    the escalate path is local-ladder-only."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_anthropic_pm_roster(tmp_path)
    # claude-opus-5 is the top of the Anthropic ladder (rank 2 = opus).
    assert mt.tier_rank_map()["claude-opus-5"] == 2
    stats = _low_success_stats("pm", "claude-opus-5", n=5, n_completed=3)
    sugs = ds.suggest_retunes(ds.aggregate(stats))
    # No promote (top of ladder), no escalate (escalate is local-only), and
    # no demote (low success, not cheap_and_clean). No cross-ladder jump.
    assert sugs == []
