# Cross-Session Messaging (fork-only)

Status: design proposal (not yet implemented)
Scope: **fork-specific** (`adurham/hermes-agent`). Not proposed for upstream.
Porting: Claude Code's cross-session messaging
(https://code.claude.com/docs/en/cross-session-messaging) — `ListAgents` +
`SendMessage`, per-session inbox, held/accept/refuse inbound policy.

## Why fork-only, and why worth doing anyway

This is genuinely useful for the user's actual workflow (multiple concurrent
CLI sessions across profiles/repos, background delegate_task swarms, gateway
sessions running unattended) but it doesn't fit upstream's contribution
rubric cleanly: it's a new core-adjacent runtime plane (inbox storage,
polling, two new tools), and upstream's bar for new *model tools* is high
("every tool ships on every API call... prefer, in order: extend existing
code -> CLI command + skill -> service-gated tool -> plugin -> MCP server ->
new core tool"). We're taking the last-resort option deliberately because
the two tools' entire value is model-driven judgment ("does the other
session need to know this *right now*") — a CLI command a human runs doesn't
capture that. Ship it fork-only, gated behind an opt-in toolset so it costs
zero tokens for anyone who doesn't enable it, and reassess upstreaming once
it's proven out.

## What already exists in this codebase (don't rebuild)

Read before implementing — these are the closest existing mechanisms and
each one was rejected as *sufficient* for a specific, documented reason:

| Mechanism | What it does | Why it doesn't cover this |
|---|---|---|
| `delegate_task` / `swarm_run` (`tools/delegate_tool.py`, `tools/swarm_tool.py`) | Parent spawns **child** `AIAgent` instances in-process; parent sees only the final summary. `swarm_run` additionally wires children to the optional external `hermes-swarm` package for **mid-task peer messaging** (`swarm_broadcast`/`swarm_inbox`) | Peer messaging only exists between children of the *same* `swarm_run` call. No mechanism for two independently-started sessions (two terminal tabs, a CLI session and a gateway session, two profiles) to find and message each other. |
| Cron (`docs/session-lifecycle.md`, `AGENTS.md` cron section) | Runs in its **own** session with a header/footer frame specifically so it never mixes into a live conversation's message-role alternation | This is a precedent for *not* injecting into a live session, not a precedent for how to do it. Useful only for the idle-session delivery case (see below), not the mid-turn case. |
| Kanban (`hermes_cli/kanban.py`) | Durable SQLite-backed shared task board across profiles/workers; has `notify-subscribe`/`notify-list`/`notify-unsubscribe` | Async, poll/notify, human- or worker-driven via CLI verbs. No live "inject into a running conversation" semantics — it's a task board, not a messaging channel. |
| `send_message_tool.py` | Sends messages **out** to external platforms (Telegram/Discord/etc. user chats) | Addresses external humans, not other Hermes sessions. Name collision risk — new tool must be named differently (see Tools section). |

Net: there is no existing "two independently-running Hermes sessions on this
machine discover each other and exchange a message" mechanism. This is new
surface, not a gap-fill.

## Hard invariants this design must not violate

From `AGENTS.md`:

1. **Per-conversation prompt caching is sacred.** Nothing may mutate past
   context, swap toolsets, or rebuild the system prompt mid-conversation.
2. **Strict message role alternation.** Never two same-role messages in a
   row; never a synthetic user message spliced into the middle of an
   in-flight turn.
3. **Core is a narrow waist.** New model tools are expensive; this feature
   must be opt-in, not added to `_HERMES_CORE_TOOLS`.

## Architecture

### Storage: state.db, not a Unix socket + registry file

*(Revised after review — the original draft proposed a per-session Unix
domain socket at `~/.hermes/run/inbox/<session_id>.sock` plus a
`registry.json` for discovery. Rejected: a held message must be listable
and approvable from a separate CLI invocation — `hermes agents inbox` — which
means held messages need shared durable storage regardless. Once that's in
state.db, the DB is the actual source of truth and the socket is reduced to
a wakeup ping that buys sub-second latency for idle sessions and nothing
else, at the cost of pid-liveness races, stale-socket reaping, and a second
concurrent-writer file (`registry.json`) alongside a database this codebase
already trusts for exactly this kind of state. Not worth it for v1.)*

Two new tables in `~/.hermes/state.db` (schema lives in
`hermes_state_schema.py`, same file that owns `sessions`,
`session_model_usage`, etc.):

```sql
CREATE TABLE cross_session_registry (
    session_id      TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,        -- /rename or derived from cwd folder name
    cwd             TEXT,
    platform        TEXT,                 -- cli, gateway:telegram, acp, cron
    profile         TEXT NOT NULL,        -- HERMES_HOME profile name — see Profile scoping
    permission_mode TEXT,                 -- for inbound default resolution (see below)
    pid             INTEGER,
    last_heartbeat  TEXT NOT NULL         -- ISO8601, updated at each turn boundary
);

CREATE TABLE cross_session_inbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_session_id TEXT NOT NULL,
    from_name       TEXT NOT NULL,
    to_session_id   TEXT NOT NULL,
    body            TEXT NOT NULL,        -- plain text only, no history/files (matches CC)
    status          TEXT NOT NULL,        -- pending | held | delivered | denied | expired
    hop_count       INTEGER NOT NULL DEFAULT 0,  -- loop guard, see Throttling
    created_at      TEXT NOT NULL,
    delivered_at    TEXT,
    expires_at      TEXT                  -- held-message approval deadline
);
CREATE INDEX idx_inbox_recipient ON cross_session_inbox(to_session_id, status);
```

`cross_session_registry` rows are upserted on a heartbeat cadence (every
turn boundary — the same point that already updates `sessions.updated_at`,
no new polling loop needed) and reaped when `last_heartbeat` is older than a
short TTL (e.g. 2x the heartbeat interval) — no pid-liveness syscalls, no
atexit handler, no crash-safety burden.

Delivery is polling, not push: a session drains
`SELECT * FROM cross_session_inbox WHERE to_session_id = ? AND status = 'pending'`
at the same safe-boundary checkpoints described below. This matches the
codebase's existing grain (kanban polling, cron's file lock + tick) instead
of introducing a new runtime primitive. If idle-session latency ever proves
to matter in practice, a UDS wakeup can be layered on top later as a pure
optimization — it should not be the v1 foundation.

