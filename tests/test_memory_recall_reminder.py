"""Tests for the memory-recall reminder feature (Phase 1 of the
2026-05-19 plan).

The reminder mirrors the existing skill-recall reminder
(``agent/fork/skill_recall.py``): after every Nth tool call, inject a
one-line nudge into the tool result asking the agent to call
``memory(action='recall', query=...)``. Optionally, in ``"auto"`` mode,
the harness extracts a query candidate from the last user message + recent
tool args, runs the recall itself, and includes the top hits in the
nudge so the agent doesn't have to come up with a query cold.

We exercise the helpers in ``agent.fork.memory_recall`` directly — they
are self-contained and only touch attributes on a stub agent object.
This mirrors how ``tests/test_skill_recall_reminder.py`` works.
"""

from __future__ import annotations

import pytest

from agent.fork import memory_recall


class FakeAgent:
    """Stand-in for the AIAgent attributes the helpers touch."""

    def __init__(
        self,
        *,
        interval: int = 8,
        mode: str = "hint",
        auto_top_k: int = 3,
        min_user_chars: int = 200,
        has_warm_facts: bool = True,
        last_user_message: str = "",
    ):
        self._memory_recall_reminder_interval = interval
        self._memory_recall_reminder_mode = mode
        self._memory_recall_auto_top_k = auto_top_k
        self._memory_recall_min_user_chars = min_user_chars
        self._turns_since_memory_recall = 0
        # The reminder uses _last_user_message to seed the query; tests
        # provide a fixture-y value.
        self._last_user_message = last_user_message
        # Stubbed warm count — the reminder skips if warm is empty.
        self._fake_warm_count = 1 if has_warm_facts else 0
        # Last recent tool args (a deque-like, but tests just pass a list).
        self._recent_tool_args = []


# ---------------------------------------------------------------------------
# extract_query_candidate
# ---------------------------------------------------------------------------


class TestExtractQueryCandidate:
    def test_returns_none_for_empty_input(self):
        assert memory_recall.extract_query_candidate("", []) is None
        assert memory_recall.extract_query_candidate("   ", []) is None

    def test_extracts_proper_nouns(self):
        msg = "NEC reported a CDN issue with Tanium Schedule"
        q = memory_recall.extract_query_candidate(msg, [])
        assert q is not None
        # Proper nouns should be in the query.
        assert "NEC" in q
        assert "Tanium" in q
        assert "Schedule" in q

    def test_extracts_long_lowercase_words(self):
        # >=6 chars lowercase counts as a noun candidate.
        msg = "investigating customer regression in clustering"
        q = memory_recall.extract_query_candidate(msg, [])
        assert q is not None
        # The 6+ char words are all candidates.
        assert "investigating" in q or "customer" in q or "regression" in q or "clustering" in q

    def test_extracts_codes_with_digits(self):
        # AIDEV-72 should survive as a token.
        msg = "AIDEV-72 reproduce the kernel issue"
        q = memory_recall.extract_query_candidate(msg, [])
        assert q is not None
        assert "AIDEV" in q

    def test_drops_stopwords(self):
        msg = "the and but for with from this that these those"
        q = memory_recall.extract_query_candidate(msg, [])
        # All of these are stopwords — should yield no candidate.
        assert q is None

    def test_limits_to_five_tokens(self):
        msg = (
            "AlphaOne BetaTwo GammaThree DeltaFour EpsilonFive "
            "ZetaSix EtaSeven ThetaEight"
        )
        q = memory_recall.extract_query_candidate(msg, [])
        assert q is not None
        # FTS5 OR-joined; count by splitting on " OR ".
        parts = q.split(" OR ")
        assert len(parts) <= 5

    def test_pulls_from_recent_tool_args(self):
        # If the user message is terse but recent tool args mention a
        # proper noun, that should still seed a query.
        msg = "ok"
        recent_args = [{"command": "grep Tanium /var/log/foo"}, {"path": "/tmp/NEC.log"}]
        q = memory_recall.extract_query_candidate(msg, recent_args)
        assert q is not None
        # At least one of the tool-arg nouns should appear.
        assert "Tanium" in q or "NEC" in q

    def test_dedupes_tokens(self):
        msg = "Tanium Tanium Tanium NEC NEC"
        q = memory_recall.extract_query_candidate(msg, [])
        assert q is not None
        parts = q.split(" OR ")
        # Distinct: at most 2 tokens.
        assert len(parts) == len(set(parts))
        assert len(parts) <= 2


