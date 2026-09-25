"""Anthropic Messages API adapter: client construction + the Messages call for Hermes's
OpenAI-style internals. Auth: API keys (``sk-ant-api*``) -> x-api-key; OAuth setup-tokens
(``sk-ant-oat*``) and Claude Code credentials -> Bearer + beta header. Endpoint predicates,
payload conversion and credentials live in ``agent/anthropic_{endpoints,message_convert,
credentials}.py``; import them from there.

FORK: targets ``client.beta.messages.{create,stream}`` (anthropic SDK 0.100+) — the beta
namespace exposes typed kwargs for ``thinking``, ``output_config``, ``context_management``,
``betas``, ``speed`` and ``metadata``. Wire shape mirrors Claude Code 2.1.119 (mitmdump
capture 2026-05-06): same betas, same body field set.
"""

import copy
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from utils import base_url_host_matches, normalize_proxy_env_vars
from hermes_constants import get_hermes_home

from agent.anthropic_credentials import (  # noqa: F401 — compat re-export surface for tests/plugins
    _OAUTH_CLIENT_ID,
    _OAUTH_REDIRECT_URI,
    _OAUTH_SCOPES,
    _OAUTH_TOKEN_URLS,
    _OAUTH_TOKEN_USER_AGENT,
    CredentialPersistError,
    _generate_pkce,
    _get_hermes_oauth_file,
    _getenv,
    _is_oauth_token,
    _list_claude_code_keychain_service_candidates,
    _pin_static_anthropic_token,
    _prefer_refreshable_claude_code_token,
    _read_claude_code_credentials_from_file,
    _read_claude_code_credentials_from_keychain,
    _refresh_oauth_token,
    _resolve_anthropic_pool_token,
    _resolve_claude_code_token_from_credentials,
    _sync_claude_code_credentials_to_keychain,
    _write_claude_code_credentials,
    _write_hermes_oauth_credentials,
    claude_code_credentials_path,
    is_claude_code_token_valid,
    is_rotation_consumed_uncommitted,
    mark_rotation_consumed_uncommitted,
    read_claude_code_credentials,
    read_hermes_oauth_credentials,
    refresh_anthropic_oauth_pure,
    resolve_anthropic_token,
    run_hermes_oauth_login_pure,
    run_oauth_setup_token,
)
from agent.anthropic_endpoints import (
    _is_azure_anthropic_endpoint, _is_kimi_coding_endpoint,
    _is_minimax_anthropic_endpoint, _is_nous_portal_endpoint, _is_opencode_endpoint,
    _is_third_party_anthropic_endpoint, _model_name_is_kimi_family, _normalize_base_url_text,
    _requires_bearer_auth,
)
from agent.anthropic_message_convert import (
    convert_messages_to_anthropic, convert_tools_to_anthropic, normalize_model_name,
)

from hermes_cli import __version__ as _HERMES_VERSION


# ``import anthropic`` is deliberately NOT at module top: the SDK costs ~220 ms of imports and
# every usage site is a cold user-triggered path. ``...`` = not yet tried; None = tried, missing.
_anthropic_sdk: Any = ...

# FORK: beta-only typed kwargs that only work on client.beta.messages.*
# (Anthropic SDK 0.100+). The fork's Claude-Code-mimicry path attaches
# these as typed body kwargs; when the client doesn't have a .beta namespace
# they must be stripped before dispatching to .messages.*.
_BETA_ONLY_KWARGS = frozenset({"context_management", "output_config", "speed", "betas"})


def _get_anthropic_sdk():
    """Return the ``anthropic`` SDK module, importing lazily. None if not installed."""
    global _anthropic_sdk
    if _anthropic_sdk is ...:
        with suppress(Exception):  # ImportError or FeatureUnavailable — fall through to the import below
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("provider.anthropic", prompt=False)
        try:
            import anthropic as _sdk
            _anthropic_sdk = _sdk
        except ImportError:
            _anthropic_sdk = None
        else:
            _install_sse_event_observer(_sdk)
    return _anthropic_sdk


def _require_sdk(purpose: str, verb: str = "Install it with"):
    """``_get_anthropic_sdk()`` or ImportError naming the feature that needs it."""
    sdk = _get_anthropic_sdk()
    if sdk is None:
        raise ImportError(f"The 'anthropic' package is required for {purpose}. {verb}: pip install 'anthropic>=0.39.0'")
    return sdk


# ── SSE event observer (ping visibility) ──────────────────────────────
#
# The Anthropic SDK silently drops SSE ``ping`` events at
# ``anthropic/_streaming.py:102`` (``if sse.event == "ping": continue``),
# so during a request's queue + prefill phase the iterator yields nothing
# even though the server is sending keep-alive pings every ~10 s.  The
# downstream stale-stream detector in ``run_agent.py`` cannot distinguish
# "queued upstream, healthy" from "connection black-holed" without ping
# visibility, and ends up killing healthy long-TTFT requests (e.g.
# Opus 4.7 + 1M-context with a 200 K-token prompt on the OAuth/subscription
# path, where TTFT routinely exceeds 5 minutes).
#
# Hook design: monkey-patch ``Stream._iter_events`` — the source iterator
# that yields *all* SSE events including pings — to fire a thread-local
# callback before passing each event through.  The SDK's filtering layer
# (``Stream.__stream__``) still drops pings as before, so consumers see
# unchanged behavior.  Patches are installed once per process, guarded
# against SDK-internal API changes; on failure we log a warning and leave
# the SDK untouched (the cold-start tolerance in run_agent.py remains as
# a backstop).
import threading as _threading

_sse_event_callback = _threading.local()


def set_sse_event_callback(callback):
    """Install a thread-local callback fired on every raw SSE event.

    The callback receives one positional argument: the event name
    (``"ping"``, ``"message_start"``, ``"content_block_delta"``, …).
    Pass ``None`` to clear.  Per-thread — workers running in different
    threads don't see each other's callbacks.
    """
    _sse_event_callback.value = callback


def _get_sse_event_callback():
    return getattr(_sse_event_callback, "value", None)


_sse_observer_installed = False


def _install_sse_event_observer(sdk) -> None:
    """Wrap ``Stream._iter_events`` so we can observe pings.

    Idempotent — only patches once per process.  Best-effort: if the SDK's
    private API surface doesn't match what we expect (different version,
    refactor), we log and skip, leaving the SDK untouched.
    """
    global _sse_observer_installed
    if _sse_observer_installed:
        return
    try:
        from anthropic._streaming import Stream as _AntStream
    except Exception as exc:
        logger.warning(
            "Anthropic SDK SSE observer not installed (import failed: %s) — "
            "stream-stale detector will use cold-start tolerance only.",
            exc,
        )
        _sse_observer_installed = True
        return

    _orig_iter_events = getattr(_AntStream, "_iter_events", None)
    if _orig_iter_events is None:
        logger.warning(
            "Anthropic SDK SSE observer not installed (Stream._iter_events "
            "missing — SDK API changed?) — stream-stale detector will use "
            "cold-start tolerance only.",
        )
        _sse_observer_installed = True
        return

    def _hermes_iter_events(self):
        cb = _get_sse_event_callback()
        if cb is None:
            yield from _orig_iter_events(self)
            return
        for sse in _orig_iter_events(self):
            try:
                cb(getattr(sse, "event", None))
            except Exception:
                # Callback errors must never break SDK iteration.
                pass
            yield sse

    _AntStream._iter_events = _hermes_iter_events
    _sse_observer_installed = True
    logger.debug(
        "Anthropic SDK SSE observer installed — stream-stale detector "
        "now sees ping events."
    )

logger = logging.getLogger(__name__)


THINKING_BUDGET = {"xhigh": 32000, "high": 16000, "medium": 8000, "low": 4000}
# Hermes effort -> Anthropic adaptive-thinking effort (output_config.effort). 4.7+ exposes
# low/medium/high/xhigh/max; Opus/Sonnet 4.6 have no xhigh, so callers downgrade xhigh->max
# there (see _supports_xhigh_effort). "minimal" is a legacy alias for low on every model.
ADAPTIVE_EFFORT_MAP = {
    "ultra": "max", "max": "max", "xhigh": "xhigh", "high": "high", "medium": "medium", "low": "low",
    "minimal": "low",
}

# Thinking-mode classification. Claude 4.6 replaced budget-based extended thinking with *adaptive*
# thinking; 4.7 additionally forbids the manual ``thinking`` block and drops temperature/top_p/
# top_k. Newer releases share no common version substring, so an allowlist of "modern" versions
# would go stale and silently route a new model down the legacy path: unknown Claude models
# DEFAULT to the modern contract and only explicit *legacy* lists are kept (mirroring
# _get_anthropic_max_output's default-to-newest). Non-Claude Anthropic-Messages models (minimax,
# qwen3, GLM, ...) fall through to the legacy manual-thinking path, which they need.
# Older Claude families that need manual thinking (budget_tokens only); ``claude-3`` covers
# 3/3.5/3.7 and the ``-2025`` entries are date-stamped 4.0 ids.
_LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS = (
    "claude-3", "claude-opus-4-0", "claude-opus-4.0", "claude-opus-4-1", "claude-opus-4.1",
    "claude-sonnet-4-0", "claude-sonnet-4.0", "claude-opus-4-2025", "claude-sonnet-4-2025",
    "claude-opus-4-5", "claude-opus-4.5", "claude-sonnet-4-5", "claude-sonnet-4.5", "claude-haiku-4-5",
    "claude-haiku-4.5",
)
# Adaptive families that reject the "xhigh" effort (arrived with Opus 4.7) and still accept
# sampling params.
_NO_XHIGH_CLAUDE_SUBSTRINGS = ("claude-opus-4-6", "claude-opus-4.6", "claude-sonnet-4-6", "claude-sonnet-4.6")
# Adaptive families where thinking is mandatory: ``thinking: {"type": "disabled"}`` answers HTTP
# 400 (Portal flags them ``reasoning.mandatory``). The failure is asymmetric — a missing entry
# 400s the turn, a spurious one only leaves thinking on — so when in doubt, add the family.
_MANDATORY_THINKING_CLAUDE_SUBSTRINGS = ("claude-fable",)


def _is_claude_model(model: str | None) -> bool:
    return "claude" in (model or "").lower()


def _model_matches(model: str, substrings) -> bool:
    """Case-insensitive substring match of ``model`` against a family list."""
    m = model.lower()
    return any(v in m for v in substrings)


# Max output tokens per model (Anthropic docs + Cline catalog). Anthropic requires max_tokens; a
# fixed 16384 starved thinking-enabled models (thinking tokens count toward the limit).
# ``claude-fable`` = Mythos-class named models (1M context); ``minimax`` is a third-party
# Anthropic-compatible endpoint; DashScope enforces ``qwen3`` max_tokens in [1, 65536].
_ANTHROPIC_OUTPUT_LIMITS = {
    # Match Claude Code 2.1.119 main chat path (verified by disassembly:
    # `max_tokens: 16000` appears 7× in the binary; 64000 once for streaming
    # paths). Since hermes already spoofs Claude Code identity (user-agent,
    # system prefix, beta headers) to use the OAuth token, matching its
    # max_tokens too keeps backend scheduling/priority signals consistent
    # with what real Claude Code sends — even though the model itself isn't
    # supposed to see this value, we don't know what other API-side decisions
    # are keyed on it. Override per-call via max_tokens kwarg when needed.
    # Mythos-class named models (claude-fable-5, …) — 1M context, reasoning
    "claude-fable":      128_000,
    # Claude Sonnet 5
    "claude-sonnet-5":   128_000,
    # Claude 4.8
    "claude-opus-4-8":   128_000,
    # Claude 4.7
    "claude-opus-4-7":    16_000,
    # Claude 4.6
    "claude-opus-4-6":    16_000,
    "claude-sonnet-4-6":  16_000,
    # Claude 4.5
    "claude-opus-4-5":    16_000,
    "claude-sonnet-4-5":  16_000,
    "claude-haiku-4-5":   16_000,
    # Claude 4
    "claude-opus-4":      32_000,
    "claude-sonnet-4":    64_000,
    # Claude 3.7
    "claude-3-7-sonnet": 128_000,
    # Claude 3.5
    "claude-3-5-sonnet":   8_192,
    "claude-3-5-haiku":    8_192,
    # Claude 3
    "claude-3-opus":       4_096,
    "claude-3-sonnet":     4_096,
    "claude-3-haiku":      4_096,
    # Third-party Anthropic-compatible providers
    "minimax":            131_072,
    # Qwen models via DashScope Anthropic-compatible endpoint
    # DashScope enforces max_tokens ∈ [1, 65536]
    "qwen3":               65_536,
}
# Unknown models get the highest current limit: future models are unlikely to have *less*.
_ANTHROPIC_DEFAULT_OUTPUT_LIMIT = 128_000


def _get_anthropic_max_output(model: str) -> int:
    """Max output tokens for ``model`` via longest substring match against
    ``_ANTHROPIC_OUTPUT_LIMITS`` (so date-stamped ids and ``:1m``/``:fast`` suffixes resolve, and
    ``claude-3-5-sonnet`` beats ``claude-3-5``). Dots normalize to hyphens (``claude-opus-4.6``)."""
    m = model.lower().replace(".", "-")
    best_key = max((key for key in _ANTHROPIC_OUTPUT_LIMITS if key in m), key=len, default=None)
    return _ANTHROPIC_OUTPUT_LIMITS[best_key] if best_key else _ANTHROPIC_DEFAULT_OUTPUT_LIMIT


def _resolve_positive_anthropic_max_tokens(value) -> Optional[int]:
    """``value`` floored to a positive int, or None when it is not a finite positive number.
    Anthropic 400s on max_tokens that are 0, negative, fractional or non-finite; the ``max_tokens
    or fallback`` idiom catches 0 but lets ``-1``/``0.5`` through. Booleans are excluded (they
    subclass int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except Exception:  # e.g. OverflowError for ints too large for float
        return None
    return int(value) if int(value) > 0 else None  # int() truncates toward zero for floats


def _resolve_anthropic_messages_max_tokens(requested, model: str, context_length: Optional[int] = None) -> int:
    """``requested`` when it is a positive finite number, else the model's output ceiling. Raises
    ValueError if neither is positive. The context-window clamp is the caller's job so the
    positive-value contract stays endpoint-agnostic."""
    resolved = _resolve_positive_anthropic_max_tokens(requested) or _get_anthropic_max_output(model)
    if resolved > 0:
        return resolved
    raise ValueError(
        f"Anthropic Messages adapter requires a positive max_tokens value for "
        f"model {model!r}; got {requested!r} and no model default resolved."
    )


def _supports_adaptive_thinking(model: str) -> bool:
    """True for Claude models using adaptive thinking (4.6+): unknown Claude models default to
    adaptive, the explicit legacy list stays manual, and non-Claude models return False — except
    Kimi/Moonshot, whose Anthropic-compatible endpoints implement the adaptive contract."""
    return _model_name_is_kimi_family(model) or (
        _is_claude_model(model) and not _model_matches(model, _LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS)
    )


def _supports_xhigh_effort(model: str) -> bool:
    """True for models accepting the 'xhigh' effort (Opus 4.7+). Opus/Sonnet 4.6 400 on it —
    callers downgrade xhigh->max when this returns False."""
    return _supports_adaptive_thinking(model) and not _model_matches(model, _NO_XHIGH_CLAUDE_SUBSTRINGS)


def _accepts_thinking_disable(model: str) -> bool:
    """True when ``model`` accepts an explicit ``thinking: {"type": "disabled"}``. Adaptive Claude
    thinks by default, so "off" only works if the disable is sent; mandatory-thinking families
    400 on it and keep the omit behavior. Legacy manual-thinking models are opt-in via
    budget_tokens, so omission is already off. Scoped to Claude: Kimi's documented disable is
    omission, and sending it a new parameter on the strength of Claude's contract is a guess."""
    return (
        _is_claude_model(model)
        and _supports_adaptive_thinking(model)
        and not _model_matches(model, _MANDATORY_THINKING_CLAUDE_SUBSTRINGS)
    )


def _forbids_sampling_params(model: str) -> bool:
    """True for models that 400 on any non-default temperature/top_p/top_k (Opus 4.7 and later;
    unknown Claude defaults to forbidding). The 4.6 family and the legacy manual-thinking families
    still accept them. Callers omit the fields entirely — the API rejects anything non-null."""
    return _is_claude_model(model) and not _model_matches(
        model, _NO_XHIGH_CLAUDE_SUBSTRINGS + _LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS
    )


def _supports_fast_mode(model: str) -> bool:
    """True for models accepting ``speed: "fast"`` (Opus 4.8 / Opus 5 / Opus 5.5, Claude API only).
    The list lives in ``agent.model_metadata`` so the wire gate and the ``/fast`` toggle agree."""
    from agent.model_metadata import is_anthropic_fast_mode_model

    return is_anthropic_fast_mode_model(model)


