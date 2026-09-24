#!/usr/bin/env python3
"""Memory Tool - persistent curated memory (MEMORY.md = agent notes, USER.md = user
profile). Both enter the system prompt as a FROZEN snapshot at session start;
mid-session writes hit disk but never change the prompt (prefix cache intact).
Single `memory` tool: add/replace/remove or a batch `operations` list."""

import copy
import json
import logging
from contextvars import ContextVar
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import is_truthy_value
from tools.registry import no_cache_check_fn

# fcntl is Unix-only; Windows uses msvcrt. MemoryStore reads both lazily from
# this module (tests patch ``memory_tool.fcntl``).
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# One tool-definition pass must use ONE config decision for availability and the
# dynamic target schema: the check_fn result flows to the immediately following
# dynamic_schema_overrides call; ContextVar isolates concurrent profile builds.
_memory_surface_flags: ContextVar[Optional[Tuple[bool, bool]]] = ContextVar("memory_surface_flags", default=None)


def get_memory_dir() -> Path:
    """Profile-scoped memories dir, resolved per call (HERMES_HOME may switch after import)."""
    return get_hermes_home() / "memories"


from tools.memory_tool_store import (  # noqa: E402,F401  (re-exports)
    ENTRY_DELIMITER, MEMORY_BLOCK_HEADERS, MemoryStore, _scan_memory_content)


# FORK: warm-tier status block for the system prompt. Upstream extracted MemoryStore into
# tools/memory_tool_store.py, which has no knowledge of the fork's warm tier, so the fork's
# ``format_for_system_prompt("warm_status")`` extension is re-homed here as a thin override
# rather than by editing the extracted upstream module (keeps the fork delta in one file).
# Consumer: agent/system_prompt.py's warm-status block.
def _format_warm_status() -> Optional[str]:
    """Return a one-line warm-tier status block for the system prompt.

    Returns ``None`` when the warm tier is empty or unavailable — callers append the
    result conditionally so the system prompt stays clean for users who haven't migrated.

    Also ``None`` when ``memory.provider: holographic`` is registered: the provider
    contributes its own "# Holographic Memory" block over the very same facts, and
    advertising both would tell the model it has two memories when it has one.
    """
    try:
        from tools.memory_warm import get_warm_store, holographic_provider_is_registered
        if holographic_provider_is_registered():
            return None
        n = get_warm_store().count()
    except Exception:
        return None
    if n <= 0:
        return None
    return (
        "══════════════════════════════════════════════\n"
        f"WARM MEMORY: {n} facts indexed (search-only)\n"
        "══════════════════════════════════════════════\n"
        "Search via memory(action=\"recall\", query=\"...\") when the "
        "user references something cross-session, you suspect related "
        "context exists, or you're debugging a system covered in prior "
        "notes. ~50 tokens per call. Default tier for new entries is "
        "\"warm\" — use tier=\"hot\" only for facts that must influence "
        "every turn (user preferences, recurring corrections)."
    )


def _format_for_system_prompt(self, target: str) -> Optional[str]:
    """Frozen load-time snapshot (NOT live state — mid-session writes don't touch it,
    preserving the prefix cache); None if empty. Special target ``"warm_status"`` (fork)
    returns the warm-tier one-liner, which is safe to include every turn."""
    if target == "warm_status":
        return _format_warm_status()
    return self._system_prompt_snapshot.get(target, "") or None


MemoryStore.format_for_system_prompt = _format_for_system_prompt


def _get_warm_store_or_error():
    """Return the warm-tier store, or a ``tool_error`` JSON if unavailable.

    Refuses when ``memory.provider: holographic`` is registered. Both paths open the
    SAME ``memory_store.db`` and the SAME rows, so serving both to the model gives it
    two names for one memory (``memory(tier="warm", ...)`` and ``fact_store``) and it
    will double-write, or rate through one surface what it recalled through the other.
    The provider wins because it strictly dominates: search, probe, related, reason,
    contradict, plus automatic ``MemoryManager.prefetch_all`` push.

    Internal fork plumbing (hot-tier-audit demote, LLM extraction, session-pin,
    auto-feedback) deliberately bypasses this gate by calling ``get_warm_store()``
    directly — those are this process's own writes over shared rows, not a rival
    surface exposed to the model.
    """
    try:
        from tools.memory_warm import get_warm_store, holographic_provider_is_registered
        if holographic_provider_is_registered():
            return None, tool_error(
                "Warm-tier actions are disabled because memory.provider is set to "
                "'holographic', which serves the same facts through fact_store / "
                "fact_feedback (with entity probe, structural related, multi-entity "
                "reason and contradiction checks). Use those instead. Hot-tier "
                "memory actions are unaffected.",
                success=False,
            )
        return get_warm_store(), None
    except Exception as e:
        return None, tool_error(
            f"Warm-tier memory unavailable: {e}. Hot-tier writes still work.",
            success=False,
        )


