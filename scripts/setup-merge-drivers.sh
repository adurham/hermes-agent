#!/usr/bin/env bash
# Retired 2026-10-09 (upstream sync v0.21.6). The `uvlock-ours` merge driver regenerated uv.lock with raw
# `uv lock`, which cannot resolve v0.21.6's dependency graph (neutts and kittentts pin different soundfile
# versions; only PM's resolver reconciles them). .gitattributes now uses `merge=ours` for uv.lock; regenerate
# after a merge with `hermes pm lock` (see the lock procedure in FORK.md's v0.21.6 sync entry).
#
# Idempotent: removes the stale driver from THIS clone's .git/config if a previous run registered it.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git config --remove-section merge.uvlock-ours 2>/dev/null && echo "removed stale 'uvlock-ours' merge driver from .git/config" || true
echo "uv.lock now merges as 'ours'; regenerate with: hermes pm lock"
