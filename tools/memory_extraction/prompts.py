"""Extraction prompts for Phase 2 auto-memory.

Prompt design philosophy:
  - Output is STRICT JSON. Parse failures are non-fatal — we drop the
    proposal silently rather than confusing the user.
  - Each prompt has a tight system message that primes the model on
    what counts as "memorable" for THIS user (a support/technical
    engineer, project context, single-user setup).
  - Few-shot examples are minimal — Sonnet/Haiku follow JSON schema
    instructions reliably without heavy priming.
  - Categories are free-form but suggested values are listed to keep
    them stable across extractions (preventing tag-soup explosion).
  - "Memorable" is bias-down: the right default is to extract NOTHING.
    Cost of a missed fact is low (it'll come up again); cost of noisy
    extractions is engineer fatigue at session-end confirm UI.

Inspired by mem0's prompt structure (system role + JSON output schema
+ minimal examples) but rewritten for our single-user / support-domain
context — we own this code.

The domain primer below is intentionally employer-agnostic (fork ships
generically; see AGENTS.md's vendor-identifying-strings policy) — it
describes the SHAPE of the user's work (support-case tooling, this
agent's own fork, internal scripts/MCPs) without naming a specific
company. If you want prompts tuned to your actual employer's product
names/module names, override ``auxiliary.memory_extraction.*`` in
config.yaml or fork this module locally.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Domain hints — generic support/technical-engineer framing, tunable per user
# ---------------------------------------------------------------------------

DOMAIN_PRIMER = """\
You are extracting durable memory entries for an AI assistant that helps a
support/technical engineer at a tech company. The user is working on:

- Customer support cases (a case-tracking system, product-specific modules)
- Hermes Agent — a long-running CLI / chat agent (their personal fork)
- Various MCPs, scripts, internal tooling

What COUNTS as memorable:
- New tooling discovered, new commands, new MCP/script paths
- API quirks, undocumented behavior, version-specific gotchas
- Project conventions ("for X, route through Y; never via Z")
- User preferences and corrections — STRONG signal: anything the user said
  to fix the assistant's behavior is HIGH priority
- Architecture facts about the employer's internal systems, Hermes
  internals, etc.
- "Solved problem" patterns the user might hit again

What DOES NOT count:
- Task progress / "we did X then Y"
- Summary of files read / commands run / outputs seen
- Outcomes of one-off investigations (those go to session_search)
- Restating what's already in well-known docs
- Anything the user is just thinking out loud about

DEFAULT to extracting NOTHING. Only propose entries when you have HIGH
confidence the fact will matter again. Empty list is a valid (and common)
answer.
"""


# Suggested category values. Free-form is allowed but consistency helps recall.
SUGGESTED_CATEGORIES = [
    "support",      # customer support case workflow, product modules
    "hermes",       # Hermes Agent internals, fork drift, plugins
    "mcp",          # MCP servers, debugging, auth
    "salesforce",   # SF case workflow, time logging, ticket refs
    "preferences",  # user preferences / corrections
    "tooling",      # CLI tools, scripts, shell quirks
    "review",       # PR review patterns, git workflows
    "general",      # default fallback
]


# ---------------------------------------------------------------------------
# JSON output schema (shared across all extraction calls)
# ---------------------------------------------------------------------------

# Per-response entry caps.
#
# Mid-session hooks (per-turn, pre-compress) stay deliberately tight: their
# output accumulates in the session buffer with nobody watching, so a loose
# cap there is pure buffer bloat. The session-end pass is the one the user
# actually reviews and approves interactively, and long dense technical
# sessions legitimately produce more than a handful of durable facts — so it
# gets a more generous ceiling.
MID_SESSION_MAX_ENTRIES = 5
SESSION_END_MAX_ENTRIES = 15

# Default cap applied by ``parse_extraction_response`` when the caller
# doesn't specify one. Set to the session-end ceiling so the interactive
# pass isn't silently truncated; mid-session prompts still ask the model
# for at most ``MID_SESSION_MAX_ENTRIES``.
DEFAULT_PARSE_MAX_ENTRIES = SESSION_END_MAX_ENTRIES


def _output_schema_docs(max_entries: int) -> str:
    """Render the shared JSON output-schema block with an explicit cap."""
    return f"""\
