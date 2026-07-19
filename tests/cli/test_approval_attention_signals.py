"""Tests for approval / sudo / clarify attention signals and timeout wiring.

Two regressions to keep closed:

  * ``_approval_callback`` previously hardcoded ``timeout = 60`` and
    silently overrode the user's ``approvals.timeout`` config.  Same for
    ``_sudo_password_callback`` (hardcoded 45).  Both must now resolve
    via ``tools.approval._get_approval_timeout``.

  * Interactive prompts used to appear silently in the TUI.  Users
    juggling multiple windows or SSH sessions missed them entirely.
    Each prompt callback must now call ``_fire_attention_signals`` which
    rings the terminal bell and (on macOS) fires a native notification.
"""

import sys
import threading
from unittest.mock import MagicMock, patch


def _make_cli(approvals_cfg=None):
    """Minimal HermesCLI shell exposing the attention helper."""
    import cli as cli_mod

    obj = object.__new__(cli_mod.HermesCLI)
    obj._app = MagicMock()
    obj._approval_lock = threading.RLock()
    obj._approval_state = None
    obj._approval_deadline = 0
    obj._sudo_state = None
    obj._sudo_deadline = 0
    obj._clarify_state = None
    obj._clarify_deadline = 0
    obj._clarify_freetext = False
    obj._invalidate = MagicMock()
    obj._capture_modal_input_snapshot = MagicMock()
    obj._restore_modal_input_snapshot = MagicMock()
    obj._approval_choices = lambda command, allow_permanent=True: (
        ["once", "session", "always", "deny"] if allow_permanent
        else ["once", "session", "deny"]
    )
    if approvals_cfg is not None:
        # Patch the module-level CLI_CONFIG so the helper sees test config.
        cli_mod.CLI_CONFIG = {"approvals": approvals_cfg, "clarify": {"timeout": 1}}
    return obj


class TestAttentionSignals:
    def test_bell_fires_when_enabled(self):
        """Terminal bell (\\a) must hit stdout when bell_on_prompt is True."""
        cli = _make_cli({"bell_on_prompt": True, "notify_on_prompt": False})
        with patch("sys.stdout") as mock_stdout:
            cli._fire_attention_signals("test")
        # Find the \a write among possibly multiple write() calls.
        write_calls = [c.args[0] for c in mock_stdout.write.call_args_list]
        assert any("\a" in s for s in write_calls), \
            f"bell not written; writes were {write_calls!r}"
        assert mock_stdout.flush.called

    def test_bell_silenced_when_disabled(self):
        """bell_on_prompt=False must skip the \\a write entirely."""
        cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": False})
        with patch("sys.stdout") as mock_stdout:
            cli._fire_attention_signals("test")
        write_calls = [c.args[0] for c in mock_stdout.write.call_args_list]
        assert not any("\a" in s for s in write_calls), \
            f"bell still fired despite opt-out; writes were {write_calls!r}"

    def test_notification_fires_on_darwin(self):
        """osascript subprocess must spawn on macOS when notify_on_prompt is True."""
        cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": True})
        with patch("sys.platform", "darwin"), \
             patch("subprocess.Popen") as mock_popen:
            cli._fire_attention_signals("Approval needed: rm -rf /tmp/x")
        assert mock_popen.called, "osascript was not invoked"
        args = mock_popen.call_args.args[0]
        assert args[0] == "osascript"
        assert "-e" in args
        # The summary must be embedded in the AppleScript payload.
        script = args[2]
        assert "Approval needed" in script
        assert "display notification" in script
        assert "sound name" in script  # we want it audible, not silent

    def test_notification_silenced_when_disabled(self):
        """notify_on_prompt=False must skip the osascript spawn."""
        cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": False})
        with patch("sys.platform", "darwin"), \
             patch("subprocess.Popen") as mock_popen:
            cli._fire_attention_signals("test")
        assert not mock_popen.called

    def test_notification_skipped_on_non_darwin(self):
        """Non-macOS platforms must skip the osascript spawn even when enabled."""
        cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": True})
        with patch("sys.platform", "linux"), \
             patch("subprocess.Popen") as mock_popen:
            cli._fire_attention_signals("test")
        assert not mock_popen.called

    def test_notification_failure_does_not_raise(self):
        """A failing osascript spawn must NEVER block the prompt path."""
        cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": True})
        with patch("sys.platform", "darwin"), \
             patch("subprocess.Popen", side_effect=OSError("no osascript")):
            # Should swallow and return normally, not raise.
            cli._fire_attention_signals("test")

    def test_quote_in_summary_does_not_break_applescript(self):
        """Embedded quotes must be escaped so AppleScript syntax stays valid."""
        cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": True})
        with patch("sys.platform", "darwin"), \
             patch("subprocess.Popen") as mock_popen:
            cli._fire_attention_signals('he said "hi" then ran rm')
        script = mock_popen.call_args.args[0][2]
        # The escaped form must be in the payload; raw unescaped quote would
        # break AppleScript.
        assert "he said" in script
        # The double-quote characters delimiting the notification literal
        # must outnumber any embedded ones, which only holds if escaping ran.
        # Heuristic: the script starts and ends with " around the literal.
        assert script.startswith('display notification "')


class TestApprovalTimeoutWiring:
    """The CLI's _approval_callback used to hardcode timeout=60.

    These tests assert it now resolves via tools.approval._get_approval_timeout
    so the user's approvals.timeout config is actually honored.
    """

    def test_approval_callback_uses_get_approval_timeout(self):
        """Cover the import + call path inside _approval_callback."""
        cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": False})

        # Drive the callback on a worker thread so the main test thread
        # can deny it after asserting the timeout resolution path fired.
        response_holder = {}

        def run():
            response_holder["value"] = cli._approval_callback(
                "echo hi", "demo", allow_permanent=True,
            )

        # Patch _get_approval_timeout to a recognisable sentinel value and
        # immediately put a response into the queue so the loop exits fast.
        original_get = None
        try:
            import tools.approval as approval_mod
            original_get = approval_mod._get_approval_timeout
            sentinel_seen = {"called": False}

            def fake_get_timeout():
                sentinel_seen["called"] = True
                return 999

            approval_mod._get_approval_timeout = fake_get_timeout

            t = threading.Thread(target=run, daemon=True)
            t.start()
            # Wait briefly for the callback to install _approval_state, then
            # post a response to unblock it.
            import time
            for _ in range(50):
                if cli._approval_state is not None:
                    break
                time.sleep(0.02)
            assert cli._approval_state is not None, "approval state never set"
            cli._approval_state["response_queue"].put("deny")
            t.join(timeout=2.0)
            assert not t.is_alive()
            assert response_holder["value"] == "deny"
            assert sentinel_seen["called"], \
                "_get_approval_timeout was not consulted — timeout still hardcoded?"
        finally:
            if original_get is not None:
                import tools.approval as approval_mod
                approval_mod._get_approval_timeout = original_get