# Beta headers safe on ordinary/native Anthropic requests. GA on Claude 4.6+ (harmless no-op
# there) but older Claude and compatible endpoints still gate on them. Do NOT add
# ``context-1m-2025-08-07``: accounts without the long-context beta get HTTP 400, breaking short
# auxiliary calls. Bedrock/Azure still need it for 1M context and opt in on their own paths.
# MiniMax's Anthropic-compatible endpoints fail tool-use requests when the tool-streaming beta is
# present. ``_FAST_MODE_BETA`` enables the ``speed: "fast"`` request parameter.
_TOOL_STREAMING_BETA = "fine-grained-tool-streaming-2025-05-14"
_COMMON_BETAS = ["interleaved-thinking-2025-05-14", _TOOL_STREAMING_BETA]
_CONTEXT_1M_BETA = "context-1m-2025-08-07"
# FORK: extra betas Claude Code 2.1.119 sends on every /v1/messages request (mitmdump capture
# 2026-05-06), plus extended-cache-ttl which enables the ``ttl`` field on cache_control markers
# — without it Anthropic ignores ttl and ``prompt_caching.cache_ttl: 1h`` silently degrades to 5m.
_EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"
# Anthropic-native-only: stripped on bearer-auth third-party endpoints (MiniMax et al. host
# their own models and reject unknown Anthropic-namespaced betas).
_ANTHROPIC_NATIVE_ONLY_BETAS = {
    "redact-thinking-2026-02-12", "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05", "effort-2025-11-24",
}
_COMMON_BETAS += [_EXTENDED_CACHE_TTL_BETA, *sorted(_ANTHROPIC_NATIVE_ONLY_BETAS)]


def _model_supports_1m_context(model: str | None) -> bool:
    """Return True only for Anthropic models that have a 1M-context tier.

    As of 2026-05, that's Opus 4.6+, Opus 4.7, and Sonnet 4.6. Haiku 4.5
    has no 1M tier — requesting the beta on a Haiku call returns
    "long context beta is not yet available" even from paid API customers
    (it's a per-model entitlement, not per-subscription).

    Without this gate, every Haiku subagent re-discovers the rejection at
    first API call, prints the noisy warning, rebuilds its client, and
    retries. With it, the beta header simply never goes out for Haiku.

    Match by substring against ``model`` so prefixed forms
    ("anthropic/claude-opus-4-7", "claude-opus-4.7", "us.claude-opus-4-7-v1")
    all resolve correctly. Returns False for empty/None — safer to drop the
    beta than guess wrong.
    """
    if not model:
        return False
    m = str(model).lower()
    # Models with a 1M-context tier. Conservative allowlist — if a future
    # Haiku gains 1M, add it here explicitly rather than fuzzy-matching.
    # Kept in sync with agent/model_metadata.py's DEFAULT_CONTEXT_LENGTHS
    # 1,000,000-token entries (opus-5/sonnet-5/opus-4-8 were added there
    # without a matching update here — 2026-09 sync drift, fixed).
    _SUPPORTS_1M = (
        "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-fable",
        "claude-opus-4-8", "claude-opus-4.8",
        "claude-opus-4-7", "claude-opus-4.7",
        "claude-opus-4-6", "claude-opus-4.6",
        "claude-sonnet-4-6", "claude-sonnet-4.6",
    )
    return any(needle in m for needle in _SUPPORTS_1M)

# Fast mode beta — enables the ``speed: "fast"`` request parameter for
# significantly higher output token throughput on Opus 4.6 (~2.5x).
# See https://platform.claude.com/docs/en/build-with-claude/fast-mode
_FAST_MODE_BETA = "fast-mode-2026-02-01"
# Required for OAuth/subscription auth; matches Claude Code / pi-ai / OpenCode.
_OAUTH_ONLY_BETAS = ["claude-code-20250219", "oauth-2025-04-20"]

# Additional beta headers required for OAuth/subscription auth.
# Matches what Claude Code (and pi-ai / OpenCode) send.
_OAUTH_ONLY_BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
]

# Claude Code identity — required for OAuth requests to be routed correctly.
# Without these, Anthropic's infrastructure intermittently 500s OAuth traffic.
# The version must stay reasonably current — Anthropic rejects OAuth requests
# when the spoofed user-agent version is too far behind the actual release.
# Confirmed failure mode for stale fallbacks: requests come back as HTTP 400
# "You're out of extra usage" — a misleading billing-tier message that
# actually signals the user-agent version is rejected. Bump this constant
# whenever you notice deployments without Claude Code installed start to
# 400 inexplicably. 2026-09-03: raised 2.1.138 → 2.1.259 — Anthropic's
# per-model version gate now requires >=2.1.251 (observed on fable-class
# models), so the no-CLI fallback constant must clear that bar too.
_CLAUDE_CODE_VERSION_FALLBACK = "2.1.259"
_claude_code_version_cache: Optional[str] = None

# Install prefixes probed in addition to PATH. GUI launches (the Electron desktop app, macOS
# LaunchAgents) inherit the bare ``/usr/bin:/bin:/usr/sbin:/sbin``, which carries none of these,
# so a PATH-only lookup finds nothing there even with the CLI installed — detection then returns
# the stale fallback and Anthropic 400s with "Claude Code X does not support this model".
# These are additive: on Windows none resolve to a file and detection falls back to the PATH
# lookup (which handles PATHEXT), leaving current behaviour there unchanged.
_CLAUDE_CODE_PREFIXES = (
    "~/.local/bin", "~/.claude/local", "~/bin", "~/.npm-global/bin", "~/.bun/bin",
    "~/.volta/bin", "/opt/homebrew/bin", "/usr/local/bin",
)


_CLAUDE_CODE_NAMES = ("claude", "claude-code")


def _claude_code_candidates() -> List[str]:
    """Executable paths to try, deduped and filtered to files that exist.

    Two passes: every PATH hit first (what the user's shell would run), then the
    well-known install prefixes. A single nested loop would probe a stale prefix
    ``claude`` before a current PATH ``claude-code``.
    """
    seen: Dict[str, None] = {}
    for name in _CLAUDE_CODE_NAMES:
        hit = shutil.which(name)
        if hit:
            seen.setdefault(hit)
    for prefix in _CLAUDE_CODE_PREFIXES:
        for name in _CLAUDE_CODE_NAMES:
            path = os.path.join(os.path.expanduser(prefix), name)
            if os.path.isfile(path):
                seen.setdefault(path)
    return list(seen)


