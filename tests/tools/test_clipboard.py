"""Tests for clipboard image paste — clipboard extraction, multimodal conversion,
and CLI integration.

Coverage:
  hermes_cli/clipboard.py  — platform-specific image extraction (macOS, WSL, Wayland, X11)
  cli.py                   — _try_attach_clipboard_image, _build_multimodal_content,
                              image attachment state, queue tuple routing
"""

import base64
import json
import os
import queue
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

from hermes_cli.clipboard import (
    save_clipboard_image,
    has_clipboard_image,
    _is_wsl,
    _linux_save,
    _macos_pngpaste,
    _macos_osascript,
    _macos_has_image,
    _xclip_save,
    _xclip_has_image,
    _wsl_save,
    _wsl_has_image,
    _wayland_save,
    _wayland_has_image,
    _windows_save,
    _windows_has_image,
    _convert_to_png,
    _kitten_save,
    _kitten_has_image,
    _kitten_preferred,
    _bridge_save,
    _bridge_has_image,
    _bridge_request,
    _bridge_sock_path,
)
from cli import _should_auto_attach_clipboard_image_on_paste

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
FAKE_BMP = b"BM" + b"\x00" * 100
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
FAKE_PNG_B64 = base64.b64encode(FAKE_PNG).decode()


# ═════════════════════════════════════════════════════════════════════════
# Level 1: Clipboard module — platform dispatch + tool interactions
# ═════════════════════════════════════════════════════════════════════════

class TestSaveClipboardImage:
    def test_creates_parent_dirs(self, tmp_path):
        dest = tmp_path / "deep" / "nested" / "out.png"
        with patch("hermes_cli.clipboard.sys") as mock_sys:
            mock_sys.platform = "linux"
            with patch("hermes_cli.clipboard._linux_save", return_value=False):
                save_clipboard_image(dest)
        assert dest.parent.exists()


# ── macOS ────────────────────────────────────────────────────────────────

class TestMacosPngpaste:
    def test_success_writes_file(self, tmp_path):
        dest = tmp_path / "out.png"
        def fake_run(cmd, **kw):
            dest.write_bytes(FAKE_PNG)
            return MagicMock(returncode=0)
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _macos_pngpaste(dest) is True
        assert dest.stat().st_size == len(FAKE_PNG)

    def test_empty_file_rejected(self, tmp_path):
        dest = tmp_path / "out.png"
        def fake_run(cmd, **kw):
            dest.write_bytes(b"")
            return MagicMock(returncode=0)
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _macos_pngpaste(dest) is False


class TestMacosHasImage:
    @pytest.mark.parametrize("stdout, expected", [
        ("«class PNGf», «class ut16»", True),
        ("«class ut16», «class utf8»", False),
    ])
    def test_image_class_detection(self, stdout, expected):
        with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=stdout, returncode=0)
            assert _macos_has_image() is expected


class TestMacosOsascript:
    def test_success_with_png(self, tmp_path):
        dest = tmp_path / "out.png"
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                return MagicMock(stdout="«class PNGf», «class ut16»", returncode=0)
            dest.write_bytes(FAKE_PNG)
            return MagicMock(stdout="", returncode=0)
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _macos_osascript(dest) is True
        assert dest.stat().st_size > 0

    def test_extraction_returns_fail(self, tmp_path):
        dest = tmp_path / "out.png"
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                return MagicMock(stdout="«class PNGf»", returncode=0)
            return MagicMock(stdout="fail", returncode=0)
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _macos_osascript(dest) is False


# ── WSL detection ────────────────────────────────────────────────────────

class TestIsWsl:
    def setup_method(self):
        # _is_wsl is hermes_constants.is_wsl; reset the function's own module
        # globals so this stays stable even if hermes_constants was imported
        # through a different module object earlier in a large xdist run.
        import hermes_constants
        hermes_constants._wsl_detected = None
        _is_wsl.__globals__["_wsl_detected"] = None

    def teardown_method(self):
        # Reset again after the test so we don't leak a cached value
        # (True/False) into whichever test the xdist worker runs next.
        import hermes_constants
        hermes_constants._wsl_detected = None
        _is_wsl.__globals__["_wsl_detected"] = None

    @pytest.mark.parametrize("content, expected", [
        ("Linux version 5.15.0 (microsoft-standard-WSL2)", True),
        # GHA hosted runners are Azure VMs whose real /proc/version often
        # contains "microsoft", so the patched `open` must actually be reached
        # (setup_method clears the cache that would short-circuit it).
        ("Linux version 6.14.0-37-generic (buildd@lcy02-amd64-049)", False),
    ])
    def test_detection_from_proc_version(self, content, expected):
        with patch.dict(_is_wsl.__globals__, {"open": mock_open(read_data=content)}):
            assert _is_wsl() is expected


    def test_result_is_cached(self):
        content = "Linux version 5.15.0 (microsoft-standard-WSL2)"
        opener = mock_open(read_data=content)
        with patch.dict(_is_wsl.__globals__, {"open": opener}):
            assert _is_wsl() is True
            assert _is_wsl() is True
            opener.assert_called_once()  # only read once


