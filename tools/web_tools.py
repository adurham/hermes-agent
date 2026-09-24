#!/usr/bin/env python3
"""Generic web_search / web_extract tools over pluggable backends.

Backend is selected during ``hermes tools`` (``web.backend`` in config.yaml; per
capability via ``web.search_backend`` / ``web.extract_backend``). Every vendor
implementation lives in ``plugins/web/<vendor>/provider.py`` and registers with
``agent.web_search_registry``; this module owns selection, safety gates,
caching, keyless rescue, and the truncate-and-store result pipeline.
Debug: ``WEB_TOOLS_DEBUG=true`` writes ``logs/web_tools_debug_<UUID>.json``.
"""

import json
import logging
import os
from typing import List, Any, Optional
# Per-vendor client cache slots; plugins read/write these via tools.web_tools (tests reset them to None).
_firecrawl_client = _firecrawl_client_config = _parallel_client = _async_parallel_client = _exa_client = None

from plugins.web.firecrawl.provider import _is_tool_gateway_ready, check_firecrawl_api_key
from tools.debug_helpers import DebugSession
from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER, read_selection, selection_exists
from tools.url_safety import async_is_safe_url
from tools.web_tools_rescue import _rescue_eligible, _rescue_search
from tools.web_tools_truncate import _effective_char_limit, _trim_results, _truncate_results, convert_base64_images_to_links
from tools.web_tools_extract import (
    _extract_safe_urls, _merge_in_order, _no_provider_error, _resolve_extract_provider, _result_entry,
    _strict_selection_error, _validate_extract_urls,
)

logger = logging.getLogger(__name__)


# ─── Backend Selection ────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """Resolve ``name`` via the config-aware env layer (``hermes config set`` values), then process env.

    Mirrors the SearXNG provider's ``_searxng_url()`` so that values set through Hermes' config/.env layer
    (``hermes config set``, ``hermes tools``) are honored here too — not just raw process-env exports.
    Without this, a config-only ``SEARXNG_URL`` (or any provider key) leaves the backend auto-detect cascade
    and ``check_web_api_key()`` blind to it. See #34290.
    """
    try:
        from hermes_cli.config import get_env_value
        val = get_env_value(name)
    except Exception:
        val = None
    return ((os.getenv(name, "") if val is None else val) or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))


def _load_web_config() -> dict:
    """Load the ``web:`` section from config.yaml; always a dict (a null section yields ``{}``)."""
    try:
        from hermes_cli.config import load_config
        return load_config().get("web") or {}
    except Exception:
        return {}


def _configured_backend(key: str = "backend") -> str:
    """Lower-cased, stripped ``web.<key>`` value ("" when unset/null)."""
    return (_load_web_config().get(key) or "").lower().strip()


def _registry_call(func_name: str, default, *args):
    """``agent.web_search_registry.<func_name>(*args)``, or *default* if it raised (registry never fatal)."""
    try:
        import agent.web_search_registry as registry_mod
        return getattr(registry_mod, func_name)(*args)
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry %s%r failed: %s", func_name, args, exc)
        return default


def _registered_web_provider(backend: str):
    """Plugin-registered web provider by name, or ``None``."""
    return _registry_call("get_provider", None, backend) if backend else None


def _list_registered_web_providers():
    """All plugin-registered web providers (empty list on failure)."""
    return _registry_call("list_providers", [])


def _probe(provider, method: str, context: str = "") -> Optional[bool]:
    """``bool(provider.<method>())``, or ``None`` if it raised (a broken provider is unavailable; *context* is
    appended to the debug log line, e.g. " during readiness check")."""
    try:
        return bool(getattr(provider, method)())
    except Exception as exc:  # noqa: BLE001 — a broken provider is "unavailable"
        name = getattr(provider, "name", provider)
        logger.debug("web provider %r.%s() raised%s: %s", name, method, context, exc)
        return None


def _get_backend() -> str:
    """Shared web backend name. A stored ``web.backend`` is returned as-is — no availability probe, no
    fallback — so a broken selection surfaces the vendor's honest error rather than silently rerouting.
    The managed ``use_gateway`` selection also resolves to firecrawl with no ladder. Autodetect runs
    whenever no SHARED web selection was ever stored: per-capability keys (``web.search_backend``,
    ``web.extract_backend``) name only their own capability and never reroute the other (#113017)."""
    configured = _configured_backend()
    if configured:
        # "nous" (managed subscription) is serviced by firecrawl, routed through the managed Tool Gateway.
        return "firecrawl" if configured == NOUS_MANAGED_PROVIDER else configured
    if read_selection("web") is not None:
        # Shared selection exists (use_gateway) but no shared name: firecrawl, no ladder.
        return "firecrawl"

    # Never-configured install. Explicit user credentials beat the managed-gateway probe (a Nous OAuth
    # token's tier may not grant web access; the gateway then fails at runtime with no fallback).
    # Free tiers trail paid.
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")), ("perplexity", _has_env("PERPLEXITY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")), ("keenable", _has_env("KEENABLE_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()), ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")), ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    # Plugin-contributed providers (built-ins are covered above); probe the held object directly.
    for provider in _list_registered_web_providers():
        if provider.name not in _LEGACY_WEB_BACKENDS and _probe(provider, "is_available"):
            return provider.name

    return _keyless_backend() or "firecrawl"  # default (backward compat)


