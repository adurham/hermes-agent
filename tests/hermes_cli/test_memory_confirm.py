"""Tests for hermes_cli/memory_confirm.py — interactive review UI.

Covers the rendering + input-handling improvements added on top of the
initial Phase 2 confirm UI:

  * grammar: "1 entry" vs "N entries"
  * tier indicator: warm:<category> vs hot:<target>
  * full-text rendering when N <= 3, truncated when N >= 4
  * dedup hint: shows the closest existing fact when verdict is NEW but
    the FTS5 candidate list is non-empty
  * default-accept rule: blank input accepts all only when N <= 3
  * `show <letter>`: prints one entry's full content and re-prompts
  * `reject <letter>`: drops one proposal and re-prompts with renumbered list

The conflict classifier is stubbed out so we don't need a warm DB; we
inject ConflictVerdict instances directly via a monkeypatched
``_classify_proposals``.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from hermes_cli import memory_confirm
from tools.memory_extraction.conflict import ConflictVerdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verdict(
    kind: str = "NEW",
    *,
    matched_id: int | None = None,
    matched_content: str | None = None,
    rationale: str = "",
    candidates: List[Dict[str, Any]] | None = None,
    merged_content: str | None = None,
) -> ConflictVerdict:
    return ConflictVerdict(
        verdict=kind,
        matched_id=matched_id,
        matched_content=matched_content,
        rationale=rationale,
        candidates=candidates or [],
        merged_content=merged_content,
    )


def _proposal(
    content: str,
    *,
    category: str = "general",
    tier: str | None = None,
    target: str | None = None,
    rationale: str = "",
) -> Dict[str, Any]:
    p: Dict[str, Any] = {"content": content, "category": category, "rationale": rationale}
    if tier:
        p["tier"] = tier
    if target:
        p["target"] = target
    return p


@pytest.fixture()
def stub_classifier(monkeypatch):
    """Stub _classify_proposals so we control verdicts without a warm DB.

    Each test calls ``stub_classifier([(proposal, verdict), ...])`` to
    register the (proposal, verdict) pairs that the next
    _interactive_review() call will see.
    """
    pairs: List[tuple[Dict[str, Any], ConflictVerdict]] = []

    def _set(items: List[tuple[Dict[str, Any], ConflictVerdict]]) -> List[Dict[str, Any]]:
        pairs.clear()
        pairs.extend(items)
        return [p for p, _ in items]

    def _fake_classify(proposals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Match by reference order — fake_classify is called on the same
        # list the test set up via _set().
        out: List[Dict[str, Any]] = []
        for p, v in pairs:
            out.append({**p, "verdict": v})
        return out

    monkeypatch.setattr(memory_confirm, "_classify_proposals", _fake_classify)
    return _set


# ---------------------------------------------------------------------------
# Grammar / pluralization
# ---------------------------------------------------------------------------

class TestPluralization:
    def test_single_entry_is_singular(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("only one fact"), _verdict("NEW")),
        ])
        # blank input → accept all when N <= 3
        monkeypatch.setattr("builtins.input", lambda *_: "")
        chosen = memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "1 proposed memory entry from" in out
        assert "entries from" not in out.split("1 proposed memory entry")[0]
        assert len(chosen) == 1

    def test_multiple_entries_is_plural(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("fact one here"), _verdict("NEW")),
            (_proposal("fact two here"), _verdict("NEW")),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "2 proposed memory entries" in out


# ---------------------------------------------------------------------------
# Tier indicator
# ---------------------------------------------------------------------------

class TestTierIndicator:
    def test_warm_tier_shows_category(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("fact in preferences", category="preferences"), _verdict("NEW")),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "[warm:preferences]" in out
        # negative: bare category bracket should NOT be present
        assert "[preferences]" not in out.replace("[warm:preferences]", "")

    def test_hot_tier_user_target(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (
                _proposal("preference fact", tier="hot", target="user"),
                _verdict("NEW"),
            ),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "[hot:user]" in out

    def test_hot_tier_default_target_is_memory(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("memory fact", tier="hot"), _verdict("NEW")),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "[hot:memory]" in out


# ---------------------------------------------------------------------------
# Full-text vs truncated rendering
# ---------------------------------------------------------------------------

class TestFullTextRendering:
    LONG = "the quick brown fox " * 30  # ~600 chars; would normally truncate

    def test_full_text_when_n_le_3(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal(self.LONG.strip()), _verdict("NEW")),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        # Full content should appear (no "..." truncation marker on this entry)
        assert "the quick brown fox" in out
        # Truncation marker shouldn't be in the rendered content for show_full
        # path — _shorten() adds "..." but we only call it for short entries
        # and matched_content. Verify the long content is wrapped, not cut off.
        assert out.count("the quick brown fox") >= 5  # appears many times in wrapped form

    def test_truncated_when_n_gt_3(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal(self.LONG.strip() + f" entry-{i}-marker"), _verdict("NEW"))
            for i in range(4)
        ])
        # Force a no-op exit; we just want the rendering output
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        # With N=4, _shorten kicks in; the unique markers should NOT all appear
        # because each entry is truncated to 90 chars
        markers_seen = sum(1 for i in range(4) if f"entry-{i}-marker" in out)
        assert markers_seen == 0, "expected truncation to hide the trailing markers"
        # And ellipsis from _shorten should be present
        assert "..." in out


# ---------------------------------------------------------------------------
# Dedup hint
# ---------------------------------------------------------------------------

class TestDedupHint:
    def test_new_with_candidates_shows_similar(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (
                _proposal("new fact about cdsdb"),
                _verdict(
                    "NEW",
                    candidates=[{"fact_id": 7, "content": "cdsdb is the TDS storage backend"}],
                ),
            ),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "similar to existing:" in out
        assert "cdsdb is the TDS storage backend" in out

    def test_new_without_candidates_no_hint(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("genuinely new fact"), _verdict("NEW", candidates=[])),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "none")
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "similar to existing:" not in out


# ---------------------------------------------------------------------------
# Default-accept rule (blank input)
# ---------------------------------------------------------------------------

class TestDefaultAccept:
    def test_blank_accepts_all_when_n_le_3(self, stub_classifier, monkeypatch):
        proposals = stub_classifier([
            (_proposal("fact one here"), _verdict("NEW")),
            (_proposal("fact two here"), _verdict("NEW")),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "")
        chosen = memory_confirm._interactive_review(proposals)
        assert len(chosen) == 2

    def test_blank_re_prompts_when_n_gt_3(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal(f"fact number {i} here padded"), _verdict("NEW"))
            for i in range(4)
        ])
        # First press Enter (no input), then say "none"
        responses = iter(["", "none"])
        monkeypatch.setattr("builtins.input", lambda *_: next(responses))
        chosen = memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "pick letters, or type" in out  # the gentle re-prompt
        assert chosen == []  # eventually rejected via "none"

    def test_default_label_reflects_size(self, stub_classifier, monkeypatch):
        # Small batch — prompt should say "[all]"
        proposals = stub_classifier([
            (_proposal("only fact"), _verdict("NEW")),
        ])
        prompts: List[str] = []

        def _capture(prompt: str = "") -> str:
            prompts.append(prompt)
            return "none"

        monkeypatch.setattr("builtins.input", _capture)
        memory_confirm._interactive_review(proposals)
        assert any("[all]" in p for p in prompts), prompts

    def test_default_label_for_large_batch(self, stub_classifier, monkeypatch):
        proposals = stub_classifier([
            (_proposal(f"fact number {i} here padded"), _verdict("NEW"))
            for i in range(4)
        ])
        prompts: List[str] = []

        def _capture(prompt: str = "") -> str:
            prompts.append(prompt)
            return "none"

        monkeypatch.setattr("builtins.input", _capture)
        memory_confirm._interactive_review(proposals)
        assert any("no default" in p for p in prompts), prompts
        # Make sure we DIDN'T also show [all] as the default
        assert not any("[all]" in p for p in prompts), prompts


# ---------------------------------------------------------------------------
# `show <letter>` and `reject <letter>` actions
# ---------------------------------------------------------------------------

class TestShowAndReject:
    def test_show_prints_full_content(self, stub_classifier, monkeypatch, capsys):
        long = "extra long content " * 40 + " sentinel-tail"
        proposals = stub_classifier([
            (_proposal("fact a short"), _verdict("NEW")),
            (_proposal("fact b short"), _verdict("NEW")),
            (_proposal("fact c short"), _verdict("NEW")),
            (_proposal(long), _verdict("NEW")),  # forces N=4 → truncated by default
        ])
        responses = iter(["show d", "none"])
        monkeypatch.setattr("builtins.input", lambda *_: next(responses))
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        # The sentinel-tail is at the END of long content and gets truncated
        # in the default render; `show d` should expose it.
        assert "sentinel-tail" in out

    def test_reject_drops_entry_and_re_prompts(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("keep this one"), _verdict("NEW")),
            (_proposal("drop this one"), _verdict("NEW")),
            (_proposal("also keep this"), _verdict("NEW")),
        ])
        # reject letter b, then accept all the rest
        responses = iter(["reject b", "all"])
        monkeypatch.setattr("builtins.input", lambda *_: next(responses))
        chosen = memory_confirm._interactive_review(proposals)
        contents = [p["content"] for p in chosen]
        assert "drop this one" not in contents
        assert "keep this one" in contents
        assert "also keep this" in contents
        out = capsys.readouterr().out
        assert "dropped:" in out
        assert "2 entries remaining" in out

    def test_reject_invalid_letter_continues(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("fact a"), _verdict("NEW")),
            (_proposal("fact b"), _verdict("NEW")),
        ])
        responses = iter(["reject z", "none"])
        monkeypatch.setattr("builtins.input", lambda *_: next(responses))
        memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert "out of range" in out

    def test_reject_last_returns_empty(self, stub_classifier, monkeypatch, capsys):
        proposals = stub_classifier([
            (_proposal("only one"), _verdict("NEW")),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "reject a")
        chosen = memory_confirm._interactive_review(proposals)
        out = capsys.readouterr().out
        assert chosen == []
        assert "no entries left" in out


# ---------------------------------------------------------------------------
# Letter-list happy path still works
# ---------------------------------------------------------------------------

class TestLetterList:
    def test_select_subset(self, stub_classifier, monkeypatch):
        proposals = stub_classifier([
            (_proposal("fact a"), _verdict("NEW")),
            (_proposal("fact b"), _verdict("NEW")),
            (_proposal("fact c"), _verdict("NEW")),
        ])
        monkeypatch.setattr("builtins.input", lambda *_: "a c")
        chosen = memory_confirm._interactive_review(proposals)
        assert [p["content"] for p in chosen] == ["fact a", "fact c"]


# ---------------------------------------------------------------------------
# _wrap_indented helper
# ---------------------------------------------------------------------------

class TestWrapIndented:
    def test_short_text_one_line(self):
        out = memory_confirm._wrap_indented("short text", indent=">> ", width=80)
        assert out == ">> short text"

    def test_long_text_wraps_with_indent(self):
        text = "alpha bravo charlie delta echo foxtrot golf hotel " * 5
        out = memory_confirm._wrap_indented(text, indent=">> ", width=40)
        lines = out.splitlines()
        assert len(lines) > 1
        for line in lines:
            assert line.startswith(">> ")
            # Width check: indent + content shouldn't massively exceed 40
            # (we don't break words, so an overrun by one word is OK)
            assert len(line) <= 60

    def test_normalizes_newlines(self):
        out = memory_confirm._wrap_indented("line one\nline two", indent="", width=80)
        assert "\n" not in out
        assert out == "line one line two"
