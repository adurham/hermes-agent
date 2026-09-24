#!/usr/bin/env python3
"""
Delegate Tool -- Subagent Architecture

Spawns child AIAgent instances with a fresh conversation, their own task_id
(terminal session, file-ops cache), the parent's toolsets minus child-blocked
tools, and a focused system prompt built from goal + context. Single-task and
batch (parallel) modes; top-level model calls run in the background while
orchestrator children wait for their own workers. The parent only ever sees
the delegation call and the summary result, never the child's intermediate
tool calls or reasoning.
"""

import logging
import time
import weakref
from typing import Any, Dict, List, Optional, Set

from tools.terminal_tool import set_approval_callback as _set_subagent_approval_cb  # noqa: F401  (used via _ChildRun.await_child)
from utils import is_truthy_value

logger = logging.getLogger(__name__)

# The delegate_tool_* siblings hold the pieces split out of this module; every name callers or patching tests reach as
# ``tools.delegate_tool.<name>`` is re-imported here. Mutable flag globals live only in their owning module.
from tools.delegate_tool_child_run import (  # noqa: F401
    _ABANDON_POLL_INTERVAL, _ChildRun, _DelegationAbandoned, _attach_child, _build_child_goal_message,
    _build_result_entry, _dump_subagent_timeout_diagnostic, _fabricated_entry,
    _lease_child_credential, _merge_late_steer, _register_child, _start_heartbeat, _validate_child_output_schema,
)
from tools.delegate_tool_config import (  # noqa: F401
    _DEFAULT_MAX_CONCURRENT_CHILDREN, _get_child_timeout, _get_max_async_children, _get_max_concurrent_children,
    _get_max_spawn_depth, _get_oneshot_max_children, _get_orchestrator_enabled, _get_subagent_approval_callback, _get_worktree_isolation,
    _inherit_parent_capabilities, _load_config, _merge_request_overrides, _resolve_child_credential_pool,
    _resolve_child_runtime, _resolve_delegation_credentials,
    _subagent_auto_approve, _subagent_auto_deny,
)
from tools.delegate_tool_dispatch import (  # noqa: F401
    _Batch, _announce_batch, _capture_origin, _owner_abandoned, _run_batch, _teardown_abandoned_children,
)
from tools.delegate_tool_progress import (  # noqa: F401
    DelegateEvent, SUBAGENT_FAILURE_STATUSES, _batch_prefix, _build_child_progress_callback,
    _build_child_system_prompt, _clean_error_text, _emit_parent_console, _quiet, _resolve_workspace_hint,
    _safe_progress, format_batch_tag, format_subagent_failure_line,
)
from tools.delegate_tool_registry import (  # noqa: F401
    _CONTROL_ACTIONS, _active_subagents, _active_subagents_lock, _capture_gateway_steer_authority,
    _handle_control_action, _is_descendant_of, _owns_subagent_record, _register_subagent, _unregister_subagent,
    get_subagent_attribution, interrupt_subagent, is_spawn_paused, list_active_subagents, set_spawn_paused,
    steer_subagent,
)
from tools.delegate_tool_tasks import (  # noqa: F401
    _MAX_TASK_IMAGES, _coerce_task_images, _coerce_task_schemas, _normalize_task_images, _normalize_task_list,
)
from tools.delegate_tool_toolsets import (  # noqa: F401
    DELEGATE_BLOCKED_TOOLS, _expand_parent_toolsets, _resolve_child_toolsets, _strip_blocked_tools,
)
from tools.delegate_tool_results import (  # noqa: F401
    _apply_summary_budget, _build_child_preserving_parent_tools, _MIN_SUMMARY_CHARS, _parent_summary_char_budget,
    _run_child_lifecycle, _summarize_tool_arguments,
)

_ROLES = frozenset({"leaf", "orchestrator"})

# Nested delegation is granted by depth/role in _build_child_agent, never by the
# model naming toolsets (there is no model-facing toolsets argument).
def _normalize_role(r: Optional[str]) -> str:
    """'leaf' | 'orchestrator'; None/empty/unknown -> 'leaf' (unknown warns)."""
    r_norm = str(r).strip().lower() if r else "leaf"
    if r_norm not in _ROLES:
        logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
        return "leaf"
    return r_norm

DEFAULT_MAX_ITERATIONS = 250
_HEARTBEAT_INTERVAL = 30  # seconds between parent activity heartbeats during delegation
# Stale-heartbeat thresholds (cycles of _HEARTBEAT_INTERVAL with no progress). Progress = iteration, current_tool OR
# last_activity_ts advancing; an in-flight model wait refreshes last_activity_ts, so slow models are not "idle". Idle
# stays tight so a truly wedged child doesn't mask the gateway timeout; in-tool is much higher so legitimately long
# tools can finish.
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 450s idle between turns → stale
_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 1200s stuck on same tool → stale

def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _open_child_session_db(parent_agent) -> Any:
    """DEDICATED SessionDB handle for the child, or None: the parent's handle can be closed by its own lifecycle while
    a background child still flushes (transcript silently dropped). It MUST open the same db FILE as the parent's
    handle (non-launch profiles), else lineage / session_search break; released by the child's close() via
    _owns_session_db."""
    # Each child gets a DEDICATED SessionDB connection instead of the parent's live object. The parent's
    # handle is owned by the parent's lifecycle (cron run_job's finally block, gateway session end, /new)
    # and can be closed while a fire-and-forget background child is still flushing on a daemon thread —
    # every subsequent flush then hits the closed handle and the child's transcript is silently dropped
    # (#81267). It MUST point at the same database FILE as the parent's handle: parents can hold non-default
    # per-profile handles (tui_gateway opens SessionDB(db_path=<profile>/ state.db) for non-launch
    # profiles), and a bare SessionDB() would write the child's transcript into the launch profile's db,
    # breaking parent_session_id lineage and session_search. AsyncSessionDB wrappers (gateway) forward
    # .db_path via __getattr__, so this works through them.
    parent_session_db = getattr(parent_agent, "_session_db", None)
    if parent_session_db is None:
        return None
    with _quiet("subagent: failed to open dedicated SessionDB; child persistence disabled", exc_info=True):
        from hermes_state_registry import acquire
        _parent_db_path = getattr(parent_session_db, "db_path", None)
        return acquire(_parent_db_path) if _parent_db_path is not None else acquire()
    return None

def _apply_child_cache_ttl(child) -> None:
    """A delegated child never uses the 1h cache tier. The tier is priced for a person who steps
    away between turns (2x write vs 1.25x for 5m, #14971); a subagent calls every few seconds for
    minutes and is gone, so it pays the 2x on every tool result and never collects the retention.
    Caching itself stays exactly as configured (disabled stays disabled)."""
    if getattr(child, "_cache_ttl", None) == "1h":
        child._cache_ttl = "5m"

_CHILD_CAP_MIN = 16_000  # below this a child compresses on every call; treat as a config error


def _child_compression_cap_tokens(raw) -> "int | None":
    """Validated ``delegation.compression_threshold_tokens``: an int >= 16000, or None for "no cap".

    Unset / ``0`` / ``false`` / ``null`` mean no subagent-specific cap: the child compacts at the
    same ratio trigger as everyone else (0.50 x window). A bool ``true`` (YAML) would coerce to 1
    and make every call compress; a string like ``"200k"`` would silently read as no cap. Both are
    config errors: warn and treat as unset so a typo never changes compaction behaviour."""
    if raw is None or raw is False or raw == 0:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) < _CHILD_CAP_MIN:
        logger.warning(
            "delegation.compression_threshold_tokens=%r is not a token count >= %d; ignoring it "
            "(children keep the ratio trigger).", raw, _CHILD_CAP_MIN,
        )
        return None
    return int(raw)


