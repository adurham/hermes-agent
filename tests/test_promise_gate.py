"""Tests for the promise-without-action gate."""

from agent.promise_gate import (
    MAX_PROMISE_GATE_RETRIES,
    looks_like_promise,
    promise_excerpt,
    should_gate,
)


class TestLooksLikePromise:
    def test_first_person_future_work_verbs(self):
        for text in (
            "I'll build the diagnostic mode now.",
            "I will write the page and run it.",
            "I'm going to add the gate to the turn loop.",
            "Let me test that against the local build.",
            "We'll dispatch the worker next.",
            "I'll re-run the sweep.",
        ):
            assert looks_like_promise(text), text

    def test_bare_announcements(self):
        for text in ("building now", "Working on it now.", "on it now"):
            assert looks_like_promise(text), text

    def test_completed_work_is_not_a_promise(self):
        for text in (
            "The gate is in and the tests pass.",
            "I built the diagnostic mode; the file has 34 diag references.",
            "Fixed: the splicer offset was 96, actual 99.",
            "Running on deepseek-v4.1-flash, 6/6 sentinel reads.",
        ):
            assert not looks_like_promise(text), text

    def test_non_work_intentions_are_not_gated(self):
        for text in (
            "I'll be honest, that result surprised me.",
            "Let me know if you want the other payload.",
            "I will say this plainly: the leak is heap-only.",
        ):
            assert not looks_like_promise(text), text

    def test_empty(self):
        assert not looks_like_promise(None)
        assert not looks_like_promise("")
        assert not looks_like_promise("   \n ")


class TestShouldGate:
    def test_gates_a_promise_with_no_tool_calls(self):
        assert should_gate(
            final_text="I'll build the page now.",
            turn_tool_call_count=0,
            retries=0,
            finish_reason="stop",
        )

    def test_never_gates_when_the_turn_acted(self):
        assert not should_gate(
            final_text="I'll build the page now.",
            turn_tool_call_count=3,
            retries=0,
            finish_reason="stop",
        )

    def test_one_retry_per_turn(self):
        assert not should_gate(
            final_text="I'll build the page now.",
            turn_tool_call_count=0,
            retries=MAX_PROMISE_GATE_RETRIES,
            finish_reason="stop",
        )

    def test_ignores_tool_call_finish_reasons(self):
        for reason in ("tool_calls", "content_filter", "error"):
            assert not should_gate(
                final_text="I'll build the page now.",
                turn_tool_call_count=0,
                retries=0,
                finish_reason=reason,
            )

    def test_plain_answers_pass(self):
        assert not should_gate(
            final_text="Six of six loads returned the sentinel.",
            turn_tool_call_count=0,
            retries=0,
            finish_reason="stop",
        )


class TestExcerpt:
    def test_excerpt_is_the_matched_phrase(self):
        excerpt = promise_excerpt("Blah. I'll build the diagnostic mode now. More text.")
        assert excerpt.startswith("I'll build")
        assert len(excerpt) <= 120

    def test_excerpt_empty_when_no_match(self):
        assert promise_excerpt("Nothing promised here.") == ""