# ── WSL (powershell.exe) ────────────────────────────────────────────────

class TestWslHasImage:
    @pytest.mark.parametrize("stdout, expected", [
        ("True\n", True),
        ("False\n", False),
    ])
    def test_clipboard_image_probe(self, stdout, expected):
        with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=stdout, returncode=0)
            assert _wsl_has_image() is expected

    def test_falls_back_to_get_clipboard_image(self):
        with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="False\n", returncode=0),
                MagicMock(stdout="True\n", returncode=0),
            ]
            assert _wsl_has_image() is True
            assert mock_run.call_count == 2


class TestWslSave:
    def test_successful_extraction(self, tmp_path):
        dest = tmp_path / "out.png"
        b64_png = base64.b64encode(FAKE_PNG).decode()
        with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=b64_png + "\n", returncode=0)
            assert _wsl_save(dest) is True
        assert dest.read_bytes() == FAKE_PNG


    def test_invalid_base64(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="not-valid-base64!!!", returncode=0)
            assert _wsl_save(dest) is False


# ── Wayland (wl-paste) ──────────────────────────────────────────────────

class TestWaylandHasImage:
    @pytest.mark.parametrize("types, expected", [
        ("image/png\ntext/plain\n", True),
        ("text/html\nimage/bmp\n", True),   # non-PNG image types count too
        ("text/plain\ntext/html\n", False),
    ])
    def test_type_list_detection(self, types, expected):
        with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=types, returncode=0)
            assert _wayland_has_image() is expected


class TestWaylandSave:
    def test_png_extraction(self, tmp_path):
        dest = tmp_path / "out.png"
        def fake_run(cmd, **kw):
            if "--list-types" in cmd:
                return MagicMock(stdout="image/png\ntext/plain\n", returncode=0)
            # Extract call — write fake data to stdout file
            if "stdout" in kw and hasattr(kw["stdout"], "write"):
                kw["stdout"].write(FAKE_PNG)
            return MagicMock(returncode=0)
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _wayland_save(dest) is True
        assert dest.stat().st_size > 0


    def test_prefers_png_over_bmp(self, tmp_path):
        """When both PNG and BMP are available, PNG should be preferred."""
        dest = tmp_path / "out.png"
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "--list-types" in cmd:
                return MagicMock(
                    stdout="image/bmp\nimage/png\ntext/plain\n", returncode=0
                )
            if "stdout" in kw and hasattr(kw["stdout"], "write"):
                kw["stdout"].write(FAKE_PNG)
            return MagicMock(returncode=0)
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _wayland_save(dest) is True
        # Verify PNG was requested, not BMP
        extract_cmd = calls[1]
        assert "image/png" in extract_cmd


# ── X11 (xclip) ─────────────────────────────────────────────────────────

class TestXclipHasImage:
    @pytest.mark.parametrize("targets, expected", [
        ("image/png\ntext/plain\n", True),
        ("text/plain\n", False),
    ])
    def test_targets_detection(self, targets, expected):
        with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=targets, returncode=0)
            assert _xclip_has_image() is expected


class TestXclipSave:
    def test_image_extraction_success(self, tmp_path):
        dest = tmp_path / "out.png"
        def fake_run(cmd, **kw):
            if "TARGETS" in cmd:
                return MagicMock(stdout="image/png\ntext/plain\n", returncode=0)
            if "stdout" in kw and hasattr(kw["stdout"], "write"):
                kw["stdout"].write(FAKE_PNG)
            return MagicMock(returncode=0)
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _xclip_save(dest) is True
        assert dest.stat().st_size > 0

    def test_extraction_fails_cleans_up(self, tmp_path):
        dest = tmp_path / "out.png"
        def fake_run(cmd, **kw):
            if "TARGETS" in cmd:
                return MagicMock(stdout="image/png\n", returncode=0)
            raise subprocess.SubprocessError("pipe broke")
        with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run):
            assert _xclip_save(dest) is False
        assert not dest.exists()


# ── Linux dispatch ──────────────────────────────────────────────────────

