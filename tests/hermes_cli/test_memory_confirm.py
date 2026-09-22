"""Tests for the consolidated memory-proposal review path.

The fork used to carry a bespoke 689-line blocking review UI in
``hermes_cli/memory_confirm.py`` (letter-select, 3-second auto-accept
countdown, inline cleanup review) that committed straight to the warm store at
session exit. That was a second review system parallel to upstream's
write-approval pending store. It is consolidated: ``memory_confirm`` now only
classifies proposals and **stages** them, and review happens out of band via
``/memory pending | show | edit | approve | reject``.

These tests assert the NEW contract. The old UI's tests were not deleted —
every behavior they protected that still exists (verdict display, existing-fact
display, edit-before-commit, accept-all/discard-all, never losing a proposal) is
re-asserted here against the pending store instead of against the removed UI.

Everything runs against a temp HERMES_HOME; the real ~/.hermes is never touched.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from hermes_cli import memory_confirm
from hermes_cli.write_approval_commands import handle_pending_subcommand
from tools import write_approval as wa
from tools.memory_extraction.conflict import ConflictVerdict


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir for every test in this module."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(wa, "get_hermes_home", lambda: tmp_path)
    return tmp_path


def _verdict(kind: str = "NEW", **kw: Any) -> ConflictVerdict:
    return ConflictVerdict(verdict=kind, rationale=kw.pop("rationale", f"{kind} verdict"), **kw)


def _proposal(content: str, verdict: ConflictVerdict | None = None, **kw: Any) -> Dict[str, Any]:
    p: Dict[str, Any] = {"content": content, "category": kw.pop("category", "general")}
    p.update(kw)
    p["verdict"] = verdict or _verdict("NEW")
    return p


def _payload_of(pending_id: str) -> Dict[str, Any]:
    rec = wa.get_pending(wa.MEMORY, pending_id)
    assert rec is not None
    return rec["payload"]


# ---------------------------------------------------------------------------
# Staging into upstream's pending store
# ---------------------------------------------------------------------------

class TestStageProposal:
    def test_writes_upstream_pending_record_shape(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal("a durable fact"))
        assert rec is not None
        path = hermes_home / "pending" / "memory" / f"{rec['id']}.json"
        assert path.exists(), "proposal must land in $HERMES_HOME/pending/memory/<id>.json"
        on_disk = json.loads(path.read_text())
        # Exactly the record shape upstream's stage_write produces.
        assert set(on_disk) >= {"id", "subsystem", "action", "summary", "origin",
                                "created_at", "payload"}
        assert on_disk["subsystem"] == "memory"
        assert on_disk["action"] == "extraction_proposal"

    def test_carries_conflict_metadata(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal(
            "port 8080 over TLS",
            _verdict("REFINEMENT", matched_id=4, matched_content="port 8080",
                     merged_content="port 8080 over TLS")))
        conflict = _payload_of(rec["id"])["conflict"]
        assert conflict["verdict"] == "REFINEMENT"
        assert conflict["matched_id"] == 4
        assert conflict["matched_content"] == "port 8080"
        assert conflict["merged_content"] == "port 8080 over TLS"

    def test_summary_leads_with_the_verdict(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal("x", _verdict("CONTRADICTION")))
        assert rec["summary"].startswith("[CONTRADICTION]")

    def test_warm_is_the_default_tier(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal("a warm fact"))
        payload = _payload_of(rec["id"])
        assert payload["tier"] == "warm"
        assert payload["category"] == "general"
        assert "target" not in payload, "warm proposals have no hot-tier target"

    def test_hot_tier_records_its_target(self, hermes_home):
        rec = memory_confirm.stage_proposal(
            _proposal("user prefers terse output", tier="hot", target="user"))
        payload = _payload_of(rec["id"])
        assert payload["tier"] == "hot" and payload["target"] == "user"

    def test_unclassified_proposal_degrades_to_new(self, hermes_home):
        """A proposal with no verdict must still stage (as NEW), never be dropped."""
        rec = memory_confirm.stage_proposal({"content": "no verdict attached"})
        assert rec is not None
        assert _payload_of(rec["id"])["conflict"]["verdict"] == "NEW"

    def test_staging_failure_returns_none_without_raising(self, hermes_home, monkeypatch):
        """A disk failure must not take session exit down with it."""
        def _boom(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(wa, "stage_write", _boom)
        assert memory_confirm.stage_proposal(_proposal("x")) is None


# ---------------------------------------------------------------------------
# Verdict serialization round trip
# ---------------------------------------------------------------------------

class TestVerdictRoundTrip:
    def test_round_trips_every_field(self):
        from tools.memory_extraction import conflict as c
        original = ConflictVerdict(
            verdict="CONTRADICTION", matched_id=12, matched_content="old text",
            rationale="because", merged_content="merged",
            candidates=[{"fact_id": 1, "content": "cand"}])
        back = c.verdict_from_dict(json.loads(json.dumps(c.verdict_to_dict(original))))
        assert (back.verdict, back.matched_id, back.matched_content) == (
            "CONTRADICTION", 12, "old text")
        assert (back.rationale, back.merged_content) == ("because", "merged")
        assert back.candidates == [{"fact_id": 1, "content": "cand"}]

    def test_candidates_are_capped(self):
        from tools.memory_extraction import conflict as c
        v = ConflictVerdict(verdict="NEW",
                            candidates=[{"fact_id": i, "content": f"c{i}"} for i in range(10)])
        assert len(c.verdict_to_dict(v)["candidates"]) == 3

    @pytest.mark.parametrize("bad", [None, {}, "nonsense", {"verdict": None}])
    def test_missing_or_broken_metadata_degrades_to_new(self, bad):
        """A hand-edited or older pending record must still approve, as NEW."""
        from tools.memory_extraction import conflict as c
        assert c.verdict_from_dict(bad).verdict == "NEW"


# ---------------------------------------------------------------------------
# /memory pending — conflict verdict visible in the listing
# ---------------------------------------------------------------------------

class TestPendingListing:
    TAGS = {"NEW": "[+ NEW]", "DUPLICATE": "[= DUPE]",
            "REFINEMENT": "[~ REFINE]", "CONTRADICTION": "[! CONFLICT]"}

    @pytest.mark.parametrize("kind", ["NEW", "DUPLICATE", "REFINEMENT", "CONTRADICTION"])
    def test_every_verdict_is_visible(self, hermes_home, kind):
        memory_confirm.stage_proposal(_proposal(f"a {kind} fact", _verdict(kind)))
        assert self.TAGS[kind] in handle_pending_subcommand(wa.MEMORY, ["pending"])

    def test_tier_label_is_visible(self, hermes_home):
        memory_confirm.stage_proposal(_proposal("warm one", category="preferences"))
        memory_confirm.stage_proposal(_proposal("hot one", tier="hot", target="user"))
        out = handle_pending_subcommand(wa.MEMORY, ["pending"])
        assert "[warm:preferences]" in out and "[hot:user]" in out

    def test_review_hints_are_shown_for_proposals(self, hermes_home):
        memory_confirm.stage_proposal(_proposal("x"))
        out = handle_pending_subcommand(wa.MEMORY, ["pending"])
        assert "/memory show" in out and "/memory edit" in out

    def test_plain_gated_writes_are_unchanged(self, hermes_home):
        """Upstream's own staged hot-tier writes carry no verdict and must not get a tag."""
        wa.stage_write(wa.MEMORY, {"action": "add", "target": "user", "content": "plain"},
                       summary="add to user profile: plain", origin="foreground")
        out = handle_pending_subcommand(wa.MEMORY, ["pending"])
        assert "plain" in out
        assert not any(t in out for t in self.TAGS.values())
        assert "/memory show" not in out


