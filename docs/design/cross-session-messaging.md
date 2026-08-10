# Cross-Session Messaging (fork-only)

Status: design proposal — unblocked (see "Round 3 review" below; the
idle-session delivery mechanism the doc previously called an unproven
blocker requiring a spike already exists in the codebase and is
production-proven for an adjacent use case).
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

`cross_session_registry` rows are upserted at the existing turn-boundary
heartbeat for **actively-turning sessions**, but see the Round-2 finding
below: heartbeat-on-turn starves the idle sessions this feature most needs
to reach, so this is flagged as unresolved, not settled. Reaped when
`last_heartbeat` is older than a TTL (e.g. 2x heartbeat interval) — no
pid-liveness syscalls, no atexit handler, no crash-safety burden. Cron
sessions do not register at all (they always `refuse`, see Inbound policy —
no reason to pay heartbeat cost for a session that can never receive).

Delivery is polling, not push: a session drains
`SELECT * FROM cross_session_inbox WHERE to_session_id = ? AND status = 'pending'`
at the same safe-boundary checkpoints described below, claimed atomically
(`UPDATE cross_session_inbox SET status='delivered', delivered_at=? WHERE id=? AND status='pending'`,
checking rows-affected before treating the message as claimed — see the
crash-semantics note in Round 2) to give at-least-once delivery without
duplicate injection on a crash between read and mark-delivered. This
matches the codebase's existing grain (kanban polling, cron's file lock +
tick) instead of introducing a new runtime primitive. If idle-session
latency ever proves to matter in practice, a UDS wakeup can be layered on
top later as a pure optimization — it should not be the v1 foundation.

### Delivery mechanics (the part that must not break the invariants)

Two distinct delivery paths, matching Claude Code's own split:

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

**Round 2 finding — this mid-turn mechanic's "same checkpoint already used
for interrupt-checking" is asserted, not verified.** No one has located the
actual line in `agent/conversation_loop.py` where that checkpoint lives, or
confirmed it's structurally reusable for this purpose. That must be
confirmed by reading the code before this section is treated as settled,
not assumed by analogy.

**Round 2 finding — provider wire-format assumption.** The tail-append fix
above assumes Anthropic-style content blocks, where a tool result is one
block among others in a single user message. If any provider path this
codebase speaks represents tool results as separate OpenAI-style
`role: "tool"` messages instead, "append a block to the same user message"
does not map and needs a per-provider design, not a single mechanic. Check
`agent/anthropic_adapter.py` / `agent/chat_completion_helpers.py` against
every provider this feature must support before implementation.

**Idle session** (no in-flight turn) — **RESOLVED, not a blocker.** The
original text here read "deliver as a new turn, framed like cron's
header/footer convention," and Round 2 rejected that framing on the theory
that an idle interactive CLI session is blocked on user input (readline /
prompt), not spinning anything that can poll `state.db` or start a turn on
its own — no such loop, Round 2 claimed, exists today.

**Round 3 finding: that premise is false.** `cli.py`'s interactive CLI is
built on `prompt_toolkit`, which is asyncio-based, not a raw blocking
`input()`/readline call. The CLI already runs a background `process_loop`
thread (started alongside the prompt_toolkit `Application`) that polls
`self._pending_input` on a 0.1s timeout in a `while not self._should_exit`
loop — running continuously regardless of whether the agent is mid-turn or
the prompt is sitting idle. On a `queue.Empty` timeout while idle, it
already calls `self._drain_process_notifications("cli-idle")` — the exact
same shape of hook this feature needs, and one that is **already shipped
and production-tested** for an adjacent use case: background terminal
processes (`terminal(background=true)`) notifying a visible, possibly-idle
CLI session of completion via `process_registry.drain_notifications()` →
claim → `self._pending_input.put(synthetic_message)`. That `put()` is
picked up by `process_loop` on its very next 0.1s tick and, if idle, starts
a fresh turn exactly like a normal typed prompt would.

Cross-session delivery for the idle case is therefore: add a
`cross_session_inbox` drain call alongside the existing
`_drain_process_notifications("cli-idle")` call site, using the same
claim-then-inject pattern already proven there. **No spike required, no
signal handling, no touching prompt_toolkit's input path at all** — the
loop that "doesn't exist" in the Round 2 framing already exists, is already
polling on a tight interval, and already has a hook for exactly this kind
of asynchronous injected notification.

This also resolves the mid-turn case's uncertainty about *which* checkpoint
to use for the idle path specifically — the mid-turn tail-append mechanic
(still needing the Round 2 verification against `agent/conversation_loop.py`
and provider wire formats, see below) and the idle drain are two genuinely
different code paths, not one mechanism serving both, and both now have a
concrete, already-shipped point of attachment.

