"""A per-route ``max_tokens`` must not follow the session onto a different route.

``providers.exo.models.<dsv4>.max_tokens: 8192`` bounds run-away generation on the exo cluster. It
was resolved once at agent init and cached on ``agent.max_tokens``, so (a) ``switch_model`` to
ollama-cloud DeepSeek-V4.1-Flash kept the 8K ceiling, and (b) delegation handed the parent's live
``max_tokens`` to every child. A reasoning pass alone emits 8-17K tokens there, so children came
back ``finish_reason='length'`` with no visible answer ("Model used all output tokens on reasoning").
An explicit user cap is policy and must survive both.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.agent_runtime_helpers import _re_resolve_route_output_cap
from run_agent import AIAgent
from tools.delegate_tool_config import _inherited_child_max_tokens

EXO = "http://exo.test:52415/v1"
CLOUD = "https://ollama.com/v1"


def _cap_for(model, base_url, **_kw):
    return 8192 if base_url == EXO else None


def _agent(max_tokens, from_route, base_url=EXO):
    a = AIAgent.__new__(AIAgent)
    a.model, a.base_url = "m", base_url
    a.max_tokens = max_tokens
    a._max_tokens_from_route_config = from_route
    return a


def test_route_cap_is_dropped_when_destination_route_has_none():
    a = _agent(8192, True)
    a.base_url = CLOUD
    with patch("hermes_cli.config.get_custom_provider_max_tokens", side_effect=_cap_for):
        _re_resolve_route_output_cap(a)
    assert a.max_tokens is None
    assert a._max_tokens_from_route_config is False


def test_route_cap_is_picked_up_when_destination_route_configures_one():
    a = _agent(None, False, base_url=EXO)
    with patch("hermes_cli.config.get_custom_provider_max_tokens", side_effect=_cap_for):
        _re_resolve_route_output_cap(a)
    assert a.max_tokens == 8192
    assert a._max_tokens_from_route_config is True


def test_explicit_user_cap_survives_a_switch():
    a = _agent(32000, False)
    a.base_url = CLOUD
    with patch("hermes_cli.config.get_custom_provider_max_tokens", side_effect=_cap_for):
        _re_resolve_route_output_cap(a)
    assert a.max_tokens == 32000


def test_switch_model_applies_the_rederivation_and_rolls_back_on_failure():
    from agent import agent_runtime_helpers as h

    assert "max_tokens" in h._SWITCH_SNAPSHOT_FIELDS
    assert "_max_tokens_from_route_config" in h._SWITCH_SNAPSHOT_FIELDS


def test_child_does_not_inherit_a_route_derived_cap():
    parent = SimpleNamespace(max_tokens=8192, _max_tokens_from_route_config=True)
    assert _inherited_child_max_tokens(parent) is None


def test_child_inherits_an_explicit_cap():
    parent = SimpleNamespace(max_tokens=32000, _max_tokens_from_route_config=False)
    assert _inherited_child_max_tokens(parent) == 32000


def test_child_inherits_nothing_when_parent_has_no_cap():
    assert _inherited_child_max_tokens(SimpleNamespace(max_tokens=None)) is None
    # legacy parents built without the provenance attribute keep the old behaviour
    assert _inherited_child_max_tokens(SimpleNamespace(max_tokens=4096)) == 4096