class TestLinuxSave:
    """Test that _linux_save dispatches correctly to bridge → kitten → WSL → Wayland → X11."""

    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._wsl_detected = None
        cb._kitten_unavailable = False
        cb._bridge_unavailable = False

    def teardown_method(self):
        import hermes_cli.clipboard as cb
        cb._kitten_unavailable = False
        cb._bridge_unavailable = False

    def test_wsl_tried_first_when_kitten_absent(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_save", return_value=None):
            with patch("hermes_cli.clipboard._kitten_save", return_value=False):
                with patch("hermes_cli.clipboard._is_wsl", return_value=True):
                    with patch("hermes_cli.clipboard._wsl_save", return_value=True) as m:
                        assert _linux_save(dest) is True
                        m.assert_called_once_with(dest)

    def test_wayland_fails_falls_through_to_xclip(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_save", return_value=None):
            with patch("hermes_cli.clipboard._kitten_save", return_value=False):
                with patch("hermes_cli.clipboard._is_wsl", return_value=False):
                    with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                        with patch("hermes_cli.clipboard._wayland_save", return_value=False):
                            with patch("hermes_cli.clipboard._xclip_save", return_value=True) as m:
                                assert _linux_save(dest) is True
                                m.assert_called_once_with(dest)

    def test_kitten_tried_first_on_headless(self, tmp_path):
        """The bug case: SSH vars stripped by `su -`, no DISPLAY/WAYLAND —
        kitten is the only backend that can reach the user's clipboard
        when the bridge (reverse-tunnel) is unavailable."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_save", return_value=None):
            with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten"):
                with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                    def fake_run(cmd, **kw):
                        dest.write_bytes(FAKE_PNG)
                        return MagicMock(returncode=0)
                    mock_run.side_effect = fake_run
                    assert _linux_save(dest) is True
        assert dest.stat().st_size == len(FAKE_PNG)
        assert mock_run.call_count == 1

    def test_kitten_success_skips_native_backends(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_save", return_value=None):
            with patch("hermes_cli.clipboard._kitten_save", return_value=True) as kitten:
                with patch("hermes_cli.clipboard._is_wsl", return_value=True) as wsl:
                    with patch("hermes_cli.clipboard._wsl_save") as wsl_save:
                        assert _linux_save(dest) is True
                        kitten.assert_called_once_with(dest)
                        wsl.assert_not_called()
                        wsl_save.assert_not_called()

    def test_kitten_timeout_falls_through_and_caches(self, tmp_path):
        """Non-kitty far-end terminal: kitten hangs → TimeoutExpired → fall
        through, and cache the negative so the next attempt skips kitten."""
        dest = tmp_path / "out.png"
        import hermes_cli.clipboard as cb
        with patch("hermes_cli.clipboard._bridge_save", return_value=None):
            with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten"):
                with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.TimeoutExpired("kitten", 3)
                    with patch("hermes_cli.clipboard._xclip_save", return_value=False):
                        assert _linux_save(dest) is False
            # Second call: kitten must be skipped via the negative cache
            with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten") as find:
                with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.TimeoutExpired("kitten", 3)
                    with patch("hermes_cli.clipboard._xclip_save", return_value=True):
                        assert _linux_save(dest) is True
                        find.assert_not_called()
        assert cb._kitten_unavailable is True

    def test_bridge_success_short_circuits(self, tmp_path):
        """Bridge succeeds → kitten (and everything after it) is never tried."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_save", return_value=True):
            with patch("hermes_cli.clipboard._kitten_save") as kitten:
                assert _linux_save(dest) is True
                kitten.assert_not_called()

    def test_bridge_unavailable_falls_back_to_kitten(self, tmp_path):
        """Bridge unavailable (None, e.g. plain SSH with no tunnel) → kitten
        is tried and can still succeed."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_save", return_value=None):
            with patch("hermes_cli.clipboard._kitten_save", return_value=True) as kitten:
                assert _linux_save(dest) is True
                kitten.assert_called_once_with(dest)

    def test_bridge_definitive_no_image_skips_kitten_but_tries_wayland(self, tmp_path):
        """Bridge answers a DEFINITIVE 'no image at the far end' (False) →
        kitten is skipped (it would read the same far-end clipboard and just
        burn its timeout), but the local-desktop backends (which read a
        DIFFERENT clipboard) are still reachable."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_save", return_value=False):
            with patch("hermes_cli.clipboard._kitten_save") as kitten:
                with patch("hermes_cli.clipboard._is_wsl", return_value=False):
                    with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                        with patch("hermes_cli.clipboard._wayland_save", return_value=True) as wayland:
                            assert _linux_save(dest) is True
                            kitten.assert_not_called()
                            wayland.assert_called_once_with(dest)

    def test_bridge_unavailable_cache_skips_repeat_socket_attempts(self, tmp_path):
        """Mirrors test_kitten_timeout_falls_through_and_caches: a socket
        timeout on the first call caches _bridge_unavailable so a second
        call in the same process skips the socket attempt entirely."""
        dest = tmp_path / "out.png"
        import hermes_cli.clipboard as cb
        with patch("hermes_cli.clipboard._bridge_sock_path", return_value=str(tmp_path / "nope.sock")):
            with patch("hermes_cli.clipboard.socket.socket") as mock_socket_cls:
                mock_sock = MagicMock()
                mock_sock.recv.side_effect = TimeoutError()
                mock_socket_cls.return_value = mock_sock
                with patch("hermes_cli.clipboard._kitten_save", return_value=False):
                    with patch("hermes_cli.clipboard._xclip_save", return_value=False):
                        assert _linux_save(dest) is False
        assert cb._bridge_unavailable is True
        # Second call: bridge socket must not be touched again
        with patch("hermes_cli.clipboard.socket.socket") as mock_socket_cls:
            with patch("hermes_cli.clipboard._kitten_save", return_value=True) as kitten:
                assert _linux_save(dest) is True
                kitten.assert_called_once_with(dest)
                mock_socket_cls.assert_not_called()


