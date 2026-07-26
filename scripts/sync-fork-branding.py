#!/usr/bin/env python3
"""Repoint "get code/docs from here" links at the adurham/hermes-agent fork.

Run this after every ``git merge upstream/main`` (see ``fork-merge-plan.py``,
step 5). Upstream's own files always say ``NousResearch/hermes-agent`` —
merging them back in silently reintroduces links that point at the wrong
repo, and (this is the part that actually bit us on 2026-07-25) some of
those links are the installer/updater's *source of truth* for where to
clone code from. Re-run this every time upstream is merged in, not just
once.

Usage::

    python scripts/sync-fork-branding.py             # apply changes
    python scripts/sync-fork-branding.py --dry-run    # preview only, no writes
    python scripts/sync-fork-branding.py --verbose    # list every match, incl. skipped

Idempotent: only matches the literal ``NousResearch`` spelling, so running
it twice in a row is always a no-op the second time.

What it deliberately does NOT touch (and why) — see NEVER_TOUCH_* below:
  - Fork-vs-upstream *detection* constants (OFFICIAL_REPO_URL and friends).
    These define what counts as "real upstream" so ``hermes update`` and the
    desktop app know they're running a fork and skip clobbering fork
    commits. Repointing them at the fork would make the tool think its own
    fork IS upstream and silently disable that protection — this is the
    exact mechanism that failed on 2026-07-25, keep it pointed at Nous.
  - Live services Nous actually operates with no fork equivalent: the
    portal, the inference API, the hosted docs site, Discord.
  - Attribution/legal text (Cargo.toml authors, NOTICE, copyright lines).
  - Historical issue/PR/security-advisory citations (only exist on the real
    upstream tracker).
  - Docker Hub / Homebrew / GitHub Releases pointers — the fork doesn't
    publish any of these yet, so pointing at an adurham/* equivalent would
    just be a broken link. (Docker Hub refs use the lowercase
    ``nousresearch/hermes-agent`` form, which none of the patterns below
    match anyway — they all key off the capitalized GitHub-org spelling.)

Full context + the manual pass this script codifies:
see the "point everything at my fork" session, 2026-07-25/26.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FORK_OWNER = "adurham"
REPO_NAME = "hermes-agent"

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
)

# ---------------------------------------------------------------------------
# Never touch: whole files/dirs. Every reference in these is fork-detection
# logic, or a test asserting fork-detection logic — not a "get code" pointer.
# ---------------------------------------------------------------------------
NEVER_TOUCH_PATHS = {
    "hermes_cli/banner.py",
    "hermes_cli/fork_banner.py",
    "apps/desktop/electron/update-remote.ts",
    "apps/desktop/electron/update-remote.test.ts",
    "FORK.md",
}
NEVER_TOUCH_DIR_PREFIXES = (
    "plugins/security-guidance/",
    "tests/",
)
NEVER_TOUCH_FILE_SUFFIXES = (
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".test.mjs",
)

# ---------------------------------------------------------------------------
# Never touch: specific lines, wherever they appear. Matched against the raw
# line text before any replacement is attempted. Checked against a two-line
# window (current + next) so a citation split across concatenated string
# literals — e.g. Python's `"https://.../" "security/advisories/..."` on
# consecutive lines — still gets caught.
# ---------------------------------------------------------------------------
NEVER_TOUCH_LINE_PATTERNS = [
    re.compile(p) for p in [
        r"OFFICIAL_REPO_URLS?\b",
        r"KNOWN_UPSTREAM_URLS\b",
        r"_UPSTREAM_REPO_URL\b",
        r"_OFFICIAL_REPO_CANONICAL\b",
        r"_CANONICAL_REPO\b",
        r"OFFICIAL_REPO_HTTPS_URL\b",
        r"OFFICIAL_REPO_CANONICAL\b",
        r"OFFICIAL_REPO\s*=",          # tools/skills_hub.py skill-provenance attribution
        r"DEFAULT_CATALOG_FALLBACK_URLS\b",  # deliberate upstream-as-fallback data source
        r"_add_upstream_remote|git remote add upstream|pull upstream main|Added upstream:",
        r"github\.repository\s*==",    # CI fork-detection gates (docker.yml, deploy-site.yml, ...)
        # No leading "/" required: a citation URL split across two
        # concatenated string literals (common in Python) can leave the "/"
        # as the trailing char of the FIRST literal, not the start of the
        # second — matching only the current+next line window, so anchoring
        # on "/" would miss exactly the case this window trick exists for.
        r"issues/\d+|pull/\d+|security/advisories",  # historical citations, real GHSAs
        r"/releases",                  # fork publishes no Releases (Homebrew tarball, release-notes links, etc.)
        r"Modifications by NousResearch|original work by NousResearch|Built by.*Nous Research",
        r"docker\s+(pull|run|build)|IMAGE_NAME\s*=|hub\.docker\.com",
        r"portal\.nousresearch\.com|inference-api\.nousresearch\.com|api\.nousresearch\.com"
        r"|agents\.nousresearch\.com|staging-nousresearch\.com|firecrawl-gateway\.nousresearch\.com"
        r"|com\.nousresearch\.hermes",
        r"discord\.gg/NousResearch",
    ]
]
NEVER_TOUCH_LINE_PATTERNS_CI = [
    re.compile(r"authors\s*=.*nousresearch", re.IGNORECASE),
]

# Multi-line literal blocks: once the opening line is seen, skip everything
# through the line that closes the bracket depth back to zero. Line-based
# matching alone misses these — e.g. OFFICIAL_REPO_URLS's member strings
# don't repeat the constant name, so a naive per-line check would "fix" the
# very set that's supposed to keep pointing at real upstream.
BLOCK_OPENER_PATTERNS = [
    re.compile(p) for p in [
        r"OFFICIAL_REPO_URLS\s*=",
        r"KNOWN_UPSTREAM_URLS\s*=",
        r"DEFAULT_CATALOG_FALLBACK_URLS\s*[:=]",
    ]
]

# Explicit file exceptions: these would otherwise match the general repo-link
# rule but either have no fork equivalent to point at yet, or are verbatim
# third-party content (quotes, testimonials) that must not be edited even
# when it happens to contain a repo URL.
NEVER_TOUCH_EXACT_FILES = {
    "SECURITY.md",
    "SECURITY.es.md",
    "packaging/homebrew/hermes-agent.rb",
    "website/src/data/userStories.json",  # verbatim third-party quotes/testimonials
    "skills/creative/ascii-video/README.md",  # describes real cross-repo skill-sync process
}

# ---------------------------------------------------------------------------
# Simple substring/regex replacements applied line-by-line, in order.
# ---------------------------------------------------------------------------
SIMPLE_REPLACEMENTS = [
    (re.compile(r"github\.com/NousResearch/hermes-agent", re.IGNORECASE),
     f"github.com/{FORK_OWNER}/{REPO_NAME}"),
    (re.compile(r"github\.com:NousResearch/hermes-agent", re.IGNORECASE),
     f"github.com:{FORK_OWNER}/{REPO_NAME}"),
    (re.compile(r"github:NousResearch/hermes-agent", re.IGNORECASE),
     f"github:{FORK_OWNER}/{REPO_NAME}"),
    (re.compile(r"raw\.githubusercontent\.com/NousResearch/hermes-agent", re.IGNORECASE),
     f"raw.githubusercontent.com/{FORK_OWNER}/{REPO_NAME}"),
]

# hermes-agent.nousresearch.com/docs/<path> -> resolved against website/docs/,
# or a known static-asset mapping, or the bare docs tree as a last resort.
DOCS_LINK_RE = re.compile(
    r"https://hermes-agent\.nousresearch\.com/docs/?([^\s\)\]\"'<]*)"
)
STATIC_ASSET_MAP = {
    "api/model-catalog.json": f"https://raw.githubusercontent.com/{FORK_OWNER}/{REPO_NAME}/main/website/static/api/model-catalog.json",
    # skills-index.json is intentionally absent: it's gitignored (build-time
    # generated, never committed), so a raw.githubusercontent link to it
    # would just 404. Left unresolved on purpose — see resolve_docs_link.
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".gz", ".tar", ".mp3", ".mp4", ".wasm", ".db", ".sqlite",
}


def is_never_touch_path(rel_path: str) -> bool:
    if rel_path in NEVER_TOUCH_PATHS or rel_path in NEVER_TOUCH_EXACT_FILES:
        return True
    if any(rel_path.startswith(p) for p in NEVER_TOUCH_DIR_PREFIXES):
        return True
    return any(rel_path.endswith(s) for s in NEVER_TOUCH_FILE_SUFFIXES)


def is_never_touch_line(window: str) -> bool:
    """``window`` is the current line, optionally joined with the next one —
    catches citations split across concatenated string literals."""
    for pat in NEVER_TOUCH_LINE_PATTERNS:
        if pat.search(window):
            return True
    for pat in NEVER_TOUCH_LINE_PATTERNS_CI:
        if pat.search(window):
            return True
    return False


def find_block_skip_lines(lines: list[str]) -> set[int]:
    """Return the set of 0-based line indices covered by a multi-line
    literal opened by BLOCK_OPENER_PATTERNS, through the line that closes
    its bracket depth back to zero. Handles reformatted/reflowed blocks
    after a future upstream merge, not just today's exact line layout."""
    skip: set[int] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(pat.search(line) for pat in BLOCK_OPENER_PATTERNS):
            depth = 0
            j = i
            opened = False
            while j < len(lines):
                for ch in lines[j]:
                    if ch in "([{":
                        depth += 1
                        opened = True
                    elif ch in ")]}":
                        depth -= 1
                skip.add(j)
                if opened and depth <= 0:
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return skip


