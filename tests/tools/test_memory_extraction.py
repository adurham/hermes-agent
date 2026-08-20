"""Tests for tools/memory_extraction/* — Phase 2 auto-memory.

We mock auxiliary_client.call_llm everywhere so tests don't actually hit
the network. Each test gets a fresh warm DB and a fresh buffer.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from tools.memory_extraction import buffer as mex_buffer
from tools.memory_extraction import conflict as mex_conflict
from tools.memory_extraction import extractor as mex_extractor
from tools.memory_extraction import prompts as mex_prompts
from tools.memory_warm import (
    get_warm_store,
    reset_warm_store_for_testing,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture()
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at tmp so buffer + warm DB land in isolation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Reset hermes_constants cache
    import hermes_constants
    if hasattr(hermes_constants, "_HERMES_HOME_CACHE"):
        hermes_constants._HERMES_HOME_CACHE = None
    yield tmp_path
    reset_warm_store_for_testing()
    if hasattr(hermes_constants, "_HERMES_HOME_CACHE"):
        hermes_constants._HERMES_HOME_CACHE = None


@pytest.fixture()
def warm(isolated_hermes_home):
    reset_warm_store_for_testing()
    s = get_warm_store(db_path=isolated_hermes_home / "warm.db")
    yield s
    reset_warm_store_for_testing()


@pytest.fixture()
def auto_extract_on(monkeypatch):
    """Force is_enabled() to return True regardless of config."""
    monkeypatch.setattr(mex_extractor, "is_enabled", lambda: True)


@pytest.fixture()
def auto_extract_off(monkeypatch):
    monkeypatch.setattr(mex_extractor, "is_enabled", lambda: False)


# =========================================================================
# prompts.py — parsing
# =========================================================================

class TestParseExtractionResponse:
    def test_clean_json_passes(self):
        text = json.dumps({"entries": [
            {"content": "fact one is here", "category": "general"}
        ]})
        result = mex_prompts.parse_extraction_response(text)
        assert len(result) == 1
        assert result[0]["content"] == "fact one is here"
        assert result[0]["category"] == "general"

    def test_code_fence_passes(self):
        text = (
            "```json\n"
            '{"entries": [{"content": "fact in fences", "category": "support"}]}\n'
            "```"
        )
        result = mex_prompts.parse_extraction_response(text)
        assert len(result) == 1
        assert result[0]["content"] == "fact in fences"

    def test_chatty_response_passes(self):
        text = (
            "Sure, here are the entries:\n\n"
            '{"entries": [{"content": "buried in chatter here"}]}\n\n'
            "Hope that helps!"
        )
        result = mex_prompts.parse_extraction_response(text)
        assert len(result) == 1

    def test_empty_entries_returns_empty(self):
        result = mex_prompts.parse_extraction_response('{"entries": []}')
        assert result == []

    def test_invalid_json_returns_empty(self):
        result = mex_prompts.parse_extraction_response("not json at all")
        assert result == []

    def test_short_content_dropped(self):
        text = json.dumps({"entries": [{"content": "x"}]})  # too short
        result = mex_prompts.parse_extraction_response(text)
        assert result == []

    def test_default_cap_is_session_end_ceiling(self):
        """Default parse cap must equal the session-end ceiling.

        Behavior contract, not a frozen literal: the interactive
        session-end pass must never be silently truncated below the
        number of entries its own prompt asks the model for.
        """
        text = json.dumps({"entries": [
            {"content": f"fact number {i} content here"}
            for i in range(40)
        ]})
        result = mex_prompts.parse_extraction_response(text)
        assert len(result) == mex_prompts.SESSION_END_MAX_ENTRIES
        assert mex_prompts.DEFAULT_PARSE_MAX_ENTRIES == mex_prompts.SESSION_END_MAX_ENTRIES

    def test_explicit_cap_is_honored(self):
        text = json.dumps({"entries": [
            {"content": f"fact number {i} content here"}
            for i in range(40)
        ]})
        result = mex_prompts.parse_extraction_response(
            text, max_entries=mex_prompts.MID_SESSION_MAX_ENTRIES,
        )
        assert len(result) == mex_prompts.MID_SESSION_MAX_ENTRIES

    def test_session_end_cap_is_more_generous_than_mid_session(self):
        assert mex_prompts.SESSION_END_MAX_ENTRIES > mex_prompts.MID_SESSION_MAX_ENTRIES


class TestEntryCapPromptContract:
    """The prompt text and the parser cap must agree.

    If they drift, the model is either asked for more entries than we'll
    keep (silent truncation of work the user never sees) or fewer than we
    allow (wasted headroom).
    """

    def test_session_end_prompt_states_the_session_end_cap(self):
        assert (
            f"Maximum {mex_prompts.SESSION_END_MAX_ENTRIES} entries"
            in mex_prompts.SESSION_END_SYSTEM
        )
        assert f"0-{mex_prompts.SESSION_END_MAX_ENTRIES} entries" in mex_prompts.SESSION_END_SYSTEM

    def test_mid_session_prompts_keep_the_conservative_cap(self):
        for prompt in (mex_prompts.PER_TURN_SYSTEM, mex_prompts.PRE_COMPRESS_SYSTEM):
            assert (
                f"Maximum {mex_prompts.MID_SESSION_MAX_ENTRIES} entries" in prompt
            )
            assert f"Maximum {mex_prompts.SESSION_END_MAX_ENTRIES} entries" not in prompt


class TestParseCleanupResponse:
    def test_clean_remove_action(self):
        text = json.dumps({"cleanup": [
            {"fact_id": 7, "action": "remove", "reason": "path moved this session"}
        ]})
        result = mex_prompts.parse_cleanup_response(text)
        assert result == [
            {"fact_id": 7, "action": "remove", "reason": "path moved this session"}
        ]

    def test_clean_merge_action(self):
        text = json.dumps({"cleanup": [
            {"fact_id": 7, "action": "merge", "reason": "dupe",
             "merge_target_id": 9, "merged_content": "combined text"}
        ]})
        result = mex_prompts.parse_cleanup_response(text)
        assert result[0]["merge_target_id"] == 9
        assert result[0]["merged_content"] == "combined text"

    def test_empty_cleanup_is_fine(self):
        assert mex_prompts.parse_cleanup_response(json.dumps({"cleanup": []})) == []

    def test_missing_key_returns_empty(self):
        assert mex_prompts.parse_cleanup_response(json.dumps({"entries": []})) == []

    def test_garbage_returns_empty(self):
        assert mex_prompts.parse_cleanup_response("not json at all") == []
        assert mex_prompts.parse_cleanup_response("") == []

    def test_code_fence_tolerated(self):
        text = (
            "```json\n"
            '{"cleanup": [{"fact_id": 3, "action": "remove", "reason": "stale"}]}\n'
            "```"
        )
        assert mex_prompts.parse_cleanup_response(text)[0]["fact_id"] == 3

    def test_unknown_action_dropped(self):
        text = json.dumps({"cleanup": [
            {"fact_id": 3, "action": "nuke", "reason": "x"}
        ]})
        assert mex_prompts.parse_cleanup_response(text) == []

    def test_fact_id_outside_shown_set_is_dropped(self):
        """Hard safety gate: the model cannot name a fact we never showed it."""
        text = json.dumps({"cleanup": [
            {"fact_id": 3, "action": "remove", "reason": "x"},
            {"fact_id": 99, "action": "remove", "reason": "hallucinated"},
        ]})
        result = mex_prompts.parse_cleanup_response(text, valid_fact_ids={3})
        assert [a["fact_id"] for a in result] == [3]

    def test_merge_without_target_is_dropped_not_downgraded_to_remove(self):
        text = json.dumps({"cleanup": [
            {"fact_id": 3, "action": "merge", "reason": "x"}
        ]})
        assert mex_prompts.parse_cleanup_response(text) == []

    def test_merge_into_itself_is_dropped(self):
        text = json.dumps({"cleanup": [
            {"fact_id": 3, "action": "merge", "merge_target_id": 3, "reason": "x"}
        ]})
        assert mex_prompts.parse_cleanup_response(text) == []

    def test_merge_target_outside_shown_set_is_dropped(self):
        text = json.dumps({"cleanup": [
            {"fact_id": 3, "action": "merge", "merge_target_id": 99, "reason": "x"}
        ]})
        assert mex_prompts.parse_cleanup_response(text, valid_fact_ids={3}) == []

    def test_duplicate_actions_on_same_fact_collapse(self):
        text = json.dumps({"cleanup": [
            {"fact_id": 3, "action": "remove", "reason": "first"},
            {"fact_id": 3, "action": "remove", "reason": "second"},
        ]})
        result = mex_prompts.parse_cleanup_response(text)
        assert len(result) == 1
        assert result[0]["reason"] == "first"

    def test_max_actions_cap_applied(self):
        text = json.dumps({"cleanup": [
            {"fact_id": i, "action": "remove", "reason": "x"} for i in range(50)
        ]})
        assert len(mex_prompts.parse_cleanup_response(text)) == 10


class TestParseConflictResponse:
    def test_clean_verdict(self):
        text = json.dumps({
            "verdict": "REFINEMENT",
            "matched_id": 5,
            "rationale": "extends",
            "merged_content": "merged here",
        })
        result = mex_prompts.parse_conflict_response(text)
        assert result["verdict"] == "REFINEMENT"
        assert result["matched_id"] == 5
        assert result["merged_content"] == "merged here"

    def test_invalid_verdict_returns_none(self):
        text = json.dumps({"verdict": "MAYBE"})
        result = mex_prompts.parse_conflict_response(text)
        assert result is None

    def test_garbage_returns_none(self):
        result = mex_prompts.parse_conflict_response("nope")
        assert result is None


# =========================================================================
# buffer.py
# =========================================================================

class TestBuffer:
    def test_append_and_read(self, isolated_hermes_home):
        sid = "session-001"
        appended = mex_buffer.append(
            sid,
            [{"content": "fact one"}, {"content": "fact two"}],
            source="per_turn",
        )
        assert appended == 2
        entries = mex_buffer.get_session_entries(sid)
        assert len(entries) == 2
        assert {e["content"] for e in entries} == {"fact one", "fact two"}

    def test_dedup_by_content(self, isolated_hermes_home):
        sid = "session-002"
        mex_buffer.append(sid, [{"content": "fact A"}], source="per_turn")
        appended = mex_buffer.append(sid, [{"content": "fact A"}], source="per_turn")
        assert appended == 0
        assert len(mex_buffer.get_session_entries(sid)) == 1

    def test_clear_session(self, isolated_hermes_home):
        sid = "session-003"
        mex_buffer.append(sid, [{"content": "x"}, {"content": "y"}], source="per_turn")
        cleared = mex_buffer.clear_session(sid)
        assert cleared == 2
        assert mex_buffer.get_session_entries(sid) == []

    def test_replace_session_entries(self, isolated_hermes_home):
        sid = "session-004"
        mex_buffer.append(sid, [{"content": "old"}], source="per_turn")
        mex_buffer.replace_session_entries(sid, [{"content": "new"}])
        entries = mex_buffer.get_session_entries(sid)
        assert len(entries) == 1
        assert entries[0]["content"] == "new"

    def test_unknown_session_empty(self, isolated_hermes_home):
        assert mex_buffer.get_session_entries("nonexistent") == []
        assert mex_buffer.clear_session("nonexistent") == 0


# =========================================================================
# conflict.py
# =========================================================================

class TestConflictClassify:
    def test_no_existing_facts_is_new(self, warm):
        verdict = mex_conflict.classify("brand new fact never seen before")
        assert verdict.verdict == "NEW"

    def test_with_match_calls_llm(self, warm, monkeypatch):
        warm.add("The internal API uses column-file storage for sensor data")
        # Mock the LLM to return REFINEMENT
        def fake_llm(*, system, user, max_tokens):
            return json.dumps({
                "verdict": "REFINEMENT",
                "matched_id": 1,
                "rationale": "adds detail",
                "merged_content": "The internal API uses column-file storage (directio) for sensor data",
            })
        verdict = mex_conflict.classify(
            "Sensor data persists in column-file storage",
            llm_caller=fake_llm,
        )
        assert verdict.verdict == "REFINEMENT"
        assert verdict.matched_id == 1
        assert "directio" in verdict.merged_content

    def test_llm_failure_falls_back_to_new(self, warm, monkeypatch):
        warm.add("The internal API uses column-file storage")
        def fake_llm(**_):
            raise RuntimeError("LLM exploded")
        verdict = mex_conflict.classify(
            "The internal API uses column-file storage",
            llm_caller=fake_llm,
        )
        assert verdict.verdict == "NEW"
        assert "failed" in verdict.rationale.lower()


class TestApplyVerdict:
    def test_new_writes_fact(self, warm):
        from tools.memory_extraction.conflict import ConflictVerdict
        verdict = ConflictVerdict(verdict="NEW")
        outcome = mex_conflict.apply_verdict(
            verdict, {"content": "shiny new fact"}, warm_store=warm,
        )
        assert outcome["action"] == "stored"
        assert isinstance(outcome["fact_id"], int)

    def test_refinement_updates_existing(self, warm):
        from tools.memory_extraction.conflict import ConflictVerdict
        # Seed an existing fact
        existing = warm.add("original fact text")
        fid = existing["fact_id"]
        verdict = ConflictVerdict(
            verdict="REFINEMENT",
            matched_id=fid,
            merged_content="original fact text with more detail",
        )
        outcome = mex_conflict.apply_verdict(
            verdict, {"content": "more detail to add"}, warm_store=warm,
        )
        assert outcome["action"] == "refined"
        assert outcome["fact_id"] == fid
        # Verify the merged content landed
        row = warm.get(fid)
        assert "more detail" in row["content"]

    def test_duplicate_returns_dedup_action(self, warm):
        from tools.memory_extraction.conflict import ConflictVerdict
        existing = warm.add("the same fact")
        fid = existing["fact_id"]
        verdict = ConflictVerdict(verdict="DUPLICATE", matched_id=fid)
        outcome = mex_conflict.apply_verdict(
            verdict, {"content": "the same fact"}, warm_store=warm,
        )
        assert outcome["action"] == "deduplicated"

    def test_contradiction_pending_when_not_auto(self, warm):
        from tools.memory_extraction.conflict import ConflictVerdict
        existing = warm.add("an in-memory KV store is the storage")
        fid = existing["fact_id"]
        verdict = ConflictVerdict(
            verdict="CONTRADICTION",
            matched_id=fid,
            matched_content="an in-memory KV store is the storage",
        )
        outcome = mex_conflict.apply_verdict(
            verdict, {"content": "cdsdb is the storage"},
            warm_store=warm, auto_commit=False,
        )
        assert outcome["action"] == "contradiction_pending"
        # Existing fact must NOT have been modified
        assert warm.get(fid)["content"] == "an in-memory KV store is the storage"

    def test_contradiction_supersedes_when_auto(self, warm):
        from tools.memory_extraction.conflict import ConflictVerdict
        existing = warm.add("an in-memory KV store is the storage")
        fid = existing["fact_id"]
        verdict = ConflictVerdict(
            verdict="CONTRADICTION",
            matched_id=fid,
            matched_content="an in-memory KV store is the storage",
        )
        outcome = mex_conflict.apply_verdict(
            verdict, {"content": "cdsdb is the storage"},
            warm_store=warm, auto_commit=True,
        )
        assert outcome["action"] == "superseded"
        # The old fact should have been tagged with [superseded by ...]
        old_row = warm.get(fid)
        assert "superseded" in old_row["content"].lower()


# =========================================================================
# extractor.py — module-level orchestration
# =========================================================================

class TestOnTurnEnd:
    def test_disabled_is_noop(self, warm, auto_extract_off):
        # Should not call the LLM, not append to buffer
        with patch.object(mex_extractor, "_call_extraction_llm") as m:
            mex_extractor.on_turn_end("sid-1", "user message", "assistant reply")
            # The thread runs, but is_enabled=False short-circuits before LLM call
            # Wait briefly for any threads
            import time
            time.sleep(0.5)
        m.assert_not_called()
        assert mex_buffer.get_session_entries("sid-1") == []

    def test_enabled_writes_to_buffer(self, warm, auto_extract_on, monkeypatch):
        # Mock the LLM to return one entry
        def fake_llm(*, system, user, max_tokens, timeout=None):
            return json.dumps({"entries": [
                {"content": "fact extracted from this turn", "category": "general"}
            ]})
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)
        mex_extractor.on_turn_end("sid-2", "user message", "assistant reply")
        # Wait for the background thread
        import time
        for _ in range(20):
            if mex_buffer.get_session_entries("sid-2"):
                break
            time.sleep(0.1)
        entries = mex_buffer.get_session_entries("sid-2")
        assert len(entries) == 1
        assert "fact extracted" in entries[0]["content"]
        assert entries[0]["source"] == "per_turn"

    def test_llm_failure_does_not_propagate(self, warm, auto_extract_on, monkeypatch):
        def fake_llm(**_):
            raise RuntimeError("network down")
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)
        # Should not raise
        mex_extractor.on_turn_end("sid-3", "u", "a")
        import time
        time.sleep(0.3)
        # Buffer is empty
        assert mex_buffer.get_session_entries("sid-3") == []


class TestOnPreCompress:
    def test_disabled_is_noop(self, warm, auto_extract_off, monkeypatch):
        m = MagicMock()
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", m)
        mex_extractor.on_pre_compress("sid", [{"role": "user", "content": "x"}])
        m.assert_not_called()

    def test_writes_to_buffer(self, warm, auto_extract_on, monkeypatch):
        def fake_llm(*, system, user, max_tokens, timeout=None):
            return json.dumps({"entries": [
                {"content": "fact extracted from compression slice", "category": "support"}
            ]})
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)
        mex_extractor.on_pre_compress(
            "sid-pre",
            [
                {"role": "user", "content": "long message about the internal API"},
                {"role": "assistant", "content": "reply about internal API details"},
            ],
        )
        entries = mex_buffer.get_session_entries("sid-pre")
        assert len(entries) == 1
        assert entries[0]["source"] == "pre_compress"


class TestOnSessionEnd:
    def test_disabled_returns_zero_summary(self, warm, auto_extract_off):
        result = mex_extractor.on_session_end("sid", [])
        assert result["committed"] == 0

    def test_no_buffer_no_messages_zero_summary(self, warm, auto_extract_on):
        result = mex_extractor.on_session_end("sid", [])
        assert result["buffered"] == 0
        # final_proposed depends on whether the LLM is invoked; with empty
        # messages and empty buffer, it should be skipped or return empty.
        # We don't strictly require 0, but committed must be 0.
        assert result["committed"] == 0

    def test_auto_commit_off_stashes_to_buffer(
        self, warm, auto_extract_on, monkeypatch,
    ):
        """When auto_commit_session_end is off and no callback, proposals are
        stashed back to the buffer (not committed)."""
        # Pre-load buffer with a proposal
        mex_buffer.append(
            "sid-stash",
            [{"content": "buffered fact one"}],
            source="per_turn",
        )

        def fake_llm(*, system, user, max_tokens, timeout=None):
            # Session-end pass returns a final list
            return json.dumps({"entries": [
                {"content": "final reconciled fact", "category": "general"}
            ]})
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)

        # Force auto_commit OFF (the default)
        monkeypatch.setattr(
            mex_extractor, "_get_extraction_config",
            lambda: {
                "model": "claude-haiku-4-5", "provider": None, "timeout": 30,
                "max_tokens_per_turn": 1024, "max_tokens_session_end": 2048,
                "include_pre_compress": True,
                "auto_commit_session_end": False,
            },
        )

        result = mex_extractor.on_session_end("sid-stash", [])
        # Nothing committed
        assert result["committed"] == 0
        assert result["skipped"] >= 1
        # Buffer now has the FINAL list (not the pre-loaded entry)
        entries = mex_buffer.get_session_entries("sid-stash")
        assert len(entries) == 1
        assert "final reconciled" in entries[0]["content"]

    def test_interactive_commits_via_callback(
        self, warm, auto_extract_on, monkeypatch,
    ):
        mex_buffer.append("sid-int", [{"content": "from buffer"}], source="per_turn")

        def fake_llm(*, system, user, max_tokens, timeout=None):
            return json.dumps({"entries": [
                {"content": "from session-end pass", "category": "general"}
            ]})
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)

        # Callback approves whatever was proposed
        def cb(proposals):
            return list(proposals)

        result = mex_extractor.on_session_end(
            "sid-int", [{"role": "user", "content": "context"}],
            interactive=True, confirm_callback=cb,
        )
        assert result["committed"] >= 1
        # Buffer is cleared
        assert mex_buffer.get_session_entries("sid-int") == []

    def test_interactive_reject_all_clears_buffer(
        self, warm, auto_extract_on, monkeypatch,
    ):
        mex_buffer.append("sid-rej", [{"content": "from buffer"}], source="per_turn")

        def fake_llm(*, system, user, max_tokens, timeout=None):
            return json.dumps({"entries": [
                {"content": "would-be entry", "category": "general"}
            ]})
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)

        def cb(proposals):
            return []  # user rejected everything

        result = mex_extractor.on_session_end(
            "sid-rej", [],
            interactive=True, confirm_callback=cb,
        )
        assert result["committed"] == 0
        # Buffer cleared (empty approved set still finalizes the session)
        assert mex_buffer.get_session_entries("sid-rej") == []

    def test_attached_verdict_is_reused_not_reclassified(
        self, warm, auto_extract_on, monkeypatch,
    ):
        """Regression: if the confirm UI attached a verdict to a proposal,
        on_session_end MUST use that exact verdict — not roll a new one.

        Bug history: extractor.on_session_end called _conflict.classify()
        unconditionally on every approved entry, throwing away the verdict
        the confirm UI already showed the user. On non-deterministic LLM
        responses this caused proposals displayed as DUPLICATE to be
        committed as NEW (or vice versa), polluting the warm store with
        the exact duplicates the user thought were being deduped.
        """
        from tools.memory_extraction.conflict import ConflictVerdict

        # Pre-populate warm with an existing fact we'll claim is the dup target
        existing = warm.add(
            content="The internal developer MCP runs at developer.example.com",
            category="mcp",
        )
        existing_id = existing["fact_id"]

        # LLM returns a final-pass entry that overlaps the existing one
        def fake_llm(*, system, user, max_tokens, timeout=None):
            return json.dumps({"entries": [
                {"content": "internal developer MCP at developer.example.com endpoint",
                 "category": "mcp"}
            ]})
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)

        # Sentinel: if classify() is called during commit, it would return
        # NEW. We pre-attach DUPLICATE — the bug-prone path would commit NEW.
        classify_calls: list = []
        original_classify = mex_conflict.classify

        def spy_classify(content, **kw):
            classify_calls.append(content)
            return original_classify(content, **kw)
        monkeypatch.setattr(mex_conflict, "classify", spy_classify)

        # Callback simulates the confirm UI: attaches a DUPLICATE verdict
        # and approves the proposal as-is.
        def cb(proposals):
            for p in proposals:
                p["verdict"] = ConflictVerdict(
                    verdict="DUPLICATE",
                    matched_id=existing_id,
                    matched_content=existing["content"]
                        if "content" in existing else None,
                    rationale="UI-attached test verdict",
                )
            return list(proposals)

        result = mex_extractor.on_session_end(
            "sid-verdict-reuse", [{"role": "user", "content": "ctx"}],
            interactive=True, confirm_callback=cb,
        )

        # Hard assertion: classify() must NOT be called on the approved
        # entry's content during commit (it WAS called on the empty
        # candidate-detection path? — no, our spy only sees calls to the
        # public classify API). Either way the recorded contents must
        # not include the approved proposal's text.
        approved_text = "internal developer MCP at developer.example.com endpoint"
        assert approved_text not in classify_calls, (
            f"classify() was called on the approved proposal at commit time, "
            f"throwing away the UI verdict. calls={classify_calls!r}"
        )

        # And the recorded action must reflect the UI verdict (DUPLICATE
        # → action='deduplicated'), NOT a fresh NEW commit.
        assert len(result["actions"]) == 1, result
        action = result["actions"][0]
        assert action["verdict"] == "DUPLICATE", action
        assert action["outcome"] == "deduplicated", action
        # And no new fact_id should have been minted — apply_verdict on
        # DUPLICATE returns the matched_id without writing a new row.
        assert action["fact_id"] == existing_id, action

        # Warm store should still contain exactly one fact (the original).
        # If the bug were live, we'd have two — the original + the dup.
        assert warm.count() == 1, (
            f"warm store grew on a DUPLICATE verdict — duplicate was committed "
            f"as NEW. count={warm.count()}"
        )

    def test_no_attached_verdict_falls_through_to_classify(
        self, warm, auto_extract_on, monkeypatch,
    ):
        """When a proposal has NO pre-attached verdict (e.g. auto-commit
        path bypasses the UI), classify() must still run at commit time."""
        def fake_llm(*, system, user, max_tokens, timeout=None):
            return json.dumps({"entries": [
                {"content": "fresh fact for classify path", "category": "general"}
            ]})
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)

        classify_calls: list = []
        original_classify = mex_conflict.classify

        def spy_classify(content, **kw):
            classify_calls.append(content)
            return original_classify(content, **kw)
        monkeypatch.setattr(mex_conflict, "classify", spy_classify)

        # Force auto-commit ON; no callback, no UI = no pre-attached verdict.
        monkeypatch.setattr(
            mex_extractor, "_get_extraction_config",
            lambda: {
                "model": "claude-haiku-4-5", "provider": None, "timeout": 30,
                "max_tokens_per_turn": 1024, "max_tokens_session_end": 2048,
                "include_pre_compress": True,
                "auto_commit_session_end": True,
            },
        )

        mex_extractor.on_session_end("sid-fresh", [])

        assert "fresh fact for classify path" in classify_calls, (
            f"classify() was NOT called on the auto-commit path. "
            f"calls={classify_calls!r}"
        )


class TestFlushBuffer:
    def test_flush_clears(self, warm, auto_extract_on):
        mex_buffer.append("sid", [{"content": "x"}], source="per_turn")
        cleared = mex_extractor.flush_buffer("sid")
        assert cleared == 1
        assert mex_buffer.get_session_entries("sid") == []


# =========================================================================
# extractor.py — cost ledger (fork-only, 2026-07-14)
#
# The CLI exit path (cli.py's _run_memory_confirm_before_exit) drains this
# ledger and folds it into session_estimated_cost_usd so the printed cost
# report includes memory-extraction LLM spend. These tests exercise
# _call_extraction_llm directly (not on_turn_end/on_session_end) since
# every other test in this file patches _call_extraction_llm itself away
# — the accounting logic lives inside that function and needs a real
# (mocked-at-the-transport-level) call to exercise.
# =========================================================================

class TestExtractionCostLedger:
    def _fake_response(self, *, model="claude-haiku-4-5", input_tokens=100, output_tokens=50):
        from types import SimpleNamespace
        usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        message = SimpleNamespace(content='{"entries": []}')
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice], usage=usage, model=model)

    def test_ledger_starts_at_zero_after_drain(self):
        # Draining with nothing recorded returns 0.0 and leaves it at 0.0.
        assert mex_extractor.get_and_reset_extraction_cost_usd() == 0.0
        assert mex_extractor.get_and_reset_extraction_cost_usd() == 0.0

    def test_call_extraction_llm_records_nonzero_cost(self, monkeypatch):
        # Drain any residue from other tests running in the same process.
        mex_extractor.get_and_reset_extraction_cost_usd()

        response = self._fake_response()
        monkeypatch.setattr(
            "agent.auxiliary_client.call_llm", lambda **kw: response,
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._resolve_task_provider_model",
            lambda *a, **kw: ("anthropic", "claude-haiku-4-5", None, None, "anthropic_messages"),
        )
        monkeypatch.setattr(
            mex_extractor, "_get_extraction_config",
            lambda: {
                "model": "claude-haiku-4-5", "provider": "anthropic", "timeout": 30,
                "max_tokens_per_turn": 1024, "max_tokens_session_end": 2048,
                "include_pre_compress": True, "auto_commit_session_end": False,
            },
        )

        mex_extractor._call_extraction_llm(system="sys", user="usr", max_tokens=100)

        cost = mex_extractor.get_and_reset_extraction_cost_usd()
        assert cost > 0.0, (
            "Expected a real API call with usage tokens against a priced "
            "model (claude-haiku-4-5) to record nonzero cost in the ledger."
        )

    def test_ledger_drains_and_resets(self, monkeypatch):
        mex_extractor.get_and_reset_extraction_cost_usd()

        response = self._fake_response()
        monkeypatch.setattr(
            "agent.auxiliary_client.call_llm", lambda **kw: response,
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._resolve_task_provider_model",
            lambda *a, **kw: ("anthropic", "claude-haiku-4-5", None, None, "anthropic_messages"),
        )
        monkeypatch.setattr(
            mex_extractor, "_get_extraction_config",
            lambda: {
                "model": "claude-haiku-4-5", "provider": "anthropic", "timeout": 30,
                "max_tokens_per_turn": 1024, "max_tokens_session_end": 2048,
                "include_pre_compress": True, "auto_commit_session_end": False,
            },
        )

        mex_extractor._call_extraction_llm(system="sys", user="usr", max_tokens=100)
        mex_extractor._call_extraction_llm(system="sys", user="usr", max_tokens=100)

        first_drain = mex_extractor.get_and_reset_extraction_cost_usd()
        assert first_drain > 0.0
        # Second drain immediately after must be zero -- the ledger resets
        # on read so the CLI exit path never double-counts a prior drain.
        second_drain = mex_extractor.get_and_reset_extraction_cost_usd()
        assert second_drain == 0.0

    def test_cost_accounting_failure_does_not_break_extraction(self, monkeypatch):
        """A pricing/accounting exception must never surface to the caller
        -- the actual extraction content is the contract; cost tracking is
        advisory bookkeeping layered on top."""
        mex_extractor.get_and_reset_extraction_cost_usd()

        response = self._fake_response()
        monkeypatch.setattr(
            "agent.auxiliary_client.call_llm", lambda **kw: response,
        )
        # Make provider/model resolution blow up -- the try/except around it
        # in _call_extraction_llm must swallow this.
        monkeypatch.setattr(
            "agent.auxiliary_client._resolve_task_provider_model",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        monkeypatch.setattr(
            mex_extractor, "_get_extraction_config",
            lambda: {
                "model": "claude-haiku-4-5", "provider": "anthropic", "timeout": 30,
                "max_tokens_per_turn": 1024, "max_tokens_session_end": 2048,
                "include_pre_compress": True, "auto_commit_session_end": False,
            },
        )

        # Must not raise despite the resolution failure.
        result = mex_extractor._call_extraction_llm(system="sys", user="usr", max_tokens=100)
        assert result == "{\"entries\": []}"


# =========================================================================
# extractor.py — session-end CLEANUP pass (real warm store, no mocked store)
# =========================================================================

class TestSessionEndCleanup:
    """E2E over a real temp warm DB — only the LLM boundary is stubbed.

    These exercise real ``WarmStore.add/get/remove/update`` calls so a
    dispatch bug shows up as a wrong DB state, not a satisfied mock.
    """

    @staticmethod
    def _cfg():
        return {
            "model": "claude-haiku-4-5", "provider": None, "timeout": 30,
            "max_tokens_per_turn": 1024, "max_tokens_session_end": 2048,
            "include_pre_compress": True, "auto_commit_session_end": False,
        }

    @staticmethod
    def _llm(entries_json, cleanup_json):
        """Route by system prompt: cleanup pass vs new-entry pass."""
        def fake_llm(*, system, user, max_tokens, timeout=None):
            if "auditing" in system:
                return json.dumps(cleanup_json)
            return json.dumps(entries_json)
        return fake_llm

    def test_propose_cleanup_annotates_with_existing_fact_text(self, warm):
        fid = warm.add(content="the deploy script lives at /old/deploy.sh")["fact_id"]
        actions = mex_extractor.propose_cleanup(
            [{"role": "user", "content": "the deploy script lives somewhere"}],
            [{"content": "the deploy script lives at /new/deploy.sh"}],
            warm_store=warm,
            llm_caller=lambda **kw: json.dumps({"cleanup": [
                {"fact_id": fid, "action": "remove", "reason": "path moved"}
            ]}),
        )
        assert len(actions) == 1
        assert actions[0]["fact_id"] == fid
        assert "/old/deploy.sh" in actions[0]["content"]

    def test_propose_cleanup_empty_response_does_not_error(self, warm):
        warm.add(content="the deploy script lives at /old/deploy.sh")
        actions = mex_extractor.propose_cleanup(
            [{"role": "user", "content": "deploy script talk"}],
            [{"content": "the deploy script lives at /new/deploy.sh"}],
            warm_store=warm,
            llm_caller=lambda **kw: json.dumps({"cleanup": []}),
        )
        assert actions == []

    def test_propose_cleanup_no_existing_facts_skips_llm(self, warm):
        calls = []

        def spy(**kw):
            calls.append(kw)
            return json.dumps({"cleanup": []})

        actions = mex_extractor.propose_cleanup(
            [{"role": "user", "content": "nothing stored yet at all"}],
            [{"content": "some brand new fact about deployment"}],
            warm_store=warm, llm_caller=spy,
        )
        assert actions == []
        assert calls == []

    def test_propose_cleanup_llm_failure_returns_empty(self, warm):
        warm.add(content="the deploy script lives at /old/deploy.sh")

        def boom(**kw):
            raise RuntimeError("provider down")

        assert mex_extractor.propose_cleanup(
            [{"role": "user", "content": "deploy script"}],
            [{"content": "the deploy script lives at /new/deploy.sh"}],
            warm_store=warm, llm_caller=boom,
        ) == []

    def test_apply_remove_deletes_the_fact(self, warm):
        fid = warm.add(content="stale fact about deployment paths")["fact_id"]
        out = mex_extractor.apply_cleanup_action(
            {"fact_id": fid, "action": "remove", "reason": "stale"},
            warm_store=warm,
        )
        assert out["action"] == "cleanup_removed"
        assert warm.get(fid) is None

    def test_apply_merge_updates_target_then_removes_source(self, warm):
        src = warm.add(content="cdsdb is the storage backend")["fact_id"]
        tgt = warm.add(content="TDS stores records somewhere")["fact_id"]
        out = mex_extractor.apply_cleanup_action(
            {"fact_id": src, "action": "merge", "merge_target_id": tgt,
             "merged_content": "TDS stores records in cdsdb", "reason": "dupe"},
            warm_store=warm,
        )
        assert out["action"] == "cleanup_merged"
        assert warm.get(src) is None
        assert warm.get(tgt)["content"] == "TDS stores records in cdsdb"

    def test_merge_failure_on_target_leaves_source_intact(self, warm):
        """Update-target-first ordering: a failed merge must not delete."""
        src = warm.add(content="source fact that must survive")["fact_id"]
        out = mex_extractor.apply_cleanup_action(
            {"fact_id": src, "action": "merge", "merge_target_id": 999999,
             "merged_content": "merged text", "reason": "dupe"},
            warm_store=warm,
        )
        assert out["action"] == "cleanup_skipped"
        assert warm.get(src) is not None

    def test_merge_without_merged_content_is_a_noop(self, warm):
        src = warm.add(content="source fact that must survive")["fact_id"]
        tgt = warm.add(content="target fact stays as written")["fact_id"]
        out = mex_extractor.apply_cleanup_action(
            {"fact_id": src, "action": "merge", "merge_target_id": tgt},
            warm_store=warm,
        )
        assert out["action"] == "cleanup_skipped"
        assert warm.get(src) is not None
        assert warm.get(tgt)["content"] == "target fact stays as written"

    def test_unknown_action_is_a_noop(self, warm):
        fid = warm.add(content="a fact that should not be touched")["fact_id"]
        out = mex_extractor.apply_cleanup_action(
            {"fact_id": fid, "action": "nuke"}, warm_store=warm,
        )
        assert out["action"] == "cleanup_skipped"
        assert warm.get(fid) is not None

    def test_session_end_surfaces_cleanup_to_callback_and_applies_approved(
        self, warm, auto_extract_on, monkeypatch,
    ):
        fid = warm.add(content="the deploy script lives at /old/deploy.sh")["fact_id"]
        monkeypatch.setattr(mex_extractor, "_get_extraction_config", self._cfg)
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", self._llm(
            {"entries": [{"content": "the deploy script lives at /new/deploy.sh"}]},
            {"cleanup": [{"fact_id": fid, "action": "remove", "reason": "path moved"}]},
        ))

        seen = {}

        def cb(entries, cleanup):
            seen["entries"] = entries
            seen["cleanup"] = cleanup
            return {"entries": entries, "cleanup": cleanup}

        result = mex_extractor.on_session_end(
            "sid-cleanup", [{"role": "user", "content": "deploy script moved"}],
            interactive=True, confirm_callback=cb,
        )
        assert [a["fact_id"] for a in seen["cleanup"]] == [fid]
        assert result["cleanup_proposed"] == 1
        assert result["cleanup_applied"] == 1
        assert warm.get(fid) is None

    def test_cleanup_not_applied_when_callback_approves_only_entries(
        self, warm, auto_extract_on, monkeypatch,
    ):
        """Accept-all on NEW entries must not sweep cleanup along with it."""
        fid = warm.add(content="the deploy script lives at /old/deploy.sh")["fact_id"]
        monkeypatch.setattr(mex_extractor, "_get_extraction_config", self._cfg)
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", self._llm(
            {"entries": [{"content": "the deploy script lives at /new/deploy.sh"}]},
            {"cleanup": [{"fact_id": fid, "action": "remove", "reason": "path moved"}]},
        ))

        # Legacy single-arg callback shape returning a bare list == the
        # "accept all new entries" path. Cleanup must be dropped.
        result = mex_extractor.on_session_end(
            "sid-nomix", [{"role": "user", "content": "deploy script moved"}],
            interactive=True, confirm_callback=lambda proposals: list(proposals),
        )
        assert result["cleanup_proposed"] == 1
        assert result["cleanup_applied"] == 0
        assert result["cleanup_skipped"] == 1
        assert warm.get(fid) is not None
        assert result["committed"] >= 1

    def test_cleanup_empty_does_not_error(
        self, warm, auto_extract_on, monkeypatch,
    ):
        warm.add(content="the deploy script lives at /old/deploy.sh")
        monkeypatch.setattr(mex_extractor, "_get_extraction_config", self._cfg)
        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", self._llm(
            {"entries": [{"content": "the deploy script lives at /new/deploy.sh"}]},
            {"cleanup": []},
        ))
        result = mex_extractor.on_session_end(
            "sid-empty", [{"role": "user", "content": "deploy script moved"}],
            interactive=True,
            confirm_callback=lambda e, c: {"entries": e, "cleanup": c},
        )
        assert result["cleanup_proposed"] == 0
        assert result["cleanup_applied"] == 0
        assert result["cleanup_actions"] == []

    def test_non_interactive_never_proposes_or_applies_cleanup(
        self, warm, auto_extract_on, monkeypatch,
    ):
        """auto_commit_session_end=True must NOT enable cleanup."""
        fid = warm.add(content="the deploy script lives at /old/deploy.sh")["fact_id"]
        cfg = dict(self._cfg())
        cfg["auto_commit_session_end"] = True
        monkeypatch.setattr(mex_extractor, "_get_extraction_config", lambda: cfg)

        systems = []

        def fake_llm(*, system, user, max_tokens, timeout=None):
            systems.append(system)
            return json.dumps({"entries": [
                {"content": "the deploy script lives at /new/deploy.sh"}
            ]})

        monkeypatch.setattr(mex_extractor, "_call_extraction_llm", fake_llm)
        result = mex_extractor.on_session_end(
            "sid-auto", [{"role": "user", "content": "deploy script moved"}],
            interactive=False,
        )
        assert result["cleanup_proposed"] == 0
        assert result["cleanup_applied"] == 0
        assert warm.get(fid) is not None
        # The cleanup prompt was never even sent.
        assert not any("auditing" in s for s in systems)
