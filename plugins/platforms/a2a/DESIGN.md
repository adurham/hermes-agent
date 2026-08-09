# A2A Platform Plugin — Design

Consolidates the entire A2A (Agent-to-Agent) feature cluster (#514 and friends)
into one **plugin** with **zero core edits**, built on capabilities the current
codebase already exposes. Implements **A2A Protocol v1.0** (JSON-RPC binding).

## Status: implemented, merged upstream, has known open bugs

This is not a proposal — it shipped. `837003b1e` (closes #514) through
`81c7e5de4` are on `NousResearch/hermes-agent` main. `tests/plugins/
test_a2a_plugin.py` + `test_a2a_phase23.py` (2,300+ lines, 168 tests) pass
against current main. The plugin directory (`adapter.py`, `protocol.py`,
`security.py`, `tools.py`) is real, working code, not a sketch.

It is not bug-free. Open issues on the upstream tracker as of 2026-08-09,
roughly in priority order:

| Issue | Problem |
|---|---|
| #78050 (dup: #81163) | Outbound client tools never register in CLI/TUI sessions — the plugin is a *deferred* platform plugin, so `register()` only fires in gateway/web-server processes that call `platform_registry.plugin_entries()` at startup. CLI/TUI never does, so the toolset is invisible where most interactive users would reach for it. |
| #78007 | Client default timeout (120s) is shorter than the server's own reply window (`A2A_REPLY_TIMEOUT`, default 300s) — long tasks always die client-side first, and the error surfaces as a raw socket timeout, not a task state. Raw-URL peer calls also can't override the hardcoded 120s at all. Additionally, the server finalizes a task as failed at its own deadline even if the agent's future later resolves successfully — the real result is discarded. |
| #78396 | `hermes send --to a2a:...` fails with a misleading "no home channel" error even against a resolvable channel. |
| #80884 | Under `gateway.multiplex_profiles: true`, inbound A2A requests routed to a secondary profile are rejected by gateway authz — `authorization_is_upstream` isn't honored outside the primary profile. |
| #80534 | Behind a reverse proxy, a shared `A2A_BEARER_TOKEN` collapses every remote peer to the same `ip:<proxy address>` identity (the proxy's socket address), silently degrading per-peer rate limiting, trust gating, and audit attribution — the documented mitigation is per-peer tokens, but nothing warns operators who reach for the shared-token path behind a proxy. |
| #81003 | Rejected requests (401/403) never reach `a2a_audit.jsonl` — only accepted requests are logged, so credential-stuffing/token-probing against the inbound port is invisible to the audit trail that's supposed to be the intrusion-detection signal. |

### External review (2026-08-09)

Two of the six are architectural, not local patches:

- **#78050** — the plugin conflates lightweight client tools (needed in
  every process) with a heavy gateway-only platform adapter inside one
  deferred-load plugin, and `resolve_toolset` silently returns `[]` when a
  deferred plugin hasn't resolved instead of forcing resolution or erroring.
  Fix the loading boundary (split tool registration from platform
  adapter registration, or make toolset resolution force-resolve deferred
  platform plugins), not just this one plugin's symptom.
- **#78007** — do not fix this by raising `_DEFAULT_TIMEOUT`. The design
  bug is (a) synchronous blocking `message/send` racing three independent
  clocks (client timeout / `A2A_REPLY_TIMEOUT` / 5-min watchdog — the
  watchdog and reply timeout are dangerously close in value already), and
  (b) `_finalize_task` marking a task terminally FAILED at the server
  deadline even though the agent's Future can still resolve successfully
  afterward, discarding real completed work. The task store already
  exists for polling (`tasks/get`); the fix is to lean on it — return a
  `working` task promptly and let the late Future finalize normally —
  not to move the deadline.

#80534 (proxy identity collapse) and #80884 (multiplexer authz) need an
actual design decision (trusted-proxy header support, or refuse
shared-token mode off-localhost; audit which profile-override paths are
honored under multiplexing generally) — not a one-line guard. #78396 and
#81003 are genuinely local, though #81003's fix belongs at the HTTP-entry
layer (log before/regardless of auth outcome), not inside the
already-authenticated adapter path, or the next rejection path will be
missed the same way.

Treat this doc as the as-built reference; fixes land as targeted patches
referencing the issue numbers above, not a redesign.

## Why a plugin, not a core feature

Earlier A2A attempts (#4135, #4948, #4952, #11025) added a standalone server
package (`a2a_adapter/`) and/or patched `gateway/run.py` + `gateway/config.py`.
Since then the codebase grew `ctx.register_platform()` (the plugin
platform-adapter API — used by irc, line, teams, ntfy, simplex, …) and
`ctx.register_tool()`. That makes the standing policy achievable: **plugins
must not touch core files.** A2A now lives entirely under
`plugins/platforms/a2a/`.

## Two directions

### Outbound — client tools (`a2a` toolset)
- `a2a_discover(url)` — fetch + summarize a peer's Agent Card (v1.0
  `supportedInterfaces` aware, tolerates 0.3 cards).
- `a2a_call(agent, message, context_id?)` — send a JSON-RPC `message/send`
  task to a peer, return the reply. Multi-turn via `context_id` (carried
  inside the Message per v1.0). Surfaces `TASK_STATE_INPUT_REQUIRED` so the
  model knows to answer and continue the context.
- `a2a_list()` — configured peers + persisted conversations + metrics.
- `a2a_history(context_id, limit?)` — recall a persisted conversation
  (this is the production consumer of the persistence layer).
- `a2a_orchestrate(capability, message, mode?)` — fan-out one task to every
  configured peer advertising a capability. Modes: `all` (every reply),
  `first` (first success), `best` (longest successful reply — a deliberately
  coarse heuristic; errors never win, and an all-error fan-out reports the
  failures instead of picking one).

Peers resolved from `config.yaml` → `a2a_agents`, or a direct URL.

### Inbound — platform adapter
- Stdlib `http.server` on a daemon thread (no asyncio loop needed at
  `register()` time — sidesteps the a2a_fleet "register outside a loop" bug
  class that killed inbound serving in forks). The request handler is a
  module-level class (`A2ARequestHandler`) reached through
  `server.adapter`, so RPC handlers are unit-testable without HTTP.
- Agent Card at `GET /.well-known/agent-card.json` (canonical v1.0 path; legacy `agent.json` also answers) (v1.0: `supportedInterfaces[]`,
  `provider`, `capabilities.extendedAgentCard`). **Dynamic**: skills are
  built from the live tool registry at serve time
  (`A2A_ADVERTISED_TOOLSETS` / `extra.advertised_toolsets` restricts them).
- JSON-RPC methods: `message/send`, `message/stream` (SSE), `tasks/get`,
  `tasks/list`, `tasks/cancel`, `tasks/subscribe`,
  `tasks/pushNotificationConfig/create` (legacy `set` names accepted).
- **Live-session injection (the #11025 insight):** inbound tasks route through
  the normal `MessageEvent` → `handle_message` path keyed by the A2A
  `contextId`, so the agent that answers is the same one serving the user —
  full memory/context, not a clone. The reply returns through `adapter.send()`,
  which fulfils the pending per-**task** `Future` the HTTP request is blocked
  on (per-context FIFO, so concurrent same-context requests can't cross-talk);
  `on_processing_complete` resolves failures/cancellations promptly.
- **Task store:** every task (including terminal ones, bounded to the last
  500) stays queryable via `tasks/get` / `tasks/list`, and `tasks/subscribe`
  reattaches to a running task's stream via store watchers. A watchdog fails
  orphaned tasks after 5 minutes (idempotent transitions — no double
  counting in metrics).
- **input-required:** the platform hint tells the agent to start a reply with
  `[INPUT_REQUIRED]` when it needs clarification; the adapter maps that to
  `TASK_STATE_INPUT_REQUIRED` with the question in `status.message`.
- **Push notifications:** config accepted inline in `message/send`
  (`configuration.taskPushNotificationConfig`) or via the create method
  (returns `configId` + `createdAt`). On terminal transition the callback
  receives a v1.0 `StreamResponse` (`statusUpdate`) payload, HMAC-SHA256
  signed (`X-A2A-Signature`, secret `A2A_PUSH_SECRET` falling back to the
  bearer token), with SSRF-guarded callback URLs.

## v1.0 wire format notes
- Task states / roles are SCREAMING_SNAKE_CASE (TASK_STATE_*, ROLE_*).
- Parts are member-presence discriminated — no kind field. All three
  Part types are supported: text (text + mediaType), file
  (url|raw + filename + mediaType), and data (data + mediaType).
  extract_text renders file/data Parts into the text stream (URL +
  filename for files, JSON for data) so the agent sees them; it also
  accepts v0.3 (kind) and pre-0.3 (type) shapes from older peers.
  Outbound replies are still text-only — the agent produces text, and
  file/data Parts are for inbound richness.
- Push notification config: full CRUD — create (inline in message/send
  via configuration.taskPushNotificationConfig, or via the create
  method), get, list, delete. Each config has a configId and createdAt.
  One config per task (v1.0 allows multiple; we keep one).
- SSE events are StreamResponse objects (statusUpdate / artifactUpdate
  members); stream closure signals the terminal state — no final field.
- contextId lives inside the Message (legacy top-level accepted inbound).
- Timestamps are ISO 8601 with millisecond precision; Tasks carry
  createdAt / lastModified.
- Error codes: A2A-reserved codes are used only with their spec semantics
  (`-32001` TaskNotFound, `-32002` TaskNotCancelable); custom errors sit at
  `-32050..-32052` (unauthorized / rate-limited / untrusted).

## Security (on by default)
- **Bind safety:** no token configured (`A2A_BEARER_TOKEN` or
  `A2A_PEER_TOKENS`) ⇒ bind `127.0.0.1` only. A token alone does not widen
  the bind; remote exposure requires token **and** explicit `A2A_HOST`.
- **Peer identity:** `A2A_PEER_TOKENS="alice:tok1,bob:tok2"` gives each peer
  its own credential; the matched name is the authenticated identity used
  for rate limiting, the trust gate, message framing, and audit. A shared
  `A2A_BEARER_TOKEN` authenticates as `ip:<addr>`. Nothing in the request
  body can assert identity. Comparisons are constant-time.
- **Trust gate:** `A2A_TRUSTED_PEERS` (or config `a2a.trusted_peers`)
  optionally restricts which authenticated identities may run tasks.
- **Injection filters:** ALL inbound text (including `/`-prefixed — remote
  peers can never reach operator slash commands) is defanged (ChatML /
  role-prefix / override patterns → `[filtered]`) and framed with a privacy
  prefix marking it untrusted peer input.
- **Outbound redaction:** credential-shaped strings (`sk-…`, `ghp_…`, JWTs,
  bearer tokens, emails) scrubbed before anything leaves.
- **Rate limiting:** sliding window per authenticated identity
  (`A2A_RATE_LIMIT`/min).
- **Anti-loop:** per-context turn cap (`A2A_MAX_PINGPONG_TURNS`, default 5,
  hard max 20) rejects (v1.0 `TASK_STATE_REJECTED`) runaway agent↔agent
  ping-pong; `tasks/cancel` resets the counter for the task's context.
- **Audit log:** append-only `~/.hermes/a2a_audit.jsonl` for every exchange.

## State placement
Task store, turn tracker, and rate limiter are **adapter-instance** objects
(classes in `protocol.py`). The metrics counter bag stays a module singleton
because it is intentionally shared between the inbound adapter and the
outbound client tools (`/metrics` and `a2a_list` report both directions).

## Persistence (survives compaction)
A2A conversations are written to `~/.hermes/a2a_conversations/<context>.jsonl`,
outside the context-compaction pipeline — compaction and restarts can't lose
them (#11025 requirement). The `a2a_history` tool recalls them by context id.

## Requirements traced to the cluster

| Source | Requirement | Where |
|---|---|---|
| #514, #23871, #4135 | Agent Card discovery | `protocol.build_agent_card`, adapter GET |
| #4135, #14559, #8948 | Client: discover / call / list | `tools.py` |
| #11025 | Live-session injection (not a clone) | `adapter._prepare_task` |
| #11025 | Privacy filters + outbound redaction + audit | `security.py` |
| #11025 | Conversation persistence outside compaction | `protocol.persist_message`, `a2a_history` |
| #514, #11025 | Auth, localhost-default | `security.authenticate`, `resolve_bind_host` |
| #56434 | Trusted peer approval | `security.is_trusted_peer` |
| #56435 | Task completion notifications | push notifications (`_send_push_notification`) |
| #25176, #689 | Agent↔agent messaging across machines | client tools + inbound adapter |
| #7517 et al. | Multi-peer orchestration | `a2a_orchestrate` |

## Deliberately out of scope (future, not this pass)
- **a2a-sdk / gRPC + HTTP+JSON bindings.** Only the JSONRPC binding is
  served; the card advertises exactly that.
- **`tenant` field, extended Agent Card, `stateTransitionHistory`.**
- **True task abort:** `tasks/cancel` marks the task canceled and drops the
  reply, but cannot abort the live session's in-flight turn.
- **DID / Ed25519 identity, OAuth2 scopes, x402 micropayments** (#14559
  bindu) — heavy, niche; revisit if there's real demand.

## Files
```
plugins/platforms/a2a/
├── plugin.yaml      # manifest (kind: platform)
├── __init__.py      # register(): platform adapter + client tools
├── adapter.py       # inbound A2A v1.0 server (stdlib http.server)
├── tools.py         # outbound client tools
├── protocol.py      # Agent Card, JSON-RPC framing, task store, persistence
├── security.py      # auth/identity, injection filters, redaction, audit
├── DESIGN.md
└── README.md
```
