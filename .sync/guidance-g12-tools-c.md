# Group g12-tools-c — FORK.md guidance

Files: tools/skills_hub.py, tools/skills_tool.py, tools/terminal_tool.py,
tools/todo_tool.py, tools/tool_result_storage.py, tools/tool_search.py,
tools/vision_tools.py, tools/web_tools.py, tools/yuanbao_tools.py

## tools/terminal_tool.py — known silent-defect history + coordinated change
A PRIOR sync's union-merge left the fork's and upstream's `owner_task_id` parameter
DUPLICATED back to back in the function signature/call. If you see this, keep ONE
`owner_task_id` param (the fork's commented form if there's a difference in
docstring/comment quality) — never leave a duplicate kwarg, Python will raise
`SyntaxError: duplicate argument`. This ties to the same `owner_task_id` coordinated
change as `tools/process_registry.py` (group g11) and `tools/delegate_tool.py` (group
g10) — if either of those land in a DIFFERENT group in this sync, flag this file's
resolution back to the orchestrator so it can cross-check consistency across all three
once every group reports in.

## tools/web_tools.py — CRITICAL, do not inline upstream's rescue logic
Do NOT inline upstream's provider-selection/rescue logic directly into this file's top
level. Instead, place any new upstream provider-selection logic INSIDE the fork's
`_run_search_single()` seam (a function specifically designed as the fork's extension
point for this), and keep upstream's readiness predicate as a fall-THROUGH (i.e. it
should be consulted, but not replace the fork's failover structure) — otherwise the
fork's `web.search_chain` failover and Anthropic-native swap STOP FIRING silently. If
`_run_search_single()` doesn't exist in the merged file, that's a sign the seam was lost
in the merge — restore it from `git show ab0d3abd11:tools/web_tools.py` (read-only,
does not touch MERGE_HEAD) rather than accepting upstream's flat structure.

## tools/vision_tools.py
New dependency landed upstream this sync: `pillow-heif` for HEIF/HEIC/AVIF decode (iPhone
photos/screenshots mislabeled .jpg). If this file has a `_normalize_to_supported_image`
function or similar image-format resolver, and upstream's diff adds HEIF/HEIC handling,
take upstream's addition — it's a genuine new capability, not a conflict to fight. Make
sure any fork-specific image routing (see `agent/image_routing.py` guidance in group g03)
still calls into this correctly afterward.

## No specific prior guidance found for
tools/skills_hub.py, tools/skills_tool.py, tools/todo_tool.py,
tools/tool_result_storage.py, tools/tool_search.py, tools/yuanbao_tools.py — use the
common-guidance default. Flag design-collision picks explicitly; these will need new
FORK.md entries.
