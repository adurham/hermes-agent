"""Promise-without-action gate.

Some models — most visibly deepseek-v4.1-flash on ollama-cloud — end a turn by
announcing work instead of performing it: "I'll build the diagnostic mode now",
"building the console page next", "let me add the gate". The operator then sees a
commitment rather than a result, and nothing was dispatched. This is endemic enough
to be a model behaviour, not a one-off lapse, so it is gated in the turn loop rather
than left to per-turn discipline.

Two shapes are gated:

* ``turn`` — the turn made no tool call and the reply is a future-tense work
  commitment. Whole-body scan: nothing ran, so any promise in it is dangling.
* ``tail`` — the turn DID use tools, but the reply's last prose paragraph ends on a
  commitment ("I'll build that offline rather than keep firing"). The earlier calls
  were earlier steps; nothing ran after the promise, so by position it is unfulfilled.
  Tail-only, because a long analysis legitimately says "I'll" mid-reply, and never
  when that final paragraph is followed by a delivered fenced block.

A third shape rides on the tail path: the present-progressive announce
("I'm dispatching it now: take the table, rank the rest…") followed by a closing
paragraph of commentary. Future tense is absent there, and the commitment is one
paragraph above the last one, so both the sentence and the paragraph tests miss it.
A first-person present-progressive work verb anchored on "now" is an action the model
claims is already underway; when the turn ends there, by construction nothing was
dispatched. That shape is read across the closing TWO paragraphs.

Narrow by construction: one retry per turn (shared by both shapes), the commitment
must sit in the final prose paragraph, and an explicit external blocker ("blocked",
"waiting on", "needs you to") is a stated blocker, not a promise.
"""

from __future__ import annotations

import re

#: Appended as an ephemeral user row when the gate fires; never persisted.
PROMISE_GATE_NUDGE = (
    "Your previous message promised work — \"{excerpt}\" — but this turn made no tool call, "
    "so nothing was actually done. Do it now in this turn with a real tool call. If it cannot "
    "be done now, say plainly what blocks it. Do not restate the intention."
)

#: Same re-prompt for a turn that DID act and then ended by promising the next piece.
PROMISE_GATE_TAIL_NUDGE = (
    "Your previous message ended by promising work — \"{excerpt}\" — and then stopped: "
    "nothing in this turn carried it out. Do it now, in THIS turn, with a real tool call; "
    "a later turn or \"offline\" is not an option. If it cannot be done now, say in one line "
    "what blocks it. Do not restate the intention."
)

#: Gate kinds, also used as the log/telemetry discriminator.
GATE_TURN = "turn"
GATE_TAIL = "tail"

#: Cap on re-prompts per turn, shared by both shapes. The loop also stops nudging once a
#: tool call appears.
MAX_PROMISE_GATE_RETRIES = 1

#: First-person future constructions followed (within a short window) by a work verb.
_FUTURE = r"(?:i['’]ll|i will|i['’]m going to|i am going to|we['’]ll|we will|let me)"

_WORK_VERB = (
    r"(?:build|building|write|writing|make|making|run|running|check|checking|add|adding|"
    r"fix|fixing|test|testing|look|looking|start|starting|try|trying|dig|digging|"
    r"wire|wiring|patch|patching|dispatch|dispatching|launch|launching|set up|spin up|"
    r"re-run|rerun|rerunning|investigate|investigating|probe|probing|verify|verifying|"
    r"fire|firing|open|opening|stage|staging|audit|auditing|scan|scanning|"
    r"sweep|sweeping|harvest|harvesting|read|reading|pull|pulling)"
)

_PROMISE_RE = re.compile(_FUTURE + r"\s+(?:now\s+)?" + _WORK_VERB + r"\b", re.IGNORECASE)

#: Present-progressive announcement: "I'm dispatching it now", "I am launching it now".
#: No future tense to hang on, so "now" is the anchor — a first-person present-progressive
#: work verb close to an explicit "now" is the model describing the action as underway. The
#: window is sentence-bounded, so a later "now" in another clause cannot manufacture a match.
_ACT_NOW_RE = re.compile(
    r"\b(?:i['’]m|i am)\s+(?:now\s+)?" + _WORK_VERB + r"\b[^.!?\n]{0,60}?\bnow\b",
    re.IGNORECASE,
)

#: Bare announcements that carry no future tense but still promise work.
_BARE_RE = re.compile(
    r"^\W*(?:building now|working on it now|doing it now|on it now|starting now)\W*$",
    re.IGNORECASE | re.MULTILINE,
)

#: A tail commitment naming a dependency outside the agent's control is a stated blocker,
#: not a promise: nudging it only costs a turn. Deliberately does NOT include soft dodges
#: ("if you want", "once you've", "your call", "let me know") — those are exactly the
#: deferral this gate exists to reject.
_BLOCKER_RE = re.compile(
    r"\b(?:blocked|can't|cannot|unable to|waiting (?:on|for)|needs you|needs the|"
    r"need the|you'll need|requires your|until you)\b",
    re.IGNORECASE,
)

#: A reply that ends on a fragment ("That's the enabling piece.") puts its commitment in the
#: sentence BEFORE it; a longer closing sentence is substantive prose, so only the final
#: sentence is considered (a promise buried under further analysis is not dangling).
_SHORT_LAST_SENTENCE = 44

_FENCE = ("```", "~~~")


