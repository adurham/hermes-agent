# Group g06-hermes_cli-b — FORK.md guidance

Files: hermes_cli/config_defaults.py, hermes_cli/config_migrations.py,
hermes_cli/curator.py, hermes_cli/doctor.py, hermes_cli/fallback_cmd.py,
hermes_cli/gateway.py, hermes_cli/input_sanitize.py, hermes_cli/inventory.py

## hermes_cli/config_migrations.py / hermes_cli/config_defaults.py
If there's a `SCHEMA_VERSION`-style bump conflict (both sides bumped it), the fork's
established pattern elsewhere (`hermes_state.py`) is `max(both) + 1`. Read BOTH sides'
migration function bodies before picking — a version bump only matters if it gates a
*destructive* migration; additive migrations (new keys with defaults) are safe to run
unconditionally and don't need the max-bump treatment.

## No specific prior guidance found for
hermes_cli/curator.py, hermes_cli/doctor.py, hermes_cli/fallback_cmd.py,
hermes_cli/gateway.py, hermes_cli/input_sanitize.py, hermes_cli/inventory.py — use the
common-guidance default (adopt upstream's structure, re-home fork additions on top of
it). Flag design-collision picks explicitly; these will need new FORK.md entries.

Note: `hermes_cli/doctor.py` powers `hermes doctor` — if the fork added any
fork-specific health checks (e.g. related to the rich banner state schema in
`hermes_cli/banner.py`, or credential-pool keychain-longlived checks), keep them; don't
let an upstream restructure of the doctor command's check registry silently drop a
fork-added check function while leaving a stale reference to it (the exact "silent
auto-merge deletion" class of bug this fork has hit before — verify with
`ruff check --select F` per the common gating rules).