def _keyless_backend() -> Optional[str]:
    """Keyless free-tier backend name, or None. Strictly the last autodetect rung so it never
    pre-empts a keyed backend. Discovery must run first: reachable from contexts that haven't
    loaded plugins (subprocess runs, delegate children)."""
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import _keyless_preference, _keyless_tier_enabled
        if _keyless_tier_enabled():
            for name in _keyless_preference():
                provider = _registered_web_provider(name)
                if provider is not None and _probe(provider, "is_keyless_available"):
                    return name
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("keyless fallback walk failed: %s", exc)
    return None


def _get_search_backend() -> str:
    """Backend for web_search: ``web.search_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _configured_backend("search_backend") or _get_backend()


def _get_search_chain() -> tuple[str, ...]:
    """Return the ordered web-search provider chain from config, if any.

    Reads ``web.search_chain`` from config.yaml. When set to a non-empty
    list of provider names, the web_search dispatcher walks them in order,
    falling through on 429 / rate-limit / failure to the next provider.
    When unset or empty, the dispatcher falls back to the single-provider
    path (``web.search_backend`` / ``web.backend`` / auto-detect).

    Provider names are normalized to lowercase and stripped. Unknown names
    are kept in the tuple so the dispatcher can log them as "not
    registered" rather than silently dropping them — matches the
    single-provider path's explicit-config-wins behavior of surfacing
    misconfiguration rather than hiding it.
    """
    raw = _load_web_config().get("search_chain")
    if not isinstance(raw, (list, tuple)):
        return ()
    names = [str(item).lower().strip() for item in raw if str(item).strip()]
    return tuple(names)


def _provider_failed(response_data: object) -> bool:
    """Return True when a provider response indicates a fallthrough-worthy failure.

    Triggers a chain fallthrough on:
      * ``success`` is False (any error — 429, network, parse, auth, etc.)
      * error string mentioning "429" / "rate limit" / "rate_limit" /
        "too many requests" (defensive: some providers return success=True
        with an error field; not all do, so check both)

    A response with ``success: True`` and a populated ``data.web`` list is
    always treated as a success — never falls through.
    """
    if not isinstance(response_data, dict):
        return True
    if response_data.get("success") is True:
        return False
    err = str(response_data.get("error") or "")
    return True


def _get_extract_backend() -> str:
    """Backend for web_extract: ``web.extract_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _configured_backend("extract_backend") or _get_backend()


def _ddgs_package_importable() -> bool:
    """ddgs is the only backend gated on package presence; single symbol so tests can patch it."""
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False


def _xai_available() -> bool:
    # Cheap probe only (env var OR auth.json OAuth): resolve_xai_http_credentials() may hit the network.
    try:
        from tools.xai_http import has_xai_credentials
        return has_xai_credentials()
    except Exception:
        return False


