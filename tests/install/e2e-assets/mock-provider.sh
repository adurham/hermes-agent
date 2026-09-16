#!/usr/bin/env bash
# Start/stop the mock inference server and point the app at it as a REAL
# configured provider.
#
# Why: the desktop app boots to the onboarding overlay when no provider is
# configured - a fullscreen div that intercepts every click, which killed
# the app-update legs. Seeding a fake key makes the overlay vanish but
# leaves a lying app; this makes the app GENUINELY configured (config.yaml
# provider + key env, exactly what tests-js/scripts/mock-server.ts's
# dev:mock flow writes) with a real, chat-capable backend.
#
# Usage (sourced from a driver):
#   mock_start <workroot>      start, write config into $HERMES_HOME
#   mock_stop                  kill the background server
#
# Requires: ASSETS (e2e-assets dir), LOG_DIR, HERMES_HOME, ok/fail helpers.

MOCK_PIDFILE=""
MOCK_URLFILE=""

mock_start() {
  local workroot="${1:?mock_start needs a workroot}"
  MOCK_PIDFILE="$workroot/mock.pid"
  MOCK_URLFILE="$workroot/mock.url"
  rm -f "$MOCK_PIDFILE" "$MOCK_URLFILE"

  # Bare `node file.ts` type-stripping works on node >=22.18 (the images
  # ship 22.22+; the installed managed node is >=26) - same contract as
  # the repo's own `dev:mock` script.
  node "$ASSETS/mock-provider.mjs" "$MOCK_URLFILE" > "$LOG_DIR/mock.log" 2>&1 &
  echo $! > "$MOCK_PIDFILE"

  local _i
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    [ -s "$MOCK_URLFILE" ] && break
    sleep 0.2
  done
  if [ ! -s "$MOCK_URLFILE" ]; then
    log_group "mock server transcript" "$LOG_DIR/mock.log"
    fail "mock inference server did not come up; transcript above"
  fi
  local url
  url="$(cat "$MOCK_URLFILE")"
  export HERMES_E2E_MOCK_URL="$url"
  ok "mock inference server: $url"

  node "$ASSETS/../../../tests-js/scripts/mock-provider-config.ts" "$HERMES_HOME" "$url" || fail "mock provider config failed"
  ok "provider 'mock' configured in $HERMES_HOME (api $url/v1)"

  # ...and also as a PLAIN OpenAI-compatible endpoint, which is the only way an
  # OLD tree can see it. Pre-mock refs decide "am I configured?" from process
  # env + .env keys + PROVIDER_REGISTRY alone -- they never read config.yaml's
  # providers block -- so MOCK_API_KEY is invisible to them and `hermes chat`
  # dies with "no API keys or providers found". OPENAI_BASE_URL is in every
  # vintage's accepted set (it is how vLLM/llama.cpp local servers are used) and
  # OPENAI_API_KEY is a plain credential for it, so this makes the install
  # genuinely chat-capable on ANY ref without the product knowing about a mock.
  _mock_write_portable_provider "$HERMES_HOME" "$url" || fail "mock .env write failed"
  ok "also reachable as an OpenAI-compatible endpoint (OPENAI_BASE_URL=$url/v1)"
}

# Idempotent: replaces exactly these two keys, keeps every other line as-is.
_mock_write_portable_provider() {
  local home="$1" url="$2" envfile tmp
  envfile="$home/.env"
  tmp="$envfile.mock.$$"
  mkdir -p "$home"
  if [ -f "$envfile" ]; then
    grep -vE '^[[:space:]]*(export[[:space:]]+)?(OPENAI_BASE_URL|OPENAI_API_KEY)[[:space:]]*=' \
      "$envfile" > "$tmp" || true
  else
    : > "$tmp"
  fi
  printf 'OPENAI_BASE_URL=%s/v1\nOPENAI_API_KEY=e2e-mock-key\n' "$url" >> "$tmp"
  chmod 600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$envfile"
}

mock_stop() {
  if [ -n "$MOCK_PIDFILE" ] && [ -f "$MOCK_PIDFILE" ]; then
    kill "$(cat "$MOCK_PIDFILE")" 2>/dev/null || true
    rm -f "$MOCK_PIDFILE"
  fi
}
