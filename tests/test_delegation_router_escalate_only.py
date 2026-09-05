"""Tests for the ESCALATE-ONLY half of ``tools.delegation_router`` (2026-09-04).

Before this change, a task carrying an explicit ``agent_type`` was excluded
from classification entirely. Now it rides along in the SAME batch classifier
call purely so the classifier's recommended tier can be compared against the
stated role's configured tier rank — and the stated choice is replaced ONLY
when the recommendation ranks strictly higher.

The properties under test:

* **Escalate up** — a deeper recommendation replaces the stated agent_type
  with the recommended tier's role, and carries its provenance
  (``escalated``/``escalated_from``/ranks).
* **Never downgrade** — an equal or lighter recommendation produces NO entry
  at all, so the stated choice reaches dispatch untouched.
* **Both ladders** — rank resolution works for roles pinned to Anthropic
  models (``family_of``/``tier_rank_map``) AND for roles pinned to
  local/ollama-cloud models (``local_ladder``, anchored jr-coder=0 /
  mid-coder=1 / sr-coder=2 / pm=2). The local path is the live-config shape:
  after the 2026-09 config change most roles resolve their PRIMARY model to
  an ollama-cloud slug, so ``family_of()`` returns None for them and rank
  resolution MUST fall through to the local ladder.
* **Fail open** — when a rank can't be resolved for either side, the task is
  left alone (no escalation, no error).
* **model= bypass** — an explicit per-task model is never classified at all,
  so it can be neither auto-routed nor escalated.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml

import tools.delegation_router as dr


# ── Anthropic-ladder role map (roles pinned to claude-* models) ────────────
ANTHROPIC_ROLE_MAP = {
    "researcher": "claude-haiku-4-5",
    "coder": "claude-sonnet-4-6",
    "system-architect": "claude-opus-4-7",
}

# ── Local-ladder role map (the live-config shape: ollama-cloud primaries,
# Anthropic only as a nested fallback). family_of() returns None for every
# primary here, so ranks MUST come from local_ladder(). ────────────────────
LOCAL_ROLE_MAP = {
    "jr-coder": {"model": "gemma4:31b", "provider": "ollama-cloud"},
    "mid-coder": {"model": "deepseek-v4-flash:0731", "provider": "ollama-cloud"},
    "sr-coder": {"model": "glm-5.3", "provider": "ollama-cloud"},
    "pm": {"model": "glm-5.3", "provider": "ollama-cloud"},
}

LOCAL_TIER_ROLES = {
    "light": "jr-coder",
    "standard": "mid-coder",
    "deep": "sr-coder",
}


def route(tasks, *, role_map=None, cfg=None, provider="anthropic"):
    return dr.route_task_models(
        tasks,
        ANTHROPIC_ROLE_MAP if role_map is None else role_map,
        {} if cfg is None else cfg,
        provider,
    )


def _seed_local_config(tmp_path, model_by_role):
    """Write a real config.yaml so model_tiers' live-config ladders resolve.

    ``local_ladder()`` reads ``delegation.model_by_role`` from the LIVE
    config on every call (never a passed-in map), so exercising the local
    ladder requires a real file under the sandboxed HERMES_HOME.
    """
    cfg = {"delegation": {"model_by_role": model_by_role}}
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ── Anthropic ladder: escalate up ─────────────────────────────────────────


def test_explicit_agent_type_escalated_when_classifier_says_deeper():
    """agent_type='researcher' (haiku, rank 0) + a 'deep' recommendation
    (system-architect, opus, rank 2) → escalated to system-architect."""
    tasks = [{"goal": "rewrite the session cookie auth surface", "agent_type": "researcher"}]
    with patch.object(dr, "_classify", return_value={0: ("deep", "auth surface", "")}):
        out = route(tasks)
    assert out[0]["escalated"] is True
    assert out[0]["escalated_from"] == "researcher"
    assert out[0]["agent_type"] == "system-architect"
    assert out[0]["role"] == "system-architect"
    assert out[0]["model"] == "claude-opus-4-7"
    assert out[0]["tier"] == "deep"
    assert out[0]["escalated_from_rank"] == 0
    assert out[0]["rank"] == 2


def test_escalation_is_single_step_when_recommendation_is_one_up():
    """rank 0 stated + 'standard' (sonnet, rank 1) recommendation → rank 1."""
    tasks = [{"goal": "add a bounded unit test", "agent_type": "researcher"}]
    with patch.object(dr, "_classify", return_value={0: ("standard", "bounded", "")}):
        out = route(tasks)
    assert out[0]["agent_type"] == "coder"
    assert out[0]["model"] == "claude-sonnet-4-6"
    assert out[0]["escalated_from_rank"] == 0
    assert out[0]["rank"] == 1


# ── Anthropic ladder: NEVER downgrade ─────────────────────────────────────


def test_explicit_agent_type_not_downgraded_when_classifier_says_lighter():
    """agent_type='system-architect' (opus, rank 2) + a 'light' (haiku,
    rank 0) recommendation → NO entry, stated choice survives untouched."""
    tasks = [{"goal": "look up a constant", "agent_type": "system-architect"}]
    with patch.object(dr, "_classify", return_value={0: ("light", "lookup", "")}):
        out = route(tasks)
    assert out == {}


def test_explicit_agent_type_not_changed_when_classifier_says_equal():
    """Equal rank is not an escalation — no entry, no change."""
    tasks = [{"goal": "bounded refactor", "agent_type": "coder"}]
    with patch.object(dr, "_classify", return_value={0: ("standard", "bounded", "")}):
        out = route(tasks)
    assert out == {}


def test_top_of_ladder_stated_choice_never_escalates():
    """A stated role already at the top rank can only ever be matched."""
    tasks = [{"goal": "design a new subsystem", "agent_type": "system-architect"}]
    with patch.object(dr, "_classify", return_value={0: ("deep", "design", "")}):
        out = route(tasks)
    assert out == {}


# ── Local (ollama-cloud) ladder — the live-config shape ───────────────────


def test_local_ladder_escalation_ollama_cloud_models(monkeypatch, tmp_path):
    """The critical non-Anthropic path: every role's PRIMARY model is an
    ollama-cloud slug, so family_of() is None for all of them and ranks MUST
    come from local_ladder() (jr-coder=0 / mid-coder=1 / sr-coder=2).

    agent_type='jr-coder' (gemma4:31b, local rank 0) + a 'deep'
    recommendation (sr-coder, glm-5.3, local rank 2) → escalated.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_config(tmp_path, LOCAL_ROLE_MAP)
    # Precondition: the Anthropic ladder genuinely cannot rank these.
    from hermes_cli import model_tiers as mt

    assert mt.family_of("gemma4:31b") is None
    assert mt.family_of("glm-5.3") is None

    cfg = {"auto_route": {"tier_roles": LOCAL_TIER_ROLES}}
    tasks = [{"goal": "redesign the token refresh flow", "agent_type": "jr-coder"}]
    with patch.object(dr, "_classify", return_value={0: ("deep", "security surface", "")}):
        out = route(tasks, role_map=LOCAL_ROLE_MAP, cfg=cfg)
    assert out[0]["escalated"] is True
    assert out[0]["escalated_from"] == "jr-coder"
    assert out[0]["agent_type"] == "sr-coder"
    assert out[0]["model"] == "glm-5.3"
    assert out[0]["escalated_from_rank"] == 0
    assert out[0]["rank"] == 2


