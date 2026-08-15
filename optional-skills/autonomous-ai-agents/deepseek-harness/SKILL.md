---
name: deepseek-harness
description: "Delegate coding to DeepSeek Harness (dsh) pointed at a local/OpenAI-compatible endpoint (e.g. exo)."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Coding-Agent, DeepSeek, DSH, exo, Local-Inference, Autonomous]
    related_skills: [claude-code, opencode, codex, grok, hermes-agent]
---

# DeepSeek Harness (dsh) — Hermes Orchestration Guide

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) is
DeepSeek's own open-source agent harness: a plugin-based runtime (built on the
Cordis framework) that provides the model adapter, tool registry, session log,
and agent loop as swappable "capability seams." It ships a native
`llm-deepseek` provider, so DeepSeek-model tool-calling/reasoning formatting is
handled by DeepSeek's own code instead of being reverse-engineered by a
third-party CLI wrapper.

**Why this matters for this setup:** the exo cluster exposes an
OpenAI-compatible endpoint serving DeepSeek-V4-Flash. The `claude` CLI fork
used for exo-path delegation has to carry fork-only patches (DSv4-specific
reasoning_channel budget detection) to make an Anthropic-shaped wire protocol
behave against a DeepSeek backend. dsh is a candidate to remove that need
entirely for coding-delegation tasks, since it talks OpenAI-compatible
natively and understands DeepSeek's own reasoning/tool-call format out of the
box.

## Status: developer preview — verify before relying on it

DeepSeek Harness is currently in developer preview and is iterating rapidly,
with compatibility-breaking changes expected between releases. Treat every
command below as needing a fresh smoke test before use in a real task — do
not assume flags/behavior are stable release-to-release. DeepSeek is not
accepting external PRs to the core repo; the intended extension path is
plugins, not core contributions.

**This has NOT yet been smoke-tested against the exo cluster in this repo.**
Before delegating any real task, run the Verification section below first.

## Prerequisites

- Node.js 22.19+ or 24+, and pnpm (Ollama also bundles/auto-installs
  `@deepseek-ai/dsh` on demand if you're going through an Ollama integration
  path instead of DeepSeek's own package).
- Install: `npm install -g @deepseek-ai/dsh` (or `npx @deepseek-ai/dsh web`
  for a one-off run without a global install).
- No API key needed for the exo path — you're pointing dsh at a local
  OpenAI-compatible endpoint, not DeepSeek's hosted API.

## Primary interface is a Web UI, not a TUI

Unlike Claude Code / Grok / OpenCode / Codex, dsh's default UX is a **local
web app**, not a terminal TUI:

```
terminal(command="npx @deepseek-ai/dsh web", background=true, notify_on_complete=true)
# Serves at http://127.0.0.1:3080 by default
```

There is community reporting of a **headless run mode** for CI/scripted use
(no interactive TUI, launcher flags parsed before the app's own args), but the
exact flag syntax is NOT confirmed against DeepSeek's own docs as of this
skill's authoring — run `dsh --help` / `dsh web --help` live and confirm
before trusting any specific flag. Do not hardcode unverified flags into
automation.

```
terminal(command="dsh --help")
terminal(command="dsh web --help")
```

## Pointing dsh at the exo cluster

dsh's provider system supports custom OpenAI-compatible endpoints via a
provider seam (analogous to OpenCode's `provider.<id>.options.baseURL`
pattern). In the Web UI: Settings → Models → Add a custom provider, and
supply:

- Provider ID: `exo` (or similar)
- Base URL: the exo cluster's OpenAI-compatible endpoint
- API protocol: `openai-completions`
- Key: none needed for a local/LAN endpoint (check what the UI requires; a
  placeholder value may be needed even if unchecked)
- Model list: the served model name exactly as exo reports it (see the
  Hermes custom-provider model-naming gotcha below — same class of bug can
  hit dsh if its catalog name doesn't exact-match the instance name)

**Pitfall carried over from Hermes' own custom-provider handling:** if dsh
does any fuzzy/auto-correct matching between a configured model name and its
own catalog (the way Hermes' `/model` switch does — see
`skill:exo-cluster-operations` `references/connecting-openai-clients-model-naming.md`),
a near-miss name can silently get rewritten and 404 against the real exo
instance name. Confirm the exact served model string against exo before
trusting dsh's model dropdown.

## Suggested evaluation procedure (do this before adopting)

1. Confirm dsh installs cleanly: `npx @deepseek-ai/dsh web --version` (or
   equivalent — verify the real flag first).
2. Add exo as a custom provider pointing at the cluster's OpenAI-compatible
   endpoint, using the exact DSv4-Flash instance name exo serves.
3. Run ONE real, bounded coding task through dsh against exo — same task
   given to the current `claude`-fork delegation path — and compare:
   - Tool-call reliability (did it actually invoke file/shell tools cleanly,
     or garble the reasoning/tool-call wire format the way the fork exists to
     patch around?)
   - Output quality (validate end-to-end, not just "it ran" — per this
     project's perf-debugging rule: never declare a result without showing
     actual generated output).
4. Only after a real side-by-side result, decide whether dsh is worth keeping
   as a second exo-path delegation option or whether the `claude`-fork path
   stays primary. Do NOT retire the fork path speculatively — this is an
   additive option until dsh proves itself, not a replacement.

## Where this lives in Hermes

This is an **optional skill** under `optional-skills/autonomous-ai-agents/`,
auto-discovered by `tools/skills_hub.py`'s `OptionalSkillSource` scanner
alongside `claude-code`, `codex`, `grok`, and `opencode`. No core code
changes were needed to add it — same pattern as the other sibling coding-agent
skills. It does NOT change exo (still just an inference server exposing an
OpenAI-compatible endpoint) and does NOT modify the `claude`-fork delegation
path; it's a parallel, independently-invoked option.

## Pitfalls & Gotchas

1. **Developer preview — expect breaking changes.** Re-verify flags/behavior
   each time before scripting against a new dsh version.
2. **Web UI first, headless unconfirmed.** Don't assume a `-p`/`exec`-style
   one-shot flag exists with the same shape as Claude Code / Grok / Codex
   until you've confirmed it against `dsh --help` on the installed version.
3. **No external PRs accepted upstream.** If dsh is missing something for
   this use case, the fix is a plugin or a wrapper on the Hermes side, not a
   PR to `deepseek-ai/deepseek-harness`.
4. **Model-name exact-match risk.** Same class of bug as Hermes' own
   `/model`-switch fuzzy auto-correct (warm fact: hermes custom-provider
   model-name gotcha) — verify the exact served model string, don't trust a
   close-but-not-exact catalog entry.
5. **Don't retire the `claude`-fork exo path preemptively.** This skill adds
   an alternative; it does not replace anything until a real side-by-side
   task comparison says otherwise.

## Verification

Before ANY real delegation, confirm:
- `dsh` (or `npx @deepseek-ai/dsh`) actually launches and reaches a UI/CLI
  prompt without error.
- The custom provider pointed at exo returns real completions (not a 404/
  connection error) — test with a trivial prompt first.
- A bounded real coding task produces correct, verifiable output — file
  changes actually present on disk, tests actually passing, not just a
  transcript that claims success.
