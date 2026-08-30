"""Per-role delegation stats — telemetry for ruflo-persona subagents.

Tracks each ``delegate_task`` child's outcome so the user can later see
which roles are over/under-tuned for the model they're pinned to.
Read-only by default: nothing here changes runtime behaviour. The
``/delegation stats`` slash command surfaces aggregations on demand.

Storage: ``~/.hermes/delegation_stats.json`` (or ``$HERMES_HOME/...``).
A list of records, append-only. File-locked writes via best-effort
fcntl on POSIX; we never block the parent on contention — if the lock
isn't free, we drop the record and log debug.

Schema is forward-compatible: readers ignore unknown keys, defaults
fill in for missing keys. Records can grow over time without breaking
old aggregations.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# Cap the on-disk record count so the file doesn't grow forever. ~10k
# records is well under 5 MB and covers years of normal usage. When we
# exceed the cap, we drop the OLDEST records (FIFO) so recent data
# survives. Set to 0 (or env HERMES_DELEGATION_STATS_DISABLED=1) to
# disable tracking entirely.
_MAX_RECORDS = 10_000


def _stats_path() -> Path:
    """Resolve the delegation stats file location.

    Honors HERMES_HOME for tests / sandboxes. Falls back to ``~/.hermes/``.
    """
    home_env = os.environ.get("HERMES_HOME")
    home = home_env or os.path.expanduser("~/.hermes")
    return Path(home) / "delegation_stats.json"


def _is_disabled() -> bool:
    val = os.environ.get("HERMES_DELEGATION_STATS_DISABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


@dataclass
class DelegationStat:
    """One row of telemetry for a single delegate_task child completion.

    All fields are best-effort — missing values default to 0/empty so
    aggregation never crashes on partial records (e.g. when ACP children
    don't expose token counts the same way).
    """

    role: str = ""              # ruflo agent_type passed to delegate_task
    model: str = ""             # model the child actually ran on
    status: str = ""            # "completed" | "failed" | "interrupted" | "error"
    exit_reason: str = ""       # "completed" | "max_iterations" | "interrupted"
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    api_calls: int = 0
    max_iterations: int = 0
    hit_max_iter: bool = False  # True when api_calls >= max_iterations
    ts: float = field(default_factory=time.time)  # unix epoch seconds


def record(stat: DelegationStat) -> bool:
    """Append a stat record to the on-disk store.

    Returns True on success, False on any I/O / serialization failure
    (including disabled-via-env). Never raises.

    Best-effort: parses the existing file even if some records are
    malformed; corrupt records are dropped from the rewrite. If the
    file is fully unreadable, we still try to write a fresh one
    containing just this record.
    """
    if _is_disabled():
        return False
    try:
        path = _stats_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    # Filter out obviously-bad entries so a partial write
                    # from an older version doesn't corrupt aggregations.
                    existing = [r for r in raw if isinstance(r, dict)]
            except (OSError, json.JSONDecodeError):
                # Corrupt file: start fresh so we don't lose the new record.
                logger.debug("delegation_stats.json unreadable; rewriting")
                existing = []
        existing.append(asdict(stat))
        # Cap retention — drop the oldest records first.
        if _MAX_RECORDS > 0 and len(existing) > _MAX_RECORDS:
            existing = existing[-_MAX_RECORDS:]
        # Atomic write: write to a temp sibling, then rename. Survives
        # process kills mid-write without corrupting the file.
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=0)
        tmp.replace(path)
        return True
    except Exception:
        logger.debug("delegation stats record failed", exc_info=True)
        return False


def load_all() -> list[DelegationStat]:
    """Return every stat record on disk, oldest first.

    Empty list when the file doesn't exist, is empty, or fails to parse
    cleanly. Unknown / future fields are silently dropped during the
    DelegationStat reconstruction.
    """
    path = _stats_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[DelegationStat] = []
    valid_fields = set(DelegationStat.__dataclass_fields__.keys())
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # Only pass fields we know about — keeps reconstruction stable
        # even when older or newer versions wrote different keys.
        kwargs = {k: v for k, v in entry.items() if k in valid_fields}
        try:
            out.append(DelegationStat(**kwargs))
        except (TypeError, ValueError):
            continue
    return out


@dataclass
class RoleAggregate:
    """Aggregated stats for a single (role, model) bucket."""

    role: str
    model: str
    n: int = 0
    n_completed: int = 0
    n_failed: int = 0
    n_interrupted: int = 0
    n_hit_max: int = 0
    total_duration: float = 0.0
    total_input: int = 0
    total_output: int = 0
    total_cost: float = 0.0

    @property
    def success_rate(self) -> float:
        return (self.n_completed / self.n) if self.n else 0.0

    @property
    def avg_duration(self) -> float:
        return (self.total_duration / self.n) if self.n else 0.0

    @property
    def avg_input(self) -> float:
        return (self.total_input / self.n) if self.n else 0.0

    @property
    def avg_output(self) -> float:
        return (self.total_output / self.n) if self.n else 0.0

    @property
    def hit_max_rate(self) -> float:
        return (self.n_hit_max / self.n) if self.n else 0.0


def aggregate(
    stats: Optional[Iterable[DelegationStat]] = None,
    *,
    since_ts: Optional[float] = None,
    role: Optional[str] = None,
) -> list[RoleAggregate]:
    """Group stats by (role, model) and return aggregates.

    Args:
        stats: Iterable of records. Defaults to :func:`load_all`.
        since_ts: If set, only include records with ts >= this value.
        role: If set, only include records matching this role.

    Returns:
        List of :class:`RoleAggregate`, sorted by total spend descending
        (most expensive role-model pair first — the actionable one).
    """
    if stats is None:
        stats = load_all()
    buckets: dict[tuple[str, str], RoleAggregate] = {}
    for s in stats:
        if since_ts is not None and s.ts < since_ts:
            continue
        if role is not None and s.role != role:
            continue
        if not s.role:
            # Untagged delegation (no agent_type) — track separately so
            # users can see how much of their spend is "untagged free-form".
            key = ("(untagged)", s.model or "?")
        else:
            key = (s.role, s.model or "?")
        agg = buckets.get(key)
        if agg is None:
            agg = RoleAggregate(role=key[0], model=key[1])
            buckets[key] = agg
        agg.n += 1
        agg.total_duration += s.duration_seconds
        agg.total_input += s.input_tokens
        agg.total_output += s.output_tokens
        agg.total_cost += s.cost_usd
        if s.status == "completed":
            agg.n_completed += 1
        elif s.status == "failed" or s.status == "error":
            agg.n_failed += 1
        elif s.status == "interrupted":
            agg.n_interrupted += 1
        if s.hit_max_iter:
            agg.n_hit_max += 1
    return sorted(buckets.values(), key=lambda a: a.total_cost, reverse=True)


# ── Suggestion engine ─────────────────────────────────────────────────────
#
# Lightweight heuristics for "this role's metrics suggest a different model".
# Surfaces at /delegation stats --suggest. Never auto-applies.
#
# The tier anchors (haiku/sonnet/opus) are NOT hardcoded here. They come
# from :mod:`hermes_cli.model_tiers`, the single source of truth shared with
# :mod:`hermes_cli.persona_library`. The rank maps are built at CALL TIME
# inside :func:`suggest_retunes` from the live delegation config roster, so
# suggestions fire for roster-current models instead of silently skipping
# them the moment the roster moves to a new generation.
#
# Two independent ladders feed the engine:
#   * the Anthropic ladder (haiku < sonnet < opus) — :func:`tier_rank_map`;
#   * the local ladder (non-Anthropic models, e.g. the ollama-cloud roster)
#     — :func:`local_ladder`, ranked from explicit role anchors.
# A model belongs to at most one ladder. Promote/demote move within the
# SAME ladder; a top-of-local-ladder model that still fails escalates to the
# role's configured fallback (availability-failover, distinct from an
# in-ladder capability promote).


@dataclass
class Suggestion:
    role: str
    current_model: str
    suggested_model: str
    direction: str  # "promote" | "demote" | "escalate"
    reason: str


def _next_rank(ranks: list[int], rank: int) -> Optional[int]:
    """Return the next rank strictly greater than ``rank``, or ``None``.

    ``ranks`` must be sorted ascending. ``None`` means ``rank`` is the top
    of its ladder (no in-ladder promote target).
    """
    for r in ranks:
        if r > rank:
            return r
    return None


def _prev_rank(ranks: list[int], rank: int) -> Optional[int]:
    """Return the previous rank strictly less than ``rank``, or ``None``.

    ``ranks`` must be sorted ascending. ``None`` means ``rank`` is the
    bottom of its ladder (no in-ladder demote target).
    """
    for r in reversed(ranks):
        if r < rank:
            return r
    return None


def suggest_retunes(
    aggs: Iterable[RoleAggregate],
    *,
    min_samples: int = 5,
) -> list[Suggestion]:
    """Heuristic re-tune suggestions based on observed metrics.

    Rules (only fire with at least ``min_samples`` runs):
      - Promote (Haiku→Sonnet, Sonnet→Opus; and within the local ladder)
        when:
        * hit_max_rate >= 0.30 — frequently running out of iterations
        * success_rate < 0.80 — failing too often
      - Demote (Opus→Sonnet, Sonnet→Haiku; and within the local ladder)
        when:
        * success_rate >= 0.95 AND avg_output < 1500 tok AND
          hit_max_rate == 0 — boring fast work that doesn't need the
          extra capability
        * total_cost > $1.00 cumulative AND avg_output < 800 — high spend
          on what looks like trivial output
      - Escalate (top of the LOCAL ladder only) when a promote condition
        fires but there is no in-ladder promote target: suggest the role's
        CONFIGURED fallback model. Three guards: (a) the aggregate's model
        must equal that role's ``model_by_role`` PRIMARY model (never a
        coincidental same-model different-role); (b) the role's entry must
        carry a nested ``fallback`` dict with a model; (c) the suggestion is
        labeled ``direction="escalate"`` (not "promote") and the reason
        names the role's configured fallback. This keeps availability-
        failover semantics distinct from in-ladder capability promotes.

    These thresholds are intentionally conservative. Users see the
    suggestion and decide; nothing changes automatically.

    The rank maps are resolved fresh on every call from the live
    delegation config roster (see :mod:`hermes_cli.model_tiers`), so a
    role whose model is the roster-current haiku/sonnet/opus — or a
    roster-current local-ladder model — is always recognized, never
    silently skipped because a hardcoded tier map went stale.
    """
    from hermes_cli.model_tiers import (
        local_ladder,
        local_ladder_models,
        rank_tier_map,
        role_fallback_model,
        role_primary_model,
        tier_rank_map,
    )

    tier_rank = tier_rank_map()
    rank_tier = rank_tier_map()
    local_rank = local_ladder()
    local_rank_models = local_ladder_models()
    local_ranks = sorted(local_rank_models)
    out: list[Suggestion] = []
    for agg in aggs:
        if agg.n < min_samples:
            continue
        if agg.role == "(untagged)":
            continue

        # Resolve which ladder this model belongs to and its rank. A model
        # in neither ladder is unranked and silently skipped (unchanged).
        if agg.model in tier_rank:
            ladder = "anthropic"
            rank = tier_rank[agg.model]
            rank_models = rank_tier
            ranks = list(range(len(rank_tier)))
        elif agg.model in local_rank:
            ladder = "local"
            rank = local_rank[agg.model]
            rank_models = local_rank_models
            ranks = local_ranks
        else:
            continue

        # Promotion rules — move to the next entry in the SAME ladder.
        promote_target = _next_rank(ranks, rank)
        if promote_target is not None:
            if agg.hit_max_rate >= 0.30:
                out.append(
                    Suggestion(
                        role=agg.role,
                        current_model=agg.model,
                        suggested_model=rank_models[promote_target],
                        direction="promote",
                        reason=(
                            f"hit max_iterations on {agg.n_hit_max}/{agg.n} "
                            f"runs ({agg.hit_max_rate:.0%}) — "
                            f"likely under-modeled"
                        ),
                    )
                )
                continue
            if agg.success_rate < 0.80:
                out.append(
                    Suggestion(
                        role=agg.role,
                        current_model=agg.model,
                        suggested_model=rank_models[promote_target],
                        direction="promote",
                        reason=(
                            f"only {agg.n_completed}/{agg.n} completed "
                            f"({agg.success_rate:.0%}) — "
                            f"likely under-modeled"
                        ),
                    )
                )
                continue

        # Top-of-local-ladder escalation: a promote condition fired but
        # there is no in-ladder target. Escalate to the role's configured
        # fallback (availability-failover), subject to the three guards.
        elif ladder == "local":
            if agg.hit_max_rate >= 0.30 or agg.success_rate < 0.80:
                if role_primary_model(agg.role) == agg.model:
                    fb = role_fallback_model(agg.role)
                    if fb is not None:
                        if agg.hit_max_rate >= 0.30:
                            reason = (
                                f"hit max_iterations on {agg.n_hit_max}/"
                                f"{agg.n} runs ({agg.hit_max_rate:.0%}) — "
                                f"{agg.model} is the top of the local "
                                f"ladder; configured fallback for role "
                                f"'{agg.role}' is {fb}"
                            )
                        else:
                            reason = (
                                f"only {agg.n_completed}/{agg.n} completed "
                                f"({agg.success_rate:.0%}) — {agg.model} is "
                                f"the top of the local ladder; configured "
                                f"fallback for role '{agg.role}' is {fb}"
                            )
                        out.append(
                            Suggestion(
                                role=agg.role,
                                current_model=agg.model,
                                suggested_model=fb,
                                direction="escalate",
                                reason=reason,
                            )
                        )
                        continue

        # Demotion rules — move to the previous entry in the SAME ladder.
        demote_target = _prev_rank(ranks, rank)
        if demote_target is not None:
            cheap_and_clean = (
                agg.success_rate >= 0.95
                and agg.avg_output < 1500
                and agg.n_hit_max == 0
            )
            expensive_for_size = (
                agg.total_cost > 1.00 and agg.avg_output < 800
            )
            if cheap_and_clean or expensive_for_size:
                out.append(
                    Suggestion(
                        role=agg.role,
                        current_model=agg.model,
                        suggested_model=rank_models[demote_target],
                        direction="demote",
                        reason=(
                            f"{agg.success_rate:.0%} success, avg "
                            f"{agg.avg_output:.0f} output tok, "
                            f"${agg.total_cost:.2f} total — "
                            f"likely over-modeled"
                        ),
                    )
                )
    return out
