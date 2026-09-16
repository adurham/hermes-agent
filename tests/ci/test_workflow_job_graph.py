"""Every job reads only from dependencies it declares in `needs`.

A job can reach another job's result solely through `needs.<job>` — so a name
read in an `if:` or any `${{ }}` expression must also appear in that job's own
`needs` list, and can never be the job itself. A self-reference is the silent
form of this bug: `needs: [validate, build-win32]` on `build-win32` reads as an
ordinary dependency to a reviewer while GitHub rejects the whole workflow as a
cycle, so the pipeline dies before any job starts.
"""
import re
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
JOB_REFERENCE = re.compile(r"\bneeds\.([A-Za-z0-9_-]+)")


def _loaded():
    yaml = YAML(typ="base")
    return {
        path.name: yaml.load(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    }


def _referenced_jobs(node, key=None):
    """Job names this job reads through `needs.` — the `if:` and every expression."""
    if isinstance(node, dict):
        for sub_key, value in node.items():
            yield from _referenced_jobs(value, str(sub_key))
    elif isinstance(node, list):
        for value in node:
            yield from _referenced_jobs(value, key)
    elif isinstance(node, str):
        # `run:` scripts may contain their own `needs.items()` — expressions only.
        yield from JOB_REFERENCE.findall(node) if key == "if" else ()
        for expression in EXPRESSION.finditer(node):
            yield from JOB_REFERENCE.findall(expression.group(1))


def test_jobs_only_read_declared_dependencies():
    violations = []
    for filename, workflow in _loaded().items():
        jobs = (workflow or {}).get("jobs") or {}
        for name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            declared = job.get("needs") or []
            if isinstance(declared, str):
                declared = [declared]
            if name in declared:
                violations.append(f"{filename}: {name} needs itself")
            for read in sorted(set(_referenced_jobs(job))):
                if read not in jobs:
                    violations.append(f"{filename}: {name} reads unknown job '{read}'")
                elif read not in declared:
                    violations.append(f"{filename}: {name} reads '{read}' without needing it")
    assert not violations, "workflow job graph:\n" + "\n".join(violations)
