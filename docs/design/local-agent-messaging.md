# Local Agent Messaging (fork-only)

Status: **APPROVED. Ready for implementation.**
Supersedes `cross-session-messaging.md`'s scope. Two Fable review rounds,
a final Fable sign-off pass, and four read-only codebase verification
passes are complete. Every Round 1 finding (8), every original open
question (5), both Round 2 gaps, every lower-severity item (cap-locking,
tool-schema naming, sender-permission gating), and every item inherited
from the predecessor doc (heartbeat/TTL, toolset name, `hermes agents
inbox` home, hold-mode UX) has a recorded, codebase-anchored decision.
**The final sign-off pass caught and fixed two real remaining issues
before approval:** (1) a wording contradiction between two decisions
about which tool (`send_agent_message` vs. `send_to_parent`) a
`background=true` subagent actually receives — fixed by clarifying
`send_to_parent` is the only subagent-facing tool, in any mode; (2) a
genuine threat-model gap — the idle-parent delivery branch, as originally
specified, would have delivered inbound messages as bare next-turn text
indistinguishable from operator input, undoing the predecessor doc's own
untrusted-content framing requirement — fixed by requiring both the
active (`steer()`) and idle (`_pending_input`) delivery branches to use
the same marked/framed envelope, stated as a mandatory implementation
requirement, not deferred. See "Closing out the remaining lower-severity
items," "Item 2" (idle/active branch), and "Final status" sections near
the end of this doc for the complete decision list. No code has been
written; implementation may now begin.

Scope: **fork-specific** (`adurham/hermes-agent`). Not proposed for upstream
in this local-only form (see "Relationship to A2A" below for the part that
IS upstream-relevant).

## What changed from the prior doc

`docs/design/cross-session-messaging.md` (3 review rounds, architecture
resolved: state.db-backed registry+inbox, tail-append mid-turn delivery,
`process_loop`-hook idle delivery, throttling, drain-time policy
enforcement) covered **session-to-session only**: two independently-started
top-level Hermes processes (two terminal tabs, a CLI session and a gateway
session, two profiles) discovering and messaging each other.

The user now wants the same capability extended to **all three pairs**:

1. **Session ↔ Session** — as designed in the prior doc. Cross-process,
   independently-started, no parent/child relationship.
2. **Session ↔ its own subagent** — a top-level session's `delegate_task`
   children. In-process (same Python process, same `AIAgent` tree),
   parent/child relationship already exists.
3. **Subagent ↔ Subagent** — siblings under the same parent, or across
   parents/sessions entirely (e.g. a subagent spawned by session A wants to
   reach a subagent spawned by session B). The sibling case is in-process;
   the cross-session case is cross-process.

The design must not just bolt subagents onto the session registry as "another
row" — subagents have fundamentally different lifetime, addressing, and
locality properties than sessions, and conflating them naively reintroduces
exactly the kind of un-scrutinized assumption Round 2/3 caught in the prior
doc. See "Why subagents are not just another registry row" below.

## Why subagents are not just another registry row

| Property | Top-level session | `delegate_task` subagent |
|---|---|---|
| Lifetime | Long-lived, human-paced (minutes to hours) | Short-lived, often seconds to a few minutes; many subagents per session over its life |
| Process locality | Independent OS process | **Same process as its parent** (and every ancestor up to the top-level session) — `AIAgent` instances in one Python process, per `tools/delegate_tool.py` |
| Identity | Stable `session_id`, persisted in `state.db` `sessions` table | Ephemeral `subagent_id` (`sa-{task_index}-{uuid8}`), exists only in the in-memory `_active_subagents` dict for the process's lifetime — never persisted today |
| Discovery today | `state.db` query (durable) | `list_active_subagents()` — in-memory snapshot, only visible to the parent process that spawned it |
| Registration cost | Heartbeat at turn boundary — cheap, session lives long enough to amortize it | A `state.db` INSERT/heartbeat for something that may live 10 seconds is disproportionate overhead, and most subagents finish before any other participant could plausibly discover and message them anyway |

The load-bearing consequence: **most subagent-involving messaging is
in-process and should never touch `state.db` at all.** A parent session
messaging its own child, or two sibling subagents under the same parent,
share one Python process — there is already a same-process delivery
mechanism for parent→child control (`interrupt_subagent`, the
`tool_progress_callback` relay in `_build_child_progress_callback`,
`_active_subagents` as the live registry). The design should extend that
same in-process mechanism for messaging, not force every subagent
interaction through the cross-process database path built for
independently-started sessions.