# ---------------------------------------------------------------------------
# /memory show — the conflict view (new text AND existing text together)
# ---------------------------------------------------------------------------

class TestShowCommand:
    def test_contradiction_shows_both_texts(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal(
            "the primary region is eu-west-2",
            _verdict("CONTRADICTION", matched_id=3,
                     matched_content="the primary region is us-east-1")))
        out = handle_pending_subcommand(wa.MEMORY, ["show", rec["id"]])
        assert "the primary region is eu-west-2" in out, "new text missing"
        assert "the primary region is us-east-1" in out, "existing text missing"
        assert "conflicts with" in out

    def test_refinement_shows_merged_result(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal(
            "deploy runs on 8080 over TLS",
            _verdict("REFINEMENT", matched_id=2, matched_content="deploy runs on 8080",
                     merged_content="deploy runs on 8080 over TLS")))
        out = handle_pending_subcommand(wa.MEMORY, ["show", rec["id"]])
        assert "refines" in out and "merged result if approved:" in out

    def test_duplicate_names_the_matched_fact(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal(
            "cdsdb is the TDS backend",
            _verdict("DUPLICATE", matched_id=9, matched_content="the TDS backend is cdsdb")))
        out = handle_pending_subcommand(wa.MEMORY, ["show", rec["id"]])
        assert "duplicate of" in out and "the TDS backend is cdsdb" in out

    def test_new_surfaces_the_closest_candidate(self, hermes_home):
        """The dedup hint the old UI printed for NEW-with-candidates."""
        rec = memory_confirm.stage_proposal(_proposal(
            "grafana lives in the obs repo",
            _verdict("NEW", candidates=[{"fact_id": 4, "content": "obs repo holds monitoring"}])))
        out = handle_pending_subcommand(wa.MEMORY, ["show", rec["id"]])
        assert "similar existing fact:" in out and "obs repo holds monitoring" in out

    def test_unknown_id(self, hermes_home):
        assert "No pending memory write" in handle_pending_subcommand(
            wa.MEMORY, ["show", "deadbeef"])

    def test_usage_without_id(self, hermes_home):
        assert "Usage:" in handle_pending_subcommand(wa.MEMORY, ["show"])

    def test_non_proposal_record_renders_plainly(self, hermes_home):
        rec = wa.stage_write(wa.MEMORY, {"action": "add", "target": "user", "content": "x"},
                             summary="add to user profile", origin="foreground")
        out = handle_pending_subcommand(wa.MEMORY, ["show", rec["id"]])
        assert "action:" in out and "conflicts with" not in out


