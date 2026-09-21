# Group g04-agent-core-d — FORK.md guidance

Files: agent/system_prompt.py, agent/thread_scoped_output.py, agent/title_generator.py,
agent/tool_executor.py, agent/tool_guardrails.py, agent/transports/anthropic.py,
agent/transports/chat_completions.py, agent/transports/codex_app_server_session.py,
agent/turn_context.py, agent/usage_pricing.py, agent/web_search_registry.py

## agent/title_generator.py — HIGH RISK, known silent-defect history
A PRIOR sync's auto-merge on this exact file (with NO conflict markers at the time)
produced an incoherent result: upstream's function body ended up wrapped in a fork-era
gate `if cfg.get("ui",{}).get("show_auxiliary_errors", True):` doing `from agent.config
import read_config` — but `agent.config` does not exist in EITHER fork or upstream. The
import raised, got swallowed by a bare `except`, and a failure callback silently never
fired. Read this file CAREFULLY end to end (not just the marker regions) for any dead
gate/wrapper that references a module or function that doesn't actually exist anywhere
in the current tree. Grep for every `from agent.` or `import agent.` in this file and
verify the target module exists (`ls agent/<name>.py`) before leaving it in place.

## agent/web_search_registry.py
Ties into the Tavily keyed/keyless decision (see FORK.md "Tavily kept keyed-only;
removed from the default-on keyless ring — 2026-09-01"). The fork's policy: Tavily is
KEYED, opt-in ONLY — it must NOT appear in `_KEYLESS_RING` / `_KEYLESS_SEARCHERS` /
`_KEYLESS_EXTRACTORS` / `_KEYLESS_PREFERENCE`. Upstream's current (v2026.9.14) ring is
`("exa", "parallel", "firecrawl", "keenable")` — no tavily. If upstream's version of
this file already matches that ring shape, take theirs; if the fork's side has additional
legacy `_LEGACY_PREFERENCE` tuple entries mentioning tavily in a preference-walk (not the
default-on ring), that's fine to keep — it's a different, lower-priority keyed-preference
list, not the default-on keyless ring. Do NOT add tavily to any KEYLESS-default-on
structure no matter what either side's diff suggests.

## agent/tool_guardrails.py
There's a fork-only test file `tests/agent/test_tool_guardrails.py` (also in your test
group's sibling list) — if you see any guardrail check the fork added (e.g. an
empty-tool-name loop dampening guard — see `tests/agent/test_empty_tool_name_loop_dampening.py`
elsewhere in this sync), keep it; verify it's not silently dropped by an upstream
restructure of the guardrail dispatch.

## No specific prior guidance found for
agent/system_prompt.py, agent/thread_scoped_output.py, agent/tool_executor.py,
agent/transports/anthropic.py, agent/transports/chat_completions.py,
agent/transports/codex_app_server_session.py, agent/turn_context.py,
agent/usage_pricing.py — use the common-guidance default. Flag design-collision picks
explicitly for new FORK.md entries.
