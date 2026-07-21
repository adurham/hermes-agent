"""Auto-generate short session titles from the first user/assistant exchange.

Runs asynchronously after the first response is delivered so it never
adds latency to the user-facing reply.
"""

import logging
import re
import threading
import time
from typing import Callable, Optional

from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# so silent-drops (e.g. OpenRouter 402 exhausting the fallback chain)
# become visible instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]
TitleCallback = Callable[[str], None]

# Validation callback: () -> bool. Called right before the LLM request in
# generate_title(). Return False to skip — e.g. the user switched models
# after this background thread captured its runtime snapshot, and sending
# the request would reload a model the runtime already evicted (#19027).
RuntimeValidator = Callable[[], bool]

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Write the title in the same language the user is writing in. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)

_TITLE_PROMPT_PINNED_LANGUAGE = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Write the title in {language}. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)


def _title_language() -> str:
    """Return configured title language, or empty string to match the user."""
    try:
        from hermes_cli.config import load_config

        return str(
            ((load_config() or {}).get("auxiliary") or {})
            .get("title_generation", {})
            .get("language", "")
        ).strip()
    except Exception:
        return ""


def _extract_text(value) -> str:
    """Coerce any message-content shape to plain text.

    The CLI may pass the user message as either a plain string OR a list
    of content blocks (the OpenAI-style ``[{"type": "text", ...},
    {"type": "image_url", ...}]`` shape) when the user attached files /
    images.  Naively slicing or f-stringing such a list embeds the entire
    base64 image in the title-gen prompt — a single screenshot can blow
    a Haiku request past Anthropic's 200K-token ceiling and turn every
    image-attached session into an "Auxiliary title generation failed:
    prompt is too long" warning.

    This helper:
      - returns ``str(value)`` for strings (fast path),
      - extracts only ``text`` blocks from list-shaped content,
      - drops ``image_url`` / ``image`` / file blocks (they don't
        meaningfully describe the conversation topic for a 3-7 word
        title and they're enormous),
      - stringifies anything else with ``str()`` as a last-resort
        fallback so we never raise from a malformed shape.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                btype = block.get("type")
                # Plain text block (OpenAI / Anthropic shape).
                if btype == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text:
                        parts.append(text)
                    continue
                # Drop binary content (images, files, audio, video, …).
                # The title model would only see opaque base64 anyway.
                if btype in {"image_url", "image", "input_image", "file",
                             "input_file", "audio", "input_audio", "video"}:
                    continue
                # Unknown block type — fall back to its ``text`` field if
                # present, else its name so the title still has a hint.
                fallback = block.get("text") or block.get("name") or ""
                if isinstance(fallback, str) and fallback:
                    parts.append(fallback)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if isinstance(value, dict):
        # Single block dict — recurse via the list path.
        return _extract_text([value])
    return str(value)


def _auto_title_enabled() -> bool:
    """Return whether automatic session title generation is enabled."""
    try:
        # Lazy imports, matching _title_language(): title_generator is imported
        # from agent code paths where a module-level hermes_cli import risks
        # circularity, and the read-only loader avoids config-migration writes.
        from hermes_cli.config import load_config_readonly
        from utils import is_truthy_value

        config = load_config_readonly()
        title_config = (config.get("auxiliary") or {}).get("title_generation") or {}
        return is_truthy_value(title_config.get("enabled"), default=True)
    except Exception:
        logger.debug("Failed to read title_generation.enabled", exc_info=True)
        return True


def generate_title(
    user_message: str,
    assistant_response: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> Optional[str]:
    """Generate a session title from the first exchange.

    Uses the main runtime's model when available, falling back to the
    auxiliary LLM client (cheapest/fastest available model).
    Returns the title string or None on failure.

    ``user_message`` and ``assistant_response`` accept either plain
    strings or content-block lists (as produced by attaching files /
    images in the CLI).  Non-text blocks are stripped before
    truncation so a 339KB base64 image never gets inlined into the
    title-gen prompt and trips Anthropic's 200K-token ceiling.

    ``failure_callback`` is invoked with ``(task, exception)`` when the
    auxiliary call raises — the caller typically wires this to
    ``AIAgent._emit_auxiliary_failure`` so the user sees a warning instead
    of silently accumulating untitled sessions.

    ``runtime_validator`` is called right before the LLM request. If it
    returns False (e.g. the user's model was switched since the background
    thread captured its runtime snapshot), the call is skipped silently —
    no request is sent, so a stale title request can't reload a model the
    runtime already unloaded (#19027).
    """
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return None

    if runtime_validator is not None:
        try:
            if not runtime_validator():
                logger.debug("Title generation skipped: runtime validator returned False")
                return None
        except Exception:
            # Fail open: a broken validator must not disable titling.
            logger.debug("Title runtime validator raised; proceeding", exc_info=True)

    # Coerce list-shaped content (image attachments etc.) to plain text
    # BEFORE truncating — otherwise [:500] slices list elements rather
    # than characters and f-stringing the result inlines the whole image.
    user_text = _extract_text(user_message)
    assistant_text = _extract_text(assistant_response)
    user_snippet = user_text[:500]
    assistant_snippet = assistant_text[:500]

    language = _title_language()
    prompt = _TITLE_PROMPT_PINNED_LANGUAGE.format(language=language) if language else _TITLE_PROMPT

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"User: {user_snippet}\n\nAssistant: {assistant_snippet}"},
    ]

    max_retries = 1
    for attempt in range(max_retries + 1):
        try:
            response = call_llm(
                task="title_generation",
                messages=messages,
                max_tokens=500,
                temperature=0.3,
                timeout=timeout,
                main_runtime=main_runtime,
            )
            content = response.choices[0].message.content or ""
            # Strip thinking/reasoning blocks that think-enabled models
            # (MiniMax M2.7, DeepSeek, etc.) emit even for simple prompts like
            # title generation. Without this the raw  thinking... response XML
            # leaks into session titles. Reuses the canonical scrubber so all
            # tag variants (unterminated blocks, orphan closes, mixed case)
            # are handled, not just a single literal  thinking pair.
            from agent.agent_runtime_helpers import strip_think_blocks
            title = strip_think_blocks(None, content).strip()
            # Clean up: remove quotes, trailing punctuation, prefixes like "Title: "
            title = title.strip('"\'')
            if title.lower().startswith("title:"):
                title = title[6:].strip()
            # Enforce reasonable length
            if len(title) > 80:
                title = title[:77] + "..."
            return title if title else None
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in err_str or "rate limit" in err_str or "quota" in err_str

            if is_rate_limit and attempt < max_retries:
                retry_after = getattr(e, "retry_after", None)
                if retry_after is None:
                    # Look for something like "after 51s." or "after 51.5s"
                    match = re.search(r"after (\d+(?:\.\d+)?)s", err_str)
                    if match:
                        retry_after = float(match.group(1))
                    else:
                        retry_after = 60.0
                logger.info("Title generation rate limited. Retrying in %ss (attempt %d/%d)", retry_after, attempt + 1, max_retries)
                time.sleep(retry_after + 1)  # add 1s buffer
                continue

            # Full detail at debug level for operators who need the stack.
            logger.debug("Title generation traceback", exc_info=True)

            if is_rate_limit:
                # Title generation is a background convenience. If it hits a rate limit
                # (common for tight per-minute quotas like Code Assist), just quietly
                # skip it rather than emitting a prominent UI warning.
                logger.info("Title generation skipped (rate limited): %s", e)
            else:
                # Log at WARNING so this shows up in agent.log without debug mode.
                logger.warning("Title generation failed: %s", e)
                if failure_callback is not None:
                    try:
                        failure_callback("title generation", e)
                    except Exception:
                        logger.debug("Title generation failure_callback raised", exc_info=True)
            return None


def _persist_session_title(session_db, session_id, title):
    """Persist a generated title, recovering from duplicate-title collisions.

    The write goes through ``set_auto_title_if_empty`` (predicate + write in
    one transaction) so a manual ``/title`` set while LLM generation was in
    flight is never overwritten — a plain ``set_session_title`` fallback keeps
    older stores working. ``set_session_title`` raises ValueError when the
    title would collide with another session (the unique-title index). Rather
    than swallow it and leave the session untitled (#50537), append a #N
    suffix via get_next_title_in_lineage() when the store supports lineage
    dedup; otherwise re-raise so the caller can decide.

    Returns the title actually persisted, or None when a concurrent manual
    title won the race (nothing was written).
    """
    atomic_fn = getattr(session_db, "set_auto_title_if_empty", None)

    def _set(t):
        if atomic_fn is not None:
            if not atomic_fn(session_id, t):
                # Predicate failed: a title appeared while generation was in
                # flight (manual /title wins), or the session vanished.
                logger.debug(
                    "Skipping auto-generated session title because a title "
                    "was set while generation was in flight"
                )
                return None
            return t
        ok = session_db.set_session_title(session_id, t)
        if ok is False:
            raise RuntimeError(
                f"session {session_id} not found when storing title"
            )
        return t

    try:
        return _set(title)
    except ValueError:
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        if next_title_fn is None:
            raise
        deduped = next_title_fn(title)
        if not deduped or deduped == title:
            raise
        return _set(deduped)


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Generate and set a session title if one doesn't already exist.

    Called in a background thread after the first exchange completes.
    Silently skips if:
    - session_db is None
    - session already has a title (user-set or previously auto-generated)
    - title generation fails
    - runtime_validator returns False (model was switched)

    Never lets an exception escape: this is a daemon-thread target, and an
    escaping exception would spray a raw traceback into the user's terminal
    via the default threading excepthook. The canonical trigger is the
    post-``hermes update`` stale-module window, where this function's lazy
    imports read NEW source from disk while already-cached modules
    (``agent.portal_tags`` etc.) are still the OLD version — the resulting
    ImportError repeats on every auto-title attempt until the long-running
    process restarts.
    """
    try:
        _auto_title_session(
            session_db,
            session_id,
            user_message,
            assistant_response,
            failure_callback=failure_callback,
            main_runtime=main_runtime,
            title_callback=title_callback,
            runtime_validator=runtime_validator,
        )
    except Exception as e:
        # WARNING (not debug) so operators see it in agent.log; the message
        # names the likely cause so "restart the process" is discoverable.
        logger.warning(
            "Auto-title failed (harmless; if this started after an update, "
            "restart the running Hermes process): %s",
            e,
        )
        logger.debug("Auto-title traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Auto-title failure_callback raised", exc_info=True)


def _auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Body of :func:`auto_title_session` — see its docstring."""
    if not session_db or not session_id:
        return

    # Check if title already exists (user may have set one via /title before first response)
    try:
        existing = session_db.get_session_title(session_id)
        if existing:
            return
    except Exception:
        return

    # This runs on a bare daemon thread spawned AFTER the turn's ambient
    # conversation context was reset, so publish it here from the session id
    # we already hold — the title-generation LLM call then carries the same
    # ``conversation=`` Portal tag as the turn it titles. Root-of-lineage for
    # consistency with the agent loop (a no-op on first exchange, where
    # titling happens, but correct if this ever runs on a continuation).
    from agent.aux_accounting import set_accounting_context
    from agent.portal_tags import set_conversation_context

    conversation_id = session_id
    try:
        conversation_id = session_db.get_conversation_root(session_id) or session_id
    except Exception:
        pass
    set_conversation_context(conversation_id)
    # Same for the accounting context, so the title call's token usage is
    # recorded against this session (task='title_generation', #23270).
    set_accounting_context(session_db, session_id)

    title = generate_title(
        user_message,
        assistant_response,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
        runtime_validator=runtime_validator,
    )
    if not title:
        return

    try:
        persisted = _persist_session_title(session_db, session_id, title)
        if persisted is None:
            return
        logger.debug("Auto-generated session title: %s", persisted)
        if title_callback is not None:
            try:
                title_callback(persisted)
            except Exception:
                logger.debug("Auto-title callback failed", exc_info=True)
    except Exception as e:
        logger.debug("Failed to set auto-generated title: %s", e)


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Fire-and-forget title generation after the first exchange.

    Only generates a title when:
    - This appears to be the first user→assistant exchange
    - No title is already set
    """
    if not session_db or not session_id or not user_message or not assistant_response:
        return

    # Count user messages in history to detect first exchange.
    # conversation_history includes the exchange that just happened,
    # so for a first exchange we expect exactly 1 user message
    # (or 2 counting system). Be generous: generate on first 2 exchanges.
    user_msg_count = sum(1 for m in (conversation_history or []) if m.get("role") == "user")
    if user_msg_count > 2:
        return

    # Config read comes after the cheap first-exchange guard so the file
    # isn't touched on every subsequent turn of a long session.
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message, assistant_response),
        kwargs={
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
            "runtime_validator": runtime_validator,
        },
        daemon=True,
        name="auto-title",
    )
    thread.start()
