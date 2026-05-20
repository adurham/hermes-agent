# Memory Recall Reminder + Session-Pin

> **Status (2026-05-19):** Both phases LANDED. Feature A lives in `agent/fork/memory_recall.py` (20 tests in `tests/test_memory_recall_reminder.py`); Feature B lives in `agent/fork/memory_session_pin.py` (18 tests in `tests/test_memory_session_pin.py`). Schema and FORK.md updated. The text below is the original plan, kept for context.

> **For Hermes:** Use test-driven-development for implementation. Pattern-match the existing skill-recall reminder. Post-refactor it lives in `agent/fork/skill_recall.py` (helpers + `_RISKY_TOOL_NAMES`), with thin forwarders in `agent/fork/_mixin.py` and call sites in `agent/tool_executor.py:444-455` (concurrent path) and `:929-937` (sequential path). `init_state` is invoked from `agent/agent_init.py:997-1004`. Config wiring lives in `agent_init.py:1143-1151`. Mirror that shape — don't try to put new logic in `run_agent.py` directly.

**Goal:** Close the gap between hot tier (always loaded, ~1k chars, tokens-every-turn) and warm tier (searchable but only on agent-initiated recall). Today the agent doesn't actively trigger warm recall when it matters most — during hypothesis formation, mid-investigation. This adds two cheap mechanisms that push warm-tier facts into context at the right moment.

**Tech Stack:** Hermes agent (`run_agent.py`), warm-tier memory store (`tools/memory_warm.py`), memory tool (`tools/memory_tool.py`), pytest.

---

## 1. Problem statement

The user (Adam) noticed during a long NEC investigation session:
> "I just don't actively trigger [warm memory] during reasoning. Fact 69 is structurally the lesson I needed this session, and I never queried for it. Hot memory's overreach checklist literally has the right shape but I never explicitly invoke it — it's documentation, not a habit."

Concrete failure mode from that session: agent formed a mechanism hypothesis ("Schedule clamps wire in non-CDN mode, so NEC's CDN environment is where the bug manifests"), committed to it, wrote a Jira draft on top of it, then the user pushed back ("but NEC disabled CDN — it SHOULDN'T matter, but they still see the symptom"). The hypothesis was incomplete in a way that fact 69 (warm tier, "READ THE SOURCE before concluding; verify ALL customer evidence is consistent") would have caught at formation time.

### Why hot tier alone isn't the answer

- Hot tier is char-capped (~600+400 across user/memory). Adam's is 97%/90% full.
- Hot tier is static — same prompt every turn. Doesn't trigger on the right *moment*.
- Promoting every "could be useful sometimes" warm fact to hot would saturate the cap.

### Why current warm tier alone isn't the answer

- Requires the agent to think "let me check past lessons" — this never happens during hypothesis formation.
- Auto-feedback (Phase 3, `tools/memory_auto_feedback/`) adjusts trust based on retrieval-then-helpful, but only after retrieval — doesn't seed it.
- No phrase/turn-based trigger.

### What works elsewhere

The skill-recall reminder (`agent/fork/skill_recall.py`, forwarders in `agent/fork/_mixin.py`, hooks in `agent/tool_executor.py:444-455` and `:929-937`) is the proven shape:
- Counts "risky tool calls" (terminal, write, edit, etc.) via `_RISKY_TOOL_NAMES`
- After N risky calls, appends a one-line reminder to the tool result asking the agent to call `skill_pitfalls(name)`
- Pointed at a cheap recall path (~500-3000 tokens vs full skill_view)
- Configurable interval (`agent.skills.recall_reminder_interval`, default 6)
- Tested in `tests/test_skill_recall_reminder.py`

This works. The agent *does* respond to these inline reminders. We mirror that machinery for memory.

---

## 2. Design

### 2.1 Feature A: Periodic context-aware recall reminder

After every N tool calls (configurable, default 8), inject a one-line reminder asking the agent to consider `memory(action='recall', query=...)`. The reminder is **context-aware**: it auto-extracts a query candidate from recent user/tool activity rather than asking the agent to come up with one cold.

#### Trigger conditions