Output ONLY a JSON object with this exact shape:

{{
  "entries": [
    {{
      "content": "<the fact, stated declaratively, as a self-contained sentence>",
      "category": "<one of: support, hermes, mcp, salesforce, preferences, tooling, review, general>",
      "tags": "<comma-separated keywords, can be empty>",
      "rationale": "<1-line explanation of why this is memorable>"
    }}
  ]
}}

Rules:
- "entries" can be an empty list. EMPTY IS THE RIGHT ANSWER MOST OF THE TIME.
- Do NOT include any prose before or after the JSON.
- Do NOT use markdown code fences.
- Each entry's "content" must be a complete declarative fact (not a question,
  not a bullet point, not a fragment). Aim for 1-3 sentences.
- Maximum {max_entries} entries per response. If you have more, pick the
  highest-value {max_entries}.
"""


# Mid-session schema block (per-turn / pre-compress). Kept as a module-level
# name because it's part of this module's existing surface.
OUTPUT_SCHEMA_DOCS = _output_schema_docs(MID_SESSION_MAX_ENTRIES)

# Session-end schema block — the interactive, user-reviewed final pass.
SESSION_END_OUTPUT_SCHEMA_DOCS = _output_schema_docs(SESSION_END_MAX_ENTRIES)


# ---------------------------------------------------------------------------
# Per-turn extraction (smallest context: one user/assistant exchange)
# ---------------------------------------------------------------------------

PER_TURN_SYSTEM = f"""{DOMAIN_PRIMER}

Your job RIGHT NOW: read a single user/assistant exchange and propose 0-5
memory entries that are worth storing for future sessions.

{OUTPUT_SCHEMA_DOCS}

Be conservative. A typical exchange yields 0 entries. Only extract when the
user said something durable (a preference, a correction, a new fact) OR the
assistant discovered something durable (a tool path, an API quirk, a fix).
"""


def per_turn_user(user_msg: str, assistant_msg: str) -> str:
    """Build the user message for a per-turn extraction call."""
    return f"""User said:
{_truncate_for_extraction(user_msg, 4000)}

Assistant replied:
{_truncate_for_extraction(assistant_msg, 8000)}

Propose 0-5 memory entries. Output JSON only."""


# ---------------------------------------------------------------------------
# Pre-compression extraction (piggybacks on compression call)
# ---------------------------------------------------------------------------

PRE_COMPRESS_SYSTEM = f"""{DOMAIN_PRIMER}

Your job RIGHT NOW: review a slice of conversation messages that are about to
be compressed and discarded. Identify any durable memory entries worth
preserving BEFORE they're lost. Be more aggressive than per-turn extraction —
this is the last chance to capture facts from this slice.

{OUTPUT_SCHEMA_DOCS}

Aim for 0-5 entries. Empty list is still valid if the slice was just
back-and-forth with no durable facts.
"""


def pre_compress_user(messages: List[Dict[str, Any]]) -> str:
    """Build the user message for pre-compression extraction."""
    body = _format_messages_for_review(messages, max_chars=20000)
    return f"""Conversation slice about to be compressed:

{body}

Propose 0-5 memory entries from this slice. Output JSON only."""


# ---------------------------------------------------------------------------
# Session-end extraction (final pass over post-compression remainder)
# ---------------------------------------------------------------------------

SESSION_END_SYSTEM = f"""{DOMAIN_PRIMER}

Your job RIGHT NOW: review the FINAL state of a conversation that just
ended, plus a buffer of memory entries already proposed during the session
(from per-turn and pre-compress hooks). Produce the FINAL deduplicated list
of entries to commit.

You MUST:
1. Drop entries from the buffer that turned out to be wrong/superseded by
   later turns in the conversation.
2. Add any new entries from the final conversation state that weren't
   captured by earlier hooks.
