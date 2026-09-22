"""Session-exit staging of LLM-extracted memory proposals.

Called from cli.py's exit handler (before shutdown_memory_provider). Runs the
session-end extraction pass, classifies each proposal against existing warm
facts, and **stages** the results into the shared write-approval pending store
(``<HERMES_HOME>/pending/memory/<id>.json``) for review with
``/memory pending | show <id> | edit <id> … | approve <id>|all | reject <id>|all``.

History: this module used to own a bespoke 689-line blocking review UI at
session exit (letter-select, 3-second auto-accept countdown, in-place edit)
that committed straight to the warm store — a second, parallel review system
next to upstream's pending-approval mechanism, which already gated the agent's
own memory writes the same way for the same reason. The two are consolidated:
proposals now flow through upstream's store and commands, and the fork's
genuinely additive capabilities (the conflict verdict a proposal was reviewed
under, the side-by-side existing-fact view, edit-before-approve) ride along as
enrichments of the pending record rather than a separate UI. See
``tools/memory_tool.py::_apply_extraction_proposal`` for the replay side.

What is left here is the staging adapter and a non-blocking exit notice:
exiting a session must never wait on a human, which is the whole point of
moving the review off the exit path.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _shorten(text: str, width: int = 90) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def _classify_proposals(proposals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Annotate each proposal with a ``verdict`` (ConflictVerdict).

    Shows a spinner: each entry can trigger its own LLM classification call
    (``conflict.classify``), so with several proposals this legitimately takes a
    few seconds with nothing else printed. Best-effort — any failure to build or
    drive the spinner degrades to no progress indicator rather than blocking.
    """
    from tools.memory_extraction import conflict

    spinner = None
    try:
        from agent.display import KawaiiSpinner
        noun = "entry" if len(proposals) == 1 else "entries"
        spinner = KawaiiSpinner(f"classifying {len(proposals)} {noun} against memory")
        spinner.start()
    except Exception:
        spinner = None

    annotated: List[Dict[str, Any]] = []
    try:
        for p in proposals:
            try:
                v = conflict.classify(p["content"])
            except Exception as e:
                logger.warning("memory confirm: classify failed: %s", e)
                v = conflict.ConflictVerdict(verdict="NEW", rationale=f"classify failed: {e}")
            annotated.append({**p, "verdict": v})
    finally:
        if spinner is not None:
            try:
                spinner.stop("")
            except Exception:
                pass
    return annotated


def stage_proposal(proposal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Stage one classified proposal into the write-approval pending store.

    The payload is exactly what ``apply_memory_pending`` needs to replay the
    write, plus the conflict metadata the reviewer needs to judge it. Returns the
    pending record, or None if staging failed (logged, never raised — a lost
    proposal must not take session exit down with it).
    """
    from tools import write_approval as wa
    from tools.memory_extraction import conflict

    attached = proposal.get("verdict")
    verdict: conflict.ConflictVerdict = (
        attached if isinstance(attached, conflict.ConflictVerdict)
        else conflict.ConflictVerdict(verdict="NEW", rationale="unclassified proposal"))
    tier = (proposal.get("tier") or "warm").lower()
    payload = {
        "action": "extraction_proposal",
        "tier": tier,
        "content": proposal.get("content") or "",
        "category": proposal.get("category") or "general",
        "tags": proposal.get("tags") or "",
        "rationale": proposal.get("rationale") or "",
        "conflict": conflict.verdict_to_dict(verdict),
    }
    if tier == "hot":
        payload["target"] = proposal.get("target") or "memory"
    try:
        return wa.stage_write(
            wa.MEMORY, payload,
            summary=f"[{verdict.verdict}] {_shorten(payload['content'], 120)}",
            origin=wa.current_origin())
    except Exception as e:
        logger.warning("memory confirm: failed to stage proposal: %s", e, exc_info=True)
        return None


def confirm_and_commit(
    session_id: str,
    final_messages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the session-end extraction pass and stage its proposals for review.

    Returns the ``on_session_end`` summary dict. Safe to call with no pending
    proposals — returns all-zero counts. Never blocks on user input.
    """
    summary: Dict[str, Any] = {
        "session_id": session_id, "buffered": 0, "final_proposed": 0, "committed": 0,
        "skipped": 0, "cleanup_proposed": 0, "cleanup_applied": 0, "cleanup_skipped": 0,
        "actions": [], "cleanup_actions": [], "staged": 0,
    }
    if not session_id:
        return summary

    try:
        from tools.memory_extraction import extractor, buffer as _buf
    except Exception as e:
        logger.warning("memory confirm: extractor import failed: %s", e)
        return summary

    if not extractor.is_enabled():
        return summary

    if not _buf.get_session_entries(session_id) and not final_messages:
        return summary

    staged: List[Dict[str, Any]] = []

    # The session-end extraction pass makes a real LLM call that can take several
    # seconds; without a spinner the process reads as frozen.
    _spinner = None
    try:
        from agent.display import KawaiiSpinner
        _spinner = KawaiiSpinner("extracting memory proposals")
        _spinner.start()
    except Exception:
        _spinner = None

    def _stage_callback(
        proposals: List[Dict[str, Any]],
        cleanup: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        # Stop the extraction spinner before _classify_proposals starts its own,
        # so two spinners never animate over the same terminal line.
        if _spinner is not None:
            try:
                _spinner.stop("")
            except Exception:
                pass
        for p in _classify_proposals(proposals):
            record = stage_proposal(p)
            if record is not None:
                staged.append(record)
        # Approve nothing inline: every proposal is now owned by the pending
        # store, and returning it here would commit it a second time.
        return {"entries": [], "cleanup": []}

    try:
        summary = extractor.on_session_end(
            session_id, final_messages or [],
            interactive=True, confirm_callback=_stage_callback,
        )
    finally:
        if _spinner is not None:
            try:
                _spinner.stop("")
            except Exception:
                pass

    summary["staged"] = len(staged)
    # ``skipped`` counts proposals the extractor did not commit; staged ones are
    # deferred rather than dropped, so don't report them as lost.
    if staged:
        summary["skipped"] = max(0, summary.get("skipped", 0) - len(staged))
    _print_staged_notice(staged, summary)
    return summary


def _print_staged_notice(staged: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    """Non-blocking exit notice: what was staged, and how to review it.

    Deliberately a notice and not a prompt — session exit never waits on a human.
    """
    if summary.get("cleanup_applied"):
        print(f"Memory: applied {summary['cleanup_applied']} cleanup action(s).")
    if not staged:
        return
    noun = "proposal" if len(staged) == 1 else "proposals"
    print()
    print(f"Memory: staged {len(staged)} {noun} for review.")
    for record in staged[:5]:
        print(f"  {record['id']}  {record.get('summary', '')}")
    if len(staged) > 5:
        print(f"  … and {len(staged) - 5} more")
    print("  Review: /memory pending   Apply all: /memory approve all")
