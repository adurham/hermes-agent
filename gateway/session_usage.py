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
import threading
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


_PORTAL_WARM_LOCK = threading.Lock()
_portal_warm_at = 0.0
_PORTAL_WARM_INTERVAL = 120.0


def _warm_portal_snapshot() -> None:
    """Ask the Portal for the account block once, off the usage tick.

    A session answering on the fallback never runs a usage preflight, which is the only thing that
    fills ``nous_account``'s 60 s snapshot cache — so the bar had no number to show, and would have
    kept none forever. Only ``force_fresh`` actually calls ``/api/oauth/account`` (the cached path
    rebuilds from the JWT, which carries no window), so this is the preflight's own call, kept on a
    daemon thread: a network round trip must not sit inside the usage tick.
    ``_PORTAL_WARM_INTERVAL`` bounds the chatter — a spent window barely moves, and the reset edge is
    caught within two minutes.
    """
    global _portal_warm_at
    now = time.monotonic()
    with _PORTAL_WARM_LOCK:
        if now - _portal_warm_at < _PORTAL_WARM_INTERVAL:
            return
        _portal_warm_at = now

    def _fetch() -> None:
        with contextlib.suppress(Exception):
            from hermes_cli.nous_account import get_nous_portal_account_info
            get_nous_portal_account_info(force_fresh=True)

    threading.Thread(target=_fetch, name='portal-usage-warm', daemon=True).start()


def _portal_window_gauges():
    """The account's own gauges, from the Portal snapshot a usage preflight keeps warm.

    A session that began on the fallback lane never saw an ``x-nous-credits-*`` reply, so
    ``_credits_state`` is empty and the status bar used to go blank exactly while the plan was
    spent. The Portal block carries the same numbers the headers do.
    """
    from hermes_cli.nous_account import cached_nous_portal_account_info

    info = cached_nous_portal_account_info()
    if info is None:
        # Stale beats blank: keep the last number up while a refresh is in flight.
        info = cached_nous_portal_account_info(any_age=True)
        _warm_portal_snapshot()
        if info is None:
            return ()
    window = getattr(info, "usage_window", None)
    if window is None:
        return ()
    from agent.credits_tracker import _window_seed_fields

    fields = _window_seed_fields(window)
    return tuple(fields.get("gauges") or ()) if fields else ()


def _fallback_bound(agent) -> bool:
    """True while a provider fallback (or a persisted fallback route) carries the turn."""
    return getattr(agent, "_fallback_activated", False) is True


def _provider_of(agent) -> str:
    return str(getattr(agent, "provider", "") or "").strip().lower()


def _primary_provider(agent) -> str:
    primary = getattr(agent, "_primary_runtime", None)
    primary = primary if isinstance(primary, dict) else {}
    return str(primary.get("provider") or "").strip().lower()


def _gauge_owner(agent) -> str:
    """Whose allowance the status bar shows: the chat's OWN route, not merely who answered.

    While a fallback carries the turn the chat keeps its route in ``_primary_runtime`` (the next
    turn restores it), so the number belongs to that primary. Without a fallback the chat's route
    is the provider that is answering — a deliberate ``switch_model`` moves ``_primary_runtime``
    with it — so the previous provider's allowance must not linger on the bar. That distinction is
    the whole bug: a spent plan used to be read as proof of a fallback, which painted "Nous Portal
    → <provider>" on a session the operator had simply switched away.
    """
    provider = _provider_of(agent)
    if _fallback_bound(agent):
        primary = _primary_provider(agent)
        if primary and primary != provider:
            return primary
    return provider


# Account-usage window labels → the ids each surface knows (desktop/i18n and the TUI render the
# client words for these; any other id shows the window's own label).
_WINDOW_GAUGE_IDS = {
    "current session": "session", "session": "session",
    "current week": "week", "weekly": "week", "week": "week",
    "today": "day", "daily": "day", "day": "day",
    "monthly": "month", "month": "month",
    "large models (monthly)": "top_tier",
}


def _window_gauge_id(label) -> str:
    return _WINDOW_GAUGE_IDS.get(str(label or "").strip().lower(), "window")


