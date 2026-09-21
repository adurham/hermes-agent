# Group g10-tools-a — FORK.md guidance

Files: tools/async_delegation.py, tools/browser_tool.py, tools/checkpoint_manager.py,
tools/delegate_tool.py, tools/delegation_live_log.py, tools/discord_tool.py,
tools/feishu_drive_tool.py, tools/file_tools.py

## tools/file_tools.py — CRITICAL, known silent-defect history
A PRIOR sync's merge SILENTLY DROPPED upstream's definition of
`_read_file_schema_overrides` while KEEPING the reference to it in a
`registry.register(name="read_file", ..., dynamic_schema_overrides=_read_file_schema_overrides)`
call — no conflict marker, no syntax error, a NameError at import only. After resolving
this file:
1. Grep every `registry.register(` call for a `dynamic_schema_overrides=<name>` or any
   other `=<bare_name>` keyword argument.
2. For each `<bare_name>` referenced, confirm a `def <bare_name>` (or assignment) exists
   somewhere in the file.
3. `ruff check --select F` on this file must catch any remaining case, but do the manual
   grep too — this exact defect class recurs across syncs.

## tools/delegate_tool.py — known silent-defect history
A PRIOR sync's merge pasted a duplicate completion-line-printing block whose indentation
put the `print(...)` OUTSIDE its enclosing per-future `for` loop — one completion line
per BATCH instead of per completed TASK. After resolving, verify every completion-print
call site's enclosing statement chain actually includes the `for future in done:` (or
equivalent) loop — read indentation carefully, don't just trust that markers resolved
cleanly means the logic is right.

## tools/process_registry.py
NOTE: this file is grouped in g11-tools-b in this sync's split, not here — but if it
lands in your list instead, its `owner_task_id` field is a documented 3-way coordinated
change (also touches `tools/terminal_tool.py` and `tools/delegate_tool.py`) — read all
three together if any of the others are also conflicted in your group, don't resolve
`owner_task_id` handling in isolation.

## tools/async_delegation.py / tools/delegation_live_log.py
Related to `tools/delegate_tool.py`'s completion-line printing (see above) — if these
files also have per-task vs per-batch completion/logging logic, apply the same
indentation-chain verification: does the print/log statement's enclosing loop actually
iterate per-task, not per-batch?

## No specific prior guidance found for
tools/browser_tool.py, tools/checkpoint_manager.py, tools/discord_tool.py,
tools/feishu_drive_tool.py — use the common-guidance default. Flag design-collision
picks explicitly; these will need new FORK.md entries.
