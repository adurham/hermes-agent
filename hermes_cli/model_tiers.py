"""Shared source of truth for delegation model tiers.

Two independent ladders feed ``suggest_retunes``:

  * The **Anthropic ladder** — the three tier anchors (haiku / sonnet /
    opus). Backbone of two surfaces:
      * ``hermes_cli.persona_library.SUGGESTED_ROLE_MODELS`` — the curated
        per-role model defaults written into ``delegation.model_by_role``.
      * ``hermes_cli.delegation_stats.suggest_retunes`` — promote/demote
        suggestions keyed on the tier a role's current model belongs to.
  * The **local ladder** — a non-Anthropic model ladder (e.g. the user's
    ollama-cloud roster: gemma4:31b / deepseek-v4-flash:0731 / glm-5.3)
    resolved live from ``delegation.model_by_role`` via explicit role
    anchors (see :data:`ROLE_TIER_GROUPS` and :func:`local_ladder`).

Historically each file hardcoded its own copy of the three Anthropic model
literals (``claude-haiku-4-5`` / ``claude-sonnet-4-6`` / ``claude-opus-4-7``).
When the live config roster moved to a new generation, the hardcoded literals
silently went stale: ``apply_suggested_defaults`` kept writing the old
generation into the user's config, and ``suggest_retunes`` silently stopped
firing because the roster's current models no longer matched the hardcoded
tier map. This module is the single source of truth both surfaces share, so
a generation bump can never again leave one surface stale. The local ladder
extends the same call-time resolution to non-Anthropic models so promote /
demote / escalate suggestions fire for them too.

Resolution is ALWAYS at call time, never import time. The test suite
sandboxes ``HERMES_HOME`` per test, so an import-time read would resolve
once under whichever home is active at import and then freeze stale. Every
public function here reads the live config on each call.
"""

from __future__ import annotations

import re
from typing import Dict, Iterator, Optional

# ── Last-known-good tier anchors ───────────────────────────────────────────
# These are BOTH the tier anchors (which family each role belongs to) AND
# the fallback when the live roster contains no model for a family (fresh
# install, all-local setup, sandboxed tests). They live in exactly ONE
# place. When the roster moves to a new generation, update ONLY these three
# literals — every consumer derives through them.
LAST_KNOWN_GOOD_TIERS: Dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

# Tier names in ascending capability order (rank 0 = cheapest).
TIER_NAMES: tuple[str, ...] = ("haiku", "sonnet", "opus")

# Family -> regex recognizing a model string as belonging to that tier.
# Matched against the provider-prefix-stripped, lowercased model id.
_FAMILY_PATTERNS: Dict[str, re.Pattern[str]] = {
    "haiku": re.compile(r"^claude-haiku-"),
    "sonnet": re.compile(r"^claude-sonnet-"),
    "opus": re.compile(r"^claude-opus-"),
}


def _normalize_model(model: str) -> str:
    """Strip a provider prefix (``anthropic/claude-opus-5`` -> ``claude-opus-5``)."""
    m = str(model).strip()
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    return m


def family_of(model: str) -> Optional[str]:
    """Return the tier name a model string belongs to, or ``None``.

    Case-insensitive and provider-prefix-tolerant: ``anthropic/claude-opus-5``
    and ``claude-opus-5`` both resolve to ``"opus"``. A model that is not an
    Anthropic haiku/sonnet/opus (e.g. ``claude-fable-5``, ``qwen3-coder``)
    returns ``None``.
    """
    norm = _normalize_model(model).lower()
    for family, pat in _FAMILY_PATTERNS.items():
        if pat.match(norm):
            return family
    return None