### Delivery mechanics (the part that must not break the invariants)

Two distinct delivery paths, matching Claude Code's own split:

**Idle session** (no in-flight turn): deliver as a new turn, framed exactly
like cron's header/footer convention — a normal `user`-role message with a
`[Message from session <name>]` header. This is safe by construction: it's
a fresh turn boundary, same mechanism cron already uses to avoid corrupting
alternation.

**Mid-turn session**: this is genuinely new territory, not covered by any
existing precedent in this codebase, and it has one sharp edge:

> The API requires a `tool_result` to appear in the **immediately next**
> user message after the assistant's `tool_use`. An incoming cross-session
> message CANNOT be inserted as a standalone user message between a tool
> call and its result — that breaks the wire contract, not just the cache.

Correct mechanic: when a turn is mid-flight and about to emit the next
`tool_result`-bearing user message anyway (the same checkpoint already used
for interrupt-checking in the conversation loop), append the framed
cross-session message as an **additional content block within that same
user message**, after the tool_result block(s). This is a tail append — no
prefix mutation, cache-safe — and preserves role alternation because it's
still one user message, just with an extra text block. If no tool call is
in flight (session is "thinking" / mid-generation with no pending
tool_result), treat it like the idle case and hold until the turn
completes, then deliver as framed context at the top of the next turn.

Do not attempt to interrupt an in-flight model generation to inject a
message. That path doesn't exist anywhere in this codebase today and
inventing it for this feature is out of scope.

### Tools

New module `tools/cross_session_tool.py`. Not added to `_HERMES_CORE_TOOLS`;
lives in a new opt-in toolset (e.g. `cross_session`) enabled via
`hermes tools` / `config.yaml`, following the `check_fn`-gated pattern
already used for Home Assistant / kanban / computer-use toolsets in
`toolsets.py`.

- `list_agents()` — queries `cross_session_registry` (profile-scoped, see
  below), returns name, cwd, platform, staleness-filtered by heartbeat age.
- `send_agent_message(target, body)` — resolves `target` by name (same
  same-name-different-cwd disambiguation Claude Code does: append a short
  id when names collide), inserts a row into `cross_session_inbox`.

Named `send_agent_message`/`list_agents` rather than reusing
`send_message`/`list_*` to avoid collision with the existing
`send_message_tool.py` (external platform messaging) — the two must never
be confused by the model or by config gating.

