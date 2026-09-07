"""Emit Termux entrypoints and package-manager hooks from declared scripts."""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import tomllib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def write_launchers(payload: Path, entries: dict[str, str]) -> None:
    from scripts.bundles.payload import posix_launcher

    lock = payload / "app/pm/lock.json"
    if lock.is_file():
        import json
        version = json.loads(lock.read_text(encoding="utf-8-sig"))["packages"]["python"]["version"]
        minor = version.split("+")[0].rsplit(".", 1)[0]
    else:
        minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    bindir = payload / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, entry in entries.items():
        text = posix_launcher(name, entry, python="venv/bin/python", repo="app",
                              site=f"venv/lib/python{minor}/site-packages", target="linux-arm64-bionic")
        path = bindir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)


def write_maintainer_scripts(control: Path, names: list[str]) -> None:
    names_text = " ".join(shlex.quote(name) for name in names)
    postinst = '''#!/data/data/com.termux/files/usr/bin/sh
set -eu
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
mkdir -p "$PREFIX/bin"
for name in __NAMES__; do
    link="$PREFIX/bin/$name"
    target="../lib/hermes-agent/bin/$name"
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
        continue
    fi
    if [ -e "$link" ] || [ -L "$link" ]; then
        printf 'Refusing to replace foreign launcher: %s\\n' "$link" >&2
        exit 1
    fi
    ln -s "$target" "$link"
done
'''
    prerm = '''#!/data/data/com.termux/files/usr/bin/sh
set -eu
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
case "${1:-remove}" in
    remove|deconfigure) ;;
    *) exit 0 ;;
esac
for name in __NAMES__; do
    link="$PREFIX/bin/$name"
    if [ -L "$link" ] && [ "$(readlink "$link")" = "../lib/hermes-agent/bin/$name" ]; then
        rm -f "$link"
    fi
done
'''
    for name, script in (("postinst", postinst), ("prerm", prerm)):
        path = control / name
        path.write_text(script.replace("__NAMES__", names_text), encoding="utf-8")
        path.chmod(0o755)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--control", type=Path)
    args = parser.parse_args()
    entries = tomllib.loads((args.payload / "app/pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    write_launchers(args.payload, entries)
    if args.control is not None:
        write_maintainer_scripts(args.control, list(entries))


if __name__ == "__main__":
    main()
