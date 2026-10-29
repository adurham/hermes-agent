"""Session-cumulative usage for a local viewer (wave A2 / CALLMAP W3).

Main's HermesCLI read its status bar (context, ``◷`` latency, ``t/s``, the spinner's ``↓ N tok``),
``/usage`` and the exit summary straight off the in-process agent's ``session_*`` counters,
``context_compressor`` and ``get_rate_limit_state()``. On the session authority the agent lives in
the gateway, so the same numbers are projected here: published as ``session.usage`` after every API
call and at turn end of a local-viewer turn, and returned by the ``session.stats`` verb.
"""
from __future__ import annotations

import contextlib
import dataclasses
import time

_COUNTERS = (("input_tokens", "session_input_tokens"), ("output_tokens", "session_output_tokens"),
             ("cache_read_tokens", "session_cache_read_tokens"),
             ("cache_write_tokens", "session_cache_write_tokens"),
             ("prompt_tokens", "session_prompt_tokens"), ("completion_tokens", "session_completion_tokens"),
             ("total_tokens", "session_total_tokens"), ("reasoning_tokens", "session_reasoning_tokens"),
             ("api_calls", "session_api_calls"))
# The output-token count at the start of the viewer's current turn (``↓ N tok`` on main's spinner).
TURN_BASE_ATTR = "_viewer_turn_output_base"
# The owner of the window-plan gauges this module projects: they exist only for the ``x-nous-credits-*``
# reply/account shape, so the allowance is Nous's no matter which session observed it — or whether that
# session has since been pointed at another provider.
GAUGE_PROVIDER = "nous"


def _finite(value):
    return round(float(value), 2) if value is not None and value == value and 0 < value < 1e6 else None


def _context(agent, compressor) -> dict:
    """Main's status-bar context figures (``_get_status_bar_snapshot``), clamped the same way."""
    if compressor is None:
        return {"last_prompt_tokens": 0, "context_tokens": 0, "context_length": None,
                "config_context_length": None, "context_source": None, "compression_count": 0}
    from agent.context_breakdown import context_display_source
    last = max(0, getattr(compressor, "last_prompt_tokens", 0) or 0)
    tokens = last
    with contextlib.suppress(Exception):
        from agent.usage_anchor import anchored_context_tokens
        messages = getattr(agent, "_session_messages", None)
        anchored = anchored_context_tokens(messages if isinstance(messages, list) else [],
                                           getattr(agent, "_turn_base_usage_anchor", None),
                                           charge_stale_thinking=False)
        if anchored is not None and anchored > 0:
            tokens = anchored
    length = max(0, getattr(compressor, "context_length", 0) or 0)
    return {"last_prompt_tokens": last, "context_tokens": min(tokens, length) if length else tokens,
            "context_length": length or None,
            "config_context_length": getattr(compressor, "_config_context_length", None),
            "context_source": context_display_source(compressor),
            "compression_count": getattr(compressor, "compression_count", 0) or 0}


def _rate_limit(agent):
    with contextlib.suppress(Exception):
        state = agent.get_rate_limit_state()
        if state is not None and dataclasses.is_dataclass(state) and not isinstance(state, type):
            return dataclasses.asdict(state)
    return None


def _gauge_payload(gauges) -> dict | None:
    """The one allowance to show: a full one first (it is what blocks), else today's, else the binding one.

    A gauge whose reset already passed is no longer current — the same rule the preflight applies.
    """
    from agent.usage_gauges import all_scope, binding_gauge

    gauges = tuple(gauges or ())
    binding = binding_gauge(gauges)
    day = next((g for g in gauges if g.id == "day" and g.scope == "all"), None)
    gauge = binding if binding is not None and binding.full else (day or binding)
    if gauge is None:
        return None
    reset = getattr(gauge, "resets_at_ms", None)
    if isinstance(reset, int) and 0 < reset <= int(time.time() * 1000):
        return None
    return {"id": gauge.id, "label": gauge.label, "used_bp": int(gauge.used_bp),
            "resets_at_ms": gauge.resets_at_ms, "provider": GAUGE_PROVIDER}