def test_local_ladder_never_downgrades(monkeypatch, tmp_path):
    """Same local roster: sr-coder (rank 2) + a 'light' (jr-coder, rank 0)
    recommendation must NOT demote."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_config(tmp_path, LOCAL_ROLE_MAP)
    cfg = {"auto_route": {"tier_roles": LOCAL_TIER_ROLES}}
    tasks = [{"goal": "grep for a symbol", "agent_type": "sr-coder"}]
    with patch.object(dr, "_classify", return_value={0: ("light", "lookup", "")}):
        out = route(tasks, role_map=LOCAL_ROLE_MAP, cfg=cfg)
    assert out == {}


# ── Fail-open when a rank can't be resolved ───────────────────────────────


def test_unrankable_stated_role_fails_open(monkeypatch, tmp_path):
    """The STATED role's model is on neither ladder → no escalation, no
    error, stated choice kept."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_config(tmp_path, LOCAL_ROLE_MAP)
    role_map = dict(LOCAL_ROLE_MAP)
    # An anchor-less, non-Anthropic model: not in local_ladder() (no anchor
    # role resolves to it) and family_of() is None.
    role_map["oddball"] = {"model": "some-unknown-model-9000", "provider": "x"}
    cfg = {"auto_route": {"tier_roles": LOCAL_TIER_ROLES}}
    tasks = [{"goal": "design an auth subsystem", "agent_type": "oddball"}]
    with patch.object(dr, "_classify", return_value={0: ("deep", "auth", "")}):
        out = route(tasks, role_map=role_map, cfg=cfg)
    assert out == {}


