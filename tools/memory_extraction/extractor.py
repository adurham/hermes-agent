"""Extractor — the main orchestration module for Phase 2 auto-memory.

Public entry points (called from run_agent.py / cli.py):
  * ``on_turn_end(session_id, user_msg, assistant_msg)``
  * ``on_pre_compress(session_id, messages)``
  * ``on_session_end(session_id, messages, *, interactive=False)``
  * ``flush_buffer(session_id)``
  * ``is_enabled()``

All entry points are best-effort — they catch every exception, log it,
and return. Extraction failures must never break the agent loop.

LLM routing: uses ``auxiliary_client.call_llm`` with task name
``memory_extraction``. User can override model / provider / timeout via
``auxiliary.memory_extraction.*`` in ``config.yaml``. Default model is
``claude-haiku-4-5``.

Concurrency: per-turn extraction runs in a background thread so it
doesn't block the agent loop. Pre-compress runs inline (it's already on
a slow path — compression itself is a multi-second LLM call). Session-end
runs inline (the user is exiting; blocking briefly is fine).

Telemetry: every extraction call's input/output token counts are logged
to ``$HERMES_HOME/logs/memory_extraction.log`` so we can tune prompts
later. Format: one JSON object per line (jsonlines).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tools.memory_extraction import buffer as _buffer
from tools.memory_extraction import conflict as _conflict
from tools.memory_extraction import prompts as _prompts

logger = logging.getLogger(__name__)

# Default model. User can override via ``auxiliary.memory_extraction.model``.
_DEFAULT_MODEL = "claude-haiku-4-5"

# Background thread pool — small, daemonized, reuses threads to avoid spawn cost.
_per_turn_lock = threading.Lock()
_per_turn_thread: Optional[threading.Thread] = None

# Telemetry log handle (lazy)
_telemetry_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Cost ledger (fork-only, 2026-07-14)
#
# Phase 2 auto-memory makes real LLM calls (per-turn, pre-compress,
# session-end, and conflict-classification) that were previously invisible
# to session cost accounting — the CLI's exit summary only ever priced the
# main agent loop's own API calls, so the "Memory: reviewing proposals..."
# step's spend simply vanished from the user-visible total. Every call in
# this module runs on the process's main thread or a short-lived worker
# thread, never overlapping across sessions in the CLI's single-process
# model, so a simple module-level accumulator (guarded by a lock for the
# per-turn background thread) is sufficient — no need for a per-session
# keyed ledger. ``get_and_reset_extraction_cost_usd`` is the drain API the
# CLI exit path uses to fold this into ``agent.session_estimated_cost_usd``
# right before printing the exit summary.
# ---------------------------------------------------------------------------
_cost_ledger_lock = threading.Lock()
_accumulated_cost_usd: float = 0.0


def get_and_reset_extraction_cost_usd() -> float:
    """Return the accumulated memory-extraction LLM spend and zero the ledger.

    Called once per CLI exit (see ``hermes_cli`` / ``cli.py``'s exit-summary
    wiring) so the reported total reflects every ``_call_extraction_llm``
    invocation since the last drain — per-turn, pre-compress, session-end,
    and conflict-classification calls all funnel through the same function
    and are recorded there regardless of which path is invoking it.
    """
    global _accumulated_cost_usd
    with _cost_ledger_lock:
        amount = _accumulated_cost_usd
        _accumulated_cost_usd = 0.0
    return amount


def _record_extraction_cost_usd(amount: float) -> None:
    global _accumulated_cost_usd
    if not amount:
        return
    with _cost_ledger_lock:
        _accumulated_cost_usd += amount


# ---------------------------------------------------------------------------
# Config / enable check
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """Return True when auto-extraction is configured ON.

    Reads ``memory.auto_extract`` from config.yaml. Default: ``False``
    (Phase 1 ships without auto-extract; user opts in).
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        mem_cfg = cfg.get("memory", {}) or {}
        return bool(mem_cfg.get("auto_extract", False))
    except Exception:
        return False


