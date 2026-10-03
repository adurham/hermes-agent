"""resolve_reasoning_config() honors agent.reasoning_effort_by_model (fork per-model pin).

The fork's UI writes per-model reasoning effort into ``agent.reasoning_effort_by_model``
(cli.py ``/effort`` + model-switch memory). cli.py reads it directly, but every other surface
(switch_model, fallback re-resolution, tui_gateway, gateway, cron) goes through
``hermes_constants.resolve_reasoning_config`` — so without a by-model consultation there, a pin
is silently clobbered back to the global on those paths.
"""

from hermes_constants import resolve_reasoning_config

EXO_MODEL = "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw"


def _cfg(by_model=None, overrides=None, global_effort="ultra"):
    agent = {"reasoning_effort": global_effort}
    if by_model is not None:
        agent["reasoning_effort_by_model"] = by_model
    if overrides is not None:
        agent["reasoning_overrides"] = overrides
    return {"agent": agent}


def test_by_model_exact_match_wins():
    cfg = _cfg(by_model={EXO_MODEL: "xhigh"})
    assert resolve_reasoning_config(cfg, EXO_MODEL) == {"enabled": True, "effort": "xhigh"}


def test_by_model_case_insensitive_key_upper():
    cfg = _cfg(by_model={EXO_MODEL.upper(): "xhigh"})
    assert resolve_reasoning_config(cfg, EXO_MODEL) == {"enabled": True, "effort": "xhigh"}


def test_by_model_case_insensitive_model_upper():
    cfg = _cfg(by_model={EXO_MODEL: "xhigh"})
    assert resolve_reasoning_config(cfg, EXO_MODEL.upper()) == {"enabled": True, "effort": "xhigh"}


def test_by_model_beats_overrides_and_global():
    cfg = _cfg(
        by_model={EXO_MODEL: "xhigh"},
        overrides={EXO_MODEL: "low"},
        global_effort="ultra",
    )
    assert resolve_reasoning_config(cfg, EXO_MODEL) == {"enabled": True, "effort": "xhigh"}


def test_no_match_overrides_beats_global():
    cfg = _cfg(
        by_model={"some-other-model": "xhigh"},
        overrides={"deepseek-v4.1-flash": "low"},
        global_effort="ultra",
    )
    assert resolve_reasoning_config(cfg, "deepseek-v4.1-flash") == {"enabled": True, "effort": "low"}


def test_no_match_no_overrides_falls_to_global():
    cfg = _cfg(by_model={"some-other-model": "xhigh"}, global_effort="ultra")
    assert resolve_reasoning_config(cfg, EXO_MODEL) == {"enabled": True, "effort": "ultra"}


def test_malformed_by_model_value_falls_through_to_global():
    cfg = _cfg(by_model={EXO_MODEL: "hgih"}, global_effort="ultra")
    assert resolve_reasoning_config(cfg, EXO_MODEL) == {"enabled": True, "effort": "ultra"}


def test_non_dict_by_model_ignored_falls_to_global():
    cfg = _cfg(by_model=["not", "a", "dict"], global_effort="ultra")
    assert resolve_reasoning_config(cfg, EXO_MODEL) == {"enabled": True, "effort": "ultra"}