def _portal_window_gauges():
    """The account's own gauges, from the Portal snapshot a usage preflight keeps warm.

    A session that began on the fallback lane never saw an ``x-nous-credits-*`` reply, so
    ``_credits_state`` is empty and the status bar used to go blank exactly while the plan was
    spent. The Portal block carries the same numbers the headers do.
    """
    from hermes_cli.nous_account import cached_nous_portal_account_info

    window = getattr(cached_nous_portal_account_info(), "usage_window", None)
    if window is None:
        return ()
    from agent.credits_tracker import _window_seed_fields

    fields = _window_seed_fields(window)
    return tuple(fields.get("gauges") or ()) if fields else ()


def _answering_elsewhere(agent) -> bool:
    """True while this agent answers on some provider other than the plan's owner."""
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    return bool(provider) and provider != GAUGE_PROVIDER


def _fallback_bound(agent) -> bool:
    """True while a provider fallback (or a persisted fallback route) carries the turn."""
    return getattr(agent, "_fallback_activated", False) is True


def plan_gauge(agent) -> dict | None:
    """Window plans only: the allowance the status bar shows at a glance (today's, else the one that binds),
    from the reply headers the agent already tracks — no extra round trip, None for everyone else.

    The dict names the plan's owner (``provider``): these gauges come from the ``x-nous-credits-*``
    reply/account shape (``agent/credits_tracker.py`` communicates no other), so a status bar can
    label whose allowance the number is — and every session of that account agrees on it — instead
    of each viewer guessing from its own session's provider.

    A turn the preflight moved off a spent Nous plan carries no headers of its own — and a fallback
    bound into the session before a restart answers without ever setting ``_fallback_activated`` at
    all — so the account's Portal block stands in. Its readout appears while a fallback is bound, or
    whenever the plan reads spent on the session that is not on it: without it the number vanished at
    100%, the one moment it exists for.
    """
    with contextlib.suppress(Exception):
        state = getattr(agent, "_credits_state", None)
        if getattr(state, "is_usage_window", False) is True:  # `is True`: a Mock agent's attribute is not a plan
            if (payload := _gauge_payload(getattr(state, "gauges", ()))) is not None:
                return payload
        if _answering_elsewhere(agent):
            payload = _gauge_payload(_portal_window_gauges())
            if payload is not None and (_fallback_bound(agent) or payload.get("used_bp", 0) >= 10_000):
                return payload
    return None


def plan_route(agent, gauge: dict | None = None) -> dict | None:
    """The swap a status bar names while the turn runs off the plan (the contract's ``Route``).

    ``primary_*`` is the route this chat returns to. ``agent._primary_runtime`` carries it — except
    after a fallback was bound into the session and the gateway restarted: the session then *is* the
    backup, and the snapshot names the backup too. A swap that names no departure is no swap, so the
    plan's owner (``GAUGE_PROVIDER``) stands in: that IS the allowance the number belongs to.
    """
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    if not provider or provider == GAUGE_PROVIDER:
        return None
    gauge = gauge if gauge is not None else plan_gauge(agent)
    if gauge is None:
        return None
    primary = getattr(agent, "_primary_runtime", None)
    primary = primary if isinstance(primary, dict) else {}
    primary_provider = str(primary.get("provider") or "").strip().lower()
    primary_model = str(primary.get("model") or "").strip() or None
    if primary_provider and primary_provider != provider:
        return {"model": str(getattr(agent, "model", "") or ""), "provider": provider,
                "fallback_active": True, "primary_model": primary_model,
                "primary_provider": primary_provider}
    return {"model": str(getattr(agent, "model", "") or ""), "provider": provider,
            "fallback_active": True, "primary_model": primary_model if primary_provider else None,
            "primary_provider": GAUGE_PROVIDER}