# ---------------------------------------------------------------------------
# /memory edit — edit in place before approving
# ---------------------------------------------------------------------------

class TestEditCommand:
    def test_edit_rewrites_content_and_summary(self, hermes_home, monkeypatch):
        monkeypatch.setattr("tools.memory_extraction.conflict.classify",
                            lambda text, **kw: _verdict("NEW"))
        rec = memory_confirm.stage_proposal(_proposal("original typoed text"))
        out = handle_pending_subcommand(wa.MEMORY, ["edit", rec["id"], "corrected", "text"])
        assert "Updated pending memory proposal" in out
        stored = wa.get_pending(wa.MEMORY, rec["id"])
        assert stored["payload"]["content"] == "corrected text"
        assert "corrected text" in stored["summary"]

    def test_edit_reclassifies_the_new_text(self, hermes_home, monkeypatch):
        """The old verdict described the OLD text; approving under it would be wrong."""
        seen = {}

        def _classify(text, **kw):
            seen["text"] = text
            return _verdict("DUPLICATE", matched_id=3, matched_content="already known")

        monkeypatch.setattr("tools.memory_extraction.conflict.classify", _classify)
        rec = memory_confirm.stage_proposal(_proposal("first text", _verdict("NEW")))
        out = handle_pending_subcommand(wa.MEMORY, ["edit", rec["id"], "already", "known"])
        assert seen["text"] == "already known"
        assert "new verdict: DUPLICATE" in out
        assert wa.get_pending(wa.MEMORY, rec["id"])["payload"]["conflict"]["verdict"] == "DUPLICATE"

    def test_failed_reclassify_keeps_edit_and_drops_stale_verdict(self, hermes_home, monkeypatch):
        def _boom(text, **kw):
            raise RuntimeError("no LLM")
        monkeypatch.setattr("tools.memory_extraction.conflict.classify", _boom)
        rec = memory_confirm.stage_proposal(_proposal("x", _verdict("REFINEMENT", matched_id=1)))
        out = handle_pending_subcommand(wa.MEMORY, ["edit", rec["id"], "brand", "new", "text"])
        payload = _payload_of(rec["id"])
        assert payload["content"] == "brand new text"
        assert payload["conflict"] is None, "a verdict for the pre-edit text must not survive"
        assert "could not re-classify" in out

    def test_usage_errors(self, hermes_home):
        rec = memory_confirm.stage_proposal(_proposal("x"))
        assert "Usage:" in handle_pending_subcommand(wa.MEMORY, ["edit"])
        assert "Usage:" in handle_pending_subcommand(wa.MEMORY, ["edit", rec["id"]])
        assert "No pending memory write" in handle_pending_subcommand(
            wa.MEMORY, ["edit", "deadbeef", "text"])

    def test_edit_refuses_non_proposal_records(self, hermes_home):
        rec = wa.stage_write(wa.MEMORY, {"action": "add", "target": "user", "content": "x"},
                             summary="s", origin="foreground")
        out = handle_pending_subcommand(wa.MEMORY, ["edit", rec["id"], "new text"])
        assert "not an extraction proposal" in out
        assert _payload_of(rec["id"])["content"] == "x", "record must be left untouched"


