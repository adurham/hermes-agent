"""Clipboard image extraction for macOS, Windows, Linux, and WSL2.

Provides a single function `save_clipboard_image(dest)` that checks the
system clipboard for image data, saves it to *dest* as PNG, and returns
True on success.  No external Python dependencies — uses only OS-level
CLI tools that ship with the platform (or are commonly installed).

Platform support:
  macOS   — osascript (always available), pngpaste (if installed)
  Windows — PowerShell via WinForms, Get-Clipboard, file-drop fallback
  WSL2    — powershell.exe via WinForms, Get-Clipboard, file-drop fallback
  Linux   — hlxc reverse-tunnel clipboard bridge (op-broker over SSH),
            kitten clipboard (kitty terminal over SSH / headless),
            wl-paste (Wayland), xclip (X11)
"""

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from hermes_constants import is_wsl as _is_wsl

logger = logging.getLogger(__name__)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def save_clipboard_image(dest: Path) -> bool:
    """Extract an image from the system clipboard and save it as PNG.

    Returns True if an image was found and saved, False otherwise.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        return _macos_save(dest)
    if sys.platform == "win32":
        return _windows_save(dest)
    return _linux_save(dest)


def has_clipboard_image() -> bool:
    """Quick check: does the clipboard currently contain an image?

    Lighter than save_clipboard_image — doesn't extract or write anything.
    """
    if sys.platform == "darwin":
        return _macos_has_image()
    if sys.platform == "win32":
        return _windows_has_image()
    # Match _linux_save fallthrough order: bridge → kitten → WSL → Wayland → X11
    bridge = _bridge_has_image()
    if bridge is True:
        return True
    if bridge is None and _kitten_has_image():
        return True
    if _is_wsl() and _wsl_has_image():
        return True
    if os.environ.get("WAYLAND_DISPLAY") and _wayland_has_image():
        return True
    return _xclip_has_image()


# ── Text write (native tools, mirrors ui-tui/src/lib/clipboard.ts) ──────

def _powershell_write_script(b64: str) -> str:
    # PowerShell decodes piped stdin with the system ANSI code page (e.g.
    # CP936), not UTF-8, so stdin-based writes mangle CJK/emoji.  Base64 the
    # UTF-8 bytes and decode inside PowerShell instead — same approach as
    # the TUI's writeClipboardText.
    return (
        "Set-Clipboard -Value ([System.Text.Encoding]::UTF8.GetString("
        f"[System.Convert]::FromBase64String('{b64}')))"
    )


def _write_clipboard_commands() -> list:
    """Return (cmd_argv, use_stdin) candidates in platform fallback order."""
    if sys.platform == "darwin":
        return [(["pbcopy"], True)]
    if sys.platform == "win32":
        return [(["powershell", "-NoProfile", "-NonInteractive"], False)]
    attempts = []
    if _is_wsl():
        attempts.append((["powershell.exe", "-NoProfile", "-NonInteractive"], False))
    if os.environ.get("WAYLAND_DISPLAY"):
        attempts.append((["wl-copy", "--type", "text/plain"], True))
    attempts.append((["xclip", "-selection", "clipboard", "-in"], True))
    attempts.append((["xsel", "--clipboard", "--input"], True))
    return attempts


def is_remote_shell_session(env=None) -> bool:
    """True when running inside an SSH session.

    Mirrors ui-tui/src/lib/terminalSetup.ts isRemoteShellSession().  Over
    SSH, native clipboard tools write the REMOTE machine's clipboard (or
    an X-forwarded one), which is almost never what the user wants —
    OSC 52 reaches the LOCAL terminal emulator instead.
    """
    e = os.environ if env is None else env
    return bool(
        e.get("SSH_CONNECTION") or e.get("SSH_TTY") or e.get("SSH_CLIENT")
    )


def write_clipboard_text(text: str) -> bool:
    """Write *text* to the system clipboard via native platform tools.

    Fallback order matches the TUI (ui-tui/src/lib/clipboard.ts):
    macOS pbcopy → Windows/WSL PowerShell Set-Clipboard → wl-copy →
    xclip → xsel.  Returns True if any backend succeeded; callers should
    fall back to OSC 52 on False.
    """
    for argv, use_stdin in _write_clipboard_commands():
        try:
            if use_stdin:
                proc = subprocess.run(
                    argv, input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            else:
                b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
                proc = subprocess.run(
                    argv + ["-Command", _powershell_write_script(b64)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


# ── macOS ────────────────────────────────────────────────────────────────

def _macos_save(dest: Path) -> bool:
    """Try pngpaste first (fast, handles more formats), fall back to osascript."""
    return _macos_pngpaste(dest) or _macos_osascript(dest)


def _macos_has_image() -> bool:
    """Check if macOS clipboard contains image data."""
    try:
        info = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return "«class PNGf»" in info.stdout or "«class TIFF»" in info.stdout
    except Exception:
        return False


def _macos_pngpaste(dest: Path) -> bool:
    """Use pngpaste (brew install pngpaste) — fastest, cleanest."""
    try:
        r = subprocess.run(
            ["pngpaste", str(dest)],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
    except FileNotFoundError:
        pass  # pngpaste not installed
    except Exception as e:
        logger.debug("pngpaste failed: %s", e)
    return False


def _macos_osascript(dest: Path) -> bool:
    """Use osascript to extract PNG data from clipboard (always available)."""
    if not _macos_has_image():
        return False

    # Extract as PNG
    script = (
        'try\n'
        '  set imgData to the clipboard as «class PNGf»\n'
        f'  set f to open for access POSIX file "{dest}" with write permission\n'
        '  write imgData to f\n'
        '  close access f\n'
        'on error\n'
        '  return "fail"\n'
        'end try\n'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
        )
        if r.returncode == 0 and "fail" not in r.stdout and dest.exists() and dest.stat().st_size > 0:
            return True
    except Exception as e:
        logger.debug("osascript clipboard extract failed: %s", e)
    return False


# ── Shared PowerShell scripts (native Windows + WSL2) ─────────────────────

# .NET System.Windows.Forms.Clipboard — used by both native Windows (powershell)
# and WSL2 (powershell.exe) paths.
_PS_CHECK_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "[System.Windows.Forms.Clipboard]::ContainsImage()"
)

_PS_EXTRACT_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "Add-Type -AssemblyName System.Drawing;"
    "$img = [System.Windows.Forms.Clipboard]::GetImage();"
    "if ($null -eq $img) { exit 1 }"
    "$ms = New-Object System.IO.MemoryStream;"
    "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png);"
    "[System.Convert]::ToBase64String($ms.ToArray())"
)

_PS_CHECK_IMAGE_GET_CLIPBOARD = (
    "try { "
    "$img = Get-Clipboard -Format Image -ErrorAction Stop;"
    "if ($null -ne $img) { 'True' } else { 'False' }"
    "} catch { 'False' }"
)

_PS_EXTRACT_IMAGE_GET_CLIPBOARD = (
    "try { "
    "Add-Type -AssemblyName System.Drawing;"
    "Add-Type -AssemblyName PresentationCore;"
    "Add-Type -AssemblyName WindowsBase;"
    "$img = Get-Clipboard -Format Image -ErrorAction Stop;"
    "if ($null -eq $img) { exit 1 }"
    "$ms = New-Object System.IO.MemoryStream;"
    "if ($img -is [System.Drawing.Image]) {"
    "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)"
    "} elseif ($img -is [System.Windows.Media.Imaging.BitmapSource]) {"
    "$enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder;"
    "$enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($img));"
    "$enc.Save($ms)"
    "} else { exit 2 }"
    "[System.Convert]::ToBase64String($ms.ToArray())"
    "} catch { exit 1 }"
)

_FILEDROP_IMAGE_EXTS = "'.png','.jpg','.jpeg','.gif','.webp','.bmp','.tiff','.tif'"

_PS_CHECK_FILEDROP_IMAGE = (
    "try { "
    "$files = Get-Clipboard -Format FileDropList -ErrorAction Stop;"
    f"$exts = @({_FILEDROP_IMAGE_EXTS});"
    "$hit = $files | Where-Object { $exts -contains ([System.IO.Path]::GetExtension($_).ToLowerInvariant()) } | Select-Object -First 1;"
    "if ($null -ne $hit) { 'True' } else { 'False' }"
    "} catch { 'False' }"
)

_PS_EXTRACT_FILEDROP_IMAGE = (
    "try { "
    "$files = Get-Clipboard -Format FileDropList -ErrorAction Stop;"
    f"$exts = @({_FILEDROP_IMAGE_EXTS});"
    "$hit = $files | Where-Object { $exts -contains ([System.IO.Path]::GetExtension($_).ToLowerInvariant()) } | Select-Object -First 1;"
    "if ($null -eq $hit) { exit 1 }"
    "[System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($hit))"
    "} catch { exit 1 }"
)

_POWERSHELL_HAS_IMAGE_SCRIPTS = (
    _PS_CHECK_IMAGE,
    _PS_CHECK_IMAGE_GET_CLIPBOARD,
    _PS_CHECK_FILEDROP_IMAGE,
)

_POWERSHELL_EXTRACT_IMAGE_SCRIPTS = (
    _PS_EXTRACT_IMAGE,
    _PS_EXTRACT_IMAGE_GET_CLIPBOARD,
    _PS_EXTRACT_FILEDROP_IMAGE,
)


def _run_powershell(exe: str, script: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
    )


def _write_base64_image(dest: Path, b64_data: str) -> bool:
    image_bytes = base64.b64decode(b64_data, validate=True)
    dest.write_bytes(image_bytes)
    return dest.exists() and dest.stat().st_size > 0


def _powershell_has_image(exe: str, *, timeout: int, label: str) -> bool:
    for script in _POWERSHELL_HAS_IMAGE_SCRIPTS:
        try:
            r = _run_powershell(exe, script, timeout=timeout)
            if r.returncode == 0 and "True" in r.stdout:
                return True
        except FileNotFoundError:
            logger.debug("%s not found — clipboard unavailable", exe)
            return False
        except Exception as e:
            logger.debug("%s clipboard image check failed: %s", label, e)
    return False


def _powershell_save_image(exe: str, dest: Path, *, timeout: int, label: str) -> bool:
    for script in _POWERSHELL_EXTRACT_IMAGE_SCRIPTS:
        try:
            r = _run_powershell(exe, script, timeout=timeout)
            if r.returncode != 0:
                continue

            b64_data = r.stdout.strip()
            if not b64_data:
                continue

            if _write_base64_image(dest, b64_data):
                return True
        except FileNotFoundError:
            logger.debug("%s not found — clipboard unavailable", exe)
            return False
        except Exception as e:
            logger.debug("%s clipboard image extraction failed: %s", label, e)
            dest.unlink(missing_ok=True)
    return False


# ── Native Windows ────────────────────────────────────────────────────────

# Native Windows uses ``powershell`` (Windows PowerShell 5.1, always present)
# or ``pwsh`` (PowerShell 7+, optional).  Discovery is cached per-process.


def _find_powershell() -> str | None:
    """Return the first available PowerShell executable, or None."""
    for name in ("powershell", "pwsh"):
        try:
            r = subprocess.run(
                [name, "-NoProfile", "-NonInteractive", "-Command", "echo ok"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
            )
            if r.returncode == 0 and "ok" in r.stdout:
                return name
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return None


# Cache the resolved PowerShell executable (checked once per process)
_ps_exe: str | None | bool = False  # False = not yet checked


def _get_ps_exe() -> str | None:
    global _ps_exe
    if _ps_exe is False:
        _ps_exe = _find_powershell()
    return _ps_exe


def _windows_has_image() -> bool:
    """Check if the Windows clipboard contains an image."""
    ps = _get_ps_exe()
    if ps is None:
        return False
    return _powershell_has_image(ps, timeout=5, label="Windows")


def _windows_save(dest: Path) -> bool:
    """Extract clipboard image on native Windows via PowerShell → base64 PNG."""
    ps = _get_ps_exe()
    if ps is None:
        logger.debug("No PowerShell found — Windows clipboard image paste unavailable")
        return False
    return _powershell_save_image(ps, dest, timeout=15, label="Windows")


# ── Linux ────────────────────────────────────────────────────────────────

def _linux_save(dest: Path) -> bool:
    """Try clipboard backends in priority order: bridge → kitten → WSL → Wayland → X11."""
    bridge = _bridge_save(dest)
    if bridge is True:
        return True
    # Bridge unavailable (no tunnel / transport stall): kitten may still work
    # over a plain SSH tty (the no-tunnel case).  A DEFINITIVE no-image answer
    # (False) means the far-end Mac clipboard is empty — kitten reads the SAME
    # far-end clipboard via OSC 5522, so it cannot know more; skip it and fall
    # through to the local-desktop backends, which read DIFFERENT clipboards.
    if bridge is None and _kitten_save(dest):
        return True

    if _is_wsl():
        if _wsl_save(dest):
            return True
        # Fall through — WSLg might have wl-paste or xclip working

    if os.environ.get("WAYLAND_DISPLAY"):
        if _wayland_save(dest):
            return True

    return _xclip_save(dest)


# ── kitty terminal (kitten clipboard, OSC 5522 over the tty) ─────────────
#
# `kitten clipboard -g <file>` is kitty's mechanism for reading the system
# clipboard from inside an SSH session: the request travels as an OSC 5522
# escape sequence down the controlling terminal (wrapping itself in the tmux
# DCS passthrough when $TMUX is set), and the LOCAL terminal emulator — the
# kitty window physically sitting at the far end of the SSH connection —
# answers with the clipboard content.  This works with no display server, X11,
# Wayland, or clipboard daemon on the remote host, which is exactly the
# headless-LXC-over-SSH case where wl-paste/xclip have nothing to talk to.
#
# kitten is officially distributed as a standalone static binary that needs
# no kitty installation on the remote host (see the kitty GitHub releases).
#
# Two cautions shape this backend:
#   * When the far-end terminal is NOT kitty, the OSC 5522 request goes
#     unanswered and `kitten` waits INDEFINITELY (its own abort is Esc-Esc).
#     We must always run it under a subprocess timeout.
#   * `kitten clipboard -g` may prompt for permission on the local kitty
#     (kitty's clipboard_control option).  With the kitty default config the
#     read needs `read-clipboard` in clipboard_control to be silent; the
#     docs describe this as a UX tradeoff the user controls on their local
#     terminal, not something the remote side can (or should) override.
#
# Process-lifetime negative cache: if kitten ever times out here (no kitty
# at the far end), later attempts in the same process skip it entirely so a
# single Ctrl+V press never pays the timeout cost twice.
#
# subprocess.run(timeout=) kills with SIGKILL, which kitten cannot intercept
# to restore the tty termios it set to raw — so on the timeout path we save
# and restore the tty settings ourselves, keeping the interactive prompt's
# Ctrl+C/echo behavior intact for whatever session survives us.

_KITTEN_TIMEOUT_S = 3  # a live kitty answers in well under a second
_kitten_unavailable: bool = False


def _tty_settings() -> list | None:
    """Snapshot termios of the controlling tty (if any), for restore."""
    try:
        import termios
        if os.isatty(0):
            return termios.tcgetattr(0)
    except Exception:
        pass
    return None


def _restore_tty(settings) -> None:
    """Re-apply termios saved by _tty_settings (no-op when None)."""
    if not settings:
        return
    try:
        import termios
        termios.tcsetattr(0, termios.TCSANOW, settings)
    except Exception as e:
        logger.debug("tty restore after kitten timeout failed: %s", e)


def _find_kitten() -> str | None:
    """Return the kitten executable path, or None.

    PATH lookup first; then the fixed install location used by the
    standalone-binary deployment (kitty ships kitten as a static binary
    that needs no kitty install on the remote host).  The fallback must
    actually exist — an unconditional path string here would turn "not
    found" into a guaranteed FileNotFoundError on every call.
    """
    found = shutil.which("kitten")
    if found:
        return found
    fixed = Path.home() / ".local/bin/kitten"
    if fixed.is_file():
        return str(fixed)
    return None


def _kitten_preferred(env=None) -> bool:
    """True when the session has no local display server to read instead.

    Over SSH into a headless box there is no DISPLAY/WAYLAND_DISPLAY and no
    clipboard daemon, so wl-paste/xclip cannot work and the only reachable
    clipboard is the local terminal emulator's (kitty).  On a local desktop
    (DISPLAY/WAYLAND_DISPLAY set) the native tools are the right choice —
    including X-forwarded sessions, where xclip reads the forwarded
    clipboard of the machine the user is actually sitting at.  WSL is
    excluded too: kitty has no native Windows build, so the far-end
    terminal of a WSL pty can never answer the OSC 5522 request and the
    Windows clipboard is already reachable via powershell.exe.
    """
    if _is_wsl():
        return False
    e = os.environ if env is None else env
    return not (e.get("DISPLAY") or e.get("WAYLAND_DISPLAY"))


def _kitten_save(dest: Path) -> bool:
    """Read the clipboard via `kitten clipboard -g` (kitty over SSH)."""
    global _kitten_unavailable
    if _kitten_unavailable or not _kitten_preferred():
        return False
    kitten = _find_kitten()
    if not kitten:
        return False
    saved_tty = _tty_settings()
    try:
        r = subprocess.run(
            [kitten, "clipboard", "-g", str(dest)],
            capture_output=True, timeout=_KITTEN_TIMEOUT_S,
        )
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        # No kitty answered — don't pay this timeout again in this process.
        _kitten_unavailable = True
    except Exception as e:
        logger.debug("kitten clipboard extraction failed: %s", e)
    finally:
        _restore_tty(saved_tty)
    dest.unlink(missing_ok=True)
    return False


def _kitten_has_image() -> bool:
    """Check for an image via `kitten clipboard -g -m .` (lists MIME types)."""
    global _kitten_unavailable
    if _kitten_unavailable or not _kitten_preferred():
        return False
    kitten = _find_kitten()
    if not kitten:
        return False
    try:
        r = subprocess.run(
            [kitten, "clipboard", "-g", "-m", ".", "/dev/stdout"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=_KITTEN_TIMEOUT_S,
        )
        if r.returncode == 0 and any(
            t.strip().startswith("image/") for t in r.stdout.splitlines()
        ):
            return True
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        _kitten_unavailable = True
    except Exception as e:
        logger.debug("kitten clipboard mime check failed: %s", e)
    return False


# ── hlxc reverse-tunnel clipboard bridge (op-broker over SSH) ─────────────
#
# The user's real access path to the headless remote box (hermes-gw-01, an
# LXC with no X11/Wayland) is `hlxc`, which always runs inside tmux.  tmux
# provably drops the OSC 5522 round trip that the kitten backend depends on:
# the server drops the pane's request, and even with allow-passthrough the
# kitty unwrapped response is dropped by the tmux client.  Inside tmux,
# `kitten clipboard -g` times out after 3s and falls through — Ctrl+V
# silently does nothing.  The bridge exists to fix exactly that case.
#
# The bridge is a Mac-side broker daemon (~/bin/op-broker.py) that listens
# on a Unix socket which hlxc's SSH connection reverse-forwards to
# /run/hermes-op-broker.sock on the box — the same tunnel that already
# carries the 1Password bridge (an established, working pattern).  The
# broker reads the Mac's clipboard natively (pngpaste/osascript) and
# returns base64 PNG.  This transport is the SSH channel, NOT the tty byte
# stream, so tmux cannot filter it — that is the entire point.
#
# Why socket-presence is the gate (not a TMUX env var, not
# is_remote_shell_session()): the real hlxc path runs `su - hermes`, which
# strips SSH_* env vars, so an SSH/TMUX gate would be a silent no-op there
# (the same reason Layer 1 rejected an SSH gate for kitten).  The socket
# exists only while an hlxc session is live, so its presence/reachability
# IS the gate, and it serves both tmux and non-tmux sessions alike.
#
# Why bridge-first: when the tunnel is up it is strictly better than kitten
# (no 3s timeout, no tty dependency), and a definitive no-image answer lets
# us skip kitten's pointless timeout entirely.  When the tunnel is down the
# bridge reports unavailable and we fall through to kitten for the
# plain-SSH-no-tunnel case.
#
# Security posture: the socket exists only while an hlxc session is live;
# it is created by root's sshd with mask 0111, so any local user on the box
# could connect to it while it exists.  This is the same posture as the
# already-shipped 1Password bridge over the same socket — the broker only
# answers clipboard reads, never secrets, and the exposure window is the
# live session.
#
# Process-lifetime negative cache: _bridge_unavailable is set ONLY on a
# socket-level timeout (transport stall — the tunnel is up but wedged, so
# retrying in the same process is pointless).  It is NEVER set on
# connect-refusal (ECONNREFUSED — the tunnel may come back within the
# process lifetime) and NEVER on a definitive no-image answer.

_BRIDGE_TIMEOUT_S = 15.0
_BRIDGE_MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # generous cap for a full-res screenshot PNG
_bridge_unavailable: bool = False


def _bridge_sock_path() -> str:
    """Return the reverse-tunneled broker socket path.

    Mirrors the existing op shim convention: the OP_BROKER_SOCK env var
    already exists on the box side (shipped in homelab commit 326a10d) and
    is reused here, not invented.  No new HERMES_* env vars.
    """
    return os.environ.get("OP_BROKER_SOCK", "/run/hermes-op-broker.sock")


def _bridge_request(req: dict, timeout: float = _BRIDGE_TIMEOUT_S) -> dict | None:
    """Send one JSON request to the broker socket and read the JSON reply.

    Returns the parsed response dict, or None on ANY failure: OSError
    (socket missing, connect refused), timeout, JSONDecodeError, or an
    empty/EOF'd read.  Never raises.

    The read loop tracks a cumulative deadline (not just a per-recv
    timeout) so a peer trickling bytes can't stretch the wait past
    *timeout* in total, and caps the accumulated buffer so a peer that
    never sends a newline can't grow memory unboundedly — no blind
    sleeps, purely recv-driven.

    A socket-level timeout is the one failure mode that sets the
    process-lifetime negative cache (_bridge_unavailable) — this is the
    single call site both _bridge_save and _bridge_has_image share, so it
    owns the cache exactly like `_kitten_save`'s own subprocess call owns
    `_kitten_unavailable`.  Connect-refused (socket present but nothing
    listening — tunnel down right now) is a plain OSError here and does
    NOT set the cache, since the tunnel may come back within the process
    lifetime.
    """
    global _bridge_unavailable
    deadline = time.monotonic() + timeout
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(_bridge_sock_path())
            sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("bridge read exceeded total timeout")
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > _BRIDGE_MAX_RESPONSE_BYTES:
                    return None
                if b"\n" in buf:
                    break
        finally:
            sock.close()
    except (TimeoutError, socket.timeout):
        _bridge_unavailable = True
        return None
    except OSError:
        return None
    if not buf:
        return None
    try:
        line = bytes(buf).split(b"\n", 1)[0].decode("utf-8")
        return json.loads(line)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None


def _bridge_save(dest: Path) -> bool | None:
    """Read the clipboard via the reverse-tunnel bridge.

    Tri-state return:
      True  — got an image, wrote it to *dest*.
      False — DEFINITIVE 'no image on the far-end (Mac) clipboard'
              (broker answered rc=1).
      None  — bridge unavailable (socket missing, connect refused, timeout
              [sets _bridge_unavailable], malformed/legacy response, rc=2/3,
              or any other error).
    """
    if _bridge_unavailable:
        return None
    resp = _bridge_request({"type": "clipboard_read"})
    if resp is None:
        return None
    rc = resp.get("returncode")
    if rc == 0:
        png_b64 = resp.get("png_b64")
        if not isinstance(png_b64, str) or not png_b64.strip():
            dest.unlink(missing_ok=True)
            return None
        try:
            image_bytes = base64.b64decode(png_b64, validate=True)
        except (ValueError, TypeError):
            dest.unlink(missing_ok=True)
            return None
        try:
            dest.write_bytes(image_bytes)
        except OSError:
            dest.unlink(missing_ok=True)
            return None
        if dest.exists() and dest.stat().st_size > 0:
            return True
        dest.unlink(missing_ok=True)
        return None
    if rc == 1:
        # Definitive no-image at the far end.
        return False
    # rc 2/3 or any other error → bridge unavailable for this attempt.
    return None


def _bridge_has_image() -> bool | None:
    """Check for an image via the reverse-tunnel bridge.

    Tri-state: True = has image; False = DEFINITIVE no image; None = bridge
    unavailable (same semantics as _bridge_save).
    """
    if _bridge_unavailable:
        return None
    resp = _bridge_request({"type": "clipboard_has_image"})
    if resp is None:
        return None
    if "has_image" not in resp:
        # Legacy broker (no "type" support) or malformed response.
        return None
    return bool(resp["has_image"])


# ── WSL2 (powershell.exe) ────────────────────────────────────────────────
# Reuses _PS_CHECK_IMAGE / _PS_EXTRACT_IMAGE defined above.

def _wsl_has_image() -> bool:
    """Check if Windows clipboard has an image (via powershell.exe)."""
    return _powershell_has_image("powershell.exe", timeout=8, label="WSL")


def _wsl_save(dest: Path) -> bool:
    """Extract clipboard image via powershell.exe → base64 → decode to PNG."""
    return _powershell_save_image("powershell.exe", dest, timeout=15, label="WSL")


# ── Wayland (wl-paste) ──────────────────────────────────────────────────

def _wayland_has_image() -> bool:
    """Check if Wayland clipboard has image content."""
    try:
        r = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return r.returncode == 0 and any(
            t.startswith("image/") for t in r.stdout.splitlines()
        )
    except FileNotFoundError:
        logger.debug("wl-paste not installed — Wayland clipboard unavailable")
    except Exception:
        pass
    return False


def _wayland_save(dest: Path) -> bool:
    """Use wl-paste to extract clipboard image (Wayland sessions)."""
    try:
        # Check available MIME types
        types_r = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        if types_r.returncode != 0:
            return False
        types = types_r.stdout.splitlines()

        # Prefer PNG, fall back to other image formats
        mime = None
        for preferred in ("image/png", "image/jpeg", "image/bmp",
                          "image/gif", "image/webp"):
            if preferred in types:
                mime = preferred
                break

        if not mime:
            return False

        # Extract the image data
        with open(dest, "wb") as f:
            subprocess.run(
                ["wl-paste", "--type", mime],
                stdout=f, stderr=subprocess.DEVNULL, timeout=5, check=True,
            )

        if not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            return False

        # save_clipboard_image() promises a PNG output path. Wayland can offer
        # JPEG/GIF/WebP/BMP payloads, so normalize every non-PNG result before
        # returning success.
        if mime != "image/png":
            if not _convert_to_png(dest) or not _is_png_file(dest):
                dest.unlink(missing_ok=True)
                return False

        return True

    except FileNotFoundError:
        logger.debug("wl-paste not installed — Wayland clipboard unavailable")
    except Exception as e:
        logger.debug("wl-paste clipboard extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False


def _convert_to_png(path: Path) -> bool:
    """Convert an image file to PNG in-place (requires Pillow or ImageMagick)."""
    # Try Pillow first (likely installed in the venv)
    try:
        from PIL import Image
        img = Image.open(path)
        img.save(path, "PNG")
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("Pillow BMP→PNG conversion failed: %s", e)

    # Fall back to ImageMagick convert
    tmp = path.with_suffix(".bmp")
    try:
        path.rename(tmp)
        r = subprocess.run(
            ["convert", str(tmp), "png:" + str(path)],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0 and path.exists() and path.stat().st_size > 0:
            tmp.unlink(missing_ok=True)
            return True
        else:
            # Convert failed — restore the original file
            tmp.rename(path)
    except FileNotFoundError:
        logger.debug("ImageMagick not installed — cannot convert BMP to PNG")
        if tmp.exists() and not path.exists():
            tmp.rename(path)
    except Exception as e:
        logger.debug("ImageMagick BMP→PNG conversion failed: %s", e)
        if tmp.exists() and not path.exists():
            tmp.rename(path)

    # Can't convert — BMP is still usable as-is for most APIs
    return path.exists() and path.stat().st_size > 0


def _is_png_file(path: Path) -> bool:
    """Return True when *path* starts with the PNG file signature."""
    try:
        with path.open("rb") as f:
            return f.read(len(_PNG_SIGNATURE)) == _PNG_SIGNATURE
    except OSError:
        return False


# ── X11 (xclip) ─────────────────────────────────────────────────────────

def _xclip_has_image() -> bool:
    """Check if X11 clipboard has image content."""
    try:
        r = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        return r.returncode == 0 and "image/png" in r.stdout
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return False


def _xclip_save(dest: Path) -> bool:
    """Use xclip to extract clipboard image (X11 sessions)."""
    # Check if clipboard has image content
    try:
        targets = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3,
        )
        if "image/png" not in targets.stdout:
            return False
    except FileNotFoundError:
        logger.debug("xclip not installed — X11 clipboard image paste unavailable")
        return False
    except Exception:
        return False

    # Extract PNG data
    try:
        with open(dest, "wb") as f:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                stdout=f, stderr=subprocess.DEVNULL, timeout=5, check=True,
            )
        if dest.exists() and dest.stat().st_size > 0:
            return True
    except Exception as e:
        logger.debug("xclip image extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False