def _apply_child_compression_cap(child, delegation_cfg: dict) -> None:
    """Optional absolute cap on the child's compaction trigger, ``delegation.compression_threshold_tokens``
    (lower of it and any global ``compression.threshold_tokens``). Off by default: a 1M-window child
    compacts where its parent does. The compressor applies the cap on first window resolution, which
    happens after construction, so setting it here is exactly equivalent to config."""
    from agent.context_compressor import ContextCompressor

    cc = getattr(child, "context_compressor", None)
    if not isinstance(cc, ContextCompressor):
        return
    cap = _child_compression_cap_tokens((delegation_cfg or {}).get("compression_threshold_tokens"))
    if cap is None:
        return
    existing = cc.threshold_tokens_cap
    cc.threshold_tokens_cap = min(cap, existing) if isinstance(existing, int) and existing > 0 else cap
    if cc._threshold_tokens is not None:  # already resolved: re-clamp now
        cc._apply_threshold_tokens_cap()


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    task_count: int,
    parent_agent,
    # Credential overrides from delegation config
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    override_request_overrides: Optional[Dict[str, Any]] = None,
    # Per-role max output tokens: a role pinned to another provider carries that
    # provider's own max_output_tokens, which must not be replaced by the parent's.
    override_max_tokens: Optional[int] = None,

    # ACP transport overrides from trusted delegation config.
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
    # Per-role fallback (delegation.model_by_role.<role>.fallback): a ONE-ENTRY runtime
    # fallback chain (the same {provider, model, ...} shape AIAgent.fallback_model accepts),
    # carrying THIS role's own fallback bundle. When given (even as an empty list) it
    # REPLACES the standard parent-chain-inheritance precedence rather than combining with
    # it — a role's fallback is independent of whatever the parent session happens to use.
    # ``None`` (the default, what every pre-existing caller passes) leaves the existing
    # precedence untouched, so this parameter is purely additive.
    override_fallback_chain: Optional[List[Dict[str, Any]]] = None,
    # Configuration block that owns the selected provider/model route. Internal
    # callers such as /review pass auxiliary.review here so fallback policy is
    # not accidentally read from the general delegation block.
    routing_cfg: Optional[Dict[str, Any]] = None,
    # Legacy; accepted for wire compat but ignored (capability is depth-derived).
    role: str = "leaf",
    # Optional ruflo agent persona (e.g. "researcher", "code-analyzer").
    # When set, ruflo's discovered .md prompt is prepended to the child's
    # system prompt and a per-role model override is consulted.
    agent_type: Optional[str] = None,
    # True when this child is part of a background=true delegation. Gates
    # the cross_session toolset: only a background child's parent keeps its
    # own conversation loop running and can actually react to a message
    # (docs/design/local-agent-messaging.md, Question 4 resolution).
    background: bool = False,
):
    """Build (don't run) a child AIAgent on the main thread. override_* (from delegation config) replace parent
    inheritance so children can run on a different provider:model pair."""
    import uuid as _uuid
    from run_agent import AIAgent
    from agent.delegation_context import delegated_child_context
    # Role is depth-derived: a child may delegate iff the kill switch is on and
    # depth budget remains below max_spawn_depth. The `role` arg is ignored.
    child_depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    max_spawn = _get_max_spawn_depth()
    effective_role = "orchestrator" if _get_orchestrator_enabled() and child_depth < max_spawn else "leaf"

    # One subagent_id shared by the progress callback, spawn_requested event and
    # the live registry; parent_id is set when THIS parent is itself a subagent.
    subagent_id = f"sa-{task_index}-{_uuid.uuid4().hex[:8]}"
    parent_subagent_id = getattr(parent_agent, "_subagent_id", None)

    # General delegation behavior (reasoning, compression, capabilities) stays
    # global. Only fallback policy follows the owner of a per-call route such
    # as auxiliary.review.
    delegation_cfg = _load_config()
    child_toolsets, child_disabled_toolsets = _resolve_child_toolsets(parent_agent, toolsets, effective_role)

    # Cross-agent messaging (docs/design/local-agent-messaging.md): a child gets
    # send_to_parent ONLY under background=true. A synchronous child's parent is blocked
    # inside the batch polling loop and cannot act on anything it sends, so shipping the
    # schema would be pure token cost. send_agent_message (the recipient-taking tool) is
    # never granted to a subagent in any mode; it stays parent/session-only.
    from tools.agent_messaging_contract import TOOLSET_NAME as _MSG_TOOLSET, TOOLSET_NAME_VISIBILITY as _VIS_TOOLSET

    if background:
        if _MSG_TOOLSET not in child_toolsets:
            child_toolsets.append(_MSG_TOOLSET)
        child_disabled_toolsets = [name for name in child_disabled_toolsets if name != _MSG_TOOLSET]
    else:
        child_toolsets = [t for t in child_toolsets if t != _MSG_TOOLSET]
        if _MSG_TOOLSET not in child_disabled_toolsets:
            child_disabled_toolsets.append(_MSG_TOOLSET)

    # Read-only agent/subagent visibility (list_agents), UNCONDITIONAL on background —
    # unlike the SEND tools above there is no "parent's thread is blocked, can't react"
    # reason to withhold a read-only lookup from a synchronous child. A synchronous
    # subagent doing file edits is the common working-directory-collision case this
    # exists to catch. Overrides even an inherited disable: delegation plumbing granted
    # to every spawned child, not a per-session opt-in (2026-08-11: this toolset started
    # life folded into cross_session and background-gated by copy-paste from the SEND
    # tools' rationale, which starved every synchronous child of it).
    if _VIS_TOOLSET not in child_toolsets:
        child_toolsets.append(_VIS_TOOLSET)
    child_disabled_toolsets = [name for name in child_disabled_toolsets if name != _VIS_TOOLSET]
    # Cross-agent stomp prevention: WARN, never block. The repo-editing tools (patch, write_file, git) are the real
    # safety net, and a hard block on cwd overlap false-positives every time two subagents legitimately work the same
    # repo on unrelated files. A warning the child sees before it starts editing is the right strength.
    workspace_hint = _resolve_workspace_hint(parent_agent)
    _collision_warning: Optional[str] = None
    if workspace_hint:
        _collisions = []
        with _quiet("delegate_task: cwd collision scan failed: %s"):
            from tools.cross_session_transport import find_cwd_collisions
            _collisions = find_cwd_collisions(workspace_hint)
        if _collisions:
            _lines = "\n".join(
                f"- {c.subagent_id} (owner session: {c.owner_session_id}, status: {c.status}, "
                f"goal: {(c.goal or 'n/a')[:100]})"
                for c in _collisions[:5]
            )
            _collision_warning = (
                f"WARNING: {len(_collisions)} other live subagent(s) on this machine are already working in this "
                f"same directory (or a parent/child of it): {workspace_hint}\n" + _lines
                + "\n\nCheck list_agents for the current picture before editing files here -- another concurrent "
                "session's subagent may be mid-edit on the same repo right now."
            )
    child_prompt = _build_child_system_prompt(
        goal, context, workspace_path=workspace_hint, role=effective_role,
        max_spawn_depth=max_spawn, child_depth=child_depth, agent_type=agent_type,
        cwd_collision_warning=_collision_warning,
    )
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # Shared ref: session_id once the child exists, delegation_id once
    # delegate_task stamps it — both ride on every relayed event.
    child_session_ref: Dict[str, Any] = {}
    # Same late-binding slot for the child object itself: the relay is built before the
    # child exists, and every relayed event re-reads its LIVE model/provider so a mid-run
    # failover shows up on the wire instead of the dispatch-time snapshot.
    child_agent_ref: Dict[str, Any] = {}
    child_progress_cb = _build_child_progress_callback(
        task_index, goal, parent_agent, task_count, subagent_id=subagent_id, parent_id=parent_subagent_id,
        depth=max(0, child_depth - 1),  # 0 = first-level child for the UI
        model=model or getattr(parent_agent, "model", None), toolsets=child_toolsets, session_ref=child_session_ref,
        agent_ref=child_agent_ref,
    )
    rt = _resolve_child_runtime(
        parent_agent, delegation_cfg, parent_api_key, model=model, override_provider=override_provider,
        override_base_url=override_base_url, override_api_key=override_api_key, override_api_mode=override_api_mode,
        override_acp_command=override_acp_command,
        override_acp_args=override_acp_args,
        routing_cfg=routing_cfg,
    )
    # A role's own fallback bundle REPLACES the inherited/pinned chain resolution above
    # (including an empty list, which means "no runtime fallback for this hop" — used when
    # the primary credential resolution already failed and we dispatched straight onto the
    # fallback bundle, so the one hop is already spent).
    if override_fallback_chain is not None:
        rt["fallback_model"] = override_fallback_chain
    # A role pinned to another provider carries that provider's own token ceiling; the
    # parent's is only the default when the role didn't supply one.
    if override_max_tokens is not None:
        rt["max_tokens"] = override_max_tokens
    if override_request_overrides is not None:
        # honored whenever set, incl. the inherit branch where
        # _resolve_delegation_credentials already merged OVER the parent's
        request_overrides = dict(override_request_overrides)
    else:
        request_overrides = {} if override_provider else dict(getattr(parent_agent, "request_overrides", {}) or {})
    parent_sid = getattr(parent_agent, "session_id", None)
    child_session_db = _open_child_session_db(parent_agent)
    with delegated_child_context():
        try:
            child = AIAgent(
                **rt, max_iterations=max_iterations, prefill_messages=getattr(parent_agent, "prefill_messages", None),
                enabled_toolsets=child_toolsets, disabled_toolsets=child_disabled_toolsets, quiet_mode=True,
                ephemeral_system_prompt=child_prompt, log_prefix=f"[subagent-{task_index}]", platform="subagent",
                skip_context_files=True, skip_memory=True, clarify_callback=None,
                thinking_callback=(
                    (lambda text: _safe_progress(child_progress_cb, "_thinking", text) if text else None)
                    if child_progress_cb else None
                ),
                session_db=child_session_db, parent_session_id=parent_sid, request_overrides=request_overrides,
                tool_progress_callback=child_progress_cb,
                iteration_budget=None,  # fresh budget per subagent
            )
        except BaseException:
            # No child close() will ever run: release the dedicated handle here.
            if child_session_db is not None:
                with _quiet(None):
                    from hermes_state_registry import release_or_close
                    release_or_close(child_session_db)
            raise
    child._print_fn = getattr(parent_agent, "_print_fn", None)
    _apply_child_cache_ttl(child)
    if child_session_db is not None:
        child._owns_session_db = True  # released by the child's close(), never by the parent
    # Ownership transfer for the dedicated handle: the child's close() must release it (nothing else holds a
    # reference), and no parent teardown can close it out from under a background child (#81267).
    child_session_ref["session_id"] = getattr(child, "session_id", "") or ""
    child._progress_identity_ref = child_session_ref
    child._delegate_depth, child._delegate_role = child_depth, effective_role  # post-degrade role
    # Stash the cwd-collision warning (if any) so the dispatch payload can surface it to the PARENT's own turn too,
    # not just the child's system prompt: a parent that dispatched two conflicting subagents in one turn should see
    # it immediately, not discover it when the summary lands.
    child._delegate_cwd_collision_warning = _collision_warning
    # Stash the ruflo persona so _run_single_child can tag delegation_stats with the right
    # role identifier and agent/turn_context.py can read it back per turn. Empty string
    # means the caller passed none — stats land in the "(untagged)" bucket.
    _dispatch_agent_type = (agent_type or "").strip()
    child._delegate_agent_type = _dispatch_agent_type
    # Dispatch-time observability: log WHICH role/persona this child was dispatched as.
    # model=/provider= alone are ambiguous when several roles share one default model
    # (e.g. pm and coder both defaulting to glm-5.3), so without this line the dispatched
    # role was only recoverable from the delegation-stats record AFTER completion — and
    # not at all for abandoned/errored children. Pairs with the per-turn "conversation
    # turn:" line in agent/turn_context.py, which reads these same attributes back.
    logger.info(
        "delegate_task: spawned subagent id=%s role=%s agent_type=%s model=%s provider=%s depth=%d task=%r",
        subagent_id, effective_role, _dispatch_agent_type or "none", rt.get("model"),
        rt.get("provider") or "unknown", child_depth, goal,
    )
    child._subagent_id, child._parent_subagent_id = subagent_id, parent_subagent_id
    # Late-bind the child into the progress relay's shared slot (see child_agent_ref).
    # Weakref so the relay never keeps a finished child alive; some test doubles aren't
    # weakref-able, so fall back to a strong ref rather than losing the identity.
    try:
        child_agent_ref["agent"] = weakref.ref(child)
    except TypeError:
        child_agent_ref["agent"] = child
    _apply_child_compression_cap(child, delegation_cfg)
    # Ownership chain for action=list/steer/stop; weakref so a finished parent
    # can be collected while a detached child record lingers in the registry.
    try:
        child._delegate_parent_ref = weakref.ref(parent_agent)
    except TypeError:
        child._delegate_parent_ref = None  # non-weakref-able test doubles
    # Sidebar marker: subagent sessions stay out of session pickers even when a
    # parent delete orphans them (mirrors /branch's ``_branched_from``).
    if parent_sid and getattr(child, "_session_init_model_config", None) is not None:
        child._session_init_model_config["_delegate_from"] = parent_sid
    # Shared pool lets children rotate credentials on rate limits.
    child_pool = _resolve_child_credential_pool(
        rt["provider"], parent_agent, rt["base_url"], effective_requested_provider=rt.get("requested_provider"),
    )
    if child_pool is not None:
        child._credential_pool = child_pool

    _attach_child(parent_agent, child)  # interrupt propagation
    # spawn_requested now — the child may queue for seconds when the pool is
    # saturated — then the subagent_start lifecycle hook.
    _safe_progress(child_progress_cb, "subagent.spawn_requested", preview=goal)
    with _quiet("subagent_start hook invocation failed", exc_info=True):
        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
        _invoke_hook(
            "subagent_start", parent_session_id=parent_sid,
            parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "", parent_subagent_id=parent_subagent_id,
            child_session_id=getattr(child, "session_id", None), child_subagent_id=subagent_id,
            child_role=effective_role, child_goal=goal,
        )
    return child