def _get_extraction_config() -> Dict[str, Any]:
    """Read auxiliary.memory_extraction.* config with defaults.

    Two ``auxiliary`` schemas are supported:

      * **task-first** (legacy): ``auxiliary.memory_extraction.{model,
        provider, timeout, …}`` carries the model directly, so read it here.
      * **provider-first** (fork schema): the model is selected from the
        provider block matching the active main provider (e.g. ``auxiliary.exo
        .default`` → Qwen when main is exo). In that case we must NOT inject a
        model/provider here — doing so passes an explicit ``model`` to
        ``call_llm`` that OVERRIDES the provider-first resolution in
        ``_resolve_task_provider_model`` and forces the wrong model (the
        stale ``claude-haiku-4-5`` default) at whatever endpoint the block
        selected → 404 on the exo cluster. Returning ``model=None`` /
        ``provider=None`` lets ``call_llm(task="memory_extraction")`` resolve
        both correctly. Per-task *settings* (timeouts, token budgets) still
        come from the shared ``auxiliary.defaults.memory_extraction`` block.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        aux_root = cfg.get("auxiliary", {}) or {}

        provider_first = False
        try:
            from agent.auxiliary_client import _aux_schema_is_provider_first
            provider_first = _aux_schema_is_provider_first(aux_root)
        except Exception:
            provider_first = False

        if provider_first:
            # Model/provider are resolved by call_llm via the task; settings
            # come from the shared defaults block.
            settings = (aux_root.get("defaults", {}) or {}).get("memory_extraction", {}) or {}
            return {
                "model": None,
                "provider": None,
                "timeout": settings.get("timeout", 30),
                "max_tokens_per_turn": settings.get("max_tokens_per_turn", 1024),
                "max_tokens_session_end": settings.get("max_tokens_session_end", 2048),
                "include_pre_compress": settings.get("include_pre_compress", True),
                "auto_commit_session_end": settings.get("auto_commit_session_end", False),
            }

        aux = aux_root.get("memory_extraction", {}) or {}
        return {
            "model": aux.get("model", _DEFAULT_MODEL),
            "provider": aux.get("provider"),
            "timeout": aux.get("timeout", 30),
            "max_tokens_per_turn": aux.get("max_tokens_per_turn", 1024),
            "max_tokens_session_end": aux.get("max_tokens_session_end", 2048),
            "include_pre_compress": aux.get("include_pre_compress", True),
            "auto_commit_session_end": aux.get("auto_commit_session_end", False),
        }
    except Exception:
        return {
            "model": _DEFAULT_MODEL,
            "provider": None,
            "timeout": 30,
            "max_tokens_per_turn": 1024,
            "max_tokens_session_end": 2048,
            "include_pre_compress": True,
            "auto_commit_session_end": False,
        }


# ---------------------------------------------------------------------------
# LLM dispatch
# ---------------------------------------------------------------------------

def _call_extraction_llm(
    *,
    system: str,
    user: str,
    max_tokens: int = 1024,
    timeout: Optional[int] = None,
) -> str:
    """Call the auxiliary LLM client with extraction-task hints.

    Returns the response text. Raises on transport failures so callers
    can fall back / log.
    """
    from agent.auxiliary_client import call_llm
    cfg = _get_extraction_config()
    call_kwargs: Dict[str, Any] = {
        "task": "memory_extraction",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    if cfg.get("model"):
        call_kwargs["model"] = cfg["model"]
    if cfg.get("provider"):
        call_kwargs["provider"] = cfg["provider"]
    if timeout is not None:
        call_kwargs["timeout"] = timeout
    elif cfg.get("timeout"):
        call_kwargs["timeout"] = cfg["timeout"]

    # Resolve provider/model/api_mode the same way call_llm will internally,
    # purely so cost estimation below has a real routing target to price
    # against — call_llm doesn't hand back its resolution, so we mirror it
    # read-only. Best-effort: any failure here just leaves cost unpriced,
    # never blocks the actual call.
    _resolved_provider = _resolved_base_url = _resolved_api_mode = None
    try:
        from agent.auxiliary_client import _resolve_task_provider_model
        _resolved_provider, _, _resolved_base_url, _, _resolved_api_mode = (
            _resolve_task_provider_model(
                "memory_extraction", cfg.get("provider"), cfg.get("model"),
            )
        )
    except Exception:
        pass

    response = call_llm(**call_kwargs)
    content = response.choices[0].message.content
    if not isinstance(content, str):
        content = str(content) if content else ""
    # Telemetry: log token usage
    _log_telemetry({
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "max_tokens": max_tokens,
        "input_chars": len(system) + len(user),
        "output_chars": len(content),
        "usage": _maybe_extract_usage(response),
    })
    # Cost accounting (fork-only): fold this call's spend into the module
    # ledger so the CLI exit path can add it to session_estimated_cost_usd.
    # Never lets a pricing failure affect the caller — this whole block is
    # advisory bookkeeping, not part of the extraction contract.
    try:
        from agent.usage_pricing import estimate_usage_cost, normalize_usage
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            canonical = normalize_usage(
                raw_usage, provider=_resolved_provider, api_mode=_resolved_api_mode,
            )
            _model_for_pricing = (
                getattr(response, "model", "") or cfg.get("model") or _DEFAULT_MODEL
            )
            cost_result = estimate_usage_cost(
                _model_for_pricing,
                canonical,
                provider=_resolved_provider,
                base_url=_resolved_base_url,
            )
            if cost_result.amount_usd is not None:
                _record_extraction_cost_usd(float(cost_result.amount_usd))
    except Exception as e:
        logger.debug("memory extraction: cost accounting failed: %s", e)
    return content.strip()


def _maybe_extract_usage(response: Any) -> Optional[Dict[str, int]]:
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    except Exception:
        return None


def _log_telemetry(record: Dict[str, Any]) -> None:
    """Append a one-line jsonl record to memory_extraction.log."""
    try:
        from hermes_constants import get_hermes_home
        log_path = get_hermes_home() / "logs" / "memory_extraction.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str) + "\n"
        with _telemetry_lock:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-turn extraction
# ---------------------------------------------------------------------------

def on_turn_end(
    session_id: str,
    user_msg: Any,
    assistant_msg: Any,
) -> None:
    """Per-turn extraction. Runs in a background thread so we don't block.

    Writes proposals to the session buffer. Final commit happens at
    session-end.
    """
    if not is_enabled() or not session_id:
        return
    if not user_msg and not assistant_msg:
        return

    def _run():
        try:
            cfg = _get_extraction_config()
            response_text = _call_extraction_llm(
                system=_prompts.PER_TURN_SYSTEM,
                user=_prompts.per_turn_user(
                    user_msg=str(user_msg or ""),
                    assistant_msg=str(assistant_msg or ""),
                ),
                max_tokens=int(cfg["max_tokens_per_turn"]),
            )
            entries = _prompts.parse_extraction_response(
                response_text, max_entries=_prompts.MID_SESSION_MAX_ENTRIES,
            )
            if entries:
                appended = _buffer.append(session_id, entries, source="per_turn")
                if appended:
                    logger.debug(
                        "memory extraction: per_turn appended %d entries to session %s",
                        appended, session_id,
                    )
        except Exception as e:
            logger.debug("memory extraction per_turn failed: %s", e)

    # Wait for the previous per-turn extraction (if still running) to
    # avoid backing up the LLM client. Best-effort, short timeout.
    global _per_turn_thread
    with _per_turn_lock:
        if _per_turn_thread and _per_turn_thread.is_alive():
            _per_turn_thread.join(timeout=2.0)
        _per_turn_thread = threading.Thread(
            target=_run,
            name=f"mem-extract-{session_id[:8]}",
            daemon=True,
        )
        _per_turn_thread.start()


# ---------------------------------------------------------------------------
# Pre-compress extraction
# ---------------------------------------------------------------------------

def on_pre_compress(
    session_id: str,
    messages: List[Dict[str, Any]],
) -> None:
    """Pre-compress extraction. Runs inline on the compression slow path.

    Extracts facts from the slice that's about to be compressed/discarded.
    """
    if not is_enabled() or not session_id or not messages:
        return
    cfg = _get_extraction_config()
    if not cfg.get("include_pre_compress", True):
        return

    try:
        response_text = _call_extraction_llm(
            system=_prompts.PRE_COMPRESS_SYSTEM,
            user=_prompts.pre_compress_user(messages),
            max_tokens=int(cfg["max_tokens_per_turn"]),
        )
        entries = _prompts.parse_extraction_response(
            response_text, max_entries=_prompts.MID_SESSION_MAX_ENTRIES,
        )
        if entries:
            appended = _buffer.append(session_id, entries, source="pre_compress")
            logger.info(
                "memory extraction: pre_compress appended %d entries to session %s",
                appended, session_id,
            )
    except Exception as e:
        logger.debug("memory extraction pre_compress failed: %s", e)


# ---------------------------------------------------------------------------
# Session-end extraction + commit
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Session-end cleanup pass (fork-only)
#
# Alongside proposing NEW entries, the session-end sweep reviews EXISTING
# warm-tier facts that are topically related to what this session touched and
# proposes removals/merges for ones that have gone stale, been superseded by
# a new proposal, or duplicate another existing fact.
#
# Safety posture — deletion is strictly higher-risk than addition, so:
#   * cleanup NEVER auto-commits, regardless of ``auto_commit_session_end``;
#   * with no interactive confirm callback, proposals are dropped (logged at
#     debug), never applied;
#   * the LLM can only name fact_ids we explicitly showed it (enforced in
#     ``prompts.parse_cleanup_response`` via ``valid_fact_ids``);
#   * a merge writes the surviving fact BEFORE removing the absorbed one, so
#     a crash mid-way leaves both facts intact rather than losing content.
# ---------------------------------------------------------------------------

# Cap on how many existing facts we pull in as cleanup candidates. Keeps the
# prompt bounded and keeps the blast radius of any single sweep small.
_CLEANUP_CANDIDATE_LIMIT = 25


def _cleanup_seed_text(
    messages: List[Dict[str, Any]],
    entries: List[Dict[str, Any]],
) -> str:
    """Build the FTS5 seed describing what this session was about.

    Proposed entries are the highest-signal summary of the session's durable
    content, so they lead. The tail of the conversation backfills topics that
    didn't produce a proposal.
    """
    parts: List[str] = [str(e.get("content") or "") for e in entries]
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") not in ("user", "assistant"):
            continue
        text = _prompts._truncate_for_extraction(msg.get("content"), 600)
        if text.strip():
            parts.append(text)
        if len(parts) >= 12:
            break
    return " ".join(parts)


def _gather_cleanup_candidates(
    messages: List[Dict[str, Any]],
    entries: List[Dict[str, Any]],
    warm_store: Any,
) -> List[Dict[str, Any]]:
    """Recall existing warm facts topically related to this session."""
    seed = _cleanup_seed_text(messages, entries)
    if not seed.strip():
        return []
    try:
        rows = warm_store.recall_related(seed, top_k=_CLEANUP_CANDIDATE_LIMIT)
    except Exception as e:
        logger.debug("memory cleanup: recall_related failed: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for row in rows or []:
        fact_id = row.get("fact_id")
        if fact_id is None or fact_id in seen:
            continue
        seen.add(fact_id)
        out.append(row)
    return out


def propose_cleanup(
    messages: List[Dict[str, Any]],
    entries: List[Dict[str, Any]],
    *,
    warm_store: Any = None,
    llm_caller: Any = None,
    max_tokens: int = 1024,
) -> List[Dict[str, Any]]:
    """Propose cleanup actions on EXISTING warm facts for this session.

    Returns a list of action dicts, each annotated with the existing fact's
    text so the confirm UI can render it without a second lookup::

        {"fact_id": int, "action": "remove"|"merge", "reason": str,
         "content": str, "category": str,
         "merge_target_id": int?, "merge_target_content": str?,
         "merged_content": str?}

    Empty list on any failure — cleanup is strictly best-effort and doing
    nothing is always the safe outcome.
    """
    if warm_store is None:
        try:
            from tools.memory_warm import get_warm_store
            warm_store = get_warm_store()
        except Exception as e:
            logger.debug("memory cleanup: warm store unavailable: %s", e)
            return []

    candidates = _gather_cleanup_candidates(messages, entries, warm_store)
    if not candidates:
        return []

    by_id = {c["fact_id"]: c for c in candidates}
    if llm_caller is None:
        llm_caller = _call_extraction_llm

    try:
        response_text = llm_caller(
            system=_prompts.SESSION_END_CLEANUP_SYSTEM,
            user=_prompts.session_end_cleanup_user(
                messages or [], entries or [], candidates,
            ),
            max_tokens=max_tokens,
        )
    except Exception as e:
        logger.debug("memory cleanup: LLM call failed: %s", e)
        return []

    actions = _prompts.parse_cleanup_response(
        response_text, valid_fact_ids=set(by_id.keys()),
    )

    annotated: List[Dict[str, Any]] = []
    for action in actions:
        source = by_id.get(action["fact_id"])
        if source is None:
            continue
        entry = {
            **action,
            "content": source.get("content") or "",
            "category": source.get("category") or "general",
        }
        target_id = action.get("merge_target_id")
        if target_id is not None:
            target = by_id.get(target_id)
            if target is None:
                continue
            entry["merge_target_content"] = target.get("content") or ""
            if not entry.get("merged_content"):
                # No merged text from the model — fall back to a lossless
                # concatenation so a merge can never silently drop content.
                entry["merged_content"] = (
                    f"{entry['merge_target_content']} {entry['content']}"
                ).strip()
        annotated.append(entry)
    return annotated


def apply_cleanup_action(
    action: Dict[str, Any],
    *,
    warm_store: Any = None,
) -> Dict[str, Any]:
    """Apply one approved cleanup action to the warm tier.

    ``remove`` deletes the fact. ``merge`` writes the merged text onto the
    surviving target FIRST and only then removes the absorbed source, so an
    interruption between the two steps leaves both facts intact (duplicated
    content) rather than destroying the absorbed fact's text.
    """
    if warm_store is None:
        from tools.memory_warm import get_warm_store
        warm_store = get_warm_store()

    fact_id = action.get("fact_id")
    kind = (action.get("action") or "").lower()

    if kind == "merge":
        target_id = action.get("merge_target_id")
        merged = (action.get("merged_content") or "").strip()
        if target_id is None or not merged:
            return {"action": "cleanup_skipped", "fact_id": fact_id,
                    "error": "merge missing target or merged content"}
        updated = warm_store.update(fact_id=target_id, content=merged)
        if not updated.get("success"):
            return {"action": "cleanup_skipped", "fact_id": fact_id,
                    "error": updated.get("error") or "merge target update failed"}
        removed = warm_store.remove(fact_id)
        if not removed.get("success"):
            # Target already carries the merged content, so nothing is lost —
            # the store just holds a redundant copy the user can clean later.
            return {"action": "cleanup_merged_source_retained", "fact_id": fact_id,
                    "merge_target_id": target_id,
                    "error": removed.get("error")}
        return {"action": "cleanup_merged", "fact_id": fact_id,
                "merge_target_id": target_id}

    if kind == "remove":
        removed = warm_store.remove(fact_id)
        if not removed.get("success"):
            return {"action": "cleanup_skipped", "fact_id": fact_id,
                    "error": removed.get("error") or "remove failed"}
        return {"action": "cleanup_removed", "fact_id": fact_id}

    return {"action": "cleanup_skipped", "fact_id": fact_id,
            "error": f"unknown cleanup action {kind!r}"}


def _invoke_confirm_callback(
    confirm_callback: Callable[..., Any],
    entries: List[Dict[str, Any]],
    cleanup: List[Dict[str, Any]],
) -> tuple:
    """Call the confirm callback and normalize its return value.

    Two callback shapes are supported so the hook stays usable by existing
    single-argument callers:

      * ``cb(entries)`` — legacy; returns the approved entry list. No
        cleanup actions are approved.
      * ``cb(entries, cleanup)`` — returns either the approved entry list
        or a dict ``{"entries": [...], "cleanup": [...]}``.
    """
    import inspect
    try:
        arity = len(inspect.signature(confirm_callback).parameters)
    except (TypeError, ValueError):
        arity = 1

    if arity >= 2:
        result = confirm_callback(entries, cleanup)
    else:
        result = confirm_callback(entries)

    if isinstance(result, dict):
        return list(result.get("entries") or []), list(result.get("cleanup") or [])
    return list(result or []), []


def on_session_end(
    session_id: str,
    messages: List[Dict[str, Any]],
    *,
    interactive: bool = False,
    confirm_callback: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Session-end extraction + commit.

    Args:
        session_id: id of the session that just ended
        messages: final conversation state (post-compression)
        interactive: when True, calls ``confirm_callback`` with the proposed
            entry list and uses the returned list. When False, the
            ``auto_commit_session_end`` config flag decides whether entries
            are auto-committed.
        confirm_callback: required when interactive=True. Called as
            ``cb(entries, cleanup)`` when it accepts two parameters,
            otherwise ``cb(entries)``. See ``_invoke_confirm_callback``
            for the accepted return shapes.

    Returns a summary dict:
        {
          "session_id": str,
          "buffered": int,           # entries from per-turn / pre-compress
          "final_proposed": int,     # entries after session-end LLM pass
          "committed": int,          # actually written to warm tier
          "skipped": int,            # rejected by user / dedup'd / errored
          "cleanup_proposed": int,   # existing-fact cleanup actions proposed
          "cleanup_applied": int,    # cleanup actions the user approved+applied
          "cleanup_skipped": int,    # proposed but not applied
          "actions": [...],          # per-entry verdict + outcome
          "cleanup_actions": [...]   # per-cleanup outcome
        }

    Failures degrade gracefully — on any error the buffer is preserved
    so the next session can retry.
    """
    summary: Dict[str, Any] = {
        "session_id": session_id,
        "buffered": 0,
        "final_proposed": 0,
        "committed": 0,
        "skipped": 0,
        "cleanup_proposed": 0,
        "cleanup_applied": 0,
        "cleanup_skipped": 0,
        "actions": [],
        "cleanup_actions": [],
    }
    if not is_enabled() or not session_id:
        return summary

    buffered = _buffer.get_session_entries(session_id)
    summary["buffered"] = len(buffered)

    cfg = _get_extraction_config()

    # Step 1: final extraction pass — reconcile buffer + final messages.
    final_entries: List[Dict[str, Any]] = []
    try:
        response_text = _call_extraction_llm(
            system=_prompts.SESSION_END_SYSTEM,
            user=_prompts.session_end_user(messages or [], buffered),
            max_tokens=int(cfg["max_tokens_session_end"]),
        )
        final_entries = _prompts.parse_extraction_response(
            response_text, max_entries=_prompts.SESSION_END_MAX_ENTRIES,
        )
        summary["final_proposed"] = len(final_entries)
    except Exception as e:
        logger.warning("memory extraction session_end failed: %s — falling back to buffer", e)
        # Fall back to buffer contents so we don't lose proposals.
        final_entries = buffered
        summary["final_proposed"] = len(buffered)

    # Step 1b: cleanup pass over EXISTING warm facts related to this session.
    #
    # Only worth running when a human will actually see the result: cleanup
    # is never auto-committed (see the module note above), so computing it on
    # a non-interactive exit would burn an LLM call to produce proposals we
    # are contractually going to throw away.
    cleanup_proposals: List[Dict[str, Any]] = []
    if interactive and confirm_callback is not None:
        try:
            cleanup_proposals = propose_cleanup(messages or [], final_entries)
        except Exception as e:
            logger.debug("memory cleanup: proposal pass failed: %s", e)
            cleanup_proposals = []
        summary["cleanup_proposed"] = len(cleanup_proposals)

    if not final_entries and not cleanup_proposals:
        # Nothing to commit. Clear the buffer to free space.
        _buffer.clear_session(session_id)
        return summary

    # Step 2: confirm UI (interactive) or auto-commit
    auto_commit = bool(cfg.get("auto_commit_session_end", False))
    approved_cleanup: List[Dict[str, Any]] = []
    if interactive and confirm_callback is not None:
        try:
            approved, approved_cleanup = _invoke_confirm_callback(
                confirm_callback, final_entries, cleanup_proposals,
            )
        except Exception as e:
            logger.warning("memory extraction confirm callback failed: %s", e)
            approved = []
            approved_cleanup = []
    elif auto_commit:
        # NOTE: auto_commit covers NEW entries only. Cleanup mutates/deletes
        # existing facts and always requires explicit confirmation, so any
        # proposals here are dropped rather than applied.
        approved = final_entries
    else:
        # Default safe path: skip auto-commit when the user isn't watching.
        # Stash proposals back into the buffer so they survive. The next
        # interactive session can pick them up via a "memory pending" prompt.
        _buffer.replace_session_entries(session_id, final_entries)
        summary["skipped"] = len(final_entries)
        return summary

    if cleanup_proposals and not approved_cleanup:
        logger.debug(
            "memory cleanup: dropping %d unconfirmed cleanup proposal(s) for session %s",
            len(cleanup_proposals), session_id,
        )
    summary["cleanup_skipped"] = len(cleanup_proposals) - len(approved_cleanup)

    # Step 3: dispatch each approved entry through conflict resolution
    #
    # IMPORTANT: when the proposal already carries a ``verdict`` field
    # (because the confirm UI ran ``_classify_proposals`` and showed it
    # to the user), we MUST reuse that exact verdict here. Re-classifying
    # at commit time would:
    #   1. Lie to the user — they approved based on the displayed verdict;
    #      the LLM is non-deterministic on edge cases and a second roll can
    #      flip DUPLICATE → NEW (or vice versa), polluting the warm store
    #      with duplicates the user thought were being deduped.
    #   2. Double the LLM cost on the slow path (one classify per proposal
    #      in the UI, one again here).
    # Only classify fresh when the verdict isn't pre-attached — i.e. the
    # non-interactive auto-commit path that bypasses the confirm UI.
    from tools.memory_extraction.conflict import ConflictVerdict
    for proposal in approved:
        try:
            attached = proposal.get("verdict")
            if isinstance(attached, ConflictVerdict):
                verdict = attached
            else:
                verdict = _conflict.classify(proposal["content"])
            outcome = _conflict.apply_verdict(verdict, proposal, auto_commit=False)
            summary["actions"].append({
                "content": proposal["content"][:120],
                "verdict": verdict.verdict,
                "outcome": outcome.get("action"),
                "fact_id": outcome.get("fact_id"),
            })
            if outcome.get("action") in (
                "stored", "refined", "deduplicated", "superseded",
            ):
                summary["committed"] += 1
            else:
                summary["skipped"] += 1
        except Exception as e:
            logger.warning("memory extraction commit failed: %s", e)
            summary["skipped"] += 1

    # Step 3b: apply approved cleanup actions — AFTER new entries are
    # committed, so a "superseded" removal never runs before its replacement
    # exists in the store.
    for action in approved_cleanup:
        try:
            outcome = apply_cleanup_action(action)
        except Exception as e:
            logger.warning("memory cleanup: apply failed: %s", e)
            outcome = {"action": "cleanup_skipped", "fact_id": action.get("fact_id"),
                       "error": str(e)}
        summary["cleanup_actions"].append({
            "fact_id": outcome.get("fact_id"),
            "requested": action.get("action"),
            "outcome": outcome.get("action"),
            "reason": action.get("reason", ""),
            "error": outcome.get("error"),
        })
        if outcome.get("action") in (
            "cleanup_removed", "cleanup_merged", "cleanup_merged_source_retained",
        ):
            summary["cleanup_applied"] += 1
        else:
            summary["cleanup_skipped"] += 1

    # Step 4: clear the buffer — proposals are now committed (or surfaced)
    _buffer.clear_session(session_id)
    return summary


def flush_buffer(session_id: str) -> int:
    """Drop a session's buffer without committing. Used on /reset."""
    return _buffer.clear_session(session_id)