### Inbound policy — `cross_session.inbound`

New `config.yaml` key (not a `HERMES_*` env var — behavioral config belongs
in config.yaml per the contribution rubric, this is a fork-only doc but
we hold ourselves to the same rule): `accept` / `hold` / `refuse`.

Defaults, deliberately more conservative than Claude Code's for the
platforms where an unexpected message would be genuinely disruptive:

- CLI sessions: `hold` (matches Claude Code's default posture)
- Gateway sessions (Telegram/Discord/etc.): `refuse` by default — a user
  mid-conversation on Telegram should never see unrelated agent chatter
  unless they've explicitly opted a profile in
- ACP (editor) sessions: `hold`
- Cron sessions: `refuse` — cron already has its own isolation invariant

A `hold`ed message needs a human decision. No GUI dialog primitive exists
in the CLI (unlike Claude Code's TUI approval dialog), so: a held message
surfaces as a terminal notice at the next natural output point, and
`hermes agents inbox [--approve ID | --deny ID]` lists/resolves held
messages from any session sharing the profile's state.db. Expires per
`expires_at` (default 5m, matching Claude Code's `dialogExpiry`) via the
same lock/expiry pattern cron already uses.

### Throttling — non-optional for v1

*(Flagged in review as the single biggest omission from the original
draft: two sessions with `inbound: accept` can ping-pong indefinitely,
each message starting or extending a turn and burning tokens, with no
human in the loop. Claude Code built rate-limiting for exactly this
reason — it can't be dropped just because it didn't show up naturally
while designing the happy path.)*

Required before ship, not a follow-up:

- `hop_count` column above: each outbound `send_agent_message` triggered
  *by a message that itself carried a nonzero hop_count* increments it.
  Reject/drop sends with `hop_count` over a small ceiling (e.g. 4).
- Per-sender-pair rate cap: max N messages per sender->recipient pair per
  rolling window (e.g. 5/minute), enforced at insert time in
  `cross_session_inbox`.
- Hard per-turn ceiling: a single turn may send at most 1
  `send_agent_message` call (mirrors Claude Code's framing of messaging as
  a deliberate, occasional handoff, not a chat loop).
- Identical-body repeat suppression within a short window (Claude Code
  does this too — cite as precedent).

### Profile scoping

Profiles are isolated HERMES_HOME directories, each with their own
`state.db`. Since the registry/inbox tables live in `state.db`, scoping is
automatic and correct by construction: **sessions can only discover and
message other sessions in the same profile.** No cross-profile bleed, no
extra code. This is a deliberate, documented decision (the original draft
left it unstated) — cross-profile messaging is out of scope for v1; if
ever needed it would require an explicit opt-in bridge, not an accidental
default.

### Explicitly out of scope for v1

- Cross-machine relay (Claude Code's Remote Control path) — no Remote
  Control equivalent exists in Hermes.
- Agent-team-style structured protocol messages — kanban already covers
  durable multi-worker coordination; don't duplicate it.
- Windows — Claude Code itself doesn't support it for this feature either;
  no reason to be first.
- UDS wakeup optimization — pure follow-up if polling latency proves to
  matter in practice.

## Open questions for the user before implementation starts

1. Toolset name / opt-in surface: new `cross_session` toolset via
   `hermes tools`, or fold into an existing catalog entry?
2. Heartbeat interval and registry TTL — proposing "update at every turn
   boundary, reap after 2x the interactive idle-timeout" but this needs a
   number tied to real session-lifecycle constants, not guessed.
3. Does `hermes agents inbox` want to be a new subcommand file under
   `hermes_cli/subcommands/`, or fits better bolted onto an existing one?

## Review note

This design was reviewed against a second-opinion pass before being
finalized. That pass caught three real defects in the original draft and
they're folded into the sections above rather than left as a changelog:
(1) the UDS+registry.json transport was replaced with the state.db
approach once it became clear the held-message approval flow already
required durable shared storage; (2) the mid-turn delivery mechanic
originally described "inserting a message between tool calls," which is
an API-contract violation (tool_result placement), not just a cache
concern — replaced with tail-appending a content block onto the existing
tool_result-bearing user message; (3) throttling/loop-guarding was present
in the feature description but absent from the original design section —
now a required v1 component, not a follow-up.
