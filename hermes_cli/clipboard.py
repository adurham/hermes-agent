"""Clipboard image extraction and text write for macOS, Windows, Linux, and WSL2.

Provides `save_clipboard_image(dest)` / `has_clipboard_image()`.  No Python deps — only
OS-level CLI tools: macOS osascript (always present) / pngpaste (optional); Windows and WSL2
PowerShell via WinForms, Get-Clipboard, then a file-drop fallback; Linux hlxc reverse-tunnel
clipboard bridge (op-broker over SSH), kitten clipboard (kitty terminal over SSH / headless),
wl-paste (Wayland), xclip (X11).
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
_TEXT = dict(capture_output=True, text=True, encoding='utf-8', errors='replace')
_PS_FLAGS = ("-NoProfile", "-NonInteractive")


def _nonempty(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _probe(argv: list, timeout: int, ok, *, missing: str | None = None) -> bool:
    """Run a text-mode probe; True when it ran and ``ok(result)`` holds.
    A missing executable logs *missing* (when given); every other failure is silent."""
    try:
        return bool(ok(subprocess.run(argv, timeout=timeout, **_TEXT)))
    except FileNotFoundError:
        if missing:
            logger.debug(missing)
    except Exception:
        pass
    return False


def _pipe_to_file(argv: list, dest: Path) -> bool:
    """Run *argv* with stdout redirected into *dest*; True when a non-empty file resulted."""
    with open(dest, "wb") as f:
        subprocess.run(argv, stdout=f, stderr=subprocess.DEVNULL, timeout=5, check=True)
    return _nonempty(dest)


def _linux_backends():
    """(enabled, has_image, save) in Linux fallthrough order: WSL → Wayland → X11
    (a failed WSL probe falls through — WSLg might have wl-paste or xclip working)."""
    return (
        (_is_wsl(), _wsl_has_image, _wsl_save),
        (bool(os.environ.get("WAYLAND_DISPLAY")), _wayland_has_image, _wayland_save),
        (True, _xclip_has_image, _xclip_save))


def save_clipboard_image(dest: Path) -> bool:
    """Save the clipboard image to *dest* as PNG; True when an image was found and written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        # pngpaste first (fast, handles more formats); osascript is the always-present fallback.
        return _macos_pngpaste(dest) or _macos_osascript(dest)
    return (_windows_save if sys.platform == "win32" else _linux_save)(dest)


def has_clipboard_image() -> bool:
    """Quick check: does the clipboard currently contain an image?"""
    if sys.platform == "darwin":
        return _macos_has_image()
    if sys.platform == "win32":
        return _windows_has_image()
    # Fork backends run ahead of upstream's _linux_backends() table: the bridge is
    # tri-state (True / DEFINITIVE-False / unavailable) and kitten is gated on that,
    # which the flat table can't express.  Order: bridge → kitten → WSL → Wayland → X11.
    bridge = _bridge_has_image()
    if bridge is True:
        return True
    if bridge is None and _kitten_has_image():
        return True
    return any(enabled and has() for enabled, has, _ in _linux_backends())


# ── Text write (native tools, mirrors ui-tui/src/lib/clipboard.ts) ──────

def _write_clipboard_commands(data: bytes) -> list:
    """(argv, run_kwargs) candidates for writing *data*, in platform fallback order."""
    # PowerShell decodes piped stdin with the system ANSI code page (e.g. CP936), not UTF-8, so
    # stdin-based writes mangle CJK/emoji. Base64 the UTF-8 bytes and decode inside PowerShell
    # instead (same approach as the TUI's writeClipboardText).
    b64 = base64.b64encode(data).decode("ascii")
    ps_argv = [*_PS_FLAGS, "-Command", "Set-Clipboard -Value ([System.Text.Encoding]::UTF8"
               f".GetString([System.Convert]::FromBase64String('{b64}')))"]
    ps_kw, pipe = {"stdin": subprocess.DEVNULL}, {"input": data}
    linux = sys.platform not in ("darwin", "win32")
    return [(argv, kw) for enabled, argv, kw in (
        (sys.platform == "darwin", ["pbcopy"], pipe),
        (sys.platform == "win32", ["powershell", *ps_argv], ps_kw),
        (linux and _is_wsl(), ["powershell.exe", *ps_argv], ps_kw),
        (linux and os.environ.get("WAYLAND_DISPLAY"), ["wl-copy", "--type", "text/plain"], pipe),
        (linux, ["xclip", "-selection", "clipboard", "-in"], pipe),
        (linux, ["xsel", "--clipboard", "--input"], pipe),
    ) if enabled]


def is_remote_shell_session(env=None) -> bool:
    """True inside an SSH session (mirrors ui-tui/src/lib/terminalSetup.ts). Over SSH, native
    clipboard tools write the REMOTE machine's clipboard (or an X-forwarded one), which is almost
    never what the user wants — OSC 52 reaches the LOCAL terminal instead."""
    e = os.environ if env is None else env
    return bool(e.get("SSH_CONNECTION") or e.get("SSH_TTY") or e.get("SSH_CLIENT"))


