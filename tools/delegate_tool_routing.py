#!/usr/bin/env python3
"""Fork role routing for ``delegate_task`` — extracted out of ``tools/delegate_tool.py``.

Every symbol here is fork-only (upstream's ``delegate_tool`` has none of it), so keeping it inline
meant carrying ~630 lines of conflict surface through every upstream sync for no benefit: the
upstream file only ever needs the call sites. The module is imported back into
``tools/delegate_tool`` under the original names, so nothing downstream changes.

Behaviour is unchanged by the move, deliberately: this is a pure relocation, verified by the
delegate suite plus a role-parity dump (every configured role -> exact model/provider). Restructuring
the routing — e.g. collapsing it to a declarative ``RoleSpec`` table — is a separate, test-gated step.

The routing precedence it implements (load-bearing, see tools/delegation_router.py):
explicit ``model=`` -> explicit ``agent_type`` role-map -> auto-route persona pick -> auto-route
tier->role->model -> ``delegation.model``/``by_provider`` -> parent's model.
"""

import logging
from typing import Any, Optional

from tools.delegate_tool_config import _resolve_delegation_credentials
from tools.delegate_tool_progress import _quiet

# Same logger name the routing code logged under before the move, so log filtering by logger
# (``tools.delegate_tool``) keeps working for operators and for tests that assert on records.
logger = logging.getLogger("tools.delegate_tool")


_ROLES = frozenset({"leaf", "orchestrator"})


def _normalize_role(r: Optional[str]) -> str:
    """'leaf' | 'orchestrator'; None/empty/unknown -> 'leaf' (unknown warns)."""
    r_norm = str(r).strip().lower() if r else "leaf"
    if r_norm not in _ROLES:
        logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
        return "leaf"
    return r_norm


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
    synthetic_cfg: dict[str, Any] = {"model": entry.get("model"), "provider": entry.get("provider")}
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


def _load_role_maps() -> tuple[dict[str, Any], dict[str, Any]]:
    """``(role_model_map, role_entry_map)`` for one delegate_task call.

    Two independent guards on purpose: a missing/failing entry-map API must never take
    the flattened model map (and therefore auto-route) down with it.
    """
    try:
        from hermes_cli.personas import get_role_model_map
        role_model_map = get_role_model_map()
    except Exception:
        role_model_map = {}
    try:
        from hermes_cli.personas import get_role_entry_map
        role_entry_map = get_role_entry_map()
    except Exception:
        role_entry_map = {}
    return (role_model_map if isinstance(role_model_map, dict) else {},
            role_entry_map if isinstance(role_entry_map, dict) else {})


def _auto_route_batch(
    task_list, role_model_map, cfg, creds, parent_agent, diagnostics: Optional[dict[str, Any]] = None,
) -> dict[int, dict[str, Any]]:
    """Auto-route verdicts for the whole batch in ONE classifier call.

    Serves two distinct populations (see tools/delegation_router.py's module docstring):
    tasks with no agent_type (or the explicit opt-in ``agent_type="auto"``) are FULLY
    routed tier → role → model; tasks that DID state an agent_type ride along only so
    their tier can be compared — an ESCALATE-ONLY check that can move a task up the
    ladder, never down. A task with an explicit ``model=`` is in neither population and
    is never classified at all.

    ``diagnostics`` (optional, caller-owned) is filled with the WHY when no routing came
    back — see route_task_models. Fail-open: on ANY failure this returns {} and every
    task falls through the existing precedence chain, i.e. exactly the behavior before
    this module existed.
    """
    try:
        from tools.delegation_router import route_task_models
        # The gate keys on the PARENT's provider, not on the resolved child creds' provider: a
        # delegation.by_provider block (or a top-level delegation.provider pin) can redirect the
        # children elsewhere, and the router must still run when the MAIN session's provider is the
        # one listed in delegation.auto_route.providers. Creds are only a fallback for a parent
        # with no provider identity at all.
        provider = (
            (getattr(parent_agent, "provider", None) or "").strip()
            or (creds.get("provider") or "").strip()
        )
        return route_task_models(task_list, role_model_map, cfg, provider, diagnostics) or {}
    except Exception as exc:
        logger.debug("delegate_task: auto-route dispatch failed", exc_info=True)
        if diagnostics is not None:
            diagnostics.clear()
            diagnostics.update({"skipped": "auto-route dispatch failed", "classifier_error": f"{type(exc).__name__}: {exc}"})
        return {}


