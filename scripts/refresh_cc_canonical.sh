#!/usr/bin/env bash
# Refresh agent/cc_canonical/tools_eager.json + the x-anthropic-billing-header
# values hardcoded in agent/anthropic_adapter.py from a live `claude` session.
#
# When real Claude Code ships a new schema or rotates its billing-header
# checksum (cch=...), our captured copies drift and Anthropic's classifier
# may start rejecting the bot's traffic. Run this to recapture both:
#
#   ./scripts/refresh_cc_canonical.sh
#
# Requirements (one-time):
#   brew install mitmproxy
#   security add-trusted-cert -d -p ssl -k ~/Library/Keychains/login.keychain \
#       ~/.mitmproxy/mitmproxy-ca-cert.pem    # macOS — accept mitmproxy CA
#
# What it does:
#   1. Starts mitmdump capturing traffic to ~/.cache/cc-refresh.flow
#   2. Runs `claude -p "say hi"` with HTTPS_PROXY + NODE_EXTRA_CA_CERTS
#      pointed at mitmproxy
#   3. Stops mitmdump, converts flows to HAR
#   4. Pulls the first /v1/messages request body
#   5. Writes the .tools array to agent/cc_canonical/tools_eager.json
#   6. Prints the captured billing-header values so you can sync the
#      hardcoded fallback in agent/anthropic_adapter.py
#
# Idempotent — re-runnable. Outputs nothing on stdout when nothing
# changed (modulo timestamps).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANONICAL="$REPO_ROOT/agent/cc_canonical/tools_eager.json"
FLOW="$HOME/.cache/cc-refresh.flow"
HAR="$HOME/.cache/cc-refresh.har"

mkdir -p "$(dirname "$FLOW")" "$(dirname "$CANONICAL")"

if ! command -v mitmdump >/dev/null 2>&1; then
    echo "ERROR: mitmdump not found. Install with: brew install mitmproxy" >&2
    exit 2
fi
if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: claude CLI not on PATH. Install via: npm i -g @anthropic-ai/claude-code" >&2
    exit 2
fi

if [[ ! -f "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" ]]; then
    echo "Initializing mitmproxy CA…" >&2
    mitmdump --listen-port 8080 --no-server >/dev/null 2>&1 &
    sleep 2
    kill $! 2>/dev/null || true
fi

echo "Starting mitmdump on :8080 …" >&2
rm -f "$FLOW"
mitmdump --listen-port 8080 --set save_stream_file="$FLOW" \
    >"$HOME/.cache/cc-refresh.mitm.log" 2>&1 &
MITM_PID=$!
trap 'kill $MITM_PID 2>/dev/null || true' EXIT
sleep 2

echo "Running claude through mitm proxy …" >&2
HTTPS_PROXY=http://localhost:8080 \
NODE_EXTRA_CA_CERTS="$HOME/.mitmproxy/mitmproxy-ca-cert.pem" \
    claude -p "say hi" >/dev/null 2>&1 || {
        echo "ERROR: claude failed under proxy. Verify the mitmproxy CA is" >&2
        echo "       trusted by Node:  ls $HOME/.mitmproxy/" >&2
        exit 3
    }

sleep 1
kill $MITM_PID 2>/dev/null || true
wait $MITM_PID 2>/dev/null || true
trap - EXIT

if [[ ! -s "$FLOW" ]]; then
    echo "ERROR: capture file $FLOW is empty — mitmdump may have crashed." >&2
    exit 4
fi

echo "Converting flows → HAR …" >&2
mitmdump -nr "$FLOW" --set hardump="$HAR" >/dev/null 2>&1

# Pull the first /v1/messages request body. There may be follow-up
# event_logging traffic; we want the first user-facing inference call.
BODY=$(jq -r '.log.entries[]
    | select(.request.url | contains("api.anthropic.com/v1/messages"))
    | .request.postData.text' "$HAR" | head -1)

if [[ -z "$BODY" ]]; then
    echo "ERROR: no /v1/messages request found in capture. Did claude actually" >&2
    echo "       run? Check $HOME/.cache/cc-refresh.mitm.log" >&2
    exit 5
fi

# Tools array → cc_canonical/tools_eager.json
echo "$BODY" | jq '.tools' > "$CANONICAL.new"
mv "$CANONICAL.new" "$CANONICAL"
N_TOOLS=$(jq 'length' "$CANONICAL")
SIZE=$(wc -c < "$CANONICAL")

echo "Wrote $CANONICAL: $N_TOOLS tools, $SIZE bytes"

# Tool sizes for at-a-glance comparison vs the budget the classifier
# accepts in a real CC session.
echo "Per-tool sizes:"
jq -r '.[] | "  \(.name)\t\(. | tostring | length)"' "$CANONICAL" | column -t -s$'\t'

# Billing-header values (block 0 of system). These need to be reflected
# in the hardcoded fallback in agent/anthropic_adapter.py until that
# function reads them from a config file.
BILLING=$(echo "$BODY" | jq -r '.system | if type=="array" then .[0].text else . end' \
    | grep -o 'x-anthropic-billing-header:.*' || true)

echo
if [[ -n "$BILLING" ]]; then
    echo "Captured billing header:"
    echo "  $BILLING"
    echo
    echo "If this differs from the hardcoded value in"
    echo "  agent/anthropic_adapter.py (search for x-anthropic-billing-header)"
    echo "update the literal string there."
else
    echo "WARNING: no billing header found in system prompt block 0 — Anthropic" >&2
    echo "         may have changed the wire format. Inspect the capture:" >&2
    echo "         $HAR" >&2
fi

echo
echo "Done. Commit the updated $CANONICAL when CI is green."
