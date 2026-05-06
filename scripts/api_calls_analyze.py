#!/usr/bin/env python3
"""Analyze the api_calls telemetry table to find anomalously slow turns.

Hermes' state.db tracks per-API-call response telemetry: cache split,
latency, request_id, model, etc. This script slices that data three ways
to make "why was THAT call slow?" answerable from the data:

  1. Latency distribution buckets — is it bimodal? Are there real outliers?
  2. Outliers (calls > N stddev above mean) — with full row detail
  3. Latency vs cache state — proves whether slow turns are cold-prefill
     or something else

Usage:
  scripts/api_calls_analyze.py
  scripts/api_calls_analyze.py --session 20260506_142504_be2c53
  scripts/api_calls_analyze.py --since 2026-05-06
  scripts/api_calls_analyze.py --outliers 2.0   # >= 2 stddev = outlier
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional


_DEFAULT_DB = Path.home() / ".hermes" / "state.db"


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"state.db not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _filter_clauses(session: Optional[str], since: Optional[float]) -> tuple[str, list]:
    clauses, params = [], []
    if session:
        clauses.append("session_id = ?")
        params.append(session)
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def buckets(conn, where, params):
    sql = f"""
        SELECT
          CASE
            WHEN latency_seconds < 5   THEN '0-5s'
            WHEN latency_seconds < 10  THEN '5-10s'
            WHEN latency_seconds < 30  THEN '10-30s'
            WHEN latency_seconds < 60  THEN '30-60s'
            WHEN latency_seconds < 120 THEN '60-120s'
            WHEN latency_seconds < 300 THEN '120-300s'
            ELSE '300s+'
          END AS bucket,
          COUNT(*) AS n,
          ROUND(AVG(input_tokens),0) AS avg_fresh,
          ROUND(AVG(cache_read_tokens),0) AS avg_cache_r,
          ROUND(AVG(cache_write_tokens),0) AS avg_cache_w,
          ROUND(AVG(output_tokens),0) AS avg_out
        FROM api_calls
        {where}
        GROUP BY bucket
        ORDER BY MIN(latency_seconds)
    """
    rows = conn.execute(sql, params).fetchall()
    print("\n== Latency distribution ==")
    print(f"  {'bucket':<10} {'n':>4}  {'fresh':>8} {'cache_r':>10} {'cache_w':>8} {'out':>6}")
    for r in rows:
        print(
            f"  {r['bucket']:<10} {r['n']:>4}  "
            f"{int(r['avg_fresh'] or 0):>8,} {int(r['avg_cache_r'] or 0):>10,} "
            f"{int(r['avg_cache_w'] or 0):>8,} {int(r['avg_out'] or 0):>6,}"
        )


def outliers(conn, where, params, k: float):
    rows = conn.execute(
        f"SELECT latency_seconds FROM api_calls {where}", params
    ).fetchall()
    if len(rows) < 5:
        print(f"\n== Outliers (>= {k} stddev) ==\n  not enough samples ({len(rows)})")
        return
    lats = [r["latency_seconds"] for r in rows]
    mu = statistics.mean(lats)
    sd = statistics.pstdev(lats)
    threshold = mu + k * sd
    sql = f"""
        SELECT
          session_id, call_seq,
          datetime(started_at,'unixepoch','localtime') AS req_started,
          ROUND(latency_seconds,1) AS sec,
          input_tokens AS fresh,
          cache_read_tokens AS cache_r,
          cache_write_tokens AS cache_w,
          output_tokens AS out_t,
          request_id, stop_reason, call_type
        FROM api_calls
        {where}
        {('AND' if where else 'WHERE')} latency_seconds >= ?
        ORDER BY latency_seconds DESC
    """
    out_rows = conn.execute(sql, params + [threshold]).fetchall()
    print(
        f"\n== Outliers (latency >= {threshold:.1f}s = mean {mu:.1f} + {k}σ {sd:.1f}) =="
    )
    if not out_rows:
        print("  none")
        return
    for r in out_rows:
        rid = r["request_id"] or "(no request_id)"
        print(
            f"  {r['req_started']}  session={r['session_id'][-12:]}  "
            f"call={r['call_seq']:>3}  {r['sec']:>6.1f}s  "
            f"fresh={int(r['fresh'] or 0):>5,} "
            f"cache_r={int(r['cache_r'] or 0):>7,} "
            f"cache_w={int(r['cache_w'] or 0):>5,} "
            f"out={int(r['out_t'] or 0):>5,}  "
            f"req={rid}  stop={r['stop_reason']}"
        )


def cache_state_signal(conn, where, params):
    """Cluster slow calls by what cache state they were in. The point: prove
    whether slow latency correlates with cache_write spikes (cold prefill)
    or NOT (server-side queue / model thinking)."""
    sql = f"""
        SELECT
          CASE
            WHEN cache_read_tokens = 0 AND cache_write_tokens > 0 THEN 'cold prefill'
            WHEN cache_read_tokens > 0 AND cache_write_tokens > cache_read_tokens / 4 THEN 'partial-rebuild'
            WHEN cache_read_tokens > 0 AND cache_write_tokens > 0 THEN 'cache-hit-with-delta'
            WHEN cache_read_tokens > 0 AND cache_write_tokens = 0 THEN 'pure-cache-hit'
            ELSE 'no-cache'
          END AS cache_state,
          COUNT(*) AS n,
          ROUND(AVG(latency_seconds),1) AS avg_sec,
          ROUND(MAX(latency_seconds),1) AS peak_sec
        FROM api_calls
        {where}
        GROUP BY cache_state
        ORDER BY peak_sec DESC
    """
    rows = conn.execute(sql, params).fetchall()
    print("\n== Latency vs cache state ==")
    print(f"  {'cache state':<22} {'n':>4} {'avg':>7} {'peak':>7}")
    for r in rows:
        print(f"  {r['cache_state']:<22} {r['n']:>4} {r['avg_sec']:>6.1f}s {r['peak_sec']:>6.1f}s")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=Path, default=_DEFAULT_DB)
    p.add_argument("--session")
    p.add_argument("--since", help="ISO date floor")
    p.add_argument("--outliers", type=float, default=2.0,
                   help="stddev multiplier for outlier detection (default 2.0)")
    args = p.parse_args()

    since_epoch = None
    if args.since:
        try:
            since_epoch = datetime.fromisoformat(args.since).timestamp()
        except ValueError:
            sys.exit(f"--since: not ISO: {args.since!r}")

    conn = _open(args.db)
    where, params = _filter_clauses(args.session, since_epoch)
    buckets(conn, where, params)
    cache_state_signal(conn, where, params)
    outliers(conn, where, params, args.outliers)


if __name__ == "__main__":
    raise SystemExit(main())
