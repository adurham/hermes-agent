"""Curator review pass: an empty model reply must never be recorded as a completed review,
and the configured ``auxiliary.curator.fallback`` must actually be used exactly once.

Regression cover for the two weekly passes (2026-10-21, 2026-10-28) that returned nothing on a
subscription route: the explainer text became the run's "LLM final summary", llm_error stayed None,
and no fallback was attempted.
"""

import run_agent

from agent import curator

EXPLAINER = "⚠️ No reply: claude-sonnet-5-5 didn't produce a reply this time, even after retries."


def _empty_result(api_calls=4):
    return {"final_response": EXPLAINER, "turn_exit_reason": "empty_response_exhausted", "api_calls": api_calls}


def _write_call(action="create"):
    return {"tool_calls": [{"id": "tc-1", "function": {"name": "skill_manage", "arguments": f'{{"action": "{action}"}}'}}]}


def _install(monkeypatch, results):
    """results: list of (conv_result, session_messages) consumed one per attempted run."""
    queue = list(results)
    seen = []

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            self.provider = kwargs.get("provider")
            self.model = kwargs.get("model")
            self._session_messages = []
            self.session_estimated_cost_usd = 0.0
            self._memory_nudge_interval = 0
            self._skill_nudge_interval = 0
            self._memory_write_origin = None
            seen.append((self.provider, self.model))

        def run_conversation(self, user_message=None, **_kwargs):
            entry = queue.pop(0)
            if isinstance(entry, Exception):
                raise entry
            conv, messages = entry
            self._session_messages = messages
            return conv

        def close(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        curator, "_resolve_review_provider",
        lambda route=None: ({"api_key": "k"},
                            route.model if route else "primary-model",
                            route.provider if route else "primary-provider", {}),
    )
    return seen


def _fallback():
    return curator._ReviewRuntimeBinding("fb-provider", "fb-model", None, None, {})


def test_empty_primary_retries_once_on_the_fallback_route(monkeypatch):
    seen = _install(monkeypatch, [(_empty_result(), []),
                                  ({"final_response": "consolidated 3 umbrellas", "api_calls": 7}, [])])
    monkeypatch.setattr(curator, "_resolve_review_fallback_binding", _fallback)

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 2, seen
    assert seen[0] == ("primary-provider", "primary-model")
    assert seen[1] == ("fb-provider", "fb-model")
    assert meta["fallback_attempted"] is True
    assert meta["fallback_from"] == "primary-provider/primary-model"
    assert meta["error"] is None
    assert meta["final"] == "consolidated 3 umbrellas"
    assert len(meta["attempts"]) == 2
    # the explainer is UI copy, not a review summary
    assert "produce a reply" not in meta["summary"]


def test_writes_in_the_first_pass_block_the_retry(monkeypatch):
    seen = _install(monkeypatch, [(_empty_result(), [_write_call()])])
    monkeypatch.setattr(curator, "_resolve_review_fallback_binding", _fallback)

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 1, "a pass that already wrote must never be re-run"
    assert meta["fallback_attempted"] is False
    assert meta["fallback_skipped_reason"] == "mutations_present"
    assert meta["error"]
    assert meta["final"] == ""


def test_unknown_mutation_state_blocks_the_retry(monkeypatch):
    # a run that dies before its tool calls can be inspected must not be read as "wrote nothing"
    seen = _install(monkeypatch, [RuntimeError("transport died")])
    monkeypatch.setattr(curator, "_resolve_review_fallback_binding", _fallback)

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 1
    assert meta["fallback_skipped_reason"] == "mutations_unknown"
    assert "transport died" in meta["error"]


def test_both_routes_empty_is_a_hard_failure(monkeypatch):
    seen = _install(monkeypatch, [(_empty_result(), []), (_empty_result(), [])])
    monkeypatch.setattr(curator, "_resolve_review_fallback_binding", _fallback)

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 2
    assert "empty_response" in meta["error"]
    assert meta["final"] == ""
    assert "produce a reply" not in meta["summary"]


def test_normal_completion_does_not_touch_the_fallback(monkeypatch):
    seen = _install(monkeypatch, [({"final_response": "nothing to do", "api_calls": 2}, [])])
    monkeypatch.setattr(curator, "_resolve_review_fallback_binding", _fallback)

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 1
    assert "fallback_attempted" not in meta
    assert meta["error"] is None
    assert meta["summary"] == "nothing to do"


def test_budget_exhausted_skips_the_retry(monkeypatch):
    seen = _install(monkeypatch, [(_empty_result(), [])])
    monkeypatch.setattr(curator, "_resolve_review_fallback_binding", _fallback)
    monkeypatch.setattr(curator, "_CURATOR_REVIEW_BUDGET_SECONDS", 0.0)

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 1
    assert meta["fallback_skipped_reason"] == "budget_exhausted"
    assert meta["error"]


def test_no_fallback_configured_is_reported(monkeypatch):
    seen = _install(monkeypatch, [(_empty_result(), [])])
    monkeypatch.setattr(curator, "_resolve_review_fallback_binding", lambda: None)

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 1
    assert meta["fallback_skipped_reason"] == "no_fallback_configured"
    assert "empty_response" in meta["error"]


def test_same_route_fallback_is_not_retried(monkeypatch):
    seen = _install(monkeypatch, [(_empty_result(), [])])
    monkeypatch.setattr(
        curator, "_resolve_review_fallback_binding",
        lambda: curator._ReviewRuntimeBinding("primary-provider", "primary-model", None, None, {}),
    )

    meta = curator._run_llm_review("prompt")

    assert len(seen) == 1
    assert meta["fallback_skipped_reason"] == "fallback_same_as_primary"