def _run_single_child(
    task_index: int, goal: str, child=None, parent_agent=None, *, owner_session_id: Optional[str] = None,
    owner_transport: Any = None, owner_session_record: Any = None, **_kwargs,
) -> Dict[str, Any]:
    """Run a pre-built child agent (called from a worker thread) and return its result entry.

    Contract, derived from the child's structured completion fields:
      status      ∈ {completed, interrupted, failed} — a structured failure
                    (failed=True / non-empty error) or an invalid terminal state
                    is "failed" even when a summary exists.
      exit_reason ∈ {completed, max_iterations, interrupted, error} —
                    "max_iterations" only for genuine budget exhaustion
                    (completed=False with no failure fields), never for errors.
      truncated   == (exit_reason == "max_iterations").

    * ``"completed"``       — normal finish. See #97655.
    """
    child_progress_cb = getattr(child, "tool_progress_callback", None)
    child_pool, leased_cred_id = _lease_child_credential(child)
    # Heartbeat keeps the parent's _last_activity_ts moving so the gateway inactivity timeout doesn't fire while the
    # child works; once the child looks stale (see _HEARTBEAT_STALE_CYCLES_*) it also ends await_child's wait.
    heartbeat = _start_heartbeat(child, parent_agent, task_index)
    # TUI/RPC registry entry (kill/pause/status by subagent_id); None for test
    # doubles without a stable id. Unregistered in the finally block.
    _subagent_id = _register_child(
        child, parent_agent, goal, owner_session_id=owner_session_id, owner_transport=owner_transport,
        owner_session_record=owner_session_record,
    )
    run = _ChildRun(child, parent_agent, task_index, goal, _subagent_id, child_progress_cb, heartbeat=heartbeat)
    # Set when a timed-out Future still owns the child: closing it from this
    # thread before the worker settles races the conversation's finally path.
    _child_close_deferred = False
    try:
        heartbeat.start()
        _safe_progress(child_progress_cb, "subagent.start", preview=goal)
        run.seed_workspace()
        result, failure_entry, _child_close_deferred = run.await_child()
        if failure_entry is not None:
            return failure_entry

        schema = _validate_child_output_schema(child, result, task_index, run.child_task_id, run.relay_text)
        _merge_late_steer(result, _subagent_id, child)
        # Flush any remaining batched progress to gateway
        if child_progress_cb and hasattr(child_progress_cb, "_flush"):
            with _quiet("Progress callback flush failed: %s"):
                child_progress_cb._flush()

        duration = run.elapsed()
        entry = _build_result_entry(child, result, task_index, duration, schema)
        run.append_sibling_write_reminder(entry)
        run.account_background_processes(entry)
        run.emit_complete(result, entry, duration)
        return run.attach_worktree(entry)
    except Exception as exc:
        # Close steer acceptance before any completion callback (see _merge_late_steer).
        _late_pending_steer = run.close_steering()
        logging.exception(f"[subagent-{task_index}] failed")
        # Entry status "error" (contract), progress event status "failed" (UI vocabulary).
        return run.finish_failed(
            _fabricated_entry(task_index, "error", str(exc), child, run.elapsed()), _late_pending_steer,
            preview=str(exc), summary=str(exc), status="failed",
        )
    finally:
        run.cleanup(heartbeat=heartbeat, child_pool=child_pool, leased_cred_id=leased_cred_id, close_deferred=_child_close_deferred)


def _resolve_role_credentials(entry: dict, parent_agent, cache: dict) -> dict:
    """Resolve the credential bundle for a per-role ``model_by_role`` entry.

    A ``delegation.model_by_role`` entry may be a dict declaring its own ``provider``
    (plus optional ``base_url``/``api_key``/``api_mode``), mirroring the shape of
    ``delegation.by_provider``. Such a role must run on ITS provider, not on the
    batch-level delegation provider — sending a role's model slug to the batch
    provider's endpoint is a guaranteed 404.

    Deliberate reuse: this builds a synthetic delegation-config dict and hands it to
    :func:`_resolve_delegation_credentials`, so the real credential resolution
    (``resolve_runtime_provider``, API-key checks, pinned-ACP-command preflight) happens
    in exactly one place. The synthetic cfg carries no ``by_provider`` key, so that
    branch is skipped.

    Results are memoized on ``cache`` keyed by the credential-bearing fields, so N
    children on the same role resolve the provider once.

    Raises ValueError (never swallowed) when resolution fails — the caller must refuse
    the spawn rather than silently fall back.
    """
    synthetic_cfg: Dict[str, Any] = {"model": entry.get("model"), "provider": entry.get("provider")}
    for key in ("base_url", "api_key", "api_mode"):
        value = entry.get(key)
        if value:
            synthetic_cfg[key] = value
    cache_key = (
        synthetic_cfg.get("provider"), synthetic_cfg.get("model"), synthetic_cfg.get("base_url"),
        synthetic_cfg.get("api_key"), synthetic_cfg.get("api_mode"),
    )
    if cache_key in cache:
        return cache[cache_key]
    resolved = _resolve_delegation_credentials(synthetic_cfg, parent_agent)
    cache[cache_key] = resolved
    return resolved


def _load_role_maps() -> tuple[Dict[str, Any], Dict[str, Any], Any]:
    """``(role_model_map, role_entry_map, resolve_role_alias)`` for one delegate_task call.

    Three independent guards on purpose: a missing/failing entry-map API must never take
    the flattened model map (and therefore auto-route) down with it, and an older
    hermes_cli without the alias table degrades to "no aliases" rather than taking both
    role maps down.
    """
    try:
        from hermes_cli.ruflo_agents import get_role_model_map
        role_model_map = get_role_model_map()
    except Exception:
        role_model_map = {}
    try:
        from hermes_cli.ruflo_agents import get_role_entry_map
        role_entry_map = get_role_entry_map()
    except Exception:
        role_entry_map = {}
    try:
        from hermes_cli.ruflo_agents import resolve_role_alias
    except Exception:
        def resolve_role_alias(role: Optional[str]) -> Optional[str]:  # type: ignore[misc]
            return None
    return (role_model_map if isinstance(role_model_map, dict) else {},
            role_entry_map if isinstance(role_entry_map, dict) else {}, resolve_role_alias)


