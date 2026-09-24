#!/usr/bin/env bash
# hlxc-test — run the hermes-agent fork's test suite on hermes-gw-01 ("hlxc").
#
# Why this exists: the fork's GitHub CI could not run the Python suite at all
# (upstream's tests.yml needs ubuntu-latest-96-core, which GitHub does not
# provision for a personal fork), so suites were being run on the user's Mac
# and eating battery. CI now runs the suite sharded on standard runners; this
# box covers what CI still cannot reach — long/unsharded runs, and anything
# needing a specific local environment.
#
# SAFETY: this NEVER touches /opt/hermes-agent, which is the LIVE install
# serving three running services (hermes-gateway, hermes-gateway-dashboard,
# hermes-serve). It works in a separate checkout under /srv/hermes-ci/repo.
#
# Usage (from the Mac):
#   ssh hermes-gw-01 hlxc-test                      # whole suite
#   ssh hermes-gw-01 'hlxc-test tests/tools/'       # one subtree
#   ssh hermes-gw-01 'hlxc-test --slice 1/6'        # one CI shard
#   ssh hermes-gw-01 'hlxc-test --sync'             # pull latest fork main first
set -uo pipefail

REPO=/srv/hermes-ci/repo
export PATH="$HOME/.local/bin:$PATH"

# Optional: refresh the checkout before running.
if [ "${1:-}" = "--sync" ]; then
  shift
  echo "==> syncing $REPO to origin/main"
  git -C "$REPO" fetch --quiet origin main
  git -C "$REPO" checkout --quiet main
  git -C "$REPO" merge --ff-only --quiet origin/main || {
    echo "!! not a fast-forward — leaving the checkout alone. Inspect $REPO." >&2
    exit 2
  }
  git -C "$REPO" log --oneline -1
  echo "==> uv sync"
  (cd "$REPO" && uv sync --locked --python 3.11 --extra all --extra dev \
      --extra anthropic --extra mistral --extra fal --extra modal \
      --extra daytona --extra hindsight --extra parallel-web 2>&1 | tail -3)
fi

cd "$REPO" || { echo "!! no checkout at $REPO — clone it first" >&2; exit 2; }

# Refuse to run if this is somehow the live install.
if [ "$(readlink -f "$REPO")" = "/opt/hermes-agent" ]; then
  echo "!! refusing: $REPO resolves to the LIVE install" >&2
  exit 3
fi

# The box has 6 cores; run_tests.sh defaults to cpu_count*2 workers, which is
# fine here (the pool is per-FILE subprocesses, and the curve is shallow).
export HERMES_TEST_WORKERS="${HERMES_TEST_WORKERS:-6}"
export OPENROUTER_API_KEY="" OPENAI_API_KEY="" NOUS_API_KEY=""

echo "==> $REPO @ $(git rev-parse --short HEAD)  workers=$HERMES_TEST_WORKERS"
if [ "$#" -eq 0 ]; then
  echo "==> full suite"
  exec bash scripts/run_tests.sh
fi
echo "==> args: $*"
exec bash scripts/run_tests.sh "$@"