3. Merge near-duplicates from the buffer into single coherent entries.

{SESSION_END_OUTPUT_SCHEMA_DOCS}

Final list should typically be 0-{SESSION_END_MAX_ENTRIES} entries. Quality over quantity — a
long, dense session may legitimately yield a dozen durable facts, but a
short one still yields zero.
"""


def session_end_user(
    final_messages: List[Dict[str, Any]],
    buffered_entries: List[Dict[str, Any]],
) -> str:
    """Build the user message for session-end extraction."""
    body = _format_messages_for_review(final_messages, max_chars=30000)
    if buffered_entries:
        buffer_str = json.dumps(buffered_entries, indent=2, default=str)
    else:
        buffer_str = "[]"
    return f"""Final conversation state (post-compression):

{body}

Buffered proposals from earlier in the session:
{buffer_str}

Produce the final deduplicated list of memory entries to commit. Output JSON only."""


# ---------------------------------------------------------------------------
# Session-end CLEANUP pass — review EXISTING warm facts for staleness /
# redundancy in light of what this session actually did.
#
# Runs alongside (never instead of) the new-entry proposal pass. Deletion is
# strictly higher-risk than addition, so this prompt is biased even harder
# toward doing nothing than the extraction prompts are: an empty list is the
# expected answer for most sessions, and the caller NEVER auto-commits these
# — every action requires explicit interactive confirmation.
# ---------------------------------------------------------------------------

SESSION_END_CLEANUP_SYSTEM = """\
You are auditing an AI assistant's long-term memory store for rot.

You are given:
  1. The final state of a conversation that just ended.
  2. NEW memory entries proposed from that conversation (not yet committed).
  3. EXISTING stored facts that are topically related to this session.

Your job: identify EXISTING facts (and only existing facts) that should be
cleaned up. A fact qualifies ONLY when one of these is clearly true:

- STALE: the fact asserts something that this session demonstrates is no
  longer true (a path that moved, a value that changed, an approach that was
  explicitly replaced). Evidence must be in the conversation — not a guess.
- SUPERSEDED: one of the NEW proposed entries states the same thing more
  completely and correctly, making the existing fact redundant.
- REDUNDANT: two EXISTING facts say materially the same thing and should be
  merged into one.

Output ONLY a JSON object with this exact shape:

{
  "cleanup": [
    {
      "fact_id": <int, id of the EXISTING fact to act on>,
      "action": "remove" | "merge",
      "reason": "<1-line justification citing the evidence>",
      "merge_target_id": <int, required when action='merge': the EXISTING
                         fact that survives and absorbs this one>,
      "merged_content": "<required when action='merge': the combined text the
                         surviving fact should end up with, preserving every
                         detail from BOTH facts>"
    }
  ]
}

Rules:
- "cleanup" can be an empty list. EMPTY IS THE EXPECTED ANSWER MOST OF THE
  TIME. Doing nothing is always safer than deleting a fact that still holds.
- Only reference fact_ids that appear in the EXISTING facts list.
- For "merge", fact_id is the fact being ABSORBED AND REMOVED, and
  merge_target_id is the fact that SURVIVES. They must be different ids.
- Never propose removing a fact just because it wasn't mentioned this
  session. Absence of evidence is not evidence of staleness.
- Never propose removing a fact you merely disagree with or find
  low-value. This pass is for rot, not for taste.
- Do NOT include prose before or after the JSON. No markdown code fences.
- Maximum 10 cleanup actions per response.
"""


def session_end_cleanup_user(
    final_messages: List[Dict[str, Any]],
    proposed_entries: List[Dict[str, Any]],
    existing_facts: List[Dict[str, Any]],
) -> str:
    """Build the user message for the session-end cleanup pass."""
    body = _format_messages_for_review(final_messages, max_chars=20000)
    if proposed_entries:
        proposed_str = "\n".join(
            f"  - {(e.get('content') or '').strip()}" for e in proposed_entries
        )
    else:
        proposed_str = "  (none)"
    existing_str = "\n".join(
        f"  [id={f.get('fact_id')}] ({f.get('category') or 'general'}) "
        f"{(f.get('content') or '').strip()}"
        for f in existing_facts
    ) or "  (none)"
    return f"""Final conversation state:

