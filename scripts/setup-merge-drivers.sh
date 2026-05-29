#!/usr/bin/env bash
# Register the fork's custom git merge drivers in THIS clone's .git/config.
#
# Merge drivers live in .git/config (not version-controlled), so each clone
# must run this once. .gitattributes (committed) names the drivers; this script
# defines what they do.
#
# Driver: uvlock-ours
#   On an uv.lock merge conflict, keep our version of the lockfile, then
#   regenerate it with `uv lock` so it reflects the MERGED pyproject.toml
#   (which git merges normally). This turns a guaranteed every-merge conflict
#   into a no-op. If `uv` isn't on PATH we keep our lockfile unchanged and warn
#   — the merge still completes; you just regenerate manually afterward.
#
# Idempotent: safe to re-run.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# %A = our version (current branch), %P = pathname of the file in the work tree.
# The driver script: keep %A as-is, then `uv lock` regenerates against merged
# pyproject. Exit 0 = resolved.
git config merge.uvlock-ours.name "uv.lock: keep ours, then regenerate from merged pyproject"
git config merge.uvlock-ours.driver \
  'sh -c '"'"'if command -v uv >/dev/null 2>&1; then uv lock --quiet >/dev/null 2>&1 && echo "[merge-driver] uv.lock regenerated from merged pyproject" || echo "[merge-driver] uv lock failed; kept ours — run uv lock manually"; else echo "[merge-driver] uv not found; kept our uv.lock — run uv lock manually"; fi; exit 0'"'"' %A'

echo "✓ Registered merge driver 'uvlock-ours' in $REPO_ROOT/.git/config"
echo "  uv.lock conflicts will now auto-resolve (ours + regenerate) on merge."
echo ""
echo "Verify with:  git config --get merge.uvlock-ours.driver"
