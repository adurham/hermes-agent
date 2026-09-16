#!/usr/bin/env bash
# Produce the user state that preserve-user-state.sh verifies — through the
# ORDINARY user surface, never by writing files ourselves.
#
# Why this exists: an upgrade-preservation check over fixtures the harness wrote
# proves only that the harness can write files. Everything here is a real
# command a user would run, against the leg's real installed CLI, with a real
# (mocked-inference) provider configured:
#
#   hermes chat -q ...        a real turn  -> sessions/ transcripts + state.db rows
#   hermes auth add ...       a pooled credential -> auth.json
#   hermes profile create ..  a second profile    -> profiles/<name>/**
#
# The assertions below are about OUR fixture production, not about the upgrade:
# if an action silently produces nothing, the leg would "pass" the preservation
# check while testing nothing, so each action proves it landed.
#
# Requires: hermes command, HERMES_HOME, ok/fail helpers. Sourced by the driver.

# Probe, do not assume (the harness's rule for old refs): use a flag only if the
# installed CLI advertises it.
_user_state_help_has() {
  local hermes="$1" flag="$2"
  "$hermes" chat --help 2>&1 | grep -qF -- "$flag"
}

_user_state_db_sessions() {
  local py out
  py="$(_user_state_python)"
  out="$("$py" - "$HERMES_HOME/state.db" <<'PY' 2>/dev/null || true
import sqlite3, sys
try:
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    print(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
except Exception:
    print(-1)
PY
)"
  printf '%s' "${out:--1}"
}

user_state_produce() {
  local hermes="$1"
  step "user state: producing it through the ordinary CLI"

  # --- a real turn ---------------------------------------------------------
  local before after
  before="$(_user_state_db_sessions)"
  if _user_state_help_has "$hermes" "--quiet" || _user_state_help_has "$hermes" "-q"; then
    local rc=0
    HERMES_DISABLE_LAZY_INSTALLS=1 "$hermes" chat -q "Reply with the single word: ok" \
      > "$LOG_DIR/user-state-chat.log" 2>&1 || rc=$?
    log_group "first real chat turn" "$LOG_DIR/user-state-chat.log"
    [ "$rc" -eq 0 ] || fail "the first chat turn failed (exit $rc); see $LOG_DIR/user-state-chat.log"
  else
    # No one-shot flag on this ref: a piped prompt would need a PTY. Record the
    # gap loudly rather than pretending the leg covered sessions.
    fail "the installed CLI has no one-shot chat flag; this leg cannot produce a session through the user path"
  fi
  after="$(_user_state_db_sessions)"
  # A fresh install has NO state.db until the first turn, so the probe's -1
  # ("no readable db yet") is the expected starting point, not a failure. Only
  # growth matters: the turn must have created rows.
  local before_n after_n
  if [ "${before:-0}" -lt 0 ]; then before_n=0; else before_n="${before:-0}"; fi
  if [ "${after:-0}" -lt 0 ]; then after_n=0; else after_n="${after:-0}"; fi
  if [ "$after_n" -gt "$before_n" ]; then
    ok "a real turn created a session (state.db sessions $before_n -> $after_n)"
  else
    fail "the chat turn produced no session row (state.db sessions $before_n -> $after_n)"
  fi

  # --- a pooled credential -------------------------------------------------
  if [ ! -f "$HERMES_HOME/auth.json" ]; then
    HERMES_DISABLE_LAZY_INSTALLS=1 \
      "$hermes" auth add mock --type api-key --api-key "e2e-preservation-not-a-real-key" \
      > "$LOG_DIR/user-state-auth.log" 2>&1 \
      || fail "hermes auth add failed; see $LOG_DIR/user-state-auth.log"
    [ -s "$HERMES_HOME/auth.json" ] || fail "hermes auth add produced no auth.json"
    ok "a pooled credential exists (auth.json)"
  else
    ok "auth.json already present"
  fi

  # --- a second profile ----------------------------------------------------
  if [ ! -d "$HERMES_HOME/profiles/e2e-second" ]; then
    HERMES_DISABLE_LAZY_INSTALLS=1 \
      "$hermes" profile create e2e-second > "$LOG_DIR/user-state-profile.log" 2>&1 \
      || fail "hermes profile create failed; see $LOG_DIR/user-state-profile.log"
    [ -d "$HERMES_HOME/profiles/e2e-second" ] || fail "hermes profile create produced no profile dir"
    ok "a second profile exists (profiles/e2e-second)"
  else
    ok "the second profile already exists"
  fi
}