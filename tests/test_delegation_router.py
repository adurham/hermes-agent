"""Tests for tools.delegation_router — auto-routing delegated tasks to the
right model tier.

Covers the fail-open guards (feature disabled, wrong provider, no role→model
map, all-explicit tasks) that short-circuit before any LLM call, plus the
end-to-end tier→role→model resolution with the classifier mocked, and the
classifier-reply parser's tolerance of fenced/garbage output.
"""

from __future__ import annotations

from unittest.mock import patch

import tools.delegation_router as dr


ROLE_MAP = {
    "researcher": "claude-haiku-4-5",
    "coder": "claude-sonnet-4-6",
    "system-architect": "claude-opus-4-7",
}


# -- Fail-open guards (no classifier call) --


def test_disabled_returns_empty():
    assert route([{"goal": "x"}], cfg={"auto_route": {"enabled": False}}) == {}


def test_wrong_provider_returns_empty():
    assert route([{"goal": "x"}], provider="openrouter") == {}


def test_provider_case_insensitive():
    with patch.object(dr, "_classify", return_value={0: ("standard", "r")}):
        out = route([{"goal": "x"}], provider="Anthropic")
    assert out and out[0]["model"] == "claude-sonnet-4-6"


def test_no_role_models_returns_empty():
    assert route([{"goal": "x"}], role_map={}) == {}


def test_all_tasks_explicit_returns_empty():
    tasks = [{"goal": "a", "model": "foo"}, {"goal": "b", "agent_type": "coder"}]
    assert route(tasks) == {}


def test_classifier_failure_fails_open():
    with patch.object(dr, "_classify", return_value={}):
        assert route([{"goal": "x"}]) == {}


# -- End-to-end routing (classifier mocked) --


def test_routes_each_tier_to_its_model():
    tiers = {0: ("light", "lookup"), 1: ("standard", "bounded"), 2: ("deep", "auth")}
    tasks = [{"goal": "find X"}, {"goal": "fix Y"}, {"goal": "design Z auth"}]
    with patch.object(dr, "_classify", return_value=tiers):
        out = route(tasks)
    assert out[0]["model"] == "claude-haiku-4-5"
    assert out[0]["tier"] == "light"
    assert out[1]["model"] == "claude-sonnet-4-6"
    assert out[2]["model"] == "claude-opus-4-7"
    assert out[2]["role"] == "system-architect"
    assert out[2]["reason"] == "auth"


def test_explicit_task_excluded_from_routing():
    tasks = [{"goal": "a", "model": "pinned"}, {"goal": "b"}]
    with patch.object(dr, "_classify", return_value={1: ("deep", "x")}) as m:
        out = route(tasks)
    (pending_arg,) = m.call_args.args
    assert [idx for idx, _ in pending_arg] == [1]
    assert 0 not in out
    assert out[1]["model"] == "claude-opus-4-7"


def test_unmapped_role_fails_open_for_that_task():
    cfg = {"auto_route": {"tier_roles": {"deep": "nonexistent-role"}}}
    with patch.object(dr, "_classify", return_value={0: ("deep", "x")}):
        out = route([{"goal": "x"}], cfg=cfg)
    assert out == {}


def test_hallucinated_index_ignored():
    with patch.object(dr, "_classify", return_value={5: ("deep", "x")}):
        out = route([{"goal": "x"}])
    assert out == {}


def test_custom_tier_roles_override():
    cfg = {"auto_route": {"tier_roles": {"standard": "researcher"}}}
    with patch.object(dr, "_classify", return_value={0: ("standard", "x")}):
        out = route([{"goal": "x"}], cfg=cfg)
    assert out[0]["model"] == "claude-haiku-4-5"


def test_router_never_raises_on_internal_error():
    with patch.object(dr, "_classify", side_effect=RuntimeError("boom")):
        assert route([{"goal": "x"}]) == {}


# -- Classifier reply parser --


def test_parse_bare_array():
    out = dr._parse_classifier_json('[{"index":0,"tier":"deep"}]')
    assert out == [{"index": 0, "tier": "deep"}]


def test_parse_fenced_array():
    text = 'Here:\n```json\n[{"index":0,"tier":"light"}]\n```'
    assert dr._parse_classifier_json(text) == [{"index": 0, "tier": "light"}]


def test_parse_garbage_returns_none():
    assert dr._parse_classifier_json("no json at all") is None
    assert dr._parse_classifier_json("") is None


# -- Helpers --


def route(tasks, *, role_map=None, cfg=None, provider="anthropic"):
    return dr.route_task_models(
        tasks,
        ROLE_MAP if role_map is None else role_map,
        {} if cfg is None else cfg,
        provider,
    )