def _handle_warm_action(
    action: str,
    args_query: Optional[str],
    args_content: Optional[str],
    args_old_text: Optional[str],
    args_top_k: Optional[int],
    args_category: Optional[str],
    args_tags: Optional[str],
    args_fact_id: Optional[int],
    args_helpful: Optional[bool],
    args_target: Optional[str],
    hot_store: Optional[MemoryStore],
    agent: Optional[Any] = None,
) -> str:
    """Dispatch warm-tier actions. Always returns a JSON string.

    ``agent`` is optional — when provided, voluntary ``recall`` calls
    reset the memory-recall-reminder counter (so the agent doesn't get a
    redundant nudge on the next tool result). Pass ``None`` from tests
    or non-agent callers.
    """
    warm, err = _get_warm_store_or_error()
    if err is not None:
        return err

    if action == "add":
        if not args_content:
            return tool_error("content is required for warm add.", success=False)
        # Warm-tier content is also injected as recall results, so the same
        # injection-safety scan applies.
        scan_error = _scan_memory_content(args_content)
        if scan_error:
            return tool_error(scan_error, success=False)
        result = warm.add(
            content=args_content,
            category=args_category or "general",
            tags=args_tags or "",
        )

    elif action == "recall":
        if not args_query:
            return tool_error("query is required for recall.", success=False)
        rows = warm.recall(
            query=args_query,
            top_k=int(args_top_k) if args_top_k else 5,
            category=args_category,
        )
        # Voluntary recall — reset the recall-reminder counter so the
        # agent doesn't get a nudge on the very next tool result.
        # Best-effort: skip silently when agent ref is unavailable.
        if agent is not None:
            try:
                from agent.fork.memory_recall import record_voluntary_recall
                record_voluntary_recall(agent)
            except Exception:
                pass
        if not rows:
            result = {
                "success": True,
                "results": [],
                "count": 0,
                "message": (
                    "No matches in warm tier. Try different keywords, or "
                    "memory(action=\"read\", tier=\"warm\") to browse."
                ),
            }
        else:
            result = {
                "success": True,
                "results": rows,
                "count": len(rows),
            }

    elif action == "recall_related":
        seed = args_query or args_content or ""
        if not seed and args_fact_id:
            row = warm.get(int(args_fact_id))
            if row is None:
                return tool_error(
                    f"No warm fact with id {args_fact_id}.", success=False,
                )
            seed = row["content"]
        if not seed:
            return tool_error(
                "recall_related requires query, content, or fact_id.",
                success=False,
            )
        rows = warm.recall_related(
            seed=seed, top_k=int(args_top_k) if args_top_k else 5,
        )
        result = {"success": True, "results": rows, "count": len(rows)}

    elif action == "read":
        rows = warm.list_facts(
            category=args_category,
            limit=int(args_top_k) if args_top_k else 50,
        )
        result = {
            "success": True,
            "results": rows,
            "count": len(rows),
            "total_indexed": warm.count(),
        }

    elif action == "remove":
        if args_fact_id is None:
            return tool_error(
                "fact_id is required for warm remove.", success=False,
            )
        result = warm.remove(int(args_fact_id))

    elif action == "replace":
        if args_fact_id is None:
            return tool_error(
                "fact_id is required for warm replace.", success=False,
            )
        if not args_content:
            return tool_error(
                "content is required for warm replace.", success=False,
            )
        scan_error = _scan_memory_content(args_content)
        if scan_error:
            return tool_error(scan_error, success=False)
        result = warm.update(
            fact_id=int(args_fact_id),
            content=args_content,
            tags=args_tags,
            category=args_category,
        )

    elif action == "feedback":
        if args_fact_id is None:
            return tool_error(
                "fact_id is required for feedback.", success=False,
            )
        if args_helpful is None:
            return tool_error(
                "helpful (true/false) is required for feedback.", success=False,
            )
        result = warm.record_feedback(
            fact_id=int(args_fact_id), helpful=bool(args_helpful),
        )

    elif action == "promote":
        # Move a warm fact to the hot tier. Fetch the row, write it to hot,
        # delete from warm only if hot write succeeded.
        #
        # Destination hot target is taken from ``target`` ('memory' or
        # 'user'), defaulting to 'memory'. Earlier versions overloaded
        # ``old_text`` for this — callers passing old_text='user' will
        # still get user-target promotion via the back-compat shim
        # below, but new code should use target=.
        if hot_store is None:
            return tool_error(
                "Hot tier is not available; cannot promote.", success=False,
            )
        if args_fact_id is None:
            return tool_error(
                "fact_id is required for promote.", success=False,
            )
        row = warm.get(int(args_fact_id))
        if row is None:
            return tool_error(
                f"No warm fact with id {args_fact_id}.", success=False,
            )
        # Resolve destination target. Prefer the new explicit ``target``
        # arg; fall back to the legacy ``old_text`` overload only when
        # target wasn't explicitly set to a valid value.
        if args_target in ("memory", "user"):
            hot_target = args_target
        elif args_old_text in ("memory", "user"):
            hot_target = args_old_text  # legacy behavior — preserved
        else:
            hot_target = "memory"
        hot_result = hot_store.add(hot_target, row["content"])
        if not hot_result.get("success"):
            return json.dumps(hot_result, ensure_ascii=False)
        # Hot write succeeded — drop from warm.
        warm.remove(int(args_fact_id))
        result = {
            "success": True,
            "message": f"Promoted warm fact {args_fact_id} to hot tier.",
            "hot_target": hot_target,
            "hot_state": hot_result,
        }

    elif action == "demote":
        # Move a hot entry to warm. Identified by old_text substring (same
        # rules as hot remove).
        #
        # Source hot target is taken from ``target`` ('memory' or 'user'),
        # defaulting to 'memory'. Earlier versions overloaded ``category``
        # for this, which clashed with category's documented meaning
        # ("warm-tier category for the new fact"). New code should use
        # target= for the source and category= for the new warm fact's
        # category. The legacy category-as-target overload is preserved
        # only when ``target`` wasn't explicitly set to a valid value
        # AND ``category`` happens to be 'memory'/'user'.
        if hot_store is None:
            return tool_error(
                "Hot tier is not available; cannot demote.", success=False,
            )
        if not args_old_text:
            return tool_error(
                "old_text is required for demote.", success=False,
            )
        if args_target in ("memory", "user"):
            hot_target = args_target
            warm_category = args_category or "general"
        elif args_category in ("memory", "user"):
            # Legacy overload — category was the source target. Preserved
            # for back-compat; new code should use target=.
            hot_target = args_category
            warm_category = "general"
        else:
            hot_target = "memory"
            warm_category = args_category or "general"
        # Find the hot entry first (without removing it), so we don't
        # delete-without-write if warm add fails.
        # v2026.9.14 extracted MemoryStore into tools/memory_tool_store.py and
        # dropped ``_reload_target`` (its reload is inlined in ``_mutate``), so the
        # old call raised AttributeError and demote was dead. Re-read from disk the
        # way ``_mutate`` does — same lock, same raw snapshot, same de-dup — so a
        # concurrently-edited MEMORY.md can't hand us a stale in-memory entry list.
        _hot_path = hot_store._path_for(hot_target)  # type: ignore[attr-defined]
        with hot_store._file_lock(_hot_path):  # type: ignore[attr-defined]
            _raw, _read_ok = hot_store._read_raw_checked(_hot_path)  # type: ignore[attr-defined]
            if not _read_ok:
                return tool_error(
                    f"Could not read {_hot_path.name}; refusing to demote.", success=False,
                )
            entries = list(dict.fromkeys(hot_store._parse_entries(_raw)))  # type: ignore[attr-defined]
            hot_store._set_entries(hot_target, entries)  # type: ignore[attr-defined]
            matches = [e for e in entries if args_old_text in e]
        if not matches:
            return tool_error(
                f"No hot entry matched '{args_old_text}'.", success=False,
            )
        if len(set(matches)) > 1:
            return tool_error(
                f"Multiple hot entries matched '{args_old_text}'. Be more specific.",
                success=False,
            )
        content = matches[0]
        warm_result = warm.add(
            content=content,
            category=warm_category,
            tags=args_tags or "demoted-from-hot",
        )
        if not warm_result.get("success"):
            return json.dumps(warm_result, ensure_ascii=False)
        # Warm write OK — drop from hot.
        hot_store.remove(hot_target, args_old_text)
        result = {
            "success": True,
            "message": f"Demoted hot entry to warm fact {warm_result.get('fact_id')}.",
            "warm_state": warm_result,
            "hot_target": hot_target,
            "warm_category": warm_category,
        }

    elif action in ("pin", "unpin", "pinned"):
        # Session-pin actions — keep a warm fact visible in the system
        # prompt for the rest of this session. See
        # ``agent.fork.memory_session_pin`` for semantics.
        if agent is None:
            return tool_error(
                "Session-pin requires an agent reference. This call must "
                "originate from the AIAgent runtime (not a subagent stub "
                "or test harness without agent= passed).",
                success=False,
            )
        try:
            from agent.fork import memory_session_pin
        except Exception as e:
            return tool_error(
                f"Session-pin module unavailable: {e}.", success=False,
            )

        if action == "pinned":
            result = memory_session_pin.list_pinned(agent)
        elif action == "pin":
            if args_fact_id is None:
                return tool_error(
                    "fact_id is required for pin.", success=False,
                )
            result = memory_session_pin.pin_fact(agent, int(args_fact_id))
        else:  # unpin
            if args_fact_id is None:
                return tool_error(
                    "fact_id is required for unpin.", success=False,
                )
            result = memory_session_pin.unpin_fact(agent, int(args_fact_id))

        # Pin/unpin mutate the system prompt — invalidate the cached
        # version so the next turn includes the change. Best-effort:
        # any failure here is non-fatal (worst case the pin shows up
        # one turn late).
        if action != "pinned" and result.get("success"):
            try:
                from agent.system_prompt import invalidate_system_prompt
                invalidate_system_prompt(agent)
            except Exception:
                pass

    else:
        return tool_error(
            f"Unknown warm action '{action}'. Use: add, recall, recall_related, "
            f"read, replace, remove, feedback, promote, demote, "
            f"pin, unpin, pinned",
            success=False,
        )

    return json.dumps(result, ensure_ascii=False, default=str)