def test_unrankable_recommended_role_fails_open(monkeypatch, tmp_path):
    """The RECOMMENDED tier's role has a model, but one on neither ladder →
    fail open rather than guess a rank."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_local_config(tmp_path, LOCAL_ROLE_MAP)
    role_map = dict(LOCAL_ROLE_MAP)
    role_map["weird-deep"] = {"model": "some-unknown-model-9000", "provider": "x"}
    cfg = {
        "auto_route": {
            "tier_roles": {**LOCAL_TIER_ROLES, "deep": "weird-deep"},
        }
    }
    tasks = [{"goal": "design an auth subsystem", "agent_type": "jr-coder"}]
    with patch.object(dr, "_classify", return_value={0: ("deep", "auth", "")}):
        out = route(tasks, role_map=role_map, cfg=cfg)
    assert out == {}


def test_unknown_stated_role_with_no_model_entry_fails_open():
    """A stated agent_type with no model_by_role entry at all is unrankable
    → fail open (this is the common 'persona has no pin' case)."""
    tasks = [{"goal": "design an auth subsystem", "agent_type": "no-such-role"}]
    with patch.object(dr, "_classify", return_value={0: ("deep", "auth", "")}):
        out = route(tasks)
    assert out == {}


# ── model= bypasses classification entirely (fix #3 invariant) ────────────


def test_explicit_model_never_classified():
    """A task with an explicit model= is in NEITHER population: it is not
    even sent to the classifier, so it can be neither auto-routed nor
    escalated."""
    tasks = [
        {"goal": "pinned work", "model": "claude-opus-5"},
        {"goal": "pinned work with a role", "model": "claude-opus-5", "agent_type": "researcher"},
        {"goal": "unpinned work"},
    ]
    with patch.object(dr, "_classify", return_value={2: ("deep", "x", "")}) as m:
        out = route(tasks)
    (pending_arg,) = m.call_args.args
    assert [idx for idx, _ in pending_arg] == [2], pending_arg
    assert 0 not in out and 1 not in out


def test_all_tasks_have_models_skips_classifier_call():
    """Every task pinned → no classifier call at all."""
    tasks = [{"goal": "a", "model": "m1"}, {"goal": "b", "model": "m2", "agent_type": "coder"}]
    with patch.object(dr, "_classify") as m:
        assert route(tasks) == {}
    m.assert_not_called()


# ── One call for both populations ─────────────────────────────────────────


def test_escalate_and_autoroute_share_one_classifier_call():
    """A batch mixing bare tasks and agent_type'd tasks must produce exactly
    ONE classifier call covering both — no second API round-trip."""
    tasks = [
        {"goal": "bare task, route me fully"},
        {"goal": "stated task, escalate-check me", "agent_type": "researcher"},
    ]
    with patch.object(
        dr,
        "_classify",
        return_value={0: ("standard", "bounded", ""), 1: ("deep", "auth", "")},
    ) as m:
        out = route(tasks)
    assert m.call_count == 1
    (pending_arg,) = m.call_args.args
    assert sorted(idx for idx, _ in pending_arg) == [0, 1]
    # Task 0: full auto-route (no escalation marker).
    assert out[0]["model"] == "claude-sonnet-4-6"
    assert "escalated" not in out[0]
    # Task 1: escalated from researcher (rank 0) to system-architect (rank 2).
    assert out[1]["escalated"] is True
    assert out[1]["escalated_from"] == "researcher"


# ── agent_type="auto" is a full-route opt-in, not a persona ───────────────


@pytest.mark.parametrize("value", ["auto", "AUTO", " Auto "])
def test_auto_agent_type_is_fully_routed_like_omission(value):
    """agent_type='auto' (any case/whitespace) joins the FULL auto-route
    population, exactly like omitting the field — not the escalate-check
    population, and never looked up as a persona."""
    tasks = [{"goal": "some task needing a real decision", "agent_type": value}]
    with patch.object(dr, "_classify", return_value={0: ("deep", "design", "")}) as m:
        out = route(tasks)
    (pending_arg,) = m.call_args.args
    assert [idx for idx, _ in pending_arg] == [0]
    assert out[0]["model"] == "claude-opus-4-7"
    assert out[0]["role"] == "system-architect"
    # Full routing, not an escalation of a stated choice.
    assert "escalated" not in out[0]


def test_auto_constant_matches_documented_literal():
    """The tool's fallback literal and the router constant must agree."""
    assert dr.AUTO_AGENT_TYPE == "auto"