{body}

NEW entries proposed from this session (not yet committed):
{proposed_str}

EXISTING stored facts related to this session:
{existing_str}

Identify existing facts that are stale, superseded, or redundant. Output JSON only."""


def parse_cleanup_response(
    text: str,
    *,
    valid_fact_ids: Optional[Any] = None,
    max_actions: int = 10,
) -> List[Dict[str, Any]]:
    """Parse a cleanup response into a list of cleanup-action dicts.

    Returns an empty list on any parse failure — cleanup is best-effort and
    silently doing nothing is always the safe degradation.

    When ``valid_fact_ids`` is provided (any container supporting ``in``),
    actions referencing ids outside it are dropped. This is a hard safety
    gate: the LLM must not be able to name a fact we never showed it and
    have us delete it.
    """
    if not text or not text.strip():
        return []
    obj = _extract_json_object(text)
    if obj is None:
        return []
    raw_actions = obj.get("cleanup", [])
    if not isinstance(raw_actions, list):
        return []

    cleaned: List[Dict[str, Any]] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            continue
        fact_id = _coerce_int(raw.get("fact_id"))
        if fact_id is None:
            continue
        action = (raw.get("action") or "").strip().lower()
        if action not in ("remove", "merge"):
            continue
        if valid_fact_ids is not None and fact_id not in valid_fact_ids:
            continue

        entry: Dict[str, Any] = {
            "fact_id": fact_id,
            "action": action,
            "reason": (raw.get("reason") or "").strip(),
        }

        if action == "merge":
            target = _coerce_int(raw.get("merge_target_id"))
            if target is None or target == fact_id:
                # A merge with no distinct surviving target is not
                # actionable — degrade by dropping it rather than
                # silently turning it into a delete.
                continue
            if valid_fact_ids is not None and target not in valid_fact_ids:
                continue
            entry["merge_target_id"] = target
            entry["merged_content"] = (raw.get("merged_content") or "").strip()

        cleaned.append(entry)

    # Drop duplicate actions on the same fact — first one wins.
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for entry in cleaned:
        if entry["fact_id"] in seen:
            continue
        seen.add(entry["fact_id"])
        deduped.append(entry)
    return deduped[: max(0, int(max_actions))]


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort int coercion. Returns None for anything non-integral."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Conflict classification — given a new entry + existing similar entries,
# classify the relationship
# ---------------------------------------------------------------------------

CONFLICT_SYSTEM = """\
You are a memory conflict resolver. Given a NEW proposed memory entry and a
list of EXISTING similar entries (retrieved by keyword search from the
user's memory store), classify the relationship.

Output ONLY a JSON object with this exact shape:

{
  "verdict": "DUPLICATE" | "REFINEMENT" | "CONTRADICTION" | "NEW",
  "matched_id": <fact_id or null>,
  "rationale": "<1-line explanation>",
  "merged_content": "<merged text, only when verdict='REFINEMENT'>"
}

Definitions:
- DUPLICATE: the new entry says nothing materially different from an
  existing one. Pick the closest match; we'll just bump its retrieval count.
- REFINEMENT: the new entry adds detail to an existing one (more specific
  paths, version info, edge cases). Provide ``merged_content`` that
  preserves all detail from both.
- CONTRADICTION: the new entry directly conflicts with an existing one
  (e.g. "the API uses OAuth2" vs "the API uses API keys"). The user
  needs to resolve.
- NEW: the new entry is genuinely new — no existing entry overlaps.

Be strict: prefer NEW unless the overlap is clear. False REFINEMENT/DUPLICATE
verdicts cause data loss.
"""


def conflict_user(
    new_content: str,
    existing: List[Dict[str, Any]],
) -> str:
    """Build the user message for conflict classification."""
    if not existing:
        return f"New entry:\n{new_content}\n\nNo existing matches. Verdict should be NEW."
    existing_str = "\n".join(
        f"  [id={e['fact_id']}] {e['content']}"
        for e in existing
    )
    return f"""New proposed entry:
{new_content}

Existing similar entries:
{existing_str}

Classify the relationship. Output JSON only."""


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

# Match a top-level JSON object even if the model wraps it in code fences
# or chats around it. Greedy match the outer braces.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_extraction_response(
    text: str,
    *,
    max_entries: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Parse an extraction response into a list of entry dicts.

    ``max_entries`` caps the returned list. Defaults to
    ``DEFAULT_PARSE_MAX_ENTRIES`` (the session-end ceiling) so the
    user-reviewed final pass isn't silently truncated; callers on the
    mid-session hooks can pass ``MID_SESSION_MAX_ENTRIES`` to stay tight.

    Returns an empty list on any parse failure — extraction is best-effort.
    """
    if not text or not text.strip():
        return []
    obj = _extract_json_object(text)
    if obj is None:
        return []
    raw_entries = obj.get("entries", [])
    if not isinstance(raw_entries, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        content = (raw.get("content") or "").strip()
        if not content or len(content) < 10:
            continue
        cleaned.append({
            "content": content,
            "category": (raw.get("category") or "general").strip().lower(),
            "tags": (raw.get("tags") or "").strip(),
            "rationale": (raw.get("rationale") or "").strip(),
        })
    cap = DEFAULT_PARSE_MAX_ENTRIES if max_entries is None else int(max_entries)
    return cleaned[: max(0, cap)]


def parse_conflict_response(text: str) -> Optional[Dict[str, Any]]:
    """Parse a conflict-classification response. Returns None on failure."""
    if not text or not text.strip():
        return None
    obj = _extract_json_object(text)
    if obj is None:
        return None
    verdict = (obj.get("verdict") or "").strip().upper()
    if verdict not in ("DUPLICATE", "REFINEMENT", "CONTRADICTION", "NEW"):
        return None
    return {
        "verdict": verdict,
        "matched_id": obj.get("matched_id"),
        "rationale": (obj.get("rationale") or "").strip(),
        "merged_content": (obj.get("merged_content") or "").strip() or None,
    }


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Pull a top-level JSON object out of free-form text, tolerating fences."""
    # Strip code fences first
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence line
        cleaned = "\n".join(cleaned.splitlines()[1:])
        # Drop the closing fence
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    # Try direct parse
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Fall back to regex extraction
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


# ---------------------------------------------------------------------------
# Message-formatting helpers
# ---------------------------------------------------------------------------

def _truncate_for_extraction(text: Any, max_chars: int) -> str:
    """Coerce text-ish content to a string and truncate if too long."""
    if text is None:
        return ""
    if isinstance(text, str):
        s = text
    elif isinstance(text, list):
        # OpenAI message content can be a list of {type, text} blocks
        parts = []
        for block in text:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block.get("text") or ""))
                elif "content" in block:
                    parts.append(str(block.get("content") or ""))
            else:
                parts.append(str(block))
        s = "\n".join(parts)
    else:
        s = str(text)
    if len(s) <= max_chars:
        return s
    head = s[: max_chars // 2]
    tail = s[-max_chars // 2 :]
    return f"{head}\n[... {len(s) - max_chars} chars elided ...]\n{tail}"


def _format_messages_for_review(
    messages: List[Dict[str, Any]],
    max_chars: int = 20000,
) -> str:
    """Format a message list for inclusion in an extraction prompt.

    Trims to last messages that fit within max_chars. Drops tool messages
    (they're noisy and rarely contain durable facts; tool RESULTS that the
    assistant cites are already in the assistant's own text).
    """
    out: List[str] = []
    total = 0
    # Iterate in reverse so we keep the LATEST messages within budget
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "tool":
            continue
        content = _truncate_for_extraction(msg.get("content"), 2000)
        if not content.strip():
            continue
        block = f"--- {role} ---\n{content}"
        if total + len(block) > max_chars:
            break
        out.insert(0, block)
        total += len(block) + 4
    return "\n\n".join(out)
