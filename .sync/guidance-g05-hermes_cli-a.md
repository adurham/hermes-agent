# Group g05-hermes_cli-a — FORK.md guidance

Files: hermes_cli/auth.py, hermes_cli/banner.py, hermes_cli/callbacks.py,
hermes_cli/cli_agent_setup_mixin.py, hermes_cli/cli_commands_mixin.py,
hermes_cli/clipboard.py, hermes_cli/commands.py, hermes_cli/config.py

## hermes_cli/banner.py — CRITICAL, known recurring silent-defect
Upstream rewrites this file periodically. **A prior sync's auto-merge DROPPED the fork's
helper functions `_skin_branding` and `_resolve_agent_name` while their CALLERS
survived — a latent runtime crash (NameError at call time, not import time, so it's easy
to miss).** After resolving this file, you MUST:
1. `grep -n "def _skin_branding"` — must find a definition.
2. `grep -n "def _resolve_agent_name"` — must find a definition.
If either is missing, restore it from the fork's pre-merge version
(`git show ab0d3abd11:hermes_cli/banner.py` — read-only, does not touch MERGE_HEAD) and
splice it back in, adapting call sites to upstream's new structure if needed.

Also: the rich `get_git_banner_state` schema (`{local, origin, upstream, carried,
upstream_behind}`) is FORK-ONLY — keep it. Fold upstream's Docker build-SHA fallback (if
present) INTO this schema rather than replacing it.

## hermes_cli/config.py
No specific prior guidance on this exact file, but it's adjacent to
`hermes_cli/config_defaults.py` and `hermes_cli/config_migrations.py` (a sibling group) —
if you see a schema-version-like bump conflict, prefer `max(both) + 1` unless one side's
version gates a destructive migration the other doesn't have (read both migration bodies
before picking, don't just take the higher number blindly).

## No specific prior guidance found for
hermes_cli/auth.py, hermes_cli/callbacks.py, hermes_cli/cli_agent_setup_mixin.py,
hermes_cli/cli_commands_mixin.py, hermes_cli/clipboard.py, hermes_cli/commands.py —
use the common-guidance default (adopt upstream's structure, re-home fork additions on
top). Flag design-collision picks explicitly; these will need new FORK.md entries.