def usage_stats(agent) -> dict:
    """The frozen ``session.usage`` / ``session.stats`` dict; zeros before the first turn built an agent."""
    stats: dict = {key: int(getattr(agent, attr, 0) or 0) for key, attr in _COUNTERS}
    stats.update(_context(agent, getattr(agent, "context_compressor", None)))
    avg_latency = tps = None
    with contextlib.suppress(Exception):
        latencies = list(getattr(agent, "_api_latency_history", None) or [])
        outputs = list(getattr(agent, "_api_output_history", None) or [])
        if n := min(len(latencies), len(outputs)):
            total = sum(latencies[-n:])
            avg_latency = _finite(total / n)
            tps = _finite(sum(outputs[-n:]) / total) if total > 0 else None
    active = 0
    with contextlib.suppress(Exception):
        from tools.async_delegation import active_count
        active = active_count()
    base = getattr(agent, TURN_BASE_ATTR, None)
    stats.update(avg_latency_s=avg_latency, tokens_per_s=tps,
                 last_turn_tokens=max(0, stats["output_tokens"] - base) if isinstance(base, int) else 0,
                 rate_limit=_rate_limit(agent) if agent is not None else None, active_subagents=active,
                 model=str(getattr(agent, "model", "") or ""), provider=str(getattr(agent, "provider", "") or ""),
                 fallback_model=(str(getattr(agent, "model", "") or "") or None)
                 if getattr(agent, "_fallback_activated", False) is True else None)
    if (gauge := plan_gauge(agent)) is not None:  # absent off a window plan, as on the embedded path
        stats["plan_gauge"] = gauge
    # A provider fallback carries the turn on another provider while the chat keeps its own route
    # (`agent.failover_state.resolve_effective_model`, the same wire shape the subagent rows use):
    # the status bar names the primary, its spent allowance and who is answering instead of showing
    # the backup as if the operator had chosen it.
    if (route := plan_route(agent, stats.get("plan_gauge"))) is not None:
        stats["route"] = route
    return stats


def carried_compression_count(live, agent=None) -> int:
    """Compactions of this conversation: main's agent counted them on its compressor for the
    session's life. A canonical compress evicts the gateway's agent (a session boundary), so the
    count rides the live session and seeds the successor (``seed_compression_count``)."""
    own = getattr(getattr(agent, "context_compressor", None), "compression_count", 0)
    carried = getattr(live, "compression_count", 0)
    return max(carried if isinstance(carried, int) else 0, own if isinstance(own, int) else 0)


def note_compression(authority, ref, live) -> None:
    """A committed (never an exact-retried) compress: one more compaction than the evicted agent saw."""
    agent = None
    with contextlib.suppress(AttributeError, KeyError):  # a runner without an agent cache has none
        agent = authority.agent(ref)
    live.compression_count = carried_compression_count(live, agent) + 1


def seed_compression_count(live, agent) -> None:
    compressor = getattr(agent, "context_compressor", None)
    if live is not None and compressor is not None:
        with contextlib.suppress(Exception):
            compressor.compression_count = carried_compression_count(live, agent)


def tui_usage(stats: dict) -> dict:
    """``stats`` in main's TUI ``Usage`` shape (``tui_gateway/server.py::_get_usage``): the fields its
    status bar (context used/max/bar, ``◷`` latency, ``t/s``, ``cmp`` badge, cache hit) and ``/usage``
    read off ``message.complete.usage``. Absent figures are omitted, never fabricated as zeros."""
    usage = {"model": stats.get("model") or "", "input": stats.get("input_tokens") or stats.get("prompt_tokens") or 0,
             "output": stats.get("output_tokens") or stats.get("completion_tokens") or 0,
             "reasoning": stats.get("reasoning_tokens") or 0, "prompt": stats.get("prompt_tokens") or 0,
             "completion": stats.get("completion_tokens") or 0, "total": stats.get("total_tokens") or 0,
             "calls": stats.get("api_calls") or 0}
    used, maximum = stats.get("last_prompt_tokens") or 0, stats.get("context_length") or 0
    if used and maximum:
        # ``agent/context_breakdown.py::context_usage_fields``: current occupancy, clamped.
        used = min(used, maximum)
        source = stats.get("context_source") or "provider_usage"
        usage.update(context_used=used, context_max=maximum,
                     context_percent=max(0, min(100, round(used / maximum * 100))),
                     context_source=source, context_estimated=source != "provider_usage")
    if stats.get("context_source") is not None:  # a compressor, sized or not (``_get_usage``'s ``if comp``)
        usage["compressions"] = stats.get("compression_count") or 0
    prompt_total, cache_read = stats.get("prompt_tokens") or 0, stats.get("cache_read_tokens") or 0
    if prompt_total > 0 and cache_read > 0:
        usage["cache_hit_pct"] = max(0, min(100, round(cache_read / prompt_total * 100)))
    for key, source_key in (("avg_latency_s", "avg_latency_s"), ("avg_tps", "tokens_per_s")):
        if isinstance(stats.get(source_key), (int, float)):
            usage[key] = round(float(stats[source_key]), 1)
    if isinstance(stats.get("active_subagents"), int):
        usage["active_subagents"] = stats["active_subagents"]
    if isinstance(stats.get("plan_gauge"), dict):
        usage["plan_gauge"] = stats["plan_gauge"]
    if isinstance(stats.get("route"), dict):
        usage["route"] = stats["route"]
    return usage