def _auto_route_batch(task_list, role_model_map, cfg, creds, parent_agent) -> Dict[int, Dict[str, Any]]:
    """Auto-route verdicts for the whole batch in ONE classifier call.

    Serves two distinct populations (see tools/delegation_router.py's module docstring):
    tasks with no agent_type (or the explicit opt-in ``agent_type="auto"``) are FULLY
    routed tier → role → model; tasks that DID state an agent_type ride along only so
    their tier can be compared — an ESCALATE-ONLY check that can move a task up the
    ladder, never down. A task with an explicit ``model=`` is in neither population and
    is never classified at all.

    Fail-open: on ANY failure this returns {} and every task falls through the existing
    precedence chain, i.e. exactly the behavior before this module existed.
    """
    try:
        from tools.delegation_router import route_task_models
        provider = (creds.get("provider") or "").strip() or getattr(parent_agent, "provider", None)
        return route_task_models(task_list, role_model_map, cfg, provider) or {}
    except Exception:
        logger.debug("delegate_task: auto-route dispatch failed", exc_info=True)
        return {}


def _resolve_task_routes(
    task_list, creds, *, cfg, parent_agent, top_role, roster_warnings,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Per-task routing decisions for a whole batch, resolved BEFORE any child is built.

    Returns ``(routes, None)`` — one dict per task carrying its final ``agent_type``,
    ``model``, credential bundle, runtime fallback chain and auto-route provenance — or
    ``([], error)`` when a role's provider pin (and its fallback, if any) cannot be
    resolved. Resolving every task up front is deliberate: a bad role pin on task 3 must
    refuse the WHOLE spawn, not leave children 1-2 already built and running.

    Precedence per task (load-bearing, see tools/delegation_router.py):
      explicit ``model=`` → explicit ``agent_type`` role-map → auto-route persona pick
      → auto-route tier→role→model → ``delegation.model``/``by_provider`` → parent's model
    """
    role_model_map, role_entry_map, resolve_role_alias = _load_role_maps()
    auto_routes = _auto_route_batch(task_list, role_model_map, cfg, creds, parent_agent)
    # The literal agent_type meaning "auto-route this task" — imported from the router
    # (single source of truth) with a literal fallback so a partially-loaded router
    # module can never break dispatch.
    try:
        from tools.delegation_router import AUTO_AGENT_TYPE
    except Exception:
        AUTO_AGENT_TYPE = "auto"

    role_creds_cache: Dict[tuple, dict] = {}
    routes: List[Dict[str, Any]] = []
    for i, t in enumerate(task_list):
        effective_role = _normalize_role(t.get("role") or top_role)
        # agent_type="auto" is NOT a persona: it is the explicit opt-in to auto-routing,
        # so it normalises to None here (identical routing to omitting the field — no
        # bogus model_by_role["auto"] lookup, no "auto" persona prompt) while still
        # counting as an explicit choice that does not trip the omission warning.
        stated_agent_type = (t.get("agent_type") or "").strip()
        agent_type_is_auto = stated_agent_type.lower() == AUTO_AGENT_TYPE
        agent_type_omitted = not stated_agent_type
        task_agent_type = None if (agent_type_omitted or agent_type_is_auto) else stated_agent_type
        task_model_explicit = (t.get("model") or "").strip() or None
        route = auto_routes.get(i)

        # Auto-route persona pick feeds the SAME task_agent_type variable an explicit
        # agent_type uses, so it fires both existing effects for free (persona-prompt
        # injection AND per-role model resolution). Explicit always wins.
        if task_agent_type is None and route:
            auto_agent_type = (route.get("agent_type") or "").strip() or None
            if auto_agent_type:
                task_agent_type = auto_agent_type

        # Escalate-only override: the task DID state an agent_type and the classifier
        # judged the work to need a strictly DEEPER tier. Replacing task_agent_type makes
        # the whole existing machinery (role→model, role→provider pin, role→persona
        # prompt, role→iteration budget) follow the escalation with no duplicated logic.
        # The router only ever emits an escalation UPWARD, so a stated choice can never be
        # silently demoted here.
        escalation = None
        if task_agent_type is not None and route and route.get("escalated") and (route.get("agent_type") or "").strip():
            escalation = {
                "from": task_agent_type, "to": (route.get("agent_type") or "").strip(),
                "tier": route.get("tier"), "from_rank": route.get("escalated_from_rank"),
                "to_rank": route.get("rank"), "reason": route.get("reason"),
            }
            task_agent_type = escalation["to"]
            roster_warnings.append(
                f"Task {i}: agent_type={escalation['from']!r} was ESCALATED to "
                f"{escalation['to']!r} (tier {escalation['tier']!r}, rank "
                f"{escalation['from_rank']}→{escalation['to_rank']}) by the auto-route classifier"
                + (f": {escalation['reason']}" if escalation.get("reason") else "")
                + ". Auto-route only ever escalates, never downgrades. Pass an explicit model= to "
                "bypass classification entirely, or set delegation.auto_route.escalate_only: false "
                "in config.yaml to disable this check."
            )
            logger.warning(
                "delegate_task: task %d agent_type=%r escalated to %r (tier=%r, rank %s->%s) by auto-route classifier",
                i, escalation["from"], escalation["to"], escalation["tier"],
                escalation["from_rank"], escalation["to_rank"],
            )

        # role= grants CAPABILITY (can this child spawn children) and is entirely
        # independent of agent_type=, which is what routes the child through
        # delegation.model_by_role to pick its MODEL. role='orchestrator' with neither
        # silently inherits the PARENT's model/provider — surface it.
        if effective_role == "orchestrator" and task_agent_type is None and task_model_explicit is None:
            roster_warnings.append(
                f"Task {i}: role='orchestrator' with no agent_type= (and no explicit model=) — this "
                f"child will silently INHERIT the parent's own model/provider instead of routing "
                f"through delegation.model_by_role. If you intended a specific configured role's "
                f"model (e.g. the 'pm' persona), pass agent_type=<role> alongside role='orchestrator'."
            )

        # Role aliases (hermes_cli.personas.ROLE_ALIASES, e.g. "sr-coder" -> "coder"): a
        # pure synonym carries no config of its own, so the config key its entry lives
        # under may differ from the dispatched agent_type. Resolved ONCE and used for BOTH
        # role-keyed lookups below (model and credential entry) so the two can never
        # disagree. An explicitly configured alias always wins — the fallback only fires
        # when the dispatched name has no entry of its own.
        role_cfg_key = task_agent_type
        if task_agent_type and not (task_agent_type in role_model_map or task_agent_type in role_entry_map):
            alias_target = resolve_role_alias(task_agent_type)
            if alias_target:
                role_cfg_key = alias_target
        role_map_model = role_model_map.get(role_cfg_key) if role_cfg_key else None

        # The classifier's model is only a FALLBACK for tasks it was allowed to route:
        # ones that stated no agent_type (or opted in with "auto"), plus escalations
        # (where task_agent_type was already replaced, so role_map_model is the escalated
        # role's own model). For a task whose stated agent_type SURVIVED, a route entry
        # must not contribute a model at all — otherwise a role with no model_by_role
        # entry would silently pick up the classifier's tier model, the exact downgrade
        # path escalate-only exists to prevent.
        stated_survived = stated_agent_type and not agent_type_is_auto and escalation is None
        auto_route_model = route.get("model") if (route and not stated_survived) else None

        task_creds = creds
        task_fallback_chain: Optional[List[Dict[str, Any]]] = None
        role_entry = role_entry_map.get(role_cfg_key) if role_cfg_key else None
        role_provider = str(role_entry.get("provider") or "").strip() if isinstance(role_entry, dict) else ""
        # Per-role fallback: a second full {model, provider, ...} bundle, consulted at
        # construction time (below) and at runtime (via override_fallback_chain). Only
        # meaningful alongside a provider pin — a fallback identity with no primary
        # provider has nothing to be a fallback FOR.
        role_fallback_entry = role_entry.get("fallback") if (role_provider and isinstance(role_entry, dict)) else None
        if not isinstance(role_fallback_entry, dict):
            role_fallback_entry = None

        if role_provider and isinstance(role_entry, dict):
            try:
                task_creds = _resolve_role_credentials(role_entry, parent_agent, role_creds_cache)
            except ValueError as primary_exc:
                via_alias = f" (dispatched as {task_agent_type!r})" if role_cfg_key != task_agent_type else ""
                if role_fallback_entry is None:
                    # Fail loud: falling back to the batch provider here is exactly the
                    # wrong-model-on-wrong-provider bug this pin exists to prevent. Name
                    # the CONFIG key (the alias target when the dispatched role is a
                    # synonym) so the user can find the entry to fix, and name the
                    # dispatched role too when they differ.
                    return [], (
                        f"delegation.model_by_role[{role_cfg_key!r}]{via_alias} pins provider "
                        f"{role_provider!r} but it could not be resolved: {primary_exc}"
                    )
                try:
                    task_creds = _resolve_role_credentials(role_fallback_entry, parent_agent, role_creds_cache)
                except ValueError as fallback_exc:
                    return [], (
                        f"delegation.model_by_role[{role_cfg_key!r}]{via_alias} pins provider "
                        f"{role_provider!r} but it could not be resolved: {primary_exc}. "
                        f"Its configured fallback also could not be resolved: {fallback_exc}"
                    )
                # Fallback resolved — dispatch proceeds on ITS bundle. The primary's
                # role-map model must not leak into the precedence chain now that the
                # provider underneath it changed; pin role_map_model to the fallback's own
                # model so the two can never mix. No further runtime fallback for this hop
                # (the one hop was already spent getting here).
                role_map_model = task_creds["model"]
                fb_provider, fb_model = str(task_creds.get("provider") or ""), str(task_creds.get("model") or "")
                fb_msg = (
                    f"🔄 Fallback engaged for role {role_cfg_key!r}: primary provider {role_provider!r} "
                    f"unresolvable ({primary_exc}) — using {fb_model} via {fb_provider} instead"
                )
                emit = getattr(parent_agent, "_emit_status", None)
                if callable(emit):
                    with _quiet("delegate_task: fallback notice emit failed", exc_info=True):
                        emit(fb_msg)
                logger.info(
                    "delegate_task: role %r primary provider %r unresolvable (%s) — engaged configured fallback %s/%s",
                    role_cfg_key, role_provider, primary_exc, fb_provider, fb_model,
                )
            else:
                # Primary resolved fine. Attach the role's own fallback as a ONE-ENTRY
                # runtime chain in the SAME raw shape the top-level fallback_providers
                # config uses — no eager resolution. AIAgent's existing
                # try_activate_fallback()/classify_api_error() machinery resolves it lazily
                # exactly like any other chain entry, only if the ACTUAL model call fails
                # with a retryable-class error.
                if role_fallback_entry is not None:
                    task_fallback_chain = [dict(role_fallback_entry)]

        # INVARIANT: task_model_explicit is FIRST and must stay first. A caller-stated
        # model= is intent and always wins — it bypasses auto-route AND the escalate-only
        # tier check entirely (the router never even classifies a task carrying model=).
        # The config fallback comes from the bundle actually used for THIS child, so a
        # role-provider child can never inherit the other provider's default model.
        effective_task_model = task_model_explicit or role_map_model or auto_route_model or task_creds["model"]

        # Silent-omission visibility: a task that stated NEITHER model= nor agent_type=
        # got its model chosen by something the caller never named. agent_type="auto" is
        # an explicit opt-in and is deliberately exempt.
        if agent_type_omitted and task_model_explicit is None and not agent_type_is_auto:
            if route:
                decision = (
                    f"auto-route classifier → tier {route.get('tier')!r} → role "
                    f"{route.get('role')!r} → model {effective_task_model!r}"
                )
                reason = str(route.get("reason") or "").strip()
                if reason:
                    decision += f" ({reason})"
            elif effective_task_model:
                decision = f"delegation config default → model {effective_task_model!r}"
            else:
                decision = "inherited the PARENT's own model/provider"
            roster_warnings.append(
                f"Task {i}: no agent_type= and no model= were given, so the model was chosen for "
                f"you: {decision}. To route deliberately, pass agent_type=<role> (resolved through "
                f"delegation.model_by_role) or model=<model>; pass agent_type='auto' to opt into "
                f"automatic routing explicitly and silence this notice."
            )
            logger.info(
                "delegate_task: task %d dispatched with no agent_type= and no model=; routing decision: %s",
                i, decision,
            )

        route_info = None
        if route:
            route_info = {
                "tier": route.get("tier"), "role": route.get("role"), "model": route.get("model"),
                "reason": route.get("reason"), "agent_type": route.get("agent_type"),
            }
            if escalation is not None:
                route_info.update({
                    "escalated": True, "escalated_from": escalation["from"],
                    "escalated_from_rank": escalation["from_rank"], "rank": escalation["to_rank"],
                })

        routes.append({
            "role": effective_role, "agent_type": task_agent_type, "model": effective_task_model,
            "creds": task_creds, "fallback_chain": task_fallback_chain, "route_info": route_info,
        })
    return routes, None


def _build_children(
    task_list: List[Dict[str, Any]], task_schemas: List[Optional[Dict[str, Any]]], creds: Dict[str, Any], *,
    top_role: str, max_iterations: int, parent_agent, routing_cfg: Dict[str, Any],
    live_deleg_id: Optional[str], live_writers: list, task_images: Optional[List[Optional[List[str]]]] = None,
    cfg: Optional[Dict[str, Any]] = None, roster_warnings: Optional[List[str]] = None,
) -> tuple[List[tuple], Optional[str]]:
    """Build every child on the main thread (construction is not thread-safe);
    ``(children, None)`` or ``([], error)`` on an explicit-pin preflight failure."""
    from tools.delegation_live_log import wrap_progress_callback
    from tools.delegation_output_schema import append_output_contract
    # Every per-task credential bundle resolves BEFORE any child is constructed, so a bad
    # role pin refuses the whole spawn instead of leaving children 1-2 already built.
    routes, err = _resolve_task_routes(
        task_list, creds, cfg=cfg if cfg is not None else routing_cfg, parent_agent=parent_agent,
        top_role=top_role, roster_warnings=roster_warnings if roster_warnings is not None else [],
    )
    if err:
        return [], err
    children = []
    for i, t in enumerate(task_list):
        _route = routes[i]
        _creds = _route["creds"]
        _task_schema = task_schemas[i] if i < len(task_schemas) else None
        _child_context = t.get("context")
        if _task_schema is not None:
            _child_context = append_output_contract(_child_context, _task_schema)
        try:
            child = _build_child_preserving_parent_tools(
                task_index=i, goal=t["goal"], context=_child_context,
                toolsets=None,  # always inherit the parent's toolsets
                model=_route["model"], max_iterations=max_iterations, task_count=len(task_list),
                parent_agent=parent_agent, role=_route["role"], agent_type=_route["agent_type"],
                override_provider=_creds["provider"], override_base_url=_creds["base_url"],
                override_api_key=_creds["api_key"], override_api_mode=_creds["api_mode"],
                override_request_overrides=_creds.get("request_overrides"),
                override_max_tokens=_creds.get("max_output_tokens"),
                override_acp_command=_creds.get("command"), override_acp_args=_creds.get("args"),
                override_fallback_chain=_route["fallback_chain"], routing_cfg=routing_cfg,
            )
        except ValueError as exc:
            return [], str(exc)
        # Auto-route provenance so the result metadata can surface the decision — silent
        # misrouting must be impossible to hide.
        if _route["route_info"] is not None:
            with _quiet("Could not attach auto-route info to child %d", i):
                child._auto_route_info = _route["route_info"]
        if _task_schema is not None:
            with _quiet("Could not attach output schema to child %d", i):
                child._delegate_output_schema = _task_schema
        # Validated per-task images; absent on image-less tasks, which keep the text-only goal turn.
        _t_images = task_images[i] if task_images and i < len(task_images) else None
        if _t_images:
            with _quiet("Could not attach images to child %d", i):
                child._delegate_images = _t_images
        # Tee progress events into the live transcript (wrapper keeps the
        # _flush contract and swallows writer failures).
        _writer = live_writers[i] if i < len(live_writers) else None
        if _writer is not None:
            child.tool_progress_callback = wrap_progress_callback(getattr(child, "tool_progress_callback", None), _writer)
            child._live_transcript_path = str(_writer.path)
        if live_deleg_id:
            setattr(child, "_delegation_id", live_deleg_id)
            _ident_ref = getattr(child, "_progress_identity_ref", None)
            if isinstance(_ident_ref, dict):
                _ident_ref["delegation_id"] = live_deleg_id
        children.append((i, t, child))
    return children, None


def _normalize_roster_model(model: Any) -> Optional[str]:
    """Normalize a model string for roster matching.

    Strips surrounding whitespace and any leading provider-prefix segment
    (``"something/"``), then lowercases, so ``"anthropic/claude-opus-5"``
    matches a roster entry ``"claude-opus-5"``. Returns None for
    empty/whitespace values (treated as absent).
    """
    if not isinstance(model, str):
        return None
    s = model.strip()
    if not s:
        return None
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s.lower()


def _build_model_roster(
    cfg: Dict[str, Any], creds: Dict[str, Any], parent_agent
) -> tuple[Set[str], bool]:
    """Build the 'known current models' roster for one delegate_task call.

    Returns ``(known_models, has_config_roster)`` where ``known_models`` is
    the normalized set of models the current config knows about, plus the
    resolved batch default and the parent's live model; and
    ``has_config_roster`` is True only when at least one CONFIG-DERIVED
    model (``delegation.by_provider`` / top-level ``delegation.model`` /
    ``delegation.model_by_role``) was found.

    ``has_config_roster`` drives fail-open: when False there is no config
    to be stale AGAINST, so the depth-0 roster-validity check is skipped
    and delegation behaves exactly as before. ``parent_agent.model`` and
    ``creds["model"]`` are permissive matchers (a task model matching the
    parent's own running model is clearly not stale) but never the sole
    basis for activating validation — the parent is always running on
    something, so counting it would make the roster never empty and defeat
    fail-open.

    Every lookup is individually exception-guarded so a broken config can
    never take delegation down.
    """
    known: Set[str] = set()
    has_config_roster = False

    # (a) delegation.by_provider.<p>.model for every provider block.
    try:
        by_provider = cfg.get("by_provider") or {}
        if isinstance(by_provider, dict):
            for _block in by_provider.values():
                if isinstance(_block, dict):
                    _m = _normalize_roster_model(_block.get("model"))
                    if _m:
                        known.add(_m)
                        has_config_roster = True
    except Exception:
        logger.debug("delegate_task: by_provider roster scan failed", exc_info=True)

    # (b) top-level delegation.model (legacy).
    try:
        _m = _normalize_roster_model(cfg.get("model"))
        if _m:
            known.add(_m)
            has_config_roster = True
    except Exception:
        logger.debug("delegate_task: top-level model roster scan failed", exc_info=True)

    # (c) delegation.model_by_role entries (get_role_entry_map) — each
    # entry's model plus its nested fallback dict's model.
    try:
        from hermes_cli.ruflo_agents import get_role_entry_map

        _entry_map = get_role_entry_map()
        if isinstance(_entry_map, dict):
            for _entry in _entry_map.values():
                if not isinstance(_entry, dict):
                    continue
                _m = _normalize_roster_model(_entry.get("model"))
                if _m:
                    known.add(_m)
                    has_config_roster = True
                _fb = _entry.get("fallback")
                if isinstance(_fb, dict):
                    _fm = _normalize_roster_model(_fb.get("model"))
                    if _fm:
                        known.add(_fm)
                        has_config_roster = True
    except Exception:
        logger.debug("delegate_task: model_by_role roster scan failed", exc_info=True)

    # (d) creds["model"] (the resolved batch default) — permissive matcher.
    try:
        _m = _normalize_roster_model(creds.get("model"))
        if _m:
            known.add(_m)
    except Exception:
        logger.debug("delegate_task: creds model roster scan failed", exc_info=True)

    # (e) parent_agent.model (the live running model) — permissive matcher.
    try:
        _m = _normalize_roster_model(getattr(parent_agent, "model", None))
        if _m:
            known.add(_m)
    except Exception:
        logger.debug("delegate_task: parent model roster scan failed", exc_info=True)

    return known, has_config_roster


def _guard_task_models(
    task_list: List[Dict[str, Any]], *, depth: int, known_models: Set[str],
    has_config_roster: bool, roster_warnings: List[str],
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Validate caller-supplied ``model=`` strings against the live config roster at EVERY
    delegation depth; ``(guarded_task_list, None)`` or ``([], error)``.

    Semantics per task carrying a non-empty ``model=``:
      * bare ``model=`` (no agent_type):
          depth >= 1 → REJECT (role governance: a nested child must route through
          ``agent_type=``); depth 0 → REJECT when STALE, allow when roster-valid.
      * ``model=`` alongside ``agent_type=``:
          depth >= 1 → DROP the model (role resolution wins), warn when STALE;
          depth 0 → DROP + warn when STALE, keep it (explicit pin wins) when roster-valid.

    FAIL-OPEN: with no config roster there is nothing to be stale AGAINST, so the depth-0
    validity check is skipped entirely. The nested role-governance rules are NOT gated on
    the roster — they fire unconditionally. Builds a FRESH list so a model-drop never
    rewrites the caller's own task dicts.
    """
    guarded: List[Dict[str, Any]] = []
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            guarded.append(task)
            continue
        model_str = str(task.get("model") or "").strip()
        if not model_str:
            guarded.append(task)
            continue
        has_agent_type = bool((task.get("agent_type") or "").strip())
        stale = has_config_roster and (_normalize_roster_model(model_str) not in known_models)
        if not has_agent_type:
            if depth >= 1:
                return [], (
                    f"Task {i}: nested delegation from a subagent requires agent_type= (role "
                    f"resolution); a bare model= is not allowed. Set agent_type= to route this child "
                    f"through delegation.model_by_role, or drop model= to let the role map pick the model."
                )
            if stale:
                return [], (
                    f"Task {i}: model={model_str!r} is not in the current model roster. Use a model "
                    f"from delegation.by_provider or delegation.model_by_role in config.yaml, or pass "
                    f"agent_type= to route this child through role resolution."
                )
            guarded.append(task)
            continue
        # model= alongside agent_type=.
        if depth >= 1 or stale:
            if stale:
                roster_warnings.append(
                    f"Task {i}: model={model_str!r} is not in the current model roster and was "
                    f"IGNORED; role resolution (agent_type={task.get('agent_type')!r}) was used "
                    f"instead. Use a model from delegation.by_provider or delegation.model_by_role "
                    f"in config.yaml, or drop model= to let the role map pick the model."
                )
                logger.warning(
                    "delegate_task: task %d supplied model=%r which is not in the current model "
                    "roster; ignoring it in favor of role resolution (agent_type=%r)",
                    i, model_str, task.get("agent_type"),
                )
            else:
                logger.warning(
                    "delegate_task: nested delegation task %d supplied both model=%r and "
                    "agent_type=%r; ignoring model in favor of role resolution",
                    i, task.get("model"), task.get("agent_type"),
                )
            task = {**task, "model": None}
        # else: depth 0 + roster-valid — keep the model (explicit pin wins).
        guarded.append(task)
    return guarded, None


def _oneshot_spawn_budget(parent_agent: Any, requested: int) -> Optional[str]:
    """Charge *requested* children against the finite one-shot session's total (delegation.oneshot_max_children);
    the error text tells the model to do the work inline. Interactive and gateway sessions are never charged."""
    from agent.oneshot_footprint import is_single_query_session
    if not is_single_query_session():
        return None
    cap = _get_oneshot_max_children()
    if cap <= 0:
        return None
    spent = getattr(parent_agent, "_oneshot_children_spawned", 0)
    if spent + requested > cap:
        return (
            f"Delegation budget for this one-shot run is exhausted ({spent}/{cap} subagents used; "
            f"delegation.oneshot_max_children). Do the remaining work yourself in this session — reviewing "
            f"your own diff and running the tests inline is expected here, not a delegated review."
        )
    parent_agent._oneshot_children_spawned = spent + requested
    return None


def delegate_task(
    goal: Optional[str] = None, context: Optional[str] = None, tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None, role: Optional[str] = None, background: Optional[bool] = None,
    model: Optional[str] = None, agent_type: Optional[str] = None,
    output_schema: Optional[Dict[str, Any]] = None, images: Optional[List[str]] = None, action: Optional[str] = None,
    subagent_id: Optional[str] = None, message: Optional[str] = None, parent_agent=None,
    credentials_cfg: Optional[Dict[str, Any]] = None, cancel: Optional[str] = None,
) -> str:
    """Spawn child agents (single ``goal`` or ``tasks=[...]`` batch) or control running ones. ``action``
    list/steer/stop run synchronously and bypass the pause gate, depth limit and async dispatch. ``role`` is legacy
    (per-task beats top-level; capability is depth-derived). Returns JSON with one results entry per task, or a
    dispatch handle when running in the background.

    Fourth mode -- cancel: pass ``cancel=<delegation_id>`` (the id from a prior BACKGROUND dispatch's
    handle / the gateway's ``⛓`` badge / ``/agents`` listing) to signal that ONE in-flight background
    delegation to stop, instead of spawning anything or touching the live synchronous-tree overlay
    ``action='stop'`` controls. Mutually exclusive with goal/tasks/action; when ``cancel`` is set,
    everything else is ignored. This is the model-facing mirror of the CLI's ``/stop <id>`` and the
    gateway slash command of the same name -- both already call the same
    ``tools.async_delegation.interrupt_by_id`` this uses.
    """
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    if cancel:
        try:
            from tools.async_delegation import interrupt_by_id
            # Scope to the calling agent's own session -- delegate_task's cancel path must only be
            # able to pull back a delegation THIS session actually dispatched, matching the ownership
            # check on the gateway's /stop <id> for the same reason (_records is process-global; a
            # gateway process runs many sessions concurrently).
            result = interrupt_by_id(
                str(cancel).strip(),
                reason="model_cancel",
                parent_session_id=getattr(parent_agent, "session_id", "") or "",
            )
        except Exception as exc:
            return tool_error(f"Cancel failed: {exc}")
        if not result.get("found"):
            return json.dumps({
                "status": "not_found",
                "delegation_id": cancel,
                "message": (
                    f"No running delegation with id '{cancel}'. It may have "
                    f"already completed (its result already re-entered the "
                    f"conversation), never existed, or the id is a typo."
                ),
            })
        if result.get("already_done"):
            return json.dumps({
                "status": "already_done",
                "delegation_id": cancel,
                "message": f"'{cancel}' already finished before the cancel landed.",
            })
        if result.get("interrupted"):
            return json.dumps({
                "status": "cancelled",
                "delegation_id": cancel,
                "message": (
                    f"Cancel signal sent to '{cancel}'. It will stop at its "
                    f"next iteration boundary and still emit a completion "
                    f"event (status='interrupted') -- expect that message to "
                    f"still arrive, just without a completed result."
                ),
            })
        return tool_error(f"Found '{cancel}' but could not signal it to stop.")

    normalized_action = (action or "").strip().lower()
    if normalized_action in _CONTROL_ACTIONS:
        return _handle_control_action(normalized_action, subagent_id, message, parent_agent)
    if normalized_action and normalized_action != "spawn":
        return tool_error(f"Unknown action '{action}'. Use spawn (default), list, steer, or stop.")

    # Operator kill switch (TUI / delegation.pause RPC): blocks NEW spawns only.
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    top_role = _normalize_role(role)
    # background applies to single tasks AND batches: a batch is ONE async unit
    # that joins on every child and re-enters as a single consolidated message.
    background = is_truthy_value(background, default=False) if background is not None else False

    depth = getattr(parent_agent, "_delegate_depth", 0)
    max_spawn = _get_max_spawn_depth()
    if depth >= max_spawn:
        return tool_error(
            f"Delegation depth limit reached (depth={depth}, max_spawn_depth={max_spawn}). Raise "
            f"delegation.max_spawn_depth in config.yaml if deeper nesting is required (no hard ceiling, but each level "
            f"multiplies API cost)."
        )

    cfg = _load_config()
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    # Caller-supplied max_iterations is ignored: the config value is authoritative
    # so budgets stay predictable (kwarg kept for internal callers/tests).
    if max_iterations is not None and max_iterations != default_max_iter:
        logger.debug(
            "delegate_task: ignoring caller-supplied max_iterations=%s; using delegation.max_iterations=%s from config",
            max_iterations, default_max_iter,
        )
    # credentials_cfg (internal callers only, e.g. /review → auxiliary.review) is
    # a per-call routing owner shaped like the delegation config section. Keep
    # the route and its fallback policy together through child construction.
    routing_cfg = credentials_cfg if credentials_cfg is not None else cfg
    try:
        creds = _resolve_delegation_credentials(routing_cfg, parent_agent)
    except ValueError as exc:
        # Explicit-pin preflight failures (e.g. pinned delegation.command missing from PATH) refuse the
        # spawn loudly (#80450).
        return tool_error(str(exc))
    max_children = _get_max_concurrent_children()
    task_list, err = _normalize_task_list(goal, context, tasks, output_schema, top_role, max_children)
    if not err:
        task_schemas, err = _coerce_task_schemas(task_list, output_schema)
    if not err:
        task_images, err = _coerce_task_images(task_list, images)
    if err:
        return tool_error(err)
    task_list = task_list or []  # narrowed: _normalize_task_list only returns None alongside err

    # Top-level model/agent_type are batch-wide DEFAULTS a per-task value overrides. The
    # single-goal branch folds them into its synthetic task; the batch branch takes caller
    # dicts verbatim, so seed them here (setdefault, never force) or a caller who set the
    # model once at the top level silently gets the config default on every child.
    if (model or agent_type) and task_list:
        task_list = [
            {**t, **({"model": t["model"] if t.get("model") else model} if model else {}),
             **({"agent_type": t["agent_type"] if t.get("agent_type") else agent_type} if agent_type else {})}
            if isinstance(t, dict) else t
            for t in task_list
        ]

    # Model-roster guardrail: validate caller-supplied model= strings against the live
    # config roster at EVERY depth, closing the depth-0 gap where a stale model string
    # (e.g. a deprecated slug typed from assistant memory) was silently accepted and a
    # real subagent ran on it. Warnings ride the same channel as the routing decisions
    # below, so they land in BOTH the immediate response and the completion event.
    _roster_warnings: List[str] = []
    _known_models, _has_config_roster = _build_model_roster(cfg, creds, parent_agent)
    if not _has_config_roster:
        logger.debug("delegate_task: no config model roster found; depth-0 model roster validation skipped (fail-open)")
    task_list, err = _guard_task_models(
        task_list, depth=depth, known_models=_known_models,
        has_config_roster=_has_config_roster, roster_warnings=_roster_warnings,
    )
    # One-shot budget (upstream): charge the requested children against the finite
    # single-query session's total; a no-op for interactive/gateway sessions.
    if not err:
        err = _oneshot_spawn_budget(parent_agent, len(task_list))
    if err:
        return tool_error(err)

    overall_start = time.monotonic()
    # Live transcripts: cache/delegation/live/<id>/task-<n>.log per task, a side channel with zero effect on message
    # content or prompt caching. Best-effort: on failure live_paths is empty and delegation proceeds.
    from tools.delegation_live_log import create_live_transcripts
    live_deleg_id, live_writers, live_paths = create_live_transcripts(
        task_list, context, model=creds.get("model"), provider=creds.get("provider")
    )
    _announce_batch(parent_agent, len(task_list), live_deleg_id)
    origin = _capture_origin()

    children, err = _build_children(
        task_list, task_schemas, creds, top_role=top_role, max_iterations=default_max_iter, parent_agent=parent_agent,
        routing_cfg=routing_cfg, live_deleg_id=live_deleg_id, live_writers=live_writers, task_images=task_images,
        cfg=cfg, roster_warnings=_roster_warnings,
    )
    if err:
        return tool_error(err)
    batch = _Batch(
        task_list, children, parent_agent, creds, context, top_role, max_children,
        live_deleg_id, live_writers, live_paths, *origin, overall_start,
        roster_warnings=list(_roster_warnings),
    )
    return _run_batch(batch, background)


# ── OpenAI function-calling schema ──────────────────────────────────────────

def _build_top_level_description(*, independent_completions=None) -> str:
    """delegate_task description: ONLY guidance stated nowhere else in the schema
    (limits live in the 'tasks' parameter description, rebuilt per get_definitions())."""
    try:
        orchestration_available = _get_max_spawn_depth() >= 2 and _get_orchestrator_enabled()
    except Exception:
        orchestration_available = False
    # Mention recursion only where it's actually available. send_message is deliberately not named (gateway-internal
    # vocabulary); model_tools session-filters the list to tools the session has.
    if orchestration_available:
        restrictions_rule = (
            "- Children cannot call clarify, memory, or cronjob.\n"
            f"- Children can themselves delegate while depth remains (max_spawn_depth={_get_max_spawn_depth()}); the "
            "runtime derives this from depth automatically.\n"
        )
    else:
        restrictions_rule = "- Children cannot call delegate_task, clarify, memory, or cronjob.\n"
    from tools.delegate_tool_config import _get_independent_completions

    if independent_completions is None:
        independent_completions = _get_independent_completions()
    delivery = (
        "each ungrouped task / `group` returns on its own"
        if independent_completions else "one message per call"
    )
    return _DESCRIPTION_HEAD.format(delivery=delivery) + restrictions_rule + _DESCRIPTION_TAIL

_DESCRIPTION_HEAD = (
    "Spawn subagents in isolated contexts; each gets its own conversation, terminal session, and toolset, and only its "
    "final summary returns to you. Pass every task in `tasks` — one entry spawns one subagent, several run in parallel "
    "(limit in the tasks description).\n\n"
    "Sessions without a later-result consumer (including one-shot CLI and cron) join parallel children "
    "and return results in this tool call. "
    "Otherwise runs in the background: dispatch returns live transcript paths and results re-enter "
    "as a new message when subagents finish ({delivery}). Background results are delivered only "
    "BETWEEN your turns: finish whatever does not depend on them, then give a one-line status and END YOUR TURN. Never "
    "wait or poll on transcripts, artifact files, or CI for a child. "
    "While children run, `action` (list/steer/stop) controls them live.\n\n"
    "USE FOR: reasoning-heavy subtasks, work that would flood your context, or independent parallel workstreams.\n"
    "DO NOT USE FOR (use these instead):\n"
    "- Mechanical multi-step work with no reasoning needed -> execute_code\n"
    "- A single tool call -> call the tool directly\n"
    "- Tasks needing user interaction -> subagents cannot ask questions\n"
    "- Durable work that must survive this session -> cronjob or terminal(background=True, notify=True); /stop, /new, "
    "or process exit halts running subagents (whole tree); each returns an 'interrupted' completion with partial output.\n\n"
    "RULES:\n"
    "- Children know nothing of this conversation: pass everything needed via 'context', including any required "
    "output language, tone, or style (e.g. \"respond in Chinese\").\n"
    "- Child summaries are SELF-REPORTS, not verified facts: a child claiming \"uploaded successfully\" or "
    "\"file written\" may be wrong. For external side effects (uploads, remote writes, publishing), require a "
    "verifiable handle (URL, ID, absolute path) and verify it yourself before telling the user the operation "
    "succeeded.\n"
    "- Children cannot close tracked work: a child asked to close it returns findings instead; "
    "the parent applies the transition.\n"
)
_DESCRIPTION_TAIL = (
    "- Children inherit the parent model unless pinned via delegation.provider / delegation.model in config.yaml."
)

def _build_tasks_param_description() -> str:
    """Compose the 'tasks' parameter description with current concurrency limit."""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"The task(s), up to {max_children} in parallel for this user (set "
        "via delegation.max_concurrent_children). Each entry spawns one "
        "subagent with isolated context and terminal session; a single task "
        "is a one-entry array. Required when spawning."
    )

def _build_dynamic_schema_overrides() -> dict:
    """Per-call schema overrides (ToolEntry.dynamic_schema_overrides): every
    get_definitions() pass rewrites the descriptions to the user's actual limits."""
    from tools.delegate_tool_config import _get_independent_completions

    independent_completions = _get_independent_completions()
    overrides_params = {**DELEGATE_TASK_SCHEMA["parameters"]}
    # Copy properties so the static schema dict is never mutated.
    overrides_params["properties"] = {k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()}
    overrides_params["properties"]["tasks"]["description"] = _build_tasks_param_description()

    if not independent_completions:
        tasks = overrides_params["properties"]["tasks"]
        tasks["items"] = {**tasks["items"], "properties": {
            k: v for k, v in tasks["items"]["properties"].items() if k != "group"
        }}

    return {
        "description": _build_top_level_description(independent_completions=independent_completions),
        "parameters": overrides_params,
    }

def _p(type_: str, description: str, **extra) -> dict:
    return {"type": type_, **extra, "description": description}

DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # description / tasks.description are placeholders: the real text is built per get_definitions() call by
    # _build_dynamic_schema_overrides() so the model sees the user's actual max_concurrent_children / max_spawn_depth.
    # Lazy (not at import) so cli.CLI_CONFIG isn't forced to load before the test conftest redirects HERMES_HOME.
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # The handler also accepts the legacy single-goal shape (top-level `goal`/`context`/`output_schema`),
            # wrapped into a one-entry batch at dispatch, and a per-task `role` (legacy, ignored: capability is
            # depth-derived). Both unadvertised on purpose (old transcripts only); do not re-add. No maxItems — the
            # runtime limit (delegation.max_concurrent_children) is enforced with a clear error in delegate_task().
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": _p(
                            "string",
                            "What this subagent should accomplish. Be specific and self-contained — it knows "
                            "nothing about your conversation history.",
                        ),
                        "context": _p(
                            "string",
                            "Background THIS child needs: file paths, error messages, constraints. Each child "
                            "sees only its own context — repeat shared background in every task that needs it.",
                        ),
                        "output_schema": _p(
                            "object",
                            "Optional JSON Schema this child's final answer must validate against (told to the "
                            "child up front; parent validates with one bounded correction retry; result gains "
                            "schema_valid, plus schema_errors on failure — the child's raw text is still returned "
                            "as summary, never discarded). Keep it forgiving — require only fields you will read.",
                        ),
                        "images": _p(
                            "array",
                            "Optional images this child must SEE (max 8): local file paths or http(s) URLs — e.g. a "
                            "screenshot the user sent, a design mock, a chart. Vision-capable children receive the "
                            "pixels on their first turn; non-vision children get path hints for vision_analyze. Text "
                            "files do NOT belong here — put paths in 'context' instead.",
                            items={"type": "string"},
                        ),
                        "group": _p(
                            "string",
                            "Optional result-delivery bucket within this call (only when delegation.independent_completions "
                            "is enabled; otherwise the whole call returns as one message). Tasks sharing a group return "
                            "together in ONE message; ungrouped tasks return individually as each finishes. This does not "
                            "order execution; if B needs A's output, dispatch B after A returns.",
                        ),
                    },
                    "required": ["goal"],
                },
                "description": "(rebuilt at get_definitions() time)",
            },
            # `background` (bool) is also accepted — DEPRECATED, ignored: top-level
            # delegations always run in the background. Unadvertised; do not re-add.
            "action": _p(
                "string",
                "Default 'spawn'. Live control of running children: "
                "'list' = ids/goals/status/transcripts; 'steer' = queue "
                "course-correction text into one child (subagent_id + "
                "message) without stopping it; 'stop' = end one child "
                "early (subagent_id; partial result still returns). "
                "Control actions return immediately; goal/tasks are ignored unless spawning.",
                enum=["spawn", "list", "steer", "stop"],
            ),
            "subagent_id": _p("string", "Target for action='steer'/'stop' (ids from the spawn response or action='list')."),
            "message": _p(
                "string",
                "For action='steer': the course correction, appended to "
                "the child's next tool result mid-run. Be directive and specific.",
            ),
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