def _iter_roster_models() -> Iterator[str]:
    """Yield model strings from the live delegation config in precedence order.

    Precedence (first match wins per family, stable across runs for a given
    config file — dict insertion order is deterministic):

      1. ``delegation.by_provider.<p>.model``  — provider-scoped blocks, in
         config order. The most explicit "this provider runs this model"
         declaration.
      2. ``delegation.model_by_role.<role>.model`` — primary per-role pins,
         in config order.
      3. ``delegation.model_by_role.<role>.fallback.model`` — nested fallback
         bundles, in config order.
      4. ``delegation.model`` — the top-level blanket default.

    Reads the live config on every call (never cached at import). Yields
    nothing when the config is missing/unparseable.
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return
    try:
        cfg = load_config()
    except Exception:
        return
    delegation = cfg.get("delegation") if isinstance(cfg, dict) else None
    if not isinstance(delegation, dict):
        return

    # 1. by_provider blocks
    by_provider = delegation.get("by_provider")
    if isinstance(by_provider, dict):
        for block in by_provider.values():
            if isinstance(block, dict):
                m = block.get("model")
                if isinstance(m, str) and m.strip():
                    yield m

    # 2. model_by_role primary entries
    mbr = delegation.get("model_by_role")
    if isinstance(mbr, dict):
        for entry in mbr.values():
            if isinstance(entry, str):
                if entry.strip():
                    yield entry
            elif isinstance(entry, dict):
                m = entry.get("model")
                if isinstance(m, str) and m.strip():
                    yield m

    # 3. model_by_role fallback bundles
    if isinstance(mbr, dict):
        for entry in mbr.values():
            if isinstance(entry, dict):
                fb = entry.get("fallback")
                if isinstance(fb, dict):
                    m = fb.get("model")
                    if isinstance(m, str) and m.strip():
                        yield m

    # 4. top-level delegation.model
    m = delegation.get("model")
    if isinstance(m, str) and m.strip():
        yield m


def resolve_tier_model(tier: str) -> str:
    """Resolve the current model for a tier from the LIVE delegation config.

    Scans the roster (see :func:`_iter_roster_models` for the precedence
    order) and returns the first model whose family matches ``tier``. When
    the roster contains no model for that family, falls back to the
    last-known-good literal in :data:`LAST_KNOWN_GOOD_TIERS` so behavior is
    unchanged where there is nothing to be current against.

    Raises ``ValueError`` for an unknown tier name.
    """
    if tier not in LAST_KNOWN_GOOD_TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
    for model in _iter_roster_models():
        if family_of(model) == tier:
            return _normalize_model(model)
    return LAST_KNOWN_GOOD_TIERS[tier]


def tier_rank_map() -> Dict[str, int]:
    """Return ``{current_model: rank}`` for the three tiers (call-time).

    Rank 0 = haiku (cheapest), 1 = sonnet, 2 = opus. Built fresh on every
    call so roster-current models are always recognized.
    """
    return {resolve_tier_model(t): i for i, t in enumerate(TIER_NAMES)}


def rank_tier_map() -> Dict[int, str]:
    """Return ``{rank: current_model}`` for the three tiers (call-time)."""
    return {i: resolve_tier_model(t) for i, t in enumerate(TIER_NAMES)}


# ── Local (non-Anthropic) ladder ───────────────────────────────────────────
#
# The Anthropic ladder above is the only ordering source for the haiku /
# sonnet / opus tiers. The LOCAL ladder is a second, independent ordering
# for non-Anthropic models (e.g. the user's ollama-cloud roster). Its ranks
# are derived from EXPLICIT role anchors in ``delegation.model_by_role``,
# NOT from role-name ordering at call time — the mapping constant below is
# the only ordering source.

#: Explicit role -> tier-rank anchors for the local ladder. Rank 0 is the
#: cheapest / least capable, ascending. ``sr-coder`` and ``pm`` both anchor
#: the heavy rank-2 tier. Roles absent from the live config contribute
#: nothing; two anchors resolving to the same model is fine (same rank).
ROLE_TIER_GROUPS: tuple[tuple[str, int], ...] = (
    ("jr-coder", 0),
    ("mid-coder", 1),
    ("sr-coder", 2),
    ("pm", 2),
)

#: Suffix patterns that mark a model as a lighter variant of a ranked base
#: model (e.g. ``glm-5.3-flash`` is a lighter ``glm-5.3``). Used ONLY as a
#: fallback when a model is not itself an anchor-resolved model. Brittle by
#: design — a model id that happens to end in one of these suffixes is
#: treated as a variant even if it is not actually one. Documented here so
#: the heuristic's limits are explicit.
_VARIANT_SUFFIXES: tuple[str, ...] = ("-flash", "-mini", "-fast")


def _read_model_by_role() -> Dict[str, object]:
    """Return the raw ``delegation.model_by_role`` dict, or ``{}``.

    Same load path as :func:`_iter_roster_models` (live ``load_config`` on
    every call). Returns an empty dict when the config is missing,
    unparseable, or has no ``model_by_role`` section.
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        cfg = load_config()
    except Exception:
        return {}
    delegation = cfg.get("delegation") if isinstance(cfg, dict) else None
    if not isinstance(delegation, dict):
        return {}
    mbr = delegation.get("model_by_role")
    return mbr if isinstance(mbr, dict) else {}


def _role_primary_model(mbr: Dict[str, object], role: str) -> Optional[str]:
    """Return the PRIMARY model string for ``role``, or ``None``.

    A bare-string entry is its own model; a dict entry contributes its
    ``model`` key. Returns ``None`` when the role is absent or has no
    usable primary model.
    """
    entry = mbr.get(role)
    if isinstance(entry, str):
        m = entry
    elif isinstance(entry, dict):
        m = entry.get("model")
    else:
        return None
    if isinstance(m, str) and m.strip():
        return _normalize_model(m.strip())
    return None


