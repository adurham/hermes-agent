"""FORK regression: a ``medium`` effort ask must never be silently downgraded on the Nous
DeepSeek route.

The Portal's ``/v1/models`` catalog publishes only ``max/xhigh/high/low`` for the DeepSeek
V4 family, so the generic nearest-weaker clamp turned every ``medium`` aux ask into ``low``
(a two-tier drop on approval, memory extraction, the delegation router and eight fallbacks —
2026-11-12). The declared vendor override now rounds ``medium`` UP to ``high``, matching the
model's real three-tier thinking knob, and stays a no-op on DeepSeek's own wire where
``medium`` is a supported level.
"""

import pytest

from agent.reasoning_effort import (
    DEEPSEEK_V4_EFFORTS,
    DEEPSEEK_V4_OVERRIDES,
    clamp_effort,
)
from providers import get_provider_profile

# Live Portal catalog for deepseek/deepseek-v4.1-flash-1017 (probed 2026-11-12).
NOUS_DSV4_CATALOG = ("max", "xhigh", "high", "low")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("low", "low"), ("medium", "high"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max")],
)
def test_catalog_ladder_plus_overrides_never_lands_below_intent(requested, expected):
    assert clamp_effort(requested, NOUS_DSV4_CATALOG, DEEPSEEK_V4_OVERRIDES) == expected


def test_direct_deepseek_wire_still_sends_medium_verbatim():
    """The override is only consulted when the level is unsupported — DeepSeek's own route
    supports ``medium``, so adding it must not change that wire's behavior."""
    assert "medium" in DEEPSEEK_V4_EFFORTS
    assert clamp_effort("medium", DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES) == "medium"


def test_nous_profile_applies_overrides_when_catalog_supplies_the_set(monkeypatch):
    from hermes_cli import models_reasoning_caps

    monkeypatch.setattr(
        models_reasoning_caps,
        "nous_model_reasoning_capabilities",
        lambda model: {"supports_reasoning": True, "supported_efforts": list(NOUS_DSV4_CATALOG)},
    )
    profile = get_provider_profile("nous")
    assert profile._wire_effort_for_model("medium", "deepseek/deepseek-v4.1-flash-1017") == "high"
    assert profile._wire_effort_for_model("low", "deepseek/deepseek-v4.1-flash-1017") == "low"
    assert profile._wire_effort_for_model("xhigh", "deepseek/deepseek-v4.1-flash-1017") == "xhigh"


def test_nous_profile_clamps_unknown_catalog_level_onto_the_ladder(monkeypatch):
    """A route whose catalog is narrower than the vendor ladder must still emit a level the
    catalog declares (never an out-of-vocabulary value the Portal would drop)."""
    from hermes_cli import models_reasoning_caps

    monkeypatch.setattr(
        models_reasoning_caps,
        "nous_model_reasoning_capabilities",
        lambda model: {"supports_reasoning": True, "supported_efforts": ["high", "max"]},
    )
    profile = get_provider_profile("nous")
    assert profile._wire_effort_for_model("medium", "deepseek/deepseek-v4.1-flash-1017") == "high"
    assert profile._wire_effort_for_model("low", "deepseek/deepseek-v4.1-flash-1017") in ("high", "max")


def test_nous_glm_flash_medium_rounds_up_too(monkeypatch):
    """glm-5.3-flash has the same catalog hole (low/high/max, no medium) — same round-up."""
    from hermes_cli import models_reasoning_caps

    monkeypatch.setattr(
        models_reasoning_caps,
        "nous_model_reasoning_capabilities",
        lambda model: {"supports_reasoning": True, "supported_efforts": ["max", "high", "low"]},
    )
    profile = get_provider_profile("nous")
    assert profile._wire_effort_for_model("medium", "z-ai/glm-5.3-flash") == "high"
    assert profile._wire_effort_for_model("xhigh", "z-ai/glm-5.3-flash") == "max"