# Built-in backends -> cheap availability probes; any other name is a plugin provider resolved via the
# registry's ``is_available()``. Lambdas so test patches of module-level helpers (_ddgs_package_importable,
# check_firecrawl_api_key) are honored at call time. ``xai`` is probed via has_xai_credentials(), not a
# registered provider, though the registry's _LEGACY_PREFERENCE omits it — drop it if xai ever registers.
_BUILTIN_AVAILABILITY = {
    "exa": lambda: _has_env("EXA_API_KEY"),
    "parallel": lambda: _has_env("PARALLEL_API_KEY"),
    "keenable": lambda: _has_env("KEENABLE_API_KEY"),
    "firecrawl": lambda: check_firecrawl_api_key(),
    "tavily": lambda: _has_env("TAVILY_API_KEY")
    or any(_configured_backend(k) == "tavily" for k in ("backend", "search_backend", "extract_backend")),
    "perplexity": lambda: _has_env("PERPLEXITY_API_KEY"),
    "searxng": lambda: _has_env("SEARXNG_URL"),
    "brave-free": lambda: _has_env("BRAVE_SEARCH_API_KEY"),
    "ddgs": lambda: _ddgs_package_importable(),
    "xai": _xai_available,
}
_LEGACY_WEB_BACKENDS = frozenset(_BUILTIN_AVAILABILITY)


def _is_backend_available(backend: str) -> bool:
    """True when *backend* is usable — the single availability chokepoint. Non-legacy names delegate to the
    registered provider's ``is_available()`` (unregistered names fall through); built-ins use cheap probes.

    For plugin-registered backends (any name outside :data:`_LEGACY_WEB_BACKENDS`), availability is
    delegated to the provider's ``is_available()`` via the web_search_registry. This is the single
    chokepoint through which ``_get_backend``, ``_get_capability_backend``, and ``check_web_api_key`` all
    resolve availability — fixing custom-provider discovery for every caller at once (issues #28651, #31873,
    #32698). Built-in backends keep their cheap hardcoded probes below.
    """
    backend = (backend or "").lower().strip()
    provider = None if backend in _LEGACY_WEB_BACKENDS else _registered_web_provider(backend)
    if provider is not None:
        return _probe(provider, "is_available") or False
    probe = _BUILTIN_AVAILABILITY.get(backend)
    return probe() if probe else False