class TestBridgeSave:
    """Direct tests for the reverse-tunnel clipboard bridge's save path.

    Most cases mock `_bridge_request` (unit level); the socket-framing
    itself is exercised for real in TestBridgeEndToEnd below.
    """

    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._bridge_unavailable = False

    def teardown_method(self):
        import hermes_cli.clipboard as cb
        cb._bridge_unavailable = False

    def test_success_writes_file(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 0, "png_b64": FAKE_PNG_B64}
            assert _bridge_save(dest) is True
        assert dest.read_bytes() == FAKE_PNG

    def test_definitive_no_image_returns_false(self, tmp_path):
        """rc=1 means the far-end Mac clipboard definitively has no image —
        distinct from bridge-unavailable (None), so kitten (same far-end
        clipboard) can be skipped by the caller."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 1, "stderr": "no image"}
            assert _bridge_save(dest) is False
        assert not dest.exists()

    def test_screen_locked_returns_none(self, tmp_path):
        """rc=3 (screen-locked refusal) is bridge-unavailable, not a
        definitive no-image — the caller should still fall back to kitten."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 3, "stderr": "screen locked"}
            assert _bridge_save(dest) is None
        assert not dest.exists()

    def test_socket_missing_returns_none_no_exception(self, tmp_path):
        """Real (unmocked) socket connect against a nonexistent path must
        surface as None, never raise — the bridge simply isn't reachable."""
        dest = tmp_path / "out.png"
        nonexistent = tmp_path / "no-such-broker.sock"
        with patch("hermes_cli.clipboard._bridge_sock_path", return_value=str(nonexistent)):
            assert _bridge_save(dest) is None
        assert not dest.exists()

    def test_timeout_returns_none_and_caches(self, tmp_path):
        """A socket-level timeout (transport stall) must return None AND set
        the process-lifetime negative cache — mirrors kitten's own timeout
        cache.  Connect-refusal must NOT set it (see test below)."""
        dest = tmp_path / "out.png"
        import hermes_cli.clipboard as cb
        with patch("hermes_cli.clipboard.socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = TimeoutError()
            mock_socket_cls.return_value = mock_sock
            assert _bridge_save(dest) is None
        assert cb._bridge_unavailable is True

    def test_connect_refused_does_not_cache(self, tmp_path):
        """ECONNREFUSED (socket present, nothing listening — e.g. broker
        mid-restart) must NOT set the negative cache: the tunnel may come
        back within the process lifetime."""
        dest = tmp_path / "out.png"
        import hermes_cli.clipboard as cb
        with patch("hermes_cli.clipboard.socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = ConnectionRefusedError()
            mock_socket_cls.return_value = mock_sock
            assert _bridge_save(dest) is None
        assert cb._bridge_unavailable is False

    def test_legacy_response_without_png_b64_returns_none(self, tmp_path):
        """The old-broker-over-tunnel trap: a legacy broker that doesn't
        understand 'type' answers rc=2 with stdout/stderr but no png_b64 —
        the client must treat this as bridge-unavailable, never crash on
        the missing field."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 2, "stdout": "", "stderr": "op-broker: empty args"}
            assert _bridge_save(dest) is None
        assert not dest.exists()

    def test_invalid_base64_returns_none(self, tmp_path):
        """Spec: png_b64 that fails strict base64 validation
        (base64.b64decode(..., validate=True)) is a malformed response,
        same bucket as any other bridge failure — None, dest not left
        behind."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 0, "png_b64": "not-valid-base64!!!"}
            assert _bridge_save(dest) is None
        assert not dest.exists()

    def test_rc0_empty_png_b64_not_success(self, tmp_path):
        """rc=0 but whitespace-only png_b64 must not be treated as success
        (mirrors the kitten rc0-but-empty-file guard)."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 0, "png_b64": "   "}
            assert _bridge_save(dest) is None
        assert not dest.exists()

    def test_unavailable_cache_skips_repeat_calls(self, tmp_path):
        dest = tmp_path / "out.png"
        import hermes_cli.clipboard as cb
        cb._bridge_unavailable = True
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            assert _bridge_save(dest) is None
            mock_req.assert_not_called()


class TestBridgeHasImage:
    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._bridge_unavailable = False

    def teardown_method(self):
        import hermes_cli.clipboard as cb
        cb._bridge_unavailable = False

    def test_has_image_true(self):
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 0, "has_image": True}
            assert _bridge_has_image() is True

    def test_has_image_false(self):
        """A definitive 'no image' — distinct from unavailable (None)."""
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 0, "has_image": False}
            assert _bridge_has_image() is False

    def test_missing_field_returns_none(self):
        """Legacy broker response with no has_image key → unavailable."""
        with patch("hermes_cli.clipboard._bridge_request") as mock_req:
            mock_req.return_value = {"returncode": 2, "stdout": "", "stderr": "op-broker: empty args"}
            assert _bridge_has_image() is None

    def test_timeout_returns_none(self, tmp_path):
        import hermes_cli.clipboard as cb
        with patch("hermes_cli.clipboard.socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = TimeoutError()
            mock_socket_cls.return_value = mock_sock
            assert _bridge_has_image() is None
        assert cb._bridge_unavailable is True


class TestBridgeEndToEnd:
    """Exercises the REAL socket code path (framing/newline/JSON) against a
    throwaway Unix socket server — catches request/response framing bugs
    that mocking `_bridge_request` can't."""

    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._bridge_unavailable = False
        self._server = None
        self._thread = None
        # AF_UNIX paths are capped at ~104 bytes on macOS/BSD; pytest's
        # tmp_path (deep under /private/var/folders/...) routinely exceeds
        # that, so the throwaway socket lives directly under /tmp instead.
        import tempfile
        self._sock_dir = tempfile.mkdtemp(prefix="cbbridge-")

    def teardown_method(self):
        import hermes_cli.clipboard as cb
        cb._bridge_unavailable = False
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        import shutil as _shutil
        _shutil.rmtree(self._sock_dir, ignore_errors=True)

    def _start_server(self, sock_path, response_bytes):
        """Spin up a bounded, single-purpose Unix socket server that reads
        one newline-terminated request and writes back *response_bytes*."""
        received = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline()
                received.append(line)
                self.wfile.write(response_bytes)

        class Server(socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        server = Server(sock_path, Handler)
        server.timeout = 5  # bounded — never hangs the test
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return received

    def test_real_socket_round_trip_clipboard_read(self, tmp_path):
        sock_path = os.path.join(self._sock_dir, "broker.sock")
        response = (json.dumps({"returncode": 0, "png_b64": FAKE_PNG_B64}) + "\n").encode("utf-8")
        received = self._start_server(sock_path, response)
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_sock_path", return_value=sock_path):
            assert _bridge_save(dest) is True
        assert dest.read_bytes() == FAKE_PNG
        # Verify the request we actually sent was well-formed JSON + newline
        assert len(received) == 1
        assert json.loads(received[0].decode("utf-8")) == {"type": "clipboard_read"}

    def test_real_socket_round_trip_has_image(self, tmp_path):
        sock_path = os.path.join(self._sock_dir, "broker.sock")
        response = (json.dumps({"returncode": 0, "has_image": True}) + "\n").encode("utf-8")
        received = self._start_server(sock_path, response)
        with patch("hermes_cli.clipboard._bridge_sock_path", return_value=sock_path):
            assert _bridge_has_image() is True
        assert json.loads(received[0].decode("utf-8")) == {"type": "clipboard_has_image"}

    def test_real_socket_legacy_broker_response(self, tmp_path):
        """A real (unmocked) socket answering the OLD broker's shape — no
        'type' understanding, no png_b64/has_image field — must resolve to
        None end-to-end, not raise."""
        sock_path = os.path.join(self._sock_dir, "broker.sock")
        response = (
            json.dumps({"returncode": 2, "stdout": "", "stderr": "op-broker: empty args"}) + "\n"
        ).encode("utf-8")
        self._start_server(sock_path, response)
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._bridge_sock_path", return_value=sock_path):
            assert _bridge_save(dest) is None
        assert not dest.exists()


class TestKittenSave:
    """Direct tests for the kitten (kitty OSC 5522) backend."""

    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._kitten_unavailable = False

    def teardown_method(self):
        import hermes_cli.clipboard as cb
        cb._kitten_unavailable = False

    def test_success_writes_file(self, tmp_path):
        dest = tmp_path / "out.png"
        def fake_run(cmd, **kw):
            dest.write_bytes(FAKE_PNG)
            return MagicMock(returncode=0)
        with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten"):
            with patch("hermes_cli.clipboard.subprocess.run", side_effect=fake_run) as mock_run:
                assert _kitten_save(dest) is True
        assert dest.stat().st_size == len(FAKE_PNG)
        assert mock_run.call_args[0][0] == ["/usr/bin/kitten", "clipboard", "-g", str(dest)]

    def test_rc0_but_no_file_fails(self, tmp_path):
        """kitten exited 0 but produced nothing — must not report success."""
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                assert _kitten_save(dest) is False
        assert not dest.exists()

    def test_kitten_not_found_falls_through(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._find_kitten", return_value=None):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                assert _kitten_save(dest) is False
                mock_run.assert_not_called()

    def test_timeout_returns_false(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.TimeoutExpired("kitten", 3)
                assert _kitten_save(dest) is False

    def test_display_set_skips_kitten(self, tmp_path):
        """Local desktop / X-forwarded session: native tools own the
        clipboard; kitten must not be tried (avoids a needless stall when
        the far end isn't kitty but xclip/wl-paste work)."""
        dest = tmp_path / "out.png"
        with patch.dict(os.environ, {"DISPLAY": ":0"}):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                assert _kitten_save(dest) is False
                mock_run.assert_not_called()

    def test_wayland_set_skips_kitten(self, tmp_path):
        dest = tmp_path / "out.png"
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                assert _kitten_save(dest) is False
                mock_run.assert_not_called()


class TestKittenHasImage:
    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._kitten_unavailable = False

    def teardown_method(self):
        import hermes_cli.clipboard as cb
        cb._kitten_unavailable = False

    @pytest.mark.parametrize("stdout, expected", [
        ("image/png\ntext/plain\n", True),
        ("text/plain\ntext/html\n", False),
        ("", False),
    ])
    def test_mime_list_detection(self, stdout, expected):
        with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout=stdout, returncode=0)
                assert _kitten_has_image() is expected

    def test_timeout_returns_false(self):
        with patch("hermes_cli.clipboard._find_kitten", return_value="/usr/bin/kitten"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.TimeoutExpired("kitten", 3)
                assert _kitten_has_image() is False

    def test_display_set_skips_kitten(self):
        with patch.dict(os.environ, {"DISPLAY": ":0"}):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                assert _kitten_has_image() is False
                mock_run.assert_not_called()


class TestKittenPreferred:
    """Gate contract: only headless Linux sessions prefer the
    terminal-emulator path."""

    def setup_method(self):
        import hermes_constants
        hermes_constants._wsl_detected = None

    def teardown_method(self):
        import hermes_constants
        hermes_constants._wsl_detected = None

    def test_headless_true(self):
        assert _kitten_preferred({"DISPLAY": "", "WAYLAND_DISPLAY": ""}) is True

    def test_display_set_false(self):
        assert _kitten_preferred({"DISPLAY": ":0"}) is False

    def test_wayland_set_false(self):
        assert _kitten_preferred({"WAYLAND_DISPLAY": "wayland-0"}) is False

    def test_wsl_false_even_when_headless(self):
        with patch("hermes_cli.clipboard._is_wsl", return_value=True):
            assert _kitten_preferred({"DISPLAY": "", "WAYLAND_DISPLAY": ""}) is False

    def test_env_argument_overrides_os_environ(self):
        with patch.dict(os.environ, {"DISPLAY": ":0"}):
            with patch("hermes_cli.clipboard._is_wsl", return_value=False):
                assert _kitten_preferred({"DISPLAY": "", "WAYLAND_DISPLAY": ""}) is True


# ── Native Windows (PowerShell) ─────────────────────────────────────────

class TestWindowsHasImage:
    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._ps_exe = False  # reset cache

    def test_clipboard_has_image(self):
        with patch("hermes_cli.clipboard._get_ps_exe", return_value="powershell"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="True\n", returncode=0)
                assert _windows_has_image() is True

    def test_falls_back_to_get_clipboard_image(self):
        with patch("hermes_cli.clipboard._get_ps_exe", return_value="powershell"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(stdout="False\n", returncode=0),
                    MagicMock(stdout="True\n", returncode=0),
                ]
                assert _windows_has_image() is True
                assert mock_run.call_count == 2


class TestWindowsSave:
    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._ps_exe = False  # reset cache

    def test_successful_extraction(self, tmp_path):
        dest = tmp_path / "out.png"
        b64_png = base64.b64encode(FAKE_PNG).decode()
        with patch("hermes_cli.clipboard._get_ps_exe", return_value="powershell"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout=b64_png + "\n", returncode=0)
                assert _windows_save(dest) is True
        assert dest.read_bytes() == FAKE_PNG

    def test_falls_back_to_filedrop_image(self, tmp_path):
        dest = tmp_path / "out.png"
        b64_png = base64.b64encode(FAKE_PNG).decode()
        with patch("hermes_cli.clipboard._get_ps_exe", return_value="powershell"):
            with patch("hermes_cli.clipboard.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    MagicMock(stdout="", returncode=1),
                    MagicMock(stdout="", returncode=1),
                    MagicMock(stdout=b64_png + "\n", returncode=0),
                ]
                assert _windows_save(dest) is True
                assert mock_run.call_count == 3
        assert dest.read_bytes() == FAKE_PNG


# ── BMP conversion ──────────────────────────────────────────────────────

class TestConvertToPng:
    def test_pillow_conversion(self, tmp_path):
        dest = tmp_path / "img.png"
        dest.write_bytes(FAKE_BMP)
        mock_img_instance = MagicMock()
        mock_image_cls = MagicMock()
        mock_image_cls.open.return_value = mock_img_instance
        # `from PIL import Image` fetches PIL.Image from the PIL module
        mock_pil_module = MagicMock()
        mock_pil_module.Image = mock_image_cls
        with patch.dict(sys.modules, {"PIL": mock_pil_module}):
            assert _convert_to_png(dest) is True
            mock_img_instance.save.assert_called_once_with(dest, "PNG")


    @pytest.mark.parametrize("failure", ["nonzero-exit", "timeout"])
    def test_imagemagick_failure_preserves_original(self, tmp_path, failure):
        """When ImageMagick can't convert, the original file must not be lost."""
        dest = tmp_path / "img.png"
        dest.write_bytes(FAKE_BMP)

        side_effect = (
            (lambda cmd, **kw: MagicMock(returncode=1))
            if failure == "nonzero-exit"
            else subprocess.TimeoutExpired("convert", 5)
        )

        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            with patch("hermes_cli.clipboard.subprocess.run", side_effect=side_effect):
                _convert_to_png(dest)

        # Original file must still exist with original content
        assert dest.exists(), "Original file was lost after failed conversion"
        assert dest.read_bytes() == FAKE_BMP


# ── has_clipboard_image dispatch ─────────────────────────────────────────

class TestHasClipboardImage:
    def setup_method(self):
        import hermes_cli.clipboard as cb
        cb._wsl_detected = None

    @pytest.mark.macos_only
    def test_macos_dispatch(self):
        """Faking darwin selected the branch but left `_macos_has_image`'s real
        facility (osascript) absent — only a real macOS host has it."""
        with patch("hermes_cli.clipboard._macos_has_image", return_value=True) as m:
            assert has_clipboard_image() is True
            m.assert_called_once()

    @pytest.mark.linux_only
    def test_wsl_falls_through_to_wayland_when_windows_path_empty(self):
        """WSLg often bridges images to wl-paste even when powershell.exe check fails.

        WSL is Linux, so the host reaches the fallthrough on its own; only the
        WSL/Wayland environment probes below are stubbed.
        """
        with patch("hermes_cli.clipboard._is_wsl", return_value=True):
            with patch("hermes_cli.clipboard._wsl_has_image", return_value=False) as wsl:
                with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                    with patch("hermes_cli.clipboard._wayland_has_image", return_value=True) as wl:
                        assert has_clipboard_image() is True
                        wsl.assert_called_once()
                        wl.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════
# Level 2: _preprocess_images_with_vision — image → text via vision tool
# ═════════════════════════════════════════════════════════════════════════

class TestPreprocessImagesWithVision:
    """Test vision-based image pre-processing for the CLI."""

    @pytest.fixture
    def cli(self):
        """Minimal HermesCLI with mocked internals."""
        with patch("cli.load_cli_config") as mock_cfg:
            mock_cfg.return_value = {
                "model": {"default": "test/model", "base_url": "http://x", "provider": "auto"},
                "terminal": {"timeout": 60},
                "browser": {},
                "compression": {"enabled": True},
                "agent": {"max_turns": 10},
                "display": {"compact": True},
                "clarify": {},
                "code_execution": {},
                "delegation": {},
            }
            with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
                with patch("cli.CLI_CONFIG", mock_cfg.return_value):
                    from cli import HermesCLI
                    cli_obj = HermesCLI.__new__(HermesCLI)
                    # Manually init just enough state
                    cli_obj._attached_images = []
                    cli_obj._image_counter = 0
                    return cli_obj

    def _make_image(self, tmp_path, name="test.png", content=FAKE_PNG):
        img = tmp_path / name
        img.write_bytes(content)
        return img

    def _mock_vision_success(self, description="A test image with colored pixels."):
        """Return an async mock that simulates a successful vision_analyze_tool call."""
        import json
        async def _fake_vision(**kwargs):
            return json.dumps({"success": True, "analysis": description})
        return _fake_vision

    def test_single_image_with_text(self, cli, tmp_path):
        img = self._make_image(tmp_path)
        with patch("tools.vision_tools.vision_analyze_tool", side_effect=self._mock_vision_success()):
            result = cli._preprocess_images_with_vision("Describe this", [img])

        assert isinstance(result, str)
        assert "A test image with colored pixels." in result
        assert "Describe this" in result
        assert str(img) in result
        assert "base64," not in result  # no raw base64 image content


    def test_vision_exception_includes_path(self, cli, tmp_path):
        img = self._make_image(tmp_path)
        async def _explode(**kwargs):
            raise RuntimeError("API down")
        with patch("tools.vision_tools.vision_analyze_tool", side_effect=_explode):
            result = cli._preprocess_images_with_vision("check this", [img])
        assert isinstance(result, str)
        assert str(img) in result  # path still included for retry


# ═════════════════════════════════════════════════════════════════════════
# Level 3: _try_attach_clipboard_image — state management
# ═════════════════════════════════════════════════════════════════════════

class TestTryAttachClipboardImage:
    """Test the clipboard → state flow."""

    @pytest.fixture
    def cli(self):
        from cli import HermesCLI
        cli_obj = HermesCLI.__new__(HermesCLI)
        cli_obj._attached_images = []
        cli_obj._image_counter = 0
        return cli_obj

    def test_image_found_attaches(self, cli):
        with patch("hermes_cli.clipboard.save_clipboard_image", return_value=True):
            result = cli._try_attach_clipboard_image()
        assert result is True
        assert len(cli._attached_images) == 1
        assert cli._image_counter == 1


    def test_image_path_follows_naming_convention(self, cli):
        with patch("hermes_cli.clipboard.save_clipboard_image", return_value=True):
            cli._try_attach_clipboard_image()
        path = cli._attached_images[0]
        assert path.parent == Path(os.environ["HERMES_HOME"]) / "images"
        assert path.name.startswith("clip_")
        assert path.suffix == ".png"


class TestAutoAttachClipboardImageOnPaste:
    @pytest.mark.parametrize("pasted, expected", [
        ("  hello world  ", False),   # real text paste — don't hijack it
        ("   \n\t  ", True),          # whitespace-only paste may be an image
    ])
    def test_auto_attach_decision(self, pasted, expected):
        assert _should_auto_attach_clipboard_image_on_paste(pasted) is expected


class TestVoiceSubmission:
    @pytest.fixture
    def cli(self):
        from cli import HermesCLI
        cli_obj = HermesCLI.__new__(HermesCLI)
        cli_obj._attached_images = [Path("/tmp/stale.png")]
        cli_obj._pending_input = queue.Queue()
        cli_obj._voice_lock = MagicMock()
        cli_obj._voice_processing = True
        cli_obj._voice_recording = True
        cli_obj._voice_continuous = False
        cli_obj._no_speech_count = 0
        cli_obj._voice_recorder = MagicMock()
        cli_obj._voice_recorder.stop.return_value = "/tmp/fake.wav"
        cli_obj._app = None
        return cli_obj

    def test_voice_transcript_clears_stale_attached_images(self, cli):
        with patch("tools.voice_mode.play_beep"):
            with patch("tools.voice_mode.transcribe_recording", return_value={"success": True, "transcript": "hello"}):
                with patch("os.path.isfile", return_value=False):
                    with patch("cli._cprint"):
                        cli._voice_stop_and_transcribe()

        assert cli._attached_images == []
        queued = cli._pending_input.get_nowait()
        # Voice transcripts are wrapped in the _VoiceInputMessage sentinel
        # (#65827) so process_loop can distinguish STT output from typed text.
        from cli import _VoiceInputMessage
        assert isinstance(queued, _VoiceInputMessage)
        assert queued.text == "hello"
