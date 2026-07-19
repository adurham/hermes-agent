"""Auto-route delegated tasks to the right model tier (fork feature).

Problem this solves: the operator's main chat model is deliberately a cheap
model (cost control), and real work is meant to fan out through
``delegate_task`` onto the *right* model per task — Haiku-class for retrieval,
Sonnet-class for bounded coding, Opus-class for architecture/security-sensitive
work.  The routing knobs for that already exist (``delegation.model_by_role`` +
the ``agent_type`` param), but they only fire when the calling model remembers
to set ``agent_type`` — an in-context judgment call that observably gets
skipped (the exact failure: three coding tasks dispatched with no
``agent_type``/``model``, all silently inheriting the parent model, including
one auth/session-surface task that warranted the top tier).

This module closes that gap mechanically: when a task arrives with neither an
explicit per-task ``model`` nor an ``agent_type``, a cheap auxiliary LLM call
classifies the task into a tier (light / standard / deep), the tier maps to a
role (config: ``delegation.auto_route.tier_roles``), and the role resolves to
a model through the existing ``delegation.model_by_role`` map — the same
single source of truth the explicit path uses.

Precedence (documented in tools/delegate_tool.py): explicit per-task
``model`` → ``agent_type`` role-map → **auto-route (this module)** →
``delegation.model`` / ``delegation.by_provider.<p>.model`` → parent's model.
Auto-route sits *above* the config-level model pin because that pin is a
blanket cost-guard default ("children default cheap"), which is exactly the
default this router refines per-task; it sits *below* anything the caller
stated explicitly, because a stated choice is intent.

Fail-open by design: any failure here (classifier unavailable, timeout,
garbage output, unmapped role, disabled feature) yields NO routing for the
affected task — the task falls through to the existing precedence chain,
i.e. exactly the behavior before this module existed.  A routing decision is
never worse than the status quo, and every decision (including fallbacks) is
surfaced in the delegation result metadata so silent misrouting can't hide.

Classification also optionally picks a ruflo persona (``agent_type``) for a
task, using the SAME single aux call — no second LLM round-trip. When the
classifier confidently matches a task to a discovered persona (name
validated against the real catalog; hallucinated/unknown names are dropped),
this module returns that ``agent_type`` alongside the tier/role/model. The
caller (``tools/delegate_tool.py``) feeds it into the same ``task_agent_type``
variable used for an explicit, caller-supplied ``agent_type`` — so it gets
BOTH pre-existing effects of that variable for free: ruflo persona-prompt
injection (``_build_child_system_prompt``) AND per-role model resolution
(``_role_model_map``/``delegation.model_by_role``), without duplicating
either mechanism here. This module never injects prompts itself and never
picks a model outside ``model_by_role`` — it only supplies the *label*.

Persona classification is a strict refinement of tier classification, not a
separate pass: the persona pick, when present, sits in front of the tier→
role→model fallback the same way an explicit ``agent_type`` sits in front of
auto-route today, i.e. the precedence chain becomes: explicit ``model`` →
explicit ``agent_type`` role-map → auto-route persona pick (this module) →
auto-route tier→role→model (this module) → ``delegation.model`` → parent's
model. Persona classification is independently toggleable via
``delegation.auto_route.classify_persona`` (default True) so a user can keep
tier-based model routing while disabling automatic persona/prompt injection
for cost or behavior-stability reasons; disabling it degrades exactly to the
pre-persona auto-route behavior, never worse.

Only personas that already resolve to a model via ``delegation.model_by_role``
are offered to the classifier — an unroutable persona pick would be a no-op
at best and a confusing partial match at worst, so the catalog is trimmed to
the working set the rest of the system can actually act on. The persona
catalog is discovered from disk once per process (``discover_personas()`` is
a filesystem walk) and cached at module scope.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Auxiliary task key for provider/model resolution (auxiliary.delegation_router
# in config.yaml). Registered in hermes_cli/config.py DEFAULT_CONFIG and the
# task-first key sets; resolves through the standard aux chain, so it lands on
# the provider's cheap default (e.g. auxiliary.anthropic.default) unless the
# user pins something else.
AUX_TASK = "delegation_router"

TIERS = ("light", "standard", "deep")

# tier → role defaults; overridable via delegation.auto_route.tier_roles.
# The role then resolves to a model via delegation.model_by_role — reusing the
# user's existing per-role pins rather than inventing a second model map.
DEFAULT_TIER_ROLES: Dict[str, str] = {
    "light": "researcher",
    "standard": "coder",
    "deep": "system-architect",
}

# Providers whose model_by_role entries make sense to auto-apply. model_by_role
# values are model slugs for a specific provider family (typically claude-*);
# blindly routing them onto an exo/ollama/openrouter child would 404. Guarded
# rather than clever; extend via delegation.auto_route.providers.
DEFAULT_PROVIDERS = ("anthropic",)

_MAX_TASK_CHARS_DEFAULT = 1500
_TIMEOUT_DEFAULT = 20

# Per-persona description excerpt length in the catalog sent to the
# classifier -- keeps the prompt bounded even with ~90 personas.
_PERSONA_DESC_CHARS = 100

_BASE_SYSTEM_PROMPT = """You are a dispatcher classifying delegated agent tasks by the model capability they need. For each task, pick exactly one tier:

