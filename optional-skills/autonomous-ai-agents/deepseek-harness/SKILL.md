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

Installed and confirmed locally on 2026-08-15: `dsh` v0.1.0-rc.6, installed
via `npm install -g @deepseek-ai/dsh` (531 packages, ~19s). Binary resolves
to `~/.local/bin/dsh`. **Not yet run against the exo cluster** — installation
and CLI shape are verified; live inference through exo is not.

## Prerequisites

- Node.js 22.19+ or 24+ (confirmed working on Node v22.23.1). No pnpm needed
  for the npm-published package — `npm install -g @deepseek-ai/dsh` pulls a
  prebuilt CLI, pnpm is only needed if building from source.
- No API key needed for the exo path — you're pointing dsh at a local
  OpenAI-compatible endpoint, not DeepSeek's hosted API.

## Real CLI shape (confirmed from `dsh --help` on the installed binary)

dsh is a **profile-boot launcher**, not a single fixed-mode CLI. It boots an
"ordered stack of plugin-bundle patch layers" (a Cordis profile) and forwards
remaining args to that profile's own app:

```
Usage: dsh [options] [command] [args...]

Options:
  -V, --version               output the version number
  --profile <name>            the profile under $DSH_HOME/profiles to boot
  --patch <path>               extra patch-list overlay applied after the profile layer
  --dump-config                print the composed profile tree and exit
  --dump-default-config        print the profile tree without user/--patch overlays

Commands:
  web [options] [args...]      boot the web profile (alias of --profile web)
  plugin [options] [args...]   manage a profile's plugins via pnpm
```

Three built-in profiles ship out of the box: `web` (the browser UI, default),
`headless`, and `tui`. **Headless is real and confirmed** — this is the
one relevant to Hermes delegation (analogous to `claude -p`, `grok -p`,
`opencode run`):

```
terminal(command='dsh --profile headless "your task text here"', workdir="/path/to/project")
```

`dsh --profile headless --help` confirms: "Answer one task, print the final
assistant message, and exit." No `--output-format json` flag was found in
the headless profile's own `--help` as of rc.6 — treat plain-text stdout as
the only confirmed output format; re-check `--help` before assuming JSON
output exists in a later version.

`$DSH_HOME` defaults to `~/.dsh` (confirmed: `~/.dsh/profiles/{web,headless,tui}`
each have their own `node_modules`, `package.json`, `cordis.yml`,
`cordis.patch.yml`). Default model per `dsh --profile headless
--dump-default-config`: provider `deepseek-official`, model
`deepseek-v4-flash` — i.e. out of the box it targets DeepSeek's own hosted
API, not a local endpoint. That has to be overridden per the section below.

## Pointing dsh at the exo cluster

The LLM seam is `@deepseek-ai/dsh-llm-pi-ai` (backed by the
`@earendil-works/pi-ai` multi-provider adapter library) — confirmed present
in the installed package tree. It takes a `providers` dict keyed by route
name in the profile's settings, e.g.:

```yaml
- id: llm
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      exo:
        displayName: exo cluster
        api: openai-completions
        baseURL: http://<exo-endpoint>/v1
        # apiKeyEnv omitted entirely for an unauthenticated local endpoint
        models:
          - id: <exact-model-name-exo-reports>
            name: DeepSeek-V4-Flash (exo)
            contextWindow: <value>
            maxTokens: <value>
```

This is a **hand-declared route** (pi-ai's built-in catalog doesn't know
about your exo cluster), so `baseURL`, `api: openai-completions`, and the
full `models` list must be spelled out — omitting `models` on a hand-declared
route leaves it with nothing to serve. Apply via `--patch <path-to-yaml>` or
by editing the active profile's settings file — confirm the exact settings
file location/precedence live (`dsh --dump-config` after a `--patch` should
show whether it composed correctly) before trusting it blind.

**Pitfall carried over from Hermes' own custom-provider handling:** if dsh's
pi-ai adapter does any fuzzy/auto-correct matching between a configured model
id and its catalog (the way Hermes' `/model` switch does — see
`skill:exo-cluster-operations` `references/connecting-openai-clients-model-naming.md`),
a near-miss `id` can silently get rewritten and 404 against the real exo
instance name. The pi-ai README states an unknown model id fails fast with
`LlmError('UNKNOWN_MODEL')` rather than silently substituting — better
behavior than Hermes' own fuzzy-match bug, but confirm this holds in practice
before trusting it.

## Suggested evaluation procedure (do this before adopting)

1. Already done: `dsh` installs cleanly via npm, `--profile headless --help`
   confirms the one-shot task mode.
2. Add exo as a hand-declared `providers.exo` route per the YAML above,
   pointing at the cluster's OpenAI-compatible endpoint, using the exact
   DSv4-Flash instance name exo serves. Confirm with `dsh --dump-config`.
3. Run ONE real, bounded coding task through
   `dsh --profile headless "..."` against exo — same task given to the
   current `claude`-fork delegation path — and compare:
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
   each time before scripting against a new dsh version (this skill was
   written against v0.1.0-rc.6; a later rc may change the profile/flag shape).
2. **Default model targets DeepSeek's hosted API, not exo.** Out of the box
   `--profile headless` resolves to `deepseek-official`/`deepseek-v4-flash`.
   You must configure the `providers.exo` route (see above) or it silently
   calls DeepSeek's real API instead of the local cluster.
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

Confirmed 2026-08-15 (before any exo cluster testing, per user request):
- `npm install -g @deepseek-ai/dsh` succeeds cleanly (531 packages).
- `dsh --version` → `0.1.0-rc.6`.
- `dsh --help` and `dsh --profile headless --help` confirm the profile-boot
  launcher shape and the one-shot headless task mode described above.
- `dsh --profile headless --dump-default-config` confirms the LLM seam
  (`@deepseek-ai/dsh-llm-pi-ai`), default provider/model
  (`deepseek-official`/`deepseek-v4-flash`), and that `$DSH_HOME/profiles/`
  exists with real per-profile config files.

Still unverified — confirm before any real delegation:
- A `providers.exo` route configured against the cluster's OpenAI-compatible
  endpoint returns real completions (not a 404/connection error) — test with
  a trivial prompt first.
- A bounded real coding task produces correct, verifiable output — file
  changes actually present on disk, tests actually passing, not just a
  transcript that claims success.