def load_on_disk_store() -> "MemoryStore":
    """Fresh on-disk MemoryStore with configured limits/flags for contexts with no live
    agent (gateway, Desktop, ``/memory``) so approvals enforce the SAME caps as
    ``agent_init``. Falls back to defaults if config can't load; never raises."""
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        mem_cfg = get_builtin_memory_config(config)
        memory_enabled, user_profile_enabled = get_builtin_memory_store_flags(config)
        store = MemoryStore(int(mem_cfg.get("memory_char_limit", 2200)), int(mem_cfg.get("user_char_limit", 1375)),
                            memory_enabled=memory_enabled, user_profile_enabled=user_profile_enabled)
    except Exception:
        store = MemoryStore()  # config optional — fall back to defaults rather than break /memory
    store.load_from_disk()
    return store


def _pin_matched_entries(store: "MemoryStore", payload: Dict[str, Any]) -> Optional[str]:
    """Record on each staged replace/remove the FULL entry its old_text selects now. Approval
    then applies to exactly the entry the approver reviewed and refuses if it changed:
    re-running the old_text search at approve time could hit a newer entry that still
    contains it. Returns the JSON error when the search fails now, as the direct write would."""
    target = payload.get("target", "memory")
    if payload.get("action") == "batch":
        result = store.resolve_batch_entries(target, payload["operations"])
        if result.get("success"):
            payload["operations"] = [op if entry is None else {**op, "matched_entry": entry}
                                     for op, entry in zip(payload["operations"], result["matched_entries"])]
    elif payload.get("action") in _BG_DELETE_ACTIONS:
        result = store.resolve_entry(target, payload.get("old_text") or "", payload["action"])
        if result.get("success"):
            payload["matched_entry"] = result["matched_entry"]
    else:
        return None
    return None if result.get("success") else json.dumps(result, ensure_ascii=False)