def _role_fallback_model(mbr: Dict[str, object], role: str) -> Optional[str]:
    """Return the nested ``fallback.model`` for ``role``, or ``None``.

    Only a dict entry with a nested ``fallback`` dict carrying a usable
    ``model`` contributes. Bare-string entries can never carry a fallback.
    """
    entry = mbr.get(role)
    if not isinstance(entry, dict):
        return None
    fb = entry.get("fallback")
    if not isinstance(fb, dict):
        return None
    m = fb.get("model")
    if isinstance(m, str) and m.strip():
        return _normalize_model(m.strip())
    return None


def local_ladder() -> Dict[str, int]:
    """Return ``{model: rank}`` for the local (non-Anthropic) ladder.

    Ranks come from :data:`ROLE_TIER_GROUPS`: each anchor role's PRIMARY
    model (resolved live from ``delegation.model_by_role``) maps to that
    role's tier rank. Roles absent from config contribute nothing; two
    anchors resolving to the same model collapse to one entry (same rank).

    A model that is not itself an anchor-resolved model but is a recognized
    suffix variant of one (see :data:`_VARIANT_SUFFIXES`) is ranked one step
    below its base — ``max(0, base_rank - 1)``. This is a fallback only and
    is deliberately brittle: any model id ending in a variant suffix is
    treated as a variant even if it is not actually one. The base model must
    itself be anchor-resolved for the variant to rank at all.

    Anthropic models are never part of the local ladder — they belong to the
    Anthropic ladder (:func:`tier_rank_map`). Enforcement is by
    :func:`family_of`: an anchor-resolved primary whose family is one of the
    three tier families (haiku / sonnet / opus) is skipped, so it can never
    join the local ladder or steal a rank slot from a local model. A
    hypothetical non-tier Anthropic id (e.g. ``claude-fable-5``) is NOT
    excluded — :func:`family_of` only recognizes the three tier families —
    but that is acceptable: such an id is not a member of the Anthropic
    ladder (:func:`tier_rank_map` ranks only haiku / sonnet / opus), so it
    carries no cross-ladder ambiguity and is treated as a local model. The
    invariant's purpose is to keep a model that IS in the Anthropic ladder
    out of the local ladder, which this achieves.
    """
    mbr = _read_model_by_role()
    ranks: Dict[str, int] = {}
    for role, rank in ROLE_TIER_GROUPS:
        model = _role_primary_model(mbr, role)
        if model is not None and family_of(model) is None:
            ranks[model] = rank
    # Suffix-variant fallback: a roster model that is NOT itself an
    # anchor-resolved model but is a lighter variant of a ranked base gets
    # max(0, base_rank - 1). Only applied when the model is not directly
    # anchor-resolved.
    base_models = set(ranks)
    for model in _iter_roster_models():
        norm = _normalize_model(model)
        if norm in ranks:
            continue  # directly anchor-resolved — heuristic does not apply
        for suffix in _VARIANT_SUFFIXES:
            if norm.endswith(suffix):
                base = norm[: -len(suffix)]
                if base in base_models:
                    ranks[norm] = max(0, ranks[base] - 1)
                    break
    return ranks


def local_ladder_models() -> Dict[int, str]:
    """Return ``{rank: model}`` for the local ladder (call-time).

    When multiple models share a rank (e.g. two anchors resolving to the
    same model, or a variant sharing its base's rank), the LAST one wins —
    matching the dict-insertion semantics of :func:`local_ladder`. This is
    the inverse map used to pick a promote/demote neighbor.
    """
    return {rank: model for model, rank in local_ladder().items()}


def role_primary_model(role: str) -> Optional[str]:
    """Return the PRIMARY model string configured for ``role``, or ``None``.

    Resolved live from ``delegation.model_by_role``. This is the model the
    role is pinned to run on — the anchor for top-of-ladder escalation.
    """
    return _role_primary_model(_read_model_by_role(), role)


def role_fallback_model(role: str) -> Optional[str]:
    """Return the nested ``fallback.model`` configured for ``role``, or ``None``.

    Resolved live from ``delegation.model_by_role``. Only a dict entry with
    a nested ``fallback`` dict carrying a usable ``model`` contributes.
    """
    return _role_fallback_model(_read_model_by_role(), role)


__all__ = [
    "LAST_KNOWN_GOOD_TIERS",
    "ROLE_TIER_GROUPS",
    "TIER_NAMES",
    "family_of",
    "local_ladder",
    "local_ladder_models",
    "rank_tier_map",
    "resolve_tier_model",
    "role_fallback_model",
    "role_primary_model",
    "tier_rank_map",
]
