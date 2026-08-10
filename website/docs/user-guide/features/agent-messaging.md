---
sidebar_position: 8
title: "Local Agent Messaging"
description: "Message other Hermes sessions and your own subagents directly, without waiting for the next turn"
---

# Local Agent Messaging

Local agent messaging lets a running Hermes agent send a short message directly to another live participant — another top-level session, its own `delegate_task` subagent, or (for a `background=true` subagent) the parent that delegated to it — instead of waiting for the recipient's next turn to start naturally.

It's fork-only (not part of upstream Hermes) and completely opt-in: it costs zero tokens unless you enable the `cross_session` toolset, and it never touches your data unless you explicitly turn it on.

## Two transports, one tool surface

The feature has two delivery mechanisms under the hood, chosen automatically based on who you're messaging — you never pick a transport yourself, you just call `send_agent_message`:

- **In-process** — for messaging your own `delegate_task` subagents, or (if you're a `background=true` subagent) messaging back to your parent. Delivery reuses the same `steer()` mechanism that powers the human `/steer` command: the message either lands at the recipient's next tool-batch boundary (if it's actively running) or starts a fresh turn (if it's idle).
- **Cross-process** — for messaging a *different*, independently-started top-level Hermes session (two terminal tabs, a CLI session and a gateway session, two profiles). This is durable: messages are written to `~/.hermes/state.db` and delivered when the recipient next polls, which may be seconds or minutes later, or never if the recipient never comes back.

Both transports share the same three tools and the same untrusted-content framing — every message you receive is wrapped in a `[CROSS-AGENT MESSAGE]` marker so you can tell it apart from something your own operator typed.

## Enabling it

Add the `cross_session` toolset like any other:

```bash
hermes chat --toolsets hermes-cli,cross_session
```

```yaml
# config.yaml
toolsets:
  - hermes-cli
  - cross_session
```

Or via `hermes tools` (curses UI) / `/tools enable cross_session` in-session.

## The tools

| Tool | Who gets it | What it does |
|---|---|---|
| `send_agent_message(recipient, body)` | Top-level sessions only (parent/session callers) | Message a subagent you spawned, or another live top-level session, by id or name. |
| `send_to_parent(body)` | `background=true` subagents only | Message the parent that delegated to you. No recipient parameter — you have exactly one valid target. |
| `list_agents()` | Top-level sessions only | List other live top-level sessions you can reach with `send_agent_message`. Subagents are never listed here — reach one by messaging the session that owns it. |

A **synchronous** `delegate_task` child gets neither tool. Its parent's thread is blocked inside the delegation call and can't act on anything sent to it until the whole batch returns, so shipping the schema would be pure token cost with no live consumer.

`send_agent_message` is also never registered at all for gateway-origin sessions (Telegram, Discord, etc.) — a gateway session driven by an untrusted external chat user should not be able to inject a message into a more-permissive CLI session. Gateway sessions keep everything else (they can still be *messaged*, subject to their own inbound policy, and their own delegated subagents still get `send_to_parent`).

## Delivery is "queued", not "delivered"

A successful `send_agent_message` call means the message was **accepted for delivery** — not that the recipient has seen it, read it, or acted on it. The tool's response tells you which:

- `queued-ephemeral` (in-process) — arrives at the recipient's next tool-batch boundary. Lost if the recipient exits before draining it.
- `queued-durable` (cross-process) — written to `state.db`. Delivered whenever the recipient next polls.
- `held` (cross-process) — the recipient's inbound policy requires a human to approve it first (see below). It may never be delivered.
- an error — the recipient doesn't exist, has exited, or a rate/size/hop limit was hit. Never silent — you always get told, with the currently-valid targets included when a subagent id was mistyped.

A message can also bounce back to you as an `unprocessed_messages` note on a subagent's `delegate_task` completion, if it arrived after the subagent's last tool call but before it produced its final response — there's nothing left to attach it to mid-turn, so it gets surfaced in the handoff instead of silently dropped.

## Inbound policy (cross-process only)

Every top-level session decides for itself what happens to an incoming cross-process message, via the `cross_session.inbound` config key:

```yaml
# config.yaml
cross_session:
  inbound: ""   # "" (default), "accept", "hold", or "refuse"
```

Leaving it empty keeps the built-in per-origin defaults, which are deliberately conservative:

| Session origin | Default |
|---|---|
| CLI | `hold` |
| ACP (editor) | `hold` |
| Gateway | `refuse` |
| Cron | `refuse` |

- `accept` — deliver automatically at the recipient's next drain checkpoint.
- `hold` — queue for your approval via `hermes agents inbox` (below). Fires the same terminal-bell/notification you already get for missed approval prompts. Expires after an hour if you never act on it.
- `refuse` — never deliver. The sender is told immediately, not left guessing.

Policy is re-evaluated by the recipient at the moment it actually drains the message, not baked in at send time — so changing your own `cross_session.inbound` setting takes effect immediately, even for messages already in flight.

## Reviewing held messages

```bash
hermes agents inbox                       # list messages currently held for approval
hermes agents inbox --approve <id>        # return it to the pending queue for normal delivery
hermes agents inbox --deny <id>           # mark it denied — never delivered
hermes agents inbox --all                 # show every message regardless of status
hermes agents inbox --json                # machine-readable
```

Approving does **not** deliver the message directly — it returns the row to the normal pending queue so the recipient's own next drain claims it, re-evaluating its current inbound policy at that point.

## Safety limits

- **Per-message size cap**: 4KB. This is a coordination channel, not a data-transfer channel — write large content to a file and send the path.
- **Coalesced pending-queue cap** (in-process): 16KB total unread bytes per recipient. A send that would exceed it is rejected synchronously, never silently truncated.
- **Hop-count ceiling** (cross-process): messages are rejected past 4 hops in a reply chain, preventing two `accept`-mode sessions from ping-ponging forever.
- **Per-sender-pair rate cap** (cross-process): at most 5 messages per minute between the same two sessions.
- **One send per turn** (cross-process): a single turn may make at most one `send_agent_message` call.
- **Identical-body repeat suppression** (cross-process): the same message body sent twice to the same recipient within a short window is suppressed as a duplicate.

## What this is not

This is a fork-only feature, not part of the real [Agent2Agent (A2A) protocol](/user-guide/messaging/a2a) implementation, which remains the genuinely network-facing, spec-compliant way to talk to remote, independently-operated Hermes instances or other A2A-compliant agents. Local agent messaging only reaches participants already running on your own machine, in your own profile. The tool-facing surface is deliberately kept identical to what a future real A2A transport would need, so one could be added later as a drop-in without changing anything you call today.

Also out of scope for v1: sibling-to-sibling subagent messaging (two subagents in the same `delegate_task` batch cannot message each other directly — only their shared parent), cross-process addressing of a specific subagent by id (message the owning session instead), and cross-machine relay.