ALL of these must hold:
- `agent.memory.recall_reminder_interval > 0` (feature enabled; default 8)
- Warm tier is non-empty (at least 1 indexed fact)
- Counter has reached the interval since session start OR since last memory recall (whichever is more recent)
- The current turn is a "substantive" turn — has tool calls OR is responding to a user message ≥ 200 chars (filters out terse Q&A)

#### Reminder content (two flavors, chosen by config)

**Flavor 1 — Hint** (`mode: "hint"`):
> [memory-recall reminder] You haven't consulted warm memory in N turns. If your current investigation touches a topic you've worked on before, call `memory(action='recall', query='...')` — keyword search across 600+ past facts. This reminder fires every {interval} turns and is cheap to act on.

**Flavor 2 — Auto-run** (`mode: "auto"`, default):
The harness extracts a 3-5 word query from the most recent user message + recent tool args, runs `memory.recall(query, top_k=3)` automatically, and injects:
> [memory-recall reminder] Auto-recall for query "{query}" returned {N} relevant facts. Top: [fact 69, trust 0.5, "READ THE SOURCE that emits it..."]. Call `memory(action='recall', query='...')` for more, or `memory(action='read', fact_id=N)` for full text.

Auto mode is more aggressive but more reliable — `hint` mode would be ignored the same way `read this skill` hints currently are.

#### Query extraction heuristic

Cheap and local — no LLM call. Take the most recent user message text + the args of the last 3 tool calls. Strip stopwords. Take the 3-5 highest-IDF-feeling nouns / proper nouns (regex for `[A-Z][a-z]+|[a-z]{6,}|[A-Z]{2,}\d+`). Joined with `OR` for FTS5.

Edge cases:
- If extraction yields nothing meaningful (e.g. user said "ok" and last tool was `read_file`), skip the reminder this turn — don't burn the cooldown.
- If recent user message contains explicit memory directives ("forget this", "remember this", "we did this before"), the reminder fires immediately regardless of counter.

#### Cooldown semantics