Cross-process subagent messaging (a subagent in session A's process wants to
reach a subagent in session B's process, or reach session B itself) is real
but rarer, and can degrade to the session-to-session path: address it as
"session B's process, subagent X" and let session B's own in-process routing
handle the last hop once the cross-process message arrives at the right
top-level process. This avoids designing a second cross-process transport
for subagents — there is exactly one cross-process transport
(`state.db`), and subagents borrow it by being addressed *through* their
owning top-level session.

## Architecture: two transports, one participant model

### Participant model (shared across both transports)

Every message-capable entity — top-level session or subagent — is a
**participant** with:

- `participant_id` — stable within its lifetime. For a session, the
  existing `session_id`. For a subagent, `subagent_id` as already generated
  today (`sa-{task_index}-{uuid8}`), scoped to its owning session.
- `owner_session_id` — for a subagent, which top-level session's process it
  lives in. For a session, itself (self-referential, or null — TBD, see
  Open Questions).
- `kind` — `session` | `subagent`.
- `parent_participant_id` — for a subagent, its immediate parent (another
  subagent, or the top-level session if it's a first-level child). Null for
  sessions. This is what makes "sibling" and "same-process" determinable
  without a live process lookup.
- `name` — human-addressable, same convention as the prior doc (`/rename`
  for sessions; for subagents, likely the task's `goal` prefix or an
  explicit name field on `delegate_task`, see Open Questions).

### Transport A — in-process (same Python process)

Applies when sender and recipient share `owner_session_id` (a session and
its own subagent tree, or two subagents under the same top-level session,
at any depth — siblings, cousins, parent-grandchild).

Mechanism: extend the existing `_active_subagents` registry
(`tools/delegate_tool.py`) — already a `Dict[str, Any]` keyed by
`subagent_id`, already thread-locked (`_active_subagents_lock`), already the
thing `interrupt_subagent`/`list_active_subagents` operate on — with a
per-participant in-memory inbox (a `queue.Queue` or a locked list, one per
registered participant, top-level session included). Sending is a direct
Python call: look up the target's queue in the shared in-process dict,
enqueue. No serialization, no polling interval, no database — the message
is visible to the recipient on its very next check.

Delivery-side hook: the recipient needs a checkpoint to actually notice the
queued message and inject it into its own turn. For a **subagent**, this is
naturally the same place `agent._interrupt_requested` is already checked
throughout `agent/chat_completion_helpers.py` (roughly a dozen call sites,
all already threaded through every provider streaming path) — a subagent
already has a proven, ubiquitous "check for external signal" checkpoint;
extend it to also drain its inbox queue at the same checkpoints, not invent
a new one. For a **top-level session's own subagent tree talking back up to
it**, that's the same `_drain_process_notifications` / `_pending_input.put()`
mechanism Round 3 of the prior doc identified for cross-process idle
delivery — it already exists specifically to inject async
completions/notifications into a running or idle CLI session, and a subagent
inbox message is architecturally the same shape of event.

This reuses two already-shipped, already-proven mechanisms
(`_interrupt_requested` checkpoints for reaching a running subagent;
`_pending_input` injection for reaching a session) rather than building a
third. **This needs verification, not assumption** (same caveat the prior
doc's Round 2 raised about the mid-turn tail-append checkpoint): confirm
`_interrupt_requested` checks are dense enough along a subagent's actual
tool-execution path (not just the streaming/generation path) that a message
sent mid-tool-call doesn't sit unseen for the tool's entire duration.

### Transport B — cross-process (`state.db`)

Applies whenever sender and recipient do NOT share `owner_session_id` — this
is exactly the session-to-session case the prior doc already designed
(state.db `cross_session_registry` + `cross_session_inbox`, polling,
tail-append mid-turn / `process_loop` idle hook, throttling, drain-time
policy enforcement — see that doc for the full mechanics, unchanged here).

**Subagents participate in this transport only by proxy through their
owning session, not by registering their own `state.db` row.** A subagent
does not get a `cross_session_registry` entry — that would mean a
heartbeat/reap cost for entities that live seconds, exactly the
disproportionate-overhead problem flagged above. Instead:

- `list_agents()` (cross-process) returns top-level sessions only, each
  optionally annotated with a live subagent count/summary (cheap: read the
  target session's... — no, a cross-process caller cannot read another
  process's in-memory `_active_subagents` dict directly. This needs an
  explicit answer, not glossed over: either (a) a session periodically
  writes a cheap in-memory-derived summary — "3 active subagents, goals:
  [...]" — into its own `cross_session_registry` row as an extra column,
  refreshed at the same heartbeat cadence, giving other processes a
  *stale-by-up-to-one-heartbeat* view of subagent activity without a
  database round trip per subagent, or (b) subagents are simply invisible
  cross-process and `send_agent_message(target="sessionB")` always means
  "deliver to session B itself, and it's session B's own in-process logic
  that decides whether to relay into one of its subagents." **(b) is
  simpler and is the recommended default for v1** — cross-process messaging
  addresses sessions; in-process messaging (Transport A) is how a session
  fans a received message out to its own subagent tree, if it chooses to at
  all. This needs an explicit decision, not left as an assumption.
- `send_agent_message(target="sessionB/subagentX", body)` — addressing a
  specific subagent cross-process — is explicitly **not supported in v1**
  under recommendation (b) above. If the user's actual use case requires
  this (e.g. "subagent in session A needs to hand a result directly to a
  specific subagent in session B, not to session B generally"), that's an
  Open Question to resolve before implementation, not something this draft
  should just assume away.

## Relationship to A2A (the upstream-relevant part)

The existing `plugins/platforms/a2a/` plugin implements the real Agent2Agent
protocol spec — HTTP + JSON-RPC, Agent Cards served over
`/.well-known/agent.json`, bearer-token auth, `A2A_TRUSTED_PROXIES` XFF
handling, etc. That plugin is untouched by this design and remains the
genuinely-network-facing, spec-compliant, upstream-mergeable A2A
implementation for talking to **remote, independently-operated** Hermes
instances or other A2A-compliant agents.

This design's local transports (A and B above) are a **second, separate
implementation of A2A-shaped concepts**, not a modification of the A2A
plugin's code:

| A2A spec concept | Local equivalent |
|---|---|
| Agent Card (`GET /.well-known/agent.json`) | A `cross_session_registry` row (session) or an in-process `_active_subagents` entry (subagent) |
| JSON-RPC `message/send` | `send_agent_message()` tool call, routed via Transport A or B depending on locality |
| Task state (`submitted`/`working`/`completed`/...) | `cross_session_inbox.status` (`pending`/`held`/`delivered`/`denied`/`expired`) for Transport B; an in-memory queue slot's presence/absence for Transport A |
| Bearer-token auth + identity resolution | `permission_mode`-based inbound policy (drain-time enforced, per the prior doc's Round 3 finding) — OS-user is the real trust boundary locally, there is no network attacker to authenticate against |

**Compatibility requirement for this design**: the tool-facing surface
(`list_agents`, `send_agent_message`, whatever the actual tool names end up
being) and the underlying message/participant data model should be defined
independently of which transport backs a given call, so that a third
transport — a real A2A HTTP client, for genuinely remote participants —
could be added later as a drop-in without changing the tool schema the model
sees. Concretely: define a small transport-selection interface (e.g.
`resolve_transport(participant_id) -> Transport`, where `Transport` is
`InProcessTransport | LocalDBTransport | (future) A2AHttpTransport`) rather
than hardcoding "if same process, do X, else do Y" inline in the tool
implementation. This is a real design decision this draft is making
explicitly, not an afterthought — it's the mechanism that keeps the promise
"compatible so its backend could use A2A" concrete rather than aspirational.

## What carries over unchanged from `cross-session-messaging.md`

Everything about Transport B's internals is unchanged and this doc does not
re-litigate it: state.db schema (`cross_session_registry`,
`cross_session_inbox`), tail-append mid-turn delivery mechanic (with the
still-open Round 2 verification against `agent/conversation_loop.py`'s
actual checkpoint and every provider's wire format), the idle-session
`process_loop`/`_drain_process_notifications` hook (Round 3 resolution),
throttling (hop_count, per-pair rate cap, per-turn ceiling), drain-time
policy enforcement, profile scoping as a UX not security boundary, and the
still-open items from that doc's "Open questions" section (toolset opt-in
surface name, heartbeat/TTL numbers, `hermes agents inbox` subcommand home,
name-resolution races, SQLite WAL/busy_timeout verification, the
hop_count-vs-supervised-dialogue tradeoff). Read that doc in full before
implementing Transport B; this doc only adds the subagent-facing Transport A
and the routing decision between them.

## Open questions for this doc specifically — ALL RESOLVED, see "Resolution of original Open Questions" near the end of this doc

1. **Cross-process subagent addressing** — RESOLVED, see Finding 7 decision
   (sessions-only cross-process; no subagent fan-out addressing in v1).
2. **Subagent naming** — RESOLVED, see "Resolution of original Open
   Questions" below (no name field; opaque `subagent_id` + goal record is
   sufficient).
3. **`_interrupt_requested` checkpoint density** — RESOLVED, see the
   Verification pass section (the real mechanic is `steer()`, not
   `_interrupt_requested`, and delivery happens at tool-batch boundaries
   regardless of individual tool duration).
4. **Toolset default vs. gated** — RESOLVED, see "Resolution of original
   Open Questions" below (mode-gated: only `background=true` delegations
   get `send_agent_message`; no subagent gets `list_agents` at all).
5. **Lost message on subagent exit** — RESOLVED, see Findings 3 and 8
   (synchronous tool error for a dead/unknown target at send time; the
   distinct end-of-turn race is surfaced in the handoff, not silently
   dropped).

## Explicitly out of scope for v1 (inherited + new)

- Everything already out of scope per the prior doc (cross-machine relay,
  agent-team structured protocols, Windows, UDS wakeup optimization).
- A real A2A-HTTP-backed transport for remote participants — the interface
  point is designed for it (see "Relationship to A2A") but building it is
  not part of this feature.
- Cross-process addressing of a specific subagent by ID (see Open Question 1)
  unless the user's sign-off says otherwise.

## Round 1 review (Fable) — real defects, not resolved here

Run specifically to stress-test this draft the same way the predecessor
doc's 3 rounds stress-tested the session-only design. Verdict: the overall
shape (two transports, one participant model, sessions-only cross-process,
no registry rows for subagents) is sound and survives scrutiny. What's
**not** ready, in order of severity:

1. **Blocker-class: Transport A's subagent-delivery half is named, not
   designed.** The doc says "piggyback on `_interrupt_requested` check
   sites" — but those sites abort/redirect generation; they do not answer
   how message *text* actually enters the subagent's transcript. The
   predecessor doc solved the equivalent problem for sessions with a
   specific mechanic (tail-append onto the tool_result-bearing user
   message). This doc has no equivalent mechanic for a subagent — it cites
   the checkpoint without specifying the injection. Before this section is
   treated as settled: confirm whether the predecessor's tail-append logic
   lives on `AIAgent`/the conversation loop (reusable by a subagent, which
   is also an `AIAgent` instance) or is coupled to `cli.py` (in which case
   Transport A's subagent half needs its own new mechanic, and the doc is
   currently underselling that cost by calling it "reuse").

2. **Blocker-class, and this is the same failure mode Round 3 caught on the
   session side: Open Question 3 (checkpoint density) is mislabeled as a
   detail when it's the load-bearing assumption.** Typical `delegate_task`
   subagents spend most of their wall-clock **inside tool execution**, not
   in the streaming/generation paths where `_interrupt_requested` is
   checked. If there is no check on the tool-execution path, Transport A
   subagent delivery fails in the *common* case, not an edge case. This
   must be verified (grep + read `agent/chat_completion_helpers.py` and the
   tool-dispatch path) before the architecture is called resolved — treat it
   with the same rigor Round 3 applied to the idle-session premise, not as
   an afterthought bullet.

3. **The uniform tool schema hides divergent delivery guarantees, and the
   sender has no way to tell which one applies.** Transport B is durable
   (state.db, explicit status column); Transport A is lose-on-exit
   in-memory with no persistence at all. One `send_agent_message` tool
   covers both, so the model calling it cannot reason about which guarantee
   it just got. Concretely: the tool's return value needs a delivery-mode/
   outcome field (e.g. `queued-durable` / `queued-ephemeral` /
   `recipient-gone`), not just a bare success. Related: unregistered
   subagents leave no tombstone, so a sender can't distinguish "this
   subagent_id never existed / was mistyped" from "it already exited" —
   send-to-a-just-exited-subagent should be a **synchronous tool error**,
   not a silently dropped queued message. This sharpens Open Question 5
   from "is losing the message acceptable" to "the sender must be told, one
   way or another, every time."

4. **Transport A has zero throttling, and the predecessor doc's whole
   throttling section (hop_count, rate caps, per-turn ceiling) was written
   for Transport B only.** A session-and-its-own-subagent (or two siblings)
   ping-ponging in-process has no rate control, and inbox drains now run
   inside hot streaming paths at a dozen-plus checkpoints. This needs an
   explicit answer, not an inherited one — Transport A's throttling cannot
   just cite the Transport B section, because the mechanics (in-memory
   queue, no persisted `hop_count` column) don't carry over as-is.

5. **The cross-process relay story is real but overstated as written.**
   Two distinct problems, both real:
   - "The receiving session's Transport A logic decides whether/how to
     relay" implies routing logic that doesn't exist under the
     sessions-only recommendation — there is no intended-final-recipient
     field in a cross-process envelope, so there is nothing to route *on*.
     What actually happens is **model-mediated relay**: the receiving
     session's LLM reads the delivered message and may choose, at its own
     discretion, to forward it into one of its subagents by calling
     `send_agent_message` itself. That's a legitimate v1 answer, but the
     doc should say "nondeterministic, prompt-dependent forwarding," not
     describe it as if transport-layer logic handles it.
   - **Relay is impossible while the parent is blocked on a synchronous
     `delegate_task` call.** If delegation is synchronous, the parent
     session's LLM cannot act on (relay) a delivered cross-session message
     until its blocking tool call returns — by which point the subagent
     it might have relayed to has typically already finished. This forecloses
     the "external session steers a currently-running subagent via relay"
     use case entirely for synchronous delegation, not just indirectly. The
     doc must state this limitation explicitly rather than let "sessions
     decide whether to relay" imply it generally works.

   Despite both of these, the sessions-only recommendation (b) is still
   correct for v1 — the alternative (direct cross-process subagent
   addressing) reintroduces the registry-row and lifetime-race costs this
   doc correctly avoided. The fix here is honesty about what v1's relay
   actually delivers, not a different recommendation.

6. **`resolve_transport(participant_id)` has no specified behavior for an
   unknown ID.** A foreign `sa-*` ID — learned from a relayed message, or
   simply hallucinated by the model — resolves to neither transport today.
   This needs an explicit error path in the design; the model will try
   addressing IDs it has no business addressing, and "undefined behavior"
   is not an acceptable answer for a tool surface.

7. **Sibling discovery is a capability-scoping decision currently hiding
   inside Open Question 4, and it's bigger than a toolset checkbox.**
   Subagent-to-subagent messaging requires a subagent to be able to
   discover its siblings — i.e. some subagent-facing equivalent of
   `list_active_subagents()`, which today serves only the parent. Granting
   that access also raises whether subagents should get reach into
   `interrupt_subagent` over their siblings. This is a real scoping
   decision (how much control does a subagent get over its peers) that
   deserves its own paragraph, not a bullet buried under "does a subagent
   get these tools by default."

8. **Delivery-window race, same class as the predecessor doc's mid-turn
   findings, not yet addressed here.** A message can arrive in the gap
   between `_register_subagent` and the subagent's first checkpoint, or
   mid-stream at an arbitrary token boundary. Where the injected content
   lands relative to in-flight assistant output needs the same explicit
   mechanic the predecessor doc's tail-append design received — this doc
   currently has no equivalent treatment for the subagent side.

None of these invalidate the overall two-transport shape — Fable explicitly
reconfirmed the split, the no-registry-row-for-subagents call, and the
sessions-only cross-process recommendation as architecturally sound. What
they invalidate is treating Open Question 1 as ready for sign-off in its
current, optimistic framing (relay "generally works") and calling
Transport A's subagent-delivery mechanic settled when it's actually
unspecified. Before implementation: resolve findings 1–2 (the actual
injection mechanic + real checkpoint-density verification) as blockers,
then 3–8 as required-before-ship, matching the predecessor doc's own
severity convention.

## Verification pass (read-only, post Round 1) — findings 1 & 2 resolved favorably

Findings 1 and 2 above were marked blocker-class specifically because they
were *unverified assumptions*. Read `run_agent.py`, `agent/tool_executor.py`,
`agent/agent_runtime_helpers.py`, and `agent/prompt_builder.py` directly
rather than continuing to assume. Result: **the injection mechanic already
exists, already ships, already covers subagents, and is not the same thing
the doc originally cited.**

### Finding 1 resolved — the real mechanic is `steer()`, not `_interrupt_requested`

The doc's original text ("piggyback on `_interrupt_requested` check sites")
was reaching for the wrong existing mechanism. The actual mid-turn,
non-disruptive delivery path is `AIAgent.steer(text)`
(`run_agent.py:3297`):

- `steer()` stashes text in `agent._pending_steer` (lock-protected) and
  **does not interrupt anything** — the docstring is explicit: "Unlike
  `interrupt()`, this does NOT stop the current tool call." This is exactly
  the delivery semantics this design needs (deliver a message without
  aborting in-flight work), and it is a *different, better-suited*
  mechanism than the interrupt-flag checkpoints the doc originally cited.
- Delivery point: `agent/agent_runtime_helpers.py`'s
  `apply_pending_steer_to_tool_results()` appends the pending steer text to
  the **last tool-result message of the just-finished batch**, wrapped in
  an explicit marker (`format_steer_marker()` /
  `STEER_MARKER_OPEN`/`STEER_MARKER_CLOSE`, `agent/prompt_builder.py:670`).
  This is called from all three tool-execution paths —
  `execute_tool_calls_concurrent`, `execute_tool_calls_sequential`, and
  `execute_tool_calls_segmented` (`agent/tool_executor.py:1366, 2143, 2204`)
  — i.e. every tool-dispatch code path this codebase has, not a partial
  covering.
- **This IS the exact mechanism that delivered the user's own out-of-band
  message earlier in this session** (see: the marker text literally reads
  "OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered
  mid-turn; not tool output"). It is not a theoretical fit — it was
  observed operating, live, on this very session's top-level agent, mid
  multi-tool-call turn.
- **Confirmed subagent-compatible, not session-only.** `delegate_task`
  children are built as real `AIAgent` instances (`tools/delegate_tool.py`
  line ~1721: `child = AIAgent(...)`) — the *same class*
  (`run_agent.py:413`), inheriting `steer()`/`redirect()` directly, no
  separate subagent-specific code path. `STEER_CHANNEL_NOTE` — the system
  prompt text instructing the model how to interpret the marker — is
  injected via `agent/system_prompt.py:243`, which runs for every `AIAgent`
  including children, not gated to top-level sessions only.
- `redirect()`'s own docstring independently confirms intended subagent
  reach: "Never kill a tool merely to deliver conversational guidance...
  including delegate_task children" (`run_agent.py:3372`).

**Consequence for the design:** Transport A's subagent-delivery mechanic
does not need to be invented — extend `steer()`/`_pending_steer` to also be
fed by a drained cross-participant inbox message (in addition to its
current direct callers), and the existing
`apply_pending_steer_to_tool_results` delivery path, marker format, and
system-prompt framing all carry over unmodified. This also uses an
*existing*, already-user-facing marker convention
(`STEER_MARKER_OPEN`/`CLOSE`) rather than inventing a new framing — directly
addressing the predecessor doc's Round 2 threat-model concern about
delivering content as unmarked bare user-role text: `steer()` messages are
already marked as coming from an out-of-band channel, not silently blended
into ordinary turn content.

### Finding 2 resolved — checkpoint is end-of-tool-batch, not mid-single-tool, and that's already the accepted tradeoff

The steer drain fires **after the current tool-call batch finishes**, in
all three execution paths, guarded by the same `finalize` flag that also
gates aggregate budget enforcement. This means:

- A steer/inbox message sent while a subagent is mid-single-long-tool-call
  (e.g. a 10-minute `terminal` command) is delivered the moment that batch
  completes, not instantly — but this is the *documented, intentional*
  latency profile the codebase already accepted for `redirect()`
  degrading to `steer()` specifically "so the tool can finish at a safe
  boundary" (`run_agent.py:3372`, comment: "Never kill a tool merely to
  deliver conversational guidance"). This is not a gap Transport A
  introduces — it's the same tradeoff already made and shipped for
  human-originated mid-turn corrections, and Transport A inherits it for
  free by reusing the mechanism rather than inventing a parallel one.
- The original worry ("subagents spend most of their wall-clock inside
  tool execution, not the streaming paths where checks live") is answered
  differently than feared: there IS no need for a check *during* tool
  execution — delivery happens at the batch boundary regardless of how long
  the batch took, by construction of where `_apply_pending_steer_to_tool_results`
  is called from. The common case (a subagent inside a tool call) is
  exactly the case this mechanism already handles correctly.

### What this resolves vs. what's still open

Findings 1 and 2 (both blocker-class) are resolved: the mechanic exists, is
proven in production (observed live this session), and already covers
subagents by construction, not by extension work.

Findings 3–8 are **not** resolved by this verification pass and still need
explicit design decisions before implementation:

- **Finding 3** (delivery-mode/tombstone feedback) has a directly reusable
  precedent now identified: `interrupt_subagent()`
  (`tools/delegate_tool.py`) already returns `False` when
  `_active_subagents.get(subagent_id)` misses — i.e. the "subagent doesn't
  exist / already exited" synchronous-error check this finding calls for
  is an existing, one-line-reusable pattern, not new design. Still needs to
  be wired into `send_agent_message`'s target-resolution path explicitly.
- **Finding 4** (Transport A throttling) — `_pending_steer` is a single
  string slot that concatenates on repeat calls
  (`self._pending_steer = self._pending_steer + "\n" + cleaned"`), with no
  count or rate limit of its own. Confirms the finding as-is: nothing here
  provides hop-count or rate-cap semantics; that machinery still needs to
  be designed for Transport A specifically, it does not come along for
  free with reusing `steer()`.
- **Findings 5–8** unaffected by this pass — still open as written.

Status update: **no longer blocked on unverified assumptions.** The doc's
core delivery mechanic is now real and confirmed. Remaining open items
(3–8) are scoping/policy decisions, not further code-archaeology.

## Decisions (resolved with the user, post-verification)

### Finding 7 — sibling discovery/messaging: OUT OF SCOPE for v1

**Decision: no sibling-to-sibling messaging in v1.** Subagents may only
message/receive from their own owning session (parent). Sibling
coordination (two subagents in the same `delegate_task` batch signaling
each other) is explicitly deferred, not designed now, and revisited only if
a real multi-worker-coordination use case materializes (e.g. a
file-per-worker refactor pattern where workers need to avoid stepping on
each other) — not built speculatively ahead of that need.

Rationale (Fable consult, confirmed against the real dispatch code): the
only *verified* existing use case for parallel subagent batches is
unrelated siblings that need zero communication (e.g. one subagent building
a feature while another investigates an unrelated bug, dispatched in the
same batch purely for parallelism). Granting siblings list+message reach
over each other would upgrade every batch dispatch — including that
verified unrelated-work pattern — into a shared-visibility group, and
because the parent currently only sees final aggregated results (not live
subagent-to-subagent traffic), sibling messaging would create an
unobservable side channel: debugging "why did subagent 3 do X" would
require tracing cross-subagent messages, not one linear transcript, and a
confused or manipulated subagent could steer its siblings mid-turn with no
real-time parent visibility.

**A real gap surfaced while evaluating the "relay through the parent
instead" alternative, and it's now documented rather than left implicit:**
relay-through-parent as a *live coordination path* only works when the
parent's own conversation loop keeps running independently — i.e.
`background=true` delegation. For a **synchronous** `delegate_task` batch,
the parent's tool-execution thread is blocked inside the batch's polling
loop (`tools/delegate_tool.py`, the `while pending: ... _cf_wait(...)` loop
around line 3696) for the batch's entire duration — it can *receive* a
steer (queued into `_pending_steer`) but cannot *act* on it (e.g. call
`send_agent_message` to relay to another child) until the whole batch
returns and its own turn resumes. This is not a defect introduced by this
design; it's a preexisting property of synchronous delegation that this
design's punt on sibling messaging means we don't have to solve yet — but
it's worth stating plainly: for synchronous batches, "the parent will
relay" is not a live capability during the batch, only after it.

### Finding 4 — Transport A throttling

**Decision, per Fable's recommendation:** size caps only, enforced at the
messaging-tool layer (not inside `steer()` itself, since `steer()` also
serves the human `/steer` UX and must not inherit this feature's limits).
No per-turn send-count cap — the failure mode here is unbounded *bytes* in
the single-slot `_pending_steer` string, not unbounded *message count*, and
a subagent's own `delegation.max_iterations` budget already terminates a
runaway parent↔child loop regardless of message traffic.

- **Per-message cap: 4KB (~1K tokens)** on a single `send_agent_message`
  call's body. This is a coordination/steering channel, not a data-transfer
  channel — a caller that needs to hand over a large artifact should write
  it to a file and send the path, not the content.
- **Coalesced pending-slot cap: 16KB (~4K tokens)** on `_pending_steer`'s
  total size at send time. A send that would push the slot over this limit
  is **rejected at send time** (synchronous tool error: "recipient has N
  unread bytes pending; retry after it reaches its next tool-batch
  boundary"), not silently truncated. Rejection over truncation because the
  sender is an agent capable of reacting to tool-result feedback (retry,
  wait, summarize) — truncating instead risks silently cutting an
  instruction in half, which is a worse failure than a visible delay.

**Real gap this decision closes, flagged directly by the same consult and
folded in rather than left as a blind spot:** a bytes-only cap protects the
parent↔single-subagent case (self-limiting because the subagent's own
`max_iterations` budget bounds it), but **does not, by itself, protect
against two long-lived, non-`max_iterations`-bounded participants
ping-ponging in-process** — the same failure class Transport B needed
`hop_count` for. Per the sibling-messaging punt above (Finding 7), Transport
A's only live v1 pairing is parent-session ↔ its own subagent, and the
subagent side of every such pair is always `max_iterations`-bounded by
construction — so this gap has no live surface in the v1 scope as designed.
**This constraint must be enforced explicitly in code** (Transport A's
send path should verify the target participant is a bounded subagent, not
assume it), not left as an implicit consequence of "we didn't build sibling
messaging" — if a future change (e.g. reopening Finding 7, or a different
kind of long-lived in-process participant) removes that bound on one side
without re-adding a hop_count-equivalent, the same runaway ping-pong
Transport B was built to prevent reappears in-process with no counter-
measure. Flagging this explicitly here so it isn't rediscovered the hard
way later.

### Finding 3 — delivery feedback / tombstones

**Decision: reuse `interrupt_subagent()`'s existing not-found pattern
directly.** `send_agent_message` targeting a subagent_id must check
`_active_subagents` the same way `interrupt_subagent()` already does
(`tools/delegate_tool.py`) and return a synchronous tool error — not a
silently-queued-then-dropped message — when the target is missing (never
existed, or already exited/unregistered). This is a one-line reuse of an
existing, proven check, not new design.

For Transport A generally, the tool's return value should also surface
which delivery mode applied (`queued-ephemeral` for Transport A vs.
whatever Transport B's equivalent states are, per the predecessor doc), so
the calling model can reason about the guarantee it actually got rather
than treating both transports as identical through one tool schema.

## Still open (not yet resolved with the user)

- **Open Questions 1–5** from the original open-questions section (cross-
  process subagent addressing sign-off, subagent naming convention, whether
  subagents get these tools in their default toolset or gated per-role, the
  toolset opt-in surface name, heartbeat/TTL numbers inherited from the
  predecessor doc) — see next section for resolution of most of these.

### Finding 5 — cross-process relay honesty: RESOLVED (wording fix, no new design)

No architecture change. Every place this doc describes cross-process
delivery to a session that might then reach one of its own subagents must
say **"the receiving session's LLM may, at its own discretion, relay the
message into its subagent tree by calling `send_agent_message` itself —
this is model-mediated forwarding, not transport-layer routing, and there
is no intended-recipient field that makes it automatic."** Additionally:
this relay capability is **only live during a `background=true` delegation
batch**, where the parent's own conversation loop keeps running
independently of the batch. During a **synchronous** batch, the parent's
tool-execution thread is blocked inside the batch's own polling loop (see
the Finding 7 section above) and cannot act on anything — including
relaying a just-received cross-session message — until the batch returns.
State this explicitly wherever relay is discussed; do not let "sessions can
relay" read as a general capability when it depends on which delegation
mode is in use.

### Finding 6 — unknown participant ID: RESOLVED

**Decision:** `resolve_transport(participant_id)` returns an explicit,
typed "not found" result (not `None`, not an exception that propagates
unhandled) for any ID that matches neither a live `_active_subagents` entry
nor a `cross_session_registry` row. `send_agent_message` surfaces this as a
synchronous tool error with a clear message (e.g. "no active session or
subagent matches '<target>' — it may have already exited, or the ID may be
incorrect"). This reuses the same category of check Finding 3 already
established for the exited-subagent case — a foreign, hallucinated, or
stale ID hits the identical code path as "target already gone," which is
the correct behavior (the caller doesn't need to distinguish "never
existed" from "existed and is now gone" — both mean "can't deliver right
now," and the tool error message can be worded generically enough to cover
both without lying about which one it is).

### Finding 8 — delivery-window race: RESOLVED, real gap found and fixed (design-level, not yet code)

This is not a race that turns out to be benign — it's a real, confirmed
gap in the current codebase, independent of anything this design adds.
`agent/turn_finalizer.py` already produces `result["pending_steer"]`
whenever a steer/message arrives after a turn's last tool batch but before
the model's final text response (nothing left to append it to). For a
**top-level session**, `cli.py` already reads this field and redelivers it
as the next turn (`self._pending_input.put(_leftover_steer)`,
`cli.py:16920`). For a **subagent**, `tools/delegate_tool.py`'s full child
lifecycle (`_run_single_child`, `_finalize_child_results`,
`_run_child_lifecycle`) never reads `result["pending_steer"]` anywhere —
**a message landing in this window for a subagent is silently dropped
today, with no error, no trace, independent of this feature** (the
predecessor `steer()` mechanism already has this gap for direct human
`/steer` calls into a subagent; this design would simply inherit it
unless fixed).

**Decision (Fable consult): surface it in the handoff, do not reawaken the
subagent for another turn.** When `_finalize_child_results` sees
`result.get("pending_steer")` on a completed child, attach it to the
delegate_task result as a structured field (e.g.
`unprocessed_messages: [...]`) alongside a one-line human-readable note in
the summary the parent sees, rather than (a) silently dropping it or (b)
spinning the subagent back up for an additional turn to process it.
Rejected re-awakening specifically because it has a real, non-trivial
recursion cost with little payoff: a re-awakened turn can itself end in the
same race window (needs a cap + fallback — which is just this same
surface-in-handoff behavior one level down), it raises unresolved questions
about whether re-awaken turns count against `delegation.max_iterations`,
and it requires changing both the synchronous-wait and `background=true`
polling paths to understand "finished, but not really yet." The parent —
which is already the thing waiting on and routing the result — is better
positioned to decide what to do with a surfaced unprocessed message
(re-delegate with it included, handle it directly, or ignore it if the
completed work already covers it) than a subagent forced back to life with
a stale, budget-exhausted context.

This fix is in scope for this feature's implementation (it's the delivery
mechanism this design depends on), even though the underlying gap in
`delegate_tool.py` predates this design and would be worth fixing on its
own regardless of whether this messaging feature ships.

## Resolution of original Open Questions 2 and 4

### Question 2 — subagent naming: NO name field. Use `subagent_id` + goal record as-is.

**Decision (Fable consult):** do not add a `name` param to `delegate_task`'s
task schema. Rationale:

- The consumer addressing a subagent is always the parent's **LLM**, not a
  human — it doesn't need a mnemonic, it needs an unambiguous key plus
  enough context to pick the right target, and `list_active_subagents()`
  already returns exactly that (`{subagent_id, goal, model, started_at,
  ...}`). Copying an ID out of a record the model just read is trivial;
  there's no cross-turn recall burden since the record stays retrievable.
- The cost of adding a name field is real and paid on **every**
  `delegate_task` call regardless of use (schema/tool-description tokens),
  and it drags in two under-specified decisions with no clean default:
  fallback derivation when omitted, and collision handling when a batch
  produces two identical names (silent suffixing changes the name the
  caller chose; erroring makes `delegate_task` fail for a cosmetic reason).
- The one scenario where a naming scheme would genuinely earn its cost —
  addressing across a sibling group without registry access — is exactly
  the case Finding 7 already put out of scope. Designing a naming scheme
  now, for a discovery model that doesn't exist yet, risks building the
  wrong primitive for whatever sibling-addressing v2 eventually needs.
- The child-to-parent direction needs no naming at all — there is exactly
  one valid target (the parent), so no disambiguation is possible in the
  first place.

**Real gap this surfaced, now in scope for implementation:** verified via
code read that `delegate_task`'s per-child result records key on
`task_index` (`tools/delegate_tool.py`, e.g. line ~2588), **not**
`subagent_id` — the parent does not currently get the `subagent_id` back
in its own result at all, only internally in the (agent-invisible)
`_active_subagents` registry record. This must be fixed as part of this
feature: `delegate_task`'s result payload needs to surface each spawned
child's `subagent_id` directly (not force the parent to a separate
`list_active_subagents()` round trip just to learn the ID of a child it
just spawned itself). Additionally, `send_agent_message` should fail
helpfully on an unknown/mistyped ID by including the current valid IDs +
goals in the error text — a self-correcting loop for the one real failure
mode (the parent hallucinating or mistyping an ID), rather than a new
namespace to prevent it.

### Question 4 — toolset default vs. gated: mode-gated, and `list_agents` is dropped from subagents entirely.

**Decision (Fable consult):**

- **`send_to_parent` (the subagent-side messaging tool — see the "Tool
  schema bifurcation" decision later in this doc, which supersedes any
  earlier reference to a subagent getting `send_agent_message` directly)
  is included in a subagent's toolset only when the delegation is
  `background=true`.** Per the already-resolved Finding 7/5 constraint, a
  synchronous batch's parent is blocked and cannot react to anything a
  subagent sends until the whole batch returns — shipping this tool's
  schema to every sync-mode subagent (multiplied per-worker in a batch,
  e.g. 3× for the file-refactor-workers example) is pure token cost with no
  live consumer, exactly the case `AGENTS.md`'s "bar for a new core tool is
  high" principle exists to prevent. Anything a sync subagent would want to
  say by the end of a batch is already covered by its normal return value.
  **`send_agent_message` (the parent/session-side tool, with a `recipient`
  parameter) is never given to a subagent, in any mode** — only
  `send_to_parent` is subagent-facing, and only under `background=true`.
  This is stated explicitly here to close a wording ambiguity a final
  review pass caught: an earlier draft of this decision named
  `send_agent_message` generically before the two-distinct-tools decision
  existed; the tool a `background=true` subagent actually receives is
  `send_to_parent`, never the recipient-taking `send_agent_message`.
- **No subagent gets `list_agents` at all, in any mode.** Under the
  Finding 7 decision, a subagent has exactly one valid recipient — its own
  parent — so a discovery tool whose answer is always the same single value
  is dead schema weight. `send_to_parent`, when called from a subagent,
  implicitly targets the parent; there is no recipient parameter for a
  subagent to fill in, and no reason to expose a tool whose only possible
  discovery result never varies.
- **Mode-gating is primary; role-gating (`_blocked_toolsets_for_role`) can
  still layer on top** as a secondary refinement (e.g. a `background=true`
  haiku-model researcher subagent still might not need messaging even
  though the mode check passes) — but mode is the right primary gate
  because it tracks actual capability (can the parent even receive right
  now?), not a judgment call about whether a given role finds it useful.
- Explicitly deferred, not designed now: whether a **synchronous**
  subagent should get a narrower, receive-only or queue-for-post-batch-
  reading variant of messaging as a structured side channel separate from
  its return value. Flagged as a plausible v2 idea, not built speculatively
  for v1.

## Remaining items inherited from the predecessor doc (still genuinely open)

These were open in `cross-session-messaging.md` and are unaffected by any
of this session's subagent-focused work — they apply to Transport B only
and still need real decisions before implementation:

- Heartbeat interval and registry TTL numbers (proposed "update at every
  turn boundary, reap after 2x the interactive idle-timeout" but no
  concrete number has been tied to real session-lifecycle constants yet).
- The toolset opt-in surface name (a new `cross_session` toolset via
  `hermes tools`, or folded into an existing catalog entry) — and by
  extension, what this doc's combined feature is actually called/gated as,
  now that it covers both transports under one conceptual `send_agent_message`
  surface.
- Whether `hermes agents inbox` (for resolving `hold`-mode approvals) wants
  its own file under `hermes_cli/subcommands/`, or fits better bolted onto
  an existing one.
- The `hold`-mode UX question (long expiry vs. desktop/terminal-bell
  notification) and the `send_agent_message`-gated-by-sender's-own-
  permission-mode threat-model question, both still explicitly open in the
  predecessor doc.

## Status: ready for a second review pass, not yet ready for implementation

Every finding from Round 1 (Fable) and every open question this doc
originally posed has a recorded decision. What remains before code is
written:

1. A second review pass (Fable or equivalent) specifically re-checking
   *these* decisions the way Round 1 checked the original architecture —
   this doc has not yet had that pass.
2. The predecessor doc's still-open Transport B items listed immediately
   above, which this session's work did not touch.
3. User sign-off on the accumulated decision set as a whole, not just
   individually as each was made in this session.

## Round 2 review (Fable) — the decision set holds up, with one real gap in the flagship flow

Sent the full decision set (all 8 Round 1 findings + all 5 original open
questions, as resolved above) back to Fable specifically to check the
*decisions*, not the architecture (already reconfirmed sound in Round 1).
Verdict: the decisions are internally consistent and nothing here is
deferral dressed up as resolution — but one gap is real and sits on the
only messaging pairing that's actually live in v1.

### Confirmed and closed by this pass

- **Decisions 2 + 9 compose correctly, and this doc hadn't noticed the
  implication.** For a **synchronous** child specifically: the child has no
  `send_agent_message` tool (decision 9, mode-gated to `background=true`
  only) AND the parent's thread is blocked inside the batch's polling loop
  (decision 2's confirmed gap) — so for sync children, **both directions of
  Transport A are dead in v1**, not just one. This is coherent, not a
  contradiction, and it usefully narrows where the remaining gap (below)
  actually needs to be checked: only the `background=true` case is live at
  all.
- **Decision 7's fix (`unprocessed_messages` in the delegate_task result)
  needed an explicit consistency check, and it passes.** Since the
  end-of-turn race can only fire for `background=true` children (per the
  point above), the fix must actually surface through the background
  collection/poll path (`tools/async_delegation.py`), not just the
  synchronous `_finalize_child_results` path this doc originally described
  it against. **This must be verified against the real background-collect
  code before implementation** (same discipline as decision 1's
  verification) — not yet done as of this pass, flagged as a concrete
  to-do below rather than assumed resolved.
- **Decision 7 also needed a sender-facing contract statement this doc was
  missing:** a successful `send_agent_message` call means **queued, not
  delivered** — the message can still bounce back as an `unprocessed_messages`
  note later. State this explicitly as the tool's actual contract, not just
  as an implementation detail buried in the Finding 8 writeup.

### Real gap found, not yet resolved: the flagship flow (background child → idle parent) is unverified

Decision 1's verification proved delivery works **mid-turn**, at
tool-batch boundaries. But the primary v1 use case for `background=true`
delegation is exactly the case where the **parent has already finished its
turn and is sitting idle** — no tool batch is coming for `steer()` to
attach to.

**Confirmed via direct code read (`cli.py`): calling `agent.steer()` while
no turn is running does nothing useful.** The CLI's own `/steer` command
already encodes the correct pattern explicitly (`cli.py`, `canonical ==
"steer"` branch, ~line 11265): it checks `self._agent_running` first —
**if true**, calls `self.agent.steer(payload)` (drained at the next
tool-batch boundary, per decision 1); **if false**, falls back to
`self._pending_input.put(payload)`, i.e. delivers it as an ordinary
next-turn message instead, exactly the same idle-delivery mechanism Round 3
of the predecessor doc validated for cross-process notifications. This is
not a subtle race — `_agent_running` is a plain, unlocked `cli.py`-level
bool, and the codebase's own `/steer` command already has to branch on it
for exactly this reason (its own docstring: "process_loop is blocked inside
self.chat() for the duration of the run... by the time the queued command
is pulled from `_pending_input`, `_agent_running` has already flipped back
to False").

**Decision needed, not yet made:** Transport A's delivery-side logic for a
background child messaging its parent must replicate this same branch —
check whether the parent's turn is currently active
(`_agent_running`-equivalent state on the target `AIAgent`/CLI, not just
existence in `_active_subagents`) and route to `steer()` if live or
`_pending_input`-equivalent injection if idle — rather than unconditionally
calling `steer()` and silently doing nothing when the parent happens to be
idle at send time. Without this, Transport A risks being, per Fable's
framing, "a notification system that's actually a dead-letter queue" for
exactly the idle-parent case that's the whole point of `background=true`
delegation (dispatch a subagent, keep doing other things, get pinged when
it has something to say).

**Also flagged, not yet resolved, lower severity than the above:**

- **Cap enforcement (decision 3, the 4KB/16KB caps) is a cross-thread
  check-then-append with no described locking.** The caps are deliberately
  enforced at the messaging-tool layer, outside `steer()` itself — but
  `steer()`'s own lock (`_pending_steer_lock`) only protects the append,
  not a preceding "is there room" check done by different code. A
  background child's thread and the parent's own thread (or a second
  background child) can race between check and append. This needs an
  atomic check-and-append inside the same lock, not two separate lock
  acquisitions.
- **The tool schema is bifurcated and not yet specified as such.** Decision
  9 gives a subagent's `send_agent_message` no recipient parameter
  (implicit: always the parent) while a parent's version needs one (which
  child, or a cross-process session). Whether this is the same tool name
  with a role-conditional schema, or two distinctly-named tools, is
  undecided — and it matters for the "Relationship to A2A" compatibility
  seam's claim that the tool schema stays stable if a real A2A HTTP
  transport is added later for remote participants.
- **Mislabeled scope in "Remaining items inherited from the predecessor
  doc":** that section frames "toolset opt-in surface name" as a
  Transport-B-only leftover, but decision 9 only resolved the *subagent*
  side of toolset gating. How a **top-level/parent session** gets
  `send_agent_message` in its own toolset at all — the gate that controls
  the parent side of Transport A — is undecided and was not flagged as
  such until this pass.

### Updated status

**Not yet ready for implementation.** Two items are on the critical path
specifically because they sit on the only pairing that's actually live in
v1 (background child ↔ parent):

1. Verify (direct code read, same rigor as decision 1) that decision 7's
   `unprocessed_messages` fix actually surfaces through
   `tools/async_delegation.py`'s background-collection path, not just the
   synchronous path it was originally described against.
2. Design and verify the idle/active branch for parent-directed delivery
   in Transport A (the `_agent_running`-equivalent check before choosing
   `steer()` vs. a `_pending_input`-equivalent injection) — this is
   currently unspecified, not just unverified.

The cap-locking gap, the bifurcated schema, and the parent-side toolset gate
are real but lower-severity — resolvable during implementation rather than
blocking the start of it.

## Verification pass 2 (read-only) — item 1 confirmed as a real, second gap; item 2 designed

### Item 1: CONFIRMED — decision 7's fix does NOT reach the background path today, and would need explicit wiring

Read `tools/async_delegation.py`'s actual completion-event builder
(`_push_completion_event`) directly rather than assuming decision 7's fix
"just works" once background delegation is in play. It doesn't, as things
stand:

`_push_completion_event` builds its event dict (`evt = {...}`) by pulling a
fixed, explicit set of fields off `result` one at a time — `summary`,
`error`, `api_calls`, `duration_seconds`, `exit_reason`, plus a
stall-metadata allowlist loop (`stalled_after_quiet_seconds`,
`stall_threshold_seconds`, `stall_phase`, `stall_grace_seconds`) explicitly
called out in the code as "additive" for the stall monitor. **`pending_steer`
/ `unprocessed_messages` is not in this list.** Any such field present on a
background child's `result` dict is silently absent from the event that
actually gets pushed onto `process_registry.completion_queue` and, from
there, delivered to the parent via `_format_async_delegation` /
`_drain_process_notifications`. This is the exact failure mode Round 2
predicted — the fix was designed against `_finalize_child_results` (the
synchronous path) and never connected to the background path that's the
only one where it can actually fire.

**Fix now specified, not just flagged:** add `unprocessed_messages` (or
whatever field name decision 7 settles on) to `_push_completion_event`'s
explicit field list, following the same pattern already used for the
stall-metadata allowlist (`if _k in result: evt[_k] = result[_k]`) so it's
additive and doesn't disturb the existing event shape for children that
never hit this race.

### Item 2: designed — idle/active branch for parent-directed delivery

**Decision:** Transport A's parent-directed delivery path must replicate
the exact branch `cli.py`'s own `/steer` command already uses (`canonical
== "steer"`, ~line 11265):

1. Check whether the parent's own turn is currently active — the
   `_agent_running`-equivalent flag on the CLI/session object owning the
   target `AIAgent`, not merely whether the parent still exists in whatever
   registry Transport A consults.
2. **If active:** call `agent.steer(message)` — delivered at the next
   tool-batch boundary per decision 1's verified mechanism.
3. **If idle:** route to the same idle-injection mechanism Round 3 of the
   predecessor doc validated for cross-process notifications
   (`self._pending_input.put(...)`-equivalent) — starting a fresh turn for
   the parent with the message as its content, the same way
   `_drain_process_notifications` already does for background-terminal-
   process completions today.

This is not new plumbing — it is the same two-mechanism pattern
(`steer()` for active, `_pending_input` injection for idle) the codebase
already uses in two other places (`cli.py`'s own `/steer` command, and
`_drain_process_notifications`'s async-delegation-completion delivery).
Transport A's parent-delivery path should be built as a third caller of
this same pattern, not a new one. **Residual open item:** the actual async
delegation completion event (item 1 above) already flows through exactly
this idle-delivery path today for the *summary* — so the natural
implementation is to have `unprocessed_messages` ride along on the same
event/delivery mechanism as the rest of a background child's completion,
rather than inventing a separate delivery channel for it. This should
collapse item 1 and item 2 into one implementation, not two: fix
`_push_completion_event` to include the field (item 1), and the existing
`_drain_process_notifications` → `_pending_input` idle-delivery path (which
already handles "parent is idle" for every other async-delegation
completion) delivers it for free. The only genuinely new logic needed is
the **active-parent** half of item 2 (routing to `steer()` when the parent
is mid-turn), since today's async-delegation completion delivery only
handles the idle case.

**Blocker caught by the final sign-off review, fixed here, not deferred:**
the two branches above have divergent untrusted-content framing today, and
faithfully reusing both mechanisms *as they currently exist* would silently
undo the predecessor doc's own threat-model requirement. The **active**
branch (`steer()`) already wraps its payload in an explicit out-of-band
marker (`STEER_MARKER_OPEN`/`CLOSE`, per Finding 1's verification) — the
model is told plainly that this content arrived out-of-band, not from its
operator. The **idle** branch (`_pending_input.put(...)`) is, as currently
used by `_drain_process_notifications` for e.g. background-terminal-process
completions, a **bare next-turn message** with no such marker — the exact
same channel ordinary human-typed input flows through. If Transport A's
idle-delivery path reused that mechanism literally as-is, a cross-session
or cross-participant message would land in the recipient's next turn
indistinguishable from an instruction typed by its own operator — precisely
the privilege-escalation path the predecessor doc's Round 2 threat-model
finding (delivered content must be framed as untrusted third-party data,
not bare user-role authority) exists to prevent, and precisely the kind of
model-mediated-relay case Finding 5/Decision 4 above already flags as a
place where inbound content might be one hop removed from an untrusted
gateway sender.

**Fix (mandatory, not optional, for both delivery branches):** every
message delivered by Transport A — whether via the active-parent `steer()`
branch or the idle-parent `_pending_input` branch — must be wrapped in the
same marker/envelope convention already used for `steer()`, stamped with
the sending participant's identity and origin type (session vs. subagent;
if a session, its origin — CLI/gateway/ACP, per the sender-permission-mode
decision elsewhere in this doc). The idle branch must **never** emit the
raw message body as bare text into `_pending_input` — it must construct the
same marked/framed form the active branch already produces via
`format_steer_marker()`-equivalent wrapping before injecting. This is a
small, concrete addition (reuse the existing marker-formatting function
for both branches, do not let the idle branch bypass it) — but it is a
correctness requirement for the whole feature's threat model, not a
nice-to-have, and it must be implemented from the first version of Transport
A's parent-delivery path, not added later.

### Updated status — ready for implementation, one design item left to write (not verify)

Both critical-path items from the previous review pass are now resolved:

1. Item 1 (background-path wiring gap) — confirmed real, fix specified (add
   the field to `_push_completion_event`'s explicit list).
2. Item 2 (idle/active branch) — designed by direct analogy to two already-
   shipped instances of the same pattern in this codebase, and recognized
   as collapsing into item 1's fix for the idle half; only the active-parent
   half (routing to `steer()` mid-turn) is genuinely new code, not a
   verification gap. **A final sign-off pass caught and fixed one more real
   gap here:** both the active and idle branches must use the same marked/
   framed envelope, not just the active branch — see the "Blocker caught by
   the final sign-off review" note above. This is now a stated implementation
   requirement, not left implicit.

**Remaining before code is written**, all lower-severity and resolvable
during implementation rather than blocking its start:

- The cap-locking gap (decision 3's 4KB/16KB caps need an atomic
  check-and-append, not two separate lock acquisitions).
- The bifurcated tool schema (parent's `send_agent_message` needs a
  recipient param; a subagent's doesn't) — needs a naming/schema decision
  (one role-conditional tool vs. two named tools) before implementation,
  not during.
- The parent-side toolset gate (how a top-level session opts into
  `send_agent_message` at all) — inherited from the predecessor doc's
  still-open "toolset opt-in surface name" question, now explicitly
  acknowledged as covering both transports' parent/session side, not just
  Transport B.
- Everything already listed under "Remaining items inherited from the
  predecessor doc" (heartbeat/TTL numbers, `hermes agents inbox`
  subcommand home, hold-mode UX, sender-permission-mode threat model).

No further Fable review rounds are scheduled by this doc — the next step
is a final user sign-off on the accumulated decision set (all of Round 1's
8 findings, both original open questions' resolutions, and Round 2's
gap-closure), followed by resolving the predecessor doc's remaining
Transport B items and the schema/naming/toolset-gate items above, before
any code is written.

## Closing out the remaining lower-severity items (final pass, all resolved)

### Cap-locking (decision 3's 4KB/16KB caps)

**Decision:** the size-check and the append must happen atomically under
`agent._pending_steer_lock` — not as two separate lock acquisitions
(check size, release, re-acquire to append), which is the exact
check-then-act race the earlier pass correctly flagged. Concretely: the
messaging-tool layer's send path acquires `_pending_steer_lock` once,
reads the current `_pending_steer` length, decides accept/reject against
the 16KB coalesced cap, and — only on accept — appends and releases, all
inside one critical section. This mirrors the pattern `steer()` itself
already uses for its own read-modify-write
(`self._pending_steer = self._pending_steer + "\n" + cleaned`, under
`_pending_steer_lock`) — the fix is doing the cap check inside that same
critical section rather than layering a second, separately-locked check on
top of it.

### Tool schema bifurcation: two distinctly-named tools, not one role-conditional schema

**Decision (Fable consult): `send_agent_message(recipient, body)` for
parent/session callers, `send_to_parent(body)` for subagent callers** — no
recipient parameter on the subagent-side tool at all (it has exactly one
valid target, per Finding 7). Rationale:

- Same-tool-name-different-schema is a hygiene trap independent of this
  feature: tool name → schema stops being a stable mapping, which breaks
  anything that caches or replays tool definitions (transcript replay,
  prompt caching, docs), and a subagent that has seen parent-side
  transcripts elsewhere in its context could hallucinate a `recipient`
  param onto the recipient-less variant it was actually given.
- **This is the better choice for the "Relationship to A2A" compatibility
  promise, not a worse one.** The A2A spec constrains the message
  envelope/transport, not model-facing tool names — there's no A2A
  argument for sharing a name. Both tools compile internally to the same
  message envelope with an explicitly resolved recipient, so the router
  has exactly one code path regardless of which tool name the model used.
- **Cleaner forward-compat if Finding 7's sibling-messaging punt is ever
  revisited:** a v2 that opens sibling messaging can give subagents the
  *same* `send_agent_message` schema additively, and `send_to_parent`
  survives unambiguously as sugar for the common case. The shared-name
  approach would instead force a schema migration on a tool name already
  in use — the exact instability distinct names avoid.

### Sender-permission-mode threat model: gate at tool registration by session origin type, not a fictional "permission_mode" field

The predecessor doc's Round 2 finding (a gateway session driven by an
untrusted external chat user could act as *sender*, injecting an
instruction into a more-permissive CLI session — recipient-side inbound
policy does nothing to stop this) is real, but **there is no
`permission_mode` field anywhere in this codebase to gate on** (verified —
grepped for it directly; the concept the predecessor doc referenced does
not exist as a real settings field on sessions today). The two closest
real analogs, checked and explicitly rejected as the wrong lever:

- `busy_input_mode` (`interrupt`/`queue`/`steer`) — a per-session UX
  preference for how the CLI handles new input while busy, not a security
  posture.
- `delegation.subagent_auto_approve` — governs whether a *subagent's own*
  dangerous-command approvals auto-approve, i.e. "what may this
  participant do unsupervised." **Explicitly do not overload this for
  sender-trust gating** — it answers a different question (what a
  subagent may do to its own environment) from "how trusted is this
  participant's inbound content to someone else," and conflating them
  would bite the first time someone wants an auto-approving subagent
  running inside an untrusted gateway session — two orthogonal settings
  collapsed into one would then fight each other.

**Decision (Fable consult): gate at tool registration by session origin
type — gateway-origin sessions do not get the cross-session
`send_agent_message` tool registered at all in v1.** They retain
`send_to_parent`/in-process child messaging within their own session tree,
but cannot reach another top-level session via Transport B. This is a
one-line conditional at tool-registration time keyed on session origin
(gateway vs. CLI vs. ACP), which is already a real, existing distinction
in this codebase (how a session was created) — not a new concept invented
for this feature. It kills the Telegram-driven-injection path outright at
the source, rather than trying to mediate it after the fact with a policy
field that doesn't exist.

**Defense in depth, and the hook for future refinement:** the message
router (not the sender) should stamp every envelope with the sending
session's origin type and ID, regardless of the registration gate above.
This is forgery-proof (the sender never self-reports its own origin) and
gives a future, real per-session policy field something to key on without
a schema change, if this coarse gate (which blocks a gateway session from
messaging *any* other session, not just more-privileged ones) ever needs
refining.

**Explicitly documented as a residual risk, not solved by this gate:** the
real remaining exposure isn't which sessions can send — it's that *any*
inbound cross-session message is untrusted content from the recipient's
point of view. The predecessor doc's Round 2 finding that delivered content
must be framed as untrusted third-party data (not bare user-role
authority) is the actual mitigation for that; this gate narrows *who* can
attempt the attack, it does not make delivered content trustworthy by
construction. Both are needed; neither substitutes for the other.

### Predecessor doc's remaining Transport B items — concrete answers found via code precedent

- **Heartbeat interval / registry TTL numbers:** this codebase already has
  exactly this cadence question solved twice, with numbers to borrow
  rather than invent. `agent/session_activity.py`'s
  `SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0` is the existing
  durable-heartbeat floor for session activity writes generally (explicitly
  documented as a hard floor: "MUST stay >= 30s" against write contention).
  `hermes_cli/kanban_db.py`'s `_STALE_HEARTBEAT_GAP_SECONDS = 3600` is the
  existing "how long without a heartbeat before we call something dead"
  reap threshold for kanban workers, a directly analogous liveness problem.
  **Decision: reuse both numbers rather than inventing new ones** —
  `cross_session_registry` heartbeats at the same 60s cadence
  `session_activity.py` already uses for session activity generally, and
  reaps at the same 3600s (1 hour) gap `kanban_db.py` already uses for
  worker liveness. This is a smaller ask than the predecessor doc's vague
  "2x the interactive idle-timeout" (which was never tied to a real
  constant) and reuses cadences this codebase has already tuned for
  write-contention and staleness detection.
- **Toolset opt-in surface name:** name it `cross_session` per the
  predecessor doc's own original proposal — no existing catalog entry fits
  better, and the name should now cover both `send_agent_message`
  (parent/session-side, Transport A+B) and `send_to_parent` (subagent-side,
  Transport A only, mode-gated per the earlier decision) under one
  opt-in toolset, consistent with the origin-type registration gate
  decided above (a gateway session's toolset check gains the origin-type
  condition on top of the existing opt-in check).
- **`hermes agents inbox` subcommand home:** `hermes_cli/subcommands/approvals.py`
  is a direct structural precedent — same shape of problem (list pending
  items needing a human decision, resolve them via CLI flags), same
  "parser here, handler injected by `main.py`" convention already
  documented in that file's own module docstring. **Decision: give this
  its own file, `hermes_cli/subcommands/agents.py`**, rather than bolting
  onto `approvals.py` — the domain (cross-session messages) is distinct
  from that file's actual domain (dangerous-command approval mining), and
  a shared file would conflate two unrelated inbox concepts under one
  misleading name. Follow `approvals.py`'s exact pattern: `build_agents_parser(subparsers, *, cmd_agents)`,
  an `inbox` sub-subcommand with `--approve ID`/`--deny ID` flags mirroring
  `approvals.py`'s `--apply`/`--json` shape.
- **Hold-mode UX (long expiry vs. notification):** this codebase already
  has a shipped, config-gated attention mechanism built for exactly this
  problem — `cli.py`'s `_fire_attention_signals()`, built for approval/
  clarify prompts that were getting silently missed across multiple
  windows/SSH sessions. It fires a terminal bell (`\a`, propagates through
  SSH/tmux/most terminal emulators) and, on macOS, a native `osascript`
  notification banner with sound — both independently gated on
  `approvals.bell_on_prompt`/`approvals.notify_on_prompt` in `config.yaml`,
  fail-soft (never blocks the prompt if notification delivery itself
  fails). **Decision: reuse `_fire_attention_signals()` directly for a
  `held` cross-session message** rather than inventing a parallel
  notification path or resolving the predecessor doc's "long expiry"
  option — a `held` message firing the same bell+banner used for
  approval/clarify prompts is more likely to actually be seen than a
  longer timeout window, and it costs zero new code (one more call site,
  reusing existing config gates rather than adding new ones). Combine with
  a longer default `expires_at` anyway (borrowing the 3600s/1-hour reap
  threshold decided above for consistency, rather than the predecessor
  doc's original 5-minute default, which was explicitly flagged as not
  credible without a synchronous dialog) — belt-and-suspenders, not
  either/or.

## Post-implementation revision — 2026-08-11 (Finding 7's visibility half reopened; SEND half unchanged)

The user's actual operating pattern surfaced a real gap after this design
shipped: they run **multiple concurrent top-level Hermes sessions on one
machine**, each dispatching its own `delegate_task` subagents, and those
subagents frequently step on each other's file edits with no way to notice
until the damage is done. Finding 7 (above) had made subagents invisible
cross-process entirely and refused `list_agents` to any subagent caller —
correct for the SEND-capability risk it was written to avoid (an
unobservable side channel between subagents; a confused/compromised
subagent steering its siblings mid-turn with no parent visibility), but it
also meant NO visibility existed at all, even read-only, even across
independently-started sessions.

**Decision, confirmed via `mcp__consult` before implementation:** the risk
Finding 7 protects against is a property of SEND capability, not of
visibility. Widening visibility does not reopen it. Subagent-to-subagent
messaging stays exactly as scoped in Finding 7 — still out of v1, still
requiring the relay-through-parent pattern described there. What changed:

1. **New durable, machine-wide subagent registry** (`cross_session_subagents`
   table: subagent_id, owner_session_id, goal, cwd, status, started_at).
   Deliberately not heartbeat-based like the session registry — a subagent's
   lifetime (seconds to minutes) is far shorter than a heartbeat/reap cadence
   tuned for sessions living minutes to hours. Written synchronously at
   spawn, deleted synchronously at completion; liveness derives from the
   owning session (cascade-reaped when the owner's own row goes stale on a
   crash), not from a heartbeat of the subagent row itself.
2. **`list_agents` now also returns live subagents**, read-only (owner,
   goal, cwd, status), to ANY caller including subagents — not just top-level
   sessions. A listed `subagent_id` is explicitly NOT a valid
   `send_agent_message` recipient unless it's the caller's own child;
   Transport B's cross-process resolution still only ever resolves sessions.
3. **A second external review pass (Fable, again via `mcp__consult`) caught
   that step 2 alone doesn't satisfy the actual goal.** Passive/on-demand
   visibility (a tool a subagent has to remember to call) is not "aware" for
   a stomping-*prevention* goal — a subagent given a terse, task-focused
   prompt will not reliably think to check before editing files. Two
   further gaps were closed as a result:
   - `list_agents` had been gated on `background=true`, copy-pasting the
     SEND tools' rationale ("parent's thread is blocked, can't react") onto
     a read-only lookup that doesn't share that justification — starving
     every *synchronous* subagent (the common file-editing case) of
     visibility entirely. Split into its own toolset (`agent_visibility`,
     `tools/agent_messaging_contract.py`/`toolsets.py`), granted
     unconditionally to every spawned child regardless of `background`.
   - Added a **proactive, dispatch-time cwd-collision check**
     (`tools/cross_session_transport.find_cwd_collisions`, prefix-overlap
     not exact-match — same repo, different subdirectories, is the actual
     common collision shape) run at spawn time. On a hit: the new
     subagent's own system prompt gets an explicit WARNING block (not a
     block/refusal — the editing tools themselves are the real safety net
     on a genuine conflict; a hard block would false-positive on the
     common "two subagents in the same repo, unrelated files, no
     coordination needed" pattern Finding 7 itself called out), and the
     parent's `delegate_task` dispatch response also surfaces it via
     `cwd_collision_warnings`, so the DISPATCHING session's own turn sees
     the heads-up immediately rather than only via the child's eventual
     summary.

Net effect: every agent and subagent on the machine can now see what every
other one is doing (goal + cwd + status), a subagent about to start
file-editing work in a directory another live subagent already occupies is
proactively told so before it starts (not just able to check), and none of
this required reopening subagent-to-subagent SEND capability.

## Final status: all identified items resolved. Ready for user sign-off.

Every item flagged across both Fable review rounds, both verification
passes, and the predecessor doc's inherited open list now has a concrete,
codebase-anchored decision:

- Architecture (2 transports, participant model, `resolve_transport` seam)
  — Round 1 confirmed sound, unchanged since.
- All 8 Round 1 findings — resolved.
- All 5 original open questions — resolved.
- Both Round 2 gaps (background-path wiring, idle/active delivery branch)
  — confirmed via direct code read and fixed.
- Cap-locking, tool-schema naming, sender-permission gating — resolved
  this pass, each via a concrete existing-code precedent or a direct
  Fable consult, not invention.
- Predecessor doc's remaining Transport B items (heartbeat/TTL, toolset
  name, `hermes agents inbox` home, hold-mode UX) — resolved this pass, all
  four via direct reuse of an already-shipped codebase mechanism
  (`session_activity.py`'s heartbeat floor, `kanban_db.py`'s stale-gap
  threshold, `approvals.py`'s subcommand pattern, `cli.py`'s
  `_fire_attention_signals()`) rather than new design.

No code has been written. Next step is user sign-off on the complete
decision set, not further design work.
