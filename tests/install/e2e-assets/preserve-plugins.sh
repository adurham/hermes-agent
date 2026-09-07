#!/usr/bin/env bash
# Shared plugin upgrade-preservation hooks for the install E2E drivers
# (POSIX + macOS). Sourced by tests/install/installer-script-e2e.sh and
# tests/install/macos-desktop-e2e.sh.
#
# The contract under test: a tagged Hermes upgrade must NOT delete or modify
# anything in the active home's plugins/** tree or any profile's plugins/**
# tree — including directory wrapper markers (mnemosyne-wrapper.json),
# symlinked runtimes, and the externally-owned sidecar witness file that
# lives OUTSIDE the home. Destructive flows (explicit uninstall, plugin
# removal, profile/user deletion) are out of contract and not exercised.
#
# The fixtures are deliberately non-dependency: directory wrappers with no
# pyproject anywhere in the scanned root, so the plugin scanner never
# recurses into a dependency graph and nothing is downloaded.
#
# State flow (called by the driver):
#   preserve_before_upgrade  seed fixtures + snapshot  ->  $PRESERVE_SNAPSHOT
#   preserve_after_upgrade   verify against snapshot    (exit 1 on violation)
#
# Requires: HERMES_HOME set (the leg's isolated home), WORK_ROOT, LOG_DIR,
# REPO_ROOT. Verifier: tests/install/e2e-assets/verify-plugin-preservation.py
# (stdlib-only; runs under python3 or $HERMES_E2E_PYTHON).

PRESERVE_SNAPSHOT="$WORK_ROOT/plugin-preservation-snapshot.json"
PRESERVE_REPORT="$LOG_DIR/plugin-preservation-report.json"
PRESERVE_EXTERNAL="$WORK_ROOT/external-mnemosyne-runtime"

_preserve_python() {
  if [ -n "${HERMES_E2E_PYTHON:-}" ]; then
    printf '%s' "$HERMES_E2E_PYTHON"
  else
    printf 'python3'
  fi
}

seed_plugin_preservation_fixtures() {
  # NOT a clobbering seeder: if the wrapper is already populated the fixture
  # state is left untouched, so a re-entered leg cannot erase a regression
  # the earlier phase recorded. Only a matching marker may be pre-existing.
  local home="$1" external="$2"
  local wrapper="$home/plugins/mnemosyne-wrapper"
  local marker="$wrapper/mnemosyne-wrapper.json"
  if [ -d "$wrapper" ] && [ -n "$(ls -A "$wrapper" 2>/dev/null)" ]; then
    grep -qF '"marker": "mnemosyne-wrapper"' "$marker" 2>/dev/null \
      || fail "refusing to reseed preservation fixtures: $wrapper already populated without the expected marker"
    ok "preservation fixtures already present; left untouched"
  else
    mkdir -p "$wrapper"
    printf '{"wrapper": true, "marker": "mnemosyne-wrapper", "owner": "e2e-preservation"}\n' \
      > "$marker"
    printf '#!/usr/bin/env python3\n# directory wrapper fixture: no dependencies\nPLUGIN = "mnemosyne-wrapper"\n' \
      > "$wrapper/plugin.py"
    if [ ! -e "$wrapper/runtime" ] && [ ! -L "$wrapper/runtime" ]; then
      ln -s "$external" "$wrapper/runtime" \
        || fail "could not create the runtime link at $wrapper/runtime; the preservation contract cannot be exercised"
    fi
  fi
  mkdir -p "$external" "$home/profiles/e2e-preserve/plugins/second-plugin"
  [ -e "$external/sidecar-witness.txt" ] \
    || printf 'external-sidecar-witness-v1\n' > "$external/sidecar-witness.txt"
  [ -e "$external/engine.bin" ] \
    || printf '\x00\x01\x02external-engine\n' > "$external/engine.bin"
  [ -e "$home/profiles/e2e-preserve/plugins/second-plugin/marker.json" ] \
    || printf '{"plugin": "second-plugin", "profile": "e2e-preserve"}\n' \
      > "$home/profiles/e2e-preserve/plugins/second-plugin/marker.json"
  [ -e "$home/profiles/e2e-preserve/plugins/second-plugin/data.bin" ] \
    || printf 'profile-plugin-bytes\n' \
      > "$home/profiles/e2e-preserve/plugins/second-plugin/data.bin"
  ok "seeded preservation fixtures: $wrapper (marker + runtime link) + profile tree; external witness at $external"
}

preserve_before_upgrade() {
  step "plugin preservation: seeding fixtures and snapshotting pre-upgrade state"
  seed_plugin_preservation_fixtures "$HERMES_HOME" "$PRESERVE_EXTERNAL"
  local py; py="$(_preserve_python)"
  "$py" "$REPO_ROOT/tests/install/e2e-assets/verify-plugin-preservation.py" \
    snapshot --home "$HERMES_HOME" --out "$PRESERVE_SNAPSHOT" 2>&1 | ts_prefix \
    || fail "plugin preservation snapshot failed"
  # An empty snapshot proves nothing; refuse to build the leg's claim on it.
  "$py" - "$PRESERVE_SNAPSHOT" <<'PYEOF' || fail "plugin preservation snapshot is EMPTY: no plugin entries recorded, cannot verify preservation"
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    snap = json.load(fh)
n = len(snap.get("entries", {}))
print(f"snapshot entries: {n}")
raise SystemExit(0 if n else 3)
PYEOF
  ok "pre-upgrade plugin snapshot at $PRESERVE_SNAPSHOT"
}

preserve_after_upgrade() {
  step "plugin preservation: verifying post-upgrade state"
  [ -f "$PRESERVE_SNAPSHOT" ] \
    || fail "no pre-upgrade plugin snapshot at $PRESERVE_SNAPSHOT; cannot verify preservation"
  local py; py="$(_preserve_python)"
  local rc=0
  "$py" "$REPO_ROOT/tests/install/e2e-assets/verify-plugin-preservation.py" \
    verify --home "$HERMES_HOME" --snapshot "$PRESERVE_SNAPSHOT" \
    --report "$PRESERVE_REPORT" > "$LOG_DIR/plugin-preservation-verify.log" 2>&1 || rc=$?
  log_group "plugin preservation verify transcript" "$LOG_DIR/plugin-preservation-verify.log"
  [ "$rc" -eq 0 ] \
    || fail "plugin preservation violated by the upgrade: deleted/modified entries above; report at $PRESERVE_REPORT"
  ok "plugins/** and profile plugin trees (markers, symlinked runtimes, external sidecar witness) survived the upgrade intact"
}