# ─── Firecrawl Client ──────────────────────────────────────────────────────── After PR #25182, the
# firecrawl client, lazy SDK proxy, dual-auth config resolution, response normalizers, and
# check_firecrawl_api_key() all live in plugins.web.firecrawl.provider.
def _web_requires_env() -> list[str]:
    """Tool-registry metadata env vars for the web backends. Gateway vars are always listed: gating them
    on ``managed_nous_tools_enabled()`` cost a synchronous portal HTTP refresh at every CLI startup.
    Contract: set var -> tool sees it; extras are harmless for the not-logged-in."""
    return [
        "EXA_API_KEY", "PARALLEL_API_KEY", "TAVILY_API_KEY", "PERPLEXITY_API_KEY", "KEENABLE_API_KEY", "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "TOOL_GATEWAY_DOMAIN", "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


# ─── Dispatch ─────────────────────────────────────────────────────────────────

# ─── Exa / Parallel inline helpers — moved into plugins ────────────────────── After PR #25182, the exa
# client + search/extract and parallel client + search/extract helpers all live in their respective plugins:
# - plugins/web/exa/provider.py - plugins/web/parallel/provider.py Both plugins register through
# agent.web_search_registry and the dispatchers in this file resolve them via get_active_*_provider().
def _ensure_web_plugins_loaded() -> None:
    """Idempotently run plugin discovery so the web registry is populated. Dispatch is reachable from contexts
    that never triggered discovery (subprocess agent runs, delegate children, scripts); without it a
    configured backend yields a misleading "No web ... provider" error.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel, tavily, firecrawl, keenable)
    registers itself via ``plugins/web/<vendor>/__init__.py`` during plugin discovery. Tool dispatch can be
    reached from contexts that haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the registry is empty and
    ``get_provider('firecrawl')`` returns ``None`` even when the user has ``web.extract_backend: firecrawl``
    configured and ``FIRECRAWL_API_KEY`` set. See #27580.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # Warning, not debug: a broken plugin import is otherwise invisible.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def _resolve_search_provider(name: str):
    """Resolve a provider name from the web search registry (None when absent).

    Imports lazily so the module can be imported without the registry populated.
    """
    from agent.web_search_registry import get_provider as _wsp_get_provider
    return _wsp_get_provider(name)


def _run_search_single(query: str, limit: int) -> dict:
    """Single-provider dispatch (the legacy path).

    Picks the configured/active provider, calls its search(), returns the response dict.

    Upstream's backend-selection diagnostics (strict selection error for a
    configured-but-unregistered backend, disabled-bundled-plugin detection), its one-shot
    keyless rescue, and its TTL memo + single-flight cache are all consumed HERE rather
    than at the ``web_search_tool`` call site. That placement is load-bearing: it keeps the
    fork's chain-vs-single dispatch a single monkeypatchable seam
    (tests/tools/test_web_search_chain.py) and keeps ``web.search_chain`` failover and the
    Anthropic-native swap firing. Do not inline this back into web_search_tool.
    """
    from agent.web_search_registry import get_active_search_provider
    from tools.tool_backend_helpers import selection_exists

    backend = _get_search_backend()
    provider = _resolve_search_provider(backend) if backend else None
    if provider is None or not provider.supports_search():
        if provider is None and backend and selection_exists("web"):
            # Strict selection: a stored-but-unregistered backend reports the real cause
            # (disabled bundled plugin, else the bad selection) instead of silently
            # switching to whatever the availability walk finds.
            return {"success": False, "error": _strict_selection_error("search", backend)}
        # Never-configured install: legacy availability-walked autodetect.
        provider = get_active_search_provider()

    if provider is None:
        fallback = "No web search provider configured. Run `hermes tools` to set one up."
        return {"success": False, "error": _no_provider_error("search", fallback)}

    logger.info("Web search via %s: '%s' (limit: %d)", provider.name, query, limit)
    return _memoized_search(provider, query, limit)


def _run_search_chain(chain: tuple[str, ...], query: str, limit: int) -> dict:
    """Walk the configured search provider chain with failover.

    For each provider name in *chain*:
      1. Resolve it from the registry. If not registered, log a warning and skip
         (treat as a failure, fall through).
      2. If it doesn't ``supports_search()``, log and skip.
      3. If ``is_available()`` is False, log and skip.
      4. Call ``.search(query, limit)``. On a fallthrough-worthy failure
         (``_provider_failed``), log the error and continue to the next.
      5. On success, return the response dict immediately.

    If every provider fails, return the last failure's response dict (or a synthesized
    "all providers in chain failed" error if none even produced a response).
    """
    last_response: dict | None = None

    for name in chain:
        provider = _resolve_search_provider(name)
        if provider is None:
            logger.warning("web_search chain: '%s' not registered; skipping", name)
            last_response = {"success": False, "error": f"Provider '{name}' not registered"}
            continue
        if not provider.supports_search():
            logger.warning("web_search chain: '%s' does not support search; skipping", name)
            last_response = {"success": False, "error": f"Provider '{name}' does not support search"}
            continue
        try:
            available = provider.is_available()
        except Exception as exc:  # noqa: BLE001
            logger.warning("web_search chain: '%s'.is_available() raised %s; skipping", name, exc)
            last_response = {"success": False, "error": f"Provider '{name}' availability check failed: {exc}"}
            continue
        if not available:
            logger.info("web_search chain: '%s' not available (missing credentials?); skipping", name)
            last_response = {"success": False, "error": f"Provider '{name}' not available"}
            continue

        logger.info("web_search chain: trying %s for '%s' (limit %d)", name, query, limit)
        try:
            response = provider.search(query, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("web_search chain: %s raised %s; falling through", name, exc)
            last_response = {"success": False, "error": f"Provider '{name}' raised: {exc}"}
            continue

        if _provider_failed(response):
            err = str(response.get("error") or "unknown error")
            logger.warning("web_search chain: %s failed (%s); falling through", name, err)
            last_response = response
            continue

        logger.info("web_search chain: %s succeeded for '%s'", name, query)
        return response

    if last_response is not None:
        return last_response
    return {
        "success": False,
        "error": "All providers in web.search_chain failed or were unavailable",
    }


def _finish_debug(call_name: str, debug_call_data: dict, error_msg: Optional[str] = None) -> Optional[str]:
    """Log the call into the debug session; with *error_msg*, record it and return its ``tool_error`` envelope."""
    if error_msg is not None:
        logger.debug("%s", error_msg)
        debug_call_data["error"] = error_msg
    _debug.log_call(call_name, debug_call_data)
    _debug.save()
    return None if error_msg is None else tool_error(error_msg)


def web_search_tool(query: str, limit: int = 5) -> str:
    """Search the web via the configured backend.

    Returns a JSON string ``{"success": bool, "data": {"web": [{"title", "url", "description", "position"},
    ...]}}`` (metadata only — use web_extract_tool for page content) or ``{"success": false, "error": ...}``.
    """
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 5
    debug_call_data = {
        "parameters": {"query": query, "limit": limit}, "error": None, "results_count": 0,
        "original_response_size": 0, "final_response_size": 0,
    }

    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)
        # Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        chain = _get_search_chain()
        if chain:
            # FORK: explicit failover chain — walk providers in order, fall through on
            # 429 / rate-limit / any failure. Returns the first successful response, or
            # the last error if all providers fail.
            response_data = _run_search_chain(chain, query, limit)
        else:
            # Single-provider dispatch. Upstream's backend-selection errors, keyless
            # rescue, and TTL result cache all live INSIDE _run_search_single so the
            # fork's chain-vs-single seam (tests/tools/test_web_search_chain.py) stays
            # monkeypatchable — do NOT hoist them back up to this call site.
            response_data = _run_search_single(query, limit)
        if not response_data.get("success") and response_data.get("error"):
            debug_call_data["error"] = response_data["error"]

        debug_call_data["results_count"] = len(response_data.get("data", {}).get("web", []))
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        _finish_debug("web_search_tool", debug_call_data)
        return result_json
    except Exception as e:
        return _finish_debug("web_search_tool", debug_call_data, f"Error searching web: {str(e)}")


def _memoized_search(provider, query: str, limit: int) -> dict:
    """TTL memo + single-flight around the paid vendor call (tools/web_result_cache.py); sits after every
    safety/config check. The provider is asked for the BUCKETED count so near-identical limits share an entry;
    the caller's count is sliced out. Only successful, non-rescued responses are cached — caching a rescue
    would make the one-shot ring fallback sticky for a whole TTL."""
    from tools.web_result_cache import bucket_limit, search_memo, slice_search_response

    def _paid_search() -> tuple[dict, bool]:
        fetch_limit = bucket_limit(limit)
        try:
            resp = provider.search(query, fetch_limit)
        except Exception as exc:  # noqa: BLE001 — candidate for rescue
            if not _rescue_eligible(provider):
                raise
            return _rescue_search(provider.name, str(exc), query, fetch_limit), True
        if not resp.get("success") and _rescue_eligible(provider):
            return _rescue_search(provider.name, str(resp.get("error", "")), query, fetch_limit), True
        return resp, False

    response_data = search_memo.lookup(provider.name, query, limit)
    if response_data is None:
        with search_memo.flight_lock(provider.name, query, limit):
            # Re-check inside the lock: a concurrent identical call may have stored.
            response_data = search_memo.lookup(provider.name, query, limit)
            if response_data is None:
                response_data, was_rescued = _paid_search()
                if not was_rescued:
                    search_memo.store(provider.name, query, limit, response_data)
    return slice_search_response(response_data, limit)


async def web_extract_tool(urls: List[Any], format: str = None, char_limit: Optional[int] = None) -> str:
    """Extract clean page content (no LLM) from URLs via the configured backend.

    Pages over ``char_limit`` (default web.extract_char_limit or 15000) are head+tail truncated with a footer
    pointing at the stored full text; inline base64 images become ``[IMAGE: alt]``. URLs carrying secrets are
    refused before any fetch; private-network URLs are blocked per entry. Returns JSON ``{"results": [...]}``.
    """
    normalized_urls, normalized_indices, invalid_urls, blocked = _validate_extract_urls(urls)
    if blocked is not None:
        return blocked
    debug_call_data = {
        "parameters": {"urls": normalized_urls, "format": format, "char_limit": char_limit}, "error": None,
        "pages_extracted": 0, "pages_truncated": 0, "original_response_size": 0, "final_response_size": 0,
        "truncation_metrics": [], "processing_applied": [],
    }

    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))
        # SSRF protection — filter private/internal URLs before any backend.
        safe_urls, safe_indices, ssrf_blocked = [], [], {}
        for index, url in zip(normalized_indices, normalized_urls):
            if await async_is_safe_url(url):
                safe_urls.append(url)
                safe_indices.append(index)
            else:
                ssrf_blocked[index] = _result_entry(
                    url, "Blocked: URL targets a private or internal network address"
                )

        results = []
        if safe_urls:
            backend = _get_extract_backend()
            _ensure_web_plugins_loaded()
            provider, error_json = _resolve_extract_provider(backend)
            if error_json is not None:
                return error_json
            results = await _extract_safe_urls(provider, safe_urls, format)
        # Reconstruct input order across invalid, blocked, and provider entries (providers preserve
        # the order of the safe URL list they receive).
        if invalid_urls or ssrf_blocked:
            fixed = {**ssrf_blocked, **invalid_urls}
            results = _merge_in_order(len(urls), fixed, safe_indices, safe_urls, results)

        logger.info("Extracted content from %d pages", len(results))
        debug_call_data["pages_extracted"] = len(results)
        debug_call_data["original_response_size"] = len(json.dumps({"results": results}))
        debug_call_data["processing_applied"].append("truncate_and_store")
        _truncate_results(results, _effective_char_limit(char_limit), debug_call_data)
        trimmed = _trim_results(results)
        result_json = (
            json.dumps({"results": trimmed}, indent=2, ensure_ascii=False) if trimmed
            else tool_error("Content was inaccessible or not found")
        )
        # Belt-and-suspenders sweep of the serialized JSON: a provider may tuck a base64 blob in metadata.
        cleaned_result = convert_base64_images_to_links(result_json)
        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_conversion")
        _finish_debug("web_extract_tool", debug_call_data)
        return cleaned_result
    except Exception as e:
        return _finish_debug("web_extract_tool", debug_call_data, f"Error extracting content: {str(e)}")


def _provider_is_ready(provider) -> bool:
    """True when *provider* is keyed-available OR keyless-capable, without raising.

    ``get_active_*_provider()`` returns an explicitly configured backend even when ``is_available()`` is
    False (so dispatch can emit a precise error), so readiness gates (tool check_fn, ``hermes doctor``)
    must probe for real. Keyless mode (Exa/Parallel free tier) is a working state, not a misconfig.

    See #78412.
    """
    if provider is None:
        return False
    ready = _probe(provider, "is_available", " during readiness check")
    if ready is None:  # broken provider == not ready; don't try the keyless probe
        return False
    return bool(ready or _probe(provider, "is_keyless_available", " during readiness check"))


# Credential probes that back other tools but serve no registered web backend: ``xai`` is
# probed via has_xai_credentials() for TTS/media only, so it must not light this gate. A
# stored ``web.backend: xai`` still counts since _get_backend returns a configured
# selection as-is and dispatch surfaces the honest "unknown provider" error.
_WEB_CHECK_SKIP = frozenset({"xai"})


def check_web_api_key() -> bool:
    """``check_fn`` gate for web_search / web_extract: is any web backend available?

    Anthropic native web_search (server-side) is also a valid backend — it requires no
    third-party key, only that we're running against a first-party Anthropic (Claude)
    endpoint. Detection is loose: any of the standard Anthropic credential paths counts.
    This function only gates whether the client `web_search` schema is exposed to the model
    at all; the actual native-vs-client swap happens in the adapter at request-build time —
    agent/fork/anthropic_native_web_search.apply_native_web_search() (called from
    anthropic_adapter.build_anthropic_kwargs) replaces the client tool entry with
    Anthropic's native web_search_20250305 server tool when the endpoint is first-party
    Anthropic. On non-Claude providers the client tool stays and dispatches to the
    configured backend as before.

    A plugin-registered provider reporting ``is_available()`` must light the tools up even with no
    built-in credentials; resolution funnels through :func:`_is_backend_available`.

    See #28651, #31873.
    """
    # An EXPLICITLY configured backend answers for itself, and nothing else may answer
    # for it (#78412). Its availability is returned directly — we do NOT fall through to
    # the other built-ins, the keyless ring, or the Anthropic-native probe below, because
    # any of those would paper over a broken explicit configuration and paint a green
    # check in `hermes doctor` for a backend that cannot actually run.
    #
    # Regression guard (v2026.9.14 merge): this stage was flattened into a boolean OR over
    # ``[configured] + _LEGACY_WEB_BACKENDS``, so `web.backend: parallel` with no
    # PARALLEL_API_KEY reported available as soon as ANY unrelated built-in was — e.g. a
    # managed-gateway firecrawl — silently masking the misconfiguration.
    configured = _configured_backend()
    if configured and (
        configured in _LEGACY_WEB_BACKENDS or _registered_web_provider(configured) is not None
    ):
        return _is_backend_available(configured)
    # No explicit config (or a name nothing recognizes): boolean OR over the built-ins —
    # probe order is irrelevant here. Non-legacy (plugin) names resolve through
    # _is_backend_available -> registry is_available(). ``xai`` is excluded: it backs
    # TTS/media, not a registered web backend (see _WEB_CHECK_SKIP).
    if any(_is_backend_available(backend) for backend in _LEGACY_WEB_BACKENDS if backend not in _WEB_CHECK_SKIP):
        return True
    # Plugin path. Discovery must run first: check_fn fires at tool-registration time, before any dispatch.
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import get_active_search_provider, get_active_extract_provider
        for provider in (get_active_search_provider(), get_active_extract_provider()):
            if provider is not None and getattr(provider, "name", None) in _WEB_CHECK_SKIP:
                # The registry's single-eligible / legacy walk picked a built-in that _get_backend
                # never autodetects (the explicit-config case was handled above): the dispatcher
                # would route to the keyless tier instead, so gate on exactly that.
                if _keyless_backend() is not None:
                    return True
                continue
            if _provider_is_ready(provider):
                return True
        # NOTE: upstream's stricter readiness predicate is kept, but as a fall-THROUGH rather
        # than its unconditional `return False`: the fork's Anthropic-native web-search path
        # below is a legitimate second source of availability, and returning False here would
        # make it dead code.
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry availability check failed: %s", exc)
    # Fall back to "Anthropic native available?" — credentials present
    # via env or Claude Code OAuth credentials file. Cheap probes only;
    # don't make network calls in a check_fn.
    if _has_env("ANTHROPIC_API_KEY") or _has_env("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    try:
        from pathlib import Path as _P
        if (_P.home() / ".claude" / ".credentials.json").exists():
            return True
    except Exception:
        pass
    return False


# ─── Registry ─────────────────────────────────────────────────────────────────
from tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    },
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns clean page content in markdown/text (no LLM summarization — fast). Also works with PDF URLs (arxiv papers, documents) — pass the PDF link directly. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and the read_file call to page through the omitted middle. Inline images appear as [IMAGE: alt] placeholders; real image URLs are kept as links. If a URL fails or times out, use the browser tool instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
            "char_limit": {
                "type": "integer",
                "description": "Optional per-page character budget sent back (default 15000). Pages larger than this are head+tail truncated with the full text stored to disk. Raise it when you need more of a long page inline.",
                "minimum": 2000
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search", toolset="web", schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key, requires_env=_web_requires_env(), emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract", toolset="web", schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [], "markdown",
        char_limit=args.get("char_limit"),
    ),
    check_fn=check_web_api_key, requires_env=_web_requires_env(), is_async=True, emoji="📄",
    max_result_size_chars=100_000,
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Dict  # noqa: F401,E402
from typing import TYPE_CHECKING  # noqa: F401,E402
import asyncio  # noqa: F401,E402
import httpx  # noqa: F401,E402
import re  # noqa: F401,E402
import sys  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_EXTRACT_CHAR_LIMIT': ('tools.web_tools_truncate', 'DEFAULT_EXTRACT_CHAR_LIMIT'),
    'Firecrawl': ('plugins.web.firecrawl.provider', 'Firecrawl'),
    'MAX_STORED_TEXT_CHARS': ('tools.web_tools_truncate', 'MAX_STORED_TEXT_CHARS'),
    'build_vendor_gateway_url': ('tools.managed_tool_gateway', 'build_vendor_gateway_url'),
    'managed_nous_tools_enabled': ('tools.tool_backend_helpers', 'managed_nous_tools_enabled'),
    'normalize_url_for_request': ('tools.url_safety', 'normalize_url_for_request'),
    'nous_tool_gateway_unavailable_message': ('tools.tool_backend_helpers', 'nous_tool_gateway_unavailable_message'),
    'prefers_gateway': ('tools.tool_backend_helpers', 'prefers_gateway'),
    'resolve_managed_tool_gateway': ('tools.managed_tool_gateway', 'resolve_managed_tool_gateway'),
    'sensitive_query_param_name': ('tools.url_safety', 'sensitive_query_param_name'),
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