def _auto_route_skip_cause(diagnostics: Optional[dict[str, Any]]) -> str:
    """Human-readable reason auto-route produced no routing decision, or "" when it
    produced routes / was never asked. Built from ``route_task_models``'s diagnostics
    so the delegation result NAMES the cause instead of only the fallback that ran."""
    if not isinstance(diagnostics, dict):
        return ""
    reason = str(diagnostics.get("skipped") or "").strip()
    if not reason:
        return ""
    if reason == "provider gate closed":
        provider = str(diagnostics.get("provider") or "unknown")
        allowed = diagnostics.get("allowed")
        allowed_str = ", ".join(str(a) for a in allowed) if isinstance(allowed, (list, tuple)) else "?"
        return (
            f"provider gate closed — this session's provider is {provider!r}, but "
            f"delegation.auto_route.providers allows only [{allowed_str}]"
        )
    if reason == "auto_route disabled":
        return "delegation.auto_route.enabled is false"
    if reason == "no tier role has a model":
        return "no delegation.auto_route.tier_roles role has a delegation.model_by_role entry"
    if reason == "no routable task":
        return "every task already stated a model= (auto-route never classifies a stated model)"
    detail = str(diagnostics.get("classifier_error") or "").strip()
    if reason == "classifier returned nothing":
        return f"the classifier returned no usable verdicts ({detail})" if detail else "the classifier returned no usable verdicts"
    if reason == "auto-route dispatch failed":
        return f"auto-route dispatch failed ({detail})" if detail else "auto-route dispatch failed"
    return reason


def _persona_exists(name: str) -> bool:
    """True when a persona markdown file named ``name`` is discoverable."""
    try:
        from hermes_cli.personas import lookup_agent
        return lookup_agent(name) is not None
    except Exception:
        logger.debug("delegate_task: persona lookup failed", exc_info=True)
        return True  # cannot tell -> do not reject


def _route_advisory(i: int, route: Optional[dict[str, Any]], roster_warnings: list[str]) -> Optional[dict[str, Any]]:
    """The classifier's advisory for task ``i`` (noticed in ``roster_warnings``), or None.

    ADVISORY-ONLY (2026-10-13): an explicit agent_type is BINDING. The auto-route
    classifier may still flag a stated role as shallower than the work warrants, but
    that is a NOTICE, never an override — the caller does NOT touch task_agent_type (so
    role→model, role→provider pin, role→persona prompt and role→iteration budget all stay
    on the caller's explicit choice). The regression that motivated this: a 'reviewer'
    task was silently re-dispatched as 'mid-coder' because the classifier's tier ranked
    higher, contradicting the documented precedence ("explicit agent_type is intent").
    """
    if not (route and route.get("advisory")):
        return None
    advisory = {
        "stated": route.get("stated_agent_type"),
        "recommended": route.get("recommended_agent_type"),
        "tier": route.get("recommended_tier"),
        "stated_rank": route.get("stated_rank"),
        "recommended_rank": route.get("recommended_rank"),
        "reason": route.get("reason"),
    }
    roster_warnings.append(
        f"Task {i}: agent_type={advisory['stated']!r} is BINDING (an explicit choice is "
        f"intent). The auto-route classifier recommends the deeper role "
        f"{advisory['recommended']!r} (tier {advisory['tier']!r}, rank "
        f"{advisory['stated_rank']}→{advisory['recommended_rank']})"
        + (f": {advisory['reason']}" if advisory.get("reason") else "")
        + ". Nothing was changed — to dispatch that role, pass "
        "agent_type=<role> explicitly."
    )
    logger.info(
        "delegate_task: task %d advisory — stated agent_type=%r kept; classifier "
        "recommends %r (rank %s->%s)",
        i, advisory["stated"], advisory["recommended"],
        advisory["stated_rank"], advisory["recommended_rank"],
    )
    return advisory