Reset counter to 0 when:
- Agent calls `memory(action='recall', ...)` voluntarily (don't double-remind)
- Reminder fires (one reminder, then wait `interval` turns again)
- Session restarts (counter is in-memory only)

#### Config

```yaml
agent:
  memory:
    recall_reminder_interval: 8    # turns between reminders; 0 = disabled
    recall_reminder_mode: "auto"   # "auto" | "hint"
    recall_auto_top_k: 3           # top_k for auto-run mode
    recall_min_user_chars: 200     # don't fire for short Q&A turns
```

#### Cost

- `auto` mode: 1 FTS5 query per N turns × ~50 tokens injected. At interval=8 over a 100-turn session: ~12 reminders, ~600 tokens. Negligible.
- `hint` mode: ~50 tokens × 12 = ~600 tokens, no DB hit.

### 2.2 Feature B: Session-pin action

New action: `memory(action='pin', fact_id=N)` keeps a warm-tier fact visible in the system prompt for the rest of the current session only. Unpinned on session restart.

#### Why this exists

Today the agent's options are:
- **Hot tier**: permanent, cap-limited (97% full for Adam right now)
- **Warm tier**: searchable but invisible unless queried
- **Skills**: load-on-demand but heavyweight (full skill content, ~30k tokens)

Mid-investigation, the agent often realizes "this fact applies to *this whole session*" but doesn't apply to every future session. Promoting to hot is too sticky. Re-querying warm every turn is unreliable.

Session-pin fits the gap: pin for this session, gone after. No cap impact across sessions, no permanent state mutation.

#### Semantics

```python
memory(action='pin', fact_id=69)
# → returns {pinned: [69], unpinned: []}
# → fact 69 content gets injected into the system prompt prelude for
#   the rest of this session, alongside the standard hot tier
# → if hot tier renders are cached / re-emitted later in the session,
#   pinned facts are included

memory(action='unpin', fact_id=69)
# → removes from session prompt
# → returns {pinned: [], unpinned: [69]}

memory(action='pinned')
# → returns list of currently session-pinned fact_ids and a preview
```

#### Storage

In-memory only on the AIAgent instance:
- `self._session_pinned_facts: dict[int, dict]` — fact_id → fact row snapshot
- Populated by `pin` action, drained by `unpin`, cleared on `__init__`
- Pinned content joined into the system prompt via the same path as hot tier

No DB writes. Pin is intentionally non-durable.

#### Cap and safety

- Cap: max 5 pinned facts per session (configurable, default 5)
- Total pinned content cap: 2000 chars
- If user/agent tries to pin a 6th, oldest pin is auto-unpinned (LRU)
- If a pin would exceed char cap, refuse with explanatory error

#### Interaction with promote/demote

- `pin` ≠ `promote`. Promote moves to hot tier permanently. Pin is session-only.
- After a session ends with a pin, if the user explicitly says "that fact has been useful, make it permanent" → agent calls `promote(fact_id)` (existing action) → next session has it in hot.
- We can offer this proactively at session end if a pin has been retrieved/used heavily during the session.

#### Config

```yaml
agent:
  memory:
    session_pin_max_count: 5
    session_pin_max_chars: 2000
```

### 2.3 Interaction with auto-feedback (Phase 3)

Both features feed retrieval signal into the existing auto-feedback machinery:
- Feature A's auto-recall mode calls `_record_recall_for_auto_feedback` (already in `memory_warm.py:310`) so trust scores update
- Feature B's pin action records a synthetic positive-signal event (the agent voluntarily kept this fact accessible — strong signal) — bump trust by +0.10 or similar

No changes needed to auto-feedback core. Just hook into the existing rating path.

---

## 3. Implementation plan

### Phase 1 — Feature A (recall reminder) skeleton

1. `tests/test_memory_recall_reminder.py` — TDD harness:
   - test_no_warm_facts_no_reminder
   - test_interval_disabled_no_reminder
   - test_short_user_turn_no_reminder
   - test_fires_after_n_turns
   - test_resets_after_voluntary_recall
   - test_auto_mode_runs_recall_and_includes_top
   - test_hint_mode_only_emits_text
   - test_query_extraction_strips_stopwords
   - test_query_extraction_extracts_proper_nouns

2. `agent/fork/memory_recall.py` (new module, mirrors `agent/fork/skill_recall.py`):
   - `extract_query_candidate(user_msg, recent_tool_args) -> str | None`
   - `maybe_memory_recall_hint(agent, function_name, recent_user_msg) -> Optional[str]`
   - `record_voluntary_recall(agent)` — called by memory_tool when the agent invokes `recall` itself; resets the counter
   - `init_state(agent)` — initializes `_turns_since_memory_recall`, `_memory_recall_reminder_interval`, `_memory_recall_reminder_mode`, `_memory_recall_auto_top_k`, `_memory_recall_min_user_chars`, `_last_user_message`

3. Wiring (mirror skill_recall):
   - `agent/fork/_mixin.py`: thin forwarder `_maybe_memory_recall_hint`, `_record_voluntary_memory_recall`
   - `agent/agent_init.py:997-1004`: add `_fork_mem` to the init_state loop
   - `agent/agent_init.py:1143-1151`: read `agent.memory.recall_reminder_interval`, `.recall_reminder_mode`, `.recall_auto_top_k`, `.recall_min_user_chars` from config
   - `agent/tool_executor.py:444-455` and `:929-937`: after the existing skill-recall hook, append a memory-recall hint if returned
   - `tools/memory_tool.py:_handle_warm_action`: on `recall`, call `record_voluntary_recall` to reset the counter (look up the agent via a contextvar-or-callsite mechanism — see below)
   - Last-user-message tracking: capture in `run_agent.py:run_conversation` on each turn entry — assign to `agent._last_user_message`

### Phase 2 — Feature B (session-pin)

1. `tests/test_memory_session_pin.py`:
   - test_pin_action_returns_fact_in_response
   - test_pinned_fact_appears_in_next_system_prompt
   - test_unpin_removes_from_system_prompt
   - test_pinned_action_lists_current_pins
   - test_max_count_evicts_lru
   - test_max_chars_refuses_oversized_pin
   - test_pin_resets_across_session
   - test_pin_records_trust_signal

2. `tools/memory_tool.py` extensions:
   - Add `pin` / `unpin` / `pinned` actions to `_handle_warm_action`
   - Validate fact_id exists, return clear errors
   - Operate on `agent._session_pinned_facts` — wire the agent reference through the handler signature (the warm dispatcher already takes `hot_store=`, mirror that)

3. `agent/fork/memory_session_pin.py` (new module):
   - `init_state(agent)` — initializes `_session_pinned_facts: dict[int, dict]`, `_session_pin_max_count`, `_session_pin_max_chars`
   - `pin_fact(agent, fact_id) -> dict` — fetch from warm store, enforce caps, LRU evict
   - `unpin_fact(agent, fact_id) -> dict`
   - `list_pinned(agent) -> dict`
   - `render_pinned_block(agent) -> Optional[str]` — produce the system-prompt block

4. System prompt integration:
   - `agent/system_prompt.py`: after the `warm_status` block (around line 248-252), append the pinned-facts block via `render_pinned_block(agent)`
   - Note: the system prompt is cached on `_cached_system_prompt` per session — pin/unpin must `invalidate_system_prompt(agent)` so the next turn picks up changes

5. CLI (optional, deferred): `/pin <id>` and `/unpin <id>` slash commands could be added via the central registry. Skip for v1; the agent calls `memory(action='pin', ...)` directly.

### Phase 3 — Polish + docs

1. Update `tools/memory_tool.py` MEMORY_SCHEMA description with new `pin`/`unpin`/`pinned` actions
2. Update `AGENTS.md` memory section with the new tier story

---

## 4. Non-goals

- **Phrase-trigger recall** ("smoking gun" / "case closed" triggers a recall). Considered, too noisy; revisit only if Feature A alone is insufficient.
- **Hot-tier rotation** (auto-swap stale hot for high-retrieval warm). Too magical for v1; users should retain control over hot-tier contents.
- **LLM-based query extraction** for Feature A. Cheap regex is sufficient for v1; LLM extraction would add latency + token cost on every reminder turn.
- **Cross-session pin durability.** Pin is intentionally session-scoped. For "I want this in hot tier permanently," use existing `promote`.
- **Re-injecting hot memory mid-session.** Separate concern — would require harness changes to re-emit system prompt. Hot-tier-rotation v2 work.

---

## 5. Rollout

1. Land Phase 1 (Feature A) with default `mode: "auto"`, `interval: 8`. Observe in Adam's normal usage for 1 week.
2. If reminders are noise (firing without value), bump interval to 12 or shift to `mode: "hint"`.
3. If reminders are helpful, land Phase 2 (session-pin) and document the full tier story.
4. After both have soaked, consider whether to surface them in setup-wizard defaults for new users.

---

## 6. Open questions

1. **Does auto-mode's injected recall result count against the agent's tool-call budget?** Probably no — it's a harness-internal recall, not an LLM-initiated tool call. But worth confirming the budget tracker treats it correctly.

2. **Should the recall reminder respect a per-session "I don't want reminders" flag?** Probably yes — `agent.memory.recall_reminder_interval = 0` is the per-config disable, but a slash command `/no-memory-reminders` for one-off sessions might be valuable. Defer to v2.

3. **For session-pin, should the pinned facts be rendered above or below hot tier in the system prompt?** Above (more attention-weight), since session-pin is a deliberate signal that THIS session needs this fact.

4. **Should there be a "frequently pinned facts" auto-promote suggestion?** If a fact is session-pinned in 3+ sessions over 30 days, suggest promoting it to hot. Defer to v2; nice-to-have but adds complexity.

---

## 7. Success metric

Hard to measure cleanly, but: **does this session's failure mode (hypothesis lock-in despite available counter-evidence in warm memory) happen less often?** Soft measures:
- Did the agent call `memory(action='recall', ...)` voluntarily more often per session after Feature A landed?
- Did pinned facts get referenced/quoted in agent responses during sessions where they were pinned?
- Did Adam report fewer "you should have remembered X" corrections in the weeks after rollout?

Track #1 and #2 mechanically via session logs. #3 is qualitative.