def write_clipboard_text(text: str) -> bool:
    """Write *text* to the clipboard via native tools; fallback order matches the TUI: pbcopy →
    Windows/WSL PowerShell Set-Clipboard → wl-copy → xclip → xsel. Returns True if any backend
    succeeded; callers fall back to OSC 52 on False."""
    for argv, kw in _write_clipboard_commands(text.encode("utf-8")):
        try:
            if subprocess.run(argv, timeout=10, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **kw).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


# ── macOS ────────────────────────────────────────────────────────────────

def _macos_has_image() -> bool:
    return _probe(["osascript", "-e", "clipboard info"], 3,
                  lambda r: "«class PNGf»" in r.stdout or "«class TIFF»" in r.stdout)


def _macos_pngpaste(dest: Path) -> bool:
    """pngpaste (brew install pngpaste) — fastest, cleanest."""
    try:
        r = subprocess.run(["pngpaste", str(dest)], capture_output=True, timeout=3)
        return r.returncode == 0 and _nonempty(dest)
    except FileNotFoundError:
        pass  # pngpaste not installed
    except Exception as e:
        logger.debug("pngpaste failed: %s", e)
    return False


def _macos_osascript(dest: Path) -> bool:
    """osascript PNG extraction (always available)."""
    if not _macos_has_image():
        return False
    script = f'''try
  set imgData to the clipboard as «class PNGf»
  set f to open for access POSIX file "{dest}" with write permission
  write imgData to f
  close access f
on error
  return "fail"
end try
'''
    try:
        r = subprocess.run(["osascript", "-e", script], timeout=5, **_TEXT)
        return r.returncode == 0 and "fail" not in r.stdout and _nonempty(dest)
    except Exception as e:
        logger.debug("osascript clipboard extract failed: %s", e)
    return False


# ── PowerShell (native Windows powershell/pwsh + WSL2 powershell.exe) ─────

_FILEDROP_IMAGE_EXTS = "'.png','.jpg','.jpeg','.gif','.webp','.bmp','.tiff','.tif'"
_PS_FILEDROP_HIT = (
    "try { "
    "$files = Get-Clipboard -Format FileDropList -ErrorAction Stop;"
    f"$exts = @({_FILEDROP_IMAGE_EXTS});"
    "$hit = $files | Where-Object { $exts -contains ([System.IO.Path]::GetExtension($_).ToLowerInvariant()) } | Select-Object -First 1;"
)

# (has_image, extract-as-base64-PNG) script pairs, tried in order.
_PS_IMAGE_STRATEGIES = (
    (  # .NET System.Windows.Forms.Clipboard
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[System.Windows.Forms.Clipboard]::ContainsImage()",
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$img = [System.Windows.Forms.Clipboard]::GetImage();"
        "if ($null -eq $img) { exit 1 }"
        "$ms = New-Object System.IO.MemoryStream;"
        "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png);"
        "[System.Convert]::ToBase64String($ms.ToArray())"),
    (  # Get-Clipboard -Format Image (System.Drawing.Image or WPF BitmapSource)
        "try { "
        "$img = Get-Clipboard -Format Image -ErrorAction Stop;"
        "if ($null -ne $img) { 'True' } else { 'False' }"
        "} catch { 'False' }",
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
        "} catch { exit 1 }"),
    (  # copied image *file* (Explorer file drop)
        _PS_FILEDROP_HIT
        + "if ($null -ne $hit) { 'True' } else { 'False' }"
        "} catch { 'False' }",
        _PS_FILEDROP_HIT
        + "if ($null -eq $hit) { exit 1 }"
        "[System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($hit))"
        "} catch { exit 1 }"))


def _ps_clipboard(exe: str, timeout: int, label: str, dest: Path | None = None) -> bool:
    """Probe (*dest* None) or extract (base64 PNG → *dest*) the Windows clipboard image via *exe*.
    A missing *exe* ends the whole chain (every script needs the same binary); any other failure
    logs (and drops a partial *dest*) then tries the next strategy."""
    for check, extract in _PS_IMAGE_STRATEGIES:
        try:
            argv = [exe, *_PS_FLAGS, "-Command", check if dest is None else extract]
            r = subprocess.run(argv, timeout=timeout, **_TEXT)
            if dest is None:
                if r.returncode == 0 and "True" in r.stdout:
                    return True
            elif r.returncode == 0 and r.stdout.strip():
                dest.write_bytes(base64.b64decode(r.stdout.strip(), validate=True))
                if _nonempty(dest):
                    return True
        except FileNotFoundError:
            logger.debug("%s not found — clipboard unavailable", exe)
            return False
        except Exception as e:
            logger.debug("%s clipboard image %s failed: %s", label,
                         "check" if dest is None else "extraction", e)
            if dest is not None:
                dest.unlink(missing_ok=True)
    return False