def _resolve_role_route_creds(
    role_entry, role_fallback_entry, parent_agent, role_creds_cache, *,
    role_cfg_key, task_agent_type, role_provider, task_fallback_chain, role_map_model,
) -> tuple[Optional[dict], Optional[list[dict[str, Any]]], Any, Optional[str]]:
    """Resolve a provider-pinned role's credentials, engaging its fallback bundle if needed.

    Returns ``(task_creds, task_fallback_chain, role_map_model, None)`` or
    ``(None, None, None, error)`` when the pin (and its fallback, if any) cannot be resolved.
    """
    try:
        task_creds = _resolve_role_credentials(role_entry, parent_agent, role_creds_cache)
    except ValueError as primary_exc:
        via_alias = (
            f" (dispatched as {task_agent_type!r})"
            if task_agent_type and role_cfg_key != task_agent_type else ""
        )
        if role_fallback_entry is None:
            # Fail loud: falling back to the batch provider here is exactly the
            # wrong-model-on-wrong-provider bug this pin exists to prevent. Name
            # the CONFIG key (the alias target when the dispatched role is a
            # synonym) so the user can find the entry to fix, and name the
            # dispatched role too when they differ.
            return None, None, None, (
                f"delegation.model_by_role[{role_cfg_key!r}]{via_alias} pins provider "
                f"{role_provider!r} but it could not be resolved: {primary_exc}"
            )
        try:
            task_creds = _resolve_role_credentials(role_fallback_entry, parent_agent, role_creds_cache)
        except ValueError as fallback_exc:
            return None, None, None, (
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
    return task_creds, task_fallback_chain, role_map_model, None


def _warn_unrouted_task(i, route, effective_task_model, auto_route_diag, roster_warnings) -> None:
    """Notice for a task that stated NEITHER model= nor agent_type=: name who chose its model."""
    if route:
        decision = (
            f"auto-route classifier → tier {route.get('tier')!r} → role "
            f"{route.get('role')!r} → model {effective_task_model!r}"
        )
        reason = str(route.get("reason") or "").strip()
        if reason:
            decision += f" ({reason})"
    elif effective_task_model:
        # Name WHY auto-route did not run, so "the config default won" is
        # actionable instead of a dead end (a closed provider gate and a
        # broken classifier look identical from here otherwise).
        decision = f"delegation config default → model {effective_task_model!r}"
        cause = _auto_route_skip_cause(auto_route_diag)
        if cause:
            decision += f" (auto-route did not run: {cause})"
    else:
        decision = "inherited the PARENT's own model/provider"
        cause = _auto_route_skip_cause(auto_route_diag)
        if cause:
            decision += f" (auto-route did not run: {cause})"
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


def _route_info(route: Optional[dict[str, Any]], advisory: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Auto-route provenance carried on a task's route (None when the task was not routed)."""
    if not route:
        return None
    route_info = {
        "tier": route.get("tier"), "role": route.get("role"), "model": route.get("model"),
        "reason": route.get("reason"), "agent_type": route.get("agent_type"),
    }
    if advisory is not None:
        route_info.update({
            "advisory": True, "recommended_agent_type": advisory["recommended"],
            "recommended_tier": advisory["tier"], "stated_rank": advisory["stated_rank"],
            "recommended_rank": advisory["recommended_rank"],
        })
    return route_info


def _resolve_task_routes(
    task_list, creds, *, cfg, parent_agent, top_role, roster_warnings,
) -> tuple[list[dict[str, Any]], Optional[str]]:
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
    role_model_map, role_entry_map = _load_role_maps()
    auto_route_diag: dict[str, Any] = {}
    auto_routes = _auto_route_batch(task_list, role_model_map, cfg, creds, parent_agent, auto_route_diag)
    # The literal agent_type meaning "auto-route this task" — imported from the router
    # (single source of truth) with a literal fallback so a partially-loaded router
    # module can never break dispatch.
    try:
        from tools.delegation_router import AUTO_AGENT_TYPE
    except Exception:
        AUTO_AGENT_TYPE = "auto"

    role_creds_cache: dict[tuple, dict] = {}
    routes: list[dict[str, Any]] = []
    for i, t in enumerate(task_list):
        effective_role = _normalize_role(t.get("role") or top_role)
        # agent_type="auto" is NOT a persona: it is the explicit opt-in to auto-routing,
        # so it normalises to None here (identical routing to omitting the field — no
        # bogus model_by_role["auto"] lookup, no "auto" persona prompt) while still
        # counting as an explicit choice that does not trip the omission warning.
        #
        # EXPLICITNESS SENTINEL: read the RAW value BEFORE any default fill-in. A
        # missing key (None) or a non-string means the caller did NOT state an
        # agent_type; only a non-empty string is an explicitly-passed, BINDING choice
        # (2026-10-13 — the auto-route classifier may no longer override it). The
        # batch-wide default seeding in delegate_task() has already run by the time we
        # get here, but it only ever forwards a caller-supplied TOP-LEVEL agent_type,
        # which is itself an explicit choice, so the sentinel remains truthful.
        _raw_agent_type = t.get("agent_type")
        stated_agent_type = _raw_agent_type.strip() if isinstance(_raw_agent_type, str) else ""
        agent_type_is_auto = stated_agent_type.lower() == AUTO_AGENT_TYPE
        agent_type_omitted = not stated_agent_type
        task_agent_type = None if (agent_type_omitted or agent_type_is_auto) else stated_agent_type
        task_model_explicit = (t.get("model") or "").strip() or None
        route = auto_routes.get(i)

        # Auto-route persona pick feeds the SAME task_agent_type variable an explicit
        # agent_type uses, so it fires both existing effects for free (persona-prompt
        # injection AND per-role model resolution). Explicit always wins.
        tier_route_role = None
        if task_agent_type is None and route:
            auto_agent_type = (route.get("agent_type") or "").strip() or None
            if auto_agent_type:
                task_agent_type = auto_agent_type
            else:
                # TIER-ONLY route (no persona pick): the tier's role is a real
                # model_by_role key, so it must be resolved EXACTLY like a stated
                # agent_type is — role_cfg_key below drives both the model lookup and
                # the per-role credential resolution (provider pin + fallback chain).
                # Without this the role's model slug was paired with the batch creds'
                # provider — the parent's, when no by_provider block matched — i.e. a
                # guaranteed wrong-endpoint 404.
                tier_route_role = (route.get("role") or "").strip() or None

        # ADVISORY-ONLY: an explicit agent_type is BINDING; the advisory is a notice and
        # task_agent_type is deliberately left untouched (see _route_advisory).
        advisory = _route_advisory(i, route, roster_warnings)

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

        # The config key used for BOTH role-keyed lookups below (model and credential
        # entry), resolved ONCE so the two can never disagree.
        # A TIER-ONLY auto-route has no agent_type to dispatch (no persona prompt), but
        # the tier's role IS a model_by_role key and is resolved through this same key —
        # that is what applies the role's provider pin and fallback chain to the route.
        role_cfg_key = task_agent_type or tier_route_role
        role_map_model = role_model_map.get(role_cfg_key) if role_cfg_key else None

        # A STATED agent_type that names neither a model_by_role entry nor a persona would
        # silently run on the default/parent model (the expensive one) — refuse the whole
        # spawn instead. Fail-open only when no roster is configured at all (nothing to be
        # unknown AGAINST). Auto-route picks and tier roles are config-derived, not stated.
        if (
            task_agent_type is not None
            and not agent_type_is_auto
            and role_model_map
            and task_agent_type == stated_agent_type
            and not (role_cfg_key in role_model_map or role_cfg_key in role_entry_map)
            and not _persona_exists(task_agent_type)
        ):
            known = ", ".join(sorted(role_model_map))
            return [], (
                f"Task {i}: agent_type={task_agent_type!r} is not a delegation.model_by_role "
                f"role and has no persona file, so it would silently run on the default/parent "
                f"model. Known roles: {known}. Pass a known role, agent_type='auto', or an "
                f"explicit model=."
            )

        # The classifier's model is only a FALLBACK for tasks it was allowed to route:
        # ones that stated no agent_type (or opted in with "auto"). For a task whose
        # stated agent_type survived — which is now ALWAYS, since an explicit agent_type
        # is binding — a route entry must not contribute a model at all, otherwise a role
        # with no model_by_role entry would silently pick up the classifier's tier model.
        stated_survived = bool(stated_agent_type) and not agent_type_is_auto
        auto_route_model = route.get("model") if (route and not stated_survived) else None

        task_creds = creds
        task_fallback_chain: Optional[list[dict[str, Any]]] = None
        role_entry = role_entry_map.get(role_cfg_key) if role_cfg_key else None
        role_provider = str(role_entry.get("provider") or "").strip() if isinstance(role_entry, dict) else ""
        # A role marked ``pinned: true`` in delegation.model_by_role is a HARD INVARIANT:
        # its (provider, model) pair must never be substituted by any automatic mechanism.
        # An explicit agent_type is already binding (above), so the remaining leak is the
        # AMBIENT fallback chain the child would otherwise inherit from the parent session
        # — that could silently reroute a pinned role (e.g. the custom exo-dsv41 role) onto
        # a different provider at runtime. A pinned role gets NO inherited chain; it may
        # still declare its own explicit ``fallback`` bundle, which is a deliberate choice.
        role_pinned = bool(isinstance(role_entry, dict) and role_entry.get("pinned"))
        if role_pinned:
            auto_route_model = None
            # An EMPTY list (not None) is the "do not inherit" signal: ``None`` means
            # "consult the ambient parent chain", ``[]`` means "no runtime fallback".
            task_fallback_chain = []
        # Per-role fallback: a second full {model, provider, ...} bundle, consulted at
        # construction time (below) and at runtime (via override_fallback_chain). Only
        # meaningful alongside a provider pin — a fallback identity with no primary
        # provider has nothing to be a fallback FOR.
        role_fallback_entry = role_entry.get("fallback") if (role_provider and isinstance(role_entry, dict)) else None
        if not isinstance(role_fallback_entry, dict):
            role_fallback_entry = None

        if role_provider and isinstance(role_entry, dict):
            task_creds, task_fallback_chain, role_map_model, error = _resolve_role_route_creds(
                role_entry, role_fallback_entry, parent_agent, role_creds_cache,
                role_cfg_key=role_cfg_key, task_agent_type=task_agent_type, role_provider=role_provider,
                task_fallback_chain=task_fallback_chain, role_map_model=role_map_model,
            )
            if error is not None:
                return [], error

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
            _warn_unrouted_task(i, route, effective_task_model, auto_route_diag, roster_warnings)

        route_info = _route_info(route, advisory)

        routes.append({
            "role": effective_role, "agent_type": task_agent_type, "model": effective_task_model,
            "creds": task_creds, "fallback_chain": task_fallback_chain, "route_info": route_info,
        })
    return routes, None


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
    cfg: dict[str, Any], creds: dict[str, Any], parent_agent
) -> tuple[set[str], bool]:
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
    known: set[str] = set()
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
        from hermes_cli.personas import get_role_entry_map

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
    task_list: list[dict[str, Any]], *, depth: int, known_models: set[str],
    has_config_roster: bool, roster_warnings: list[str],
) -> tuple[list[dict[str, Any]], Optional[str]]:
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
    guarded: list[dict[str, Any]] = []
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
