#!/usr/bin/env python3
"""Pre-merge analyzer for the adurham/hermes-agent fork.

Run before merging ``upstream/main`` into ``main`` to see what conflicts
to expect. Doesn't actually merge anything — read-only analysis.

Usage:
    python scripts/fork-merge-plan.py
    python scripts/fork-merge-plan.py --fetch     # fetch upstream first
    python scripts/fork-merge-plan.py --verbose   # show per-hunk overlap detail

What it does:

1. Lists commits in ``upstream/main`` NOT yet in ``main`` (since last merge).
2. For each upstream-changed file, checks if the fork also has divergence there.
3. For files with overlap, runs ``git merge-tree`` to detect real conflicts
   ahead of time without touching the working tree.
4. Prints a per-file verdict: ``CLEAN``, ``LIKELY-MERGE``, or ``CONFLICTS``.

Exit code:
    0  no conflicts predicted
    1  conflicts predicted (still merge-able; just review carefully)
    2  internal error
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict


def run(args, *, check=True):
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"ERROR running {' '.join(args)}: {r.stderr}", file=sys.stderr)
        sys.exit(2)
    return r.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="run `git fetch upstream` first")
    parser.add_argument("--verbose", action="store_true", help="show per-hunk overlap detail")
    parser.add_argument("--upstream", default="upstream/main", help="upstream ref (default: upstream/main)")
    parser.add_argument("--ours", default="main", help="local ref (default: main)")
    args = parser.parse_args()

    if args.fetch:
        print("Fetching upstream...")
        run(["git", "fetch", "upstream"], check=False)

    # 1. New upstream commits
    log = run(["git", "log", "--oneline", "--no-merges", f"{args.ours}..{args.upstream}"]).strip()
    if not log:
        print(f"✓ {args.ours} is up to date with {args.upstream}. Nothing to merge.")
        return 0

    commits = log.split("\n")
    print(f"Upstream has {len(commits)} new commits since last merge:\n")
    for c in commits[:20]:
        print(f"  {c}")
    if len(commits) > 20:
        print(f"  ... and {len(commits) - 20} more")
    print()

    # 2. Files changed upstream
    upstream_files = set(run(["git", "diff", "--name-only", f"{args.ours}..{args.upstream}"]).strip().split("\n"))
    upstream_files.discard("")

    # 3. Files changed in fork (divergence vs upstream)
    fork_files = set(run(["git", "diff", "--name-only", f"{args.upstream}..{args.ours}"]).strip().split("\n"))
    fork_files.discard("")

    # 4. Overlap = both sides changed it
    overlap = sorted(upstream_files & fork_files)
    upstream_only = sorted(upstream_files - fork_files)
    fork_only = sorted(fork_files - upstream_files)

    print(f"Files changed upstream: {len(upstream_files)}")
    print(f"Files changed in fork:  {len(fork_files)}")
    print(f"Files changed in BOTH:  {len(overlap)} ← potential conflicts\n")

    # 5. Use git merge-tree to do a real dry-run merge
    base = run(["git", "merge-base", args.ours, args.upstream]).strip()

    print("=" * 78)
    print("MERGE PREDICTION")
    print("=" * 78)
    print()

    conflicts = []
    likely_merges = []
    clean = []

    # Modern git merge-tree: ``git merge-tree --write-tree <branch1> <branch2>``
    # On success (no conflicts) exits 0 with just the tree SHA.
    # On conflict exits 1 with tree SHA + list of conflicting paths + messages.
    r = subprocess.run(
        ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages",
         args.ours, args.upstream],
        capture_output=True, text=True
    )
    # Output format: first line is the merged-tree SHA, subsequent lines are
    # conflicting paths (when --name-only is set and there are conflicts).
    out = r.stdout.strip()
    lines = [l for l in out.split("\n") if l]
    conflicting_paths = set()
    for ln in lines:
        if re.match(r"^[0-9a-f]{40}$", ln):
            continue
        conflicting_paths.add(ln)

    for f in overlap:
        if f in conflicting_paths:
            conflicts.append(f)
        else:
            likely_merges.append(f)

    for f in upstream_only:
        clean.append(f)

    # Output
    if conflicts:
        print(f"❌ CONFLICTS predicted in {len(conflicts)} files:")
        for f in conflicts:
            print(f"   {f}")
        print()

    if likely_merges:
        print(f"⚠  LIKELY-MERGE (both sides changed but no overlap): {len(likely_merges)} files")
        if args.verbose:
            for f in likely_merges:
                print(f"   {f}")
        else:
            for f in likely_merges[:10]:
                print(f"   {f}")
            if len(likely_merges) > 10:
                print(f"   ... and {len(likely_merges) - 10} more (use --verbose to show all)")
        print()

    if clean:
        print(f"✅ CLEAN apply (upstream-only changes): {len(clean)} files")
        if args.verbose:
            for f in clean[:50]:
                print(f"   {f}")
            if len(clean) > 50:
                print(f"   ... and {len(clean) - 50} more")
        else:
            print(f"   (use --verbose to list)")
        print()

    # Verbose: for each conflicting file, run merge-tree without --name-only and show
    # which fork hunks are at risk
    if args.verbose and conflicts:
        print("=" * 78)
        print("VERBOSE CONFLICT DETAIL (per-hunk)")
        print("=" * 78)
        print()

        # For each conflicting file, find the conflict regions
        for f in conflicts:
            print(f"--- {f} ---")
            # Use git merge-file to detect actual lines
            # Simpler: show both sides' hunk summaries vs base
            up_diff = run(["git", "diff", "--stat", f"{base}..{args.upstream}", "--", f]).strip()
            our_diff = run(["git", "diff", "--stat", f"{base}..{args.ours}", "--", f]).strip()
            print(f"  Upstream: {up_diff}")
            print(f"  Fork:     {our_diff}")
            print()

    # Summary verdict
    print("=" * 78)
    if conflicts:
        print(f"VERDICT: {len(conflicts)} conflict file(s). Plan resolution before merging.")
        print("Recommended approach:")
        print("  1. git checkout -b merge/upstream-$(date +%Y-%m-%d)")
        print(f"  2. git merge {args.upstream}")
        print("  3. Resolve conflicts:")
        for f in conflicts:
            if f.startswith("agent/fork/") or f == "FORK.md":
                hint = "(shouldn't happen — fork-only file)"
            elif f == "run_agent.py":
                hint = "take ours for forwarders; review structural changes"
            elif f == "agent/anthropic_adapter.py":
                hint = "take ours for OAuth path; review base-url + beta-gating areas"
            elif f == "agent/chat_completion_helpers.py":
                hint = "the wholesale-replaced function — likely take ours wholesale"
            elif f == "agent/conversation_loop.py":
                hint = "refusal handler ~line 1273 + fork callouts; review carefully"
            else:
                hint = "review case by case"
            print(f"     - {f}: {hint}")
        print("  4. Run tests: source .venv/bin/activate && pytest tests/run_agent tests/agent -x")
        print("  5. Push merge branch and FF main locally before pushing main")
        return 1
    else:
        print("VERDICT: No conflicts predicted. Should merge cleanly.")
        print("Quick merge command:")
        print(f"  git merge {args.upstream} && pytest tests/run_agent tests/agent -x && git push origin main")
        return 0


if __name__ == "__main__":
    sys.exit(main())
