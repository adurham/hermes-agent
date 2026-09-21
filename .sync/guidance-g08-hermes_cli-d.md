# Group g08-hermes_cli-d — FORK.md guidance

Files: hermes_cli/providers.py, hermes_cli/setup.py, hermes_cli/setup_whatsapp_cloud.py,
hermes_cli/skin_engine.py, hermes_cli/subcommands/profile.py, hermes_cli/tools_config.py,
hermes_cli/uninstall.py, hermes_cli/update_cmd.py, hermes_cli/web_server.py

## hermes_cli/tools_config.py
When adopting upstream's web-provider row changes, keep building rows via the fork's
`_plugin_provider_row()` helper if it exists in this file or is imported by it. Upstream's
inline dict approach (if present in their diff) DROPS `requires_nous_auth` /
`managed_nous_feature` / `_is_available` fields, which makes every web row falsely report
"configured" in the `hermes tools` picker. If you see an inline dict replacing a call to
`_plugin_provider_row()`, that's the bug pattern — keep the fork's row-builder function
call instead.

Also relevant here: this file may reference the Tavily provider's setup schema (badge
"paid", keyed-only per the FORK.md Tavily decision — see agent/web_search_registry.py
guidance in group g04 for the full policy). Do not let this file's rows imply Tavily is
keyless/free-tier.

## hermes_cli/skin_engine.py
Related to `hermes_cli/banner.py`'s `_skin_branding` helper (see group g05's CRITICAL
banner.py note) — if this file also references skin/branding resolution, cross-check that
`_skin_branding` still exists in `hermes_cli/banner.py` after that group resolves it
(you can `git show ab0d3abd11:hermes_cli/banner.py` read-only to compare, this does not
touch MERGE_HEAD).

## No specific prior guidance found for
hermes_cli/providers.py, hermes_cli/setup.py, hermes_cli/setup_whatsapp_cloud.py,
hermes_cli/subcommands/profile.py, hermes_cli/uninstall.py, hermes_cli/update_cmd.py,
hermes_cli/web_server.py — use the common-guidance default. Flag design-collision picks
explicitly; these will need new FORK.md entries.