def _model_background_value(args: dict, parent_agent=None) -> bool:
    """Background flag for the MODEL-facing dispatch path (registry fallback). Top-level delegations always run in the
    background — the model does not choose — for single tasks and fan-out batches alike (one async unit, one
    consolidated result); an orchestrator subagent (depth > 0) is the exception since it needs its workers' results
    within its own turn. The live path is ``run_agent._dispatch_delegate_task``; this mirrors it for the rare case
    the intercept is bypassed. Direct Python callers keep the synchronous default."""
    return not getattr(parent_agent, "_delegate_depth", 0) > 0

_MODEL_HIDDEN_TASK_FIELDS = {"acp_command", "acp_args"}

def _strip_model_hidden_task_fields(tasks: Any) -> Any:
    """Drop trusted-config-only task fields from model-supplied tasks (same list object back when nothing changed)."""
    if not isinstance(tasks, list) or not any(isinstance(t, dict) and _MODEL_HIDDEN_TASK_FIELDS & t.keys() for t in tasks):
        return tasks
    return [{k: v for k, v in t.items() if k not in _MODEL_HIDDEN_TASK_FIELDS} if isinstance(t, dict) else t for t in tasks]


def _is_blocking_spawn_call(args: dict, parent_agent: Any = None) -> bool:
    """True when this delegate_task call BLOCKS on child agents it supervises.

    Consumed by the tool registry's ``owns_own_deadline`` hook so the generic
    per-call executor deadline is not applied to a call whose runtime is, by
    design, the runtime of the whole child agent tree beneath it.

    Only the SPAWN form qualifies, and only when it actually blocks:

    * ``action`` in {list, steer, stop} and the ``cancel=`` form are cheap
      in-turn control calls that return immediately — they keep the deadline.
    * A top-level (depth 0) spawn is forced ``background=True``: it dispatches
      and returns a handle in milliseconds, and the persistent CLI/gateway
      process drains the completion later. It keeps the deadline too.
    * A NESTED spawn from an orchestrator subagent (depth > 0) is forced
      synchronous — it must block until its own workers finish, because a
      bounded subagent turn is not a persistent listener that could ever
      consume an async completion. That is the call this exemption exists for.

    Depth is read from the live parent agent rather than the args, so the
    exemption tracks the same signal the sync/async decision itself uses
    (``run_agent._dispatch_delegate_task`` / ``_model_background_value``).

    The incident this closes (2026-08-23): a depth-1 orchestrator's nested
    batch hit the 420s generic deadline at 07:00 into a legitimate multi-child
    run. The executor abandoned the worker but could NOT cancel it, so the
    aggregation kept running headless, its children finished ~70s later, and
    the consolidated result was returned into a Future nobody would ever read.
    The orchestrator meanwhile reported "completed" to its own parent. Work
    stalled silently for ~7 hours.
    """
    if not isinstance(args, dict):
        return False
    action = str(args.get("action") or "").strip().lower()
    if action in {"list", "steer", "stop"}:
        return False
    if str(args.get("cancel") or "").strip():
        return False
    if not (args.get("goal") or args.get("tasks")):
        return False
    # Only the synchronous (nested, depth > 0) spawn blocks. A top-level spawn
    # returns a handle immediately and must stay bounded.
    return not _model_background_value(args, parent_agent)


