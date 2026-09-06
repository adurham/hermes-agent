"""Exercise a freshly installed Termux bundle with networking disabled."""
from __future__ import annotations

import fcntl
import importlib
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import tempfile
import time


def run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=90)
    print("+", " ".join(argv), flush=True)
    print(result.stdout, result.stderr, flush=True)
    result.check_returncode()
    return result


def tui_smoke(launcher: Path, env: dict[str, str], cwd: Path) -> None:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    child = subprocess.Popen(
        [str(launcher), "--tui"], stdin=slave, stdout=slave, stderr=slave,
        cwd=cwd, env={**env, "TERM": "xterm-256color"}, start_new_session=True,
    )
    os.close(slave)
    captured = bytearray()
    ready = False
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.5)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
                plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(captured))
                if b"Setup Required" in plain and b"/model" in plain:
                    ready = True
                    break
            if child.poll() is not None:
                break
        if not ready:
            raise RuntimeError("TUI never reached its real setup screen:\n" + captured.decode(errors="replace"))
        print("TUI_SETUP_SCREEN_OK", flush=True)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        os.close(master)


def main() -> None:
    prefix = Path(os.environ["PREFIX"])
    root = prefix / "lib/hermes-agent"
    with tempfile.TemporaryDirectory(prefix="hermes-install-proof-", dir=prefix / "tmp") as tmp:
        home = Path(tmp)
        env = {
            "PREFIX": str(prefix), "HOME": str(home), "HERMES_HOME": str(home / "state"),
            "PATH": str(prefix / "bin"), "TERM": "xterm-256color", "LANG": "C.UTF-8",
            "LD_LIBRARY_PATH": ":".join((
                str(root / "tools/python" / prefix.relative_to("/") / "lib"),
                str(root / "tools/node" / prefix.relative_to("/") / "lib"),
                str(root / "tools/ffmpeg" / prefix.relative_to("/") / "lib"),
                str(root / "runtime-libs/lib"), str(prefix / "lib"),
            )),
            "HERMES_RUNTIME_DIR": str(root / "tools"),
            "PYTHONPATH": str(root / "app"),
            "PYTHONPYCACHEPREFIX": str(home / "pycache"),
        }
        launcher = prefix / "bin/hermes"
        run([str(launcher), "--version"], env, home)
        run([str(launcher), "chat", "--help"], env, home)
        run([str(prefix / "bin/hermes-acp"), "--check"], env, home)
        python = root / "venv/bin/python"
        run([
            str(python), "-c",
            "import ctypes, ssl, sqlite3, bz2, lzma, zlib, hashlib, readline; "
            "import cli, run_agent, tui_gateway.server; "
            "from hermes_cli.config import detect_install_method; "
            "assert detect_install_method() == 'apt'; "
            "print('CLI_AND_STDLIB_IMPORTS_OK')",
        ], env, home)
        natives = json.loads((root / "native-wheels.json").read_text(encoding="utf-8"))
        run([
            str(python), str(root / "app/scripts/termux/build_wheels.py"),
            "--import-modules", *natives,
        ], env, home)
        node = root / "tools/node" / prefix.relative_to("/") / "bin/node"
        run([str(node), "--version"], env, home)
        run([str(node), str(root / "tools/npm/lib/node_modules/npm/bin/npm-cli.js"), "--version"], env, home)
        run([str(root / "tools/ripgrep/rg"), "--version"], env, home)
        ffmpeg = root / "tools/ffmpeg" / prefix.relative_to("/") / "bin/ffmpeg"
        output = home / "silence.wav"
        run([str(ffmpeg), "-hide_banner", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.1", str(output)], env, home)
        import wave
        with wave.open(str(output), "rb") as audio:
            assert audio.getnframes() > 0 and audio.getframerate() == 16000
        print("FFMPEG_MEDIA_CONVERSION_OK", flush=True)
        run([
            str(python), "-c",
            "import pm; import subprocess; from pathlib import Path; "
            "p,e=pm.uv(realize=False); assert p and Path(p).is_file(), p; "
            "subprocess.run([p, '--version'],env=e,check=True); "
            "assert not pm.check(), pm.check(); print('PM_RUNTIME_TOOLS_OK')",
        ], env, home)
        result = subprocess.run([str(launcher), "update"], env=env, cwd=home, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 or "pkg upgrade hermes-agent" not in result.stdout + result.stderr:
            raise RuntimeError(f"wrong updater refusal ({result.returncode}): {result.stdout}\n{result.stderr}")
        print("APT_UPDATE_REFUSAL_OK", flush=True)
        tui_smoke(launcher, env, home)
        print("INSTALLED_BUNDLE_VALIDATION_OK", flush=True)


if __name__ == "__main__":
    main()
