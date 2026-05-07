#!/usr/bin/env python3
"""Delete source files for toolsets disabled in ~/.hermes/config.yaml.

The runtime hardening in ``agent.disabled_toolsets`` already prevents these
tools from registering with the agent's schema. This script is the next
layer — actually removing the source files from disk so they aren't part
of the supply-chain surface area at all.

Idempotent: re-running after an upstream pull will re-rip any files the
pull restored. Defaults to --dry-run so you see what will be deleted
before anything happens.

Usage:
    python scripts/corporate-rip.py            # dry-run (default)
    python scripts/corporate-rip.py --apply    # actually delete
    python scripts/corporate-rip.py --apply --quiet
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml


# Map toolset key (as listed in agent.disabled_toolsets) to the source
# files / directories that should be removed when that toolset is disabled.
# Paths are relative to the repo root.
#
# Only includes toolsets where supply-chain pruning is meaningful — i.e.
# ones that ship dedicated source files. Toolsets like ``web`` or ``terminal``
# share infrastructure with the rest of hermes and aren't ripped here.
TOOLSET_FILES: dict[str, list[str]] = {
    "discord": [
        "tools/discord_tool.py",
        "tests/tools/test_discord_tool.py",
    ],
    "discord_admin": [
        # Lives in the same module as ``discord``; rip is shared.
        "tools/discord_tool.py",
        "tests/tools/test_discord_tool.py",
    ],
    "messaging": [
        "tools/send_message_tool.py",
        "tests/tools/test_send_message_tool.py",
        "tests/tools/test_send_message_missing_platforms.py",
    ],
    "feishu_doc": [
        "tools/feishu_doc_tool.py",
        "tests/tools/test_feishu_tools.py",
    ],
    "feishu_drive": [
        "tools/feishu_drive_tool.py",
    ],
    "yuanbao": [
        "tools/yuanbao_tools.py",
        "tests/test_yuanbao_integration.py",
        "tests/test_yuanbao_markdown.py",
        "tests/test_yuanbao_pipeline.py",
        "tests/test_yuanbao_proto.py",
    ],
    "homeassistant": [
        "tools/homeassistant_tool.py",
        "tests/tools/test_homeassistant_tool.py",
        "tests/gateway/test_homeassistant.py",
    ],
    "moa": [
        "tools/mixture_of_agents_tool.py",
        "tests/tools/test_mixture_of_agents_tool.py",
    ],
    "rl": [
        "tools/rl_training_tool.py",
        "rl_cli.py",
        "tests/tools/test_rl_training_tool.py",
    ],
    "spotify": [
        "plugins/spotify",  # whole directory
        "tests/hermes_cli/test_spotify_auth.py",
        "tests/tools/test_spotify_client.py",
    ],
    "image_gen": [
        "tools/image_generation_tool.py",
        "plugins/image_gen",  # whole directory
        "tests/agent/test_image_gen_registry.py",
        "tests/hermes_cli/test_image_gen_picker.py",
        "tests/plugins/image_gen",  # whole directory
        "tests/tools/test_image_generation.py",
        "tests/tools/test_image_generation_env.py",
        "tests/tools/test_image_generation_plugin_dispatch.py",
    ],
    # ``video`` intentionally absent — video_analyze ships in
    # tools/vision_tools.py alongside the still-active vision_analyze.
    # Runtime disable via agent.disabled_toolsets is sufficient.
}


def load_disabled_toolsets(config_path: Path) -> list[str]:
    if not config_path.exists():
        sys.exit(f"error: {config_path} does not exist")
    with config_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return list(cfg.get("agent", {}).get("disabled_toolsets") or [])


def collect_targets(disabled: list[str], repo_root: Path) -> list[tuple[str, Path]]:
    """Return [(toolset, path), ...] for files that exist and would be removed."""
    targets: list[tuple[str, Path]] = []
    seen_paths: set[Path] = set()
    for ts in disabled:
        for rel in TOOLSET_FILES.get(ts, []):
            p = (repo_root / rel).resolve()
            if p in seen_paths:
                continue
            seen_paths.add(p)
            if p.exists():
                targets.append((ts, p))
    return targets


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete files (default is dry-run)",
    )
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".hermes" / "config.yaml"),
        help="path to hermes config.yaml (default: ~/.hermes/config.yaml)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-file output",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    disabled = load_disabled_toolsets(Path(args.config))
    targets = collect_targets(disabled, repo_root)

    if not targets:
        if not args.quiet:
            print("nothing to rip — no disabled toolsets matched files on disk")
        return 0

    label = "would delete" if not args.apply else "deleting"
    print(f"{label} {len(targets)} item(s) for {len(set(t[0] for t in targets))} disabled toolset(s):")
    by_ts: dict[str, list[Path]] = {}
    for ts, p in targets:
        by_ts.setdefault(ts, []).append(p)
    for ts in sorted(by_ts):
        print(f"  [{ts}]")
        for p in sorted(by_ts[ts]):
            print(f"    {p.relative_to(repo_root)}")

    if not args.apply:
        print()
        print("dry-run; pass --apply to actually delete")
        return 0

    failed: list[tuple[Path, Exception]] = []
    for _, p in targets:
        try:
            remove(p)
        except Exception as e:
            failed.append((p, e))
    if failed:
        print()
        print(f"FAILED on {len(failed)} item(s):")
        for p, e in failed:
            print(f"  {p}: {e}")
        return 1
    if not args.quiet:
        print()
        print(f"removed {len(targets)} item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
