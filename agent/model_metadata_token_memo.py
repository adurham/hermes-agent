"""Release a closed conversation's entries from the per-message token-estimate memo.

``agent.model_metadata._MSG_TOKENS_CACHE`` pins every string it fingerprints (the pin keeps the ``id()``
key sound), so a long-lived process kept each closed session's message bodies resident until 4096 newer
messages pushed them out.
"""

from __future__ import annotations

from typing import Any


def forget_message_token_memo(messages: Any) -> int:
    """Evict memo entries pinning any long string value of ``messages``; return how many.

    Eviction never changes an estimate: a later lookup recomputes the same value.
    """
    from agent.model_metadata import _MSG_TOKENS_CACHE

    owned: set[int] = set()
    stack = [messages]
    while stack:
        value = stack.pop()
        if type(value) is dict:
            stack.extend(value.values())
        elif type(value) is list or type(value) is tuple:
            stack.extend(value)
        elif type(value) is str and len(value) > 64:  # short values ("assistant", tool names) are shared
            owned.add(id(value))
    if not owned:
        return 0
    stale = [key for key, (pins, _text, _images) in list(_MSG_TOKENS_CACHE.items())
             if any(id(pin) in owned for pin in pins)]
    for key in stale:
        _MSG_TOKENS_CACHE.pop(key, None)
    return len(stale)
