"""The model-facing schema advertises agent_type and model routing fields.

An upstream-sync merge dropped both properties from DELEGATE_TASK_SCHEMA while
the handler (``delegate_task()`` and ``run_agent._dispatch_delegate_task``)
kept accepting them — so passing them worked, but the model was never told the
fields existed and could not use the routing at all.

Contracts asserted here:
  * both fields are advertised at BOTH levels (top level and tasks[] items);
  * they survive ``_build_dynamic_schema_overrides()`` (the real per-call
    definition the model sees), without mutating the static schema;
  * the wording stays model-agnostic — naming a specific vendor's models as
    "examples" is what makes a model reach for a slug that is not a valid
    (model, provider) pair in its own config, which is the whole bug class
    agent_type= exists to prevent.
"""

import json

from tools.delegate_tool import DELEGATE_TASK_SCHEMA, _build_dynamic_schema_overrides
from tools.registry import registry

_STATIC_TOP = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
_STATIC_TASK = _STATIC_TOP["tasks"]["items"]["properties"]

# Vendor-prefixed slugs: a description that names any of these is inviting the
# model to invent a model string instead of routing through its own config.
_VENDOR_SLUGS = ("claude-", "gpt-", "gemini-", "deepseek-", "qwen", "llama", "grok-")


def _definition_fields():
    definition = registry.get_definitions({"delegate_task"})[0]
    top = definition["function"]["parameters"]["properties"]
    return top, top["tasks"]["items"]["properties"]


def test_agent_type_advertised_at_both_levels():
    assert _STATIC_TOP["agent_type"]["type"] == "string"
    assert _STATIC_TASK["agent_type"]["type"] == "string"


def test_model_advertised_at_both_levels():
    assert _STATIC_TOP["model"]["type"] == "string"
    assert _STATIC_TASK["model"]["type"] == "string"


def test_routing_fields_survive_dynamic_overrides():
    dynamic_top, dynamic_task = _definition_fields()

    assert "agent_type" in dynamic_top and "model" in dynamic_top
    assert "agent_type" in dynamic_task and "model" in dynamic_task


def test_dynamic_overrides_do_not_mutate_the_static_schema():
    before = json.dumps(DELEGATE_TASK_SCHEMA)
    _build_dynamic_schema_overrides()
    assert json.dumps(DELEGATE_TASK_SCHEMA) == before


def test_agent_type_description_names_the_routing_contract():
    """The description must say what a role DOES (model + provider + persona)
    and that 'auto' is the explicit opt-in — the facts a model needs to use
    the field correctly, not just its name."""
    for description in (_STATIC_TOP["agent_type"]["description"], _STATIC_TASK["agent_type"]["description"]):
        assert "delegation.model_by_role" in description
        assert "'auto'" in description


def test_descriptions_stay_model_agnostic():
    """No vendor model slugs in the routing fields' own text."""
    for field in ("agent_type", "model"):
        for props in (_STATIC_TOP, _STATIC_TASK):
            description = props[field]["description"].lower()
            for slug in _VENDOR_SLUGS:
                assert slug not in description, f"{field} description names a model slug {slug!r}"


def test_model_description_points_at_agent_type_first():
    """A bare model slug carries no provider; the description must prefer the
    role route. This is the wording that prevents the wrong-endpoint class."""
    for description in (_STATIC_TOP["model"]["description"], _STATIC_TASK["model"]["description"]):
        assert "agent_type" in description
