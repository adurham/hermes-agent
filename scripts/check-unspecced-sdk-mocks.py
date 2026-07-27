#!/usr/bin/env python3
"""
Grep-based checker for unspecced Mock()/MagicMock() at known SDK client
boundaries in tests/.

Context: a bare ``MagicMock()`` auto-vivifies EVERY attribute access as a
truthy sub-mock. A test that does
``client.messages.stream.side_effect = ...`` still "passes" forever even
after production code moves to a different attribute path (e.g.
``client.beta.messages.stream``) — the wrong branch silently absorbs the
call and the assertion never runs. This shipped and went undetected for an
unknown period (see FORK.md's 2026-07-27 test-suite-comprehensiveness
entry). ``create_autospec`` / ``Mock(spec=...)`` fix this by raising
AttributeError on any attribute the real class doesn't have.

This checker does NOT try to retrofix the ~5,300 existing unspecced mocks
across the suite (a full migration was judged too large/risky for a single
pass — see the FORK.md entry). It only stops the count from growing: flags
NEW/any unspecced ``Mock()``/``MagicMock()`` instantiations that are used to
build a fake SDK/provider client (assigned to a variable matching a
known-client-ish name, or immediately followed by an attribute chain that
matches a known SDK shape) without ``spec=``/``autospec=``/``create_autospec``.

Usage:
    # Scan staged changes (default when run from a git checkout)
    python scripts/check-unspecced-sdk-mocks.py

    # Scan the full tests/ tree (baseline audit — WILL be noisy; see below)
    python scripts/check-unspecced-sdk-mocks.py --all

    # Scan a specific file or directory
    python scripts/check-unspecced-sdk-mocks.py tests/agent/test_foo.py

    # Scan only modified files vs. main
    python scripts/check-unspecced-sdk-mocks.py --diff main

Exit status:
    0 — no unspecced SDK-boundary mocks found in scanned files (or all
        suppressed)
    1 — at least one unsuppressed match

NOTE on --all: the existing suite has ~5,300 unspecced mocks accumulated
before this checker existed. Running --all is a baseline/reporting tool,
not a merge gate — CI only runs this against staged/diffed files (see
lint.yml), so only NEW unspecced SDK mocks block a PR. Use --all locally to
pick the next file to migrate (see conftest.py's spec_anthropic_client /
spec_async_anthropic_client fixtures for a ready-made Anthropic client
fixture that handles the .messages/.beta cached_property wrinkle).

Suppress an intentional use with:
    fake_client = MagicMock()  # mock-spec: ok — not a real SDK client shape
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

SUPPRESS_MARKER = re.compile(r"#\s*mock-spec\s*:\s*ok\b", re.IGNORECASE)

# Dirs we never scan.
EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "site-packages",
    "optional-skills",
}

# Only scan the tests/ tree — that's where these mocks live. Running this
# against production code would be a no-op (no matches) but wastes time.
SCAN_ROOT = REPO_ROOT / "tests"

EXCLUDED_FILES = {
    "scripts/check-unspecced-sdk-mocks.py",
}

# Variable-name hints: assignments like `client = MagicMock()`,
# `fake_client = MagicMock()`, `mock_anthropic_client = Mock()`.
# Deliberately narrow — broad names like `mock`/`m` are too noisy (most are
# NOT SDK clients; plain business-object doubles vastly outnumber SDK
# clients in this suite and forcing spec= on all of them is not this
# checker's job).
CLIENT_VAR_HINTS = re.compile(
    r"\b(?:"
    r"\w*anthropic\w*_client\w*"
    r"|\w*openai\w*_client\w*"
    r"|fake_client"
    r"|mock_client"
    r"|\w*_client"
    r")\s*=\s*(?:Magic)?Mock\(\)",
    re.IGNORECASE,
)

# Known SDK-shaped attribute chains that, if set on an unspecced mock a few
# lines after a bare (Magic)Mock() assignment to the same name, indicate
# we're building a fake SDK client. This is what actually distinguishes
# "SDK client double" from "generic business object double" — the same
# heuristic used for the manual audit this checker codifies.
SDK_SHAPE_HINTS = (
    ".messages.stream",
    ".messages.create",
    ".beta.messages",
    ".chat.completions.create",
    ".chat.completions",
)


def should_scan_file(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDED_DIRS:
        return False
    if path.suffix != ".py":
        return False
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return True
    return rel not in EXCLUDED_FILES


def iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        if p.is_file():
            if should_scan_file(p):
                yield p
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
                for fname in files:
                    fpath = Path(root) / fname
                    if should_scan_file(fpath):
                        yield fpath


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(line_number, matched_line)] for unspecced SDK-client mocks.

    Heuristic, two-pass:
      1. Find lines assigning `<name-matching-CLIENT_VAR_HINTS> = (Magic)Mock()`
         with no `spec=`/`autospec=` on that same line.
      2. Confirm the variable is actually used with an SDK-shaped attribute
         chain SOMEWHERE LATER in the file (not just any mock named
         "client" — e.g. a plain business-logic fake named `db_client`
         never touching `.messages.stream` is not this checker's concern).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()

    matches: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        if SUPPRESS_MARKER.search(line):
            continue
        if "spec=" in line or "autospec" in line:
            continue
        m = CLIENT_VAR_HINTS.search(line)
        if not m:
            continue
        # Extract the assigned variable name.
        var_match = re.match(r"\s*(\w+)\s*=", line)
        if not var_match:
            continue
        varname = var_match.group(1)
        # Confirm SDK shape usage somewhere in the rest of the file,
        # referencing this exact variable name.
        rest = "\n".join(lines[i:])
        shape_pattern = re.compile(
            rf"\b{re.escape(varname)}\b(?:{'|'.join(re.escape(s) for s in SDK_SHAPE_HINTS)})"
        )
        if not shape_pattern.search(rest):
            continue
        matches.append((i, line.rstrip()))
    return matches


def get_staged_files() -> list[Path]:
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [REPO_ROOT / f for f in out.splitlines() if f.strip() and f.startswith("tests/")]


def get_diff_files(ref: str) -> list[Path]:
    try:
        out = subprocess.check_output(
            ["git", "diff", f"{ref}...HEAD", "--name-only", "--diff-filter=ACMR"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [REPO_ROOT / f for f in out.splitlines() if f.strip() and f.startswith("tests/")]


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Flag unspecced Mock()/MagicMock() at SDK client boundaries in tests/."
    )
    p.add_argument("paths", nargs="*", type=Path, help="Specific files/dirs to scan.")
    p.add_argument("--all", action="store_true", help="Scan the full tests/ tree (baseline audit, noisy).")
    p.add_argument("--diff", metavar="REF", help="Scan tests/ files changed vs. the given git ref.")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args(argv)

    if args.all:
        roots = [SCAN_ROOT]
    elif args.diff:
        roots = get_diff_files(args.diff)
    elif args.paths:
        roots = [p.resolve() for p in args.paths]
    else:
        roots = get_staged_files()
        if not roots:
            print(
                "No staged tests/ files to scan. Pass --all for a full-suite "
                "baseline audit, --diff <ref> for a range diff, or paths "
                "explicitly.",
                file=sys.stderr,
            )
            return 0

    total_matches = 0
    files_scanned = 0
    for path in iter_files(roots):
        files_scanned += 1
        matches = scan_file(path)
        for lineno, line in matches:
            rel = path.relative_to(REPO_ROOT).as_posix()
            print(f"{rel}:{lineno}: unspecced SDK-client mock")
            print(f"    {line.strip()}")
            print(
                "    — this variable is later used with an SDK-shaped "
                "attribute chain (.messages.stream, .beta.messages, "
                ".chat.completions.create, ...) but was built with a bare "
                "Mock()/MagicMock(), which auto-vivifies ANY attribute as a "
                "truthy sub-mock. If production code patches/calls a "
                "different branch than the one this test configures, the "
                "test will silently pass while testing nothing."
            )
            print(
                "    Fix: use create_autospec(RealClientClass, "
                "instance=True) — for anthropic.Anthropic/AsyncAnthropic "
                "specifically, use the spec_anthropic_client / "
                "spec_async_anthropic_client fixtures in tests/conftest.py "
                "(they handle the .messages/.beta cached_property wrinkle "
                "create_autospec alone doesn't see through)."
            )
            print()
            total_matches += 1

    if total_matches:
        print(
            f"\n✗ {total_matches} unspecced SDK-client mock(s) found across "
            f"{files_scanned} file(s) scanned.",
            file=sys.stderr,
        )
        print(
            "  If a match is a false positive, suppress it with "
            "`# mock-spec: ok` on the same line.",
            file=sys.stderr,
        )
        return 1

    print(f"✓ No unspecced SDK-client mocks found ({files_scanned} file(s) scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