- "light": retrieval, lookup, grep/search, summarizing documents, extracting data, simple triage. No novel reasoning.
- "standard": bounded code changes that follow existing patterns in a codebase, code review, writing tests, debugging with a clear reproducer, focused analysis with clear success criteria (tests/typecheck).
- "deep": designing NEW architecture or subsystem plumbing that doesn't already exist, anything touching auth/session/cookie/security/data-integrity surfaces, work whose correctness can NOT be verified by automated tests, or genuinely open-ended/ambiguous design work.

Escalate to "deep" if ANY deep criterion applies, even when the task otherwise looks like standard coding. When genuinely uncertain between two tiers, pick the higher one."""

_TIER_ONLY_SUFFIX = """

Respond with ONLY a JSON array, no prose, one entry per task:
[{"index": <int>, "tier": "light"|"standard"|"deep", "reason": "<one short clause>"}]"""

_PERSONA_SUFFIX_TEMPLATE = """

You may ALSO pick a specialist persona from this catalog if -- and only if --
one is a clearly, specifically better fit than the generic tier role. When
uncertain, or when no persona stands out, leave "agent_type" empty; a bad
persona match is worse than no match.

PERSONA CATALOG (name -- category -- description):
{catalog}

Respond with ONLY a JSON array, no prose, one entry per task:
[{{"index": <int>, "tier": "light"|"standard"|"deep", "agent_type": "<persona-name-or-empty>", "reason": "<one short clause>"}}]"""


_PERSONA_CATALOG_CACHE: Optional[List[Tuple[str, str, str]]] = None


def _persona_catalog(role_model_map: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """Return ``(name, category, description)`` tuples for classifiable personas.

    Discovered once per process (filesystem walk) and cached at module scope.
    Trimmed to personas whose name already resolves to a model via
    ``delegation.model_by_role`` — a persona pick that can't resolve to a
    model would be a confusing partial routing decision, so it's excluded
    from the catalog offered to the classifier entirely.
    """
    global _PERSONA_CATALOG_CACHE
    if _PERSONA_CATALOG_CACHE is None:
        try:
            from hermes_cli.personas import discover_personas

            personas = discover_personas()
        except Exception:
            personas = []
        _PERSONA_CATALOG_CACHE = [
            (p.name, p.category, (p.description or "")[:_PERSONA_DESC_CHARS])
            for p in personas
        ]
    if not role_model_map:
        return []
    routable = set(role_model_map.keys())
    return [entry for entry in _PERSONA_CATALOG_CACHE if entry[0] in routable]


def _excerpt(task: Dict[str, Any], max_chars: int) -> str:
    goal = str(task.get("goal") or "").strip()
    context = str(task.get("context") or "").strip()
    # Goal carries the intent; give it priority, then fill with context.
    if len(goal) >= max_chars:
        return goal[:max_chars]
    remaining = max_chars - len(goal)
    if context:
        return f"{goal}\n[context] {context[:remaining]}"
    return goal


def _auto_route_cfg(cfg: dict) -> dict:
    raw = cfg.get("auto_route") if isinstance(cfg, dict) else None
    return raw if isinstance(raw, dict) else {}


def _parse_classifier_json(text: str) -> Optional[List[dict]]:
    """Parse the classifier reply defensively.

    Accepts a bare JSON array, or an array embedded in fenced/prose output
    (models occasionally wrap despite instructions). Returns None when no
    parsable array is found — caller fails open.
    """
    if not text:
        return None
    candidates = [text.strip()]
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            parsed = json.loads(cand, strict=False)
        except Exception:
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def route_task_models(
    task_list: List[Dict[str, Any]],
    role_model_map: Dict[str, str],
    delegation_cfg: dict,
    active_provider: Optional[str],
) -> Dict[int, Dict[str, str]]:
    """Classify unrouted tasks and return per-index model/persona routing.

    Args:
        task_list: the normalized delegate_task task dicts (goal/context/
            model/agent_type/...). Read-only — never mutated here.
        role_model_map: ``delegation.model_by_role`` (already loaded by the
            caller; passed in so config is read once per delegation).
        delegation_cfg: the ``delegation`` config block (for ``auto_route``).
        active_provider: the provider the children will actually run on
            (delegation override if set, else the parent's provider).

    Returns:
        ``{task_index: {"model": ..., "tier": ..., "role": ..., "reason": ...,
        "agent_type": ...}}`` for every task the router confidently routed.
        ``agent_type`` is present (non-empty) only when the classifier
        confidently matched a discovered, model-routable persona; absent
        otherwise, in which case the caller should treat the entry as
        tier/model-only routing. Tasks absent from the map fall through to
        the existing precedence chain (fail-open). Never raises.
    """
    try:
        ar = _auto_route_cfg(delegation_cfg)
        if not ar.get("enabled", True):
            return {}

        providers_raw = ar.get("providers")
        providers = (
            tuple(str(p).strip().lower() for p in providers_raw if str(p).strip())
            if isinstance(providers_raw, (list, tuple)) and providers_raw
            else DEFAULT_PROVIDERS
        )
        if (active_provider or "").strip().lower() not in providers:
            return {}

        # Only tasks with neither an explicit model nor an agent_type are
        # eligible — a stated choice is intent and always wins.
        pending: List[int] = [
            i
            for i, t in enumerate(task_list)
            if not str(t.get("model") or "").strip()
            and not str(t.get("agent_type") or "").strip()
        ]
        if not pending:
            return {}

        tier_roles_raw = ar.get("tier_roles")
        tier_roles = dict(DEFAULT_TIER_ROLES)
        if isinstance(tier_roles_raw, dict):
            for k, v in tier_roles_raw.items():
                if isinstance(k, str) and k in TIERS and isinstance(v, str) and v.strip():
                    tier_roles[k] = v.strip()

        # If no tier's role resolves to a model, classification is pointless.
        if not any(role_model_map.get(r) for r in tier_roles.values()):
            logger.debug(
                "delegation auto-route: no tier role has a model_by_role entry; skipping"
            )
            return {}

        max_chars = ar.get("max_chars")
        max_chars = (
            max_chars
            if isinstance(max_chars, int) and max_chars > 0
            else _MAX_TASK_CHARS_DEFAULT
        )
        timeout = ar.get("timeout")
        timeout = (
            timeout if isinstance(timeout, (int, float)) and timeout > 0 else _TIMEOUT_DEFAULT
        )

        classify_persona = bool(ar.get("classify_persona", True))
        persona_catalog = _persona_catalog(role_model_map) if classify_persona else []
        persona_names = {name for name, _cat, _desc in persona_catalog}

        results = _classify(
            [(i, _excerpt(task_list[i], max_chars)) for i in pending],
            timeout=float(timeout),
            persona_catalog=persona_catalog,
        )
        if not results:
            return {}

        routes: Dict[int, Dict[str, str]] = {}
        for idx, result in results.items():
            # Tolerate legacy 2-tuple (tier, reason) results — e.g. tests or
            # callers that monkeypatch _classify pre-persona-support — as
            # well as the current 3-tuple (tier, reason, agent_type).
            if len(result) == 3:
                tier, reason, agent_type = result
            else:
                tier, reason = result
                agent_type = ""
            if idx not in pending:  # classifier hallucinated an index
                continue
            role = tier_roles.get(tier, "")
            model = role_model_map.get(role, "") if role else ""
            if not model:
                # Unmapped role — fail open for this task, but say so.
                logger.debug(
                    "delegation auto-route: task %s classified %r but role %r has "
                    "no model_by_role entry; inheriting default",
                    idx, tier, role,
                )
                continue
            route: Dict[str, str] = {
                "model": model,
                "tier": tier,
                "role": role,
                "reason": reason,
            }
            # Validate the persona pick against the actual catalog offered —
            # a hallucinated/unrecognized name is dropped, never passed
            # through blindly (falls back to tier/model-only routing).
            if agent_type and agent_type in persona_names:
                route["agent_type"] = agent_type
            elif agent_type:
                logger.debug(
                    "delegation auto-route: task %s classifier picked unknown "
                    "persona %r; dropping (tier/model routing still applies)",
                    idx, agent_type,
                )
            routes[idx] = route
        return routes
    except Exception:
        # Absolute fail-open: a router bug must never break delegation itself.
        logger.debug("delegation auto-route failed; inheriting defaults", exc_info=True)
        return {}


def _classify(
    pending: List[Tuple[int, str]],
    *,
    timeout: float,
    persona_catalog: Optional[List[Tuple[str, str, str]]] = None,
) -> Dict[int, Tuple[str, str, str]]:
    """One auxiliary LLM call classifying every pending task.

    Returns ``{index: (tier, reason, agent_type)}``; ``agent_type`` is ""
    when no persona was picked. Empty dict on any failure.
    """
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
    except Exception:
        return {}
    try:
        client, model = get_text_auxiliary_client(AUX_TASK)
    except Exception:
        return {}
    if client is None or not model:
        return {}

    lines = [f"Task {i}:\n{text}" for i, text in pending]
    user_msg = "\n\n---\n\n".join(lines)

    if persona_catalog:
        catalog_lines = "\n".join(
            f"- {name} ({category}): {desc}" if desc else f"- {name} ({category})"
            for name, category, desc in persona_catalog
        )
        system_prompt = _BASE_SYSTEM_PROMPT + _PERSONA_SUFFIX_TEMPLATE.format(
            catalog=catalog_lines
        )
    else:
        system_prompt = _BASE_SYSTEM_PROMPT + _TIER_ONLY_SUFFIX

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=100 + 80 * len(pending),
            temperature=0,
            timeout=timeout,
        )
        text = (resp.choices[0].message.content or "") if resp.choices else ""
    except Exception:
        logger.debug("delegation auto-route classifier call failed", exc_info=True)
        return {}

    parsed = _parse_classifier_json(text)
    if parsed is None:
        logger.debug("delegation auto-route: unparsable classifier reply: %.200s", text)
        return {}

    out: Dict[int, Tuple[str, str, str]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        tier = str(item.get("tier") or "").strip().lower()
        if isinstance(idx, int) and tier in TIERS:
            agent_type = str(item.get("agent_type") or "").strip()
            out[idx] = (tier, str(item.get("reason") or "").strip()[:200], agent_type)
    return out