def _detect_claude_code_version() -> str:
    """Installed Claude Code version (``claude --version``), else the static fallback."""
    for cmd in _claude_code_candidates():
        with suppress(Exception):
            result = subprocess.run(
                [cmd, "--version"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                version = result.stdout.strip().split()[0]  # "2.1.74 (Claude Code)" or "2.1.74"
                if version and version[0].isdigit():
                    return version
    return _CLAUDE_CODE_VERSION_FALLBACK


_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
# Real Claude Code MCP tools follow ``mcp__<server>__<tool>`` (double-
# underscore separators).  Hermes' MCP-source tools are registered with the
# same convention now (see ``tools/mcp_tool.py::_convert_mcp_schema``).  This
# constant is the *prefix* check — anything starting with ``mcp__`` is
# treated as already-prefixed by the OAuth-path identity rewriter.
_MCP_TOOL_PREFIX = "mcp__"


def _get_claude_code_version() -> str:
    """Detect lazily (only OAuth headers need it) and cache for the process."""
    global _claude_code_version_cache
    if _claude_code_version_cache is None:
        _claude_code_version_cache = _detect_claude_code_version()
    return _claude_code_version_cache


_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
_MCP_TOOL_PREFIX = "mcp__"

# Anthropic's OAuth billing classifier fingerprints certain Hermes tool schemas/prose as a
# third-party app and reroutes to the metered extra-usage lane (HTTP 400 "You're out of extra
# usage" on a valid subscription). Live A/B repros isolated two independent triggers — the
# ``session_search`` tool (schema/name/prose) and the ``memory`` tool (schema/name) — so both are
# aliased on the OAuth wire only; normalize_response reverses the mapping.
_OAUTH_TOOL_NAME_ALIASES = {"session_search": "chat_history_lookup", "memory": "context_notes"}
_OAUTH_TOOL_NAME_REVERSE_ALIASES = {wire_name: name for name, wire_name in _OAUTH_TOOL_NAME_ALIASES.items()}

# Aliases ALSO safe to substitute in free-form prose (system prompt, tool descriptions). "memory"
# is ordinary English throughout the prompt and inside the memory tool's own parameter docs (an
# enum the model must emit verbatim), so rewriting it would corrupt guidance; a model that calls
# bare ``memory`` still dispatches, since normalize_response resolves it through the registry.
_OAUTH_PROSE_ALIAS_NAMES = frozenset({"session_search"})

# Word-boundary matchers so a longer identifier containing the token (e.g.
# ``tools/session_search_tool.py`` in AGENTS.md) is left alone; ``\b`` treats ``_`` as a word char.
_OAUTH_PROSE_ALIAS_PATTERNS = tuple(
    (re.compile(rf"\b{re.escape(name)}\b"), _OAUTH_TOOL_NAME_ALIASES[name])
    for name in sorted(_OAUTH_PROSE_ALIAS_NAMES)
)


def _system_prompt_mode_compact() -> bool:
    """Return True when ``agent.system_prompt_mode`` is set to ``compact``.

    Cheap import — the module loads lazily so we don't pay for it on every
    request unless the user opts in to compact mode. Falls back to False on
    any config-load failure so legacy behavior wins under errors.
    """
    try:
        from hermes_cli.config import load_config as _load_cfg
        mode = ((_load_cfg() or {}).get("agent") or {}).get("system_prompt_mode")
        return str(mode or "").strip().lower() == "compact"
    except Exception:
        return False


def _prepend_user_message_preamble(
    messages: List[Dict[str, Any]],
    preamble: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Insert ``preamble`` (a content block) at the head of the first
    user-role message's content list. Pure — returns a new list.

    Used by compact-mode system-prompt placement: dynamic context that
    would otherwise live in ``system`` rides on the conversation instead.
    Handles three content shapes:
      * ``content`` is a string → wrap in a list and prepend
      * ``content`` is already a list → prepend the block in place
      * No user messages exist → return ``messages`` unchanged

    Tool_result-only first turns (resume from background tool call) are
    rare on the gateway path; if encountered we leave them alone since
    Anthropic disallows non-tool_result content as the first block of a
    tool_result turn.
    """
    if not isinstance(messages, list) or not messages:
        return messages

    out = list(messages)
    for i, msg in enumerate(out):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        # Skip messages whose first content block is a tool_result —
        # Anthropic enforces tool_result-first ordering on those turns.
        if isinstance(content, list) and content and isinstance(content[0], dict):
            if content[0].get("type") == "tool_result":
                continue
        new_msg = dict(msg)
        if isinstance(content, str):
            new_msg["content"] = [preamble, {"type": "text", "text": content}]
        elif isinstance(content, list):
            new_msg["content"] = [preamble, *content]
        else:
            # Unrecognized content shape — leave it alone, return untouched.
            return messages
        out[i] = new_msg
        return out

    return messages










# Model-name prefixes that identify the Kimi / Moonshot family.  Covers
# - official slugs: ``kimi-k2.5``, ``kimi_thinking``, ``moonshot-v1-8k``
# - common release lines: ``k1.5-...``, ``k2-thinking``, ``k25-...``, ``k2.5-...``,
#   and the bare Coding Plan slug ``k3`` (plus ``k3.x``/``k3-...`` variants)
# Matched case-insensitively against the post-``normalize_model_name`` form,
# so a caller's ``provider/vendor/model`` slug is handled the same as a
# bare name.

# Bare release slugs with no separator suffix (Kimi Coding Plan serves K3
# as the exact slug ``k3``). Kept exact-match so unrelated model names that
# merely start with the same characters don't get misclassified.




def _is_kimi_family_endpoint(base_url: str | None, model: str | None = None) -> bool:
    """Return True for any Kimi / Moonshot Anthropic-Messages-speaking endpoint.

    Broader than ``_is_kimi_coding_endpoint`` — matches:

    - Kimi's official ``/coding`` URL (legacy check, preserved)
    - Any ``api.kimi.com`` / ``moonshot.ai`` / ``moonshot.cn`` host
    - Custom or proxied endpoints whose *model* name is in the Kimi / Moonshot
      family (``kimi-*``, ``moonshot-*``, ``k1.*``, ``k2.*``, …).  Users with
      ``api_mode: anthropic_messages`` on a private gateway fronting Kimi
      fall into this branch — the upstream still enforces Kimi's thinking
      semantics (reasoning_content required on every replayed tool-call
      message) regardless of the gateway's hostname.

    Used to decide whether to drop Anthropic's ``thinking`` kwarg and to
    preserve unsigned reasoning_content-derived thinking blocks on replay.
    See hermes-agent#13848, #17057.
    """
    if _is_kimi_coding_endpoint(base_url):
        return True
    for _domain in ("api.kimi.com", "moonshot.ai", "moonshot.cn"):
        if base_url_host_matches(base_url or "", _domain):
            return True
    if _model_name_is_kimi_family(model):
        return True
    return False


def _is_deepseek_anthropic_endpoint(base_url: str | None) -> bool:
    """Return True for DeepSeek's Anthropic-compatible endpoint.

    DeepSeek's ``/anthropic`` route speaks the Anthropic Messages protocol
    but, when thinking mode is enabled, requires the ``thinking`` blocks
    from prior assistant turns to round-trip on subsequent requests — the
    generic third-party path strips them and triggers HTTP 400::

        The content[].thinking in the thinking mode must be passed back
        to the API.

    Per DeepSeek's published compatibility matrix the blocks are unsigned
    (no Anthropic-proprietary signature, no ``redacted_thinking`` support),
    so this endpoint is handled with the same strip-signed / keep-unsigned
    policy used for Kimi's ``/coding`` endpoint.  The match is pinned to
    the ``/anthropic`` path so the OpenAI-compatible ``api.deepseek.com``
    base URL (which never reaches this adapter) is not misclassified.
    See hermes-agent#16748.
    """
    if not base_url_host_matches(base_url or "", "api.deepseek.com"):
        return False
    normalized = _normalize_base_url_text(base_url)
    if not normalized:
        return False
    return "/anthropic" in normalized.rstrip("/").lower()






def _base_url_needs_context_1m_beta(base_url: str | None) -> bool:
    """FORK override of ``anthropic_endpoints._base_url_needs_context_1m_beta`` (azure-only):
    the fork also opts NATIVE Anthropic in, because its 1M usage is gated per-model by
    ``_model_supports_1m_context`` plus the ``_oauth_1m_beta_disabled`` latch rather than by
    endpoint. Deliberately not imported from anthropic_endpoints for that reason.

    Return True for endpoints that gate 1M context behind a beta.

    Native Anthropic (no base_url override, or any *.anthropic.com host)
    plus Azure AI Foundry. Bedrock has its own client helper
    (``build_anthropic_bedrock_client``) that opts in explicitly.
    Bearer-auth third-party endpoints (MiniMax) reject the beta and have
    it stripped further down in ``_common_betas_for_base_url``. Custom
    base_urls of unknown origin do NOT get the beta — conservative
    default to avoid the "long context beta is not yet available"
    rejection from third-party providers that mimic Anthropic's surface.
    """
    normalized = _normalize_base_url_text(base_url).lower()
    if not normalized:
        return True  # native Anthropic — default base_url
    if "azure.com" in normalized:
        return True
    if "anthropic.com" in normalized:
        return True
    return False





def _apply_oauth_prose_aliases(text: str) -> str:
    """Rewrite prose-safe tool-name tokens to their OAuth wire aliases."""
    for pattern, wire_name in _OAUTH_PROSE_ALIAS_PATTERNS:
        text = pattern.sub(wire_name, text)
    return text


def _common_betas_for_base_url(
    base_url: str | None, *, drop_context_1m_beta: bool = False, model: str | None = None,
) -> list[str]:
    """Beta headers safe for the configured endpoint. MiniMax (Bearer-auth) rejects both the
    fine-grained-tool-streaming beta (every tool-use message errors) and the 1M-context beta.
    Azure AI Foundry also uses Bearer auth but keeps both — it needs the 1M beta for 1M context,
    which native Anthropic does not get by default (some subscriptions reject it; Bedrock opts in
    via its own client helper). ``drop_context_1m_beta`` strips the 1M beta after a
    subscription/endpoint rejected it.

    FORK: ``model``, when known, gates the 1M beta proactively — models with no 1M tier (Haiku
    4.5, older Claude) drop the header so those agents never trigger the rejection-and-retry
    path. ``model=None`` keeps the endpoint+latch gating only."""
    betas = list(_COMMON_BETAS)
    if (
        _base_url_needs_context_1m_beta(base_url)
        and not drop_context_1m_beta
        and (model is None or _model_supports_1m_context(model))
    ):
        # FORK: insert after fine-grained-tool-streaming to preserve Claude Code 2.1.119 wire
        # ordering (verified by mitmdump against api.anthropic.com).
        betas.insert(2, _CONTEXT_1M_BETA)
    if _requires_bearer_auth(base_url):
        # FORK: MiniMax rejects tool-streaming AND context-1m AND the Anthropic-native-only
        # betas; Azure (and other future bearer-auth endpoints) only rejects the native-only set.
        if _is_minimax_anthropic_endpoint(base_url):
            _stripped = {_TOOL_STREAMING_BETA, _CONTEXT_1M_BETA, _EXTENDED_CACHE_TTL_BETA} | _ANTHROPIC_NATIVE_ONLY_BETAS
        else:
            _stripped = set(_ANTHROPIC_NATIVE_ONLY_BETAS)
        return [b for b in betas if b not in _stripped]
    if drop_context_1m_beta:
        return [b for b in betas if b != _CONTEXT_1M_BETA]
    return betas


def _beta_header(betas: list) -> Dict[str, str]:
    """``{"anthropic-beta": ...}`` when there are betas, else ``{}``."""
    return {"anthropic-beta": ",".join(betas)} if betas else {}


def _attribution_headers() -> Dict[str, str]:
    """Same client-attribution set sent to OpenRouter / Vercel AI Gateway / Fireworks."""
    return {
        "HTTP-Referer": "https://hermes-agent.nousresearch.com", "X-Title": "Hermes Agent",
        "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
    }


def _client_timeout(timeout):
    """httpx.Timeout with the caller's read timeout (default 900s) and a 10s connect."""
    from httpx import Timeout
    read = timeout if (isinstance(timeout, (int, float)) and timeout > 0) else 900.0
    return Timeout(timeout=float(read), connect=10.0)


def _base_client_kwargs(base_url, timeout) -> tuple[str, Dict[str, Any]]:
    """Shared SDK constructor kwargs -> ``(normalized_base_url, kwargs)``. Retry is delegated to
    hermes's outer loop (``max_retries=0``): the SDK default of 2 uses its own backoff that ignores
    Retry-After and double-retries inside our loop. Any trailing ``/v1`` is stripped because the
    SDK appends ``/v1/messages``. Azure's ``api-version`` goes through ``default_query`` so the
    base_url is not corrupted into ``/anthropic?api-version=.../v1/messages``."""
    kwargs: Dict[str, Any] = {"timeout": _client_timeout(timeout), "max_retries": 0}
    normalized = re.sub(r"/v1/?$", "", _normalize_base_url_text(base_url).rstrip("/"))
    if normalized:
        kwargs["base_url"] = normalized
        if _is_azure_anthropic_endpoint(normalized) and "api-version" not in normalized:
            kwargs["default_query"] = {"api-version": "2025-04-15"}
    return normalized, kwargs


def _build_anthropic_client_with_bearer_hook(
    token_provider, base_url: str = None, timeout: float = None, *, drop_context_1m_beta: bool = False
):
    """Anthropic-on-Foundry Entra ID variant of :func:`build_anthropic_client`. The SDK stores
    ``api_key``/``auth_token`` as static strings, so per-request bearer refresh (Microsoft's
    documented Foundry pattern) uses a custom ``httpx.Client`` whose request hook mints a fresh JWT
    and rewrites ``Authorization``; the SDK skips its own auth when ``http_client`` is given. The
    placeholder ``auth_token`` is still required at construction and makes any leak diagnosable."""
    sdk = _require_sdk("Azure Foundry Anthropic-style endpoints with Entra ID auth", verb="Install with")
    normalize_proxy_env_vars()
    from agent.azure_identity_adapter import build_bearer_http_client
    normalized_base_url, kwargs = _base_client_kwargs(base_url, timeout)
    kwargs["http_client"] = build_bearer_http_client(token_provider, timeout=kwargs["timeout"])
    kwargs["auth_token"] = "entra-id-bearer-via-http-hook"
    betas = _common_betas_for_base_url(normalized_base_url, drop_context_1m_beta=drop_context_1m_beta)
    from agent.anthropic_credentials import anthropic_route_is_oauth
    if anthropic_route_is_oauth(base_url, token_provider):
        # key_cmd-sourced Claude Code OAuth on the native host: a bare bearer without the Claude Code
        # identity is answered with 429 rate_limit_error "Error" (#114967) — same headers as the
        # static "oauth" style in build_anthropic_client.
        headers = _beta_header(betas + _OAUTH_ONLY_BETAS)
        headers["user-agent"] = f"claude-code/{_get_claude_code_version()} (external, cli)"
        headers["x-app"] = "cli"
    else:
        headers = _beta_header(betas)
    return _new_sdk_client(sdk, kwargs, headers, route=base_url)


def _new_sdk_client(sdk, kwargs: Dict[str, Any], headers: Dict[str, str], route: str = None):
    """``sdk.Anthropic(**kwargs)`` with ``headers`` attached, sending exactly ONE credential.
    ``route`` is the caller's un-normalized base_url (the ``/v1`` form ``custom_providers`` entries are
    keyed by; ``kwargs["base_url"]`` has it stripped) for the per-provider ``extra_headers`` lookup.

    The SDK fills whichever of ``api_key`` / ``auth_token`` we left unset from ANTHROPIC_API_KEY /
    ANTHROPIC_AUTH_TOKEN in the environment (both loaded from ~/.hermes/.env) and then sends dual
    auth — x-api-key *and* Authorization: Bearer — shipping a foreign credential to Portal / MiniMax
    / OAuth / Entra / third-party endpoints (#26970, #105774). An ``Omit()`` default header is the
    SDK-sanctioned way to drop the other header, and unlike an attribute clear it survives
    ``with_options()``, which re-runs the constructor and re-reads the environment."""
    merged = dict(headers)
    if "api_key" in kwargs and "auth_token" not in kwargs:
        merged["Authorization"] = sdk.Omit()
    elif "auth_token" in kwargs and "api_key" not in kwargs:
        merged["X-Api-Key"] = sdk.Omit()
    # Per-provider ``custom_providers[].extra_headers`` last: the most specific config level wins
    # over the SDK User-Agent and the attribution/beta sets above, on every builder path (init,
    # /model switch, rebuild, auxiliary) — the OpenAI-wire clients already do this (#24293, #9721).
    merged.update(_custom_provider_extra_headers(route or kwargs.get("base_url")))
    if merged:
        kwargs["default_headers"] = merged
    return sdk.Anthropic(**kwargs)


def _custom_provider_extra_headers(base_url) -> Dict[str, str]:
    """``extra_headers`` of the ``custom_providers`` entry routed at *base_url*, else ``{}``.
    SECURITY: values routinely carry credentials (Cloudflare Access tokens) — never log them."""
    if not base_url:
        return {}
    try:
        from hermes_cli.config import get_custom_provider_extra_headers
        return get_custom_provider_extra_headers(str(base_url))
    except Exception:
        logger.debug("custom-provider extra_headers skipped for Anthropic client", exc_info=True)
        return {}


def _auth_style(api_key, base_url, normalized_base_url) -> str:
    """Order-sensitive endpoint/key classification for :func:`build_anthropic_client`. ``kimi``:
    Kimi's /coding endpoint 403s without a User-Agent (the Kimi team asked for proper attribution).
    ``bearer``: MiniMax & co. want Authorization: Bearer — checked before the OAuth shape test
    because their secrets lack the sk-ant-api prefix and would be misread as OAuth/setup tokens.
    ``api_key``: third-party proxies use their own x-api-key keys (skip OAuth detection). ``oauth``:
    Bearer auth + Claude Code identity (Anthropic routes OAuth by user-agent; without it, 500s)."""
    if _is_kimi_coding_endpoint(base_url):
        return "kimi"
    if _requires_bearer_auth(normalized_base_url):
        return "bearer"
    if _is_third_party_anthropic_endpoint(base_url):
        return "api_key"
    if _is_oauth_token(api_key):
        return "oauth"
    return "api_key"


def build_anthropic_client(
    api_key, base_url: str = None, timeout: float = None, *,
    drop_context_1m_beta: bool = False, model: Optional[str] = None,
):
    """Create an Anthropic client, auto-detecting setup-tokens vs API keys. ``api_key`` is a static
    ``str`` or a ``Callable[[], str]`` Entra ID bearer provider (routed through
    :func:`_build_anthropic_client_with_bearer_hook`). ``timeout`` overrides the 900s read timeout
    (connect stays 10s). ``drop_context_1m_beta`` strips ``context-1m-2025-08-07`` from the
    client-level beta header — the reactive OAuth retry in run_agent uses it after a subscription
    rejects it; fresh clients keep the default so 1M-capable subscriptions keep the capability.

    FORK: ``model`` (when known) additionally lets ``_common_betas_for_base_url`` strip the
    1M beta proactively for models with no 1M tier (Haiku 4.5) — the auxiliary client passes it
    so Haiku-routed aux calls don't 400 on a client-level ``context-1m-...`` header."""
    sdk = _require_sdk("the Anthropic provider")
    if callable(api_key) and not isinstance(api_key, str):
        return _build_anthropic_client_with_bearer_hook(
            api_key, base_url, timeout, drop_context_1m_beta=drop_context_1m_beta
        )
    normalize_proxy_env_vars()
    normalized_base_url, kwargs = _base_client_kwargs(base_url, timeout)
    if "default_query" in kwargs:  # historical: this path also strips a stray trailing slash on Azure
        kwargs["base_url"] = normalized_base_url.rstrip("/")
    common_betas = _common_betas_for_base_url(
        normalized_base_url, drop_context_1m_beta=drop_context_1m_beta, model=model,
    )
    style = _auth_style(api_key, base_url, normalized_base_url)
    kwargs["auth_token" if style in ("bearer", "oauth") else "api_key"] = api_key
    headers = _beta_header(common_betas + _OAUTH_ONLY_BETAS if style == "oauth" else common_betas)
    if style == "kimi":
        headers = {**_attribution_headers(), **headers}
    elif style == "oauth":
        headers["user-agent"] = f"claude-code/{_get_claude_code_version()} (external, cli)"
        headers["x-app"] = "cli"
    if _is_opencode_endpoint(base_url):
        # OpenCode identifies clients by request headers (like OpenRouter). The OpenAI-wire paths
        # get these from profile.default_headers, but this route never sees the profile.
        for k, v in _attribution_headers().items():
            headers.setdefault(k, v)
    return _new_sdk_client(sdk, kwargs, headers, route=base_url)


def build_anthropic_bedrock_client(region: str):
    """AnthropicBedrock client for Bedrock Claude models (boto3 default credential chain). The
    SDK's native Bedrock adapter gives full Claude feature parity (prompt caching, thinking
    budgets, adaptive thinking, fast mode) that Converse lacks. The common betas plus
    ``context-1m-2025-08-07`` are attached: without the latter Bedrock caps Opus 4.6/4.7 at 200K.
    A configured ``bedrock.guardrail`` rides as InvokeModel headers so every client built here
    (primary, auxiliary, per-request rebuild) enforces it."""
    from agent.bedrock_adapter import bedrock_guardrail_headers, scoped_aws_session_kwargs
    sdk = _require_sdk("the Bedrock provider")
    if not hasattr(sdk, "AnthropicBedrock"):
        raise ImportError("anthropic.AnthropicBedrock not available. Upgrade with: pip install 'anthropic>=0.39.0'")
    # Routed multiplex profile: its own AWS_* from the secret scope (the SDK would otherwise read the
    # launch profile's process env); unscoped passes nothing and keeps the default chain.
    scoped = scoped_aws_session_kwargs()
    aws_kwargs = {"aws_access_key": scoped.get("aws_access_key_id"), "aws_secret_key": scoped.get("aws_secret_access_key"),
                  "aws_session_token": scoped.get("aws_session_token"), "aws_profile": scoped.get("profile_name")}
    return sdk.AnthropicBedrock(
        aws_region=region, timeout=_client_timeout(None), **{k: v for k, v in aws_kwargs.items() if v},
        max_retries=0,  # retry belongs to hermes's outer loop (honors Retry-After)
        default_headers={**_beta_header([*_COMMON_BETAS, _CONTEXT_1M_BETA]), **bedrock_guardrail_headers()},
    )


# ---------------------------------------------------------------------------
# Message / tool / response format conversion
# ---------------------------------------------------------------------------


def _is_bedrock_model_id(model: str) -> bool:
    """Detect AWS Bedrock model IDs that use dots as namespace separators.

    Bedrock model IDs come in two forms:
    - Bare:    ``anthropic.claude-opus-4-7``
    - Regional (inference profiles): ``us.anthropic.claude-sonnet-4-5-v1:0``

    In both cases the dots separate namespace components, not version
    numbers, and must be preserved verbatim for the Bedrock API.
    """
    lower = model.lower()
    # Regional inference-profile prefixes
    if any(lower.startswith(p) for p in (
        "global.", "us.", "eu.", "apac.", "ap.", "au.", "jp.",
        "ca.", "sa.", "me.", "af.",
    )):
        return True
    # Bare Bedrock model IDs: provider.model-family
    if lower.startswith("anthropic."):
        return True
    return False




def _sanitize_tool_id(tool_id: str) -> str:
    """Sanitize a tool call ID for the Anthropic API.

    Anthropic requires IDs matching [a-zA-Z0-9_-]. Replace invalid
    characters with underscores and ensure non-empty.
    """
    import re
    if not tool_id:
        return "tool_0"
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_id)
    return sanitized or "tool_0"


def _normalize_tool_input_schema(schema: Any) -> Dict[str, Any]:
    """Normalize tool schemas before sending them to Anthropic.

    Anthropic's tool schema validator rejects nullable unions such as
    ``anyOf: [{"type": "string"}, {"type": "null"}]`` that Pydantic/MCP
    commonly emits for optional fields. Tool optionality is represented by
    the parent ``required`` array, so we delegate to the shared
    ``strip_nullable_unions`` helper to collapse nullable unions to the
    non-null branch while preserving metadata like description/default.

    ``keep_nullable_hint=False`` because the Anthropic validator does not
    recognize the OpenAPI-style ``nullable: true`` extension and strict
    schema-to-grammar converters may reject unknown keywords.

    Top-level ``oneOf``/``allOf``/``anyOf`` are also stripped here: the
    Anthropic API rejects union keywords at the schema root with a generic
    HTTP 400. Several upstream and plugin tools ship schemas with one of
    these keywords at the top level (commonly for Pydantic discriminated
    unions). If we land here with those keywords still present after
    nullable-union stripping, drop them and fall back to a plain object
    schema so the tool still validates at the Anthropic boundary.
    """
    if not schema:
        return {"type": "object", "properties": {}}

    from tools.schema_sanitizer import strip_nullable_unions

    normalized = strip_nullable_unions(schema, keep_nullable_hint=False)
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    # Strip top-level union keywords that Anthropic's validator rejects.
    banned = {"oneOf", "allOf", "anyOf"}
    if banned & normalized.keys():
        normalized = {k: v for k, v in normalized.items() if k not in banned}
        if "type" not in normalized:
            normalized["type"] = "object"
    if normalized.get("type") == "object" and not isinstance(normalized.get("properties"), dict):
        normalized = {**normalized, "properties": {}}
    return normalized


def _strip_unknown_tool_blocks(
    anthropic_messages: List[Dict],
    available_tool_names: set,
) -> List[Dict]:
    """Drop tool_use / tool_result blocks for tools not in the live tool list.

    Anthropic's Messages API rejects any request whose history contains a
    ``tool_use`` block whose ``name`` is not present in the current
    ``tools`` array — the error surfaces as
    ``invalid_request_error: Tool reference 'X' not found in available tools``.

    This is easy to hit in practice:

      * MCP server reconnect storms — when ``mcp__salesforce__*`` /
        ``mcp__jira__*`` / ``StackOverflowTeams_*`` tools were used
        last turn but the MCP server fails to reconnect this turn,
        their schemas are absent from the tool list while the prior
        ``tool_use`` blocks remain in the conversation transcript.
      * Toolset switches mid-session via ``/toolsets remove`` — drops
        ``clarify`` / ``send_message`` etc. while the assistant message
        history still carries calls to them.
      * Subagents / batched delegates — the parent's history contains
        tool calls that the leaf subagent's narrower toolset doesn't
        expose.

    We replace each unknown ``tool_use`` (and its matching ``tool_result``)
    with a small text block describing what was called.  Pure removal would
    be safer wire-shape-wise but lossier: the model loses the breadcrumb
    that a tool ran.  Text replacement preserves the trail while satisfying
    Anthropic's validator.

    Empty / None ``available_tool_names`` is treated as "drop everything"
    — the orphan-stripping in ``convert_messages_to_anthropic`` already
    handles the no-tools-at-all case for unmatched pairs, but a matched
    pair with a stale name still slips through; this catches it.
    """
    if not anthropic_messages:
        return anthropic_messages

    # First pass: identify unknown tool_use ids (we need them to also
    # rewrite the matching tool_result blocks in user messages).
    unknown_tool_use_ids: dict[str, dict] = {}  # id -> {name, input_summary}
    for msg in anthropic_messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name and name in available_tool_names:
                continue
            tool_id = block.get("id")
            if not tool_id:
                continue
            # Brief input echo for the breadcrumb — capped so a giant
            # base64 payload doesn't bloat the replacement message.
            try:
                inp_str = str(block.get("input") or {})
            except Exception:
                inp_str = "{}"
            if len(inp_str) > 200:
                inp_str = inp_str[:200] + "...(truncated)"
            unknown_tool_use_ids[tool_id] = {
                "name": name or "(unnamed)",
                "input_summary": inp_str,
            }

    if not unknown_tool_use_ids:
        return anthropic_messages

    # Second pass: rewrite blocks in place.
    for msg in anthropic_messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_blocks: list = []
        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue
            btype = block.get("type")
            if btype == "tool_use" and block.get("id") in unknown_tool_use_ids:
                meta = unknown_tool_use_ids[block["id"]]
                new_blocks.append({
                    "type": "text",
                    "text": (
                        f"[Previous tool call: {meta['name']}("
                        f"{meta['input_summary']}) — tool no longer available "
                        f"in this turn.]"
                    ),
                })
                continue
            if btype == "tool_result" and block.get("tool_use_id") in unknown_tool_use_ids:
                # Best-effort summary of the original result text so the
                # model can still reason about what came back.
                try:
                    result_content = block.get("content")
                    if isinstance(result_content, list):
                        # Anthropic tool_result content is a list of text/image blocks
                        text_pieces = []
                        for rc in result_content:
                            if isinstance(rc, dict) and rc.get("type") == "text":
                                text_pieces.append(str(rc.get("text", "")))
                        result_summary = "\n".join(text_pieces)
                    else:
                        result_summary = str(result_content or "")
                except Exception:
                    result_summary = ""
                if len(result_summary) > 400:
                    result_summary = result_summary[:400] + "...(truncated)"
                meta = unknown_tool_use_ids[block["tool_use_id"]]
                new_blocks.append({
                    "type": "text",
                    "text": (
                        f"[Previous tool result for {meta['name']}: "
                        f"{result_summary}]"
                    ),
                })
                continue
            new_blocks.append(block)
        # Empty content after rewrites — leave a placeholder so the
        # message still validates (Anthropic rejects empty content).
        if not new_blocks:
            new_blocks = [{"type": "text", "text": "(content removed)"}]
        # In a user message that's responding to an assistant tool_use,
        # Anthropic requires tool_result blocks to come BEFORE any other
        # content; otherwise the API 400s with
        #   `tool_use` ids were found without `tool_result` blocks
        #   immediately after: <id>
        # The in-place rewrite above can leave leading text breadcrumbs
        # ahead of a surviving real tool_result (when some — but not all
        # — tool_results in the same user message were converted). Stable
        # partition restores the required ordering while preserving the
        # breadcrumbs after the live tool_results.
        if msg.get("role") == "user" and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in new_blocks
        ):
            tool_results = [
                b for b in new_blocks
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            other = [
                b for b in new_blocks
                if not (isinstance(b, dict) and b.get("type") == "tool_result")
            ]
            new_blocks = tool_results + other
        # Symmetric fix on the assistant side: Anthropic rejects an
        # assistant message whose ``tool_use`` is followed by any other
        # block with the SAME 400 (``tool_use`` ids without
        # ``tool_result`` blocks immediately after). This rewrite can
        # produce that pattern when one tool_use survives (live tool) and
        # a later sibling tool_use becomes a text breadcrumb — leaving
        # ``[tool_use, text]`` in the same message. Move surviving
        # tool_use blocks to the tail to restore the contract.
        #
        # Thinking-signature safety: thinking blocks are signed against
        # their position in the response stream. If any thinking block
        # is present, reordering risks invalidating the signature; skip
        # and log instead so Anthropic surfaces the issue.
        elif msg.get("role") == "assistant" and any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in new_blocks
        ):
            first_tu = next(
                i for i, b in enumerate(new_blocks)
                if isinstance(b, dict) and b.get("type") == "tool_use"
            )
            last_non_tu = max(
                (i for i, b in enumerate(new_blocks)
                 if not (isinstance(b, dict) and b.get("type") == "tool_use")),
                default=-1,
            )
            if first_tu < last_non_tu:
                has_thinking = any(
                    isinstance(b, dict)
                    and b.get("type") in ("thinking", "redacted_thinking")
                    for b in new_blocks
                )
                if has_thinking:
                    logger.warning(
                        "anthropic_adapter: assistant message has tool_use "
                        "followed by non-tool_use blocks AND contains a "
                        "thinking block; cannot reorder without invalidating "
                        "the thinking signature. Anthropic may reject with "
                        "a 400 about tool_use without tool_result.",
                    )
                else:
                    tool_uses = [
                        b for b in new_blocks
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    ]
                    other = [
                        b for b in new_blocks
                        if not (isinstance(b, dict) and b.get("type") == "tool_use")
                    ]
                    new_blocks = other + tool_uses
        msg["content"] = new_blocks

    if unknown_tool_use_ids:
        logger.info(
            "anthropic_adapter: rewrote %d tool_use/result block(s) for tools "
            "no longer available: %s",
            len(unknown_tool_use_ids),
            sorted({m["name"] for m in unknown_tool_use_ids.values()}),
        )
    return anthropic_messages




def _image_source_from_openai_url(url: str) -> Dict[str, str]:
    """Convert an OpenAI-style image URL/data URL into Anthropic image source."""
    url = str(url or "").strip()
    if not url:
        return {"type": "url", "url": ""}

    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = "image/jpeg"
        if header.startswith("data:"):
            mime_part = header[len("data:"):].split(";", 1)[0].strip()
            if mime_part.startswith("image/"):
                media_type = mime_part
        return {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        }

    return {"type": "url", "url": url}


def _convert_content_part_to_anthropic(part: Any) -> Optional[Dict[str, Any]]:
    """Convert a single OpenAI-style content part to Anthropic format."""
    if part is None:
        return None
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return {"type": "text", "text": str(part)}

    ptype = part.get("type")

    if ptype == "input_text":
        block: Dict[str, Any] = {"type": "text", "text": part.get("text", "")}
    elif ptype == "text":
        # A stored Anthropic text block. Rebuild from whitelisted fields only —
        # SDK response text blocks carry output-only siblings (parsed_output,
        # citations=None) that the Messages INPUT schema rejects with HTTP 400
        # "Extra inputs are not permitted". Do NOT dict(part) it verbatim.
        block = {"type": "text", "text": part.get("text", "")}
        cits = part.get("citations")
        if isinstance(cits, list) and cits:
            block["citations"] = cits
    elif ptype in {"image_url", "input_image"}:
        image_value = part.get("image_url", {})
        url = image_value.get("url", "") if isinstance(image_value, dict) else str(image_value or "")
        block = {"type": "image", "source": _image_source_from_openai_url(url)}
    else:
        block = dict(part)

    if isinstance(part.get("cache_control"), dict) and "cache_control" not in block:
        block["cache_control"] = dict(part["cache_control"])
    return block


def _to_plain_data(value: Any, *, _depth: int = 0, _path: Optional[set] = None) -> Any:
    """Recursively convert SDK objects to plain Python data structures.

    Guards against circular references (``_path`` tracks ``id()`` of objects
    on the *current* recursion path) and runaway depth (capped at 20 levels).
    Uses path-based tracking so shared (but non-cyclic) objects referenced by
    multiple siblings are converted correctly rather than being stringified.
    """
    _MAX_DEPTH = 20
    if _depth > _MAX_DEPTH:
        return str(value)

    if _path is None:
        _path = set()

    obj_id = id(value)
    if obj_id in _path:
        return str(value)

    if hasattr(value, "model_dump"):
        _path.add(obj_id)
        try:
            # warnings=False: content blocks from the streaming accumulator
            # (ParsedTextBlock et al.) trip pydantic's serializer-mismatch
            # UserWarning against the generic Message union; the dump itself
            # is correct, and the warning leaks to the user's terminal.
            dumped = value.model_dump(warnings=False)
        except TypeError:
            # Duck-typed model_dump without pydantic's signature.
            dumped = value.model_dump()
        result = _to_plain_data(dumped, _depth=_depth + 1, _path=_path)
        _path.discard(obj_id)
        return result
    if isinstance(value, dict):
        _path.add(obj_id)
        result = {k: _to_plain_data(v, _depth=_depth + 1, _path=_path) for k, v in value.items()}
        _path.discard(obj_id)
        return result
    if isinstance(value, (list, tuple)):
        _path.add(obj_id)
        result = [_to_plain_data(v, _depth=_depth + 1, _path=_path) for v in value]
        _path.discard(obj_id)
        return result
    if hasattr(value, "__dict__"):
        _path.add(obj_id)
        result = {
            k: _to_plain_data(v, _depth=_depth + 1, _path=_path)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
        _path.discard(obj_id)
        return result
    return value


def _extract_preserved_thinking_blocks(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return Anthropic thinking blocks previously preserved on the message."""
    raw_details = message.get("reasoning_details")
    if not isinstance(raw_details, list):
        return []

    preserved: List[Dict[str, Any]] = []
    for detail in raw_details:
        if not isinstance(detail, dict):
            continue
        block_type = str(detail.get("type", "") or "").strip().lower()
        if block_type not in {"thinking", "redacted_thinking"}:
            continue
        preserved.append(copy.deepcopy(detail))
    return preserved


# Input-accepted fields per assistant content block type, derived at import
# time from the Anthropic SDK's BetaXBlockParam annotations.  The SDK is
# the source of truth — when it bumps and adds a new field, this map
# updates automatically.  Hardcoded baseline below covers the same set in
# case the SDK rearranges its module layout (we'd notice on the next test
# run rather than silently passing response-only fields through).
#
# Why this matters: Anthropic's response models carry fields not on the
# input param models (e.g. text.parsed_output from structured output).
# Replaying a response block verbatim trips the input validator with
# HTTP 400 "Extra inputs are not permitted".  Allowlisting to input-shape
# is the only stable contract.
_INPUT_BLOCK_FIELDS_FALLBACK: Dict[str, frozenset] = {
    "text": frozenset({"type", "text", "cache_control"}),
    "thinking": frozenset({"type", "thinking", "signature"}),
    "redacted_thinking": frozenset({"type", "data"}),
    "tool_use": frozenset({"type", "id", "name", "input", "cache_control", "caller"}),
    "image": frozenset({"type", "source", "cache_control"}),
    "document": frozenset({"type", "source", "title", "context", "citations", "cache_control"}),
}


def _build_input_block_fields() -> Dict[str, frozenset]:
    """Resolve input-allowed fields per block type from the SDK at import.

    Returns the SDK-derived map merged over the hardcoded baseline so a
    block type the SDK exposes wins, while a block type the SDK rearranged
    out of the import path still has a working entry.
    """
    # (block "type" string, param class import path).  When the SDK adds a
    # new block type with a Param model, drop a tuple here — no other
    # change needed.
    _PARAM_REGISTRY = (
        ("text", "BetaTextBlockParam"),
        ("thinking", "BetaThinkingBlockParam"),
        ("redacted_thinking", "BetaRedactedThinkingBlockParam"),
        ("tool_use", "BetaToolUseBlockParam"),
        ("image", "BetaImageBlockParam"),
        ("document", "BetaBase64PDFBlockParam"),
    )
    resolved: Dict[str, frozenset] = dict(_INPUT_BLOCK_FIELDS_FALLBACK)
    try:
        import anthropic.types.beta as _beta_mod
    except ImportError:
        return resolved
    for block_type, cls_name in _PARAM_REGISTRY:
        cls = getattr(_beta_mod, cls_name, None)
        if cls is None:
            continue
        annotations = getattr(cls, "__annotations__", None)
        if not annotations:
            continue
        resolved[block_type] = frozenset(annotations.keys())
    return resolved


_INPUT_BLOCK_FIELDS: Dict[str, frozenset] = _build_input_block_fields()


def _sanitize_block_for_anthropic_input(block: Dict[str, Any]) -> Dict[str, Any]:
    """Strip response-only fields from a captured response block so it round-trips.

    Anthropic's response models (e.g. BetaTextBlock) carry fields not present
    on the corresponding input param models (e.g. BetaTextBlockParam).
    Replaying a response block verbatim trips the input validator with
    HTTP 400 "Extra inputs are not permitted" on those extra fields.
    Allowlist to known-good input fields per block type; pass through
    unknown types unchanged so a new block type added by Anthropic doesn't
    silently get stripped before this map is updated.
    """
    btype = block.get("type")
    allowed = _INPUT_BLOCK_FIELDS.get(btype) if isinstance(btype, str) else None
    if allowed is None:
        # Unknown type — let it through; a block type added by Anthropic
        # is never silently stripped before this map learns about it.
        return block
    sanitized = {k: v for k, v in block.items() if k in allowed}
    # Strip citations from text blocks. Citations with encrypted_index reference
    # Anthropic's server-side web search results — sending them without the
    # corresponding web_search_tool_result block causes Anthropic to try to
    # validate the reference and 400 with "unexpected tool_use_id found in
    # web_search_tool_result blocks". The text content is complete without
    # citations metadata; removing it is safe for all replay scenarios.
    if btype == "text":
        sanitized.pop("citations", None)
    return sanitized


def _convert_content_to_anthropic(content: Any) -> Any:
    """Convert OpenAI-style multimodal content arrays to Anthropic blocks."""
    if not isinstance(content, list):
        return content

    converted = []
    for part in content:
        block = _convert_content_part_to_anthropic(part)
        if block is not None:
            converted.append(block)
    return converted


def _content_parts_to_anthropic_blocks(parts: Any) -> List[Dict[str, Any]]:
    """Convert OpenAI-style tool-message content parts → Anthropic tool_result inner blocks.

    Used for multimodal tool results (e.g. computer_use screenshots). Each
    part is normalized via `_convert_content_part_to_anthropic`, then
    filtered to the block types Anthropic tool_result accepts (text + image).
    """
    if not isinstance(parts, list):
        return []
    out: List[Dict[str, Any]] = []
    for part in parts:
        block = _convert_content_part_to_anthropic(part)
        if not block:
            continue
        btype = block.get("type")
        if btype == "text":
            text_val = block.get("text")
            if isinstance(text_val, str) and text_val:
                out.append({"type": "text", "text": text_val})
        elif btype == "image":
            src = block.get("source")
            if isinstance(src, dict) and src:
                out.append({"type": "image", "source": src})
    return out


_EMPTY_TEXT_PLACEHOLDER = "(empty)"


def _safe_text(text: Any) -> str:
    """Return ``text`` if it's non-whitespace, else a non-whitespace placeholder.

    The Anthropic Messages API rejects requests where a text content block is
    empty or whitespace-only (HTTP 400 "text content blocks must contain
    non-whitespace text"). When such a block gets stored in session history —
    e.g. produced by context compression — it is replayed verbatim on every
    subsequent turn, permanently wedging the session. Coercing to a
    non-whitespace placeholder is self-healing: the next API call recovers.

    Mirrors ``bedrock_adapter._safe_text`` (#9486); ref #69512.
    """
    if text is None:
        return _EMPTY_TEXT_PLACEHOLDER
    if not isinstance(text, str):
        text = str(text)
    return text if text.strip() else _EMPTY_TEXT_PLACEHOLDER


def _sanitize_replay_block(b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Strip output-only fields from a stored Anthropic content block so it is
    valid as REQUEST input on replay.

    The SDK response objects carry output-only attributes that the Messages
    *input* schema forbids ("Extra inputs are not permitted"): text blocks get
    ``parsed_output``/``citations`` (when null), tool_use blocks get ``caller``,
    etc. ``normalize_response`` captured blocks verbatim via ``_to_plain_data``,
    so these leak back as input on the next turn → HTTP 400.

    Explicit reconstruction for the common types below (needs custom logic —
    e.g. tool_id sanitizing, dropping empty redacted_thinking). Any OTHER type
    (``server_tool_use``, ``web_search_tool_result``, ``tool_search_tool_*``,
    future SDK server-tool blocks, …) falls through to
    ``_sanitize_block_for_anthropic_input`` — the SAME generic, SDK-derived
    allowlist used for tool_result inner blocks — which fails OPEN (passes an
    unrecognized type through unchanged) rather than dropping it.

    This used to fail CLOSED here (return None for anything not in a tiny
    hardcoded list), which silently erased server-side tool evidence
    (server_tool_use / web_search_tool_result) from the persisted
    ``anthropic_content_blocks`` column with no trace — the exact thing that
    made a real invisible-cost-multiplier bug (native web_search causing a
    second Anthropic-side inference pass, root-caused 2026-07-24, session
    20260723_211736_99ee22, warm memory fact 1486) look "not backed by any
    evidence" when the DB was queried for it. Fail-open here matches the
    already-correct contract of ``_sanitize_block_for_anthropic_input`` so a
    future server-tool type doesn't inherit the same blind spot.
    """
    if not isinstance(b, dict):
        return None
    btype = b.get("type")
    if btype == "text":
        text_val = b.get("text", "")
        # Bedrock and strict Anthropic-compatible endpoints reject text
        # blocks where "text" is empty or whitespace-only (#69512). Drop the
        # blank block (the caller relocates any cache_control it carried and
        # falls back to a non-whitespace placeholder when nothing survives)
        # rather than coercing in place — a coerced "(empty)" block would be
        # model-visible noise next to surviving thinking/tool_use blocks.
        # Type-safe: captured blocks can carry text=None from an invalid
        # upstream payload, which a bare .strip() would crash on.
        if not isinstance(text_val, str) or not text_val.strip():
            return None
        out: Dict[str, Any] = {"type": "text", "text": text_val}
        # citations is input-valid ONLY when it's a non-empty list; the SDK
        # emits citations=None on responses, which the input schema rejects.
        cits = b.get("citations")
        if isinstance(cits, list) and cits:
            out["citations"] = cits
        if isinstance(b.get("cache_control"), dict):
            out["cache_control"] = b["cache_control"]
        return out
    if btype == "thinking":
        out = {"type": "thinking", "thinking": b.get("thinking", "")}
        if b.get("signature"):
            out["signature"] = b["signature"]
        return out
    if btype == "redacted_thinking":
        # Only valid with its data payload; drop if missing.
        return {"type": "redacted_thinking", "data": b["data"]} if b.get("data") else None
    if btype == "tool_use":
        out = {
            "type": "tool_use",
            "id": _sanitize_tool_id(b.get("id", "")),
            "name": b.get("name", ""),
            "input": b.get("input", {}),
        }
        if isinstance(b.get("cache_control"), dict):
            out["cache_control"] = b["cache_control"]
        return out
    if btype == "image":
        src = b.get("source")
        return {"type": "image", "source": src} if isinstance(src, dict) else None
    # Any other type (including server_tool_use / web_search_tool_result /
    # tool_search_tool_*_tool_result and anything future SDK versions add):
    # delegate to the generic, SDK-derived allowlist rather than dropping.
    # _sanitize_block_for_anthropic_input already passes genuinely unknown
    # types through unchanged, so this can never be MORE lossy than the old
    # fail-closed behavior — only strictly less so.
    return _sanitize_block_for_anthropic_input(b)


def _apply_assistant_cache_control_to_last_cacheable_block(
    blocks: List[Dict[str, Any]],
    cache_control: Any,
) -> None:
    if not isinstance(cache_control, dict):
        return
    for block in reversed(blocks):
        if isinstance(block, dict) and block.get("type") in {"text", "tool_use"}:
            block.setdefault("cache_control", dict(cache_control))
            break


def _convert_assistant_message(m: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an assistant message to Anthropic content blocks.

    Handles thinking blocks, regular content, tool calls, and
    reasoning_content injection for Kimi/DeepSeek endpoints.
    """
    content = m.get("content", "")
    # Anthropic interleaved-thinking fast path: when this turn carries a
    # verbatim, order-preserving block list (set by normalize_response only
    # for turns that interleave SIGNED thinking with tool_use), replay it.
    # Each block is run through _sanitize_replay_block to strip output-only
    # SDK fields (parsed_output, caller, citations=None, …) that the Messages
    # INPUT schema forbids — replaying them verbatim caused HTTP 400 "Extra
    # inputs are not permitted" (text.parsed_output). Block ORDER is preserved
    # (the reason this channel exists); only forbidden sibling fields are
    # dropped, leaving thinking signatures and tool_use id/name/input intact.
    ordered_blocks = m.get("anthropic_content_blocks")
    if isinstance(ordered_blocks, list) and ordered_blocks:
        # Re-source each tool_use input from the stored tool_calls map rather
        # than the captured block. The ordered-blocks list captures tool_use
        # input from the RAW API response (normalize_response), which is NOT
        # credential-redacted; tool_calls[].function.arguments IS redacted at
        # storage time (build_assistant_message, #19798). Replaying the raw
        # block input would resurrect a secret the model inlined into a tool
        # call (e.g. terminal(command="curl -H 'Authorization: Bearer sk-...'")
        # onto the wire, even though the same value is redacted everywhere else
        # in history. Keying by sanitized tool id preserves interleave order
        # (the reason this channel exists) while swapping in the redacted
        # input. Adapted from #36071 (replay-time tool-input re-sourcing).
        redacted_input_by_id: Dict[str, Any] = {}
        for tc in m.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) or {}
            raw_args = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, ValueError):
                parsed_args = {}
            redacted_input_by_id[_sanitize_tool_id(tc.get("id", ""))] = parsed_args
        replayed: List[Dict[str, Any]] = []
        _relocated_replay_cache_control = None
        _dropped_blank_text = False
        for b in ordered_blocks:
            clean = _sanitize_replay_block(b)
            if clean is None:
                if isinstance(b, dict) and b.get("type") == "text":
                    _dropped_blank_text = True
                if isinstance(b, dict) and isinstance(b.get("cache_control"), dict):
                    # A dropped blank text block can still carry the cache
                    # breakpoint marker -- relocate it rather than losing it.
                    _relocated_replay_cache_control = b["cache_control"]
                continue
            if clean.get("type") == "tool_use":
                # Override raw (un-redacted) input with the redacted copy when
                # we have one for this id; fall back to the sanitized block
                # input only if the tool_call is missing (shape mismatch).
                redacted = redacted_input_by_id.get(clean.get("id", ""))
                if redacted is not None:
                    clean["input"] = redacted
            replayed.append(clean)
        # When every text block was blank and nothing cacheable survived
        # (e.g. signed thinking + a blank text block, or a SOLE blank
        # cache-marked block), emit the non-whitespace placeholder so the
        # replayed message stays schema-valid (#69512) and a relocated cache
        # marker still has a carrier instead of being silently lost.
        _has_cacheable_replay = any(
            isinstance(b, dict) and b.get("type") in {"text", "tool_use"}
            for b in replayed
        )
        if not _has_cacheable_replay and (
            _dropped_blank_text or _relocated_replay_cache_control is not None
        ):
            replayed.append({"type": "text", "text": _EMPTY_TEXT_PLACEHOLDER})
        if replayed:
            if _relocated_replay_cache_control is not None:
                _apply_assistant_cache_control_to_last_cacheable_block(
                    replayed, _relocated_replay_cache_control
                )
            _apply_assistant_cache_control_to_last_cacheable_block(
                replayed, m.get("cache_control")
            )
            # apply_anthropic_cache_control marks an assistant turn with
            # non-empty text by writing cache_control INTO ``content`` (see
            # _apply_cache_marker's list branch), not at the top level. This
            # branch rebuilds the message from ordered_blocks and never reads
            # ``content``, so that marker would be dropped -- and because
            # _can_carry_marker already counted this message as a carrier, the
            # breakpoint is burned rather than relocated. #56195 covered the
            # complementary shape (blank content -> top-level marker); this is
            # the interleaved thinking + preamble-text + tool_use shape.
            _inline_cc = None
            _msg_content = m.get("content")
            if isinstance(_msg_content, list):
                for _blk in _msg_content:
                    if isinstance(_blk, dict) and isinstance(
                        _blk.get("cache_control"), dict
                    ):
                        _inline_cc = _blk["cache_control"]
                        break
            if _inline_cc is not None:
                _apply_assistant_cache_control_to_last_cacheable_block(
                    replayed, _inline_cc
                )
            return {"role": "assistant", "content": replayed}

    blocks = _extract_preserved_thinking_blocks(m)
    # Cache markers dropped along with a blank block are relocated onto the
    # last surviving cacheable block below (via
    # _apply_assistant_cache_control_to_last_cacheable_block), rather than
    # lost -- prompt_caching.py's _apply_cache_marker() sets cache_control
    # directly on content[-1] for list content, so if that last part happens
    # to be blank text, dropping it silently would lose the breakpoint.
    _relocated_cache_control = None
    if content:
        if isinstance(content, list):
            converted_content = _convert_content_to_anthropic(content)
            if isinstance(converted_content, list):
                # Bedrock and strict Anthropic-compatible endpoints reject
                # text blocks where "text" is empty or whitespace-only. The
                # ordered-replay path enforces the same invariant via
                # _sanitize_replay_block(). Type-safe against ANY invalid
                # "text" value from an upstream payload -- None, or a
                # truthy non-string like an int -- not just None: checking
                # isinstance() first (rather than `blk.get("text") or ""`)
                # means a non-string value is treated as blank/invalid
                # instead of reaching .strip() and raising AttributeError.
                for blk in converted_content:
                    _blk_text = blk.get("text") if isinstance(blk, dict) else None
                    if (
                        isinstance(blk, dict)
                        and blk.get("type") == "text"
                        and (not isinstance(_blk_text, str) or not _blk_text.strip())
                    ):
                        if isinstance(blk.get("cache_control"), dict):
                            _relocated_cache_control = blk["cache_control"]
                        continue
                    blocks.append(blk)
        else:
            # Scalar (non-list) content: a whitespace-only string is the
            # same invalid-payload case as an empty list block -- drop it
            # rather than emitting a blank text block.
            text_str = str(content)
            if text_str.strip():
                blocks.append({"type": "text", "text": text_str})
    for tc in m.get("tool_calls", []):
        if not tc or not isinstance(tc, dict):
            continue
        fn = tc.get("function", {})
        args = fn.get("arguments", "{}")
        try:
            parsed_args = json.loads(args) if isinstance(args, str) else args
        except (json.JSONDecodeError, ValueError):
            parsed_args = {}
        blocks.append({
            "type": "tool_use",
            "id": _sanitize_tool_id(tc.get("id", "")),
            "name": fn.get("name", ""),
            "input": parsed_args,
        })
    # Kimi's /coding endpoint (Anthropic protocol) requires assistant
    # tool-call messages to carry reasoning_content when thinking is
    # enabled server-side.  Preserve it as a thinking block so Kimi
    # can validate the message history.  See hermes-agent#13848.
    #
    # Accept empty string "" — _copy_reasoning_content_for_api()
    # injects "" as a tier-3 fallback for Kimi tool-call messages
    # that had no reasoning.  Kimi requires the field to exist, even
    # if empty.
    #
    # Prepend (not append): Anthropic protocol requires thinking
    # blocks before text and tool_use blocks.
    #
    # Guard: only add when reasoning_details didn't already contribute
    # thinking blocks.  On native Anthropic, reasoning_details produces
    # signed thinking blocks — adding another unsigned one from
    # reasoning_content would create a duplicate (same text) that gets
    # downgraded to a spurious text block on the last assistant message.
    reasoning_content = m.get("reasoning_content")
    _already_has_thinking = any(
        isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"}
        for b in blocks
    )
    if isinstance(reasoning_content, str) and not _already_has_thinking:
        blocks.insert(0, {"type": "thinking", "thinking": reasoning_content})
    # Anthropic rejects empty assistant content. IMPORTANT: fall back only
    # to the placeholder, never to the raw `content` variable -- `content`
    # is the UNFILTERED original message content, and can itself be exactly
    # the blank/whitespace-only payload the filtering above just removed
    # (a sole blank text block, or scalar whitespace with no tool_calls).
    # `blocks or content` there would silently restore the invalid provider
    # payload this function exists to prevent (#69512).
    effective = blocks if blocks else [{"type": "text", "text": _EMPTY_TEXT_PLACEHOLDER}]
    # Applied here (after the empty-fallback resolution) rather than
    # earlier against `blocks` directly, so a cache_control relocated from
    # a dropped blank block that was the ONLY block still lands on the
    # (empty) placeholder instead of being silently lost when blocks was
    # empty at the point the marker would otherwise have been applied.
    if _relocated_cache_control is not None:
        _apply_assistant_cache_control_to_last_cacheable_block(
            effective, _relocated_cache_control
        )
    _apply_assistant_cache_control_to_last_cacheable_block(
        effective, m.get("cache_control")
    )
    return {"role": "assistant", "content": effective}


def _convert_tool_message_to_result(
    result: List[Dict[str, Any]], m: Dict[str, Any]
) -> None:
    """Convert a tool message to an Anthropic tool_result, merging consecutive
    results into one user message.

    Mutates ``result`` in place — either appends a new user message or extends
    the trailing user message's tool_result list.
    """
    content = m.get("content", "")
    multimodal_blocks: Optional[List[Dict[str, Any]]] = None
    if isinstance(content, dict) and content.get("_multimodal"):
        multimodal_blocks = _content_parts_to_anthropic_blocks(
            content.get("content") or []
        )
        # Fallback text if the conversion produced nothing usable.
        if not multimodal_blocks and content.get("text_summary"):
            multimodal_blocks = [
                {"type": "text", "text": str(content["text_summary"])}
            ]
    elif isinstance(content, list):
        converted = _content_parts_to_anthropic_blocks(content)
        if any(b.get("type") == "image" for b in converted):
            multimodal_blocks = converted
    # Back-compat: some callers stash blocks under a private key.
    if multimodal_blocks is None:
        stashed = m.get("_anthropic_content_blocks")
        if isinstance(stashed, list) and stashed:
            text_content = content if isinstance(content, str) and content.strip() else None
            multimodal_blocks = (
                [{"type": "text", "text": text_content}] + stashed
                if text_content else list(stashed)
            )

    if multimodal_blocks:
        result_content: Any = multimodal_blocks
    elif isinstance(content, str):
        result_content = content
    else:
        result_content = json.dumps(content) if content else "(no output)"
    if not result_content:
        result_content = "(no output)"
    tool_result = {
        "type": "tool_result",
        "tool_use_id": _sanitize_tool_id(m.get("tool_call_id", "")),
        "content": result_content,
    }
    if isinstance(m.get("cache_control"), dict):
        tool_result["cache_control"] = dict(m["cache_control"])
    # Merge consecutive tool results into one user message
    if (
        result
        and result[-1]["role"] == "user"
        and isinstance(result[-1]["content"], list)
        and result[-1]["content"]
        and result[-1]["content"][0].get("type") == "tool_result"
    ):
        result[-1]["content"].append(tool_result)
    else:
        result.append({"role": "user", "content": [tool_result]})


def _convert_user_message(content: Any) -> Dict[str, Any]:
    """Validate and convert a user message to anthropic format."""
    if isinstance(content, list):
        converted_blocks = _convert_content_to_anthropic(content)
        kept_blocks = _fix_blank_text_blocks_in_list(
            converted_blocks,
            placeholder_text="(empty message)",
            msg_index=-1,
            role="user",
            location="_convert_user_message",
        )
        return {"role": "user", "content": kept_blocks}
    else:
        if not content or (isinstance(content, str) and not content.strip()):
            content = "(empty message)"
        return {"role": "user", "content": content}


def _strip_orphaned_tool_blocks(result: List[Dict[str, Any]]) -> None:
    """Strip tool_use blocks with no matching tool_result, and vice versa.

    Context compression or session truncation can remove either side of a
    tool-call pair, or insert messages between a tool_use and its result.
    Anthropic requires each tool_use to have a matching tool_result in the
    IMMEDIATELY FOLLOWING user message — a global ID match is not enough.
    Mutates ``result`` in place.
    """
    # Pass 1: For each assistant message with tool_use blocks, check that
    # EACH tool_use ID has a matching tool_result in the immediately following
    # user message.  Strip tool_use blocks that lack an adjacent result —
    # Anthropic rejects non-adjacent pairs with HTTP 400 even when the IDs
    # match somewhere later in the conversation.
    for i, m in enumerate(result):
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        tool_use_ids_in_turn = {
            b.get("id")
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
        }
        if not tool_use_ids_in_turn:
            continue

        # Collect result IDs from the immediately following user message only.
        adjacent_result_ids: set = set()
        if i + 1 < len(result):
            nxt = result[i + 1]
            if nxt.get("role") == "user" and isinstance(nxt.get("content"), list):
                for block in nxt["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        adjacent_result_ids.add(block.get("tool_use_id"))

        orphaned = tool_use_ids_in_turn - adjacent_result_ids
        if not orphaned:
            continue

        kept = [
            b
            for b in m["content"]
            if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in orphaned)
        ]
        # If stripping an orphaned tool_use mutated a turn that also carries a
        # signed thinking block, that block's Anthropic signature was computed
        # against the ORIGINAL (un-stripped) turn content and is now invalid.
        # Anthropic rejects the replayed turn with HTTP 400 "thinking blocks in
        # the latest assistant message cannot be modified".  Flag the turn so
        # _manage_thinking_signatures can demote the dead signature instead of
        # replaying it verbatim.  See hermes-agent: extended-thinking + parallel
        # tool batch interrupted mid-flight → non-retryable 400 crash-loop.
        if len(kept) != len(m["content"]) and any(
            isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"}
            for b in m["content"]
        ):
            m["_thinking_signature_invalidated"] = True
        m["content"] = kept if kept else [{"type": "text", "text": "(tool call removed)"}]

    # Pass 2: Rebuild the set of tool_use IDs that survived pass 1, then
    # strip tool_result blocks that no longer have any matching tool_use
    # anywhere in the conversation.
    surviving_tool_use_ids: set = set()
    for m in result:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    surviving_tool_use_ids.add(block.get("id"))

    for m in result:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        new_content = [
            b
            for b in m["content"]
            if not (isinstance(b, dict) and b.get("type") == "tool_result")
            or b.get("tool_use_id") in surviving_tool_use_ids
        ]
        if len(new_content) != len(m["content"]):
            m["content"] = new_content if new_content else [{"type": "text", "text": "(tool result removed)"}]


def _merge_consecutive_roles(result: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge consecutive same-role messages to enforce Anthropic alternation.

    Returns a new list (caller must rebind ``result``).
    """
    fixed = []
    for m in result:
        if fixed and fixed[-1]["role"] == m["role"]:
            if m["role"] == "user":
                prev_content = fixed[-1]["content"]
                curr_content = m["content"]
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    fixed[-1]["content"] = prev_content + "\n" + curr_content
                elif isinstance(prev_content, list) and isinstance(curr_content, list):
                    fixed[-1]["content"] = prev_content + curr_content
                else:
                    if isinstance(prev_content, str):
                        prev_content = [{"type": "text", "text": prev_content}]
                    if isinstance(curr_content, str):
                        curr_content = [{"type": "text", "text": curr_content}]
                    fixed[-1]["content"] = prev_content + curr_content
            else:
                # Consecutive assistant messages — merge text content.
                # Propagate the orphan-strip signature-invalidation flag onto the
                # surviving (prev) dict so _manage_thinking_signatures still sees it.
                if m.get("_thinking_signature_invalidated"):
                    fixed[-1]["_thinking_signature_invalidated"] = True
                # Drop thinking blocks from the *second* message: their
                # signature was computed against a different turn boundary
                # and becomes invalid once merged.
                if isinstance(m["content"], list):
                    m["content"] = [
                        b for b in m["content"]
                        if not (isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"})
                    ]
                prev_blocks = fixed[-1]["content"]
                curr_blocks = m["content"]
                if isinstance(prev_blocks, list) and isinstance(curr_blocks, list):
                    fixed[-1]["content"] = prev_blocks + curr_blocks
                elif isinstance(prev_blocks, str) and isinstance(curr_blocks, str):
                    fixed[-1]["content"] = prev_blocks + "\n" + curr_blocks
                else:
                    if isinstance(prev_blocks, str):
                        prev_blocks = [{"type": "text", "text": prev_blocks}]
                    if isinstance(curr_blocks, str):
                        curr_blocks = [{"type": "text", "text": curr_blocks}]
                    fixed[-1]["content"] = prev_blocks + curr_blocks
        else:
            fixed.append(m)
    return fixed


def _manage_thinking_signatures(
    result: List[Dict[str, Any]], base_url: str | None, model: str | None
) -> None:
    """Strip or preserve thinking blocks based on endpoint type.

    Anthropic signs thinking blocks against the full turn content.
    Any upstream mutation (context compression, session truncation, orphan
    stripping, message merging) invalidates the signature, causing HTTP 400
    "Invalid signature in thinking block".

    Signatures are Anthropic-proprietary.  Third-party endpoints (MiniMax,
    Azure AI Foundry, AWS Bedrock, self-hosted proxies) cannot validate them
    and will reject them outright.  Kimi's /coding and DeepSeek's /anthropic
    endpoints speak the Anthropic protocol upstream but require unsigned
    thinking blocks (synthesised from ``reasoning_content``) to round-trip on
    replayed assistant tool-call messages.  See hermes-agent#13848 (Kimi) and
    hermes-agent#16748 (DeepSeek).

    Nous Portal's ``/v1/messages`` route is the exception among third-party
    hosts: it proxies Claude to Anthropic/Vertex/Bedrock and validates the
    same signed thinking blocks.  Sticky ``session_id`` keeps a conversation
    on one upstream instance so those signatures stay warm — stripping them
    here would 400 the first tool-loop turn ("thinking must be passed back").
    Portal therefore takes the native Anthropic replay path below.

    Mutates ``result`` in place.
    """
    _THINKING_TYPES = frozenset(("thinking", "redacted_thinking"))
    # Portal speaks Anthropic's thinking contract end-to-end; do not treat it
    # as a signature-blind proxy even though the host is not anthropic.com.
    _is_third_party = (
        _is_third_party_anthropic_endpoint(base_url)
        and not _is_nous_portal_endpoint(base_url)
    )

    last_assistant_idx = None
    for i in range(len(result) - 1, -1, -1):
        if result[i].get("role") == "assistant":
            last_assistant_idx = i
            break

    for idx, m in enumerate(result):
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue

        if _is_kimi_family_endpoint(base_url, model):
            # Kimi does not enforce thinking signatures — replay as-is
            # (shared cleanup below still strips cache markers + the internal flag).
            pass
        elif _is_deepseek_anthropic_endpoint(base_url):
            # DeepSeek: strip signed, preserve unsigned.
            new_content = []
            for b in m["content"]:
                if not isinstance(b, dict) or b.get("type") not in _THINKING_TYPES:
                    new_content.append(b)
                    continue
                if b.get("signature") or b.get("data"):
                    # Signed (or redacted-with-data) — upstream can't validate, strip.
                    continue
                new_content.append(b)
            m["content"] = new_content or [{"type": "text", "text": "(empty)"}]
        elif _is_third_party or idx != last_assistant_idx:
            # Third-party: strip ALL thinking blocks (signatures are proprietary).
            # Direct Anthropic: strip from non-latest assistant messages only.
            stripped = [
                b for b in m["content"]
                if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)
            ]
            m["content"] = stripped or [{"type": "text", "text": "(thinking elided)"}]
        else:
            # Latest assistant on direct Anthropic: keep signed, downgrade unsigned
            # to text so the reasoning isn't lost.
            #
            # Exception: if orphan-stripping (or another structural mutation) removed
            # a tool_use block from THIS turn, every thinking signature on it was
            # computed against the original turn content and is now dead.  Anthropic
            # rejects the turn either way — replaying the signed block 400s with
            # "thinking blocks in the latest assistant message cannot be modified",
            # and a bare signed block with no following tool_use is also invalid.
            # Demote ALL thinking blocks on this turn to text so the turn replays
            # cleanly and the model can re-plan from the surviving tool results.
            signature_dead = bool(m.get("_thinking_signature_invalidated"))
            new_content = []
            for b in m["content"]:
                if not isinstance(b, dict) or b.get("type") not in _THINKING_TYPES:
                    new_content.append(b)
                    continue
                if signature_dead:
                    thinking_text = b.get("thinking", "")
                    if thinking_text:
                        new_content.append({"type": "text", "text": thinking_text})
                    continue
                if b.get("type") == "redacted_thinking":
                    # Redacted blocks use 'data' for the signature payload —
                    # drop the block when 'data' is missing (can't be validated).
                    if b.get("data"):
                        new_content.append(b)
                elif b.get("signature"):
                    new_content.append(b)
                else:
                    thinking_text = b.get("thinking", "")
                    if thinking_text:
                        new_content.append({"type": "text", "text": thinking_text})
            m["content"] = new_content or [{"type": "text", "text": "(empty)"}]

        # Strip cache_control from any remaining thinking/redacted_thinking
        # blocks — cache markers interfere with signature validation.
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") in _THINKING_TYPES:
                b.pop("cache_control", None)

        # Drop the internal bookkeeping flag — it must never reach the API payload.
        m.pop("_thinking_signature_invalidated", None)


def _evict_old_screenshots(result: List[Dict[str, Any]]) -> None:
    """Keep only the most recent ``_MAX_KEEP_IMAGES`` computer-use screenshots.

    Base64 images cost ~1,465 tokens each and accumulate across tool calls.
    Walk backward, keep the most recent N, replace older ones with a placeholder.

    Mutates ``result`` in place.
    """
    _MAX_KEEP_IMAGES = 3
    _image_count = 0
    for msg in reversed(result):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if not isinstance(inner, list):
                continue
            has_image = any(
                isinstance(b, dict) and b.get("type") == "image"
                for b in inner
            )
            if not has_image:
                continue
            _image_count += 1
            if _image_count > _MAX_KEEP_IMAGES:
                block["content"] = [
                    b if b.get("type") != "image"
                    else {"type": "text", "text": "[screenshot removed to save context]"}
                    for b in inner
                ]


def _ensure_leading_user_turn(result: List[Dict[str, Any]]) -> None:
    """Anthropic requires messages[0] to have role=user.

    After a second context compaction on the auto path the summary can be
    emitted as role=assistant with nothing in front of it (the system prompt
    lives outside messages[] or is extracted into the separate ``system``
    param), so messages[0] ends up assistant and the Messages API rejects
    the request with HTTP 400 — often masked by a misleading
    "tool_use ids were found without tool_result blocks" error (#52160).

    Mirror the Bedrock Converse adapter, which unconditionally prepends a
    minimal user turn when the first message is not user
    (convert_messages_to_converse).

    The inserted text block must be non-whitespace: Anthropic separately
    rejects any text content block whose text is empty or whitespace-only
    ("text content blocks must contain non-whitespace text"), so a single
    space here traded the "leading assistant turn" 400 for that one (#69512
    class). Uses the same placeholder as every other synthesized filler
    block in this module for consistency.
    """
    if result and result[0].get("role") != "user":
        result.insert(
            0, {"role": "user", "content": [{"type": "text", "text": _EMPTY_TEXT_PLACEHOLDER}]}
        )


def _fix_blank_text_blocks_in_list(
    blocks: List[Any],
    *,
    placeholder_text: str,
    msg_index: int,
    role: Any,
    location: str,
) -> List[Any]:
    """Drop blank/whitespace-only text blocks from ``blocks``, in place logic.

    Non-text blocks (tool_use, tool_result, image, document, thinking, …)
    and the relative order of everything else are left untouched. A
    cache_control marker riding on a dropped block is relocated onto the
    last surviving text/tool_use block so a breakpoint is never silently
    lost. If nothing survives, a single non-blank placeholder text block
    takes the dropped blocks' place (carrying the relocated cache_control,
    if any) so the message never has empty content.

    Returns a new list; does not mutate ``blocks``.
    """
    kept: List[Any] = []
    relocated_cache_control = None
    for block_index, blk in enumerate(blocks):
        if (
            isinstance(blk, dict)
            and blk.get("type") == "text"
            and not (isinstance(blk.get("text"), str) and blk["text"].strip())
        ):
            if isinstance(blk.get("cache_control"), dict):
                relocated_cache_control = blk["cache_control"]
            logger.warning(
                "Pre-call sanitizer: dropped blank text content block "
                "(message_index=%d role=%s location=%s block_index=%d "
                "block_type=text)",
                msg_index,
                role,
                location,
                block_index,
            )
            continue
        kept.append(blk)
    if not kept:
        placeholder: Dict[str, Any] = {"type": "text", "text": placeholder_text}
        if relocated_cache_control is not None:
            placeholder["cache_control"] = relocated_cache_control
        kept.append(placeholder)
    elif relocated_cache_control is not None:
        _apply_assistant_cache_control_to_last_cacheable_block(kept, relocated_cache_control)
    return kept


def _scrub_blank_text_blocks(result: List[Dict[str, Any]]) -> None:
    """Final provider-boundary guard against blank Anthropic text blocks.

    Anthropic rejects any text content block whose ``text`` is empty or
    whitespace-only with HTTP 400 ("text content blocks must contain
    non-whitespace text"). ``_convert_assistant_message``,
    ``_convert_user_message`` and ``_ensure_leading_user_turn`` already
    avoid emitting these for the paths that build them, but this pass runs
    last — after every other transform in ``convert_messages_to_anthropic``
    — so a blank block from any current or future producer (including one
    nested inside a ``tool_result``'s own content list) never reaches the
    wire. Diagnostics are structural only: message index, role, content
    location, block index/type. Never logs message text, tool arguments,
    tokens, or credentials. Mutates ``result`` in place.
    """
    for msg_index, msg in enumerate(result):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list) or not content:
            continue
        placeholder_text = _EMPTY_TEXT_PLACEHOLDER if role == "assistant" else "(empty message)"
        new_content = _fix_blank_text_blocks_in_list(
            content,
            placeholder_text=placeholder_text,
            msg_index=msg_index,
            role=role,
            location="content",
        )
        for blk in new_content:
            if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                continue
            inner = blk.get("content")
            if isinstance(inner, list) and inner:
                blk["content"] = _fix_blank_text_blocks_in_list(
                    inner,
                    placeholder_text="(no output)",
                    msg_index=msg_index,
                    role=role,
                    location="tool_result",
                )
        msg["content"] = new_content


def _apply_tool_search(
    anthropic_tools: List[Dict[str, Any]],
    tool_search_config: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply the client-side tool_search deferral policy to the converted tools array.

    Stubs are regular tools (no ``defer_loading`` flag), and the model discovers
    tools via the client-side ``hermes_load_tools`` tool registered in
    ``tools/hermes_load_tools.py`` and dispatched out of the agent loop.  Each
    load step is a normal client-side round-trip — billed once per call, no
    multiplier.  Names in ``promoted_tools`` skip the stub and ship their full
    schema.

    (The legacy ``server_side`` mode — Anthropic's ``tool_search_tool_<variant>``
    server tool with ``defer_loading`` stubs — was retired 2026-09-25 with the
    rest of the fork's Anthropic server-tool cluster.)

    Deferral policy (additive, evaluated in order):
      1. ``additional_deferred`` — exact tool names always deferred.
      2. ``additional_eager`` — exact tool names always eager (overrides 1).
      3. ``defer_mcp_tools`` — when True, any tool whose name starts with
         a known MCP server prefix is deferred.  Server prefixes are
         passed via ``tool_search_config["mcp_server_prefixes"]``.

    Returns the transformed list.  Returns the input unchanged when
    tool_search is disabled, when there are no tools, or when all/none of the
    tools would be deferred (a stub array with no full tools is unhelpful).
    """
    if not tool_search_config or not tool_search_config.get("enabled"):
        return anthropic_tools
    if not anthropic_tools:
        return anthropic_tools

    eager_names = set(tool_search_config.get("additional_eager") or [])
    deferred_names = set(tool_search_config.get("additional_deferred") or [])
    mcp_prefixes = tuple(tool_search_config.get("mcp_server_prefixes") or [])
    defer_mcp = bool(tool_search_config.get("defer_mcp_tools", True))
    # client_side mode only — names the model has already loaded this
    # session.  Promoted names skip the stub branch and ship their full
    # schema even when the policy would otherwise defer them.
    promoted_tools = set(tool_search_config.get("promoted_tools") or ())

    def _should_defer(name: str) -> bool:
        if name in eager_names:
            return False
        if name in deferred_names:
            return True
        if defer_mcp and mcp_prefixes and name.startswith(mcp_prefixes):
            return True
        return False

    # Build the stub used for deferred entries.  Anthropic's validator
    # requires ``description`` and ``input_schema`` to exist even on
    # name-only entries, so we send minimal placeholders (empty
    # description, ``{"type":"object"}``).  Each stub stays under ~120
    # bytes on the wire vs 1-5KB for a real schema.
    def _make_stub(name: str, original: Dict[str, Any]) -> Dict[str, Any]:
        stub: Dict[str, Any] = {
            "name": name,
            "description": (
                "Stubbed MCP tool — call hermes_load_tools with this "
                "name to load the full schema."
            ),
            "input_schema": {"type": "object"},
        }
        # Preserve cache_control if the caller had set it; it affects
        # prompt-caching boundary placement and is cheap.
        if "cache_control" in original:
            stub["cache_control"] = original["cache_control"]
        return stub

    transformed: List[Dict[str, Any]] = []
    deferred_count = 0
    eager_count = 0
    for tool in anthropic_tools:
        name = tool.get("name", "")
        if _should_defer(name) and name not in promoted_tools:
            transformed.append(_make_stub(name, tool))
            deferred_count += 1
        else:
            transformed.append(tool)
            eager_count += 1

    # Anthropic returns 400 when every tool is deferred (no eager tool to
    # ground the deferral). Skip injection in that case.  Also skip when
    # nothing is deferred (no benefit).
    if deferred_count == 0 or eager_count == 0:
        return anthropic_tools

    return transformed


def _normalize_to_mcp_wire(name: str) -> str:
    """OAuth wire form of a tool name (no aliasing): ``mcp__<...>``. Anthropic's OAuth billing
    classifier treats a single-underscore ``mcp_`` tool name as a third-party-app fingerprint
    (HTTP 400 "Third-party apps now draw from extra usage"); ``mcp__foo`` is accepted. Both bare
    Hermes tools (``read_file``) and native MCP tools registered as ``mcp_<server>_<tool>`` must
    land on the double-underscore form. normalize_response reverses both via registry lookup."""
    if name.startswith("mcp__"):
        return name  # already correct, don't double-prefix
    return _MCP_TOOL_PREFIX + name.removeprefix("mcp_")


def build_anthropic_kwargs(
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]],
    max_tokens: Optional[int],
    reasoning_config: Optional[Dict[str, Any]],
    tool_choice: Optional[str] = None,
    is_oauth: bool = False,
    preserve_dots: bool = False,
    context_length: Optional[int] = None,
    base_url: str | None = None,
    fast_mode: bool = False,
    drop_context_1m_beta: bool = False,
    tool_search_config: Optional[Dict[str, Any]] = None,
    cache_tools: bool = False,
    cache_ttl: str = "5m",
) -> Dict[str, Any]:
    """Build kwargs for ``client.beta.messages.{create,stream}``.

    Naming note — two distinct concepts, easily confused:
      max_tokens     = OUTPUT token cap for a single response.
                       Anthropic's API calls this "max_tokens" but it only
                       limits the *output*.  Anthropic's own native SDK
                       renamed it "max_output_tokens" for clarity.
      context_length = TOTAL context window (input tokens + output tokens).
                       The API enforces: input_tokens + max_tokens ≤ context_length.
                       Stored on the ContextCompressor; reduced on overflow errors.

    When *max_tokens* is None the model's native output ceiling is used
    (e.g. 128K for Opus 4.6, 64K for Sonnet 4.6).

    When *context_length* is provided and the model's native output ceiling
    exceeds it (e.g. a local endpoint with an 8K window), the output cap is
    clamped to context_length − 1.  This only kicks in for unusually small
    context windows; for full-size models the native output cap is always
    smaller than the context window so no clamping happens.
    NOTE: this clamping does not account for prompt size — if the prompt is
    large, Anthropic may still reject the request.  The caller must detect
    "max_tokens too large given prompt" errors and retry with a smaller cap
    (see parse_available_output_tokens_from_error + _ephemeral_max_output_tokens).

    When *is_oauth* is True, applies Claude Code compatibility transforms:
    system prompt prefix, tool name prefixing, and prompt sanitization.

    When *preserve_dots* is True, model name dots are not converted to hyphens
    (for Alibaba/DashScope anthropic-compatible endpoints: qwen3.5-plus).

    When *base_url* points to a third-party Anthropic-compatible endpoint,
    thinking block signatures are stripped (they are Anthropic-proprietary).

    When *fast_mode* is True, sets typed ``speed="fast"`` and adds the
    fast-mode beta to the per-request ``betas`` list for ~2.5x faster output
    throughput on Opus 4.6. Native Anthropic only — third-party gateways
    don't recognize the speed parameter.

    Output kwargs assume ``client.beta.messages.{create,stream}``: typed
    fields ``thinking``, ``output_config``, ``context_management``, ``betas``,
    ``speed`` all land on the wire as top-level body fields.
    """
    system, anthropic_messages = convert_messages_to_anthropic(
        messages, base_url=base_url, model=model
    )
    anthropic_tools = convert_tools_to_anthropic(tools) if tools else []

    # Drop / rewrite tool_use blocks for tools that aren't in the live tool
    # list — Anthropic's API hard-rejects them with
    #   invalid_request_error: Tool reference 'X' not found in available tools
    # See _strip_unknown_tool_blocks for the full list of triggering
    # scenarios (MCP reconnect failures, mid-session toolset switches,
    # subagents with narrower toolsets).  We do this here, AFTER tools
    # are converted, so the lookup set reflects exactly what's going on
    # the wire (post-server-tool unwrap, post-dedup).
    available_tool_names = {
        t.get("name") for t in anthropic_tools if isinstance(t, dict) and t.get("name")
    }
    anthropic_messages = _strip_unknown_tool_blocks(
        anthropic_messages, available_tool_names
    )

    # Nous Portal routes on its own catalog ids (``anthropic/claude-opus-4.8``);
    # normalizing to the bare Anthropic slug would make the model unresolvable
    # there. Skipping the call preserves the prefix AND the dots, so
    # ``preserve_dots`` stays irrelevant for Portal.
    if not _is_nous_portal_endpoint(base_url):
        model = normalize_model_name(model, preserve_dots=preserve_dots)
    # effective_max_tokens = output cap for this call (≠ total context window)
    # Use the resolver helper so non-positive values (negative ints,
    # fractional floats, NaN, non-numeric) fail locally with a clear error
    # rather than 400-ing at the Anthropic API. See openclaw/openclaw#66664.
    effective_max_tokens = _resolve_anthropic_messages_max_tokens(
        max_tokens, model, context_length=context_length
    )

    # Clamp output cap to fit inside the total context window.
    # Only matters for small custom endpoints where context_length < native
    # output ceiling.  For standard Anthropic models context_length (e.g.
    # 200K) is always larger than the output ceiling (e.g. 128K), so this
    # branch is not taken.
    if context_length and effective_max_tokens > context_length:
        effective_max_tokens = max(context_length - 1, 1)

    # ── OAuth: Claude Code identity ──────────────────────────────────
    # _to_oauth_wire_name is defined inside this block (needs anthropic_tools
    # in scope to compute _claimed_wire_names) but is also invoked later for
    # tool_choice, well outside the block. Every call site below guards on
    # `is_oauth` first, so it is always bound by the time it's used.
    if is_oauth:
        # 1. Prepend Claude Code system prompt identity
        cc_block = {"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX}
        if isinstance(system, list):
            system = [cc_block] + system
        elif isinstance(system, str) and system:
            system = [cc_block, {"type": "text", "text": system}]
        else:
            system = [cc_block]

        # 2. Sanitize system prompt — replace product name references
        #    to avoid Anthropic's server-side content filters.
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                text = text.replace("Hermes Agent", "Claude Code")
                text = text.replace("Hermes agent", "Claude Code")
                # Upstream's identifier-safe slug rewrite: only a standalone prose word is
                # renamed — an address the model dereferences (``NousResearch/hermes-agent``,
                # ``~/.hermes/hermes-agent/venv``, a path or mailbox) must survive verbatim
                # (#48860). Supersedes the fork's naive ``str.replace("hermes-agent", ...)``.
                text = _OAUTH_SLUG_PATTERN.sub("claude-code", text)
                text = text.replace("Nous Research", "Anthropic")
                block["text"] = _apply_oauth_prose_aliases(text)  # upstream: prose-safe aliases only

        # 3. Normalize tool names so NOTHING goes on the OAuth wire with a
        #    single-underscore ``mcp_`` prefix.  Anthropic's subscription/OAuth
        #    billing classifier treats a single-underscore ``mcp_`` tool name as
        #    a third-party-app fingerprint and rejects the request with HTTP 400
        #    "Third-party apps now draw from extra usage, not plan limits"
        #    (verified empirically: a single ``mcp_foo`` tool flips a request
        #    from plan-billing to the extra-usage lane; ``mcp__foo`` is accepted).
        #
        #    Two cases, both must land on the double-underscore ``mcp__`` form:
        #      a) bare Hermes-native tools (``read_file``)  -> ``mcp__read_file``
        #      b) native MCP server tools registered under their full
        #         single-underscore ``mcp_<server>_<tool>`` name
        #         (``mcp_linear_get_issue``) -> ``mcp__linear_get_issue``
        #    Case (b) is the gap that the bare ``mcp_``->``mcp__`` constant swap
        #    left open: those tools were *skipped* and stayed single-underscore,
        #    so any session with an MCP server configured still tripped the
        #    classifier. normalize_response reverses both forms via registry
        #    lookup so the dispatcher still sees the original name. GH-25255.
        #
        # Upstream (2026-09 sync) aliases two tools whose schema/name the billing
        # classifier fingerprints on their own (session_search, memory): each is aliased
        # (when the alias isn't already claimed) and then mcp__-normalized.
        _claimed_wire_names = {
            _normalize_to_mcp_wire(t["name"]) for t in (anthropic_tools or []) if isinstance(t, dict) and t.get("name")
        }

        def _to_oauth_wire_name(name: str) -> str:
            aliased = _OAUTH_TOOL_NAME_ALIASES.get(name)
            if aliased and _MCP_TOOL_PREFIX + aliased not in _claimed_wire_names:
                name = aliased
            return _normalize_to_mcp_wire(name)

        if anthropic_tools:
            for tool in anthropic_tools:
                if "name" in tool:
                    tool["name"] = _to_oauth_wire_name(tool["name"])
                if isinstance(tool.get("description"), str):
                    tool["description"] = _apply_oauth_prose_aliases(tool["description"])

        # Apply the same normalization to tool names in message history
        # (tool_use blocks) so replayed turns match the wire names above.
        for msg in anthropic_messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use" and "name" in block:
                            block["name"] = _to_oauth_wire_name(block["name"])
                        elif block.get("type") == "tool_result" and "tool_use_id" in block:
                            pass  # tool_result uses ID, not name

        # 4. system_prompt_mode=compact: move everything past the CC prefix
        #    into a preamble block on the first user message.
        #
        #    Anthropic's billing classifier on personal Max plans rejects
        #    OAuth requests whose ``system`` extends beyond the official
        #    Claude Code identity prefix — they get routed to "extra
        #    usage" billing and 400 with a misleading
        #    "out of extra usage" error. Mirroring Claude Code's
        #    --exclude-dynamic-system-prompt-sections flag, we keep only
        #    the CC prefix in ``system`` and ride everything dynamic on
        #    the conversation. Behavior is unchanged (the model still
        #    sees the same content); only the placement moves.
        #
        #    Cache control: if the moved blocks carried cache_control
        #    markers, we preserve them on the preamble block so prompt
        #    caching continues to work across turns.
        if _system_prompt_mode_compact() and isinstance(system, list) and len(system) > 1:
            tail_blocks = system[1:]
            system = [system[0]]
            tail_text_parts = []
            tail_cache_control = None
            for blk in tail_blocks:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text":
                    txt = blk.get("text", "")
                    if txt:
                        tail_text_parts.append(txt)
                # Inherit the strongest cache_control found on the moved
                # blocks (last write wins — typical pattern is a single
                # ephemeral marker on the final static block).
                cc = blk.get("cache_control")
                if cc:
                    tail_cache_control = cc
            if tail_text_parts:
                preamble = {
                    "type": "text",
                    "text": "\n\n".join(tail_text_parts),
                }
                if tail_cache_control:
                    preamble["cache_control"] = tail_cache_control
                anthropic_messages = _prepend_user_message_preamble(
                    anthropic_messages, preamble
                )


    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": effective_max_tokens,
    }

    if system:
        kwargs["system"] = system

    if anthropic_tools:
        anthropic_tools = _apply_tool_search(anthropic_tools, tool_search_config)
        if cache_tools:
            from agent.prompt_caching import apply_anthropic_tools_cache_control
            anthropic_tools = apply_anthropic_tools_cache_control(
                anthropic_tools, cache_ttl=cache_ttl
            )
        kwargs["tools"] = anthropic_tools
        # Map OpenAI tool_choice to Anthropic format
        if tool_choice == "auto" or tool_choice is None:
            # Mirror Claude Code: omit tool_choice (the API treats absent as
            # "auto", so we save bytes and match CC's wire shape exactly).
            pass
        elif tool_choice == "required":
            kwargs["tool_choice"] = {"type": "any"}
        elif tool_choice == "none":
            # Anthropic has no tool_choice "none" — omit tools entirely to prevent use
            kwargs.pop("tools", None)
        elif isinstance(tool_choice, str):
            # Specific tool name. On the OAuth wire every tools[] entry is
            # mcp__-prefixed/CC-aliased above, so a forced tool_choice must go
            # through the same mapping or it names a tool that no longer
            # exists on the wire (Anthropic 400s, or worse, silently targets
            # the wrong tool if a stale non-prefixed name happens to
            # collide). Mirrors upstream's to_wire(tool_choice) composition
            # in build_anthropic_kwargs.
            wire_name = _to_oauth_wire_name(tool_choice) if is_oauth else tool_choice
            kwargs["tool_choice"] = {"type": "tool", "name": wire_name}

    # Map reasoning_config to Anthropic's thinking parameter.
    # Claude 4.6+ models use adaptive thinking + output_config.effort.
    # Older models use manual thinking with budget_tokens.
    # MiniMax Anthropic-compat endpoints support thinking (manual mode only,
    # not adaptive).  Haiku does NOT support extended thinking — skip entirely.
    #
    # Kimi / Moonshot models also use adaptive thinking: their
    # Anthropic-compatible endpoints (api.moonshot.cn/anthropic,
    # api.kimi.com/coding) accept ``thinking.type="adaptive"`` +
    # ``output_config.effort``, and the replay-validation 400s that
    # originally motivated dropping the parameter (#13848) no longer
    # occur.  (Kimi on chat_completions enables thinking via extra_body
    # in the ChatCompletionsTransport — see #13503.)
    #
    # On 4.7+ ``thinking.display`` defaults to "omitted" (no summary text
    # generated). Previously hermes set "summarized" to keep the activity
    # feed populated, but verified via binary inspection 2026-05-06 that
    # Claude Code DOES NOT set ``display`` — it accepts the omitted default.
    # Multi-minute "queued/prefilling" stalls hermes was hitting that
    # Claude Code didn't correlate with this difference: producing a
    # summary forces the model to generate extra tokens after thinking
    # before the visible output streams, magnifying any internal-thinking
    # latency.  Match Claude Code's wire shape — let display default.
    # See ``HERMES_THINKING_DISPLAY=summarized`` env var to opt back in
    # if the activity feed UX matters more than latency parity.
    # When reasoning_config is unset, default to enabling adaptive thinking
    # at medium effort on Anthropic-native + adaptive-supporting models.
    # Mirrors Claude Code 2.1.119 wire shape (verified by mitmdump capture
    # 2026-05-06: every /v1/messages call sends thinking={type:"adaptive"}
    # + output_config.effort).  Without this default, the entire
    # thinking/output_config block below was a no-op for callers that
    # don't explicitly pass reasoning_config — i.e. nearly every default
    # session — leaving the interleaved-thinking + effort betas dormant.
    if reasoning_config is None and _supports_adaptive_thinking(model):
        reasoning_config = {"enabled": True, "effort": "medium"}
    if reasoning_config and isinstance(reasoning_config, dict):
        if reasoning_config.get("enabled") is False:
            # "Thinking off". Adaptive models think by DEFAULT, so omitting the
            # parameter is not a disable — it silently leaves thinking on and
            # the user keeps paying for it. Send the disable explicitly.
            # Mandatory-thinking models reject it with a 400, so they keep the
            # omission: a silently-ignored disable beats a dead turn.
            if _accepts_thinking_disable(model):
                kwargs["thinking"] = {"type": "disabled"}
        elif "haiku" not in model.lower():
            effort = str(reasoning_config.get("effort", "medium")).lower()
            budget = THINKING_BUDGET.get(effort, 8000)
            if _supports_adaptive_thinking(model):
                _thinking_cfg: Dict[str, Any] = {"type": "adaptive"}
                _display_override = os.environ.get(
                    "HERMES_THINKING_DISPLAY", ""
                ).strip().lower()
                if _display_override in {"summarized", "verbose", "all", "omitted"}:
                    _thinking_cfg["display"] = _display_override
                kwargs["thinking"] = _thinking_cfg
                adaptive_effort = ADAPTIVE_EFFORT_MAP.get(effort, "medium")
                # Downgrade xhigh on models that don't support it. Claude Code
                # falls back to "high" for non-4.7 models (verified by
                # disassembling its 2.1.119 binary: `return"xhigh";return"high"`).
                # Don't fall back to "max" — Sonnet 4.6 and Haiku 4.5 don't
                # support max either (Opus-tier only), so the previous
                # "downgrade to max" path 400'd on Sonnet/Haiku requests.
                if adaptive_effort == "xhigh" and not _supports_xhigh_effort(model):
                    adaptive_effort = "high"
                kwargs["output_config"] = {
                    "effort": adaptive_effort,
                }
                # Mirror Claude Code 2.1.119: every /v1/messages call carries
                # ``context_management`` with the clear_thinking_20251015 edit
                # set to keep:"all".  Activates the server-side thinking-block
                # lifecycle so cached thinking-blocks survive across turns
                # (paired with redact-thinking-2026-02-12 +
                # context-management-2025-06-27 betas).  Native Anthropic only
                # — third-party gateways don't recognize the field.  Typed
                # kwarg in client.beta.messages.* (Anthropic SDK 0.100+).
                if not _is_third_party_anthropic_endpoint(base_url):
                    kwargs["context_management"] = {
                        "edits": [
                            {"type": "clear_thinking_20251015", "keep": "all"},
                        ],
                    }
            else:
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                # Anthropic requires temperature=1 when thinking is enabled on older models
                kwargs["temperature"] = 1
                kwargs["max_tokens"] = max(effective_max_tokens, budget + 4096)

    # ── Strip sampling params on 4.7+ ─────────────────────────────────
    # Opus 4.7 rejects any non-default temperature/top_p/top_k with a 400.
    # Callers (auxiliary_client, etc.) may set these for older models;
    # drop them here as a safety net so upstream 4.6 → 4.7 migrations
    # don't require coordinated edits everywhere.
    if _forbids_sampling_params(model):
        for _sampling_key in ("temperature", "top_p", "top_k"):
            kwargs.pop(_sampling_key, None)

    # ── Fast mode (Opus 4.6 only) ────────────────────────────────────
    # Sets typed ``speed="fast"`` + adds the fast-mode beta to the
    # per-request ``betas`` list for ~2.5x output speed.  Per Anthropic
    # docs, fast mode is only supported on Opus 4.6 — Opus 4.7 and other
    # models 400 on the speed parameter.
    # Only for native Anthropic endpoints — third-party providers would
    # reject the unknown beta header and speed parameter.
    if (
        fast_mode
        and not _is_third_party_anthropic_endpoint(base_url)
        and _supports_fast_mode(model)
    ):
        # Typed ``speed`` kwarg in client.beta.messages.* (SDK 0.100+).
        kwargs["speed"] = "fast"
        # Per-request betas list overrides the client-level
        # default_headers["anthropic-beta"] for this call.
        betas = list(_common_betas_for_base_url(
            base_url,
            drop_context_1m_beta=drop_context_1m_beta,
            model=model,
        ))
        if is_oauth:
            betas.extend(_OAUTH_ONLY_BETAS)
        betas.append(_FAST_MODE_BETA)
        kwargs["betas"] = betas

    # ── Server-side tool beta headers ────────────────────────────────
    # ── 1M context tier gate (DEFAULT OFF) ───────────────────────────
    # Background: hermes hits sporadic multi-minute stalls on Opus 4.7
    # even with perfect cache hits. Theory was that opting into
    # ``context-1m-2025-08-07`` routes requests to a smaller, slower-
    # served 1M-context model fleet vs the standard 200K tier.
    #
    # Why disabled by default (2026-05-06): the gate uses request-body
    # size to decide, but the relevant size is the running CONTEXT
    # (cached prefix + new tokens), which can be much larger than the
    # body bytes we send (cached prefix is server-side). Adam's
    # workflows regularly run 600K+ of cached context — those genuinely
    # need the 1M beta even though each individual request body is small.
    # Stripping the beta in that case would either break cache continuity
    # or fail outright (200K context can't hold a 600K prefix).
    #
    # Set ``HERMES_CONTEXT_1M_THRESHOLD_TOKENS`` to a positive integer to
    # enable the gate at that body-size threshold. Use only when you're
    # confident the running context (not just the body) fits in 200K.
    try:
        _threshold = int(os.environ.get(
            "HERMES_CONTEXT_1M_THRESHOLD_TOKENS", "0"
        ))
    except (TypeError, ValueError):
        _threshold = 0
    if (
        _threshold > 0
        and not _requires_bearer_auth(base_url)
        and _model_supports_1m_context(model)
    ):
        # Cheap byte-based prompt estimate — char/4 is the standard
        # rough conversion. Tools count too.
        _est_chars = 0
        sys_obj = kwargs.get("system")
        if sys_obj is not None:
            try:
                _est_chars += len(json.dumps(sys_obj))
            except Exception:
                pass
        _msgs = kwargs.get("messages")
        if isinstance(_msgs, list):
            try:
                _est_chars += len(json.dumps(_msgs))
            except Exception:
                pass
        _tools_for_estimate = kwargs.get("tools")
        if isinstance(_tools_for_estimate, list):
            for _t in _tools_for_estimate:
                try:
                    _est_chars += len(json.dumps(_t))
                except Exception:
                    pass
        _est_tokens = _est_chars // 4
        if _est_tokens < _threshold:
            prior = list(kwargs.get("betas") or [])
            if not prior:
                # No prior per-request override — start from the same
                # base set the client would otherwise send. Then strip
                # context-1m and emit as a per-request override.
                prior = list(_common_betas_for_base_url(
                    base_url,
                    drop_context_1m_beta=False,
                    model=model,
                ))
                if is_oauth:
                    prior.extend(_OAUTH_ONLY_BETAS)
            stripped = [b for b in prior if b != _CONTEXT_1M_BETA]
            if len(stripped) != len(prior):
                kwargs["betas"] = stripped


    return kwargs


_OAUTH_SYSTEM_REPLACEMENTS = (
    ("Hermes Agent", "Claude Code"), ("Hermes agent", "Claude Code"), ("Nous Research", "Anthropic"),
)
# The slug is rewritten only as a standalone prose word. Joined to a host, path, repo, mailbox
# or quoted as an identifier (``hermes-agent.nousresearch.com``, ``~/.hermes/hermes-agent/venv``,
# ``NousResearch/hermes-agent``, ``skill_view(name='hermes-agent')``) it is an address the model
# dereferences, and the rewritten form does not exist (#48860). The OPENING quote marks an
# identifier; a sentence-final ``.`` or a possessive ``'s`` is prose.
_OAUTH_SLUG_PATTERN = re.compile(r"""(?<![\w./:@'"`-])hermes-agent(?![\w/@-]|\.\w)""")


def _thinking_kwargs(reasoning_config: Dict[str, Any], model: str, effective_max_tokens: int) -> Dict[str, Any]:
    """Map ``reasoning_config`` to Anthropic thinking kwargs. Adaptive models (Claude 4.6+,
    Kimi/Moonshot) get ``thinking.type=adaptive`` + ``output_config.effort``; older models and
    manual-only compat endpoints (MiniMax) get budget_tokens. Haiku has no extended thinking. On
    4.7+ ``thinking.display`` defaults to "omitted", hiding the reasoning Hermes shows in its CLI,
    so "summarized" is requested to keep the activity feed populated."""
    if reasoning_config.get("enabled") is False:
        # Adaptive models think by DEFAULT, so omitting the parameter is not a disable — the user
        # silently keeps paying. Mandatory-thinking models 400 on the disable, so they keep the
        # omission: a silently-ignored disable beats a dead turn.
        return {"thinking": {"type": "disabled"}} if _accepts_thinking_disable(model) else {}
    if "haiku" in model.lower():
        return {}
    effort = str(reasoning_config.get("effort", "medium")).lower()
    if _supports_adaptive_thinking(model):
        adaptive_effort = ADAPTIVE_EFFORT_MAP.get(effort, "medium")
        if adaptive_effort == "xhigh" and not _supports_xhigh_effort(model):
            adaptive_effort = "max"
        return {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": adaptive_effort}}
    budget = THINKING_BUDGET.get(effort, 8000)
    return {
        "thinking": {"type": "enabled", "budget_tokens": budget},
        "temperature": 1,  # required when thinking is enabled on older models
        "max_tokens": max(effective_max_tokens, budget + 4096),
    }


# OpenAI tool_choice -> Anthropic; any other string is a forced tool name.
_TOOL_CHOICE_MAP = {None: {"type": "auto"}, "auto": {"type": "auto"}, "required": {"type": "any"}}


# Keys exclusive to the OpenAI Responses / Codex shape; the Messages SDK raises ``TypeError: ...
# unexpected keyword argument`` on any of them.
_RESPONSES_ONLY_KWARGS = frozenset({"instructions", "input", "store", "parallel_tool_calls"})


def sanitize_anthropic_kwargs(api_kwargs: Any, *, log_prefix: str = "") -> Any:
    """Drop Responses-API-only keys before an Anthropic Messages SDK call. Boundary guard for
    api_mode-flip races (a concurrent auxiliary call mutating a shared agent between kwargs build
    and dispatch): a Responses-shaped payload reaching ``messages.stream()`` dies with a
    non-retryable TypeError that takes the whole turn and fallback chain with it. Mutates and
    returns ``api_kwargs``; logs a WARNING so the race stays visible."""
    leaked = _RESPONSES_ONLY_KWARGS.intersection(api_kwargs) if isinstance(api_kwargs, dict) else ()
    if leaked:
        for key in leaked:
            del api_kwargs[key]
        logger.warning(
            "%sStripped Responses-only kwarg(s) %s from an Anthropic Messages "
            "call (api_mode flip race — see #31673). The call will proceed; "
            "this breadcrumb means a kwargs build ran under a Responses "
            "api_mode while dispatch ran under anthropic_messages.",
            log_prefix,
            sorted(leaked),
        )
    return api_kwargs


def buffer_anthropic_tool_input(api_kwargs: dict[str, Any], base_url: str | None) -> None:
    """Retry knob for a malformed fine-grained tool-JSON stream (#107830): the beta streams tool
    args unvalidated, so a model that emits ``{"names": cronjob_manage}`` breaks the SDK parser
    and an identical retry breaks identically. ``eager_input_streaming: false`` per tool restores
    Anthropic's buffered, validated args for the rest of this turn (the flag lives on the turn's
    kwargs, so a later retry of the same turn keeps it; the changed ``tools`` block costs one
    prompt-cache miss, cheaper than a dead turn). Off the happy path on purpose:
    buffering a large payload is a zero-event gap the stale-stream detector kills. No-op on
    endpoints that never get the beta (MiniMax) rather than sending them an unknown field."""
    if _TOOL_STREAMING_BETA not in _common_betas_for_base_url(base_url):
        return
    for tool in api_kwargs.get("tools") or ():
        tool["eager_input_streaming"] = False


def _is_stream_unavailable_error(exc: Exception) -> bool:
    """True when an Anthropic stream call should fall back to create()."""
    err_lower = str(exc).lower()
    if "stream" in err_lower and "not supported" in err_lower:
        return True
    if "invokemodelwithresponsestream" not in err_lower:
        return False
    from agent.bedrock_adapter import is_streaming_access_denied_error
    return is_streaming_access_denied_error(exc)


def _stream_final_message(stream_fn, api_kwargs, log_prefix, on_stream_event, on_response):
    """``messages.stream()`` -> final Message, ticking the best-effort callbacks."""
    with stream_fn(**{k: v for k, v in api_kwargs.items() if k != "stream"}) as stream:
        if callable(on_response):
            try:
                on_response(getattr(stream, "response", None))
            except Exception:
                logger.debug("%son_response callback failed", log_prefix, exc_info=True)
        # Consume manually so each event ticks the progress callback; get_final_message then
        # returns the accumulated snapshot. TimeoutError is the caller's deadline seam: the host
        # has given up, so abandon the stream (``with`` closes it) instead of streaming an answer
        # nobody reads.
        # Some SDK versions drop optional message_delta metadata from the final snapshot.
        # Non-iterable shims (get_final_message-only) skip straight to the snapshot.
        stop_details = None
        for event in (stream if isinstance(stream, Iterable) else ()):
            if getattr(event, "type", None) == "message_delta":
                details = getattr(getattr(event, "delta", None), "stop_details", None)
                if details is not None:
                    stop_details = details
            if not callable(on_stream_event):
                continue
            try:
                on_stream_event(event)
            except TimeoutError:
                # The callback is the caller's deadline seam (#99692: the host waiting on this summary has
                # already given up). Abandon the stream — the ``with`` closes it — instead of streaming an
                # answer nobody will read.
                raise
            except Exception:
                logger.debug("%son_stream_event callback failed", log_prefix, exc_info=True)
        message = stream.get_final_message()
        if stop_details is not None:
            message.stop_details = stop_details
        return message


def _coerce_positive_seconds(raw: Any) -> Optional[float]:
    """Coerce a seconds value to a positive float, or ``None``.

    ``None``, non-numeric, and non-positive values all mean "no bound" — a
    deadline is only ever applied when the caller passed a usable one.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def create_anthropic_message(
    client: Any,
    api_kwargs: dict,
    *,
    log_prefix: str = "",
    prefer_stream: bool = True,
    on_stream_event=None,
    on_response=None,
    total_ceiling: Optional[float] = None,
    no_progress_timeout: Optional[float] = None,
    is_progress_event=None,
) -> Any:
    """Create an Anthropic message, aggregating via stream when available. Some Anthropic-compatible
    gateways are SSE-only and answer ``create()`` with ``text/event-stream``, which the SDK surfaces
    as raw text (callers then crash on ``.content``), so prefer ``messages.stream()`` like the main
    turn path and fall back to ``create()`` only for providers that explicitly don't support
    streaming (restricted Bedrock roles). Both callbacks are best-effort and fire only on the
    streaming path: ``on_stream_event(event)`` lets liveness watchdogs see forward progress;
    ``on_response(httpx_response)`` exposes headers the parsed Message drops (Nous Portal's
    ``x-nous-credits-*`` balance family).

    FORK — ``total_ceiling`` / ``no_progress_timeout`` + ``is_progress_event``: the SDK/httpx
    ``timeout`` is a per-READ (idle) timeout, not a total budget, and Anthropic emits content-free
    ``ping`` keepalives every ~10-25s during extended thinking, so a stream that will never finish
    is never killed. Two phases, because "no content" does not mean "no progress" on this wire:
      1. BEFORE the first substantive payload, bounded ONLY by ``total_ceiling``.
         ``thinking.display`` defaults to "omitted", so a model reasoning at high/max effort
         legitimately emits nothing but keepalives for minutes (a 60s content window applied from
         the start killed a healthy claude-fable-5-1 call at 253.6s mid-thought, 2026-09-10).
      2. AFTER the first substantive payload, ``no_progress_timeout`` arms and each further
         substantive event re-arms it; keepalive/lifecycle frames do not.
    Both raise ``TimeoutError`` phrased with "timed out" so the auxiliary client's
    ``_is_timeout_error()`` runs the normal provider fallback chain. Both ``None`` (the default)
    preserves the historical unbounded behavior for callers managing their own liveness."""
    sanitize_anthropic_kwargs(api_kwargs, log_prefix=log_prefix)

    # FORK: prefer the ``.beta.messages`` namespace when the client exposes it.
    # The fork's Claude-Code-mimicry path attaches beta-ONLY *body* fields
    # (``context_management``, ``output_config``, ``speed``, ``betas``) that
    # the plain ``.messages.create()/.stream()`` reject with
    # ``TypeError: ... got an unexpected keyword argument 'context_management'``
    # (the betas ride in ``default_headers`` from build_anthropic_client, but
    # the typed body kwargs only exist on ``client.beta.messages.*``). Routing
    # through ``.beta.messages`` accepts them AND keeps upstream's SSE-only
    # stream aggregation. Falls back to ``.messages`` for clients without a
    # ``.beta`` namespace (mocks, non-Anthropic-SDK clients, SDK < 0.100),
    # stripping the beta-only kwargs so the call doesn't TypeError.
    _beta = getattr(client, "beta", None)
    _has_beta_messages = getattr(_beta, "messages", None) is not None
    if not _has_beta_messages:
        # Strip beta-only kwargs when the client doesn't support .beta.messages.
        # The betas still ride in default_headers from build_anthropic_client,
        # so server-side behavior (thinking-block lifecycle, fast mode, etc.)
        # is preserved — only the typed body kwargs are removed.
        for _k in _BETA_ONLY_KWARGS:
            api_kwargs.pop(_k, None)
    messages_api = getattr(_beta, "messages", None) or getattr(client, "messages", None)
    stream_fn = getattr(messages_api, "stream", None)
    if prefer_stream and callable(stream_fn):
        try:
            _ceiling = _coerce_positive_seconds(total_ceiling)
            _idle_window = _coerce_positive_seconds(no_progress_timeout)
            _bounded = _ceiling is not None or _idle_window is not None
            _stream_started = time.monotonic()
            # Armed lazily: stays None until the first substantive payload, so
            # pre-content thinking silence is bounded only by _ceiling. See the
            # two-phase rationale in the docstring.
            _progress_deadline = None
            stream_kwargs = {k: v for k, v in api_kwargs.items() if k != "stream"}
            with stream_fn(**stream_kwargs) as stream:
                if callable(on_response):
                    try:
                        on_response(getattr(stream, "response", None))
                    except Exception:
                        logger.debug(
                            "%son_response callback failed",
                            log_prefix, exc_info=True,
                        )
                if callable(on_stream_event) or _bounded:
                    # Consume the event stream manually so each event can
                    # tick the caller's progress callback; get_final_message
                    # then returns the accumulated snapshot. This loop is
                    # also the only place the deadlines below can be
                    # enforced — the per-read idle timeout inside the SDK is
                    # re-armed by every keepalive ping and so never fires.
                    #
                    # Not every stream object supports iteration: the SDK's
                    # MessageStream does, but Anthropic-compatible shims and
                    # restricted backends may expose only get_final_message().
                    # Those cannot carry per-event deadlines by construction,
                    # so fall through to the aggregate call rather than
                    # raising TypeError on a path that used to work.
                    _iter_fn = getattr(stream, "__iter__", None)
                    if not callable(_iter_fn):
                        logger.debug(
                            "%sAnthropic stream object is not iterable; "
                            "per-event progress/deadline enforcement "
                            "unavailable for this client",
                            log_prefix,
                        )
                        return stream.get_final_message()
                    for _event in stream:
                        if callable(on_stream_event):
                            try:
                                on_stream_event(_event)
                            except TimeoutError:
                                # Upstream: the callback is the caller's deadline seam (#99692) —
                                # abandon the stream instead of finishing an answer nobody reads.
                                raise
                            except Exception:
                                logger.debug(
                                    "%son_stream_event callback failed",
                                    log_prefix, exc_info=True,
                                )
                        if not _bounded:
                            continue
                        _now = time.monotonic()
                        if _ceiling is not None and _now - _stream_started >= _ceiling:
                            raise TimeoutError(
                                f"{log_prefix}Anthropic stream timed out after "
                                f"{_now - _stream_started:.1f}s without completing "
                                f"(total ceiling {_ceiling:.1f}s)"
                            )
                        if _idle_window is None:
                            continue
                        # Only substantive payloads arm/re-arm the window;
                        # keepalive and lifecycle frames deliberately do not.
                        _is_progress = True
                        if callable(is_progress_event):
                            try:
                                _is_progress = bool(is_progress_event(_event))
                            except Exception:
                                logger.debug(
                                    "%sis_progress_event callback failed; "
                                    "treating event as progress",
                                    log_prefix, exc_info=True,
                                )
                                _is_progress = True
                        if _is_progress:
                            # First content also ARMS the window; before this
                            # point silence is extended thinking, not a stall.
                            _progress_deadline = _now + _idle_window
                        elif _progress_deadline is not None and _now >= _progress_deadline:
                            raise TimeoutError(
                                f"{log_prefix}Anthropic stream timed out: "
                                f"content stopped for {_idle_window:.1f}s after "
                                f"starting (no-progress timeout, "
                                f"{_now - _stream_started:.1f}s elapsed)"
                            )
                return stream.get_final_message()
        except TimeoutError:
            raise
        except Exception as exc:
            if not _is_stream_unavailable_error(exc):
                raise
            logger.debug(
                "%sAnthropic Messages stream unavailable; falling back to messages.create(): %s", log_prefix, exc
            )
    return messages_api.create(**{k: v for k, v in api_kwargs.items() if k != "stream"})


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from pathlib import Path  # noqa: F401,F811,E402
from typing import Tuple  # noqa: F401,F811,E402
import copy  # noqa: F401,F811,E402
import json  # noqa: F401,F811,E402
import os  # noqa: F401,F811,E402
import platform  # noqa: F401,F811,E402
import secrets  # noqa: F401,F811,E402
import stat  # noqa: F401,F811,E402
from urllib.parse import urlparse  # noqa: F401,F811,E402


_PLUGIN_COMPAT_LAZY = {
    'CredentialPersistError': ('agent.anthropic_credentials', 'CredentialPersistError'),
    'base_url_host_matches': ('utils', 'base_url_host_matches'),
    'base_url_hostname': ('utils', 'base_url_hostname'),
    'claude_code_credentials_path': ('agent.anthropic_credentials', 'claude_code_credentials_path'),
    'get_hermes_home': ('hermes_constants', 'get_hermes_home'),
    'is_claude_code_token_valid': ('agent.anthropic_credentials', 'is_claude_code_token_valid'),
    'is_rotation_consumed_uncommitted': ('agent.anthropic_credentials', 'is_rotation_consumed_uncommitted'),
    'mark_rotation_consumed_uncommitted': ('agent.anthropic_credentials', 'mark_rotation_consumed_uncommitted'),
    'read_claude_code_credentials': ('agent.anthropic_credentials', 'read_claude_code_credentials'),
    'read_hermes_oauth_credentials': ('agent.anthropic_credentials', 'read_hermes_oauth_credentials'),
    'refresh_anthropic_oauth_pure': ('agent.anthropic_credentials', 'refresh_anthropic_oauth_pure'),
    'resolve_anthropic_token': ('agent.anthropic_credentials', 'resolve_anthropic_token'),
    'run_hermes_oauth_login_pure': ('agent.anthropic_credentials', 'run_hermes_oauth_login_pure'),
    'run_oauth_setup_token': ('agent.anthropic_credentials', 'run_oauth_setup_token'),
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
