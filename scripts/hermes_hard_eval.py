#!/usr/bin/env python3
"""Run exo/bench/hard_eval.py's task suite THROUGH hermes-agent's real request path.

Motivation (2026-07-26): the original exo-vs-Ollama DSv4 comparison (90.9 vs 89.4)
was done with bare API calls, bypassing hermes entirely — no system prompt, no
reasoning-timeout/thinking-budget handling, no tool overhead. Adam uses both
providers through hermes daily, so this re-runs the same tasks/graders via
hermes's own plumbing.

Two modes (both reuse hard_eval.py's TASKS + graders verbatim):

  --mode cli   (default) Full suite via `hermes -z` subprocess per trial.
               This is the REAL daily-driver path: hermes system prompt,
               rules/memory/AGENTS.md, configured cli toolsets, provider
               resolution, internal retries/timeouts, budget handling.
               Only final text is observable (no reasoning/finish_reason —
               that's inherent to the oneshot surface), so truncation
               detection is "empty output" rather than finish_reason==length.

  --mode probe Raw-shape probe via agent.auxiliary_client.call_llm with
               provider=custom:exo / custom:ollama. Same single-user-message
               request the bare-API harness sent (temp 0.0, max_tokens 8192,
               timeout 600) but built/sent/retried by hermes's own client
               code. Captures content + reasoning + finish_reason so the
               original truncation question is answerable. Applies the
               reasoning_content-OR-reasoning fallback fix (Ollama's OpenAI
               compat layer returns `reasoning`, not `reasoning_content` —
               upstream gap ollama/ollama#16184).

Must run under the installed hermes venv python:
  ~/.hermes/hermes-agent/venv/bin/python scripts/hermes_hard_eval.py ...

Results are appended incrementally as JSONL (resumable: existing
provider/task/trial keys are skipped on rerun).

NOTE: deliberately untracked — do not commit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERMES_INSTALL = Path.home() / ".hermes" / "hermes-agent"
# Overridable so a fix in the REPO tree can be live-verified without touching
# the production install, e.g.:
#   HERMES_HARD_EVAL_BIN=~/repos/hermes-agent/scripts/lib/repo-hermes ...
# where repo-hermes is: PYTHONPATH=~/repos/hermes-agent exec
#   ~/.hermes/hermes-agent/venv/bin/python -c 'from hermes_cli.main import main; main()' "$@"
HERMES_BIN = os.path.expanduser(
    os.environ.get("HERMES_HARD_EVAL_BIN", str(Path.home() / ".local" / "bin" / "hermes"))
)
EXO_BENCH = Path.home() / "repos" / "exo" / "bench"

sys.path.insert(0, str(EXO_BENCH))
import hard_eval as he  # noqa: E402

PROVIDERS = {
    # cli_provider: what `hermes -z --provider` gets.
    # probe_provider: explicit custom:<name> so resolution can only hit the
    #   config.yaml providers block (bare "ollama" aliases to local in some paths).
    "exo": {
        "cli_provider": "exo",
        "probe_provider": "custom:exo",
        "model": "mlx-community/DeepSeek-V4-Flash",
    },
    "ollama": {
        "cli_provider": "ollama",
        "probe_provider": "custom:ollama",
        "model": "deepseek-v4-flash:cloud",
    },
}

CLI_SUBPROCESS_TIMEOUT = 1500.0  # generous: an agent turn may span several model calls
PROBE_MAX_TOKENS = 8192          # hard_eval.py defaults
PROBE_TEMPERATURE = 0.0
PROBE_TIMEOUT = 600.0


def _load_done(jsonl_path: Path) -> set[tuple[str, str, str, int]]:
    done = set()
    if jsonl_path.exists():
        for line in jsonl_path.read_text().splitlines():
            try:
                r = json.loads(line)
                done.add((r["mode"], r["provider"], r["task_id"], r["trial"]))
            except Exception:
                continue
    return done


_write_lock = threading.Lock()


def _append(jsonl_path: Path, rec: dict) -> None:
    with _write_lock:
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------- CLI mode
def run_cli_trial(pname: str, task, trial: int, workdir: Path) -> dict:
    cfg = PROVIDERS[pname]
    trial_dir = workdir / f"{pname}_{task.task_id}_t{trial}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    usage_file = trial_dir / "usage.json"

    env = dict(os.environ)
    env.pop("HERMES_INFERENCE_MODEL", None)
    env.pop("HERMES_INFERENCE_PROVIDER", None)

    cmd = [
        HERMES_BIN, "-z", task.prompt,
        "--provider", cfg["cli_provider"],
        "-m", cfg["model"],
        "--usage-file", str(usage_file),
    ]
    t0 = time.time()
    error = ""
    content = ""
    exit_code: int | None = None
    try:
        proc = subprocess.run(
            cmd, cwd=str(trial_dir), env=env,
            capture_output=True, text=True, timeout=CLI_SUBPROCESS_TIMEOUT,
        )
        exit_code = proc.returncode
        content = proc.stdout or ""
        if proc.returncode != 0:
            error = (proc.stderr or "")[-500:]
    except subprocess.TimeoutExpired:
        error = f"subprocess timeout after {CLI_SUBPROCESS_TIMEOUT}s"
    latency_s = time.time() - t0

    usage = {}
    try:
        usage = json.loads(usage_file.read_text())
    except Exception:
        pass

    # No finish_reason/reasoning on the oneshot surface: pass empty strings.
    scored = he.score_trial(task, content, "", "")
    scored.update({
        "mode": "cli", "provider": pname, "trial": trial,
        "latency_s": round(latency_s, 1), "exit_code": exit_code,
        "error": error,
        "routed_provider": usage.get("provider"),
        "routed_model": usage.get("model"),
        "api_calls": usage.get("api_calls"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "content": content[-4000:],
    })
    return scored


# -------------------------------------------------------------- probe mode
def run_probe_trial(pname: str, task, trial: int) -> dict:
    from agent.auxiliary_client import call_llm

    cfg = PROVIDERS[pname]
    t0 = time.time()
    content = reasoning = finish_reason = ""
    error = ""
    try:
        resp = call_llm(
            provider=cfg["probe_provider"],
            model=cfg["model"],
            messages=[{"role": "user", "content": task.prompt}],
            temperature=PROBE_TEMPERATURE,
            max_tokens=PROBE_MAX_TOKENS,
            timeout=PROBE_TIMEOUT,
        )
        choice = resp.choices[0]
        msg = choice.message
        content = getattr(msg, "content", None) or ""
        # THE FIX: hard_eval.py only read reasoning_content; Ollama's OpenAI
        # compat layer emits `reasoning` instead.
        reasoning = (
            getattr(msg, "reasoning_content", None)
            or getattr(msg, "reasoning", None)
            or ""
        )
        finish_reason = getattr(choice, "finish_reason", None) or ""
    except Exception as exc:  # noqa: BLE001 — record, don't retry beyond hermes's own
        error = f"{type(exc).__name__}: {exc}"[:500]
    latency_s = time.time() - t0

    scored = he.score_trial(task, content, reasoning, finish_reason)
    scored.update({
        "mode": "probe", "provider": pname, "trial": trial,
        "latency_s": round(latency_s, 1), "error": error,
        "content": content[-4000:],
        "reasoning_tail": reasoning[-1500:],
    })
    return scored


# ---------------------------------------------------------------- reporting
def report(jsonl_path: Path, mode: str) -> None:
    rows = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["mode"] == mode]
    for pname in sorted({r["provider"] for r in rows}):
        prows = [r for r in rows if r["provider"] == pname]
        results: dict[str, list[dict]] = {}
        for r in prows:
            results.setdefault(r["task_id"], []).append(r)
        print(f"\n############ mode={mode} provider={pname} "
              f"model={PROVIDERS[pname]['model']} ############")
        he.print_report(results, PROVIDERS[pname]["model"], f"hermes:{pname}")
        # extra: break longctx_* out of "math" to match the requested framing
        for label, pred in (("math(core)", lambda t: t.startswith("math_")),
                            ("longcontext", lambda t: t.startswith("longctx_"))):
            vals = [r["pass"] for r in prows if pred(r["task_id"])]
            if vals:
                print(f"  {label:<12} {100.0 * sum(vals) / len(vals):5.1f}%  (n={len(vals)})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["cli", "probe"], default="cli")
    ap.add_argument("--providers", default="exo,ollama")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--tasks", default="all", help="csv of task ids, 'all', or 'math' (math_*+longctx_*)")
    ap.add_argument("--out", required=True, help="JSONL results path (appended, resumable)")
    ap.add_argument("--workdir", default="", help="scratch dir for cli-mode trial cwds")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    jsonl_path = Path(args.out).expanduser()
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        report(jsonl_path, args.mode)
        return 0

    if args.tasks == "all":
        tasks = list(he.TASKS)
    elif args.tasks == "math":
        tasks = [t for t in he.TASKS if t.task_id.startswith(("math_", "longctx_"))]
    else:
        wanted = {s.strip() for s in args.tasks.split(",") if s.strip()}
        tasks = [t for t in he.TASKS if t.task_id in wanted]
    if not tasks:
        print("no tasks matched", file=sys.stderr)
        return 2

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    for p in providers:
        if p not in PROVIDERS:
            print(f"unknown provider {p}", file=sys.stderr)
            return 2

    workdir = Path(args.workdir or (jsonl_path.parent / "cli_trials")).expanduser()
    done = _load_done(jsonl_path)

    def worker(pname: str) -> None:
        for task in tasks:
            for trial in range(1, args.trials + 1):
                key = (args.mode, pname, task.task_id, trial)
                if key in done:
                    continue
                t0 = time.strftime("%H:%M:%S")
                if args.mode == "cli":
                    rec = run_cli_trial(pname, task, trial, workdir)
                else:
                    rec = run_probe_trial(pname, task, trial)
                _append(jsonl_path, rec)
                print(f"[{t0}->{time.strftime('%H:%M:%S')}] {pname} {task.task_id} "
                      f"t{trial} pass={rec['pass']} ({rec.get('snippet','')[:40]})"
                      f"{' ERR:' + rec['error'][:60] if rec.get('error') else ''}",
                      flush=True)

    threads = [threading.Thread(target=worker, args=(p,), name=p) for p in providers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    report(jsonl_path, args.mode)
    return 0


if __name__ == "__main__":
    # probe mode imports hermes agent modules from the installed tree
    sys.path.insert(0, str(HERMES_INSTALL))
    os.chdir(str(HERMES_INSTALL))
    # Load ~/.hermes/.env exactly like the hermes CLI entrypoint does, so
    # key_env references (e.g. OLLAMA_API_KEY) resolve in probe mode.
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
    raise SystemExit(main())
