"""Stage the shared native payload and launchers without an Electron package."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.bundles.native import stage_native
from scripts.bundles.payload import stage_launchers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    code = stage_native(args)
    if code:
        return code
    root = Path(args.out).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
    stage_launchers(root, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