def _gate_or_stage(store: "MemoryStore", summary: str, detail: str, payload: Dict[str, Any]) -> Optional[str]:
    """JSON tool-result string when the write must NOT proceed (blocked or staged
    for approval), None to proceed. Fails open if the gate module can't load."""
    try:
        from tools import write_approval as wa
    except Exception:
        return None
    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)
    if (unmatched := _pin_matched_entries(store, payload)) is not None:
        return unmatched
    record = wa.stage_write(wa.MEMORY, payload, summary=f"{summary}: {detail[:120]}", origin=wa.current_origin())
    return json.dumps({"success": True, "staged": True, "pending_id": record["id"], "message": decision.message},
                      ensure_ascii=False)


# action -> (store call, gate (summary, detail) text) for the live tool path and staged replay.
_STORE_ACTIONS = {
    "add": (lambda store, target, content, old_text, entry=None: store.add(target, content),
            lambda label, content, old_text: (f"add to {label}", content or "")),
    "replace": (lambda store, target, content, old_text, entry=None: store.replace(target, old_text, content, entry),
                lambda label, content, old_text: (f"replace in {label}",
                                                  f"entry matching: {old_text}\nwhole entry becomes: {content}")),
    "remove": (lambda store, target, content, old_text, entry=None: store.remove(target, old_text, entry),
               lambda label, content, old_text: (f"remove from {label}", old_text or ""))}


def _batch_op_line(op: Dict[str, Any]) -> str:
    op = op or {}
    act, content, old = op.get("action", "?"), op.get("content") or op.get("new_text") or "", op.get("old_text", "")
    if act == "remove":
        return f"- remove: {old}"
    # Whole-entry contract (#117952): the approver must not read this as a span patch.
    return (f"- replace entry matching '{old}' -> whole entry becomes: {content}" if act == "replace"
            else f"- {act}: {content}")


