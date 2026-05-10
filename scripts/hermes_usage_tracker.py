#!/usr/bin/env python3
"""Periodic usage tracker for the personal Anthropic OAuth account.

Run mode (--append): appends one CSV row to ~/.hermes/usage.csv with
account-wide utilization (from the OAuth /api/oauth/usage endpoint)
plus bot-side per-window token totals fetched over SSH from the
hermes gateway LXC. Designed to be invoked by launchd every 5 min.

Snapshot mode (--summary): reads the CSV and prints the current state
plus deltas over the last 1h and 24h. Useful for spotting anomalies
("the bot just consumed 8% of session budget in 10 min" = problem).

The script uses the hermes-agent fork's account_usage module so the
auth path matches what the laptop's `hermes status` would report.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Anchor the import path on the hermes-agent fork so we can reuse
# `_fetch_anthropic_account_usage` (handles OAuth token resolution +
# response parsing) without copy-pasting it.
HERMES_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERMES_REPO))

from agent.account_usage import _fetch_anthropic_account_usage  # noqa: E402


CSV_PATH = Path.home() / ".hermes" / "usage.csv"
LXC_SSH = os.environ.get("HERMES_GW_SSH", "root@172.16.0.50")

# Opus 4.7 list pricing per Mtok. Cache write 1.25x input, cache read 0.10x.
PRICE_INPUT  = 15.00
PRICE_OUTPUT = 75.00
PRICE_CW     = 18.75
PRICE_CR     = 1.50


CSV_FIELDS = [
    "ts_utc",
    "session_pct",        # 5h rolling, from OAuth usage API
    "session_resets_at",
    "week_pct",
    "opus_week_pct",
    "sonnet_week_pct",
    "extra_used_credits",  # USD
    # Bot-side, 5h rolling window matching session reset
    "bot_turns_5h",
    "bot_input_5h",
    "bot_cw_5h",
    "bot_cr_5h",
    "bot_output_5h",
    "bot_est_cost_5h",
]


def _bot_tokens_5h() -> dict:
    """Aggregate bot-side token usage over the last 5 hours.

    Runs a small python snippet over SSH on the LXC. Returns zeros if
    the LXC is unreachable or session dir is empty — this script's job
    is best-effort observability, not gating.
    """
    snippet = r"""
import json, glob, os, datetime, sys
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
ti = to = cr = cw = 0
turns = 0
for path in sorted(glob.glob('/home/hermes/.hermes/sessions/session_*.json')):
    try: d = json.load(open(path))
    except: continue
    for u in d.get('usage_history') or []:
        try:
            ts = datetime.datetime.fromisoformat(u.get('ts',''))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        if ts < cutoff:
            continue
        ti += u.get('input', 0) or 0
        to += u.get('output', 0) or 0
        cr += u.get('cache_read', 0) or 0
        cw += u.get('cache_write', 0) or 0
        turns += 1
