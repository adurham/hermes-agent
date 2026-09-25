#!/usr/bin/env python3
"""Audit and clean up aged ``hermes update`` autostash orphans in a git checkout.

`hermes update` parks a ``hermes-update-autostash-<stamp>`` entry whenever the tree is dirty
(``--keep-stash`` parks it deliberately; a non-interactive update parks it when the restore is
declined). Nothing in the product ever drops one -- it only prints a >7-day notice -- so cleanup
is manual. This script does it in the safe order:

  1. triage   -- autostash entries older than --keep-days (stamp from the subject, else commit time)
  2. audit    -- for every touched path (tracked AND untracked), the entry's blob must be provably
                 contained in HEAD or in some reachable commit. "The file changed since" is NOT
                 evidence: a stash holds whole-file blobs. Uncontained blobs are recoverable work
                 and are NEVER dropped (reported and kept; exit code 2).
  3. archive  -- before dropping, bundle every affected stash commit (temp refs -> bundle -> verify
                 -> manifest json -> temp refs removed); dropped commits stay unreachable but
                 unpruned, so the bundle is a second copy.
  4. drop     -- by INDEX, highest first, re-resolving the list each iteration
                 (`git stash drop <sha>` errors: "is not a stash reference").
  5. verify   -- HEAD unchanged, expected remainder, archived SHAs still resolvable, and 0 entries
                 would still warn on the next `hermes update`.

Read-only unless --apply is passed. Stdlib only; run with any python3 >= 3.8.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

PREFIX = "hermes-update-autostash-"
STAMP_RE = re.compile(r"stash@\{(\d+)\}")


def git(*args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def rev_parse(cwd, rev):
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", rev], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() or None


def entries(cwd):
    out = git("stash", "list", "--format=%gd|%H|%ct|%s", cwd=cwd)
    res = []
    for line in out.splitlines():
        sel, sha, ct, subj = line.split("|", 3)
        idx = STAMP_RE.match(sel)
        if not idx:
            continue
        res.append({
            "selector": sel,
            "idx": int(idx.group(1)),
            "sha": sha,
            "commit_time": datetime.datetime.fromtimestamp(int(ct), datetime.timezone.utc),
            "subject": subj,
        })
    return res


def stamp_of(subject):
    """UTC time from the entry's own ``hermes-update-autostash-YYYYmmdd-HHMMSS`` name, else None."""
    pos = subject.find(PREFIX)
    if pos < 0:
        return None
    raw = subject[pos + len(PREFIX):][:15]
    try:
        return datetime.datetime.strptime(raw, "%Y%m%d-%H%M%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def touched_paths(cwd, sel):
    tracked = [p for p in git("diff", "--name-only", f"{sel}^1", sel, cwd=cwd).splitlines() if p]
    untracked = []
    if rev_parse(cwd, f"{sel}^3"):
        untracked = [p for p in git("ls-tree", "-r", "--name-only", f"{sel}^3", cwd=cwd).splitlines() if p]
    return tracked, untracked


def blob_in_history(cwd, path, blob, memo):
    """True when some commit reachable outside the stash refs holds exactly *blob* at *path*."""
    if path not in memo:
        revs = git("rev-list", "--all", "^refs/stash", "--", path, cwd=cwd).splitlines()
        memo[path] = {rev_parse(cwd, f"{r}:{path}") for r in revs}
    return blob in memo[path]


def audit(cwd, entry, memo, allow_removed=False):
    """(tracked, untracked, uncontained, removed) for one stash entry.

    ``uncontained`` = paths whose stashed blob no commit holds (recoverable work).
    ``removed`` (only collected when *allow_removed*) = a path whose blob no commit holds, is
    ABSENT from HEAD, and WAS TRACKED at the stash's own base commit (``<sel>^1``) - the signature
    of a file deliberately deleted upstream. That last condition is load-bearing: an untracked
    path (from ``<sel>^3``) is absent from HEAD BY DEFINITION, so without it a never-committed
    file of unique work would read as "superseded" and get dropped. Still a judgment call, never
    automatic: reported either way, droppable only under --allow-removed.
    """
    sel = entry["selector"]
    tracked, untracked = touched_paths(cwd, sel)
    uncontained, removed = [], []
    for path, rev in [(p, sel) for p in tracked] + [(p, f"{sel}^3") for p in untracked]:
        blob = rev_parse(cwd, f"{rev}:{path}")
        if blob and blob == rev_parse(cwd, f"HEAD:{path}"):
            continue
        if blob and blob_in_history(cwd, path, blob, memo):
            continue
        was_tracked = rev_parse(cwd, f"{sel}^1:{path}") is not None
        if allow_removed and was_tracked and rev_parse(cwd, f"HEAD:{path}") is None:
            removed.append((path, (blob or "")[:10], rev_parse(cwd, f"{sel}^1") or ""))
            continue
        uncontained.append((path, (blob or "")[:10]))
    return tracked, untracked, uncontained, removed


def archive(cwd, victims, dest_dir):
    """Bundle every victim stash commit via temp refs; write a manifest beside it."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = dest_dir / f"autostash-archive-{stamp}.bundle"
    refs = []
    try:
        for i, e in enumerate(sorted(victims, key=lambda x: x["sha"])):
            ref = f"refs/autostash-archive/{i:02d}"
            git("update-ref", ref, e["sha"], cwd=cwd)
            refs.append(ref)
        git("bundle", "create", str(bundle), *refs, cwd=cwd)
        verify = subprocess.run(["git", "bundle", "verify", str(bundle)], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if "complete history" not in verify.stdout + verify.stderr:
            raise SystemExit(f"bundle verify failed:\n{verify.stdout}{verify.stderr}")
        heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], cwd=cwd,
                               capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        missing = [e["sha"] for e in victims if e["sha"] not in heads]
        if missing:
            raise SystemExit(f"bundle is missing {missing}")
    finally:
        for ref in refs:
            git("update-ref", "-d", ref, cwd=cwd)
    return bundle


def write_manifest(bundle, cwd, records):
    manifest = bundle.with_suffix(".json")
    manifest.write_text(json.dumps({
        "repo": str(cwd),
        "archived_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "bundle": bundle.name,
        "entries": records,
    }, indent=1), encoding="utf-8")
    return manifest


def warn_count(cwd, keep_days):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=keep_days)
    n = 0
    for e in entries(cwd):
        when = stamp_of(e["subject"]) or e["commit_time"]
        if PREFIX in e["subject"] and when < cutoff:
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="checkout to operate on (default: cwd)")
    ap.add_argument("--keep-days", type=int, default=7, help="age threshold in days (default: 7, same as the updater warning)")
    ap.add_argument("--apply", action="store_true", help="archive + drop the audited entries (default: report only)")
    ap.add_argument("--allow-removed", action="store_true",
                    help="also treat entries as superseded when every uncontained path is ABSENT from HEAD "
                         "(superseded-by-removal, e.g. upstream deleted the file). Only after verifying the "
                         "deletion was intentional - the report names each path and its deleting commits.")
    ap.add_argument("--archive-dir", default=str(Path.home() / ".hermes" / "backups"), help="where the archive bundle lands")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args()

    cwd = git("rev-parse", "--show-toplevel", cwd=args.repo) or "."
    head_before = git("rev-parse", "HEAD", cwd=cwd)
    all_entries = entries(cwd)

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.keep_days)
    stale, kept_fresh, manual = [], [], []
    for e in all_entries:
        if PREFIX not in e["subject"]:
            manual.append(e)
            continue
        when = stamp_of(e["subject"]) or e["commit_time"]
        if when < cutoff:
            stale.append(e)
        else:
            kept_fresh.append(e)

    if not stale:
        report = {"repo": cwd, "head": head_before, "drop_candidates": [], "kept_fresh": len(kept_fresh),
                  "kept_uncontained": [], "manual_entries": [e["subject"] for e in manual],
                  "dropped": [], "archive": None, "warn_count_now": warn_count(cwd, args.keep_days)}
        print(json.dumps(report, indent=1) if args.json else
              f"{cwd}\nNo autostash entries older than {args.keep_days} days - nothing to do.\n"
              f"({len(kept_fresh)} younger entr{'y' if len(kept_fresh)==1 else 'ies'} parked; "
              f"{len(manual)} non-autostash entr{'y' if len(manual)==1 else 'ies'} left alone; "
              f"warnings on next update: {report['warn_count_now']})")
        return 0

    memo = {}
    droppable, uncontained_all, removed_all = [], [], []
    for e in stale:
        tracked, untracked, uncontained, removed = audit(cwd, e, memo, allow_removed=args.allow_removed)
        if uncontained:
            uncontained_all.append({"selector": e["selector"], "sha": e["sha"], "subject": e["subject"],
                                    "uncontained": [{"path": p, "blob": b} for p, b in uncontained]})
        if removed:
            removed_all.append({"selector": e["selector"], "sha": e["sha"], "subject": e["subject"],
                                "removed": [{"path": p, "blob": b, "base": base} for p, b, base in removed]})
        if not uncontained:
            droppable.append(e)

    lines = [f"{cwd}", f"HEAD {head_before[:10]} | {len(all_entries)} stash entries | "
             f"{len(stale)} older than {args.keep_days}d"]
    for e in stale:
        tag = "DROP" if any(d["sha"] == e["sha"] for d in droppable) else "KEEP(uncontained)"
        t, u = touched_paths(cwd, e["selector"])
        listing = ", ".join(t + [p + " (untracked)" for p in u]) or "no paths"
        lines.append(f"  [{tag}] {e['selector']}  {e['subject']}  ({listing})")
    for u in uncontained_all:
        lines.append(f"    !! {u['selector']} carries blobs no commit holds - recoverable work, kept:")
        for item in u["uncontained"]:
            lines.append(f"       {item['path']}  blob {item['blob']}   inspect: git show {u['sha']}:{item['path']}")
    for r in removed_all:
        lines.append(f"    ?? {r['selector']} touches paths absent from HEAD - SUPERSEDED-BY-REMOVAL only if that")
        lines.append(f"       deletion was intentional; verify, then re-run with --allow-removed:")
        for item in r["removed"]:
            lines.append(f"       {item['path']}  blob {item['blob']}   inspect: git show {r['sha']}:{item['path']}"
                         f"   (deleted since {item['base'][:10]}; git log --oneline --diff-filter=D -- {item['path']})")

    archive_path = None
    if args.apply and droppable:
        victims = sorted(droppable, key=lambda x: x["sha"])
        bundle = archive(cwd, victims, Path(args.archive_dir))
        records = []
        for e in victims:
            tracked, untracked = touched_paths(cwd, e["selector"])
            records.append({"selector": e["selector"], "sha": e["sha"], "subject": e["subject"],
                            "stamp_utc": (stamp_of(e["subject"]) or e["commit_time"]).isoformat(),
                            "tracked_files": tracked, "untracked_files": untracked})
        write_manifest(bundle, cwd, records)
        archive_path = str(bundle)
        lines.append(f"  archived -> {bundle} (+{bundle.with_suffix('.json').name})")

    dropped = []
    if args.apply:
        while True:
            current = [e for e in entries(cwd) if any(d["sha"] == e["sha"] for d in droppable)]
            if not current:
                break
            victim = max(current, key=lambda e: e["idx"])
            git("stash", "drop", victim["selector"], cwd=cwd)
            dropped.append(victim)

    remaining = entries(cwd)
    head_after = git("rev-parse", "HEAD", cwd=cwd)
    dirty = git("status", "--porcelain", cwd=cwd)
    still_warn = warn_count(cwd, args.keep_days)
    lines.append(f"  dropped {len(dropped)} | remaining {len(remaining)} | HEAD {'unchanged' if head_after == head_before else 'CHANGED!'}"
                 f" | tree {'clean' if not dirty else 'DIRTY'} | would-warn now: {still_warn}")

    ok = head_after == head_before and still_warn == 0 and not (args.apply and any(
        rev_parse(cwd, e["sha"]) is None for e in dropped))
    report = {"repo": cwd, "head": head_before, "drop_candidates": [e["selector"] for e in droppable],
              "kept_uncontained": uncontained_all, "kept_fresh": len(kept_fresh),
              "manual_entries": [e["subject"] for e in manual], "dropped": [e["selector"] for e in dropped],
              "archive": archive_path, "remaining": len(remaining), "warn_count_now": still_warn, "ok": ok}
    print(json.dumps(report, indent=1) if args.json else "\n".join(lines))
    if not args.apply:
        print("(report only - re-run with --apply to archive and drop)")
    return 0 if ok and not uncontained_all else (2 if uncontained_all else 1)


if __name__ == "__main__":
    sys.exit(main())