def _apply_write_gate(store: "MemoryStore", action: str, target: str, content: Optional[str],
                      old_text: Optional[str], operations: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """Gate one mutating op, or (``operations`` set) a whole batch as a single unit."""
    label = "user profile" if target == "user" else "memory"
    if operations is not None:
        return _gate_or_stage(store, f"apply {len(operations)} op(s) to {label}",
                              "\n".join(_batch_op_line(op) for op in operations),
                              {"action": "batch", "target": target, "operations": operations})
    return _gate_or_stage(store, *_STORE_ACTIONS[action][1](label, content, old_text),
                          {"action": action, "target": target, "content": content, "old_text": old_text})


def _validate_single_op(store, action, target, content, old_text) -> Optional[str]:
    """Validate BEFORE the gate so an invalid write is rejected now, not at approve time.
    Missing ``old_text`` is recoverable (it can't be schema-required — needs a combinator
    the Codex backend rejects): return the inventory plus a retry instruction."""
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action in ("replace", "remove") and not old_text:
        replace_hint = (" For 'replace', content is the COMPLETE new entry -- the whole "
                        "matched entry is overwritten, not just the old_text span."
                        if action == "replace" else "")
        return json.dumps({
            "success": False,
            "error": (f"'{action}' needs old_text -- a short unique substring of the entry "
                      f"to {action}. None was provided. Reissue the {action} with old_text "
                      f"set to part of one of the current_entries below.{replace_hint}"),
            "current_entries": store._entries_for(target), "usage": store._usage(target)}, ensure_ascii=False)
    if action == "replace" and not content:
        return tool_error("content is required for 'replace' action.", success=False)
    return None


_BG_DELETE_ACTIONS = ("replace", "remove")


def destructive_ops(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The replace/remove ops of a staged memory payload, single-op or batch shape."""
    ops = (payload.get("operations") or []) if payload.get("action") == "batch" else [payload]
    return [op for op in ops if (op or {}).get("action") in _BG_DELETE_ACTIONS]


def _background_delete_gate(store, action, operations, target="memory", content=None,
                            old_text=None) -> Optional[str]:
    """Fail-closed operation gate for unattended background-review forks (#105921): ``add``
    stays available (it is all any review prompt asks for), while ``replace``/``remove`` —
    single or inside a batch — are never applied unattended. The op is staged in the pending
    store instead of merely denied: the fork's own review summary is never published back, so
    a plain denial would drop the consolidation request with no surfacing path at all. A
    staging failure fails closed to a plain denial."""
    from tools.skill_provenance import is_unattended_review

    if not is_unattended_review():
        return None
    payload = ({"action": "batch", "target": target, "operations": operations}
               if operations is not None else
               {"action": action, "target": target, "content": content, "old_text": old_text})
    if not destructive_ops(payload):
        return None
    detail = ("; ".join(_batch_op_line(op) for op in operations) if operations is not None
              else _batch_op_line({"action": action, "content": content, "old_text": old_text}))
    try:
        if (unmatched := _pin_matched_entries(store, payload)) is not None:
            return unmatched
        from tools import write_approval as wa
        record = wa.stage_write(
            wa.MEMORY, payload,
            summary=(f"background review consolidation ({'batch' if operations is not None else action} "
                     f"on {target}): {detail}")[:200],
            origin=wa.current_origin())
        return json.dumps({
            "success": True, "staged": True, "proposal_staged": True, "pending_id": record["id"],
            "message": ("Background review may not delete memory entries unattended. The proposed "
                        f"{'batch' if operations is not None else action} was staged for your approval — "
                        "review it with /memory pending (approve to apply, discard to drop)."),
        }, ensure_ascii=False)
    except Exception:
        logger.warning("Failed to stage background-review consolidation; denying", exc_info=True)
        return tool_error(
            "Background review may not delete memory entries ('replace'/'remove', including in a "
            "batch); 'add' is still available.", success=False)


def memory_tool(
    # action may be None when using the batch `operations` shape (upstream);
    # target stays Optional/None so the warm-tier promote/demote dispatch can
    # tell "defaulted" from "explicit" (fork). Hot-tier ops default target to
    # 'memory' internally.
    action: str = None,
    target: Optional[str] = None,
    content: str = None,
    old_text: str = None,
    new_text: str = None,
    operations: Optional[List[Dict[str, Any]]] = None,
    store: Optional[MemoryStore] = None,
    # Warm-tier extension (Phase 1 dynamic memory recall):
    tier: str = "hot",
    query: Optional[str] = None,
    top_k: Optional[int] = None,
    category: Optional[str] = None,
    tags: Optional[str] = None,
    fact_id: Optional[int] = None,
    helpful: Optional[bool] = None,
    # Agent reference for recall-reminder counter reset on voluntary
    # recall calls (see ``agent.fork.memory_recall``). Optional —
    # tests and non-agent callers pass None.
    agent: Optional[Any] = None,
) -> str:
    """Tool entry point; returns a JSON string. Dispatches to MemoryStore (hot tier) or
    WarmStore (warm tier) based on the ``tier`` arg or action.

    Hot tier: ``add``/``replace``/``remove``/``read`` — small, always-loaded, file-backed
    (MEMORY.md / USER.md), bounded by char_limit. Single op (action + content/old_text) or
    batch (``operations``, atomic against the final budget). ``new_text`` aliases ``content``;
    for 'replace' both mean the COMPLETE new entry (the whole matched entry is overwritten,
    old_text only locates it).

    Warm tier: ``add``/``recall``/``recall_related``/``read``/``replace``/``remove``/
    ``feedback`` — unbounded, search-only via FTS5 + BM25, SQLite-backed at
    ``$HERMES_HOME/memory_store.db``. Plus cross-tier: ``promote`` (warm → hot),
    ``demote`` (hot → warm).
    """
    # Warm-tier-only actions route directly regardless of tier param.
    WARM_ONLY_ACTIONS = {
        "recall", "recall_related", "feedback", "promote", "demote",
        # Session-pin actions operate on the agent's session state but
        # are dispatched alongside warm-tier actions because they
        # reference warm-tier fact ids.
        "pin", "unpin", "pinned",
    }
    is_warm_action = (tier == "warm") or (action in WARM_ONLY_ACTIONS)

    # Accept new_text as an alias for content (single-op path). See docstring.
    if content is None and new_text is not None:
        content = new_text

    if is_warm_action:
        return _handle_warm_action(
            action=action,
            args_query=query,
            args_content=content,
            args_old_text=old_text,
            args_top_k=top_k,
            args_category=category,
            args_tags=tags,
            args_fact_id=fact_id,
            args_helpful=helpful,
            args_target=target,
            hot_store=store,
            agent=agent,
        )

    # Hot-tier path (legacy behavior — unchanged for backward compat).
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    # Strict providers send JSON null for optional fields; treat as omitted. The warm path
    # handles its own target resolution (None means "not specified" — see _handle_warm_action's
    # promote/demote branches), so this default is applied only on the hot-tier path.
    target = "memory" if target is None else target
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return json.dumps(target_error)
    if operations:
        if not isinstance(operations, list):
            return tool_error("operations must be a list of {action, content?, old_text?} objects.", success=False)
        denied = _background_delete_gate(store, action, operations, target)
        if denied is not None:
            return denied
        # Approval gate: stages (background/gateway) or prompts inline (CLI); off by default.
        gate_result = _apply_write_gate(store, "batch", target, None, None, operations)
        if gate_result is not None:
            return gate_result
        return json.dumps(store.apply_batch(target, operations), ensure_ascii=False)
    # FORK: hot-tier "read" returns live entries. _success_response() is deliberately
    # write-shaped (entries are withheld after add/replace/remove to avoid inviting the
    # model to re-issue redundant writes); a read has no such write to guard against and
    # its entire purpose is to surface the entries, so build the response directly.
    # See tests/tools/test_memory_warm.py::TestBackwardCompat::test_hot_read_returns_state.
    if action == "read":
        _entries = store._entries_for(target)
        _current, _limit = store._char_count(target), store._char_limit(target)
        _pct = min(100, int((_current / _limit) * 100)) if _limit > 0 else 0
        return json.dumps({
            "success": True, "target": target, "entries": _entries, "entry_count": len(_entries),
            "usage": f"{_pct}% — {_current:,}/{_limit:,} chars",
            "message": "Hot tier entries returned."}, ensure_ascii=False)
    if action not in _STORE_ACTIONS:
        return tool_error(
            f"Unknown action '{action}'. Hot-tier actions: add, replace, remove, read. "
            f"Warm-tier actions (use tier='warm' or these names): "
            f"recall, recall_related, feedback, promote, demote", success=False)
    invalid = (_validate_single_op(store, action, target, content, old_text)
               or _background_delete_gate(store, action, None, target, content, old_text)
               or _apply_write_gate(store, action, target, content, old_text))
    if invalid is not None:
        return invalid
    return json.dumps(_STORE_ACTIONS[action][0](store, target, content, old_text), ensure_ascii=False)


def get_builtin_memory_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalized ``memory`` config section ({} when missing/malformed → flags default to
    enabled). ``agent_init`` reads the same section so availability and store cannot diverge."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read memory config for availability", exc_info=True)
            return {}
    section = config.get("memory") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def get_builtin_memory_store_flags(config: Optional[Dict[str, Any]] = None) -> Tuple[bool, bool]:
    """Return ``(memory_enabled, user_profile_enabled)`` from resolved config."""
    section = get_builtin_memory_config(config)
    return tuple(is_truthy_value(section.get(k), default=True) for k in ("memory_enabled", "user_profile_enabled"))


@no_cache_check_fn
def check_memory_requirements() -> bool:
    """Snapshot store flags and report whether the built-in tool is available."""
    _memory_surface_flags.set(None)
    flags = get_builtin_memory_store_flags()
    _memory_surface_flags.set(flags)
    return flags[0] or flags[1]


def _memory_target_error(store: "MemoryStore", target: str) -> Optional[Dict[str, Any]]:
    """Return a shared validation error for an invalid or disabled target."""
    if target not in {"memory", "user"}:
        from tools.registry import _bound_error_text
        return {"success": False,
                "error": _bound_error_text(f"Invalid memory target '{target}'. Use 'memory' or 'user'.")}
    if store.target_enabled(target):
        return None
    label = "USER.md" if target == "user" else "MEMORY.md"
    return {"success": False, "error": f"Built-in {label} writes are disabled in memory config.", "target": target}


def _apply_extraction_proposal(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged LLM-extraction proposal (``/memory approve`` on a session-exit review).

    Unlike the hot-tier actions around it, a warm proposal is applied through the
    extraction pipeline's conflict resolver so DUPLICATE/REFINEMENT/CONTRADICTION
    still do the right thing (dedupe / merge into the matched fact / supersede it)
    instead of blindly appending a new row.

    The verdict is rehydrated from the record, never recomputed: the user approved
    the verdict they were SHOWN at stage time, and the classifier is an LLM whose
    second roll can disagree with its first (see ``conflict.verdict_to_dict``).
    """
    content = (payload.get("content") or "").strip()
    if not content:
        return {"success": False, "error": "Staged proposal has no content."}

    # tier=hot proposals are ordinary MEMORY.md/USER.md adds — no warm store involved.
    if (payload.get("tier") or "warm").lower() == "hot":
        return _STORE_ACTIONS["add"][0](store, payload.get("target") or "memory", content, "")

    try:
        from tools.memory_extraction import conflict
    except Exception as e:
        return {"success": False, "error": f"Memory extraction unavailable: {e}"}

    verdict = conflict.verdict_from_dict(payload.get("conflict"))
    try:
        # auto_commit=True: the review already happened (at stage time, and again
        # when the user ran /memory approve) — a CONTRADICTION must now be applied,
        # not deferred back into a second pending state.
        outcome = conflict.apply_verdict(verdict, {
            "content": content,
            "category": payload.get("category") or "general",
            "tags": payload.get("tags") or "",
        }, auto_commit=True)
    except Exception as e:
        logger.warning("Failed to apply staged memory proposal: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}
    return {"success": True, "tier": "warm", "verdict": verdict.verdict,
            "outcome": outcome.get("action"), "fact_id": outcome.get("fact_id")}


def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged write against the store, bypassing the gate (/memory approve). A
    replace/remove applies to exactly its pinned ``matched_entry`` or is refused; a record
    staged before pinning has no verifiable target, so it is refused rather than replayed by
    old_text (which could hit a newer entry the approver never saw)."""
    action, target = payload.get("action"), payload.get("target", "memory")
    # Warm-tier extraction proposals don't write through MemoryStore at all, so the
    # hot-tier target check below doesn't apply to them (their 'target' is unset).
    if action == "extraction_proposal" and (payload.get("tier") or "warm").lower() != "hot":
        return _apply_extraction_proposal(payload, store)
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return target_error
    if action == "extraction_proposal":
        return _apply_extraction_proposal(payload, store)
    if any(not op.get("matched_entry") for op in destructive_ops(payload)):
        return {"success": False, "error": "This destructive pending write predates entry pinning and cannot be "
                                           "verified; nothing was applied. Reject it and recreate the change."}
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action not in _STORE_ACTIONS:
        return {"success": False, "error": f"Unknown staged action '{action}'."}
    return _STORE_ACTIONS[action][0](store, target, payload.get("content") or "", payload.get("old_text") or "",
                                     payload.get("matched_entry"))


MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory and recall it across sessions. "
        "TWO TIERS, ONE TOOL — pick the right tier per fact.\n\n"
        "HOT TIER (tier='hot', the default for add/replace/remove with target='memory' or 'user'):\n"
        "  - Always loaded into the system prompt at session start. Costs tokens every turn forever.\n"
        "  - SMALL CAP (~600+400 chars combined). Use only for facts that MUST influence every turn.\n"
        "  - Best fit: user preferences, recurring corrections, routing rules, hot environment quirks.\n"
        "  - Targets: 'memory' (your notes) or 'user' (who the user is).\n\n"
        "WARM TIER (tier='warm' on add, or use any warm-only action):\n"
        "  - Searchable via memory(action='recall', query='...'). Not in the prompt by default.\n"
        "  - UNBOUNDED. SQLite + FTS5 keyword search with trust scoring.\n"
        "  - Best fit: factual reference (TDS internals, MCP procedures), debugging notes, "
        "project conventions, lessons learned. Anything you'd otherwise jam into hot tier and run out of room.\n"
        "  - DEFAULT for new content unless it genuinely belongs in hot tier — when in doubt, warm.\n\n"
        "BATCH (hot tier): make multiple hot-tier changes in ONE call via an 'operations' array "
        "(each item: {action, content?, old_text?}). The batch applies atomically and the hot-tier "
        "char limit is checked only on the FINAL result — so a single call can remove/replace stale "
        "entries to free room AND add new ones, even when an add alone would overflow. Use the bare "
        "action/content/old_text fields only for a single lone change.\n\n"
        "WHEN TO SAVE (proactively, don't wait):\n"
        "- User corrects you or says 'remember this'\n"
        "- User shares a preference / personal detail → HOT (target='user')\n"
        "- You discover environment / project / API quirks → WARM\n"
        "- You learn a stable fact useful in future sessions → WARM unless it's a recurring correction\n\n"
        "ACTIONS:\n"
        "  HOT-TIER: add (target+content), replace (target+old_text+content), "
        "remove (target+old_text), read (target). Batch via 'operations'.\n"
        "  WARM-TIER: add (content [+category +tags]), recall (query [+top_k +category]), "
        "recall_related (query OR fact_id), read ([+category +top_k]), "
        "replace (fact_id+content), remove (fact_id), "
        "feedback (fact_id+helpful) — train trust scores by rating retrieved facts.\n"
        "  SESSION-PIN: pin (fact_id) — keep a warm fact visible in the system prompt "
        "for the rest of THIS session; unpin (fact_id); pinned — list current pins. "
        "Use pin when a fact applies to your whole current investigation; gone on session restart.\n"
        "  CROSS-TIER: promote (fact_id [+target]) — move warm fact to hot tier "
        "(target='memory' or 'user', defaults to 'memory'); "
        "demote (old_text [+target +category]) — move hot entry to warm "
        "(target picks the source hot tier; category sets the new warm category).\n\n"
        "RECALL: use memory(action='recall', query='...') when the user references something cross-session, "
        "you suspect related context exists from prior work, or you're debugging a system covered in older notes. "
        "It's keyword search (BM25), so use exact terms / proper nouns when possible. ~50 tokens per call.\n\n"
        "IF FULL: a hot-tier add is rejected with the current entries shown. Reissue as ONE batch "
        "that removes or shortens enough stale entries and adds the new one together.\n\n"
        "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO state. "
        "Use session_search for those. If you've solved a non-trivial problem worth reusing, save it as a skill.\n\n"
        "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps, and temporary task state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "add", "replace", "remove", "read",
                    "recall", "recall_related",
                    "feedback", "promote", "demote",
                    "pin", "unpin", "pinned",
                ],
                "description": (
                    "The action to perform (single-op shape). Omit when using the "
                    "hot-tier 'operations' batch array."
                ),
            },
            "tier": {
                "type": "string",
                "enum": ["hot", "warm"],
                "description": (
                    "Which tier to write/read. Defaults to 'hot' for backward compat. "
                    "Use 'warm' for new content unless it must be always-loaded. "
                    "Warm-only actions (recall, recall_related, feedback, promote, demote) "
                    "ignore this param."
                ),
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": (
                    "'memory' for personal notes, 'user' for user profile. "
                    "Hot tier (add/replace/remove/read): which file to operate on. "
                    "Cross-tier promote: which hot file the fact lands in. "
                    "Cross-tier demote: which hot file the fact comes from. "
                    "Defaults to 'memory'. Ignored for warm-tier-only actions "
                    "(add/recall/recall_related/read/replace/remove/feedback)."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "The entry content. Required for 'add' and 'replace' (both tiers). "
                    "For 'replace' it is the COMPLETE new entry text: the whole matched "
                    "entry is overwritten, so include everything you want to keep. "
                    "Alias: 'new_text' is also accepted (same full-entry meaning)."
                ),
            },
            "old_text": {
                "type": "string",
                "description": (
                    "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique "
                    "substring IDENTIFYING the existing entry to modify — it locates the "
                    "entry, it is not spliced out. Omit only for 'add'. Hot tier (and "
                    "cross-tier demote): the substring identifies the entry to replace, "
                    "remove, or demote. Ignored for warm tier (warm uses fact_id)."
                ),
            },
            "new_text": {
                "type": "string",
                "description": "Alias for 'content' (single-op shape): the COMPLETE new entry for 'replace', not a patch of old_text. If both are set, 'content' wins."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Hot-tier batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple hot-tier "
                    "changes or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string", "description": "Entry content for add/replace. For replace, the COMPLETE new entry (whole entry is overwritten). Alias: 'new_text'."},
                        "new_text": {"type": "string", "description": "Alias for 'content' in a batch op."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                    },
                    "required": ["action"],
                },
            },
            "query": {
                "type": "string",
                "description": (
                    "Warm-tier search query. Required for 'recall'. Used as the seed for "
                    "'recall_related' if no fact_id is given. Plain text — keyword search."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Warm-tier max results (default 5, max 25). For 'read' max 200.",
            },
            "category": {
                "type": "string",
                "description": (
                    "Warm-tier category filter / assignment. Free-form string "
                    "(e.g. 'tanium', 'debugging', 'preferences'). Defaults to 'general' on add."
                ),
            },
            "tags": {
                "type": "string",
                "description": (
                    "Warm-tier tags on add/replace. Comma-separated free-form (e.g. 'tds,mcp,review')."
                ),
            },
            "fact_id": {
                "type": "integer",
                "description": (
                    "Warm-tier fact id. Required for 'replace'/'remove'/'feedback'/'promote'. "
                    "Returned by 'add'/'recall'."
                ),
            },
            "helpful": {
                "type": "boolean",
                "description": (
                    "Warm-tier feedback flag. True → trust+0.05, helpful_count+1. "
                    "False → trust-0.10. Helps the recall ranker prefer reliable facts."
                ),
            },
        },
    },
}


# Schema text when only one built-in store is enabled: (target description, TARGETS replacement).
_SINGLE_TARGET_TEXT = {
    ("memory",): ("The enabled built-in store: 'memory' for personal notes.",
                  "TARGET: only 'memory' is enabled for personal notes (environment, conventions, "
                  "tool quirks, lessons)."),
    ("user",): ("The enabled built-in store: 'user' for user profile.",
                "TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).")}


def _build_memory_schema_overrides() -> Dict[str, Any]:
    """Narrow the advertised target surface using the availability snapshot."""
    flags = _memory_surface_flags.get() or get_builtin_memory_store_flags()
    _memory_surface_flags.set(None)
    targets = [t for t, on in zip(("memory", "user"), flags) if on]
    parameters = copy.deepcopy(MEMORY_SCHEMA["parameters"])
    target_schema, description = parameters["properties"]["target"], MEMORY_SCHEMA["description"]
    target_schema["enum"] = targets
    # Fork note: upstream narrows the advertised target by str.replace()-ing a verbatim
    # "TARGETS: 'user' = ... 'memory' = ..." sentence out of its own description. The fork
    # rewrote that description wholesale for the hot/warm two-tier surface, so upstream's
    # anchor sentence does not exist here and the replace() silently no-ops — the model
    # would still be told both stores exist while the enum advertises one. Append an
    # explicit notice instead: same information, no dependence on either side's prose.
    if narrowed := _SINGLE_TARGET_TEXT.get(tuple(targets)):
        target_schema["description"] = narrowed[0]
        description += (
            f"\n\nSTORE AVAILABILITY: {narrowed[1][len('TARGET: '):]} "
            f"The other built-in store is disabled — do not pass it as target.")

    return {"description": description, "parameters": parameters}


from tools.registry import registry, tool_error  # noqa: E402  (registration at import time)

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        # target=None means "not specified" — memory_tool defaults it to 'memory' on
        # hot-tier ops and treats None as a signal to the warm promote/demote branches.
        target=args.get("target"),
        store=kw.get("store"),
        **{k: args.get(k) for k in ("content", "old_text", "new_text", "operations",
                                    "query", "top_k", "category", "tags", "fact_id", "helpful")},
        tier=args.get("tier", "hot"),
    ),
    check_fn=check_memory_requirements,
    emoji="🧠",
    dynamic_schema_overrides=_build_memory_schema_overrides)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from contextlib import contextmanager  # noqa: F401,E402
import time  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'atomic_write_text': ('utils', 'atomic_write_text'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
