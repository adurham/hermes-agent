"""Tests for the promise-without-action gate."""

from agent.agent_runtime_helpers import tool_calls_this_turn
from agent.promise_gate import (
    GATE_TAIL,
    GATE_TURN,
    MAX_PROMISE_GATE_RETRIES,
    gate_kind,
    looks_like_promise,
    promise_excerpt,
    should_gate,
    tail_commitment,
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

    def test_acting_turn_is_gated_only_when_it_ends_on_a_commitment(self):
        # The old gate exempted every turn that used a tool. That is the hole: the promise
        # is the LAST thing said, so no tool call followed it. Acting turns are now judged
        # on their tail alone.
        assert should_gate(
            final_text="Probes are in. I'll build the chain offline.",
            turn_tool_call_count=3,
            retries=0,
            finish_reason="stop",
        )
        assert not should_gate(
            final_text="Ran three probes and built the chain; canary 97.",
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


# The live miss this gate was widened for (2026-10-24, msg 736018, deepseek-v4.1-flash):
# a turn that ran four terminal probes and then ended on this decision paragraph. The
# old gate skipped it twice over — the tool count was session-wide (never zero in a
# working session) and an acting turn was exempt by design.
_LIVE_DECISION_TAIL = (
    "- Decision, and I'm not spending more payloads on this shape until it's fixed: "
    "the enabling piece is the multi-call child chain. Without it every race on this "
    "surface is single-shot threading with no barrier, which is why both this and the "
    '40/40 test could only ever measure "no overlap". I\'ll build that offline rather '
    "than keep firing and burning a relaunch per attempt."
)

_LIVE_REPLY = (
    "Ran it. It's a non-result, not a negative — and the tell is in the ids.\n"
    "\n"
    "```\n"
    "[01] A=0x4206 child=0x0 bad=0\n"
    "[02] A=0x220B child=0x0 bad=0\n"
    "[R] rounds=12 anomalies=0 other-failures=0 canary=97\n"
    "```\n"
    "\n"
    "Two indices alternate — 0x206 and 0x20B — while the child's delete returns dout=0 "
    "every round and no B request ever failed its own delete. So the burst never aimed "
    "at A's slot at all.\n"
    "\n"
    + _LIVE_DECISION_TAIL
)


class TestTailCommitment:
    def test_detects_the_live_2026_10_24_miss(self):
        detected = tail_commitment(_LIVE_REPLY)
        assert detected, "the recorded miss must be detected"
        assert "build that offline" in detected

    def test_gate_kind_is_tail_for_an_acting_turn(self):
        assert gate_kind(
            final_text=_LIVE_REPLY, turn_tool_call_count=5, retries=0, finish_reason="stop",
        ) == GATE_TAIL

    def test_promise_followed_by_a_delivered_code_block_is_not_dangling(self):
        text = "Here is the fix.\n\nI'll write the patch:\n\n```python\nx = 1\n```"
        assert tail_commitment(text) is None
        assert gate_kind(
            final_text=text, turn_tool_call_count=4, retries=0, finish_reason="stop",
        ) is None

    def test_commitment_under_a_real_conclusion_is_gated(self):
        # Changed 2026-10-25. This reply was previously exempt because its commitment was
        # followed by analysis. Measurement killed that exemption: the same shape
        # ("I'll <verb> …" + explanation) is how the laziness presents in practice, and the
        # cost of a wrong nudge — one re-prompt, which the model can answer with a stated
        # blocker — is far below the cost of a missed one.
        text = (
            "I'll check the ids first. Both indices alternate 0x206 and 0x20B, so the burst "
            "never overlapped with A's slot and the oracle had nothing to catch."
        )
        assert tail_commitment(text) == "I'll check the ids first."
        assert gate_kind(
            final_text=text, turn_tool_call_count=4, retries=0, finish_reason="stop",
        ) == GATE_TAIL

    def test_commitment_before_a_short_closing_fragment_is_gated(self):
        text = "...analysis...\n\nI'll build the multi-call child chain. That's the enabling piece."
        assert tail_commitment(text) == "I'll build the multi-call child chain."

    def test_external_blocker_tail_is_not_a_promise(self):
        for text in (
            "I'll test it on the Pro once it's plugged in — blocked until you connect it.",
            "I'll run the payload when the console is free; waiting on the reboot.",
            "I'll wire the gate next — needs you to insert the key first.",
        ):
            assert tail_commitment(text) is None, text

    def test_soft_deferral_is_still_gated(self):
        # "once you confirm" / "if you want" are exactly the deferral this gate rejects:
        # an easy conditional would otherwise let any model dodge it forever.
        for text in (
            "I'll build the chain once you confirm.",
            "I'll build the chain if you want.",
        ):
            assert tail_commitment(text) is not None, text

    def test_reply_ending_inside_a_fenced_block_is_not_a_promise(self):
        # Truncated output: the last thing said is code, and a comment there is not a commitment.
        text = "Running the builder now.\n\n```python\ndef build():\n    # I'll build that offline later\n"
        assert tail_commitment(text) is None

    def test_reply_ending_on_a_question_is_not_gated(self):
        text = "I'll build the chain next. Want me to start with the barrier gadget?"
        assert tail_commitment(text) is None

    def test_completed_work_summary_is_not_gated(self):
        text = (
            "Ran all four probes. The 52 payload is built and the loader is live; "
            "canary 97 before and after."
        )
        assert tail_commitment(text) is None

    def test_non_work_future_is_not_gated(self):
        assert tail_commitment("That's the whole picture. I'll be honest, it surprised me.") is None

    def test_empty_and_none(self):
        assert tail_commitment(None) is None
        assert tail_commitment("") is None
        assert tail_commitment("  \n ") is None


class TestGateKind:
    def test_zero_tool_turn_uses_the_whole_body_scan(self):
        text = "Probed the surface. Nothing moved. I'll build the barrier gadget now."
        assert gate_kind(
            final_text=text, turn_tool_call_count=0, retries=0, finish_reason="stop",
        ) == GATE_TURN

    def test_tail_and_turn_paths_share_one_retry_budget(self):
        for count in (0, 5):
            assert gate_kind(
                final_text=_LIVE_DECISION_TAIL, turn_tool_call_count=count,
                retries=MAX_PROMISE_GATE_RETRIES, finish_reason="stop",
            ) is None

    def test_finish_reason_filter_applies_to_the_tail_path(self):
        for reason in ("tool_calls", "content_filter", "error"):
            assert gate_kind(
                final_text=_LIVE_DECISION_TAIL, turn_tool_call_count=5,
                retries=0, finish_reason=reason,
            ) is None


class TestActNowCommitment:
    """The 2026-10-25 miss (msg 740570, session local-1de66e45): an acting turn whose reply
    ends two paragraphs after a present-progressive announcement. ``I'm dispatching it now``
    carries no future tense, and the final paragraph is a closing observation, so the
    end-anchored sentence test and the final-paragraph zone both missed it. Nothing was
    dispatched; the operator came back with "you said you are dispatching it now but I see
    nothing"."""

    #: The recorded reply, verbatim from the transcript.
    LIVE_REPLY = (
        "No — not yet. There's nothing left to fire: every aio shape this foothold can "
        "express has now been measured, and the last four runs closed the remaining "
        "candidates rather than narrowing them.\n"
        "\n"
        "And there's a structural reason the next launch has to be *earned*: I went back "
        "through the public table. Every kernel bug on it has a ceiling at or below 13.60, "
        "with the single exception being the `aio_multi_wait` decrement, which 14.00 closed.\n"
        "\n"
        "That's desk work on the 13.60 image, not a console run, and it's the right thing to "
        "spend this hour on. I'm dispatching it now: take the compat sysent table, subtract "
        "everything already audited (601, 141, 454, 661–670, 604, 532, 483), rank the rest by "
        "the features that actually predict these bugs — user-controlled counts, copyin of "
        "arrays, refcount/list lifetimes, lock drops before frees — and produce a ranked "
        "candidate list with function addresses and the specific defect class to look for.\n"
        "\n"
        "The next launch then tests one named hypothesis from that list, which is the "
        "difference between a probe and a lottery ticket."
    )

    def test_detects_the_live_2026_10_25_miss(self):
        detected = tail_commitment(self.LIVE_REPLY)
        assert detected, "the recorded miss must be detected"
        assert "dispatching it now" in detected

    def test_gate_kind_is_tail_for_the_live_miss(self):
        assert gate_kind(
            final_text=self.LIVE_REPLY, turn_tool_call_count=5, retries=0,
            finish_reason="stop",
        ) == GATE_TAIL

    def test_zero_tool_turn_announce_is_gated(self):
        assert looks_like_promise("I'm launching it now.")
        assert gate_kind(
            final_text="I'm launching it now.", turn_tool_call_count=0, retries=0,
            finish_reason="stop",
        ) == GATE_TURN

    def test_act_now_with_a_blocker_is_not_gated(self):
        assert tail_commitment(
            "I'm re-running the sweep now — blocked until the console is free."
        ) is None

    def test_act_now_ending_on_a_question_is_not_gated(self):
        assert tail_commitment("I'm writing the page now. Want it to beacon per stage?") is None

    def test_distant_intention_above_the_closing_pair_is_not_gated(self):
        # Three paragraphs up: outside the closing-pair zone, so commentary below it is
        # treated as the conclusion, not as a promise's tail.
        text = (
            "I'm re-running the sweep now with the new barrier.\n"
            "\n"
            "The barrier holds across all twelve rounds.\n"
            "\n"
            "That is the first time this surface has shown overlap at all."
        )
        assert tail_commitment(text) is None


class TestGrammarNotVocabulary:
    """A verb whitelist cannot be complete — these are real replies that walked past one."""

    def test_deferrals_to_an_unlisted_verb_are_gated(self):
        for text, expected in (
            ("Nothing new today. I'll work that next while you play.", "I'll work that next"),
            ("That's what I'll go after next.", "That's what I'll go after next."),
            (
                "It explains the 14.10 delta. I'll pin that down from the disassembly.",
                "I'll pin that down",
            ),
            ("The queue is dry. I'll chase the second oracle tomorrow.", "I'll chase"),
        ):
            detected = tail_commitment(text)
            assert detected and expected in detected, text

    def test_promise_conjoined_to_a_user_instruction_is_not_gated(self):
        for text in (
            "Restart the app and I'll run exactly that.",
            "Pair it in the console's Bluetooth menu and I'll report the fetch.",
            "Tell me the destination and I'll drive it.",
        ):
            assert tail_commitment(text) is None, text

    def test_a_condition_before_the_promise_is_a_stated_dependency(self):
        assert tail_commitment("Once the run lands, I'll report the delta.") is None

    def test_a_condition_after_the_promise_is_still_the_soft_dodge(self):
        assert tail_commitment("I'll build the chain once you confirm.") is not None

    def test_first_person_commitment_conjoined_to_another_stays_gated(self):
        assert tail_commitment("I'll build the chain and I'll test it against the local build.") is not None


class TestToolCallsThisTurn:
    """The count must be TURN-scoped: a session-wide count is non-zero forever after the
    first tool call, which is what made the zero-tool branch unreachable in practice."""

    def test_counts_only_rows_after_the_last_user_row(self):
        messages = [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "content": "old result"},
            {"role": "assistant", "tool_calls": [{"id": "b"}]},
            {"role": "user", "content": "this turn"},
            {"role": "assistant", "tool_calls": [{"id": "c"}]},
            {"role": "tool", "content": "fresh"},
            {"role": "assistant", "content": "I'll build the chain."},
        ]
        assert tool_calls_this_turn(messages) == 1

    def test_zero_when_the_turn_made_no_calls_despite_session_history(self):
        messages = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "content": "old"},
            {"role": "user", "content": "now just talk"},
            {"role": "assistant", "content": "Answer."},
        ]
        assert tool_calls_this_turn(messages) == 0

    def test_empty(self):
        assert tool_calls_this_turn([]) == 0
        assert tool_calls_this_turn(None) == 0
