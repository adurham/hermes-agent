"""Record the staged bionic tools through pm's existing facts contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))




def write_facts(payload: Path, lock_path: Path, build_set: Path) -> None:
    target = "linux-arm64-bionic"
    from scripts.bundles.payload import record_tools
    record_tools(payload, lock_path, target, {name: name for name in ("python", "node", "uv", "npm", "ffmpeg", "ripgrep")})
    natives = [line.strip() for line in build_set.read_text(encoding="utf-8").splitlines() if line.strip()]
    (payload / "native-wheels.json").write_text(json.dumps(natives) + "\n", encoding="utf-8")
    from scripts.bundles.payload import write_manifest
    write_manifest(payload, target=target, repo="app")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("build_set", type=Path)
    args = parser.parse_args()
    write_facts(args.payload, args.payload / "app/pm/lock.json", args.build_set)