def _account_window_gauge(provider: str) -> dict | None:
    """The answering provider's own subscription allowance, from the account-usage cache.

    The cache is what the model picker and ``/usage`` fill; this only reads it and asks for a
    background refresh when the snapshot is missing or stale. Never fetch inline: this runs on
    every usage tick, and the providers' usage APIs are rate-limited (the plugin hook that owns
    Claude's endpoint honours its ``retry-after`` on its own).

    Only account-scoped windows are eligible: a model-scoped weekly cap (Anthropic's Opus/Sonnet
    weeks) says nothing about the account, so showing it as the session's one allowance would
    claim a limit that does not block the model in use.
    """
    from agent.account_usage_cache import cached_account_usage, refresh_account_usage_async, snapshot_is_stale

    snapshot = cached_account_usage(provider)
    if snapshot is None or snapshot_is_stale(snapshot):
        refresh_account_usage_async([provider])
    if snapshot is None or snapshot_is_stale(snapshot):
        return None
    now_ms = int(time.time() * 1000)
    live: list[tuple[object, int | None, float]] = []
    for window in snapshot.windows:
        used = getattr(window, "used_percent", None)
        if getattr(window, "scope", "account") != "account" or used is None:
            continue
        reset = getattr(window, "reset_at", None)
        reset_ms = int(reset.timestamp() * 1000) if reset is not None else None
        if reset_ms is not None and reset_ms <= now_ms:
            continue  # the window it describes already rolled over
        live.append((window, reset_ms, float(used)))
    if not live:
        return None
    full = [row for row in live if row[2] >= 100.0]
    # A full window is what blocks, and of several the one that frees up last (same rule as
    # ``usage_gauges.binding_gauge``); otherwise the fullest is the one worth showing.
    window, reset_ms, used = (max(full, key=lambda row: row[1] or 0) if full
                              else max(live, key=lambda row: row[2]))
    return {"id": _window_gauge_id(getattr(window, "label", "")), "label": getattr(window, "label", ""),
            "used_bp": max(0, min(10_000, round(used * 100))), "resets_at_ms": reset_ms,
            "provider": provider}


def plan_gauge(agent) -> dict | None:
    """The allowance the status bar shows at a glance, from the route the chat is actually on.

    On a Nous window plan the gauge comes from the ``x-nous-credits-*`` reply headers the agent
    already tracks; a turn a fallback carries carries no headers of its own, so the account's
    Portal block stands in (the number must not vanish at 100%, the one moment it exists for).
    On any other provider the gauge is that provider's own subscription windows (Claude 5h/week,
    Codex session/weekly, ...), from the account-usage cache — never Nous's, which is not in the
    mix for a session the operator moved away.

    The dict names the plan's owner (``provider``) so a status bar can label whose allowance the
    number is — and every session of that account agrees on it.
    """
    with contextlib.suppress(Exception):
        owner = _gauge_owner(agent)
        if not owner:
            return None
        if owner == GAUGE_PROVIDER:
            state = getattr(agent, "_credits_state", None)
            if getattr(state, "is_usage_window", False) is True:  # `is True`: a Mock agent's attribute is not a plan
                if (payload := _gauge_payload(getattr(state, "gauges", ()))) is not None:
                    return payload
            # A swap away from the plan never saw its own reply headers. The Portal block is the
            # account's, and only a fallback may put another provider's turn under it.
            if _fallback_bound(agent) and _provider_of(agent) != GAUGE_PROVIDER:
                return _gauge_payload(_portal_window_gauges())
            return None
        return _account_window_gauge(owner)
    return None


def plan_route(agent, gauge: dict | None = None) -> dict | None:
    """The swap a status bar names while a fallback carries the turn (the contract's ``Route``).

    ``primary_*`` is the route this chat returns to. Present ONLY while a fallback is actually
    active: a session the operator switched to another provider is on that provider by choice,
    and calling it a fallback from the plan owner would be a fabricated departure. The previous
    implementation inferred one from a spent plan, which put "Nous Portal → Claude" on a manual
    switch — the chat's own route had already moved, so nothing had been left behind.
    """
    provider = _provider_of(agent)
    if not provider or not _fallback_bound(agent):
        return None
    primary_provider = _primary_provider(agent)
    if not primary_provider or primary_provider == provider:
        return None
    gauge = gauge if gauge is not None else plan_gauge(agent)
    if gauge is None:
        return None
    primary = getattr(agent, "_primary_runtime", None)
    primary = primary if isinstance(primary, dict) else {}
    primary_model = str(primary.get("model") or "").strip() or None
    return {"model": str(getattr(agent, "model", "") or ""), "provider": provider,
            "fallback_active": True, "primary_model": primary_model,
            "primary_provider": primary_provider}


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


_STORED_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def _stored_stats(row: dict) -> dict:
    """A session row's cumulative counters in ``usage_stats``' shape. The row's ``input_tokens`` excludes cache
    reads and writes, so the prompt total adds them back for the cache hit rate."""
    counts = {key: int(row.get(key) or 0) for key in _STORED_COUNTERS}
    return {**counts, "prompt_tokens": counts["input_tokens"] + counts["cache_read_tokens"] + counts["cache_write_tokens"],
            "completion_tokens": counts["output_tokens"], "total_tokens": counts["input_tokens"] + counts["output_tokens"],
            "api_calls": int(row.get("api_call_count") or 0), "model": row.get("model") or ""}


def snapshot_usage(authority, ref, agent) -> dict | None:
    """The status-bar ``usage`` a resume / ``session.info`` snapshot carries, so a viewer switching to the
    session paints its own cache hit and ``t/s`` instead of the last-viewed session's. An agent that has
    called the model answers as its ``session.usage`` frames do; otherwise (no agent since a restart, or one
    not used yet) the stored row answers, with no ``t/s`` since latency is not stored."""
    if agent is not None and getattr(agent, "session_api_calls", 0):
        result = usage_stats(agent)
        result["compression_count"] = carried_compression_count(authority.sessions.get(ref.session_id), agent)
        return tui_usage(result)
    row = authority.db.get_session(ref.session_id)
    return tui_usage(_stored_stats(row)) if row else None


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