### Tools

New module `tools/cross_session_tool.py`. Not added to `_HERMES_CORE_TOOLS`;
lives in a new opt-in toolset (e.g. `cross_session`) enabled via
`hermes tools` / `config.yaml`, following the `check_fn`-gated pattern
already used for Home Assistant / kanban / computer-use toolsets in
`toolsets.py`.

- `list_agents()` — queries `cross_session_registry` (profile-scoped, see
  below), returns name, cwd, platform, staleness-filtered by heartbeat age.
- `send_agent_message(target, body)` — resolves `target` by name (see
  Round 3 finding below on why "resolve by name" is itself underspecified;
  Claude Code's disambiguation-by-appending-an-id is cited here but not
  yet mapped onto a concrete uniqueness rule for this registry), inserts a
  row into `cross_session_inbox`.

  **Round 3 correction — Round 2's fix here contradicts the polling
  architecture and must be reverted.** Round 2 proposed returning the
  inbox row's *terminal* status (delivered/denied/expired) to the caller
  synchronously, reasoning that a purely fire-and-forget send gives the
  model no feedback and defeats the rate cap. But delivery only happens
  when the *recipient* polls and claims the row — which could be seconds,
  minutes, or (idle/held-awaiting-a-human) never. Returning "delivered"
  synchronously is therefore impossible without either (a) blocking the
  sender's own turn for an unbounded window waiting on someone else's poll
  cycle, or (b) returning a non-terminal status like `pending`/`held` —
  which is exactly the fire-and-forget outcome Round 2 was trying to
  eliminate. `send_agent_message` should return the **insert-time**
  outcome only: `queued` (accepted, will be delivered per the recipient's
  policy), `held` (queued but the recipient's policy requires human
  approval), `denied` (blocked at insert time — rate cap, hop ceiling,
  recipient unknown/stale), never a promise of eventual delivery. This
  still gives the model real feedback (a `denied` result should stop it
  from silently retrying) without violating the delivery model.

Names come from `/rename` (interactive sessions) or are derived from the
session's cwd folder name at registration time, same convention Claude
Code uses for its own agent naming — this needs to be nailed down against
real session-lifecycle code before implementation, not re-derived from
scratch.

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
`expires_at` via the same lock/expiry pattern cron already uses.

