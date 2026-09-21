# Group g11-tools-b — FORK.md guidance

Files: tools/lazy_deps.py, tools/mcp_oauth.py, tools/mcp_tool.py, tools/memory_tool.py,
tools/process_registry.py, tools/registry.py, tools/send_message_tool.py,
tools/skill_manager_tool.py

## tools/process_registry.py — coordinated `owner_task_id` change
This is a documented 3-way coordinated consumer reconciliation touching
`tools/terminal_tool.py` (group g10/g12) and `tools/delegate_tool.py` (group g10).
Taking upstream's implementation of `owner_task_id` handling requires rewiring the
fork-side consumers correctly. Verify by tracing: real terminal-tool spawns through
`process_registry.spawn_local` across at least 3 shapes — explicit raw subagent
`task_id`, omitted `task_id` falling back through the registry, and a collapsing
container key. `owner_task_id` must resolve correctly in all three (raw id preserved,
distinct from a collapsed `"default"` key). If you can't verify this dynamically, at
minimum read the function bodies of all 3 files for consistent `owner_task_id`
threading, and flag it clearly if `tools/terminal_tool.py` or `tools/delegate_tool.py`
are NOT in your group (they're in g10/g12) so the orchestrator cross-checks after all
groups land.

## tools/memory_tool.py
Upstream sometimes narrows the advertised store description by doing a `str.replace()`
on a VERBATIM sentence in the tool's docstring/description. **The fork has rewritten
that description in the past, so those `str.replace()` calls silently no-op** (the
sentence being replaced no longer exists verbatim in the fork's version). Never assume
upstream's exact prose survives across the fork boundary — if you see a
`.replace("some verbatim sentence", ...)` pattern reaching across from upstream's
diff, and the fork's current description doesn't contain that exact sentence, that
replace is dead code silently. Prefer appending a separate notice/paragraph instead of
relying on string-replace patching prose.

## tools/mcp_tool.py
Ties to the tool-naming divergence documented for `agent/anthropic_adapter.py` (group
g01): the fork does NOT prepend `mcp_` to bare tool names (registers as
`mcp__server__tool`). If this file has any prefix-stripping/adding logic for MCP tool
names, keep the fork's non-prefixed convention; drop upstream's prefix loop if it
reappears here too.

## No specific prior guidance found for
tools/lazy_deps.py, tools/mcp_oauth.py, tools/registry.py, tools/send_message_tool.py,
tools/skill_manager_tool.py — use the common-guidance default. Flag design-collision
picks explicitly; these will need new FORK.md entries.
