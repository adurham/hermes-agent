# g07-hermes_cli-c — fork features NEEDING RE-HOMING outside this group

Upstream v2026.9.14 decomposed several god-files in this group. Per guidance ("adopt
upstream's refactor as the new base structure, then re-home the fork's additive behavior on
top of it"), where the upstream destination is a file in MY list I re-homed in place. Where
the destination is a sibling module OUTSIDE my 8 files, I could not touch it — listed below.
Each was verified absent in the destination as of this write.

## Needs re-homing (fork behavior currently LOST — orchestrator must re-apply)

1. hermes_cli/sessions_cmd_browse.py  <- from main.py block 2
   Fork's terminal-too-small handling in the curses session picker: pre-check
   os.get_terminal_size() before curses init, and raise _TerminalTooSmall (instead of
   painting "Terminal too small" + blocking on getch()) so it falls through to the
   numbered-list picker. NOTE: class _TerminalTooSmall SURVIVES in main.py (upstream kept
   it, line ~1098) and tests/hermes_cli/test_session_browse.py:303 imports it from
   hermes_cli.main — but nothing raises it anymore. sessions_cmd_browse.py:201-208 still
   has the old dead-end getch() path.

2. hermes_cli/main_agent_cmds.py (cmd_insights)  <- from main.py block 6
   Fork's best-effort Anthropic billing anchor: fetch_anthropic_billing() -> account_billed
   dict passed to InsightsEngine.generate(..., account_billed=...). Gated on
   config model.provider in ("", "anthropic"), all failures swallowed.

3. hermes_cli/profile_cmd.py (cmd_profile create)  <- from main.py block 4
   Fork's --link wiring: link = getattr(args,"link",False); clone_config = clone or
   clone_from is not None or link; create_profile(..., link=link); and the
   "Skills, plugins, and memory are LINKED (shared) with <source>" message branch.
   The profiles.py side (create_profile(link=...), symlink logic) IS resolved and in place,
   so the CLI flag is currently inert.
   Related: hermes_cli/subcommands/profile.py still has UNRESOLVED conflict markers around
   the --link argparse flag (not in my group).

4. hermes_cli/plugins_manifest.py + hermes_cli/plugins_loader.py  <- from plugins.py blocks 1,2
   Fork's #78050 deferred-platform client-tools hook:
     - PluginManifest.client_tools_module: str = "" field (+ its contract docstring)
     - _parse_manifest reading data.get("client_tools_module")
     - PluginManager._register_deferred_platform_client_tools(manifest, loaded) and its
       call site in _register_deferred_platform
   plugins/platforms/a2a/plugin.yaml STILL declares `client_tools_module: tools` and
   plugins/platforms/a2a/__init__.py references the mechanism, so a2a's client tools are
   currently unreachable from plain CLI/TUI sessions.

5. hermes_cli/models_catalog_static.py  <- from models.py blocks 0,1
   (a) Fork's two provider rows dropped from CANONICAL_PROVIDERS:
         ProviderEntry("google-gemini-cli", "agy/antigravity cli", "Antigravity CLI via OAuth + Code Assist (Code Assist OAuth flow)")
         ProviderEntry("google-antigravity", "Google Antigravity (OAuth)", "Google Antigravity via OAuth + Code Assist (Gemini 3.5/3.1, Claude, GPT-OSS where entitled)")
   (b) Fork had PRUNED four retired Anthropic ids from _PROVIDER_MODELS["anthropic"]
       (claude-opus-4-5-20251101, claude-sonnet-4-5-20250929, claude-opus-4-20250514,
       claude-sonnet-4-20250514); upstream's extracted copy still lists them (line ~205).
       Low-risk cosmetic; re-apply only if the fork still wants them gone.

6. hermes_cli/main_desktop.py  <- from main.py block 3
   Fork moved _desktop_macos_relaunchable_fixup(desktop_dir) OUT of the
   "a rebuild just happened" branch to an unconditional
   `if not source_mode and packaged_executable is not None:` call, so the fixup reaches an
   already-packaged bundle when the content-hash stamp skips the rebuild. Upstream's
   main_desktop.py only calls the fixup inside the rebuild/staging paths (lines 1034, 1333).

7. hermes_cli/models_validate.py (or wherever the auto-correct now lives) <- from model_switch.py block 0
   Fork's "don't fuzzy-auto-correct an explicitly configured model" guard. Upstream's
   _validate_switch REMOVED the whole `validation["corrected_model"]` mechanism from
   model_switch.py (0 occurrences in pristine v2026.9.14; models_validate.py's docstring now
   says "The requested id is never rewritten: what the user selected is what the wire sees").
   => Upstream appears to have solved the same problem globally. LIKELY NO ACTION NEEDED,
   but worth a confirm: tests/hermes_cli/test_model_validation.py still asserts on
   corrected_model, and hermes_cli/models_validate.py never sets it.

8. Fork doc-URL rewrites (hermes-agent.nousresearch.com -> github.com/adurham/...) that moved
   out of my files with upstream's extractions:
     - hermes_cli/kanban_parser.py:438 (kanban docs URL)  <- kanban.py
     - hermes_cli/subcommands/fallback.py (fallback-providers)  <- main.py
     - hermes_cli/subcommands/secrets.py (user-guide/secrets/)  <- main.py
     - hermes_cli/subcommands/egress.py (egress/iron-proxy)  <- main.py
     - hermes_cli/main_dashboard.py (web-dashboard#authentication-gated-mode)  <- main.py
     - hermes_cli/model_switch_providers.py (nous model-catalog.json comment) <- model_switch.py
   Purely cosmetic help-text; batch-apply if the fork wants its own docs URLs everywhere.
   (model_catalog.py's DEFAULT_CATALOG_URL and portal_cli.py's DOCS_URL — both in my files —
   ARE preserved.)

## Pre-existing, NOT introduced by this merge
- main.py: fork has inline `submit` / `mcp-gateway` argparse definitions directly in
  _build_cli_parser() rather than in hermes_cli/subcommands/*. That is the pattern this
  repo's own AGENTS.md plugin policy warns about (a past PR removed 95 lines of hardcoded
  honcho argparse from this exact file). Per g07 guidance I PRESERVED it verbatim and am
  flagging it rather than redesigning it mid-merge.
- main.py: 30 ruff-F401 "imported but unused" (frozen-updater re-export surface) — upstream
  v2026.9.14 pristine has 31 of the same. I re-added the missing build_agents_parser() call
  (upstream imports it but never calls it, while "agents" is still in its known-subcommand
  list), which fixes one of them.
- plugins.py: 1 ruff-F811 (duplicate `import contextvars` in upstream's PLUGIN-COMPAT block)
  — identical in pristine v2026.9.14. Left alone (no drive-by edits).
