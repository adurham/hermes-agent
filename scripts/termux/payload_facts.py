"""Record the staged bionic tools through pm's existing facts contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pm.lock import Facts, Lockfile
from pm.store import tree_digest
from pm.registry import get_package
import pm.packages  # Registers the runtime definitions.


def write_facts(payload: Path, lock_path: Path, build_set: Path) -> None:
    target = "linux-arm64-bionic"
    lock = Lockfile(lock_path)
    store = payload / "tools"
    facts = Facts(store / "facts.json")
    for name in ("python", "node", "uv", "npm", "ffmpeg", "ripgrep"):
        entry = store / name
        if not entry.is_dir():
            raise RuntimeError(f"missing payload tool: {name}")
        version = lock.version(name)
        artifacts = lock.artifacts(name, target)
        if not version or not artifacts:
            raise RuntimeError(f"missing pin: {name} on {target}")
        facts.record(
            name, version, name, get_package(name).env(entry, target), store,
            target=target, artifacts=[a["sha256"] for a in artifacts],
            digest=tree_digest(entry),
        )
    natives = [line.strip() for line in build_set.read_text(encoding="utf-8").splitlines() if line.strip()]
    (payload / "native-wheels.json").write_text(json.dumps(natives) + "\n", encoding="utf-8")
    manifest = {"schema": 1, "target": target, "repo": "app", "venv": "venv", "store": "tools"}
    (payload / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("build_set", type=Path)
    args = parser.parse_args()
    write_facts(args.payload, args.payload / "app/pm/lock.json", args.build_set)