**Round 2 finding — the 5-minute default is not credible UX and the state
machine is underspecified.** Copying Claude Code's `dialogExpiry` default
assumes a synchronous approval dialog the human sees immediately; this
design instead requires the human to notice a terminal notice on a session
they may not be looking at, then separately run `hermes agents inbox
--approve`. For an idle session that's realistically never within 5
minutes — as designed, `hold` mode is functionally `refuse` with extra
steps. Needs either a much longer default expiry for CLI (e.g. hours, or
no expiry until the session's own idle-timeout), or a push notification
path (desktop notification, terminal bell) at minimum. Flagging as an open
question, not resolving here.

The state machine is also incomplete: `pending|held|delivered|denied|expired`
has no explicit transition for "approved, now waiting for the recipient to
actually poll it" — does `--approve` flip `held` back to `pending`? Who
decides `pending` vs `held` at insert time — the sender (requires reading
the recipient's policy out of the registry at send time, which races
against a policy change) or the recipient at poll time? This needs an
explicit state-transition table before implementation, not assumed.

**Round 2 finding — no threat model, and this is a real gap, not a
nice-to-have.** The message is delivered framed as **user**-role content —
user-level authority in the model's eyes. `cross_session_registry` stores
`permission_mode`, meaning any session with the toolset enabled can
enumerate other sessions' permission modes and target the most permissive
one. A gateway session driven by an untrusted external user (Telegram/
Discord) could be instructed to `send_agent_message` an instruction into a
permissive CLI session — `refuse`-by-default on gateway *inbound* does
nothing to stop a gateway session acting as *sender*. Before implementation:
(1) injected content must be framed as untrusted third-party content, not
bare user-role text — same category of care as tool output from the web;
(2) consider gating `send_agent_message` itself (not just inbound) by the
sending session's own permission mode; (3) this needs its own short
threat-model section, not a bullet in an open-questions list.

### Throttling — non-optional for v1

*(Flagged in review as the single biggest omission from the original
draft: two sessions with `inbound: accept` can ping-pong indefinitely,
each message starting or extending a turn and burning tokens, with no
human in the loop. Claude Code built rate-limiting for exactly this
reason — it can't be dropped just because it didn't show up naturally
while designing the happy path.)*

**Round 2 correctness fix — the hop_count rule below was wrong in the
prior draft and is corrected here.** The earlier phrasing ("increments
when a send is triggered by a message that itself carried a nonzero
hop_count") has a broken base case: an initial send has `hop_count = 0`
(not nonzero), so a reply triggered by it would never increment either,
and two `accept`-mode sessions can ping-pong forever at `hop_count = 0`,
under the rate cap the whole time. Corrected rule:

- Any `send_agent_message` call made **during a turn that itself delivered
  one or more cross-session inbox messages** must set the new row's
  `hop_count = max(hop_count of the messages delivered into this turn) + 1`,
  unconditionally — including the very first reply (0 + 1 = 1, not 0).
  A send made with no delivered messages in the current turn is a fresh
  chain and stays `hop_count = 0`.
- This requires tracking "which inbox rows (and their hop_counts) were
  delivered into the current turn" as in-turn state — this is new state
  that doesn't exist anywhere in the conversation loop today and needs an
  explicit home (likely alongside wherever the turn's message list is
  built). Not persisted across a session resume — acceptable for v1, but
  say so rather than leave it implicit.

Required before ship, not a follow-up:

- `hop_count`: see the corrected rule above — max delivered hop + 1, not a
  conditional-on-nonzero increment. Reject/drop sends with `hop_count` over
  a small ceiling (e.g. 4).
- Per-sender-pair rate cap: max N messages per sender->recipient pair per
  rolling window (e.g. 5/minute), enforced at insert time in
  `cross_session_inbox`.
- Hard per-turn ceiling: a single turn may send at most 1
  `send_agent_message` call (mirrors Claude Code's framing of messaging as
  a deliberate, occasional handoff, not a chat loop). **Round 2 note:**
  this is a real v1 tradeoff against a fan-out/coordinator use case (one
  turn notifying several peer sessions at once) — stating it explicitly
  rather than leaving it as an unexamined default; revisit if that use
  case turns out to matter.
- Identical-body repeat suppression within a short window (Claude Code
  does this too — cite as precedent).

INDEX ON `(to_session_id, status)` — **Round 2 note:** this serves the
delivery poll but not the per-sender-pair rate-cap check above, which
filters by `(from_session_id, to_session_id, created_at)`. Add
`CREATE INDEX idx_inbox_sender_pair ON cross_session_inbox(from_session_id, to_session_id, created_at)`
or accept a scan on that check — decide before implementation, don't
silently scan a table that's meant to stay small but has no guaranteed
bound.

**Round 2 finding — no lifecycle for `pending` messages targeting a
session that dies.** Only `held` messages have an `expires_at`. A `pending`
message addressed to a session that crashes, is killed, or is simply never
started again sits in the table forever with no expiry and no cleanup.
Needs its own TTL or a cleanup pass tied to the registry reap, not left
implicit. Related unresolved race: `list_agents` returns a session, the
user's next `send_agent_message` targets it, but the session was reaped
from the registry in between — decide whether `send_agent_message` should
re-validate liveness at send time (and what it reports back if not) rather
than trusting a stale `list_agents` snapshot.

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
   number tied to real session-lifecycle constants, not guessed. Also now
   entangled with the idle-wakeup blocker below — heartbeat-on-turn starves
   exactly the idle sessions this feature needs to reach.
3. Does `hermes agents inbox` want to be a new subcommand file under
   `hermes_cli/subcommands/`, or fits better bolted onto an existing one?
4. **New, from Round 2:** what is the acceptable UX for `hold`-mode
   approval given no synchronous dialog exists — long expiry, desktop/
   terminal-bell notification, both?
5. **New, from Round 2:** should `send_agent_message` itself be gated by
   the *sending* session's permission mode, not just the recipient's
   inbound policy, to close the gateway-as-sender privilege path?

## Review history

**Round 1** (pre-finalization) caught three structural defects, all
resolved and folded into the sections above: (1) UDS+registry.json
transport replaced with state.db once it was clear the held-message
approval flow already needed durable shared storage; (2) the mid-turn
delivery mechanic originally described "inserting a message between tool
calls," which is an API wire-contract violation (tool_result placement),
not just a cache concern — replaced with tail-appending a content block
onto the existing tool_result-bearing user message; (3) throttling was
described in the background section but absent from the design — promoted
to a required v1 component.

**Round 2** (this pass) was run specifically to check whether Round 1's
fixes actually held up, and found the v2 doc was **not implementation-ready
despite reading as finished.** Findings, in order of severity:

1. **Blocker.** The idle-session delivery path — arguably the single most
   important recipient case — assumed a receiving loop that doesn't exist:
   an idle interactive CLI session is blocked on stdin, not polling
   anything. Cron's header/footer precedent doesn't transfer, because cron
   *is* the loop rather than injecting into someone else's blocked
   process. **A throwaway spike answering "can an idle `cli.py` session
   wake up and inject a turn without corrupting the input loop" must run
   before this design is treated as final** — if it fails, idle-session
   delivery either drops to v2 or the feature narrows to gateway/ACP
   sessions (which already run event loops) for v1.
2. **Correctness bug.** The hop-count throttle as originally written never
   fires on the most common ping-pong case (base case used "nonzero"
   instead of "always increment on any delivered-message turn") — two
   `accept`-mode sessions could loop forever under the rate cap. Fixed
   above; the fix needs the same scrutiny once implemented, since it
   introduces new in-turn state that has no home yet.
3. **No threat model.** Delivering as bare user-role content, combined with
   the registry exposing `permission_mode`, is a plausible privilege path
   from a gateway session (driven by an untrusted external chat user) into
   a more permissive CLI session. This needs its own short section before
   implementation, not a footnote — added as an open question above rather
   than resolved here, since the right mitigation depends on decisions the
   user hasn't made yet (per-sender gating vs. content framing vs. both).
4. Several completeness gaps also confirmed real rather than cosmetic:
   sender gets no delivery feedback (defeats the rate cap — a model that
   never learns a send failed just retries); the inbox state machine is
   missing an explicit `held -> pending` transition and a policy for who
   decides `pending` vs `held` at insert time; `pending` messages targeting
   a dead session have no expiry; the 5-minute hold-expiry default is not
   credible without a synchronous approval UI; the provider wire-format fix
   assumes Anthropic-style content blocks and hasn't been checked against
   every provider path this codebase supports; the interrupt-checking
   checkpoint the mid-turn mechanic depends on has not actually been
   located in `agent/conversation_loop.py`.

None of this invalidates the overall shape (state.db over sockets, opt-in
toolset, tail-append for mid-turn delivery, throttling as a hard
requirement) — Round 2 explicitly reconfirmed those. What it invalidates is
calling the doc implementation-ready. Status is set to blocked at the top
of this file until the idle-wakeup spike runs and the open questions above
get real answers.

**Round 3** (this pass, run by an independent reviewer specifically
skeptical of Rounds 1–2's own process, not just their current open items)
found that the two prior rounds anchored on a flawed framing of the idle
problem and, in one case, introduced a bug while "fixing" a bug. Findings:

1. **The idle-wakeup blocker is dissolved, not just narrowed.** See the
   Idle session section above — `cli.py` already runs a continuously
   polling background thread (`process_loop`, 0.1s tick on
   `self._pending_input`) with a proven, shipped precedent for exactly
   this kind of asynchronous cross-process notification
   (`_drain_process_notifications`, feeding background-terminal-process
   completions into a possibly-idle CLI session via the same
   `_pending_input.put()` call this feature needs). Both prior rounds
   accepted the premise that no such loop exists; it does. The "narrow to
   gateway/ACP sessions only" fallback in Round 2's write-up is now moot —
   and was a bad fallback on its own terms regardless: it would have kept
   gateway sessions (which the design's own inbound-policy table treats as
   the untrusted-sender risk) in scope while cutting CLI sessions (the
   flagship Claude Code parity use case), inverting the feature's own
   trust model.
2. **Round 2's delivery-feedback fix silently broke the delivery model.**
   Returning a synchronous *terminal* status (delivered/denied/expired)
   from `send_agent_message` is incompatible with delivery being
   recipient-driven polling — see the correction inline in the Tools
   section above. This is direct evidence of anchoring: a local patch was
   applied to fix "no feedback" without re-checking it against the
   architecture the same round had just reconfirmed as sound.
3. **Delivery-guarantee mislabel.** The claim-then-inject sequence
   (`UPDATE ... SET status='delivered' WHERE id=? AND status='pending'`,
   then inject) is **at-most-once**, not "at-least-once, no duplicate
   injection" as currently written — a crash between the UPDATE committing
   and the injection actually happening silently drops the message, full
   stop. At-most-once is probably an acceptable v1 choice (an occasional
   dropped cross-session note is low-stakes), but it must be stated as a
   deliberate choice, not mislabeled as a stronger guarantee it doesn't
   provide. True at-least-once would need an intermediate `claimed`/unacked
   state and a re-delivery timeout, which is more machinery than this
   feature needs for v1.
4. **Name resolution is unspecified and has real races.** `name` has no
   uniqueness constraint in the schema. `send_agent_message(target)`
   resolving "by name" is ambiguous when two live sessions share a name; a
   `/rename` racing a concurrent send is unhandled; `from_name` is
   denormalized into the inbox row and thus stale/spoofable relative to
   the sender's current registry name. Needs an explicit rule (e.g.
   resolve against the *current* registry at send time, fail closed with a
   clear error on ambiguity rather than guessing which of two same-named
   sessions was meant) before implementation, not left as "same
   disambiguation Claude Code does" without translating that into this
   schema's actual columns and race conditions.
5. **Inbound policy enforcement point is undefined, and it matters for
   both correctness and the threat model.** If `accept`/`hold`/`refuse` is
   evaluated at *send* time (implied by the existing "recipient's policy
   read at send time races against a policy change" note), then the
   policy, the per-pair rate cap, and the hop ceiling are all bypassable by
   anything with direct `state.db` write access — same trust boundary as
   everything else here (see point 7), but worth being explicit that a
   send-time-only check is decorative, not enforced, against that
   boundary. Enforcing policy at **drain time** (the recipient evaluates
   its own current config when claiming a row, not trusting whatever the
   sender computed) closes that gap and independently resolves the
   already-flagged pending-vs-held race, since the recipient's own current
   policy is authoritative for its own inbox. This should be stated as a
   design decision (drain-time enforcement) rather than left implicit.
6. **Profile scoping is a UX boundary, not a security boundary, and the
   doc should say so plainly.** Per-profile `state.db` isolation prevents
   accidental cross-profile bleed, which is its stated purpose and is fine
   for that. But any process running as the same OS user can open any
   profile's `state.db` directly — nothing here is a security control
   against another process owned by the same user. The already-flagged
   "no threat model" gap (see below) should state the real trust boundary
   explicitly: **the OS user**, not the profile.
7. **SQLite multi-writer mechanics need explicit verification, not
   assumption.** This design turns `state.db` into a genuinely
   multi-writer database — every registered session heartbeating, every
   send inserting, every poll cycle claiming rows — where today it may be
   effectively single-writer-per-moment. Confirm WAL mode and a sane
   `busy_timeout` are set on every connection path that will touch these
   tables, or this feature will introduce `database is locked` errors into
   a session's core path, not just its own feature surface. Separately:
   `cross_session_registry`'s `ON DELETE CASCADE` FK does nothing unless
   `PRAGMA foreign_keys=ON` is set per-connection — verify the codebase
   already does this (it's easy to assume and be wrong); `cross_session_inbox`
   has no FK to `sessions` at all, which is inconsistent with the registry
   table and looks unintentional rather than deliberate, given the
   already-flagged "dead session's pending messages never expire" gap is
   exactly the kind of problem a FK + cascade would have caught structurally.
8. **The hop_count ceiling caps legitimate supervised dialogue, not just
   runaway loops, and the doc should say so rather than let it read as a
   pure anti-loop mechanism.** The Round 2 corrected rule (increment on any
   send from a turn that itself drained a delivered message, unconditionally)
   is right for stopping two `accept`-mode sessions ping-ponging
   autonomously, but it also increments on a turn a human explicitly
   triggered by typing a reply after reading a delivered/held message — a
   real back-and-forth between two sessions that a human is actively
   steering hits the same ceiling (~4) as an unsupervised loop and simply
   stops working, with no way to distinguish "runaway" from "human is
   right here approving every step." A reset-on-human-initiated-turn
   carve-out is the obvious fix but is also the obvious loophole for a
   held/idle-delivered message to exploit (is the *next* turn
   "human-initiated" just because the human happened to press Enter to
   dismiss a notification?) — this needs real design thought, not a
   one-line patch, and at minimum the doc should document the ceiling as
   bounding total conversation depth including supervised exchanges, not
   only autonomous ones, so this tradeoff is a stated decision rather than
   a surprise once implemented.

None of Round 3's findings invalidate the core architecture either — if
anything they reinforce it (state.db, tail-append, throttling-as-required)
while correcting one introduced bug (finding 2), one mislabeled guarantee
(finding 3), and dissolving the one item that was blocking implementation
outright (finding 1). Remaining before implementation: resolve findings
4–8 above (name resolution, drain-time policy enforcement, the explicit
OS-user trust boundary statement, SQLite connection settings, and a real
decision on the hop_count/supervised-dialogue tradeoff), plus the
already-open items in "Open questions" that Round 3 did not re-litigate
(toolset opt-in surface, heartbeat/TTL numbers — now decoupled from the
idle-wakeup question since that's resolved — and the `hermes agents inbox`
subcommand's home).