print(json.dumps({'turns': turns, 'input': ti, 'output': to, 'cw': cw, 'cr': cr}))
"""
    # Pipe via stdin (python -) — passing a multi-line script as -c argv
    # gets mangled by the remote shell (each line becomes its own command).
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", LXC_SSH,
             "/opt/hermes-agent/venv/bin/python", "-"],
            input=snippet, capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return {"turns": 0, "input": 0, "output": 0, "cw": 0, "cr": 0}
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"turns": 0, "input": 0, "output": 0, "cw": 0, "cr": 0}


def _est_cost(input_tok: int, cw: int, cr: int, output: int) -> float:
    return (
        input_tok * PRICE_INPUT  / 1_000_000
        + cw      * PRICE_CW     / 1_000_000
        + cr      * PRICE_CR     / 1_000_000
        + output  * PRICE_OUTPUT / 1_000_000
    )


def _row_for_now() -> dict:
    snap = _fetch_anthropic_account_usage()
    by_label = {w.label: w for w in (snap.windows if snap else ())}
    extra_used = ""
    for d in snap.details if snap else ():
        if d.startswith("Extra usage:"):
            try:
                extra_used = d.split(":", 1)[1].strip().split("/", 1)[0].strip().rstrip("USD").strip()
            except Exception:
                pass
    bot = _bot_tokens_5h()
    cost = _est_cost(bot["input"], bot["cw"], bot["cr"], bot["output"])

    sess = by_label.get("Current session")
    week = by_label.get("Current week")
    opus = by_label.get("Opus week")
    son  = by_label.get("Sonnet week")

    return {
        "ts_utc":             datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_pct":        f"{sess.used_percent:.2f}" if sess else "",
        "session_resets_at":  sess.reset_at.isoformat() if (sess and sess.reset_at) else "",
        "week_pct":           f"{week.used_percent:.2f}" if week else "",
        "opus_week_pct":      f"{opus.used_percent:.2f}" if opus else "",
        "sonnet_week_pct":    f"{son.used_percent:.2f}" if son else "",
        "extra_used_credits": extra_used,
        "bot_turns_5h":       bot["turns"],
        "bot_input_5h":       bot["input"],
        "bot_cw_5h":          bot["cw"],
        "bot_cr_5h":          bot["cr"],
        "bot_output_5h":      bot["output"],
        "bot_est_cost_5h":    f"{cost:.4f}",
    }


def _append(row: dict) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def _read_csv() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, newline="") as f:
        return list(csv.DictReader(f))


def _delta(rows: list[dict], window: timedelta) -> dict | None:
    """Return the delta between the latest row and the row closest to
    ``window`` in the past. Returns None if we lack data."""
    if not rows:
        return None
    latest = rows[-1]
    try:
        now = datetime.fromisoformat(latest["ts_utc"])
    except Exception:
        return None
    target = now - window
    # Walk backwards to find first row at or before target
    earlier = None
    for r in reversed(rows[:-1]):
        try:
            ts = datetime.fromisoformat(r["ts_utc"])
        except Exception:
            continue
        if ts <= target:
            earlier = r
            break
    if earlier is None:
        return None

    def _f(r, k):
        try: return float(r.get(k) or 0)
        except: return 0.0
    def _i(r, k):
        try: return int(r.get(k) or 0)
        except: return 0

    return {
        "since":            earlier["ts_utc"],
        "session_pct_d":    _f(latest, "session_pct") - _f(earlier, "session_pct"),
        "week_pct_d":       _f(latest, "week_pct") - _f(earlier, "week_pct"),
        "opus_week_pct_d":  _f(latest, "opus_week_pct") - _f(earlier, "opus_week_pct"),
        "extra_credits_d":  _f(latest, "extra_used_credits") - _f(earlier, "extra_used_credits"),
        "bot_turns_d":      _i(latest, "bot_turns_5h") - _i(earlier, "bot_turns_5h"),
        "bot_cost_d":       _f(latest, "bot_est_cost_5h") - _f(earlier, "bot_est_cost_5h"),
    }


def cmd_summary() -> int:
    rows = _read_csv()
    if not rows:
        print(f"No data yet at {CSV_PATH}. Run with --append first.")
        return 0
    latest = rows[-1]
    print(f"Latest snapshot ({latest['ts_utc']}):")
    print(f"  Current session:  {latest['session_pct']:>6}%   resets {latest['session_resets_at']}")
    print(f"  Current week:     {latest['week_pct']:>6}%")
    print(f"  Opus week:        {latest['opus_week_pct']:>6}%")
    print(f"  Sonnet week:      {latest['sonnet_week_pct']:>6}%")
    print(f"  Extra used:       ${latest['extra_used_credits']}")
    print(f"  Bot 5h:           {latest['bot_turns_5h']} turns, "
          f"in={latest['bot_input_5h']} cw={latest['bot_cw_5h']} "
          f"cr={latest['bot_cr_5h']} out={latest['bot_output_5h']} "
          f"cost=${latest['bot_est_cost_5h']}")
    for label, window in [("1h", timedelta(hours=1)), ("24h", timedelta(hours=24))]:
        d = _delta(rows, window)
        if not d:
            print(f"  {label} delta:        (insufficient history)")
            continue
        print(f"  {label} delta (since {d['since']}):")
        print(f"    session: +{d['session_pct_d']:+.2f}%   week: +{d['week_pct_d']:+.2f}%   "
              f"opus_week: +{d['opus_week_pct_d']:+.2f}%   extra: +${d['extra_credits_d']:.2f}")
        print(f"    bot: +{d['bot_turns_d']} turns, +${d['bot_cost_d']:.4f}")
    return 0


def cmd_append() -> int:
    row = _row_for_now()
    _append(row)
    print(f"Appended {row['ts_utc']} session={row['session_pct']}% "
          f"week={row['week_pct']}% bot_5h={row['bot_turns_5h']}t/${row['bot_est_cost_5h']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("append", help="Poll API + LXC, append one CSV row")
    sub.add_parser("summary", help="Print latest snapshot + deltas")
    args = p.parse_args()
    if args.cmd == "append":
        return cmd_append()
    if args.cmd == "summary":
        return cmd_summary()
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