# ---------------------------------------------------------------------------
# maybe_memory_recall_hint — trigger logic
# ---------------------------------------------------------------------------


class TestMaybeMemoryRecallHint:
    def test_interval_zero_disables(self):
        a = FakeAgent(interval=0, last_user_message="NEC Tanium long message " * 20)
        # Even after many ticks, no hint.
        for _ in range(50):
            assert memory_recall.maybe_memory_recall_hint(a, "terminal") is None

    def test_no_hint_when_warm_empty(self, monkeypatch):
        # Stub warm store with count=0.
        monkeypatch.setattr(
            memory_recall, "_get_warm_count",
            lambda: 0,
        )
        a = FakeAgent(
            interval=2,
            last_user_message="NEC Tanium something long " * 20,
        )
        # 2 calls would normally fire, but warm is empty.
        for _ in range(10):
            assert memory_recall.maybe_memory_recall_hint(a, "terminal") is None

    def test_no_hint_for_short_user_message(self, monkeypatch):
        monkeypatch.setattr(memory_recall, "_get_warm_count", lambda: 5)
        a = FakeAgent(interval=2, min_user_chars=200, last_user_message="ok")
        for _ in range(10):
            assert memory_recall.maybe_memory_recall_hint(a, "terminal") is None

    def test_fires_after_interval(self, monkeypatch):
        monkeypatch.setattr(memory_recall, "_get_warm_count", lambda: 5)
        a = FakeAgent(
            interval=3,
            min_user_chars=10,
            last_user_message="NEC investigation about Tanium Schedule",
            mode="hint",
        )
        # First two calls: no hint.
        assert memory_recall.maybe_memory_recall_hint(a, "terminal") is None
        assert memory_recall.maybe_memory_recall_hint(a, "terminal") is None
        # Third call: fires.
        hint = memory_recall.maybe_memory_recall_hint(a, "terminal")
        assert hint is not None
        assert "memory-recall reminder" in hint
        # Counter reset.
        assert a._turns_since_memory_recall == 0

    def test_no_query_extracted_skips_without_burning_cooldown(self, monkeypatch):
        """If the user message and tool args yield no extractable query,
        the reminder should NOT fire AND should NOT reset the counter —
        we just skip this turn and wait for a turn with real signal."""
        monkeypatch.setattr(memory_recall, "_get_warm_count", lambda: 5)
        a = FakeAgent(
            interval=2,
            min_user_chars=5,
            # Only stopwords — extract_query_candidate returns None.
            last_user_message="the and but for with",
            mode="hint",
        )
        # Tick to interval. The counter advances normally but, when
        # extraction fails on the firing turn, the reminder is skipped
        # AND the counter is held back by one so the next turn with
        # real signal can fire.
        memory_recall.maybe_memory_recall_hint(a, "terminal")  # counter = 1
        # Firing turn: extraction fails, counter is reset to (interval - 1)
        # so the next non-empty turn can fire.
        assert memory_recall.maybe_memory_recall_hint(a, "terminal") is None
        assert a._turns_since_memory_recall == 1  # held back

    def test_explicit_memory_directive_fires_immediately(self, monkeypatch):
        """When the user says 'remember' or 'we did this before', fire
        the reminder regardless of counter."""
        monkeypatch.setattr(memory_recall, "_get_warm_count", lambda: 5)
        a = FakeAgent(
            interval=8,
            min_user_chars=10,
            last_user_message="we did this NEC investigation before",
            mode="hint",
        )
        hint = memory_recall.maybe_memory_recall_hint(a, "terminal")
        assert hint is not None
        assert "memory-recall reminder" in hint

    def test_hint_mode_does_not_run_recall(self, monkeypatch):
        """hint mode just emits text, no DB hit."""
        recall_calls = []
        monkeypatch.setattr(memory_recall, "_get_warm_count", lambda: 5)

        def stub_recall(query, top_k):
            recall_calls.append((query, top_k))
            return []

        monkeypatch.setattr(memory_recall, "_run_warm_recall", stub_recall)
        a = FakeAgent(
            interval=1,
            min_user_chars=10,
            last_user_message="NEC Tanium issue",
            mode="hint",
        )
        hint = memory_recall.maybe_memory_recall_hint(a, "terminal")
        assert hint is not None
        assert recall_calls == []  # no recall run

    def test_auto_mode_runs_recall_and_includes_top(self, monkeypatch):
        monkeypatch.setattr(memory_recall, "_get_warm_count", lambda: 5)

        def stub_recall(query, top_k):
            return [
                {
                    "fact_id": 69,
                    "trust_score": 0.5,
                    "content": "READ THE SOURCE before concluding; verify ALL customer evidence.",
                },
            ]

        monkeypatch.setattr(memory_recall, "_run_warm_recall", stub_recall)
        a = FakeAgent(
            interval=1,
            min_user_chars=10,
            last_user_message="NEC Tanium investigation",
            mode="auto",
            auto_top_k=3,
        )
        hint = memory_recall.maybe_memory_recall_hint(a, "terminal")
        assert hint is not None
        assert "fact 69" in hint
        assert "READ THE SOURCE" in hint or "READ THE" in hint

    def test_auto_mode_falls_back_to_hint_on_empty_recall(self, monkeypatch):
        """If the auto-run returns zero hits, emit the plain hint
        rather than a misleading 'top: nothing' message."""
        monkeypatch.setattr(memory_recall, "_get_warm_count", lambda: 5)
        monkeypatch.setattr(memory_recall, "_run_warm_recall", lambda *a, **k: [])
        a = FakeAgent(
            interval=1,
            min_user_chars=10,
            last_user_message="NEC Tanium investigation",
            mode="auto",
        )
        hint = memory_recall.maybe_memory_recall_hint(a, "terminal")
        # Either no hint (empty recall is a no-op) or a hint that does
        # NOT pretend to have hits. Both are acceptable; we just don't
        # want garbage. We require: if a hint comes back, no fake hits.
        if hint is not None:
            assert "fact 0" not in hint
            assert "Auto-recall for query" in hint


# ---------------------------------------------------------------------------
# record_voluntary_recall — counter reset
# ---------------------------------------------------------------------------


class TestRecordVoluntaryRecall:
    def test_resets_counter(self):
        a = FakeAgent(interval=8)
        a._turns_since_memory_recall = 6
        memory_recall.record_voluntary_recall(a)
        # After a voluntary recall the agent doesn't need another reminder
        # right away.
        assert a._turns_since_memory_recall == 0

    def test_safe_when_attribute_missing(self):
        """If the agent was constructed before the feature wired in
        (subagent, test, etc.), record_voluntary_recall must not crash."""

        class Bare:
            pass

        b = Bare()
        # No exception.
        memory_recall.record_voluntary_recall(b)


# ---------------------------------------------------------------------------
# init_state — defaults
# ---------------------------------------------------------------------------


class TestInitState:
    def test_sets_defaults(self):
        class Bare:
            pass

        a = Bare()
        memory_recall.init_state(a)
        assert a._memory_recall_reminder_interval == 8
        assert a._memory_recall_reminder_mode == "auto"
        assert a._memory_recall_auto_top_k == 3
        assert a._memory_recall_min_user_chars == 200
        assert a._turns_since_memory_recall == 0
        assert a._last_user_message == ""