# ── escalate_only config toggle ───────────────────────────────────────────


def test_escalate_only_disabled_restores_old_exclusion():
    """delegation.auto_route.escalate_only: false → tasks with an explicit
    agent_type are excluded from classification entirely (pre-2026-09-04)."""
    tasks = [{"goal": "design an auth subsystem", "agent_type": "researcher"}]
    cfg = {"auto_route": {"escalate_only": False}}
    with patch.object(dr, "_classify") as m:
        assert route(tasks, cfg=cfg) == {}
    m.assert_not_called()


def test_escalate_only_disabled_still_auto_routes_bare_tasks():
    """Disabling escalate_only must not disable full auto-route."""
    tasks = [{"goal": "bare task"}, {"goal": "stated", "agent_type": "researcher"}]
    cfg = {"auto_route": {"escalate_only": False}}
    with patch.object(dr, "_classify", return_value={0: ("deep", "x", "")}) as m:
        out = route(tasks, cfg=cfg)
    (pending_arg,) = m.call_args.args
    assert [idx for idx, _ in pending_arg] == [0]
    assert out[0]["model"] == "claude-opus-4-7"
    assert 1 not in out


# ── Rank helpers ──────────────────────────────────────────────────────────


def test_model_rank_recognizes_dated_anthropic_variants():
    """A dated id (claude-haiku-4-5-20251001) is not the roster-current
    literal but must still rank via family_of()."""
    anthropic, local = dr._rank_maps()
    assert dr._model_rank("claude-haiku-4-5-20251001", anthropic, local) == 0
    assert dr._model_rank("anthropic/claude-opus-4-7", anthropic, local) == 2


def test_model_rank_returns_none_for_unknown():
    assert dr._model_rank("some-unknown-model-9000", {}, {}) is None
    assert dr._model_rank("", {}, {}) is None


def test_role_rank_none_for_missing_role():
    assert dr._role_rank("", ANTHROPIC_ROLE_MAP, {}, {}) is None
    assert dr._role_rank("nope", ANTHROPIC_ROLE_MAP, {}, {}) is None