def _delegate_owns_own_deadline(args: dict, parent_agent: Any = None) -> bool:
    """Registry hook: blocking spawns own their bound, everything else doesn't."""
    return _is_blocking_spawn_call(args, parent_agent)

registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"), context=args.get("context"), tasks=_strip_model_hidden_task_fields(args.get("tasks")),
        max_iterations=args.get("max_iterations"), role=args.get("role"),
        model=args.get("model"), agent_type=args.get("agent_type"),
        background=_model_background_value(args, kw.get("parent_agent")), output_schema=args.get("output_schema"),
        images=args.get("images"), action=args.get("action"), subagent_id=args.get("subagent_id"), message=args.get("message"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    owns_own_deadline=_delegate_owns_own_deadline,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from concurrent.futures import TimeoutError as FuturesTimeoutError  # noqa: F401,E402
import contextvars  # noqa: F401,E402
import enum  # noqa: F401,E402
import json  # noqa: F401,E402
import os  # noqa: F401,E402
import re  # noqa: F401,E402
import threading  # noqa: F401,E402
from urllib.parse import urlsplit  # noqa: F401,E402
from urllib.parse import urlunsplit  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_CHILD_TIMEOUT': ('tools.delegate_tool_config', 'DEFAULT_CHILD_TIMEOUT'),
    'DEFAULT_MAX_SUMMARY_CHARS': ('tools.delegate_tool_results', 'DEFAULT_MAX_SUMMARY_CHARS'),
    'DEFAULT_TOOLSETS': ('tools.delegate_tool_toolsets', 'DEFAULT_TOOLSETS'),
    'MAX_DEPTH': ('tools.delegate_tool_config', 'MAX_DEPTH'),
    'TOOLSETS': ('toolsets', 'TOOLSETS'),
    'base_url_hostname': ('utils', 'base_url_hostname'),
    'file_state': ('tools', 'file_state'),
    'request_hard_interrupt': ('agent.interrupt_compat', 'request_hard_interrupt'),
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
