"""Promise-without-action gate.

Some models — most visibly deepseek-v4.1-flash on ollama-cloud — end a turn by
announcing work instead of performing it: "I'll build the diagnostic mode now",
"building the console page next", "let me add the gate". The operator then sees a
commitment rather than a result, and nothing was dispatched. This is endemic enough
to be a model behaviour, not a one-off lapse, so it is gated in the turn loop rather
than left to per-turn discipline.

The gate fires only when the *whole turn* made no tool calls: a reply that summarises
completed work is fine, and a reply that mixes a completed action with a note about
what comes next is also fine. What is not fine is a turn whose entire content is a
future-tense work commitment with no tool call behind it.

Kept deliberately narrow to avoid re-prompt loops: one retry per turn, and the reply
must match a small set of first-person future constructions followed by an action verb.
"""

from __future__ import annotations

import re

#: Appended as an ephemeral user row when the gate fires; never persisted.
PROMISE_GATE_NUDGE = (
    "Your previous message promised work — \"{excerpt}\" — but this turn made no tool call, "
    "so nothing was actually done. Do it now in this turn with a real tool call. If it cannot "
    "be done now, say plainly what blocks it. Do not restate the intention."
)

#: Cap on re-prompts per turn. The loop also stops nudging once a tool call appears.
MAX_PROMISE_GATE_RETRIES = 1

#: First-person future constructions followed (within a short window) by a work verb.
_FUTURE = r"(?:i['’]ll|i will|i['’]m going to|i am going to|we['’]ll|we will|let me)"

_WORK_VERB = (
    r"(?:build|building|write|writing|make|making|run|running|check|checking|add|adding|"
    r"fix|fixing|test|testing|look|looking|start|starting|try|trying|dig|digging|"
    r"wire|wiring|patch|patching|dispatch|dispatching|launch|launching|set up|spin up|"
    r"re-run|rerun|rerunning|investigate|investigating|probe|probing|verify|verifying)"
)

_PROMISE_RE = re.compile(_FUTURE + r"\s+(?:now\s+)?" + _WORK_VERB + r"\b", re.IGNORECASE)

#: Bare announcements that carry no future tense but still promise work.
_BARE_RE = re.compile(
    r"^\W*(?:building now|working on it now|doing it now|on it now|starting now)\W*$",
    re.IGNORECASE | re.MULTILINE,
)


def looks_like_promise(text: str | None) -> bool:
    """True when *text* is a future-tense work commitment.

    Deliberately conservative: matching requires a first-person future construction
    adjacent to a work verb, or a bare "building now"-style line. Completed-work
    summaries ("I built X", "the gate is in") never match.
    """
    if not text:
        return False
    body = text.strip()
    if not body:
        return False
    return bool(_PROMISE_RE.search(body) or _BARE_RE.search(body))


def promise_excerpt(text: str | None, limit: int = 120) -> str:
    """The matched promise, trimmed for the nudge text (empty when no match)."""
    if not text:
        return ""
    match = _PROMISE_RE.search(text) or _BARE_RE.search(text)
    if not match:
        return ""
    excerpt = match.group(0).strip()
    if len(excerpt) > limit:
        excerpt = excerpt[: limit - 1].rstrip() + "…"
    return excerpt


def should_gate(
    *,
    final_text: str | None,
    turn_tool_call_count: int,
    retries: int,
    finish_reason: str | None = None,
) -> bool:
    """Whether to re-prompt instead of ending the turn.

    ``turn_tool_call_count`` is the number of tool calls made anywhere in this turn:
    any real action at all disables the gate, which keeps it from nagging turns that
    did work and merely ended with a note about the next step.
    """
    if retries >= MAX_PROMISE_GATE_RETRIES:
        return False
    if turn_tool_call_count > 0:
        return False
    if finish_reason not in (None, "stop", "length"):
        return False
    return looks_like_promise(final_text)