def usage_frame(stats: dict) -> dict:
    """The ``session.usage`` event payload: BOTH views of the same counters, in one frame.

    ``SessionUsagePayload`` (``tui_gateway/contracts/events.py``) allows either view — the TUI
    ``usage`` object (``server._start_usage_ticker``) or the session-cumulative counters at the top
    level — and the readers on the far end want different halves. The classic CLI
    (``hermes_cli.gateway_connection.SessionState.apply_event``) keeps the top-level counters
    whenever they are present and ignores the nested view; ACP (``acp_adapter.gateway_server``)
    reads only the top level; a Desktop bundle older than ``usageFromTick`` reads only
    ``payload.usage``, so its context / cache-hit / tokens-per-second meters render their empty
    placeholder for as long as the frame carries the counters alone. Publishing both serves every
    viewer from one frame, and leaves the counters untouched for every existing consumer.

    ``usage`` is the standard status-bar projection of the embedded ticker's ``server._get_usage``
    view, with two exceptions: ``dev_credits_spent_micros`` (``HERMES_DEV_CREDITS`` only) is not
    carried, and ``avg_latency_s`` / ``avg_tps`` are rounded from the counters' two decimals rather
    than the raw values, so they can differ by 0.1 at a rounding boundary. A Desktop bundle that
    reads only ``payload.usage`` fills its meters here too. The
    top-level counters on this event are deprecated: new viewers read ``usage`` (or call
    ``session.stats`` for the counters). Before they can be dropped, these readers must move off
    them: the classic CLI's ``SessionState.apply_event`` → ``GatewayAgent.apply_session_usage``,
    ACP's ``_project_usage`` (``usage_update`` / ``turn_usage``), and the flat branch of
    ``usageFromTick`` (Desktop and Ink).
    """
    return {"usage": tui_usage(stats), **stats}


def publish_usage(turn, agent, *, record: bool = False) -> None:
    """One ``session.usage`` frame on the viewer's stream; never breaks the turn. At turn end
    (``record``) the same dict rides the captured execution result, so the turn's
    ``message.complete`` carries main's ``usage`` (``session_settlement_recovery.completion_payload``)."""
    if agent is None:
        return
    with contextlib.suppress(Exception):
        stats = usage_stats(agent)
        turn._publish_execution("session.usage", usage_frame(stats))
        if record:
            from gateway.session_results import execution_result
            captured = execution_result.get()
            if captured is not None:
                captured["session_usage"] = stats


async def stats(connection, ref, params):
    """``session.stats {session_id}``: the same dict ``session.usage`` carries."""
    from hermes_state_runtime import RuntimeStoreError
    if set(params) - {"session_id", "profile"}:
        raise RuntimeStoreError("invalid_params")
    await connection.authority.resolve(connection.actor, ref)
    agent = connection.authority.agent(ref)
    result = usage_stats(agent)
    result["compression_count"] = carried_compression_count(connection.authority.sessions.get(ref.session_id), agent)
    return result