def resolve_docs_link(path_part: str, unresolved: list[str], rel_path: str) -> str | None:
    """Return the replacement URL for a hermes-agent.nousresearch.com/docs/<path>
    link, or None if it can't be confidently resolved (caller leaves it as-is)."""
    path_part = path_part.rstrip("/")

    if not path_part:
        return f"https://github.com/{FORK_OWNER}/{REPO_NAME}/tree/main/website/docs"

    if path_part in STATIC_ASSET_MAP:
        return STATIC_ASSET_MAP[path_part]

    docs_dir = REPO_ROOT / "website" / "docs"
    for candidate in (
        docs_dir / f"{path_part}.md",
        docs_dir / f"{path_part}.mdx",
        docs_dir / path_part / "index.md",
        docs_dir / path_part / "index.mdx",
    ):
        if candidate.is_file():
            rel = candidate.relative_to(REPO_ROOT).as_posix()
            return f"https://github.com/{FORK_OWNER}/{REPO_NAME}/blob/main/{rel}"

    unresolved.append(f"{rel_path}: /docs/{path_part}")
    return None


def process_file(path: Path, *, dry_run: bool, verbose: bool, unresolved: list[str]) -> int:
    rel_path = path.relative_to(REPO_ROOT).as_posix()
    if is_never_touch_path(rel_path):
        return 0
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return 0

    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 0

    if "nousresearch" not in original.lower():
        return 0

    lines = original.splitlines(keepends=True)
    block_skip = find_block_skip_lines(lines)

    changed_lines = 0
    out_lines = []
    for idx, line in enumerate(lines):
        if idx in block_skip:
            out_lines.append(line)
            continue

        window = line + (lines[idx + 1] if idx + 1 < len(lines) else "")
        if "nousresearch" not in line.lower() or is_never_touch_line(window):
            out_lines.append(line)
            continue

        new_line = line
        for pat, repl in SIMPLE_REPLACEMENTS:
            new_line = pat.sub(repl, new_line)

        def _docs_sub(m: re.Match) -> str:
            resolved = resolve_docs_link(m.group(1), unresolved, rel_path)
            return resolved if resolved is not None else m.group(0)

        new_line = DOCS_LINK_RE.sub(_docs_sub, new_line)

        if new_line != line:
            changed_lines += 1
            if verbose:
                print(f"  {rel_path}:\n    - {line.strip()}\n    + {new_line.strip()}")
        out_lines.append(new_line)

    if changed_lines and not dry_run:
        path.write_text("".join(out_lines), encoding="utf-8")

    return changed_lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    parser.add_argument("--verbose", action="store_true", help="print every changed line")
    args = parser.parse_args()

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout.splitlines()

    files_changed = 0
    lines_changed = 0
    unresolved: list[str] = []

    for rel in tracked:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        n = process_file(path, dry_run=args.dry_run, verbose=args.verbose, unresolved=unresolved)
        if n:
            files_changed += 1
            lines_changed += n
            if not args.verbose:
                print(f"  {rel} ({n} line{'s' if n != 1 else ''})")

    print()
    print("=" * 78)
    action = "Would change" if args.dry_run else "Changed"
    print(f"{action} {lines_changed} line(s) across {files_changed} file(s).")
    if unresolved:
        print()
        print(f"⚠ {len(unresolved)} docs link(s) could not be resolved to a real file — left unchanged, review by hand:")
        for u in unresolved:
            print(f"  {u}")
    if args.dry_run:
        print()
        print("Dry run — nothing written. Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