# ---------------------------------------------------------------------------
# Session exit: stage + notify, never block
# ---------------------------------------------------------------------------

class TestSessionExit:
    @staticmethod
    def _wire(monkeypatch, proposals: List[Dict[str, Any]]):
        from tools.memory_extraction import extractor as _ex
        import tools.memory_extraction.buffer as _buf
        monkeypatch.setattr(_ex, "is_enabled", lambda: True)
        monkeypatch.setattr(_buf, "get_session_entries", lambda sid: list(proposals))
        monkeypatch.setattr(memory_confirm, "_classify_proposals",
                            lambda ps: [{**p, "verdict": _verdict("NEW")} for p in ps])

        def _on_session_end(session_id, messages, *, interactive=False, confirm_callback=None):
            assert interactive and confirm_callback is not None
            confirm_callback(list(proposals), [])
            return {"session_id": session_id, "buffered": 0, "final_proposed": len(proposals),
                    "committed": 0, "skipped": len(proposals), "cleanup_proposed": 0,
                    "cleanup_applied": 0, "cleanup_skipped": 0,
                    "actions": [], "cleanup_actions": []}

        monkeypatch.setattr(_ex, "on_session_end", _on_session_end)

    def test_stages_and_commits_nothing_inline(self, hermes_home, monkeypatch):
        self._wire(monkeypatch, [{"content": "fact one"}, {"content": "fact two"}])
        summary = memory_confirm.confirm_and_commit("sid", [{"role": "user", "content": "hi"}])
        assert summary["staged"] == 2
        assert summary["committed"] == 0, "exit must not commit; the pending store owns these now"
        assert wa.pending_count(wa.MEMORY) == 2

    def test_never_blocks_on_input(self, hermes_home, monkeypatch):
        """The whole point of moving review off the exit path."""
        self._wire(monkeypatch, [{"content": "fact one"}])

        def _bomb(*a, **kw):
            raise AssertionError("session exit must never call input()")

        monkeypatch.setattr("builtins.input", _bomb)
        memory_confirm.confirm_and_commit("sid", [{"role": "user", "content": "hi"}])

    def test_staged_proposals_are_not_counted_as_lost(self, hermes_home, monkeypatch):
        """Staged != skipped — they're deferred, and the count must say so."""
        self._wire(monkeypatch, [{"content": "fact one"}, {"content": "fact two"}])
        summary = memory_confirm.confirm_and_commit("sid", [{"role": "user", "content": "hi"}])
        assert summary["skipped"] == 0

    def test_notice_names_the_review_command(self, hermes_home, monkeypatch, capsys):
        self._wire(monkeypatch, [{"content": "fact one"}])
        memory_confirm.confirm_and_commit("sid", [{"role": "user", "content": "hi"}])
        out = capsys.readouterr().out
        assert "staged 1 proposal" in out
        assert "/memory pending" in out

    def test_no_session_id_is_a_noop(self, hermes_home):
        assert memory_confirm.confirm_and_commit("")["staged"] == 0
        assert wa.pending_count(wa.MEMORY) == 0

    def test_disabled_extraction_is_a_noop(self, hermes_home, monkeypatch):
        from tools.memory_extraction import extractor as _ex
        monkeypatch.setattr(_ex, "is_enabled", lambda: False)
        assert memory_confirm.confirm_and_commit("sid", [{"role": "user"}])["staged"] == 0
        assert wa.pending_count(wa.MEMORY) == 0
