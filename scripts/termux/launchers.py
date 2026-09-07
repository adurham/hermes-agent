"""Emit Termux entrypoints and package-manager hooks from declared scripts."""
from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import tomllib


_LAUNCHER = '''#!/data/data/com.termux/files/usr/bin/sh
set -eu
self="$0"
while [ -L "$self" ]; do
    target="$(readlink "$self")"
    case "$target" in
        /*) self="$target" ;;
        *) self="$(dirname "$self")/$target" ;;
    esac
done
root="$(cd "$(dirname "$self")/.." && pwd)"
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
export PREFIX
unset PYTHONHOME
export LD_LIBRARY_PATH="$root/tools/python/data/data/com.termux/files/usr/lib:$root/tools/node/data/data/com.termux/files/usr/lib:$root/tools/ffmpeg/data/data/com.termux/files/usr/lib:$root/runtime-libs/lib:$PREFIX/lib"
export PYTHONPATH="$root/app"
export HERMES_PYTHON_SRC_ROOT="$root/app"
export HERMES_PYTHON="$root/venv/bin/python"
export HERMES_NODE="$root/tools/node/data/data/com.termux/files/usr/bin/node"
export HERMES_RUNTIME_DIR="$root/tools"
export PATH="$root/tools/npm/bin:$root/tools/node/data/data/com.termux/files/usr/bin:$root/tools/ffmpeg/data/data/com.termux/files/usr/bin:$root/tools/ripgrep:$PATH"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${XDG_CACHE_HOME:-$HOME/.cache}/hermes-pycache}"
exec "$HERMES_PYTHON" -P -c __ENTRY__ "$@"
'''


def write_launchers(payload: Path, entries: dict[str, str]) -> None:
    bindir = payload / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name, entry in entries.items():
        module, func = entry.split(":", 1)
        script = f"import sys; sys.argv[0] = {name!r}; from {module} import {func}; sys.exit({func}())"
        path = bindir / name
        path.write_text(_LAUNCHER.replace("__ENTRY__", shlex.quote(script)), encoding="utf-8")
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
