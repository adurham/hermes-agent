"""Tests for subagent summary budgeting (PR #9126).

delegate_task caps subagent summaries against the parent's remaining context
headroom (split across the batch) before they enter the parent's context, and
spills the full text to disk so nothing is lost. This guards the
compression/429 death spiral that batch fan-out could trigger by returning N
full summaries verbatim into the parent.
"""

import os
import tempfile


import tools.delegate_tool as dt
from tools.delegate_tool_results import _MIN_SUMMARY_CHARS, _parent_summary_char_budget


class _FakeCompressor:
    def __init__(self, context_length, max_tokens, used_tokens=0):
        self.context_length = context_length
        self.max_tokens = max_tokens
        self._used_tokens = used_tokens

    def display_prompt_tokens(self):
        return self._used_tokens


class _FakeParent:
    def __init__(self, context_length, used_tokens, max_tokens, session_total=None):
        self.context_compressor = _FakeCompressor(context_length, max_tokens, used_tokens)
        # Current prompt size (last call) drives the budget; the cumulative session counter must not.
        self._last_turn_usage = {"prompt_tokens": used_tokens}
        # session_prompt_tokens is deliberately NOT read by _parent_summary_char_budget (regression
        # guard for the cumulative-vs-current bug: it only grows across the whole session and is not
        # the current context size). It defaults to 7x the current size so the guard test can prove
        # a large cumulative counter does not clamp the budget.
        self.session_prompt_tokens = session_total if session_total is not None else used_tokens * 7


def test_small_summaries_pass_through_untouched():
    parent = _FakeParent(context_length=200_000, used_tokens=10_000, max_tokens=8_000)
    results = [
        {"task_index": 0, "summary": "short result A", "status": "completed"},
        {"task_index": 1, "summary": "short result B", "status": "completed"},
    ]
    dt._apply_summary_budget(results, parent)
    assert results[0]["summary"] == "short result A"
    assert "summary_truncated" not in results[0]
    assert "summary_truncated" not in results[1]


def test_batch_overflow_trimmed_and_spilled_losslessly(monkeypatch):
    # Isolate spill directory to a temp HERMES_HOME.
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", os.path.join(td, ".hermes"))
        # Distinct head + tail markers so we can prove the tail survives.
        big = "HEAD_MARKER\n" + ("X" * 50_000) + "\nTAIL_MARKER"
        # Parent nearly full (120k/131k) → tiny headroom → aggressive trim.
        parent = _FakeParent(context_length=131_000, used_tokens=120_000, max_tokens=8_000)
        results = [
            {"task_index": i, "summary": big, "status": "completed"} for i in range(5)
        ]
        dt._apply_summary_budget(results, parent)
        for r in results:
            assert r["summary_truncated"] is True
            assert len(r["summary"]) < len(big)
            # Head+tail window: both ends survive in-context.
            assert "HEAD_MARKER" in r["summary"]
            assert "TAIL_MARKER" in r["summary"]
            path = r.get("summary_full_path")
            assert path and os.path.exists(path)
            # The spill file holds the FULL original text — nothing is lost.
            with open(path, encoding="utf-8") as fh:
                assert fh.read() == big
            # The footer points the parent at the full version with an offset.
            assert "read_file" in r["summary"]
            assert "offset=" in r["summary"]
            # Spilled into the delegation cache (mounted into remote backends).
            assert os.path.join("cache", "delegation") in path


def test_dynamic_budget_shrinks_as_batch_grows():
    def cap_for(n):
        return dt._parent_summary_char_budget(
            _FakeParent(131_000, 30_000, 8_000), n
        )

    c1, c5, c20 = cap_for(1), cap_for(5), cap_for(20)
    assert c1 is not None and c5 is not None and c20 is not None
    # More children → smaller per-summary slice of the same headroom.
    assert c1 > c5 > c20


def test_floor_enforced_when_parent_over_budget():
    # Parent already over its context budget → each summary gets only the floor.
    budget = dt._parent_summary_char_budget(
        _FakeParent(131_000, 200_000, 8_000), 3
    )
    assert budget == dt._MIN_SUMMARY_CHARS