def _prose_after_last_fence(text: str) -> str:
    """The prose that follows the last *closed* fenced block.

    Anything above a closing fence was followed by delivered output, so a commitment
    there is backed: "I'll write the patch:" + a fenced patch must never be gated. A
    message that ends inside a fence has no prose tail at all.
    """
    lines = (text or "").splitlines()
    last_close = -1
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(_FENCE):
            if in_fence:
                last_close = i
            in_fence = not in_fence
    if in_fence:
        # The reply ends INSIDE a fenced block (truncated output, a half-written snippet):
        # the last thing said is code, not prose. A comment there ("# I'll build that later")
        # is code, not a commitment.
        return ""
    if last_close < 0:
        return text or ""
    return "\n".join(lines[last_close + 1:])


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _tail_paragraph(text: str) -> str:
    """The final prose paragraph — the only zone a dangling promise can live in."""
    paragraphs = _paragraphs(_prose_after_last_fence(text))
    return paragraphs[-1] if paragraphs else ""


#: How many closing prose paragraphs the present-progressive ("I'm dispatching it now") scan
#: reads. One is not enough: the announcement is routinely followed by a closing paragraph of
#: commentary ("The next launch then tests one named hypothesis…"), which is what the recorded
#: 2026-10-25 miss looked like. Still bounded, so an intention stated earlier in a long analysis
#: stays untouched.
_ACT_NOW_ZONE_PARAGRAPHS = 2


def _act_now_commitment(text: str) -> str | None:
    """The present-progressive announcement in the closing paragraphs, or None.

    "I'm dispatching it now: …" claims the action is already underway. When the turn ends
    there nothing was dispatched, so it is a promise in the present tense — invisible to the
    future-tense sentence test, and invisible to the final-paragraph test whenever a closing
    paragraph of commentary follows it.
    """
    paragraphs = _paragraphs(_prose_after_last_fence(text))
    if not paragraphs:
        return None
    for paragraph in reversed(paragraphs[-_ACT_NOW_ZONE_PARAGRAPHS:]):
        if _BLOCKER_RE.search(paragraph):
            continue
        for sentence in _sentences(paragraph):
            if sentence.rstrip().endswith("?"):
                continue
            if _ACT_NOW_RE.search(sentence):
                return sentence
    return None



def _sentence_containing(text: str, offset: int) -> str:
    """The sentence of *text* that spans character *offset* (used for the nudge excerpt)."""
    start = 0
    for m in re.finditer(r"[.!?…]\s+|\n", text[:offset]):
        start = m.end()
    end = len(text)
    for m in re.finditer(r"[.!?…](?=\s|$)", text[offset:]):
        end = offset + m.end()
        break
    return text[start:end].strip()


def _trim(sentence: str, limit: int) -> str:
    if len(sentence) > limit:
        return sentence[: limit - 1].rstrip() + "…"
    return sentence


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
    return bool(
        _PROMISE_RE.search(body) or _BARE_RE.search(body) or _ACT_NOW_RE.search(body)
    )


def tail_commitment(text: str | None) -> str | None:
    """The commitment sentence a reply ENDS on, or None.

    Only the final prose paragraph decides, so a long reply that mentions an intention
    mid-analysis (or before a delivered code block) is untouched. The turn must not end
    on a question, and a tail naming an external blocker is a stated blocker, not a promise.
    """
    if not text:
        return None
    paragraph = _tail_paragraph(text)
    if not paragraph:
        return None
    sentences = _sentences(paragraph)
    if not sentences:
        return None
    last = sentences[-1]
    if last.rstrip().endswith("?"):
        return None
    if _BLOCKER_RE.search(paragraph):
        return None
    act_now = _act_now_commitment(text)
    if act_now:
        return act_now
    candidates = [last]
    if len(sentences) > 1 and len(last) <= _SHORT_LAST_SENTENCE:
        candidates.insert(0, sentences[-2])
    for sentence in candidates:
        if _PROMISE_RE.search(sentence) or _BARE_RE.search(sentence):
            return sentence
    return None


def promise_excerpt(text: str | None, limit: int = 120) -> str:
    """The matched promise, trimmed for the nudge text (empty when no match)."""
    if not text:
        return ""
    tail = tail_commitment(text)
    if tail:
        return _trim(tail, limit)
    body = text.strip()
    match = _PROMISE_RE.search(body) or _BARE_RE.search(body)
    if not match:
        return ""
    return _trim(_sentence_containing(body, match.start()), limit)


def gate_kind(
    *,
    final_text: str | None,
    turn_tool_call_count: int,
    retries: int,
    finish_reason: str | None = None,
) -> str | None:
    """Which promise shape (if any) should re-prompt instead of ending the turn.

    ``turn_tool_call_count`` must be scoped to THIS turn (rows after the last user row).
    A session-wide count makes the zero-tool-call branch unreachable in any session that
    ever ran a tool — the bug that let 5-probe-then-promise turns through untouched.
    """
    if retries >= MAX_PROMISE_GATE_RETRIES:
        return None
    if finish_reason not in (None, "stop", "length"):
        return None
    if turn_tool_call_count > 0:
        return GATE_TAIL if tail_commitment(final_text) else None
    return GATE_TURN if looks_like_promise(final_text) else None


def should_gate(
    *,
    final_text: str | None,
    turn_tool_call_count: int,
    retries: int,
    finish_reason: str | None = None,
) -> bool:
    """Whether to re-prompt instead of ending the turn (see :func:`gate_kind`)."""
    return gate_kind(
        final_text=final_text,
        turn_tool_call_count=turn_tool_call_count,
        retries=retries,
        finish_reason=finish_reason,
    ) is not None
