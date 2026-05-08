"""Auto-generate short session titles from the first user/assistant exchange.

Runs asynchronously after the first response is delivered so it never
adds latency to the user-facing reply.
"""

import logging
import threading
from typing import Callable, Optional

from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# so silent-drops (e.g. OpenRouter 402 exhausting the fallback chain)
# become visible instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]
TitleCallback = Callable[[str], None]

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)


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


def generate_title(
    user_message,
    assistant_response,
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
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
    """
    # Coerce list-shaped content (image attachments etc.) to plain text
    # BEFORE truncating — otherwise [:500] slices list elements rather
    # than characters and f-stringing the result inlines the whole image.
    user_text = _extract_text(user_message)
    assistant_text = _extract_text(assistant_response)
    user_snippet = user_text[:500]
    assistant_snippet = assistant_text[:500]

    messages = [
        {"role": "system", "content": _TITLE_PROMPT},
        {"role": "user", "content": f"User: {user_snippet}\n\nAssistant: {assistant_snippet}"},
    ]

    try:
        response = call_llm(
            task="title_generation",
            messages=messages,
            max_tokens=500,
            temperature=0.3,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        title = (response.choices[0].message.content or "").strip()
        # Clean up: remove quotes, trailing punctuation, prefixes like "Title: "
        title = title.strip('"\'')
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        # Enforce reasonable length
        if len(title) > 80:
            title = title[:77] + "..."
        return title if title else None
    except Exception as e:
        # Log at WARNING so this shows up in agent.log without debug mode.
        # Full detail at debug level for operators who need the stack.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Title generation failure_callback raised", exc_info=True)
        return None


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
) -> None:
    """Generate and set a session title if one doesn't already exist.

    Called in a background thread after the first exchange completes.
    Silently skips if:
    - session_db is None
    - session already has a title (user-set or previously auto-generated)
    - title generation fails
    """
    if not session_db or not session_id:
        return

    # Check if title already exists (user may have set one via /title before first response)
    try:
        existing = session_db.get_session_title(session_id)
        if existing:
            return
    except Exception:
        return

    title = generate_title(
        user_message, assistant_response, failure_callback=failure_callback, main_runtime=main_runtime
    )
    if not title:
        return

    try:
        session_db.set_session_title(session_id, title)
        logger.debug("Auto-generated session title: %s", title)
        if title_callback is not None:
            try:
                title_callback(title)
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

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message, assistant_response),
        kwargs={
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
        },
        daemon=True,
        name="auto-title",
    )
    thread.start()