def test_unknown_context_falls_back_to_static_ceiling(monkeypatch):
    class _Bare:
        pass

    # No compressor → dynamic budget is unknowable.
    assert dt._parent_summary_char_budget(_Bare(), 3) is None

    # But the static delegation.max_summary_chars ceiling still trims.
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", os.path.join(td, ".hermes"))
        results = [{"task_index": 0, "summary": "Y" * 40_000, "status": "completed"}]
        dt._apply_summary_budget(results, _Bare())
        assert results[0]["summary_truncated"] is True
        assert len(results[0]["summary"]) < 40_000


def test_disabled_static_ceiling_and_unknown_context_leaves_summary_intact(monkeypatch):
    class _Bare:
        pass

    # Both caps off: static ceiling 0 (disabled) AND no compressor (no dynamic).
    monkeypatch.setattr(dt, "_load_config", lambda: {"max_summary_chars": 0})
    results = [{"task_index": 0, "summary": "Z" * 40_000, "status": "completed"}]
    dt._apply_summary_budget(results, _Bare())
    assert "summary_truncated" not in results[0]
    assert len(results[0]["summary"]) == 40_000


def test_uses_current_context_not_cumulative_session_total():
    """Regression guard: session_prompt_tokens is a cumulative sum across
    every API call this session has ever made — it only grows, so after a
    few turns it exceeds context_length even on a session compaction has
    kept small, clamping every subsequent batch to the floor. The budget
    must be sized off the CURRENT context (display_prompt_tokens()), not
    that ever-growing counter.
    """
    parent = _FakeParent(context_length=131_000, used_tokens=10_000, max_tokens=8_000)
    # A long-running session: cumulative session_prompt_tokens is huge
    # (7x current, per the fixture) — if the bug were still present this
    # would blow past context_length and clamp to the floor.
    assert parent.session_prompt_tokens == 70_000
    budget = dt._parent_summary_char_budget(parent, 3)
    assert budget is not None
    assert budget > dt._MIN_SUMMARY_CHARS



def test_empty_results_is_noop():
    # No summaries → nothing to do, must not raise.
    dt._apply_summary_budget([], _FakeParent(131_000, 1_000, 8_000))
    dt._apply_summary_budget(
        [{"task_index": 0, "status": "failed", "summary": None}],
        _FakeParent(131_000, 1_000, 8_000),
    )


def test_budget_uses_current_prompt_size_not_the_session_sum():
    """A long-lived parent has a session sum far past any window while its current prompt is small; the
    budget must follow the current prompt, otherwise every summary collapses to the floor."""
    long_lived = _FakeParent(context_length=200_000, used_tokens=30_000, max_tokens=8_000, session_total=25_000_000)
    fresh = _FakeParent(context_length=200_000, used_tokens=30_000, max_tokens=8_000)
    assert _parent_summary_char_budget(long_lived, 1) == _parent_summary_char_budget(fresh, 1)
    assert _parent_summary_char_budget(long_lived, 1) > _MIN_SUMMARY_CHARS


def test_unknown_parent_usage_means_static_ceiling_not_zero_context():
    """Independent-review witness: a parent with no usage yet was treated as 0 tokens used, so a 190K/200K
    prompt got a 384K-char summary budget instead of ~4K."""
    from types import SimpleNamespace
    from tools.delegate_tool_results import _parent_summary_char_budget
    parent = SimpleNamespace(context_compressor=SimpleNamespace(context_length=200_000, max_tokens=0),
                             _last_turn_usage=None)
    assert _parent_summary_char_budget(parent, 1) is None


def test_moa_fold_does_not_inflate_the_parents_prompt_size():
    """MoA folds advisor prompts into reported usage; the parent's context holds only the aggregator's."""
    from types import SimpleNamespace
    from tools.delegate_tool_results import _parent_summary_char_budget
    cc = SimpleNamespace(context_length=200_000, max_tokens=0)
    folded = SimpleNamespace(context_compressor=cc, _last_turn_usage={"prompt_tokens": 190_000}, _last_prompt_size_tokens=50_000)
    unfolded = SimpleNamespace(context_compressor=cc, _last_turn_usage={"prompt_tokens": 50_000})
    assert _parent_summary_char_budget(folded, 1) == _parent_summary_char_budget(unfolded, 1)
