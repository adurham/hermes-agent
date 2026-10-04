"""A delegated child that fell back must say so in its parent-visible result: live failover
fields (consumed by the async completion event + the ⚠ model label), the recorded fallback
events, and ONE notice line at the top of the summary text the orchestrating LLM reads."""

from __future__ import annotations

from types import SimpleNamespace

from tools.delegate_tool_child_run import _SchemaOutcome, _build_result_entry

_NO_SCHEMA = _SchemaOutcome(None, None, [], 0)
_EVENT = {"from_model": "claude-opus-5-5", "from_provider": "claude-subscription-directsdk-experimental",
          "to_model": "glm-5.3", "to_provider": "ollama-cloud", "cause": "empty_response"}


def _child(**overrides):
    base = dict(model="claude-opus-5-5", provider="claude-subscription-directsdk-experimental",
                _fallback_activated=False, _fallback_events=[],
                _primary_runtime={"model": "claude-opus-5-5",
                                  "provider": "claude-subscription-directsdk-experimental"},
                session_estimated_cost_usd=0.0, session_cost_status="included",
                session_prompt_tokens=10, session_completion_tokens=5)
    base.update(overrides)
    return SimpleNamespace(**base)


def _entry(child, summary="Did the work."):
    result = {"final_response": summary, "messages": [], "api_calls": 3, "completed": True}
    return _build_result_entry(child, result, 0, 4.2, _NO_SCHEMA)


def test_fallen_back_child_reports_live_fields_events_and_notice():
    child = _child(model="glm-5.3", provider="ollama-cloud", _fallback_activated=True,
                   _fallback_events=[dict(_EVENT)])
    entry = _entry(child)

    assert entry["model"] == "glm-5.3"
    assert entry["provider"] == "ollama-cloud"
    assert entry["fallback_active"] is True
    assert entry["primary_model"] == "claude-opus-5-5"
    assert entry["primary_provider"] == "claude-subscription-directsdk-experimental"
    assert "⚠" in entry["model_label"]
    assert entry["fallback_events"] == [_EVENT]
    first, _, rest = entry["summary"].partition("\n\n")
    assert "\n" not in first
    assert first.startswith("[⚠ this subagent fell back claude-opus-5-5 → glm-5.3 (ollama-cloud)")
    assert "empty_response" in first
    assert rest == "Did the work."


def test_completion_label_renders_fallback_marker_from_entry():
    """The async completion path copies these fields onto its event and renders the label
    with _result_model_label — the entry must now carry what it reads."""
    from tools.process_registry import _result_model_label

    entry = _entry(_child(model="glm-5.3", provider="ollama-cloud", _fallback_activated=True,
                          _fallback_events=[dict(_EVENT)]))
    label = _result_model_label(entry)
    assert "⚠" in label and "glm-5.3" in label and "claude-opus-5-5" in label


def test_restored_child_still_notes_the_episode():
    child = _child(_fallback_events=[dict(_EVENT), {**_EVENT, "from_model": "glm-5.3", "to_model": "x",
                                                    "cause": "empty_response"}])
    entry = _entry(child)
    assert entry["fallback_active"] is False
    assert len(entry["fallback_events"]) == 2
    first = entry["summary"].split("\n\n", 1)[0]
    assert "primary was restored" in first and "2 switches" in first


def test_healthy_child_entry_unchanged_except_additive_fields():
    entry = _entry(_child())
    assert entry["summary"] == "Did the work."
    assert "fallback_events" not in entry
    assert entry["fallback_active"] is False
    assert entry["model_label"] == "claude-opus-5-5"


def test_notice_survives_missed_steer_suffix():
    child = _child(model="glm-5.3", provider="ollama-cloud", _fallback_activated=True,
                   _fallback_events=[dict(_EVENT)])
    result = {"final_response": "Done.", "messages": [], "api_calls": 1, "completed": True,
              "pending_steer": "also check X"}
    entry = _build_result_entry(child, result, 0, 1.0, _NO_SCHEMA)
    assert entry["summary"].startswith("[⚠ this subagent fell back")
    assert entry["summary"].endswith("also check X]")
