"""Claude Code CLI web provider: ``web_search`` / ``web_extract`` through Claude's own WebSearch / WebFetch.

For installs whose only trusted vendor is Anthropic (an enterprise Claude seat) and that want no Brave /
Exa / Firecrawl-style third party to see queries or URLs. Each call runs ONE short-lived ``claude -p``
child restricted to exactly one built-in tool; the chat transport is untouched.

Config (``config.yaml``)::

    web:
      backend: claude-code            # or search_backend / extract_backend
      claude_code:
        model: haiku                  # claude model alias for the helper child (default: haiku)
        timeout_s: 60                 # per call, default 60 (extract: 2x)
        env: {NO_PROXY: "*"}          # extra env for the child (e.g. proxy workarounds), default none

Requirements: ``claude`` on PATH and logged in. The provider is explicit-only: ``is_available()`` is true only
when ``claude`` is on PATH AND a ``web.*backend`` key names ``claude-code``, so merely having the CLI never
routes queries to Anthropic. It never spawns the CLI to check: probing ``claude auth status`` is not free (a
proxy-broken probe can clear the stored login).

Hardening of the child (it reads untrusted web text, so it must have nothing to abuse):
  * ``--tools <one tool>`` + ``--allowedTools <same>``: no Read/Bash/Edit/MCP, so an injected page cannot
    read local files or send them out through the other tool;
  * ``--setting-sources ''`` (no user/project settings), ``--strict-mcp-config`` (no MCP), no slash
    commands, no session persistence, ``--permission-mode dontAsk``;
  * empty temp cwd; ``ANTHROPIC_API_KEY`` & co. stripped so the subscription login is used;
  * telemetry / error-reporting / auto-update / feedback off;
  * the query/URLs go on stdin (never argv), so a leading ``-`` is not a flag;
  * spawns are serialized across processes (one login, one refresh at a time), killed as a process
    group on timeout, and an "Unable to verify organization" failure opens a short circuit breaker so a
    broken proxy cannot turn into a retry storm against the login.

Results: search returns the WebSearch link list (title + url) plus Claude's synthesized ``summary``;
extract returns what WebFetch returns, which is a SUMMARY of each page (WebFetch summarizes with a small
model; it does not return the raw page), flagged ``metadata.summarized``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from plugins.web._common import BaseWebSearchProvider, document, extract_fail, search_fail, search_ok, setup_schema

logger = logging.getLogger(__name__)

NAME = "claude-code"
DEFAULT_MODEL = "haiku"
DEFAULT_TIMEOUT_S = 60
ORG_VERIFY_MARKER = "Unable to verify organization"
BREAKER_SECONDS = 300
LOCK_WAIT_S = 90
MAX_EXTRACT_URLS = 10
# Credentials that would make Claude Code bill an API key instead of using the subscription login.
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY")
_QUIET_ENV = {
    "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
    "DISABLE_FEEDBACK_COMMAND": "1", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
}

_SEARCH_PROMPT = (
    "You are a web search backend. Call WebSearch exactly once, with the user's message as the query "
    "verbatim. Do not call any other tool. After the tool returns, reply with the single word DONE."
)
_EXTRACT_PROMPT = (
    "You are a page-fetch backend. The user's message is a list of URLs, one per line. Call WebFetch exactly "
    "once for each URL, with the prompt: \"Return the page's main content as markdown, keeping headings, "
    "lists, tables and code blocks. Do not add commentary.\" Do not call any other tool and do not follow "
    "links. After the tools return, reply with the single word DONE."
)

_THREAD_LOCK = threading.Lock()
_BREAKER_UNTIL = 0.0


class ClaudeCodeError(Exception):
    """A failure that carries a user-facing message (the call is reported as failed, never raised)."""


# --- config -------------------------------------------------------------------------------------------
def _selected() -> bool:
    """True when ``web.backend`` / ``search_backend`` / ``extract_backend`` names this provider.

    ``is_available()`` is gated on it so that having ``claude`` on PATH never makes the registry pick this
    provider on its own (it would send queries to Anthropic nobody chose to send): it is explicit-only.
    """
    try:
        from hermes_cli.config import load_config
        web = load_config().get("web") or {}
    except Exception as exc:  # health: allow BLE001 -- config load failure means "not selected", never a crash
        logger.debug("claude-code web selection unreadable: %s", exc)
        return False
    return any(str(web.get(key) or "").strip().lower() == NAME for key in ("backend", "search_backend", "extract_backend"))


def _settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        value = (load_config().get("web") or {}).get("claude_code") or {}
        return value if isinstance(value, dict) else {}
    except Exception as exc:  # health: allow BLE001 -- config load failure means defaults, never a crash
        logger.debug("claude-code web settings unreadable: %s", exc)
        return {}


def _timeout(settings: dict[str, Any], factor: int = 1) -> float:
    try:
        return max(5.0, float(settings.get("timeout_s", DEFAULT_TIMEOUT_S))) * factor
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S * factor


def _child_env(settings: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    extra = settings.get("env")
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})
    env.update(_QUIET_ENV)  # last: config cannot turn the quiet flags back on
    return env


def _build_args(binary: str, tool: str, system_prompt: str, settings: dict[str, Any], max_turns: int) -> list[str]:
    model = str(settings.get("model") or DEFAULT_MODEL)
    return [
        binary, "-p", "--model", model, "--output-format", "stream-json", "--verbose",
        "--tools", tool, "--allowedTools", tool,
        "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        "--no-session-persistence", "--permission-mode", "dontAsk",
        "--max-turns", str(max_turns), "--system-prompt", system_prompt,
    ]


# --- spawn serialization ------------------------------------------------------------------------------
def _lock_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "locks" / "claude-code-web.lock"


@contextlib.contextmanager
def _serialized(wait_s: float) -> Iterator[None]:
    """One Claude Code web child at a time per machine: in-process lock, then a file lock across processes."""
    deadline = time.monotonic() + wait_s
    if not _THREAD_LOCK.acquire(timeout=wait_s):
        raise ClaudeCodeError("another claude-code web call is still running (timed out waiting)")
    handle = None
    try:
        try:
            import fcntl
        except ImportError:  # Windows: the in-process lock is all we have
            yield
            return
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ClaudeCodeError("another hermes process is using claude-code web (timed out waiting)")
                time.sleep(0.2)
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()  # closing releases the flock
        _THREAD_LOCK.release()


# --- running the child --------------------------------------------------------------------------------
def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        os.killpg(proc.pid, signal.SIGKILL) if os.name != "nt" else proc.kill()
    with contextlib.suppress(Exception):
        proc.communicate(timeout=5)


def _breaker_open() -> Optional[str]:
    remaining = _BREAKER_UNTIL - time.monotonic()
    if remaining > 0:
        return (
            "Claude Code could not verify the organization a moment ago (on a corp VPN this is usually the proxy "
            f"auto-config); not retrying for {int(remaining)}s so the stored login is not put at risk"
        )
    return None


def _trip_breaker() -> None:
    global _BREAKER_UNTIL
    _BREAKER_UNTIL = time.monotonic() + BREAKER_SECONDS


def _reset_state() -> None:
    """Tests: clear the circuit breaker."""
    global _BREAKER_UNTIL
    _BREAKER_UNTIL = 0.0


def _run(tool: str, system_prompt: str, stdin_text: str, max_turns: int, timeout_factor: int = 1) -> list[dict[str, Any]]:
    """Run one restricted ``claude -p`` child; returns its stream-json events. Raises ClaudeCodeError."""
    binary = shutil.which("claude")
    if not binary:
        raise ClaudeCodeError("claude CLI not found on PATH")
    blocked = _breaker_open()
    if blocked:
        raise ClaudeCodeError(blocked)
    settings = _settings()
    timeout = _timeout(settings, timeout_factor)
    args = _build_args(binary, tool, system_prompt, settings, max_turns)
    with _serialized(LOCK_WAIT_S), tempfile.TemporaryDirectory(prefix="hermes-claude-web-") as cwd:
        blocked = _breaker_open()  # another call may have tripped it while we waited for the lock
        if blocked:
            raise ClaudeCodeError(blocked)
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", cwd=cwd, env=_child_env(settings),
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            raise ClaudeCodeError(f"could not launch claude CLI: {exc}") from exc
        try:
            stdout, stderr = proc.communicate(stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise ClaudeCodeError(f"claude-code {tool} timed out after {int(timeout)}s") from None
    combined = f"{stdout}\n{stderr}"
    if ORG_VERIFY_MARKER in combined:
        _trip_breaker()
        raise ClaudeCodeError(
            "Claude Code could not verify the organization (usually the corp VPN proxy auto-config; "
            "retrying can clear the stored login, so not retrying)"
        )
    events: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    events.append(parsed)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if proc.returncode != 0 and result is None:
        raise ClaudeCodeError(f"claude CLI exited {proc.returncode}: {(stderr or '').strip()[:300] or '(no stderr)'}")
    if result is not None and result.get("is_error") and result.get("subtype") not in (None, "success", "error_max_turns"):
        raise ClaudeCodeError(f"claude CLI reported {result.get('subtype')}: {str(result.get('result') or '')[:300]}")
    return events


# --- parsing ------------------------------------------------------------------------------------------
def _tool_blocks(events: list[dict[str, Any]], name: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """(tool_use id -> input, tool_use id -> {text, is_error, structured}) for tool *name*."""
    uses: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        raw_message = event.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use" and block.get("name") == name:
                tool_input = block.get("input")
                uses[str(block.get("id"))] = tool_input if isinstance(tool_input, dict) else {}
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else "".join(
                    str(part.get("text", "")) for part in content or [] if isinstance(part, dict)
                )
                structured = event.get("tool_use_result")
                results[str(block.get("tool_use_id"))] = {
                    "text": text, "is_error": bool(block.get("is_error")),
                    "structured": structured if isinstance(structured, dict) else {},
                }
    return uses, {k: v for k, v in results.items() if k in uses}


def _search_links(structured: dict[str, Any], text: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for group in structured.get("results") or []:
        for item in (group.get("content") if isinstance(group, dict) else None) or []:
            if isinstance(item, dict) and item.get("url"):
                links.append(item)
    if links or "Links:" not in text:
        return links
    try:  # fall back to the text form: 'Links: [{"title":..., "url":...}, ...]'
        payload, _ = json.JSONDecoder().raw_decode(text.split("Links:", 1)[1].lstrip())
        return [item for item in payload if isinstance(item, dict) and item.get("url")]
    except (ValueError, IndexError):
        return []


def _search_summary(text: str) -> str:
    if "Links:" not in text:
        return ""
    try:
        _, end = json.JSONDecoder().raw_decode(text.split("Links:", 1)[1].lstrip())
    except (ValueError, IndexError):
        return ""
    tail = text.split("Links:", 1)[1].lstrip()[end:]
    return tail.split("REMINDER:")[0].strip()


class ClaudeCodeWebProvider(BaseWebSearchProvider):
    NAME = NAME
    DISPLAY_NAME = "Claude Code"
    KEY_ENV = ""
    EXTRACT = True

    def is_available(self) -> bool:
        """``claude`` on PATH AND explicitly selected in ``web.*backend``; spawn-free by design (see docstring)."""
        return shutil.which("claude") is not None and _selected()

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return search_fail("empty query")
        try:
            events = _run("WebSearch", _SEARCH_PROMPT, query, max_turns=3)
        except ClaudeCodeError as exc:
            logger.warning("claude-code search failed: %s", exc)
            return search_fail(str(exc))
        _, results = _tool_blocks(events, "WebSearch")
        if not results:
            return search_fail("claude-code search produced no WebSearch result (the tool may be disabled by the organization)")
        first = next(iter(results.values()))
        if first["is_error"]:
            return search_fail(f"WebSearch failed: {first['text'][:300]}")
        links = _search_links(first["structured"], first["text"])
        # Title-first row: the same wire shape as brave/searxng/ddgs/tavily/xai (_common.title_hit).
        rows = [
            {"title": str(r.get("title", "")), "url": str(r["url"]), "description": "", "position": i + 1}
            for i, r in enumerate(links[: max(1, int(limit))])
        ]
        out = search_ok(rows)
        summary = _search_summary(first["text"])
        if summary:
            out["data"]["summary"] = summary
        logger.info("claude-code search %r: %d results", query, len(rows))
        return out

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        urls = [str(u).strip() for u in urls or [] if str(u).strip()]
        if not urls:
            return []
        if len(urls) > MAX_EXTRACT_URLS:
            return extract_fail(urls, f"claude-code extract takes at most {MAX_EXTRACT_URLS} URLs per call")
        try:
            events = _run("WebFetch", _EXTRACT_PROMPT, "\n".join(urls), max_turns=len(urls) + 3, timeout_factor=2)
        except ClaudeCodeError as exc:
            logger.warning("claude-code extract failed: %s", exc)
            return extract_fail(urls, str(exc))
        uses, results = _tool_blocks(events, "WebFetch")
        by_url: dict[str, dict[str, Any]] = {}
        for tool_id, result in results.items():
            url = str((uses.get(tool_id) or {}).get("url") or result["structured"].get("url") or "")
            by_url.setdefault(url, result)
        out: list[dict[str, Any]] = []
        for url in urls:
            result = by_url.get(url)
            if result is None:
                out.append({"url": url, "title": "", "content": "", "error": "claude-code fetched no result for this URL"})
            elif result["is_error"] or not result["text"].strip():
                out.append({"url": url, "title": "", "content": "", "error": f"WebFetch failed: {result['text'][:300] or 'empty result'}"})
            else:
                doc = document(url, url, result["text"])
                doc["metadata"].update({"source": NAME, "summarized": True, "bytes": result["structured"].get("bytes")})
                out.append(doc)
        return out

    def get_setup_schema(self) -> dict[str, Any]:
        return setup_schema(
            "Claude Code", "your Claude login · no key", "Claude's own WebSearch / WebFetch via the Claude Code CLI; "
            "queries and URLs go only to Anthropic. Fetch returns a summary of each page.",
        )