_ps_exe: str | None | bool = False  # resolved PowerShell executable; False = not yet checked


def _get_ps_exe() -> str | None:
    """First working PowerShell — ``powershell`` (5.1, always present) or ``pwsh`` (7+,
    optional) — cached per process; None when neither runs."""
    global _ps_exe
    if _ps_exe is False:
        _ps_exe = next((name for name in ("powershell", "pwsh") if _probe(
            [name, *_PS_FLAGS, "-Command", "echo ok"], 5,
            lambda r: r.returncode == 0 and "ok" in r.stdout)), None)
    return _ps_exe


def _windows_has_image() -> bool:
    ps = _get_ps_exe()
    return ps is not None and _ps_clipboard(ps, 5, "Windows")


def _windows_save(dest: Path) -> bool:
    ps = _get_ps_exe()
    if ps is None:
        logger.debug("No PowerShell found — Windows clipboard image paste unavailable")
        return False
    return _ps_clipboard(ps, 15, "Windows", dest)


# ── Linux: WSL (powershell.exe) → Wayland (wl-paste) → X11 (xclip) ───────

def _linux_save(dest: Path) -> bool:
    """Try clipboard backends in priority order: bridge → kitten → then upstream's
    _linux_backends() table (WSL → Wayland → X11)."""
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
    return any(enabled and save(dest) for enabled, _, save in _linux_backends())



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
    return _ps_clipboard("powershell.exe", 8, "WSL")


def _wsl_save(dest: Path) -> bool:
    return _ps_clipboard("powershell.exe", 15, "WSL", dest)


_WAYLAND_MIME_PREFERENCE = ("image/png", "image/jpeg", "image/bmp", "image/gif", "image/webp")
_WL_LIST_TYPES = ["wl-paste", "--list-types"]
_WL_MISSING = "wl-paste not installed — Wayland clipboard unavailable"


def _wayland_has_image() -> bool:
    return _probe(_WL_LIST_TYPES, 3, lambda r: r.returncode == 0 and any(
        t.startswith("image/") for t in r.stdout.splitlines()), missing=_WL_MISSING)


def _wayland_save(dest: Path) -> bool:
    try:
        types_r = subprocess.run(_WL_LIST_TYPES, timeout=3, **_TEXT)
        types = types_r.stdout.splitlines() if types_r.returncode == 0 else ()
        mime = next((m for m in _WAYLAND_MIME_PREFERENCE if m in types), None)  # PNG preferred
        if not mime:
            return False
        # save_clipboard_image() promises a PNG. Wayland can offer JPEG/GIF/WebP/BMP payloads,
        # so every non-PNG result is normalized (and re-verified) before reporting success.
        if _pipe_to_file(["wl-paste", "--type", mime], dest) and (
                mime == "image/png" or (_convert_to_png(dest) and _is_png_file(dest))):
            return True
        dest.unlink(missing_ok=True)
    except FileNotFoundError:
        logger.debug(_WL_MISSING)
    except Exception as e:
        logger.debug("wl-paste clipboard extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False


def _convert_to_png(path: Path) -> bool:
    """Convert an image file to PNG in-place: Pillow first (likely in the venv), then ImageMagick.
    When neither works the file is left as-is — BMP is still usable for most APIs."""
    try:
        from PIL import Image
        Image.open(path).save(path, "PNG")
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("Pillow BMP→PNG conversion failed: %s", e)
    tmp = path.with_suffix(".bmp")
    try:
        path.rename(tmp)
        r = subprocess.run(["convert", str(tmp), "png:" + str(path)], capture_output=True,
                           timeout=5)
        if r.returncode == 0 and _nonempty(path):
            tmp.unlink(missing_ok=True)
            return True
        tmp.rename(path)  # convert failed — restore the original file
    except Exception as e:
        if isinstance(e, FileNotFoundError):
            logger.debug("ImageMagick not installed — cannot convert BMP to PNG")
        else:
            logger.debug("ImageMagick BMP→PNG conversion failed: %s", e)
        if tmp.exists() and not path.exists():
            tmp.rename(path)
    return _nonempty(path)


def _is_png_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(len(_PNG_SIGNATURE)) == _PNG_SIGNATURE
    except OSError:
        return False


_XCLIP_TARGETS = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]


def _xclip_has_image() -> bool:
    return _probe(_XCLIP_TARGETS, 3, lambda r: r.returncode == 0 and "image/png" in r.stdout)


def _xclip_save(dest: Path) -> bool:
    if not _probe(_XCLIP_TARGETS, 3, lambda r: "image/png" in r.stdout,
                  missing="xclip not installed — X11 clipboard image paste unavailable"):
        return False
    try:
        return _pipe_to_file(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], dest)
    except Exception as e:
        logger.debug("xclip image extraction failed: %s", e)
        dest.unlink(missing_ok=True)
    return False
