#!/usr/bin/env python3
"""Hermes Agent CLI — interactive terminal interface (``python cli.py --help`` for usage)."""

# Must be the very first import (UTF-8 stdio on Windows). Missing only mid-``hermes update``.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass

import logging
import json
import os
import functools
import shutil  # noqa: F401 — tests patch shutil/time through the cli facade
import sys
import textwrap
import re
import atexit
import errno
import time  # noqa: F401 — see shutil
from collections import deque
from dataclasses import dataclass
from contextlib import contextmanager, suppress
from pathlib import Path
from rich.console import Console
from datetime import datetime  # noqa: F401 — siblings import it lazily through cli
from typing import List, Dict, Any, Optional, Mapping

logger = logging.getLogger(__name__)

os.environ["HERMES_QUIET"] = "1"  # suppress our modules' startup chatter


from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from hermes_cli.cli_commands_mixin import CLICommandsMixin
from hermes_cli.cli_billing_mixin import CLIBillingMixin
from hermes_cli.cli_loops_mixin import CLILoopsMixin
from hermes_cli.cli_info_mixin import CLIInfoMixin
from hermes_cli.cli_terminal_mixin import CLITerminalMixin
from hermes_cli.cli_modal_mixin import CLIModalMixin
from hermes_cli.cli_stream_mixin import CLIStreamMixin
from hermes_cli.cli_session_mixin import CLISessionMixin
from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin
from hermes_cli.cli_voice_mixin import CLIVoiceMixin
from hermes_cli.cli_status_bar_mixin import CLIStatusBarMixin
from hermes_cli.cli_tui_mixin import CLITuiMixin
from hermes_cli.cli_process_notifications import CLIProcessNotificationsMixin
from hermes_cli.cli_init_mixin import CLIInitMixin
from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
# Extracted clusters (mechanical split, #116911); re-exported here so `cli.<name>` stays the seam.
from hermes_cli.cli_shutdown import (  # noqa: F401,E402
    _CLEANUP_STEPS,
    _arm_exit_watchdog,
    _emit_interrupted_session_end,
    _exit_watchdog_timeout,
    _finalize_single_query,
    _float_env,
    _flush_logging_and_stdio,
    _flush_one_shot_session_store,
    _interrupt_async_delegations,
    _invoke_interrupted_session_end,
    _notify_session_finalize,
    _notify_single_query_session_finalize,
    _oneshot_agent_and_session,
    _should_emit_cleanup_session_finalize,
    _shutdown_agent_memory_provider,
    _shutdown_cached_aux_clients,
    _shutdown_mcp_servers,
    _stop_cli_wake_word,
    _sync_process_session_id,
    _wait_for_oneshot_background_completions,
)
from hermes_cli.cli_auto_maintenance import (  # noqa: F401,E402
    _run_checkpoint_auto_maintenance,
    _run_state_db_auto_maintenance,
)
from hermes_cli.cli_render import (  # noqa: F401,E402
    ChatConsole,
    _ACCENT,
    _ACCENT_ANSI_DEFAULT,
    _BOLD,
    _DA1_REPLY_RE,
    _DIM,
    _FALSE_RE,
    _LIGHT_DEFAULT_TERM_PROGRAMS,
    _LIGHT_MODE_REMAP,
    _LIGHT_MODE_REMAP_UPPER,
    _REASONING_TAGS,
    _RST,
    _STREAM_PAD,
    _STREAM_PARTIAL_PREVIEW_LEN,
    _SkinAwareAnsi,
    _TOOL_CALL_TAGS,
    _TRUE_RE,
    _WINDOWS_PATH_WITH_DOT_SEGMENT_RE,
    _accent_hex,
    _add_suspect_rows,
    _append_blank_panel_line,
    _append_panel_line,
    _assistant_content_as_text,
    _assistant_copy_text,
    _b,
    _build_compact_banner,
    _clear_output_history,
    _cli_visible_print,
    _coerce_output_history_limit,
    _cprint,
    _d,
    _detect_light_mode_uncached,
    _heal_cooked_mode_drift,
    _hex_to_ansi,
    _install_skin_light_mode_hook,
    _line_rows,
    _luminance_from_hex,
    _maybe_remap_for_light_mode,
    _output_history_lines,
    _output_history_recording,
    _output_history_rows,
    _output_tail_fitting,
    _painted_columns,
    _PaintedLine,
    _panel_box_width,
    _panel_cwidth,
    _panel_ljust,
    _post_stream_transform_output,
    _prepend_note_to_message,
    _preserve_windows_dot_segments_for_markdown,
    _pt_app_is_running,
    _pt_print_ansi,
    _query_osc11_background,
    _record_output_history,
    _record_output_history_entry,
    _release_paints,
    _render_final_assistant_content,
    _rich_text_from_ansi,
    _set_chrome_floor,
    _strip_markdown_syntax,
    _strip_reasoning_tags,
    _terminal_columns,
    _terminal_reflows,
    _terminal_width_for_streaming,
    _tty_wrap,
    _wrap_panel_text,
    _wrap_panel_text_keep_ws,
)
from hermes_cli.cli_config_load import (  # noqa: F401,E402
    _AUXILIARY_TASK_ENV,
    _CWD_PLACEHOLDERS,
    _TERMINAL_ENV_MAPPINGS,
    _cli_config_defaults,
    _init_logging_and_display_from_config,
    _load_prefill_messages,
    _merge_file_config,
    _mirror_config_to_env,
    _parse_reasoning_config,
    _parse_service_tier_config,
    _resolve_prefill_messages_file,
    load_cli_config,
)
from hermes_cli.cli_terminal_input import (  # noqa: F401,E402
    _BACKSLASH_LINE_CONTINUATION_RE,
    _DSR_CPR_ESC_RE,
    _DSR_CPR_VISIBLE_RE,
    _EXTENDED_ENTER_KEYS_SEQ,
    _IMAGE_EXTENSIONS,
    _KITTY_KEYBOARD_PUSH_SEQ,
    _MODIFY_OTHER_KEYS_SEQ,
    _SGR_MOUSE_BARE_RE,
    _SGR_MOUSE_ESC_RE,
    _SGR_MOUSE_VISIBLE_RE,
    _TERMINAL_INPUT_MODE_RESET_SEQ,
    _apply_backslash_line_continuation,
    _apply_bracketed_paste_timeout_patch,
    _bind_prompt_submit_keys,
    _build_cpr_disabled_output,
    _cli_multiline_shortcuts_enabled,
    _collect_query_images,
    _detect_file_drop,
    _disable_prompt_toolkit_cpr_warning,
    _enable_extended_enter_keys,
    _estimate_tui_input_height,
    _file_drop_result,
    _format_image_attachment_badges,
    _hermes_call_output_screen_diff,
    _is_backslash_line_continuation,
    _is_ghostty_terminal,
    _preserve_ctrl_enter_newline,
    _resolve_attachment_path,
    _select_classic_cli_pt_output,
    _should_auto_attach_clipboard_image_on_paste,
    _split_path_input,
    _status_bar_visible_from_display_config,
    _strip_leaked_terminal_responses_with_meta,
    _terminal_may_leak_cpr,
    _terminal_supports_extended_enter_keys,
    _termux_example_image_path,
)
from hermes_cli.cli_single_query import (  # noqa: F401,E402
    _TERMINAL_PROVIDER_REASONS,
    _TRANSIENT_PROVIDER_REASONS,
    _collect_kanban_task_images,
    _configure_quiet_agent,
    _install_single_query_signal_handlers,
    _int_or,
    _interrupt_agent_for_signal,
    _route_single_query_images,
    _run_kanban_goal_loop_chat,
    _run_kanban_goal_loop_q,
    _run_quiet_single_query,
    _run_single_query_mode,
    _single_query_exit_code,
    _sync_cli_session_id_from_agent,
)

from prompt_toolkit.patch_stdout import patch_stdout
try:
    from prompt_toolkit.enums import EditingMode
except ImportError:  # partial prompt_toolkit stubs in tests
    EditingMode = None
from prompt_toolkit import print_formatted_text as _pt_print
from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
try:
    from prompt_toolkit.cursor_shapes import CursorShape
    _STEADY_CURSOR = CursorShape.BLOCK
except (ImportError, AttributeError):
    _STEADY_CURSOR = None

try:
    from hermes_cli import pt_input_extras as _pt_extras

    _pt_extras.install_shift_enter_alias()
    _pt_extras.install_ctrl_enter_alias()
    _pt_extras.install_cmd_backspace_alias()
    _pt_extras.install_modify_other_keys_aliases()
    _pt_extras.install_keypress_data_normalization()
    _pt_extras.install_ignored_terminal_sequences()
    del _pt_extras
except Exception:
    pass
import threading
import queue


def _lazy_shim(module: str, name: str, alias: str | None = None):
    """Import ``module.name`` on first call; keeps heavy imports off startup while ``cli.<name>`` stays patchable."""
    import importlib

    def shim(*args, **kwargs):
        return getattr(importlib.import_module(module), name)(*args, **kwargs)

    shim.__name__ = shim.__qualname__ = alias or name
    return shim


def format_duration_compact(*args, **kwargs):
    seconds = float(args[0] if args else kwargs.get("seconds", 0.0))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        remaining_min = int(minutes % 60)
        return f"{int(hours)}h {remaining_min}m" if remaining_min else f"{int(hours)}h"
    days = hours / 24
    return f"{days:.1f}d"


# model id -> shortest configured alias (process-lifetime cache; config is read once).
_REVERSE_ALIAS_CACHE: dict[str, str] | None = None


def _reverse_alias_for_display(model_name: str) -> str:
    """Shortest alias for ``model_name`` from ``model_aliases:`` or ``model.aliases:``, else ``model_name``."""
    global _REVERSE_ALIAS_CACHE
    if not model_name:
        return model_name
    if _REVERSE_ALIAS_CACHE is None:
        rmap: dict[str, str] = {}

        def _put(m: str, alias: str) -> None:
            if m and (m not in rmap or len(alias) < len(rmap[m])):
                rmap[m] = alias

        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            ma = cfg.get("model_aliases")
            if isinstance(ma, dict):
                for alias, entry in ma.items():
                    if isinstance(entry, dict):
                        _put(str(entry.get("model", "") or "").strip(), alias)
            mdl = cfg.get("model", {}) or {}
            if isinstance(mdl, dict):
                simple = mdl.get("aliases")
                if isinstance(simple, dict):
                    for alias, val in simple.items():
                        if isinstance(val, str) and val.strip():
                            v = val.strip()
                            _put(v.split("/", 1)[1] if "/" in v else v, alias)
        except Exception:
            pass
        _REVERSE_ALIAS_CACHE = rmap
    return _REVERSE_ALIAS_CACHE.get(model_name, model_name)


def format_token_count_compact(*args, **kwargs):
    value = int(args[0] if args else kwargs.get("value", 0))
    abs_value = abs(value)
    if abs_value < 1_000:
        return str(value)

    sign = "-" if value < 0 else ""
    units = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for threshold, suffix in units:
        if abs_value >= threshold:
            scaled = abs_value / threshold
            text = f"{scaled:.{2 if scaled < 10 else 1 if scaled < 100 else 0}f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"

    return f"{value:,}"


realign_markdown_tables = _lazy_shim("agent.markdown_tables", "realign_markdown_tables")

_COMMAND_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


# ~/.hermes/.env first, project .env as dev fallback; user env files override stale shell exports.
from hermes_constants import get_hermes_home
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_cli.fallback_config import get_fallback_chain
from hermes_state_ids import new_session_id
from utils import base_url_host_matches, base_url_hostname, fast_safe_load

_hermes_home = get_hermes_home()
_project_env = Path(__file__).parent / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=_project_env)


_REASONING_TAGS = ("REASONING_SCRATCHPAD", "think", "thinking", "reasoning", "thought")
# FORK: "invoke"/"parameter" appended — some backends leak Anthropic-style tool XML.
_TOOL_CALL_TAGS = ("tool_call", "tool_calls", "tool_result", "function_call", "function_calls",
                   "invoke", "parameter")


def _resolve_reasoning_for_model(
    model: str,
    by_model_map: dict | None,
    global_effort: str,
) -> dict | None:
    """Resolve reasoning config for a specific model, checking per-model map first.

    ``by_model_map`` is a dict of model name → effort string (e.g.
    ``{"deepseek-v4-flash": "xhigh", "claude-sonnet-4-6": "high"}``).
    Model names are matched case-insensitively. Falls back to ``global_effort``
    when no per-model entry exists.
    """
    if by_model_map and model:
        model_lower = model.strip().lower()
        for saved_model, saved_effort in by_model_map.items():
            if saved_model.strip().lower() == model_lower:
                return _parse_reasoning_config(str(saved_effort))
    return _parse_reasoning_config(global_effort)


# terminal.<key> -> TERMINAL_<KEY> env var. Container-resource keys apply to docker,
# singularity, modal, daytona and vercel_sandbox only (ignored for local/ssh).
_TERMINAL_ENV_MAPPINGS = {
    key: f"TERMINAL_{key.upper()}"
    for key in (
        "degraded_mode", "cwd", "timeout", "home_mode", "lifetime_seconds", "docker_image",
        "wait_max_timeout",  # FORK
        "docker_forward_env", "singularity_image", "modal_image", "daytona_image", "vercel_runtime",
        "ssh_host", "ssh_user", "ssh_port", "ssh_key", "container_cpu", "container_memory",
        "container_disk", "container_persistent", "docker_volumes", "docker_env", "docker_extra_args",
        "docker_shm_size", "docker_mount_cwd_to_workspace", "docker_network", "docker_run_as_host_user",
        "docker_snap_compat",
        "docker_persist_across_processes", "docker_shared_container_key", "docker_orphan_reaper",
        "sandbox_dir", "persistent_shell",
    )
}
_TERMINAL_ENV_MAPPINGS = {"env_type": "TERMINAL_ENV", **_TERMINAL_ENV_MAPPINGS, "sudo_password": "SUDO_PASSWORD"}
# Per-task auxiliary endpoint tuples (config key -> env var).
_AUXILIARY_TASK_ENV = {
    "vision": {
        "provider": "AUXILIARY_VISION_PROVIDER",
        "model": "AUXILIARY_VISION_MODEL",
        "base_url": "AUXILIARY_VISION_BASE_URL",
        "api_key": "AUXILIARY_VISION_API_KEY",
    },
    "approval": {
        "provider": "AUXILIARY_APPROVAL_PROVIDER",
        "model": "AUXILIARY_APPROVAL_MODEL",
        "base_url": "AUXILIARY_APPROVAL_BASE_URL",
        "api_key": "AUXILIARY_APPROVAL_API_KEY",
    },
}
_CWD_PLACEHOLDERS = (".", "auto", "cwd")


CLI_CONFIG = load_cli_config()


_init_logging_and_display_from_config()

# Neuter AsyncHttpxClientWrapper.__del__ before any AsyncOpenAI client exists: it
# schedules aclose() on the running loop (prompt_toolkit's, during idle), closing
# transports bound to dead worker loops ("Event loop is closed" / "Press ENTER to
# continue..."). A meta_path finder patches ``openai._base_client`` at first import —
# eager import costs ~166ms/30MB cold, and the patch is guaranteed to land before
# instantiation. See ``agent.auxiliary_client.neuter_async_httpx_del``.
try:
    import sys as _httpx_neuter_sys
    import importlib.util as _httpx_neuter_imp_util

    class _AsyncHttpxDelNeuter:
        """Patch ``AsyncHttpxClientWrapper.__del__`` to a no-op when ``openai._base_client`` loads."""

        _armed = True

        def find_spec(self, fullname, path=None, target=None):
            if not self._armed or fullname != "openai._base_client":
                return None
            # Disarm before delegating so the recursive find_spec doesn't loop through us.
            self._armed = False
            try:
                _httpx_neuter_sys.meta_path.remove(self)
            except ValueError:
                pass
            spec = _httpx_neuter_imp_util.find_spec(fullname)
            if spec is None or spec.loader is None:
                return None
            _orig_exec = spec.loader.exec_module

            def _patched_exec(module):
                _orig_exec(module)
                try:
                    cls = getattr(module, "AsyncHttpxClientWrapper", None)
                    if cls is not None:
                        cls.__del__ = lambda self: None  # type: ignore[assignment]
                except Exception:
                    pass

            spec.loader.exec_module = _patched_exec  # type: ignore[method-assign]
            return spec

    _httpx_neuter_sys.meta_path.insert(0, _AsyncHttpxDelNeuter())
except Exception:
    pass


# Agent/tool systems load lazily: bare startup only needs the prompt.
def get_tool_definitions(*args, **kwargs):
    from hermes_cli.mcp_startup import wait_for_mcp_discovery
    from model_tools import get_tool_definitions as _get_tool_definitions

    wait_for_mcp_discovery()
    return _get_tool_definitions(*args, **kwargs)


validate_toolset = _lazy_shim("toolsets", "validate_toolset")


_cleanup_all_terminals = _lazy_shim("tools.terminal_tool", "cleanup_all_environments", "_cleanup_all_terminals")
set_sudo_password_callback = _lazy_shim("tools.terminal_tool", "set_sudo_password_callback")
set_approval_callback = _lazy_shim("tools.terminal_tool", "set_approval_callback")
set_secret_capture_callback = _lazy_shim("tools.skills_tool", "set_secret_capture_callback")
_cleanup_all_browsers = _lazy_shim("tools.browser_tool_lifecycle", "_emergency_cleanup_all_sessions", "_cleanup_all_browsers")

_cleanup_done = False  # _run_cleanup runs exactly once
_cleanup_in_progress = False
_cli_wake_owner = None
# One-shot finalization runs before process cleanup (plugins see the boundary while the
# agent is attached); atexit cleanup must not finalize those sessions again.
_single_query_finalize_attempted_session_ids: set[str | None] = set()
# /handoff sessions belong to the gateway: finalizing them here would stamp end_reason on
# a row the gateway just reopened, making the handoff leg vanish from history.
# Session IDs that were handed off to the gateway via /handoff. The CLI process exits after a successful
# handoff, but the gateway now owns the session lifecycle — _run_cleanup must NOT call finalize_session on
# these, because doing so sets end_reason on a row the gateway just reopened and is actively writing to
# (#88234). The race made the handoff leg vanish from session history and broke session_search recall for
# the handed-off session.
_handed_off_session_ids: set[str | None] = set()
_active_agent_ref = None  # active AIAgent, for memory-provider shutdown at exit
_deferred_agent_startup_done = False
# Set once the TUI app starts (focus reporting + mouse tracking on); gates the on-exit
# terminal reset so non-TUI one-shot runs never emit codes for modes they never enabled.
_tui_input_modes_active = False
# Guard so the Phase 2 memory-confirm UI (see _run_memory_confirm_before_exit)
# runs at most once per process, even though it's now called from multiple
# exit call sites (each guarded by its own early-return / atexit path) plus
# _run_cleanup_body's fallback invocation for paths that skip the explicit
# pre-summary call.
_memory_confirm_attempted = False
# Same idempotency guard, for the background skill-curator cost fold-in
# (see _fold_curator_cost_before_exit) — separate flag since the two run
# independently and one being attempted says nothing about the other.
_curator_fold_attempted = False


# Set True once the TUI's prompt_toolkit app starts (which enables focus reporting + mouse tracking). Gates
# the on-exit terminal reset so non-TUI one-shot CLI runs — which also register _run_cleanup via atexit —
# don't emit escape codes for modes they never enabled (#36823).
def _mark_tui_input_modes_active() -> None:
    """Record that the TUI app started, so _run_cleanup resets input modes."""
    global _tui_input_modes_active
    _tui_input_modes_active = True


def _prepare_deferred_agent_startup() -> None:
    """Run Termux-deferred agent discovery before the first real agent turn."""
    global _deferred_agent_startup_done
    if _deferred_agent_startup_done:
        return
    if os.environ.get("HERMES_DEFER_AGENT_STARTUP") != "1":
        return
    _deferred_agent_startup_done = True
    _accept_hooks = os.environ.get("HERMES_ACCEPT_HOOKS", "").lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception:
        logger.warning("plugin discovery failed at deferred CLI startup", exc_info=True)
    try:
        from hermes_cli.mcp_startup import start_background_mcp_discovery

        start_background_mcp_discovery(logger=logger, thread_name="termux-cli-mcp-discovery")
    except Exception:
        logger.debug("MCP tool discovery failed at deferred CLI startup", exc_info=True)
    try:
        from agent.shell_hooks import register_from_config
        from agent.outbound_webhooks import register_from_config as register_outbound_webhooks
        from hermes_cli.config import load_config

        _hooks_cfg = load_config()
        register_from_config(_hooks_cfg, accept_hooks=_accept_hooks)
        register_outbound_webhooks(_hooks_cfg)
    except Exception:
        logger.debug("shell-hook registration failed at deferred CLI startup", exc_info=True)


def _install_cleanup_skip_handler():
    """Let an impatient user Ctrl+C past a slow ``_run_cleanup()`` instead
    of sitting through the full ``HERMES_EXIT_WATCHDOG_S`` wait (default
    60s — memory-confirm LLM call + MCP teardown can legitimately take
    that long). Returns a zero-arg restore callback; call it once cleanup
    finishes normally (a no-op restore is returned when installation is
    skipped or fails, so callers never need to branch).

    Safe to install unconditionally during ``_run_cleanup()``: by the time
    it runs, ``app.run()`` has already returned, so prompt_toolkit's own
    TUI-level Ctrl+C binding (see the Windows SIGINT-absorb handler
    earlier in this file) is no longer live — this is a different phase
    of shutdown, not a competing handler for the same keypress.

    The pressed-Ctrl+C path calls ``os._exit(0)`` directly rather than
    letting a raised ``KeyboardInterrupt`` unwind — cleanup steps are
    littered with bare ``except Exception`` blocks that would likely
    swallow it and keep going anyway, defeating the point of a fast exit.
    Skipped entirely under pytest (mirrors ``_arm_exit_watchdog``'s own
    guard) so test runs never have their SIGINT handling hijacked.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return lambda: None
    try:
        import signal as _signal
    except Exception:
        return lambda: None

    def _skip_wait(signum, frame):
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)

    try:
        _previous_handler = _signal.getsignal(_signal.SIGINT)
        _signal.signal(_signal.SIGINT, _skip_wait)
    except (ValueError, OSError, AttributeError):
        # ValueError: not the main thread. OSError/AttributeError: platform
        # doesn't support this signal the way we expect. Either way, fall
        # back to "no skip available" rather than risk a half-installed
        # handler — the watchdog is still the safety net.
        return lambda: None

    def _restore():
        try:
            _signal.signal(_signal.SIGINT, _previous_handler)
        except Exception:
            pass

    return _restore
_signal_watchdog_armed = False


def _arm_exit_watchdog_on_shutdown_signal() -> None:
    """Arm the exit backstop the moment a termination signal arrives (idempotent; never raises).

    The graceful unwind has wedge points BEFORE ``_run_cleanup`` arms its own watchdog
    (main thread in a syscall, prompt_toolkit teardown never returning). Leash is 2x
    the cleanup timeout so a progressing cleanup is never cut short. Never arm at
    startup: the timer exits unconditionally.

    SIGTERM/SIGHUP establish unambiguous shutdown intent, but the graceful path from signal →
    ``agent.interrupt()`` → ``app.exit()`` / ``KeyboardInterrupt`` → ``finally`` → ``_run_cleanup`` has
    several wedge points BEFORE ``_run_cleanup`` arms the normal watchdog: a main thread parked in a syscall
    that never observes the unwind, a prompt_toolkit teardown that never returns, or an agent worker
    blocking the ``finally``. When that happens the process has NO backstop and a "dead" CLI lingers
    (observed: ``hermes --tui`` alive ~47 min at 4% CPU after terminal close — the #65998 class).
    """
    global _signal_watchdog_armed
    if _signal_watchdog_armed:
        return
    _signal_watchdog_armed = True
    base = _exit_watchdog_timeout()
    if base <= 0:
        return  # explicitly disabled
    with suppress(Exception):  # never let the backstop break signal handling
        _arm_exit_watchdog(timeout_s=base * 2, from_signal=True)


def _run_cleanup(*, notify_session_finalize: bool = True):
    """Run resource cleanup exactly once."""
    global _cleanup_done, _cleanup_in_progress
    if _cleanup_done:
        return
    _cleanup_done = True
    # Bound total shutdown time: if cleanup (or the interpreter's
    # thread-join teardown after it) wedges, force-exit instead of
    # leaving a zombie CLI holding the terminal for minutes.
    _cleanup_in_progress = True
    _restore_sigint = _install_cleanup_skip_handler()
    try:
        print(
            f"{_DIM}(cleaning up — press Ctrl+C to quit immediately){_RST}",
            flush=True,
        )
    except Exception:
        pass

    try:
        _run_cleanup_body(notify_session_finalize=notify_session_finalize)
    finally:
        _restore_sigint()
        _cleanup_in_progress = False


def _run_memory_confirm_before_exit() -> None:
    """Run the Phase 2 memory-confirm UI for buffered proposals, once.

    Fork-only — no upstream equivalent. Historically this lived inline
    inside ``_run_cleanup_body``, which put it BEHIND
    ``self._print_exit_summary()`` in source order on every interactive-exit
    call site. Since ``confirm_and_commit`` makes a real LLM call (up to the
    ``auxiliary.memory_extraction.timeout`` default of 30s) and the exit
    watchdog (``_arm_exit_watchdog``) can fire mid-``_run_cleanup``, that
    ordering meant the process could ``os._exit(0)`` before the cost
    report / resume hint ever printed (see the 2026-07-14 exit-summary-
    ordering fix in FORK.md) — and separately, the confirm step's own LLM
    spend was invisible to ``session_estimated_cost_usd`` because nothing
    folded it in.
    Callers now invoke this explicitly BEFORE ``_print_exit_summary()`` so
    both the confirm UI and its cost are guaranteed to land in the printed
    summary. ``_run_cleanup_body`` still calls this too (idempotently, via
    the module-level guard below) as a safety net for any exit path that
    doesn't call it explicitly first — better a double-checked no-op than a
    dropped memory review.
    """
    global _memory_confirm_attempted
    if _memory_confirm_attempted:
        return
    _memory_confirm_attempted = True
    try:
        if _active_agent_ref:
            _session_msgs_for_mex = getattr(_active_agent_ref, '_session_messages', None) or []
            from hermes_cli.memory_confirm import confirm_and_commit
            confirm_and_commit(
                getattr(_active_agent_ref, 'session_id', "") or "",
                _session_msgs_for_mex if isinstance(_session_msgs_for_mex, list) else [],
            )
            # Fold the confirm step's LLM spend (session-end extraction pass
            # + any conflict-classification calls) into the same counter
            # _print_exit_summary reads, so the printed total reflects the
            # real cost of ending the session, not just the conversation
            # that preceded it.
            try:
                from tools.memory_extraction.extractor import (
                    get_and_reset_extraction_cost_usd,
                )
                _mex_cost = get_and_reset_extraction_cost_usd()
                if _mex_cost:
                    _active_agent_ref.session_estimated_cost_usd = (
                        float(getattr(_active_agent_ref, "session_estimated_cost_usd", 0.0) or 0.0)
                        + _mex_cost
                    )
            except Exception:
                pass
    except Exception:
        # Never block exit on extraction issues
        pass


def _fold_curator_cost_before_exit() -> None:
    """Fold any COMPLETED background skill-curator review's LLM cost into
    ``session_estimated_cost_usd``, once. Never blocks.

    ``maybe_run_curator`` (kicked off at CLI/session startup, see
    ``show_banner``) spawns a forked ``AIAgent`` in a daemon thread
    (``agent.curator.run_curator_review``'s ``_llm_pass``) that can
    legitimately run for minutes — its own docstring: "50-100 API calls
    against hundreds of candidate skills". Exit must NEVER wait on that
    thread the way ``_run_memory_confirm_before_exit`` waits on the
    (bounded, ~30s) memory-extraction call. Instead:
      - If the pass already finished, its cost is sitting in
        ``agent.curator``'s ledger — drain it and fold it in, same pattern
        as the memory-extraction cost above.
      - If it's still running, print a one-line note so the cost total
        isn't silently wrong — the user sees that a real, uncounted spend
        is still in flight rather than assuming the printed total is
        complete. That spend surfaces later via ``hermes curator status``.
    Fork-only — no upstream equivalent (upstream has no curator subsystem).
    """
    global _curator_fold_attempted
    if _curator_fold_attempted:
        return
    _curator_fold_attempted = True
    if not _active_agent_ref:
        return
    try:
        from agent.curator import get_and_reset_curator_cost_usd, is_curator_running
        _curator_cost = get_and_reset_curator_cost_usd()
        if _curator_cost:
            _active_agent_ref.session_estimated_cost_usd = (
                float(getattr(_active_agent_ref, "session_estimated_cost_usd", 0.0) or 0.0)
                + _curator_cost
            )
        elif is_curator_running():
            try:
                print(
                    f"{_DIM}(background skill curator still running — its "
                    f"cost isn't included above; check `hermes curator "
                    f"status` after it finishes){_RST}"
                )
            except Exception:
                pass
    except Exception:
        # Never block exit on curator cost bookkeeping issues
        pass


def _run_cleanup_body(*, notify_session_finalize: bool = True):
    """The actual cleanup steps, split out of ``_run_cleanup`` (FORK) so the Ctrl+C-skip
    handler installed there always gets restored via a ``finally``, regardless of how this
    body exits (normal return, exception, or the ``BaseException`` catch around MCP
    shutdown)."""
    # Bound total shutdown time: if cleanup (or the interpreter's thread-join teardown
    # after it) wedges, force-exit instead of leaving a zombie CLI holding the terminal.
    _arm_exit_watchdog()

    # Reset terminal input modes FIRST: teardown below can take seconds and a later step
    # raising must not skip the reset. No-op unless the TUI ran. See #36823.
    _reset_terminal_input_modes_on_exit()

    for step, swallow in _CLEANUP_STEPS:
        with suppress(swallow):
            globals()[step]()

    # Session-finalize notification. Upstream caught up with the fork's inline
    # ``invoke_hook("on_session_finalize", ...)`` by extracting it into
    # ``_notify_session_finalize`` (same hook, now guarded by the ``notify_session_finalize``
    # param + ``_should_emit_cleanup_session_finalize`` dedup). Took upstream's version.
    if notify_session_finalize:
        cleanup_session_id = _active_agent_ref.session_id if _active_agent_ref else None
        if _should_emit_cleanup_session_finalize(cleanup_session_id):
            _notify_session_finalize(session_id=cleanup_session_id, platform="cli", reason="shutdown")

    # FORK: Phase-2 auto-extraction — surface the confirm UI for buffered proposals BEFORE
    # shutdown_memory_provider runs (the latter would auto-stash with no confirm callback
    # registered). No-op when memory.auto_extract is off or there are no proposals. Both
    # helpers are idempotent module-level guards, so this is the safety net for exit routes
    # that skip the explicit call.
    #
    # Order: curator check first (near-instant — folds an already-finished pass's cost or
    # prints one note; never blocks), then the memory-confirm UI (interactive), then (by the
    # caller) the exit summary.
    _fold_curator_cost_before_exit()
    _run_memory_confirm_before_exit()

    try:
        _shutdown_agent_memory_provider(_active_agent_ref)
    except Exception as e:
        logger.warning("CLI cleanup memory shutdown failed: %s", e, exc_info=True)


def _reset_terminal_input_modes_on_exit() -> None:
    """Disable focus reporting + mouse tracking on TUI exit (best-effort).

    Ctrl+C / SIGTERM / crashes bypass prompt_toolkit's unwind, leaving focus events and
    mouse reports as visible text in the next shell. Writes to stdout when it is the
    terminal, else /dev/tty (the TUI may have run with stdout redirected).

    Called from ``_run_cleanup`` (atexit-registered + invoked on the normal / EOF / interrupt exit paths)
    this covers normal quit, Ctrl+C and SIGTERM/SIGHUP. ``kill -9`` is uncatchable, and the kanban worker's
    ``os._exit(0)`` path bypasses ``atexit``; neither runs this — but both are non-TTY / non-TUI, so there
    is nothing to reset there. See #36823.
    """
    global _tui_input_modes_active
    if not _tui_input_modes_active:
        return
    # Clear first so a re-armed _run_cleanup doesn't re-emit.
    _tui_input_modes_active = False
    try:
        stream = sys.stdout
        if stream is not None and stream.isatty():
            stream.write(_TERMINAL_INPUT_MODE_RESET_SEQ)
            stream.flush()
            return
    except Exception:
        pass
    with suppress(Exception), open("/dev/tty", "w", encoding="ascii") as tty:
        tty.write(_TERMINAL_INPUT_MODE_RESET_SEQ)
        tty.flush()


from hermes_cli.worktree_ops import (
    _git_quiet,
    _git_repo_root,
    _maintain_pack_health,
    _prune_stale_worktrees,
    _repo_is_shallow,
    _setup_worktree,
    _worktree_has_unpushed_commits,
    release_lsp_clients,
)

# ============================================================================= Git Worktree Isolation
# (#652) =============================================================================
_active_worktree: Optional[Dict[str, str]] = None


def _cleanup_worktree(info: Dict[str, str] = None) -> None:
    """Remove a worktree and its branch on exit; kept only when it has unpushed commits."""
    global _active_worktree
    info = info or _active_worktree
    if not info:
        return

    wt_path, branch, repo_root = info["path"], info["branch"], info["repo_root"]
    if not Path(wt_path).exists():
        return

    if _worktree_has_unpushed_commits(wt_path, timeout=10):
        if _repo_is_shallow(repo_root):
            # Shallow boundary makes the unpushed verdict unreliable; the startup pruner reaps later.
            _cprint(f"\n\033[33m⚠ Shallow clone — cannot verify push state, keeping: {wt_path}\033[0m")
            print("  The next `hermes -w` session deepens the clone and prunes merged worktrees automatically.")
        else:
            _cprint(f"\n\033[33m⚠ Worktree has unpushed commits, keeping: {wt_path}\033[0m")
            print(f"  To clean up manually: git worktree remove --force {wt_path}")
        _active_worktree = None
        return

    # Release the tree's language servers while the path still exists, then unlock so `remove`
    # isn't blocked by the lock placed at creation. Fail-soft.
    release_lsp_clients(wt_path)
    _git_quiet(["worktree", "unlock", wt_path], repo_root, log="git worktree unlock failed (non-fatal)")
    _git_quiet(["worktree", "remove", wt_path, "--force"], repo_root, timeout=15, log="Failed to remove worktree")
    _git_quiet(["branch", "-D", branch], repo_root, log=f"Failed to delete branch {branch}")

    _active_worktree = None
    _cprint(f"\033[32m✓ Worktree cleaned up: {wt_path}\033[0m")


_ACCENT_ANSI_DEFAULT = "\033[1;38;2;255;215;0m"  # #FFD700 bold fallback
_BOLD = "\033[1m"
_RST = "\033[0m"
def _load_stream_pad() -> str:
    """Left-margin indent for streamed response/reasoning box text.

    User-configurable via ``display.response_indent_width`` (default 4,
    the pre-July-2026 upstream default). Set to 0 to restore the
    flush-left rendering upstream switched to in July 2026 specifically
    for clean mouse-copy/paste (every selected line otherwise carries
    this many leading spaces). ``/copy`` writes the ORIGINAL message
    text via the native clipboard regardless of this setting, so it
    remains the clean-copy path no matter which value is configured
    here — this indent is purely cosmetic on-screen framing.
    """
    try:
        n = int(CLI_CONFIG["display"].get("response_indent_width", 4))
    except Exception:
        n = 4
    return " " * max(0, n)


_STREAM_PAD = _load_stream_pad()

_STREAM_PARTIAL_PREVIEW_LEN = 60  # tail of an unfinished logical line mirrored
# into the spinner while streaming (TTFT perception without hard-wrapping)


# Light/dark terminal detection (mirrors ui-tui/src/theme.ts detectLightMode()). Priority:
# HERMES_LIGHT/HERMES_TUI_LIGHT env, HERMES_TUI_THEME, HERMES_TUI_BACKGROUND, COLORFGBG
# (bg slot 7/15 = light), OSC 11 query, default dark. Cached so the terminal is queried once.
_LIGHT_MODE_CACHE: bool | None = None
_TRUE_RE = re.compile(r"^(1|true|on|yes|y)$")
_FALSE_RE = re.compile(r"^(0|false|off|no|n)$")
_LIGHT_DEFAULT_TERM_PROGRAMS = frozenset()  # Apple_Terminal isn't reliable; require explicit config


_DA1_REPLY_RE = re.compile(rb"\x1b\[\?[0-9;]*c")


def _detect_light_mode() -> bool:
    global _LIGHT_MODE_CACHE
    if _LIGHT_MODE_CACHE is not None:
        return _LIGHT_MODE_CACHE
    try:
        result = _detect_light_mode_uncached()
    except Exception:
        result = False
    _LIGHT_MODE_CACHE = result
    return result


_install_skin_light_mode_hook()


# Prime the light-mode cache when interactive so OSC 11 happens before prompt_toolkit owns the tty.
with suppress(Exception):
    if sys.stdin.isatty() and sys.stdout.isatty():
        _detect_light_mode()


_ACCENT = _SkinAwareAnsi("response_border", "#FFD700", bold=True)
# dim+italic attributes (not a hex) so dim text inherits the terminal foreground in both modes.
_DIM = "\x1b[2;3m"


_b = functools.partial(_tty_wrap, sgr="\x1b[1m")  # bold when stdout is a real TTY
_d = functools.partial(_tty_wrap, sgr="\x1b[2;3m")  # dim-italic when stdout is a real TTY


# Special-token control markup that some open-weight backends can leak into the
# assistant *content* stream when their tool-call parser fails on a malformed
# block. These are model control tokens, never meant to reach the user:
#   - DeepSeek V3.2 / V4 DSML: ``<｜DSML｜tool_calls>``, ``<｜DSML｜invoke …>``,
#     ``<｜DSML｜parameter …>`` (fullwidth-bar ｜ = U+FF5C). Seen leaking via
#     exo's OpenAI-compatible endpoint when DSv4 parrots a prior tool result
#     inside a tool_calls wrapper.
# The owning backend (exo) is the primary fix site; this is a display-side
# safety net so a leak from ANY backend never paints raw control tokens.
_DSML_BAR = "\uff5c"  # ｜ U+FF5C FULLWIDTH VERTICAL LINE
# Well-formed DSML tag: <｜DSML｜name ...> or </｜DSML｜name>. Name is word-like
# so a stray ``<｜DSML｜`` glued to prose is left to the orphan pass below.
_LEAKED_DSML_TAG_RE = re.compile(
    rf"</?{re.escape(_DSML_BAR)}DSML{re.escape(_DSML_BAR)}\w+(?:\s+[^>]*)?>"
)
# Orphaned DSML sentinel (optional leading '<' / '</') with no valid tag.
_LEAKED_DSML_ORPHAN_RE = re.compile(
    rf"(?:<\s*/?\s*)?{re.escape(_DSML_BAR)}DSML{re.escape(_DSML_BAR)}"
)


def _strip_special_token_markup(text: str) -> str:
    """Strip leaked model control-token markup from display text.

    Defense-in-depth: if a backend's tool-call parser fails and leaks raw
    control tokens (e.g. DeepSeek ``<｜DSML｜…>``) into the content stream,
    remove them here so they never render in the response box. Surrounding
    prose is preserved — only the special-token markup is removed.
    """
    if not text or _DSML_BAR not in text:
        return text
    text = _LEAKED_DSML_TAG_RE.sub("", text)
    text = _LEAKED_DSML_ORPHAN_RE.sub("", text)
    return text


_WINDOWS_PATH_WITH_DOT_SEGMENT_RE = re.compile(r"(?i)(?:\b[a-z]:\\|\\\\)[^\s`]*\\\.[^\s`]*")


def _wrap_stream_line(line: str) -> list[str]:
    """Hard-wrap one logical line of streamed prose to the box's width.

    FORK-only (tests/hermes_cli/test_stream_symmetric_wrap.py). Gives the streaming box a
    symmetric right margin matching ``_STREAM_PAD``'s left indent instead of relying on the
    terminal's soft-wrap. Trade-off accepted by the user: mouse-selecting a hard-wrapped
    paragraph pastes as several short lines — ``/copy`` still writes the original unwrapped
    text via the native clipboard.

    Prose lines only. Table rows are column-aligned by ``realign_markdown_tables`` and
    word-wrapping them would destroy that alignment.
    """
    if not line:
        return [""]
    wrapped = textwrap.wrap(
        line,
        width=max(8, _terminal_width_for_streaming()),
        replace_whitespace=False,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped or [""]


_OUTPUT_HISTORY_ENABLED = True
_OUTPUT_HISTORY_REPLAYING = False
_OUTPUT_HISTORY_SUPPRESSED = False
_OUTPUT_HISTORY_MAX_LINES = 200
_OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


def _configure_output_history(enabled: bool, max_lines=200) -> None:
    """Configure recent CLI output replayed after terminal redraws."""
    global _OUTPUT_HISTORY_ENABLED, _OUTPUT_HISTORY_MAX_LINES, _OUTPUT_HISTORY
    _OUTPUT_HISTORY_ENABLED = bool(enabled)
    _OUTPUT_HISTORY_MAX_LINES = _coerce_output_history_limit(max_lines)
    _OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


@contextmanager
def _suspend_output_history():
    global _OUTPUT_HISTORY_SUPPRESSED
    old_value = _OUTPUT_HISTORY_SUPPRESSED
    _OUTPUT_HISTORY_SUPPRESSED = True
    try:
        yield
    finally:
        _OUTPUT_HISTORY_SUPPRESSED = old_value


def _replay_output_history(fit=None, output=None) -> None:
    """Repaint recent output above the prompt after a full screen clear.

    ``fit=(rows, columns, painted, top)`` replays only the newest lines whose wrapped height
    fits ``rows`` (see ``_output_tail_fitting``) — the older ones are still in scrollback
    (#95375) — from screen row ``top`` when known (``_set_chrome_floor``). ``output``: paint
    now, straight to this prompt_toolkit output, where the caller just erased the viewport and
    reset the renderer — ``run_in_terminal`` would first erase below the top row, which
    scroll-on-clear terminals (tmux) take as a clear and copy the blank screen into scrollback.
    """
    global _OUTPUT_HISTORY_REPLAYING
    if not _OUTPUT_HISTORY_ENABLED or not _OUTPUT_HISTORY:
        return
    _OUTPUT_HISTORY_REPLAYING = True
    try:
        rendered_lines = _output_history_lines()
        top = None
        if fit is not None:
            rows, columns, painted, top = fit
            rendered_lines = _output_tail_fitting(rendered_lines, rows, columns, painted)
        if rendered_lines:
            # One payload: per-line pt prints each force a sync redraw (a waterfall of old output).
            if output is None:
                _pt_print(_PT_ANSI("\n".join(rendered_lines)))
            else:
                from prompt_toolkit.renderer import print_formatted_text as _paint_formatted_text
                from prompt_toolkit.styles import Style
                _paint_formatted_text(output, _PT_ANSI("\n".join(rendered_lines) + "\n"), Style([]))
                size = output.get_size()
                if top is not None:  # the chrome's top is now this many rows down
                    top += sum(_line_rows(line, columns) for line in rendered_lines)
                    _set_chrome_floor(max(0, size.rows - top))
                    if size.columns != columns:
                        _add_suspect_rows(top + 1 - size.rows)
            width = _painted_columns() if fit is None else columns
            for line in rendered_lines:  # repainted: they wrap at today's width from now on
                if isinstance(line, _PaintedLine):
                    line.width = width
    except Exception:
        pass
    finally:
        _OUTPUT_HISTORY_REPLAYING = False


_strip_leaked_bracketed_paste_wrappers = _lazy_shim(
    "hermes_cli.input_sanitize", "strip_leaked_bracketed_paste_wrappers", "_strip_leaked_bracketed_paste_wrappers"
)


# CPR replies (``ESC[<row>;<col>R``) can race past the input parser under resize storms
# and land as literal text; the ``^[[...R`` form appears when a filter stripped the ESC.
# Cursor Position Report (CPR / DSR) response, format ``ESC[<row>;<col>R``. prompt_toolkit's _on_resize() +
# renderer send ``ESC[6n`` queries to the terminal; under resize storms or tab switches the terminal's reply
# can race past the input parser and end up in the input buffer as literal text (see issue #14692). Also
# matches the visible-form ``^[[<row>;<col>R`` that appears when the ESC byte was stripped by a prior
# filter.
_DSR_CPR_ESC_RE = re.compile(r"\x1b\[\d+;\d+R")
_DSR_CPR_VISIBLE_RE = re.compile(r"\^\[\[\d+;\d+R")
_SGR_MOUSE_ESC_RE = re.compile(r"\x1b\[<\d+;\d+;\d+[Mm]")
_SGR_MOUSE_VISIBLE_RE = re.compile(r"\^\[\[<\d+;\d+;\d+[Mm]")
# Bare "<btn;col;rowM" fragments; deliberately broad, they are almost never intentional input.
_SGR_MOUSE_BARE_RE = re.compile(r"<\d+;\d+;\d+[Mm]")
_TERMINAL_INPUT_MODE_RESET_SEQ = (
    "\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l"  # mouse: SGR, any-motion, button-motion, click
    "\x1b[?1004l"  # focus events
    "\x1b[?2004l"  # bracketed paste
    "\x1b[?1049l"  # leave alt screen
    "\x1b[<u"  # pop kitty keyboard mode
    "\x1b[>4m"  # reset modifyOtherKeys
    "\x1b[0m\x1b[?25h"  # reset attributes, show cursor
)
_KITTY_KEYBOARD_PUSH_SEQ = "\x1b[>1u"
_MODIFY_OTHER_KEYS_SEQ = "\x1b[>4;2m"
_EXTENDED_ENTER_KEYS_SEQ = _KITTY_KEYBOARD_PUSH_SEQ + _MODIFY_OTHER_KEYS_SEQ


_BACKSLASH_LINE_CONTINUATION_RE = re.compile(r"\\[ \t]*$")


# OSC sequences (e.g. OSC-8 links): pt's ANSI parser strips the ESC but leaks the payload as text.
_OSC_ESCAPE_RE = re.compile(r"\x1b\][\s\S]*?(?:\x07|\x1b\\)")


def _looks_like_slash_command(text: str) -> bool:
    """``/help`` yes, ``/Users/x/file.md`` no: a command's first word has no further ``/``."""
    if not text or not text.startswith("/"):
        return False
    return "/" not in text.split()[0][1:]


_skill_commands = None
_skill_bundles = None


def _slash_args(cmd: str) -> str:
    """Text after the slash-command word, stripped ("" when absent)."""
    parts = cmd.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _ensure_skill_commands() -> dict:
    global _skill_commands
    if _skill_commands is None:
        from agent.skill_commands import scan_skill_commands

        _skill_commands = scan_skill_commands()
    return _skill_commands


def get_skill_commands() -> dict:
    return _ensure_skill_commands()


build_skill_invocation_message = _lazy_shim("agent.skill_commands", "build_skill_invocation_message")
build_preloaded_skills_prompt = _lazy_shim("agent.skill_commands", "build_preloaded_skills_prompt")


def get_skill_bundles() -> dict:
    global _skill_bundles
    if _skill_bundles is None:
        from agent.skill_bundles import get_skill_bundles as _impl

        _skill_bundles = _impl()
    return _skill_bundles


build_bundle_invocation_message = _lazy_shim("agent.skill_bundles", "build_bundle_invocation_message")


def _get_plugin_cmd_handler_names() -> set:
    """Return plugin command names (without slash prefix) for dispatch matching."""
    try:
        from hermes_cli.plugins import get_plugin_commands
        return set(get_plugin_commands().keys())
    except Exception:
        return set()


def _parse_skills_argument(skills: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize a CLI skills flag into a deduplicated list of skill identifiers."""
    if not skills:
        return []
    raw_values = [str(item) for item in skills if item is not None] if isinstance(skills, (list, tuple)) else [str(skills)]
    parts = (p.strip() for raw in raw_values for p in raw.split(","))
    return list(dict.fromkeys(p for p in parts if p))


def save_config_value(key_path: str, value: any) -> bool:
    """Persist dot-separated ``key_path`` = value into HERMES_HOME/config.yaml; True on success.

    Never the repo's cli-config.yaml: no config reader loads it, so the value would vanish.
    """
    config_path = get_hermes_home() / 'config.yaml'

    try:
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(config_path.parent)
        from utils import atomic_roundtrip_yaml_update
        atomic_roundtrip_yaml_update(config_path, key_path, value)
        try:  # owner-only: config files contain API keys
            os.chmod(config_path, 0o600)
        except (OSError, NotImplementedError):
            pass
        return True
    except Exception as e:
        logger.error("Failed to save config: %s", e)
        return False


def _persist_global_model_switch(result) -> None:
    """Persist a ``/model --global`` switch to config.yaml.

    Delegates to upstream's canonical ``persist_model_selection()``, which owns
    the ONE config.yaml shape a persisted selection produces: model.default /
    provider / base_url / api_mode, the endpoint-credential reconciliation this
    fork's earlier hand-rolled version existed to do (clearing the PREVIOUS
    provider's inline ``model.base_url`` / ``model.api_key`` / ``model.api_mode``
    — the exo→anthropic class where aux tasks 404'd against the stale endpoint),
    AND the ``model.context_length`` context-pin clear when the route identity
    changed. The hand-rolled version dropped that last part, so a --global switch
    away from a pinned route left the old pin behind and the next startup honored
    a context length belonging to a different endpoint.
    """
    from hermes_cli.model_switch import persist_model_selection

    persist_model_selection(result)

    # FORK: upstream's ``persist_model_selection`` deliberately never WRITES an
    # inline ``model.api_key`` — it only clears stale ones, because its own
    # callers re-submit the key afterwards (dashboard: the settings UI;
    # model_setup_flows: the custom-endpoint wizard). The CLI's ``/model
    # --global`` path has no such re-submit step, so a switch TO a custom
    # endpoint must persist the key the result carries or the endpoint lands in
    # config.yaml without credentials (regression caught by
    # tests/hermes_cli/test_cli_model_switch_persist.py). Guarded on the target
    # being custom: a built-in target's inline key is exactly the leftover
    # upstream clears, and an empty/absent key must not clobber the existing
    # same-route key.
    api_key = getattr(result, "api_key", None)
    if api_key and str(getattr(result, "target_provider", "") or "").strip().lower().startswith("custom"):
        save_config_value("model.api_key", api_key)


# ============================================================================
# HermesCLI Class
# ============================================================================


def _normalize_moa_model(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``moa:<preset>`` -> ``("moa", preset)`` (same routing as ``/moa``); anything else -> ``(None, model)``.

    Returns ``("moa", "<preset>")`` when *model* selects the MoA virtual provider, otherwise ``(None,
    model)`` unchanged. This gives non-interactive ``hermes chat -Q -m moa:<preset>`` the same routing the
    interactive ``/moa`` command and the model picker already use: ``resolve_runtime_provider`` handles
    ``requested_provider == "moa"`` and ``agent_init`` builds the MoAClient off ``provider == "moa"``.
    Without this the raw ``moa:<preset>`` string is sent to the real provider and rejected with a 401/400
    "model not supported" (#56828).
    """
    if isinstance(model, str) and model.strip().lower().startswith("moa:"):
        preset = model.strip().split(":", 1)[1].strip()
        if preset:
            return "moa", preset
    return None, model

_split_model_config_default = _lazy_shim("hermes_cli.config", "split_model_config_default", "_split_model_config_default")


class _VoiceInputMessage:
    """Sentinel for voice-transcribed input so the concise voice prefix never applies to typed text.

    Distinguishes STT output from manually typed text while voice mode is active, so the
    concise-voice-response prefix is applied only to messages that actually came from the microphone
    (#65827).
    """

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text

    def __str__(self) -> str:
        return self.text


class _SeededQueryMessage:
    """Sentinel for a ``-q`` prompt seeded into an interactive session; treated LITERALLY (no slash/!/file-drop)."""

    __slots__ = ("text", "images")

    def __init__(self, text: str, images=None):
        self.text = text or ""
        self.images = list(images or [])

    def __str__(self) -> str:
        return self.text


def _should_seed_interactive(query, image, quiet: bool, oneshot: bool) -> bool:
    """``-q`` seeds an interactive session only on a real TTY without ``--oneshot``/``-Q`` (automation answers and exits)."""
    if not (query or image) or oneshot or quiet:
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


@dataclass
class _ChatTurn:
    """Per-turn state shared by the ``chat()`` phases and the agent worker thread.

    ``result`` is written by the worker and read after the join; ``tts_normal_exit`` is
    set only when the TTS worker drained on its own so the last sentence is never cut.
    """

    result: Optional[dict] = None
    mute_notification_reply: bool = False
    use_streaming_tts: bool = False
    box_opened: bool = False
    thinking_started: bool = False
    text_queue: Optional[queue.Queue] = None
    tts_thread: Optional[threading.Thread] = None
    stream_callback: Optional[Any] = None
    stop_event: Optional[threading.Event] = None
    tts_normal_exit: bool = False
    voice_prefix: str = ""
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin


_PASTE_REF_RE = re.compile(r'\[Pasted text #\d+: \d+ lines \u2192 (.+?)\]')


class HermesCLI(CLIInitMixin, CLITuiRuntimeMixin, CLIProcessNotificationsMixin, CLIAgentSetupMixin, CLICommandsMixin, CLIBillingMixin, CLITuiMixin, CLIStatusBarMixin, CLIVoiceMixin, CLIModelSwitchMixin, CLISessionMixin, CLIStreamMixin, CLIModalMixin, CLITerminalMixin, CLIInfoMixin, CLILoopsMixin, CLIChatTurnMixin):
    """Interactive REPL for the Hermes Agent."""

    # Seeded -q first message (see _should_seed_interactive); run() re-creates
    # _pending_input, so it is enqueued only after the fresh queue exists.
    _seeded_first_message: Optional["_SeededQueryMessage"] = None
    # Inspection surfaces (banner, /tools, status line) read this on partially built instances too.
    disabled_toolsets: Optional[List[str]] = None

    def __init__(
        self,
        model: str = None,
        toolsets: List[str] = None,
        provider: str = None,
        reasoning: str = None,
        api_key: str = None,
        base_url: str = None,
        max_turns: int = None,
        run_budget: float = None,
        verbose: Optional[bool] = None,
        compact: bool = False,
        resume: str = None,
        checkpoints: bool = False,
        pass_session_id: bool = False,
        ignore_rules: bool = False,
    ):
        """CLI args win over config; ``reasoning`` is per-run only; ``resume`` restores history from SQLite."""
        self._init_display_options(verbose, compact)
        self._init_model_routing(model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget,
                                 checkpoints, pass_session_id, ignore_rules)
        self._init_runtime_state(resume)

    def _claim_active_session(self, surface: str = "cli", *, stderr: bool = False) -> bool:
        """Claim a global active-session slot for this CLI process."""
        if self._active_session_lease is not None:
            return True
        try:
            from hermes_cli.active_sessions import format_refusal_stderr, try_acquire_active_session

            lease, message = try_acquire_active_session(
                session_id=self.session_id,
                surface=surface,
                config=self.config,
                # Writer identity: a re-claim by this process replaces its own entry.
                # See #94595.
                metadata={"live_session_id": str(self.session_id)},
            )
        except Exception as exc:
            logger.warning("Failed to claim active session slot: %s", exc)
            return True
        if message:
            print(format_refusal_stderr(message), file=sys.stderr) if stderr else self._console_print(f"[bold red]{message}[/]")
            return False
        self._active_session_lease = lease
        with suppress(Exception):
            atexit.register(self._release_active_session)
        return True

    def _release_active_session(self) -> None:
        lease = getattr(self, "_active_session_lease", None)
        if lease is None:
            return
        try:
            lease.release()
        except Exception:
            logger.debug("Failed to release active session slot", exc_info=True)
        finally:
            self._active_session_lease = None

    def _install_resize_safe_screen_diff_patch(self) -> None:
        """Monkey-patch prompt_toolkit's _output_screen_diff to suppress
        the deliberate "reserve vertical space" scroll-up, but ONLY during
        the transient post-resize recovery window.

        Background: prompt_toolkit's renderer (renderer.py L232-242)
        explicitly moves the cursor to the bottom of the canvas after
        painting "to make sure the terminal scrolls up, even when the
        lower lines of the canvas just contain whitespace".  In
        non-fullscreen mode this scrolls chrome content (status bar,
        input rules) into terminal scrollback on every render.  When
        the terminal column-shrinks, the emulator reflows the previously
        rendered full-width rows into multiple narrower rows that get
        pushed up — leaving ghost duplicates AND polluting scrollback.
        Same issue as pt #29 (open since 2014), #1675, #1933.

        Surgical fix: wrap _output_screen_diff so that when its internal
        `if current_height > previous_screen.height` branch fires (the
        one that does the bottom-cursor-move), we make it fall through
        by inflating previous_screen.height first — but ONLY while
        ``_status_bar_suppressed_after_resize`` is true (the short window
        right after a real terminal resize, set in _recover_after_resize
        and cleared by a debounce timer once reflow settles — see
        _schedule_resize_recovery / _schedule_status_bar_unsuppress).

        Why the gate matters: current_height > previous_screen.height is
        NOT resize-specific — it also fires on ordinary typing whenever
        the completion menu pops open or inline history auto-suggest
        ghost-text appears, since either grows the rendered frame by a
        row. Inflating previous_screen.height on those frames desyncs the
        renderer's cursor-position bookkeeping from the real terminal
        cursor, so the FOLLOWING frame's cell writes land at stale
        offsets — producing fragments of later text overwriting earlier
        text on the input line (reported live: typing "testing that you
        are working" rendered as "tou ing that ye..."). Restricting the
        inflate to the actual resize-recovery window keeps the original
        ghost-status-bar fix intact while no longer misfiring on every
        keystroke that resizes the menu.

        Also retries once with ``previous_screen=None`` on AttributeError/
        TypeError from a corrupt previous paint buffer (classic after tmux
        attach with the same width — "'cell' object has no attribute
        'char'"), so pt takes the first-paint erase path instead of
        wedging the event loop (#83874).
        """
        try:
            import prompt_toolkit.renderer as _pt_renderer
            from prompt_toolkit.renderer import _output_screen_diff as _orig_osd

            if getattr(_pt_renderer, "_hermes_osd_patched", False):
                return

            cli_self = self

            def _patched_output_screen_diff(
                app, output, screen, current_pos, color_depth,
                previous_screen, last_style, is_done, full_screen,
                attrs_for_style_string, style_string_has_style,
                size, previous_width,
            ):
                # Critical: do NOT replace a None previous_screen with a
                # fresh Screen() — that would skip the proper
                # reset_attributes()+erase_down() at L178-185 which fires
                # when previous_screen is None (first-paint / width-
                # change).  Without that reset, ANSI styles leak between
                # renders.
                try:
                    if (
                        previous_screen is not None
                        and hasattr(previous_screen, "height")
                        and getattr(cli_self, "_status_bar_suppressed_after_resize", False)
                    ):
                        if previous_screen.height < screen.height:
                            previous_screen.height = screen.height
                except Exception:
                    pass

                try:
                    return _orig_osd(
                        app, output, screen, current_pos, color_depth,
                        previous_screen, last_style, is_done, full_screen,
                        attrs_for_style_string, style_string_has_style,
                        size, previous_width,
                    )
                except (AttributeError, TypeError):
                    # Corrupt previous_screen / row cells after client
                    # reattach (classic after tmux attach at the same
                    # width — "'cell' object has no attribute 'char'").
                    # Retry once with previous_screen=None so pt takes
                    # the first-paint erase path instead of wedging the
                    # event loop (#83874).
                    return _orig_osd(
                        app, output, screen, current_pos, color_depth,
                        None,  # previous_screen -> first-paint erase path
                        None,  # last_style
                        is_done, full_screen,
                        attrs_for_style_string, style_string_has_style,
                        size, 0,  # previous_width -> treat as changed
                    )

            _pt_renderer._output_screen_diff = _patched_output_screen_diff
            _pt_renderer._hermes_osd_patched = True
        except Exception:
            pass

    # FORK: KEEP-FORK shadow (deliberate) over CLIStatusBarMixin._get_status_bar_session_title.
    # Two fork-only behaviors the mixin lacks:
    #   1. The ``display.status_bar_session_title`` config gate — when off, this returns ""
    #      unconditionally (``_status_bar_session_title_visible``, read as default-True via
    #      getattr so a freshly-constructed CLI matches upstream) so the badge never renders;
    #      title generation and persistence (/resume, `hermes -c`) are untouched. The mixin
    #      has no such gate.
    #   2. The pending-title fast path — a queued ``_pending_title`` short-circuits BEFORE the
    #      1.5s cache-freshness check (no time.monotonic() call, no state.db touch): the
    #      pending value is written into the title cache and returned immediately. In the
    #      mixin the pending value is resolved only after the cache-freshness computation.
    # Do not delete this copy without porting both into the mixin.
    def _get_status_bar_session_title(self) -> str:
        """Return the current title without polling state.db on every repaint.

        FORK: gated by display.status_bar_session_title (default True). When
        disabled, always returns "" so the badge never renders — title
        generation and persistence (used by /resume, `hermes -c`, etc.) are
        untouched; this only hides the status-bar display.
        """
        if not getattr(self, "_status_bar_session_title_visible", True):
            return ""
        pending = str(getattr(self, "_pending_title", None) or "").strip()
        session_id = str(getattr(self, "session_id", "") or "")
        if pending:
            self._status_bar_title_session_id = session_id
            self._status_bar_title_cache = pending
            self._status_bar_title_checked_at = time.monotonic()
            return pending

        now = time.monotonic()
        cached_session_id = getattr(self, "_status_bar_title_session_id", None)
        checked_at = float(getattr(self, "_status_bar_title_checked_at", 0.0) or 0.0)
        if cached_session_id == session_id and now - checked_at < 1.5:
            return str(getattr(self, "_status_bar_title_cache", "") or "")

        title = ""
        db = getattr(self, "_session_db", None)
        if db is not None and session_id:
            try:
                title = str(db.get_session_title(session_id) or "").strip()
            except Exception:
                title = ""
        self._status_bar_title_session_id = session_id
        self._status_bar_title_cache = title
        self._status_bar_title_checked_at = now
        return title

    @staticmethod
    def _format_context_delta(snapshot: dict) -> Optional[str]:
        """Format the per-turn context delta segment, or None to omit it.

        Shown for ANY positive per-turn growth — parity with the other
        always-on status bar pieces (session token counters,
        spinner_token_flow's live ``↓ Nk tok``), not gated behind an
        arbitrary "meaningful" floor. A shrink or flat turn (delta<=0, e.g.
        right after compression) omits the segment since there's nothing to
        attribute a cause to. The cause tag tells the two mechanisms apart
        at a glance:
          ``Δ+23K new``   — fat tool result / genuinely new content this turn
          ``Δ+89K cache`` — prompt cache expired during idle; prefix re-charged
        """
        delta = snapshot.get("context_delta")
        if delta is None or delta <= 0:
            return None
        cause = snapshot.get("context_delta_cause")
        amount = format_token_count_compact(delta)
        if cause == "cache":
            return f"Δ+{amount} cache"
        if cause == "new":
            return f"Δ+{amount} new"
        return f"Δ+{amount}"

    @staticmethod
    def _panel_cwidth(text: str) -> int:
        """Terminal cell width of ``text`` — see ``_panel_ljust`` docstring
        for why plain ``len()`` undercounts wide glyphs in panel sizing.
        Delegates to ``agent.display.display_cwidth()`` — see that
        function's docstring for why plain ``get_cwidth`` also undercounts
        emoji+VS-16 tool glyphs specifically."""
        from agent.display import display_cwidth
        return display_cwidth(text)

    @staticmethod
    def _panel_ljust(text: str, inner_width: int) -> str:
        """Pad ``text`` with trailing spaces to fill ``inner_width`` terminal
        cells (display width), not Python codepoints.

        ``str.ljust()`` pads by character COUNT, which undercounts wide
        glyphs (emoji, CJK, box-drawing) that render as 2 terminal cells but
        are 1 Python character. Modal panels (clarify / approval / sudo /
        secret) that mix such glyphs into otherwise-ASCII rows — e.g. a tool
        emoji, an arrow prefix, a CJK question forwarded from a
        non-English-speaking user, or Fable-5-style unicode-heavy prose via
        the `consult` tool — under-pad by the glyph's extra cell(s). The
        panel's right border then lands one or more columns short of where
        the top/bottom border rules were drawn, visually shifting/clipping
        that row relative to its neighbors — the "garbled/truncated clarify
        panel" symptom. Mirrors ``_status_bar_display_width``'s use of
        ``get_cwidth`` for the same reason.
        """
        current = HermesCLI._panel_cwidth(text)
        pad = max(0, inner_width - current)
        return text + (" " * pad)

    @staticmethod
    def _get_tui_terminal_height(default: tuple[int, int] = (80, 24)) -> int:
        """Return the live prompt_toolkit height, falling back to ``shutil``.

        Height twin of ``_get_tui_terminal_width`` — same prompt_toolkit-first
        ordering and same reason (the TUI layout knows its real size; shutil
        can report stale/fallback values, notably on Termux/mobile shells).
        Used to bound the subagent dock's row budget so a wide/deep delegation
        tree can't grow that panel until it crowds out the conversation.
        """
        try:
            from prompt_toolkit.application import get_app
            return get_app().output.get_size().rows
        except Exception:
            return shutil.get_terminal_size(default).lines

    def _invalidate_app(self) -> None:
        """Ask prompt_toolkit to schedule a re-render.

        Safe to call from any thread.  No-op when no Application is running
        (single-query / non-TUI invocations) — the widget getter will pick
        up board state on the next natural redraw if one occurs.
        """
        try:
            from prompt_toolkit.application import get_app_or_none
            app = get_app_or_none()
        except Exception:
            return
        if app is None:
            return
        try:
            app.invalidate()
        except Exception:
            pass

    # ── Per-turn accounting (display.turn_summary / spinner_token_flow) ──
    #
    # Both features are CLI-only chrome. The tally is observed from the
    # tool-progress callback this class already receives on every tool call,
    # so nothing is threaded through the agent loop. Token flow reads the
    # agent's cumulative session counters (bumped per API call in
    # agent/conversation_loop.py) and subtracts a per-turn baseline.

    _PET_FRAME_INTERVAL = 0.16
    _PET_CFG_INTERVAL = 2.5

    def _apply_reasoning_for_new_model(self, new_model: str) -> None:
        """When switching to a new model, look up its saved per-model reasoning
        effort and apply it. Falls back to the global ``agent.reasoning_effort``
        when no per-model entry exists.
        """
        resolved = _resolve_reasoning_for_model(
            new_model,
            self._reasoning_effort_by_model,
            CLI_CONFIG["agent"].get("reasoning_effort", ""),
        )
        if resolved != self.reasoning_config:
            self.reasoning_config = resolved
            self.agent = None  # Force agent re-init

    # ── Streaming display ────────────────────────────────────────────────

    def _emit_or_defer_post_stream(self, message: str) -> None:
        """Print ``message`` immediately, or defer it until the response box closes.

        Confirmations like "Queued for the next turn" are emitted by the
        UI thread (key handler) while the agent thread may be actively
        streaming response tokens INTO an open box frame.  A direct
        ``_cprint`` from the UI thread races with the streamed lines and
        the confirmation visibly interleaves between body lines, breaking
        the frame.  When the box is open we stash the message and let
        ``_flush_stream`` print it after the closing ``╰───╯``.

        Outside an open box (idle prompt, no agent running, or the
        stream has already drained), print right away — no reason to
        delay.
        """
        try:
            box_open = bool(getattr(self, "_stream_box_opened", False))
            already_drained = bool(getattr(self, "_stream_drained", False))
        except Exception:
            box_open = False
            already_drained = False
        if not box_open or already_drained:
            _cprint(message)
            return
        try:
            with self._post_stream_lock:
                # Re-check inside the lock: _flush_stream may have
                # drained between our check above and acquiring the
                # lock.  If so, fall through to a direct print.
                if getattr(self, "_stream_drained", False):
                    _cprint(message)
                else:
                    self._post_stream_messages.append(message)
        except Exception:
            # Lock missing (very early init) — fall back to direct print
            _cprint(message)

    def _install_tool_callbacks(self) -> None:
        """Install tool callbacks that need the live prompt UI."""
        if self._tool_callbacks_installed:
            return
        set_sudo_password_callback(self._sudo_password_callback)
        set_approval_callback(self._approval_callback)
        set_secret_capture_callback(self._secret_capture_callback)
        from agent.vault_backends.unlock import set_code_prompt_callback, set_save_login_prompt_callback, set_unlock_prompt_callback
        set_unlock_prompt_callback(self._vault_unlock_callback)
        set_save_login_prompt_callback(self._vault_save_login_callback)
        set_code_prompt_callback(self._vault_code_callback)
        self._tool_callbacks_installed = True

    def _ensure_tirith_security(self) -> None:
        """Check tirith availability once before tools can run terminal commands."""
        if self._tirith_security_checked:
            return
        self._tirith_security_checked = True
        try:
            from tools.tirith_security import ensure_installed, is_platform_supported

            if (
                ensure_installed(log_failures=False) is None and is_platform_supported()
                and (self.config.get("security", {}) or {}).get("tirith_enabled", True)
            ):
                _cprint(
                    f"  {_DIM}⚠ tirith security scanner enabled but not available "
                    f"— command scanning will use pattern matching only{_RST}"
                )
        except Exception:
            pass

    def _init_agent(self, *, model_override: str = None, runtime_override: dict = None,
                    request_overrides: dict | None = None) -> bool:
        """FORK shim over ``CLIAgentSetupMixin._init_agent``.

        Upstream's mixin copy is a strict superset of the old inline body (it adds
        ``finalize_preloaded_skills()``, the ``cli._active_agent_ref`` module-global fix
        (#49287), ``run_budget_seconds``/``tool_progress_mode``, the notice/reaction
        callbacks, the headless single-query clarify callback (#94943) and credits seeding),
        so it is the base. Two fork-only wirings it does not know about are re-applied here:

    * ``agent.interleaved_thinking`` — ``agent_init`` only does a post-construction
      attribute assignment for this flag, so setting it here is equivalent to passing
      ``interleaved_thinking=`` to ``AIAgent(...)``.
    * ``agent._cli_ref`` — the back-reference ``tools/delegate_tool_registry.py`` walks to
      find the CLI from a (sub)agent. Set last so a partially built agent is never reachable.
        """
        if not super()._init_agent(
            model_override=model_override,
            runtime_override=runtime_override,
            request_overrides=request_overrides,
        ):
            return False
        if self.agent is not None:
            self.agent.interleaved_thinking = bool(getattr(self, "interleaved_thinking", False))
            self.agent._cli_ref = self
        return True

    def _show_security_advisories(self):
        """Startup banner for unacked security advisories, on stderr (piped stdout stays clean); 24h rate-limited."""
        try:
            from hermes_cli.security_advisories import detect_compromised, startup_banner

            banner = startup_banner(detect_compromised())
            if banner:
                print(banner, file=sys.stderr, flush=True)
        except Exception:
            pass  # never block startup

    def _show_browser_backend_notice(self):
        """Once-per-24h hint when the default Browser Use backend silently fell back to built-in tools."""
        try:
            from tools.browser_use_cli import default_downgrade_notice

            notice = default_downgrade_notice()
            if notice:
                from gateway.warning_notifications import render_notification
                render_notification(lambda: self._console_print(f"[yellow]⚠ {notice}[/yellow]"), platform="cli")
        except Exception:
            logger.debug("browser backend notice failed", exc_info=True)

    def finalize_preloaded_skills(self) -> None:
        """Join the background --skills preload and fold it into the prompt (idempotent).

        Raises ``ValueError`` only when EVERY requested skill was unknown.
        """
        if getattr(self, "_preload_skills_finalized", False):
            return
        thread = getattr(self, "_preload_skills_thread", None)
        if thread is None:
            self._preload_skills_finalized = True
            return
        thread.join(timeout=120)
        self._preload_skills_finalized = True
        err = getattr(self, "_preload_skills_error", None)
        if err is not None:
            raise err
        auto_result = getattr(self, "_auto_load_skills_result", None)
        if auto_result and auto_result[2]:
            logger.warning("skills.auto_load: skill(s) not found or disabled, skipped: %s", ", ".join(auto_result[2]))
        # auto_load names first, then explicit -s names that were not already pinned.
        self.preloaded_skills = list(auto_result[1]) if auto_result else []
        result = getattr(self, "_preload_skills_result", None)
        if not result:
            return
        skills_prompt, loaded_skills, missing_skills = result
        if missing_skills:
            missing_display = ", ".join(missing_skills)
            # A typo'd name must not crash a kanban worker; only a fully-missing set fails loudly.
            if loaded_skills:
                logger.warning(
                    "Unknown skill(s) requested, skipping: %s. "
                    "Continuing with: %s. "
                    "List available skills with `hermes skills list`.",
                    missing_display,
                    ", ".join(loaded_skills),
                )
            else:
                raise ValueError(f"Unknown skill(s): {missing_display}")
        if skills_prompt:
            self.system_prompt = "\n\n".join(p for p in (self.system_prompt, skills_prompt) if p).strip()
        self.preloaded_skills += [name for name in loaded_skills if name not in self.preloaded_skills]

    def _show_tool_availability_warnings(self):
        """Warn about toolsets switched off at startup (missing API keys, unusable terminal backend)."""
        try:
            # Runs on a daemon thread on the snapshot fast path: keep the imports to modules the
            # registry walk already loaded plus the pure notices module (a heavy import here races
            # importlib's module locks against the main thread).
            from model_tools import check_tool_availability
            from hermes_cli.tool_availability_notices import (
                current_terminal_backend, filter_to_enabled_toolsets, tool_availability_warning_lines,
            )
            from tools.terminal_tool import terminal_backend_unavailable_reason
            from toolsets import resolve_toolset

            _, unavailable = check_tool_availability()
            # Only toolsets this CLI session actually has. The selection is usually a composite bundle
            # (``hermes-cli``), so expand it to tool names before matching — a raw name comparison
            # matched nothing on a default install and silently dropped the terminal notice.
            unavailable = filter_to_enabled_toolsets(unavailable, self.enabled_toolsets or [], resolve_toolset)
            lines = tool_availability_warning_lines(
                unavailable, terminal_reason=terminal_backend_unavailable_reason(),
                terminal_backend=current_terminal_backend())
            if lines:
                self._console_print()
                for line in lines:
                    self._console_print(line)
        except Exception:
            pass


    def show_config(self):
        """Display current configuration with kawaii ASCII art."""
        terminal_env = os.getenv("TERMINAL_ENV", "local")
        terminal_cwd = os.getenv("TERMINAL_CWD", os.getcwd())
        terminal_timeout = os.getenv("TERMINAL_TIMEOUT", "60")

        config_path = _hermes_home / 'config.yaml'
        if not config_path.exists():
            config_path = Path(__file__).parent / 'cli-config.yaml'
        config_status = "(loaded)" if config_path.exists() else "(not found)"

        # ``api_key`` may be a callable (Entra ID bearer provider): never invoke it. Prefer the
        # LIVE agent's key: the constructor seeds self.api_key from env before provider
        # resolution, so on non-OpenAI providers it can be another vendor's key.
        from agent.azure_identity_adapter import is_token_provider

        display_key = self.api_key
        if self.agent is not None and getattr(self.agent, "api_key", None):
            display_key = self.agent.api_key
        if is_token_provider(display_key):
            api_key_display = "Microsoft Entra ID"
        elif isinstance(display_key, str) and len(display_key) > 12:
            api_key_display = f"{display_key[:8]}...{display_key[-4:]}"
        else:
            api_key_display = "Not set!"

        title = "(^_^) Configuration"
        width = 50
        pad = width - len(title)
        ssh_target = (
            f"{os.getenv('TERMINAL_SSH_USER', 'not set')}@{os.getenv('TERMINAL_SSH_HOST', 'not set')}"
            f":{os.getenv('TERMINAL_SSH_PORT', '22')}"
        ) if terminal_env == "ssh" else None
        sections = (
            ("Model", (("Model:    ", self.model), ("Base URL: ", self.base_url), ("API Key:  ", api_key_display))),
            ("Terminal", (
                ("Environment: ", terminal_env),
                *((("SSH Target:  ", ssh_target),) if ssh_target else ()),
                ("Working Dir: ", terminal_cwd),
                ("Timeout:     ", f"{terminal_timeout}s"),
            )),
            ("Agent", (
                ("Max Turns: ", self.max_turns),
                ("Toolsets:  ", ", ".join(self.enabled_toolsets) if self.enabled_toolsets else "all"),
                ("Verbose:   ", self.verbose),
            )),
            ("Session", (
                ("Started:    ", self.session_start.strftime("%Y-%m-%d %H:%M:%S")),
                ("Config File:", f"{config_path} {config_status}"),
            )),
        )
        print()
        print("+" + "-" * width + "+")
        print("|" + " " * (pad // 2) + title + " " * (pad - pad // 2) + "|")
        print("+" + "-" * width + "+")
        for name, rows in sections:
            print()
            print(f"  -- {name} --")
            for label, value in rows:
                print(f"  {label} {value}")
        print()

    def _rewind_persisted_user_turn(
        self,
        *,
        warm_history: List[Dict[str, Any]],
        user_ordinal: int,
        warm_live_view: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        """Bind one warm user ordinal to a durable row and rewind it atomically."""
        if self._session_db is None or not self.session_id:
            raise RuntimeError("session database is unavailable")

        from agent.context_compressor import (
            history_before_user_originated_turn,
            split_user_originated_turn,
            user_originated_turn_view,
        )
        from agent.memory_manager import sanitize_context
        from agent.tool_dispatch_helpers import (
            _is_multimodal_tool_result,
            _multimodal_text_summary,
        )
        from run_agent import _is_ephemeral_scaffolding

        def _persistence_content(content: Any) -> Any:
            """Project warm content exactly as the session DB flush does."""
            if _is_multimodal_tool_result(content):
                return _multimodal_text_summary(content)
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(str(part.get("text", "")))
                    elif isinstance(part, dict) and part.get("type") in {
                        "image",
                        "image_url",
                        "input_image",
                    }:
                        text_parts.append("[screenshot]")
                return "\n".join(text_parts) if text_parts else None
            return content

        def _comparison_content(message: Dict[str, Any]) -> Any:
            content = _persistence_content(message.get("content"))
            if message.get("role") in {"user", "assistant"} and isinstance(
                content, str
            ):
                return sanitize_context(content).strip()
            return content

        expected_active_ids = self._session_db.get_active_message_ids(
            self.session_id
        )
        durable = self._session_db.get_messages_as_conversation(
            self.session_id,
            include_row_ids=True,
        )
        warm_persistence_history = [
            message
            for message in warm_history
            if not _is_ephemeral_scaffolding(message)
        ]
        warm_user_indices = [
            index
            for index, message in enumerate(warm_persistence_history)
            if user_originated_turn_view(message) is not None
        ]
        durable_user_indices = [
            index
            for index, message in enumerate(durable)
            if user_originated_turn_view(message) is not None
        ]
        if len(durable_user_indices) != len(warm_user_indices):
            raise RuntimeError(
                "session history changed before the rewind could be persisted"
            )
        if user_ordinal < 0 or user_ordinal >= len(durable_user_indices):
            raise RuntimeError("persisted rewind target is no longer available")

        warm_prefix, _ = history_before_user_originated_turn(
            warm_persistence_history, warm_user_indices[user_ordinal]
        )
        durable_target_index = durable_user_indices[user_ordinal]
        durable_target = durable[durable_target_index]
        durable_prefix, durable_live_view = history_before_user_originated_turn(
            durable, durable_target_index
        )
        if _comparison_content(durable_live_view) != _comparison_content(
            warm_live_view
        ):
            raise RuntimeError(
                "session history changed before the rewind could be persisted"
            )
        target_row_id = durable_target.get("_row_id")
        if not isinstance(target_row_id, int):
            raise RuntimeError("persisted rewind target has no row identity")
        scaffold, _ = split_user_originated_turn(durable_target)
        result = self._session_db.rewind_to_message(
            self.session_id,
            target_row_id,
            preserve_compaction_handoff=scaffold is not None,
            expected_active_ids=expected_active_ids,
            expected_target_content=durable_live_view.get("content"),
        )
        if scaffold is not None:
            replacement_id = result.get("replacement_message_id")
            if not isinstance(replacement_id, int) or not durable_prefix:
                raise RuntimeError("rewind did not retain its compaction handoff")
            durable_prefix[-1]["_row_id"] = replacement_id
            durable_prefix[-1]["_db_persisted"] = True
            warm_prefix[-1] = durable_prefix[-1]
        return warm_prefix, durable_live_view, result
    
    def _run_curses_picker(self, title: str, items: list[str], default_index: int = 0) -> int | None:
        """Run curses_single_select via run_in_terminal so prompt_toolkit handles terminal ownership cleanly."""
        import threading
        from hermes_cli.curses_ui import curses_single_select

        result = [None]

        def _pick():
            result[0] = curses_single_select(title, items, default_index=default_index)

        # run_in_terminal requires an asyncio event loop — only exists in the
        # main prompt_toolkit thread.  If we're in a background thread (e.g.
        # process_loop), fall back to direct curses call.
        in_main_thread = threading.current_thread() is threading.main_thread()

        if self._app and in_main_thread:
            from prompt_toolkit.application import run_in_terminal
            was_visible = self._status_bar_visible
            self._status_bar_visible = False
            self._app.invalidate()
            try:
                run_in_terminal(_pick)
            finally:
                self._status_bar_visible = was_visible
                self._app.invalidate()
        else:
            _pick()

        return result[0]

    def _clear_persisted_context_for_model_switch(self, result) -> None:
        """Drop a global context pin when its configured owner changes."""
        try:
            from hermes_cli.config import load_config_readonly
            from hermes_cli.route_identity import should_clear_context_pin

            config = load_config_readonly()
            model_cfg = config.get("model", {}) if isinstance(config, dict) else {}
            if not isinstance(model_cfg, dict) or "context_length" not in model_cfg:
                return
            if should_clear_context_pin(
                model_cfg.get("default") or model_cfg.get("model"),
                result.new_model,
                model_cfg.get("base_url"),
                result.base_url,
                model_cfg.get("provider"),
                result.target_provider,
            ):
                save_config_value("model.context_length", None)
        except Exception:
            save_config_value("model.context_length", None)

    def _apply_model_switch_result(
        self, result, persist_global: bool, custom_providers=None, reasoning_effort: str = ""
    ) -> None:
        """Picker-path commit (superset of CLIModelSwitchMixin._apply_model_switch_result).

        Keeps the fork's inline staging + per-model ``_apply_reasoning_for_new_model``
        resolution and accepts the mixin's ``reasoning_effort`` (ride-along
        ``--reasoning <level>`` / the picker's effort step), which is applied AFTER the
        agent swap — ``agent.switch_model`` re-resolves ``reasoning_config`` from
        config.yaml and would clobber an earlier write.
        """
        if not result.success:
            _cprint(f"  ✗ {result.error_message}")
            return

        if self.agent is not None:
            try:
                from hermes_cli.context_switch_guard import merge_preflight_compression_warning

                # Prefer the fresh inventory list (same source as switch_model /
                # TUI); fall back to the agent-init snapshot.
                _cp = (
                    custom_providers
                    if custom_providers is not None
                    else getattr(self.agent, "_custom_providers", None)
                )
                merge_preflight_compression_warning(
                    result,
                    agent=self.agent,
                    messages=list(self.conversation_history or []),
                    custom_providers=_cp,
                    config_context_length=getattr(self.agent, "_config_context_length", None),
                )
            except Exception as exc:
                logger.debug("preflight-compression switch warning failed: %s", exc)

        old_model = self.model
        # Snapshot the CLI-level credential/runtime fields BEFORE mutating them
        # so a failed in-place agent swap can roll the whole CLI back to the old
        # working model.  Otherwise the broken credentials staged below leak into
        # the next turn's resolution even though the agent itself rolled back
        # (#50163).
        _cli_snapshot = {
            "model": self.model,
            "provider": self.provider,
            "requested_provider": self.requested_provider,
            "_explicit_api_key": getattr(self, "_explicit_api_key", None),
            "_explicit_base_url": getattr(self, "_explicit_base_url", None),
            "api_key": self.api_key,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
        }
        self.model = result.new_model
        self.provider = result.target_provider
        self.requested_provider = result.target_provider
        # Always overwrite explicit overrides so stale credentials from the
        # previous provider (e.g. Ollama api_key/base_url) don't leak into
        # the new provider's credential resolution on the next turn.
        self._explicit_api_key = result.api_key
        self._explicit_base_url = result.base_url
        if result.api_key:
            self.api_key = result.api_key
        if result.base_url:
            self.base_url = result.base_url
        if result.api_mode:
            self.api_mode = result.api_mode

        # Apply per-model reasoning effort for the new model
        self._apply_reasoning_for_new_model(result.new_model)

        if self.agent is not None:
            try:
                self.agent.switch_model(
                    new_model=result.new_model,
                    new_provider=result.target_provider,
                    api_key=result.api_key,
                    base_url=result.base_url,
                    api_mode=result.api_mode,
                    capabilities=getattr(result, "runtime_capabilities", None),
                )
            except Exception as exc:
                # The agent rolled itself back to the old working model/client.
                # Roll the CLI's own staged fields back too and abort the rest
                # of the commit (note + success print) so a failed switch is a
                # no-op rather than a dead session (#50163).
                for _k, _v in _cli_snapshot.items():
                    setattr(self, _k, _v)
                _cprint(
                    f"  ⚠ Model switch to {result.new_model} failed ({exc}); "
                    f"staying on {old_model}."
                )
                return

        from hermes_cli.model_switch import format_model_for_display
        _display_old = format_model_for_display(old_model)
        _display_new = format_model_for_display(result.new_model)

        self._pending_model_switch_note = (
            f"[Note: model was just switched from {_display_old} to {_display_new} "
            f"via {result.provider_label or result.target_provider}. "
            f"Adjust your self-identification accordingly.]"
        )

        provider_label = result.provider_label or result.target_provider
        _cprint(f"  ✓ Model switched: {_display_new}")
        _cprint(f"    Provider: {provider_label}")

        # Context: always resolve via the provider-aware chain so Codex OAuth,
        # Copilot, and Nous-enforced caps win over the raw models.dev entry
        # (e.g. gpt-5.5 is 1.05M on openai but 272K on Codex OAuth).
        mi = result.model_info
        try:
            from hermes_cli.model_switch import resolve_display_context_length
            ctx = resolve_display_context_length(
                result.new_model,
                result.target_provider,
                base_url=result.base_url or self.base_url or "",
                api_key=result.api_key or self.api_key or "",
                model_info=mi,
                config_context_length=getattr(self.agent, "_config_context_length", None) if self.agent else None,
                custom_providers=getattr(self.agent, "_custom_providers", None) if self.agent else None,
            )
            if ctx:
                _cprint(f"    Context: {ctx:,} tokens")
        except Exception:
            pass
        if mi:
            if mi.max_output:
                _cprint(f"    Max output: {mi.max_output:,} tokens")
            _cprint(f"    Capabilities: {mi.format_capabilities()}")

        cache_enabled = (
            (base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower())
            or result.api_mode == "anthropic_messages"
        )
        if cache_enabled:
            _cprint("    Prompt caching: enabled")
        if result.warning_message:
            _cprint(f"    ⚠ {result.warning_message}")

        # Pick-path ride-along effort (the picker's effort step): applied AFTER the staging
        # swap, since the agent re-resolved reasoning_config from config.yaml. Persistence
        # matches the pick itself (--global).
        if reasoning_effort:
            from hermes_cli.cli_model_switch_mixin import _apply_reasoning_after_switch

            _apply_reasoning_after_switch(self, reasoning_effort, persist_global=persist_global)

        if persist_global:
            HermesCLI._clear_persisted_context_for_model_switch(self, result)
            _persist_global_model_switch(result)
            _cprint("    Saved to config.yaml (--global)")
        else:
            _cprint("    (session only — add --global to persist)")

        # Persist the switch to this session's row so --resume /
        # session.resume restore it. --global also updates config.yaml
        # (future sessions), but the row still records what THIS session
        # actually runs — otherwise a later resume would restore the stale
        # creation-time model over the user's new global choice.
        HermesCLI._persist_model_switch_to_session(self, result)

    def _confirm_and_apply_cli_model_switch(
        self, result, persist_global: bool, one_turn: bool, custom_provs=None, reasoning_effort: str = ""
    ) -> None:
        """Confirm an expensive model switch and apply it to CLI state.

        Runs on a worker thread when the TUI is active (see
        _handle_model_switch) so the confirmation modal can render.

        ``reasoning_effort`` is the ride-along ``/model <name> --reasoning <level>``
        value, passed POSITIONALLY by ``_run_confirm_and_apply``; it is applied after
        the agent swap (``agent.switch_model`` re-resolves ``reasoning_config`` from
        config.yaml and would clobber an earlier write).
        """
        if not self._confirm_expensive_model_switch(result):
            _cprint("  Model switch cancelled.")
            return

        # Apply to CLI state.
        # Update requested_provider so _ensure_runtime_credentials() doesn't
        # overwrite the switch on the next turn (it re-resolves from this).
        old_model = self.model
        _one_turn_restore_snapshot = self._snapshot_model_runtime() if one_turn else None
        # Snapshot CLI-level fields before mutation so a failed in-place swap
        # rolls the whole CLI back to the old working model (#50163).
        _cli_snapshot = {
            "model": self.model,
            "provider": self.provider,
            "requested_provider": self.requested_provider,
            "_explicit_api_key": getattr(self, "_explicit_api_key", None),
            "_explicit_base_url": getattr(self, "_explicit_base_url", None),
            "api_key": self.api_key,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
        }
        self.model = result.new_model
        self.provider = result.target_provider
        self.requested_provider = result.target_provider
        # Always overwrite explicit overrides so stale credentials from the
        # previous provider (e.g. Ollama api_key/base_url) don't leak into
        # the new provider's credential resolution on the next turn.
        self._explicit_api_key = result.api_key
        self._explicit_base_url = result.base_url
        if result.api_key:
            self.api_key = result.api_key
        if result.base_url:
            self.base_url = result.base_url
        if result.api_mode:
            self.api_mode = result.api_mode

        # Apply per-model reasoning effort for the new model
        self._apply_reasoning_for_new_model(result.new_model)

        # Apply to running agent (in-place swap)
        if self.agent is not None:
            try:
                self.agent.switch_model(
                    new_model=result.new_model,
                    new_provider=result.target_provider,
                    api_key=result.api_key,
                    base_url=result.base_url,
                    api_mode=result.api_mode,
                    capabilities=getattr(result, "runtime_capabilities", None),
                )
            except Exception as exc:
                # Agent rolled itself back; roll the CLI back too and abort so a
                # failed switch is a no-op rather than a dead session (#50163).
                for _k, _v in _cli_snapshot.items():
                    setattr(self, _k, _v)
                _cprint(
                    f"  ⚠ Model switch to {result.new_model} failed ({exc}); "
                    f"staying on {old_model}."
                )
                return

        # Store a note to prepend to the next user message so the model
        # knows a switch occurred (avoids injecting system messages mid-history
        # which breaks providers and prompt caching).
        from hermes_cli.model_switch import format_model_for_display
        _display_old = format_model_for_display(old_model)
        _display_new = format_model_for_display(result.new_model)

        self._pending_model_switch_note = (
            f"[Note: model was just switched from {_display_old} to {_display_new} "
            f"via {result.provider_label or result.target_provider}. "
            f"{'This override applies to the next turn only. ' if one_turn else ''}"
            f"Adjust your self-identification accordingly.]"
        )
        if one_turn:
            self._pending_one_turn_model_restore = _one_turn_restore_snapshot
        else:
            self._pending_one_turn_model_restore = None

        # Display confirmation with full metadata
        provider_label = result.provider_label or result.target_provider
        _cprint(f"  ✓ Model switched: {_display_new}")
        _cprint(f"    Provider: {provider_label}")

        # Context: always resolve via the provider-aware chain so Codex OAuth,
        # Copilot, and Nous-enforced caps win over the raw models.dev entry
        # (e.g. gpt-5.5 is 1.05M on openai but 272K on Codex OAuth).
        mi = result.model_info
        from hermes_cli.model_switch import resolve_display_context_length
        ctx = resolve_display_context_length(
            result.new_model,
            result.target_provider,
            base_url=result.base_url or self.base_url or "",
            api_key=result.api_key or self.api_key or "",
            model_info=mi,
            config_context_length=getattr(self.agent, "_config_context_length", None) if self.agent else None,
            custom_providers=getattr(self.agent, "_custom_providers", None) if self.agent else None,
        )
        if ctx:
            _cprint(f"    Context: {ctx:,} tokens")
        if mi:
            if mi.max_output:
                _cprint(f"    Max output: {mi.max_output:,} tokens")
            _cprint(f"    Capabilities: {mi.format_capabilities()}")

        # Cache notice
        cache_enabled = (
            (base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower())
            or result.api_mode == "anthropic_messages"
        )
        if cache_enabled:
            _cprint("    Prompt caching: enabled")

        # Warning from validation
        if result.warning_message:
            _cprint(f"    ⚠ {result.warning_message}")

        # Ride-along effort (--reasoning <level>): applied AFTER the agent swap, since
        # switch_model re-resolved reasoning_config from config.yaml. Session-scoped unless
        # the pick itself persists (--global); --once never writes config.
        if reasoning_effort:
            from hermes_cli.cli_model_switch_mixin import _apply_reasoning_after_switch

            _apply_reasoning_after_switch(
                self, reasoning_effort, persist_global=persist_global and not one_turn
            )

        # Persistence
        if persist_global:
            HermesCLI._clear_persisted_context_for_model_switch(self, result)
            _persist_global_model_switch(result)
            _cprint("    Saved to config.yaml")
        elif one_turn:
            _cprint("    (next turn only — restores after one response)")
        else:
            _cprint("    (session only — add --global to persist)")

        # Persist the switch to this session's row so --resume /
        # session.resume restore it (--global also updates config.yaml but
        # the row still records what THIS session runs; --once is ephemeral
        # and restored after one turn, so it must not touch the row).
        if not one_turn:
            HermesCLI._persist_model_switch_to_session(self, result)

    def _output_console(self):
        """Use prompt_toolkit-safe Rich rendering once the TUI is live."""
        if getattr(self, "_app", None):
            return ChatConsole()
        return self.console


    # canonical command -> (method name, pass cmd_original?). Absent commands resolve to
    # ``_handle_<name>_command(cmd)``. Looked up via getattr at dispatch time so
    # monkeypatching works. A handler returning False exits the REPL.
    _SLASH_DISPATCH: dict[str, tuple[str, bool]] = {
        "exit": ("_cmd_exit", True), "quit": ("_cmd_exit", True), "help": ("_cmd_help", True),
        "palette": ("_open_command_palette", False), "whoami": ("_handle_whoami_command", False),
        "profile": ("_handle_profile_command", False), "toolsets": ("show_toolsets", False),
        "config": ("show_config", False), "redraw": ("_cmd_redraw", True), "clear": ("_cmd_clear", True),
        "history": ("show_history", False), "title": ("_cmd_title", True), "new": ("_cmd_new", True),
        "model": ("_handle_model_switch", True), "codex-runtime": ("_handle_codex_runtime", True),
        "retry": ("_cmd_retry", True), "prompt": ("_handle_prompt_compose_command", True),
        "undo": ("_cmd_undo", True), "save": ("save_conversation", True), "skills": ("_cmd_skills", True),
        "platforms": ("_show_gateway_status", False), "status": ("_show_session_status", False),
        "context": ("_show_context_breakdown", True), "egress": ("_cmd_egress", True),
        "statusbar": ("_cmd_statusbar", True), "verbose": ("_toggle_verbose", False), "yolo": ("_toggle_yolo", False),
        "compress": ("_manual_compress", True), "subscription": ("_show_subscription", False),
        "topup": ("_show_billing", True), "insights": ("_show_insights", True), "update": ("_cmd_update", True),
        "version": ("_cmd_version", True), "paste": ("_handle_paste_command", False), "reload": ("_cmd_reload", True),
        "reload-mcp": ("_confirm_and_reload_mcp", True), "reload-skills": ("_cmd_reload_skills", True),
        "plugins": ("_cmd_plugins", True), "stop": ("_handle_stop_command", False),
        "agents": ("_handle_agents_command", False), "bg": ("_handle_background_command", True),
        "queue": ("_cmd_queue", True), "steer": ("_cmd_steer", True), "moa": ("_cmd_moa", True),
    }

    @classmethod
    def _slash_handler(cls, canonical: str) -> tuple[str, bool] | None:
        """(method name, pass cmd_original?) for a registered command, else None."""
        entry = cls._SLASH_DISPATCH.get(canonical)
        if entry is None:
            name = f"_handle_{canonical.replace('-', '_')}_command"
            if callable(getattr(cls, name, None)):
                entry = (name, True)
        return entry

    def process_command(self, command: str) -> bool:
        """Dispatch a slash command; returns False to exit the REPL."""
        cmd_lower = command.lower().strip()  # lowercase only for matching; args keep their case
        cmd_original = command.strip()

        # Aliases resolve via the central registry (hermes_cli/commands.py).
        from hermes_cli.commands import resolve_command as _resolve_cmd
        _base_word = cmd_lower.split()[0].lstrip("/")
        _cmd_def = _resolve_cmd(_base_word)
        canonical = _cmd_def.name if _cmd_def else _base_word

        # Observer-only pre_command plugin hook (return values ignored; never raises).
        if _cmd_def is not None:
            from hermes_cli.plugins import fire_pre_command_hook
            fire_pre_command_hook(
                surface="cli", command=canonical, alias_used=_base_word, args_raw=_slash_args(cmd_original),
                session_key=getattr(self, "session_id", None), platform="cli",
            )

        # A bare `/resume` prompt is one-shot: any other command disarms it so a later
        # number isn't swallowed as a stale selection.
        # See #34584.
        if canonical not in {"resume", "sessions"}:
            # Armed when a bare `/resume` prints the recent-sessions list so the very next bare numeric
            # input (e.g. `3`) resolves to that session. Holds the exact list used for index resolution;
            # one-shot (cleared on the next submitted input, whether it's the selection or anything else).
            # See #34584.
            self._pending_resume_sessions = None

        entry = self._slash_handler(canonical)
        if entry is None:
            return self._process_unregistered_slash(cmd_original, cmd_lower)
        method_name, pass_arg = entry
        handler = getattr(self, method_name)
        result = handler(cmd_original) if pass_arg else handler()
        return result is not False

    def _process_unregistered_slash(self, cmd_original: str, cmd_lower: str) -> bool:
        """Slash input with no built-in handler; precedence: quick_commands -> plugins -> bundles -> skills -> prefix expansion."""
        base_cmd = cmd_lower.split()[0]
        bare = base_cmd.lstrip("/")
        skill_commands = _ensure_skill_commands()
        skill_bundles = get_skill_bundles()
        quick_commands = self.config.get("quick_commands", {})
        user_args = cmd_original[len(base_cmd):].strip()
        if bare in quick_commands:
            return self._run_quick_command(base_cmd, quick_commands[bare], user_args)
        if bare in _get_plugin_cmd_handler_names():
            self._run_plugin_slash_command(base_cmd, user_args)
        elif base_cmd in skill_bundles:
            self._run_skill_bundle_command(base_cmd, skill_bundles[base_cmd], user_args)
        elif base_cmd in skill_commands:
            self._run_skill_slash_command(base_cmd, skill_commands[base_cmd], user_args)
        else:
            return self._expand_slash_prefix(cmd_original, cmd_lower, skill_commands, skill_bundles)
        return True

    def _run_quick_command(self, base_cmd: str, qcmd: dict, user_args: str) -> bool:
        """User-defined quick command (config.yaml): ``exec`` runs a shell snippet, ``alias`` re-dispatches."""
        qtype = qcmd.get("type")
        if qtype == "alias":
            target = qcmd.get("target", "").strip()
            if target:
                target = target if target.startswith("/") else f"/{target}"
                return self.process_command(f"{target} {user_args}".strip())
            self._console_print(f"[bold red]Quick command '{base_cmd}' has no target defined[/]")
            return True
        if qtype != "exec":
            self._console_print(f"[bold red]Quick command '{base_cmd}' has unsupported type (supported: 'exec', 'alias')[/]")
            return True
        import subprocess
        exec_cmd = qcmd.get("command", "")
        if not exec_cmd:
            self._console_print(f"[bold red]Quick command '{base_cmd}' has no command defined[/]")
            return True
        try:
            # shell=True is intentional (user-authored config snippets, never LLM controlled);
            # the env is sanitized because this process holds every API key.
            from tools.environments.local import build_subprocess_env
            from hermes_cli._subprocess_compat import windows_hide_flags
            result = subprocess.run(
                exec_cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, env=build_subprocess_env(),
                creationflags=windows_hide_flags(),  # no console flash on Windows (#56747)
            )
            # See #56747.
            output = result.stdout.strip() or result.stderr.strip()
            if output:
                from agent.redact import redact_sensitive_text
                self._console_print(_rich_text_from_ansi(redact_sensitive_text(output)))
            else:
                self._console_print("[dim]Command returned no output[/]")
        except subprocess.TimeoutExpired:
            self._console_print("[bold red]Quick command timed out (30s)[/]")
        except Exception as e:
            self._console_print(f"[bold red]Quick command error: {e}[/]")
        return True

    def _run_plugin_slash_command(self, base_cmd: str, user_args: str) -> None:
        from hermes_cli.plugins import get_plugin_command_handler, resolve_plugin_command_result

        plugin_handler = get_plugin_command_handler(base_cmd.lstrip("/"))
        if not plugin_handler:
            return
        try:
            result = resolve_plugin_command_result(plugin_handler(user_args))
            if result:
                _cprint(str(result))
        except Exception as e:
            _cprint(f"\033[1;31mPlugin command error: {e}{_RST}")

    def _drain_process_notifications(self, consumer: str) -> None:
        """FORK: interrupt hold-off in front of ``CLIProcessNotificationsMixin``'s drain.

        A drain injects the formatted completion into ``_pending_input``, which process_loop
        picks up as a new turn on the very next tick — racing the user's interrupt and
        immediately restarting the session they were trying to stop. With several finished
        background processes queued, every Ctrl+C gets clobbered by the next completion.
        Skipping leaves the events in ``completion_queue`` (not lost); they drain on the next
        user-initiated turn, which clears ``_last_turn_interrupted`` at turn start. Mirrors
        the identical guard in ``_drain_cross_session_inbox``.
        """
        if getattr(self, "_last_turn_interrupted", False):
            return
        super()._drain_process_notifications(consumer)

    def _queue_skill_message(self, msg) -> None:
        if hasattr(self, '_pending_input'):
            self._pending_input.put(msg)

    def _run_skill_bundle_command(self, base_cmd: str, bundle_info: dict, user_instruction: str) -> None:
        """``/<bundle>`` loads several skills at once (bundles win over same-named skills)."""
        bundle_result = build_bundle_invocation_message(base_cmd, user_instruction, task_id=self.session_id)
        if not bundle_result:
            ChatConsole().print(f"[bold red]Failed to load bundle for {base_cmd}[/]")
            return
        msg, loaded_names, missing = bundle_result
        self._queue_loaded_skills(msg, f"Loading bundle: {bundle_info['name']} ({len(loaded_names)} skills)", missing)

    def _queue_loaded_skills(self, msg, label: str, missing) -> None:
        print(f"\n⚡ {label}")
        if missing:
            ChatConsole().print(f"[yellow]Skipped missing skills: {', '.join(missing)}[/]")
        self._queue_skill_message(msg)

    def _run_skill_slash_command(self, base_cmd: str, skill_info: dict, rest: str) -> None:
        """``/<skill> ...``; stacked ``/skill-a /skill-b do XYZ`` loads every leading skill (up to 5)."""
        from agent.skill_commands import build_stacked_skill_invocation_message, split_stacked_skill_commands

        extra_keys, user_instruction = split_stacked_skill_commands(rest)
        if extra_keys:
            stacked_result = build_stacked_skill_invocation_message(
                [base_cmd, *extra_keys], user_instruction, task_id=self.session_id,
            )
            if not stacked_result:
                ChatConsole().print(f"[bold red]Failed to load stacked skills for {base_cmd}[/]")
                return
            msg, loaded_names, missing = stacked_result
            self._queue_loaded_skills(
                msg, f"Loading {len(loaded_names)} stacked skills: {', '.join(loaded_names)}", missing
            )
            return
        msg = build_skill_invocation_message(base_cmd, rest, task_id=self.session_id)
        if msg:
            self._queue_loaded_skills(msg, f"Loading skill: {skill_info['name']}", None)
        else:
            ChatConsole().print(f"[bold red]Failed to load skill for {base_cmd}[/]")

    def _expand_slash_prefix(self, cmd_original: str, cmd_lower: str, skill_commands, skill_bundles) -> bool:
        """Unique-prefix expansion against built-in COMMANDS + skill commands/bundles (agrees with tab-completion)."""
        from hermes_cli.commands import COMMANDS
        typed_base = cmd_lower.split()[0]
        all_known = set(COMMANDS) | set(skill_commands) | set(skill_bundles)
        matches = [c for c in all_known if c.startswith(typed_base)]
        if len(matches) > 1:
            if typed_base in matches:
                matches = [typed_base]
            else:
                # Unique shortest match wins: /qui -> /quit (5) over /quint-pipeline (15)
                min_len = min(len(c) for c in matches)
                shortest = [c for c in matches if len(c) == min_len]
                if len(shortest) == 1:
                    matches = shortest
        if len(matches) == 1 and matches[0] != typed_base:
            # Expand to the full name, preserving arguments.
            return self.process_command(matches[0] + cmd_original.strip()[len(typed_base):])
        if len(matches) > 1:
            _cprint(f"{_ACCENT}Ambiguous command: {cmd_lower}{_RST}")
            _cprint(f"{_DIM}Did you mean: {', '.join(sorted(matches))}?{_RST}")
        else:
            # Exact token with no handler (never re-dispatch the same token: recursion), or no match.
            from hermes_cli.cli_unknown_command import unknown_command_lines
            lead, pointer = unknown_command_lines(cmd_lower, all_known)
            _cprint(f"\033[1;31m{lead}{_RST}")
            _cprint(f"{_DIM}{_ACCENT}{pointer}{_RST}")
        return True

    def _drain_cross_session_inbox(self) -> None:
        """Deliver cross-session agent messages addressed to this CLI session.

        Transport B's idle-recipient path (docs/design/cross-session-messaging.md,
        "Delivery mechanics"). Runs on the same idle tick as
        ``_drain_process_notifications`` and reuses the same proven
        claim-then-inject shape: the transport claims each row atomically
        before returning it, and this method pushes it onto ``_pending_input``
        — the sink that starts a fresh turn on the next 0.1s tick.

        What is injected is ``DrainedMessage.framed_body``, which the
        transport already wrapped with ``build_agent_message_marker()``. The
        raw body is not reachable from here at all. Injecting an unwrapped
        body would be indistinguishable from operator-authored input and
        would defeat the untrusted-content framing this feature depends on.

        Held messages fire ``_fire_attention_signals`` — the same config-gated
        terminal bell + macOS notification used for approval/clarify prompts —
        rather than a parallel notification path.

        Housekeeping (reaping dead registry rows, expiring stale holds) is
        throttled inside ``maintenance_tick``; this call site fires at 10Hz
        and must not run two DB writes per tick.

        Interrupt hold-off: same rationale as ``_drain_process_notifications``.
        A drain injects a framed message into ``_pending_input``, which
        process_loop picks up as a new turn on the very next 0.1s tick —
        racing the user's interrupt and immediately re-starting the session
        they were trying to stop. Skip the drain entirely while
        ``_last_turn_interrupted`` is set; the rows stay ``pending`` in
        ``cross_session_inbox`` (not lost, not silently dropped) and are
        drained on the next user-initiated turn, which resets
        ``_last_turn_interrupted`` at turn start and drains normally at its
        post-turn hook.
        """
        if getattr(self, "_last_turn_interrupted", False):
            return
        from tools.cross_session_integration import (
            drain_to_idle_injection,
            install_transport,
            maintenance_tick,
            register_session_participant_for,
        )

        session_key = getattr(self, "session_id", "") or ""
        if not session_key:
            return
        # Idempotent; guarantees this process participates in
        # resolve_transport()'s fan-out without a separate startup call site
        # that a non-CLI entrypoint could forget.
        install_transport()
        # Same reasoning for Transport A: make this session addressable
        # in-process so a background subagent's send_to_parent resolves
        # directly instead of falling through to Transport B's approval gate.
        # Additive and idempotent, so re-running it here also picks up any
        # session_id reassignment (resume, rename) and refreshes the stored
        # agent reference after an agent reinit, without ever dropping an
        # older id an in-flight subagent is still keyed to.
        register_session_participant_for(getattr(self, "agent", None), self)
        maintenance_tick()
        drain_to_idle_injection(
            session_id=session_key,
            inject=self._pending_input.put,
            on_held=self._fire_attention_signals,
        )

    def _drain_interrupt_queue_to_pending_input(self) -> None:
        """Move stray ``_interrupt_queue`` messages into ``_pending_input`` after every turn.

        Busy-time input lands in ``_interrupt_queue`` and is only drained by the explicit
        interrupt path; a turn that finishes naturally would otherwise strand it and the
        CLI appears to hang. Never raises.

        Called once at the end of every turn from ``process_loop``'s ``finally`` block. Catches and swallows
        ``Exception`` because the drain must never break the main loop. (#20271)
        """
        try:
            while not self._interrupt_queue.empty():
                stray = self._interrupt_queue.get_nowait()
                if stray:
                    self._pending_input.put(stray)
        except Exception:
            pass

    def _reasoning_levels_for_active_model(self) -> list[str]:
        """Return the reasoning levels that make sense for the active model.

        Different reasoning ecosystems support different tiers:

        * DSv4-Flash supports **three** documented modes per its
          HuggingFace model card (deepseek-ai/DeepSeek-V4-Flash):

            - Non-think → ``none`` (enable_thinking=False)
            - Think High → ``high`` (the default thinking mode)
            - Think Max → ``xhigh`` (mapped to reasoning_effort="max"
              by exo's _v4_reasoning_effort wrapper; needs ≥384K context)

          ``minimal``/``low``/``medium`` are NOT distinct modes on DSv4;
          they all collapse to "default thinking" (= Think High) through
          exo's wrapper. Listing them in the picker would be misleading,
          so we omit them.
        * MiniMax has binary thinking only.
        * Anthropic adaptive thinking (Claude 4.6+) exposes a fixed
          set of levels: low/medium/high/max on 4.6, plus xhigh on 4.7.
          The adapter aliases ``minimal`` → ``low``
          (``agent/anthropic_adapter.py:ADAPTIVE_EFFORT_MAP``), so
          ``minimal`` is omitted to avoid a duplicate of ``low``.
        * Tiered-reasoning models (gpt-5, o-series, openrouter
          passthroughs) keep the full six-tier ladder.
        """
        full_ladder = ["none", "minimal", "low", "medium", "high", "xhigh"]
        m = (self.model or "").lower()
        if "deepseek" in m or "dsv4" in m:
            # DSv4 3-mode set per HF model card.
            return ["none", "high", "xhigh"]
        if "minimax" in m:
            return ["none", "on"]
        try:
            from agent.anthropic_adapter import (
                _supports_adaptive_thinking,
                _supports_xhigh_effort,
            )
            if _supports_adaptive_thinking(self.model or ""):
                levels = ["none", "low", "medium", "high"]
                if _supports_xhigh_effort(self.model or ""):
                    levels.append("xhigh")
                levels.append("max")
                return levels
        except Exception:
            pass
        # Default to full ladder when uncertain — overshooting is
        # better than locking out a real reasoning model.
        return full_ladder

    def _open_reasoning_picker(self) -> None:
        """Open the /reasoning prompt_toolkit-native picker modal."""
        levels = self._reasoning_levels_for_active_model()
        # Display labels per level. Generic ladder gets bare names;
        # DSv4 / MiniMax get a hint about what each tier means since
        # those models map effort levels through model-specific wrappers.
        m = (self.model or "").lower()
        is_dsv4 = "deepseek" in m or "dsv4" in m
        # Hints reflect the three modes documented on DSv4-Flash's
        # HuggingFace model card. Kept short to fit a typical panel
        # width without wrapping; the model card is the canonical
        # reference for full descriptions.
        dsv4_hints = {
            "none": "none — Non-think (fast)",
            "high": "high — Think High (default)",
            "xhigh": "xhigh — Think Max (needs ≥384K ctx)",
        }
        binary_hints = {
            "none": "none (no thinking)",
            "on": "on (thinking enabled)",
        }
        choices: list[dict] = []
        for level in levels:
            if is_dsv4:
                label = dsv4_hints.get(level, level)
            elif level in binary_hints:
                label = binary_hints[level]
            else:
                label = level
            choices.append({"key": level, "label": label, "kind": "level"})
        choices.append({"key": "show", "label": "show — render model thinking inline", "kind": "display"})
        choices.append({"key": "hide", "label": "hide — suppress model thinking", "kind": "display"})
        choices.append({"key": "__cancel__", "label": "Cancel", "kind": "cancel"})

        # Default selection: current effort if listed, else 0.
        current_level = self._current_reasoning_level_label()
        default_idx = next(
            (i for i, c in enumerate(choices) if c["kind"] == "level" and c["key"] == current_level),
            0,
        )

        self._capture_modal_input_snapshot()
        self._reasoning_picker_state = {
            "choices": choices,
            "selected": default_idx,
            "current_level": current_level,
            "current_display": "on" if self.show_reasoning else "off",
            "_scroll_offset": 0,
        }
        self._invalidate(min_interval=0.0)

    def _close_reasoning_picker(self) -> None:
        self._reasoning_picker_state = None
        self._restore_modal_input_snapshot()
        self._invalidate(min_interval=0.0)

    def _current_reasoning_level_label(self) -> str:
        """Return the active reasoning effort as one of the user-facing
        keys (``none`` / ``on`` / ``minimal`` / ``low`` / ... ).
        """
        rc = self.reasoning_config
        if rc is None:
            return "medium"
        if rc.get("enabled") is False:
            return "none"
        # Binary models report any enabled state as "on".
        if "on" in self._reasoning_levels_for_active_model():
            return "on"
        return rc.get("effort", "medium")

    def _handle_reasoning_picker_selection(self) -> None:
        """Apply the picker selection on Enter."""
        state = self._reasoning_picker_state
        if not state:
            return
        choices = state.get("choices") or []
        idx = state.get("selected", 0)
        if idx < 0 or idx >= len(choices):
            self._close_reasoning_picker()
            return
        choice = choices[idx]
        kind = choice.get("kind")
        key = choice.get("key")
        self._close_reasoning_picker()
        if kind == "cancel":
            return
        if kind == "display":
            # Reuse the existing show/hide path.
            self._apply_reasoning_arg(key)
            return
        if kind == "level":
            self._apply_reasoning_arg(key)

    def _apply_reasoning_arg(self, arg: str, *, persist_global: bool = False) -> None:
        """Shared apply path for the picker's effort/display rows.

        Session-scoped unless ``persist_global``: the typed form (``/reasoning <level>``,
        ``/effort <level>``) became session-only-with-``--global`` upstream (#86414), and this
        path has to agree with it or one command would carry two persistence policies. The
        per-model map write is deliberately NOT gated on ``persist_global`` — it is this fork's
        ``agent.reasoning_effort_by_model`` isolation feature (read by
        ``_apply_reasoning_for_new_model`` on every model switch) and this helper is its only
        writer, so gating it would leave the documented feature write-dead.
        """
        arg = arg.strip().lower()
        if arg in ("show", "on") and arg != "on":
            self.show_reasoning = True
            if self.agent:
                self.agent.reasoning_callback = self._current_reasoning_callback()
            save_config_value("display.show_reasoning", True)
            _cprint(f"  {_ACCENT}✓ Reasoning display: ON (saved){_RST}")
            return
        if arg in {"hide", "off"}:
            self.show_reasoning = False
            if self.agent:
                self.agent.reasoning_callback = self._current_reasoning_callback()
            save_config_value("display.show_reasoning", False)
            _cprint(f"  {_ACCENT}✓ Reasoning display: OFF (saved){_RST}")
            return
        # "on" for binary models maps to enable_thinking=True (effort
        # value doesn't matter for DSv4 — exo treats anything non-none
        # as enable_thinking=True).
        if arg == "on":
            arg = "medium"
        parsed = _parse_reasoning_config(arg)
        if parsed is None:
            _cprint(f"  {_DIM}(._.) Unknown argument: {arg}{_RST}")
            return
        self.reasoning_config = parsed
        self.agent = None  # Force agent re-init with new reasoning config

        # Global default: only on an explicit persistence request.
        saved_global = persist_global and save_config_value("agent.reasoning_effort", arg)

        # Per-model default so switching back to this model restores it.
        current_model = (self.model or "").strip()
        if current_model:
            by_model = dict(self._reasoning_effort_by_model)
            by_model[current_model] = arg
            save_config_value("agent.reasoning_effort_by_model", by_model)
            self._reasoning_effort_by_model = by_model
            scope = "saved to config" if saved_global else f"session; default for {current_model}"
        else:
            scope = "saved to config" if saved_global else "session only"
        _cprint(f"  {_ACCENT}✓ Reasoning effort set to '{arg}' ({scope}){_RST}")

    # ── /delegation — ruflo agent persona → model assignments ─────────────

    # Curated short list shown in the model-picker. Other model names can
    # still be set via the typed form `/delegation <role> <model>`.
    _DELEGATION_MODEL_CHOICES = (
        ("claude-haiku-4-5", "Haiku 4.5 — cheapest, fast retrieval / triage"),
        ("claude-sonnet-4-6", "Sonnet 4.6 — balanced (good default for analysis)"),
        ("claude-opus-4-7", "Opus 4.7 — deepest reasoning, most expensive"),
    )

    def _handle_delegation_command(self, cmd: str) -> None:
        """Handle /delegation — configure ruflo agent persona → model map.

        Usage:
            /delegation                       Open interactive picker
            /delegation <role>                Show current pin / pick a model
            /delegation <role> <model>        Pin role to model
            /delegation <role> clear          Remove the pin (revert to inherit)
            /delegation list                  Print the current map
            /delegation defaults              Apply curated defaults (preserves
                                              user pins; only fills empties)
            /delegation defaults --force      Apply curated defaults, OVERWRITING
                                              any existing user pins
            /delegation stats                 Show per-role observed metrics
                                              (n, success%, avg duration/tokens,
                                              total cost)
            /delegation stats --suggest       Same + heuristic re-tune hints
            /delegation stats --role <name>   Restrict to one role
            /delegation stats --days <N>      Restrict to last N days
            /delegation parallel              Show current max parallel children
            /delegation parallel <N>          Set delegation.max_concurrent_children
            /delegation parallel pick         Open the curses picker
            /delegation depth                 Show current max spawn depth
            /delegation depth <N>             Set delegation.max_spawn_depth (1-3)
            /delegation depth pick            Open the curses picker
        """
        parts = cmd.strip().split(maxsplit=2)

        if len(parts) >= 2 and parts[1].lower() == "list":
            self._print_delegation_map()
            return

        if len(parts) >= 2 and parts[1].lower() == "stats":
            # `/delegation stats [--suggest] [--role X] [--days N]`
            rest = cmd.strip().split()[2:]  # everything after `/delegation stats`
            self._print_delegation_stats(rest)
            return

        if len(parts) >= 2 and parts[1].lower() == "defaults":
            force = len(parts) >= 3 and parts[2].strip().lower() in (
                "--force",
                "force",
                "-f",
                "overwrite",
            )
            self._apply_delegation_defaults(overwrite=force)
            return

        if len(parts) >= 2 and parts[1].lower() in ("parallel", "concurrency"):
            arg = parts[2].strip() if len(parts) >= 3 else ""
            if not arg:
                self._show_delegation_concurrency()
                return
            if arg.lower() in ("pick", "picker", "menu"):
                self._open_delegation_concurrency_picker()
                return
            self._apply_delegation_concurrency(arg)
            return

        if len(parts) >= 2 and parts[1].lower() == "depth":
            arg = parts[2].strip() if len(parts) >= 3 else ""
            if not arg:
                self._show_delegation_depth()
                return
            if arg.lower() in ("pick", "picker", "menu"):
                self._open_delegation_depth_picker()
                return
            self._apply_delegation_depth(arg)
            return

        if len(parts) >= 3:
            role = parts[1].strip()
            model = parts[2].strip()
            self._apply_delegation_assignment(role, model)
            return

        if len(parts) == 2:
            # /delegation <role> — show + pick model
            self._open_delegation_model_picker(parts[1].strip())
            return

        # No args → open the agent picker.
        self._open_delegation_agent_picker()

    def _apply_delegation_defaults(self, *, overwrite: bool) -> None:
        try:
            from hermes_cli.ruflo_agents import (
                apply_suggested_defaults,
                SUGGESTED_ROLE_MODELS,
            )
        except Exception:
            _cprint(f"  {_DIM}(._.) Delegation module not available{_RST}")
            return
        applied, skipped = apply_suggested_defaults(overwrite=overwrite)
        total = len(SUGGESTED_ROLE_MODELS)
        if applied == 0 and skipped == 0:
            _cprint(f"  {_DIM}(>_<) Failed to save defaults{_RST}")
            return
        mode = "overwriting existing pins" if overwrite else "preserving existing pins"
        _cprint(
            f"  {_ACCENT}✓ Applied curated defaults: {applied} updated, "
            f"{skipped} kept ({total} curated total, {mode}){_RST}"
        )
        if applied > 0:
            _cprint(
                f"  {_DIM}Run /delegation list to inspect, /delegation <role> "
                f"to re-pin individually.{_RST}"
            )

    def _print_delegation_stats(self, args: list) -> None:
        """Print per-role aggregated stats from delegation_stats.json.

        Flags:
            --suggest         Also show heuristic re-tune suggestions
            --role <name>     Restrict to one role
            --days <N>        Restrict to records from the last N days
        """
        try:
            from hermes_cli.delegation_stats import (
                aggregate,
                load_all,
                suggest_retunes,
            )
        except Exception:
            _cprint(f"  {_DIM}(._.) Delegation stats module not available{_RST}")
            return

        # Parse flags (lightweight; we only have three).
        suggest_only = False
        role_filter: Optional[str] = None
        since_ts: Optional[float] = None
        i = 0
        while i < len(args):
            a = args[i].lower()
            if a in ("--suggest", "suggest"):
                suggest_only = True
                i += 1
            elif a in ("--role", "-r") and i + 1 < len(args):
                role_filter = args[i + 1]
                i += 2
            elif a in ("--days", "-d") and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                    since_ts = time.time() - (days * 86400.0)
                except ValueError:
                    _cprint(f"  {_DIM}Invalid --days value: {args[i + 1]}{_RST}")
                    return
                i += 2
            else:
                _cprint(f"  {_DIM}(._.) Unknown stats flag: {args[i]}{_RST}")
                return

        all_stats = load_all()
        if not all_stats:
            _cprint(
                f"  {_DIM}No delegation stats yet. They start collecting on "
                f"the next /delegate-driven run.{_RST}"
            )
            return

        aggs = aggregate(all_stats, since_ts=since_ts, role=role_filter)
        if not aggs:
            _cprint(f"  {_DIM}No matching records.{_RST}")
            return

        # Header: role, model, n, ok%, hit_max%, avg dur, avg out tok, total $
        # Build dynamic widths so role names fit.
        role_w = max(4, max(len(a.role) for a in aggs))
        model_w = max(5, max(len(a.model) for a in aggs))
        header = (
            f"  {'role':<{role_w}}  {'model':<{model_w}}  "
            f"{'n':>3}  {'ok%':>4}  {'max%':>4}  "
            f"{'avg_dur':>8}  {'avg_out':>8}  {'total $':>9}"
        )
        _cprint(header)
        _cprint(f"  {'─' * (len(header) - 2)}")
        for a in aggs:
            ok_pct = f"{a.success_rate * 100:.0f}%" if a.n else "—"
            max_pct = f"{a.hit_max_rate * 100:.0f}%" if a.n else "—"
            dur = f"{a.avg_duration:.0f}s"
            out_tok = f"{a.avg_output:.0f}"
            cost = f"${a.total_cost:.4f}"
            _cprint(
                f"  {a.role:<{role_w}}  {a.model:<{model_w}}  "
                f"{a.n:>3}  {ok_pct:>4}  {max_pct:>4}  "
                f"{dur:>8}  {out_tok:>8}  {cost:>9}"
            )

        # Suggestions
        suggestions = suggest_retunes(aggs)
        if suggestions:
            _cprint("")
            _cprint(f"  {_ACCENT}Suggested re-tunes (run /delegation <role> "
                    f"<model> to apply):{_RST}")
            for s in suggestions:
                arrow = "↑ promote" if s.direction == "promote" else "↓ demote"
                _cprint(
                    f"    {arrow}  {s.role:<{role_w}}  "
                    f"{s.current_model} → {s.suggested_model}"
                )
                _cprint(f"      {_DIM}{s.reason}{_RST}")
        elif suggest_only:
            _cprint("")
            _cprint(
                f"  {_DIM}No suggestions — every role with ≥5 samples is "
                f"performing within thresholds for its current model.{_RST}"
            )

    def _print_delegation_map(self) -> None:
        try:
            from hermes_cli.ruflo_agents import get_role_model_map
        except Exception:
            _cprint(f"  {_DIM}(._.) Delegation module not available{_RST}")
            return
        m = get_role_model_map()
        if not m:
            _cprint(f"  {_DIM}No per-role model assignments configured.{_RST}")
            _cprint(
                f"  {_DIM}Run /delegation to open the picker, or "
                f"/delegation <role> <model>.{_RST}"
            )
            return
        _cprint("  Current ruflo persona → model assignments:")
        width = max(len(k) for k in m.keys())
        for role in sorted(m.keys()):
            _cprint(f"    {role:<{width}}  →  {m[role]}")

    def _apply_delegation_assignment(self, role: str, model: str) -> None:
        """Pin (or clear) a per-role model assignment and persist."""
        try:
            from hermes_cli.ruflo_agents import set_role_model, lookup_agent
        except Exception:
            _cprint(f"  {_DIM}(._.) Delegation module not available{_RST}")
            return
        if not role:
            _cprint(f"  {_DIM}(._.) Role name required{_RST}")
            return
        # Sanity check: the role should match a discovered ruflo agent.
        # We don't HARD-fail unknowns (the user may map a custom role
        # they invent), but warn so typos are obvious.
        try:
            agent = lookup_agent(role)
        except Exception:
            agent = None
        clear = model.lower() in ("clear", "none", "inherit", "unset", "")
        ok = set_role_model(role, None if clear else model)
        if not ok:
            _cprint(f"  {_DIM}(>_<) Failed to save delegation map{_RST}")
            return
        if clear:
            _cprint(f"  {_ACCENT}✓ Cleared model pin for '{role}' (saved){_RST}")
        else:
            note = "" if agent else f"  {_DIM}(role not found in ruflo — saved anyway){_RST}"
            _cprint(f"  {_ACCENT}✓ '{role}' → {model} (saved){_RST}{note}")

    # ------------------------------------------------------------------
    # /delegation parallel  — max_concurrent_children
    # /delegation depth     — max_spawn_depth
    # ------------------------------------------------------------------

    # Curated picker rows for parallel-children. Users can also set
    # arbitrary integers via `/delegation parallel <N>`.
    _DELEGATION_PARALLEL_CHOICES: tuple[tuple[int, str], ...] = (
        (1, "1   — serial (one child at a time)"),
        (3, "3   — default (Hermes ships with this)"),
        (5, "5   — moderate fan-out"),
        (8, "8   — aggressive (watch your token spend)"),
        (12, "12  — heavy (cost scales linearly)"),
        (20, "20  — schema ceiling for delegate_task batch size"),
    )

    _DELEGATION_DEPTH_CHOICES: tuple[tuple[int, str], ...] = (
        (1, "1   — flat: parent → leaf children only (default)"),
        (2, "2   — orchestrator: children may spawn their own workers"),
        (3, "3   — three-level: rarely needed; cost compounds"),
    )

    @staticmethod
    def _read_delegation_int(key: str, default: int) -> int:
        """Read delegation.<key> from the active config, fallback to default."""
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            val = (cfg.get("delegation") or {}).get(key)
            return int(val) if val is not None else default
        except Exception:
            return default

    @staticmethod
    def _save_delegation_int(key: str, value: int) -> bool:
        """Persist delegation.<key> = value into active config.yaml."""
        try:
            from hermes_cli.personas import _save_to_config_yaml
            return _save_to_config_yaml(f"delegation.{key}", int(value))
        except Exception:
            return False

    def _show_delegation_concurrency(self) -> None:
        cur = self._read_delegation_int("max_concurrent_children", 3)
        _cprint(
            f"  {_ACCENT}delegation.max_concurrent_children = {cur}{_RST}  "
            f"{_DIM}(parallel children per delegate_task batch){_RST}"
        )
        _cprint(
            f"  {_DIM}Change with: /delegation parallel <N> "
            f"or /delegation parallel pick{_RST}"
        )

    def _apply_delegation_concurrency(self, arg: str) -> None:
        try:
            n = int(arg)
        except (TypeError, ValueError):
            _cprint(f"  {_DIM}(._.) Expected an integer, got {arg!r}{_RST}")
            return
        if n < 1:
            _cprint(f"  {_DIM}(._.) Must be ≥ 1{_RST}")
            return
        if not self._save_delegation_int("max_concurrent_children", n):
            _cprint(f"  {_DIM}(>_<) Failed to save delegation.max_concurrent_children{_RST}")
            return
        warn = ""
        if n > 10:
            warn = f"  {_DIM}(heads up: each child costs API tokens — cost scales linearly){_RST}"
        _cprint(
            f"  {_ACCENT}✓ delegation.max_concurrent_children = {n} (saved){_RST}{warn}"
        )

    def _open_delegation_concurrency_picker(self) -> None:
        try:
            from hermes_cli.curses_ui import curses_radiolist
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) Picker unavailable: {e}{_RST}")
            return
        cur = self._read_delegation_int("max_concurrent_children", 3)
        items: list[str] = []
        actions: list[Optional[int]] = []
        for n, label in self._DELEGATION_PARALLEL_CHOICES:
            marker = "  ●" if n == cur else "   "
            items.append(f"{marker}  {label}")
            actions.append(n)
        items.append("       Cancel")
        actions.append(None)
        default_idx = next((i for i, n in enumerate(actions) if n == cur), 0)
        try:
            picked = curses_radiolist(
                title="Pick max parallel children for delegate_task",
                items=items,
                selected=default_idx,
                cancel_returns=-1,
                description=(
                    f"Currently: {cur}\n"
                    "Each running child consumes API tokens independently. "
                    "Higher values fan out faster but cost scales linearly.\n"
                    "Override per-batch via DELEGATION_MAX_CONCURRENT_CHILDREN env var."
                ),
            )
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) Picker failed: {e}{_RST}")
            return
        if picked is None or picked < 0 or picked >= len(actions):
            return
        n = actions[picked]
        if n is None:
            return
        self._apply_delegation_concurrency(str(n))

    def _show_delegation_depth(self) -> None:
        cur = self._read_delegation_int("max_spawn_depth", 1)
        meanings = {1: "flat", 2: "orchestrator", 3: "three-level"}
        meaning = meanings.get(cur, f"clamped → {cur}")
        _cprint(
            f"  {_ACCENT}delegation.max_spawn_depth = {cur}{_RST}  "
            f"{_DIM}({meaning}){_RST}"
        )
        _cprint(
            f"  {_DIM}Change with: /delegation depth <1|2|3> "
            f"or /delegation depth pick{_RST}"
        )

    def _apply_delegation_depth(self, arg: str) -> None:
        try:
            n = int(arg)
        except (TypeError, ValueError):
            _cprint(f"  {_DIM}(._.) Expected an integer, got {arg!r}{_RST}")
            return
        if n < 1 or n > 3:
            _cprint(f"  {_DIM}(._.) Depth must be 1, 2, or 3{_RST}")
            return
        if not self._save_delegation_int("max_spawn_depth", n):
            _cprint(f"  {_DIM}(>_<) Failed to save delegation.max_spawn_depth{_RST}")
            return
        _cprint(f"  {_ACCENT}✓ delegation.max_spawn_depth = {n} (saved){_RST}")

    def _open_delegation_depth_picker(self) -> None:
        try:
            from hermes_cli.curses_ui import curses_radiolist
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) Picker unavailable: {e}{_RST}")
            return
        cur = self._read_delegation_int("max_spawn_depth", 1)
        items: list[str] = []
        actions: list[Optional[int]] = []
        for n, label in self._DELEGATION_DEPTH_CHOICES:
            marker = "  ●" if n == cur else "   "
            items.append(f"{marker}  {label}")
            actions.append(n)
        items.append("       Cancel")
        actions.append(None)
        default_idx = next((i for i, n in enumerate(actions) if n == cur), 0)
        try:
            picked = curses_radiolist(
                title="Pick max spawn depth for delegate_task children",
                items=items,
                selected=default_idx,
                cancel_returns=-1,
                description=(
                    f"Currently: {cur}\n"
                    "Depth 1 = flat (most cases). Depth 2 lets orchestrator "
                    "children spawn their own workers. Depth 3 is rarely "
                    "useful and compounds cost."
                ),
            )
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) Picker failed: {e}{_RST}")
            return
        if picked is None or picked < 0 or picked >= len(actions):
            return
        n = actions[picked]
        if n is None:
            return
        self._apply_delegation_depth(str(n))

    def _open_delegation_agent_picker(self) -> None:
        """Curses radiolist over discovered ruflo agents.

        ENTER on a row → opens the model picker for that agent.
        ESC bails to the prompt.
        """
        try:
            from hermes_cli.ruflo_agents import (
                discover_ruflo_agents,
                get_role_model_map,
                group_by_category,
            )
            from hermes_cli.curses_ui import curses_radiolist
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) /delegation unavailable: {e}{_RST}")
            return
        try:
            agents = discover_ruflo_agents()
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) Could not discover ruflo agents: {e}{_RST}")
            return
        if not agents:
            _cprint(
                f"  {_DIM}No ruflo agents found at ~/repos/ruflo. "
                f"Set delegation.ruflo_path or RUFLO_PATH to override.{_RST}"
            )
            return
        m = get_role_model_map()
        # Build display list grouped by category. Headers are non-selectable
        # by virtue of having a name we'll filter on selection.
        display: list[str] = []
        index_map: list[Optional[str]] = []  # role name or None for header
        groups = group_by_category(agents)
        for cat in sorted(groups.keys()):
            display.append(f"━━ {cat} ━━")
            index_map.append(None)
            for a in groups[cat]:
                pinned = m.get(a.name, "")
                pin_str = f"  →  {pinned}" if pinned else ""
                desc_str = (
                    f"  ({a.description[:50]}{'…' if len(a.description) > 50 else ''})"
                    if a.description
                    else ""
                )
                display.append(f"  {a.name}{pin_str}{desc_str}")
                index_map.append(a.name)
        # Default selection: first selectable row.
        try:
            default_idx = next(
                i for i, name in enumerate(index_map) if name is not None
            )
        except StopIteration:
            _cprint(f"  {_DIM}No agents to choose from{_RST}")
            return
        try:
            picked = curses_radiolist(
                title="Pick a ruflo agent persona to assign a model",
                items=display,
                selected=default_idx,
                cancel_returns=-1,
                description=(
                    f"{len(agents)} agents across {len(groups)} categories. "
                    "Selecting an agent opens the model picker."
                ),
            )
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) Picker failed: {e}{_RST}")
            return
        if picked is None or picked < 0 or picked >= len(index_map):
            return
        role = index_map[picked]
        if role is None:
            return  # User landed on a category header — silently bail
        self._open_delegation_model_picker(role)

    def _open_delegation_model_picker(self, role: str) -> None:
        """Second-stage picker: choose a model for ``role``.

        Includes a "Clear / inherit" option to remove an existing pin and
        a "Cancel" option that no-ops.
        """
        try:
            from hermes_cli.ruflo_agents import get_role_model_map, lookup_agent
            from hermes_cli.curses_ui import curses_radiolist
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) /delegation unavailable: {e}{_RST}")
            return
        current = get_role_model_map().get(role, "")
        try:
            agent = lookup_agent(role)
        except Exception:
            agent = None
        # Build options: model choices + clear + cancel.
        items: list[str] = []
        actions: list[tuple[str, Optional[str]]] = []  # (kind, model)
        for model, label in self._DELEGATION_MODEL_CHOICES:
            marker = "  ●" if model == current else "   "
            items.append(f"{marker}  {label}")
            actions.append(("model", model))
        items.append("       Clear (inherit from delegation.model / parent)")
        actions.append(("clear", None))
        items.append("       Cancel")
        actions.append(("cancel", None))
        # Default cursor: current model row, else first.
        default_idx = next(
            (i for i, (k, m) in enumerate(actions) if k == "model" and m == current),
            0,
        )
        desc_lines = [f"Role: {role}"]
        if agent:
            desc_lines.append(f"Category: {agent.category}")
            if agent.description:
                desc_lines.append(f"  {agent.description[:120]}")
        if current:
            desc_lines.append(f"Currently pinned to: {current}")
        else:
            desc_lines.append("Currently inherits from delegation.model / parent")
        try:
            picked = curses_radiolist(
                title=f"Pick a model for ruflo persona '{role}'",
                items=items,
                selected=default_idx,
                cancel_returns=-1,
                description="\n".join(desc_lines),
            )
        except Exception as e:
            _cprint(f"  {_DIM}(>_<) Picker failed: {e}{_RST}")
            return
        if picked is None or picked < 0 or picked >= len(actions):
            return
        kind, model = actions[picked]
        if kind == "cancel":
            return
        if kind == "clear":
            self._apply_delegation_assignment(role, "")
            return
        if kind == "model" and model:
            self._apply_delegation_assignment(role, model)

    def _handle_interleaved_command(self, cmd: str):
        """Handle /interleaved — toggle one-tool-per-turn agent loop.

        When enabled, Hermes truncates the assistant's tool_calls list to
        just the FIRST call before execution. After the result returns,
        the next API call gets a fresh `<think>` block. Trades ~2× wall
        time for finer-grained reasoning between tools — only worth it
        for adaptive agent tasks on models like DSv4-Flash that emit one
        thinking block per turn but support reasoning chained across
        turns.

        Usage:
            /interleaved              Show current state
            /interleaved on           Enable (saves to config)
            /interleaved off          Disable (saves to config)
        """
        parts = cmd.strip().split(maxsplit=1)
        if len(parts) < 2:
            state = "on" if self.interleaved_thinking else "off"
            _cprint(f"  {_ACCENT}Interleaved thinking: {state}{_RST}")
            _cprint(
                f"  {_DIM}One tool call per turn so the model emits a fresh "
                f"<think> block before each tool. ~2× wall time.{_RST}"
            )
            _cprint(f"  {_DIM}Usage: /interleaved [on|off]{_RST}")
            return

        arg = parts[1].strip().lower()
        if arg in ("on", "true", "enable", "enabled", "yes", "1"):
            new_value = True
        elif arg in ("off", "false", "disable", "disabled", "no", "0"):
            new_value = False
        else:
            _cprint(f"  {_DIM}(._.) Unknown argument: {arg}{_RST}")
            _cprint(f"  {_DIM}Usage: /interleaved [on|off]{_RST}")
            return

        self.interleaved_thinking = new_value
        if self.agent is not None:
            self.agent.interleaved_thinking = new_value
        if save_config_value("agent.interleaved_thinking", new_value):
            _cprint(
                f"  {_ACCENT}✓ Interleaved thinking: "
                f"{'ON' if new_value else 'OFF'} (saved){_RST}"
            )
        else:
            _cprint(
                f"  {_ACCENT}✓ Interleaved thinking: "
                f"{'ON' if new_value else 'OFF'} (session only){_RST}"
            )

    def _handle_toolsearch_command(self, cmd: str):
        """Handle /toolsearch — toggle lazy MCP tool loading.

        Client-side only: discovery goes through the Hermes-side
        hermes_load_tools tool — each discovery is one normal API
        round-trip, billed once, no prompt-token multiplier.  (The legacy
        server_side mode was retired 2026-09-25 with the rest of the
        fork's Anthropic server-tool cluster.)

        Reads/writes ``tool_search.enabled`` and ``tool_search.mode`` in
        config.yaml. The agent reads this fresh on every API call, so
        toggles take effect on the very next turn — no restart needed.

        Usage:
            /toolsearch                       Alias for /toolsearch status
            /toolsearch status                Show current state
            /toolsearch on                    Enable (uses current mode)
            /toolsearch off                   Disable
            /toolsearch client_side           Enable + set mode=client_side
            /toolsearch mode client_side      Set mode without changing enabled
        """
        parts = cmd.strip().split()
        # Drop the "/toolsearch" token; remainder is the sub-command.
        argv = parts[1:] if parts else []

        try:
            from hermes_cli.config import load_config as _load_cfg
            cfg = _load_cfg() or {}
        except Exception:
            cfg = {}
        ts_cfg = cfg.get("tool_search") if isinstance(cfg, dict) else {}
        ts_cfg = ts_cfg if isinstance(ts_cfg, dict) else {}

        def _show_status():
            enabled = bool(ts_cfg.get("enabled"))
            defer_mcp = bool(ts_cfg.get("defer_mcp_tools", True))
            state = "ON" if enabled else "OFF"
            _cprint(f"  {_ACCENT}Tool search: {state}{_RST}")
            _cprint(f"  {_DIM}defer_mcp_tools={defer_mcp}{_RST}")
            _cprint(
                f"  {_DIM}Discovery via Hermes-side hermes_load_tools tool. "
                f"Each schema-load is one normal round-trip; no multiplier.{_RST}"
            )
            _cprint(
                f"  {_DIM}Usage: /toolsearch [on|off|client_side|status|mode <m>]{_RST}"
            )

        if not argv or argv[0].lower() in ("status", "show"):
            _show_status()
            return

        first = argv[0].lower()

        # /toolsearch mode <client_side|server_side>
        if first == "mode":
            if len(argv) < 2:
                _cprint(f"  {_DIM}Usage: /toolsearch mode [client_side]{_RST}")
                return
            new_mode = argv[1].lower()
            if new_mode == "server_side":
                _cprint(
                    f"  {_DIM}server_side was retired 2026-09-25 (fork Anthropic "
                    f"server-tool removal); tool_search is client_side-only.{_RST}"
                )
                return
            if new_mode != "client_side":
                _cprint(f"  {_DIM}(._.) Unknown mode: {new_mode}{_RST}")
                return
            if save_config_value("tool_search.mode", new_mode):
                _cprint(f"  {_ACCENT}✓ tool_search.mode = {new_mode} (saved){_RST}")
            else:
                _cprint(f"  {_ACCENT}✓ tool_search.mode = {new_mode} (session only){_RST}")
            return

        # Shorthand: /toolsearch client_side  → enable + set mode
        if first in ("client_side", "client-side"):
            save_config_value("tool_search.enabled", True)
            save_config_value("tool_search.mode", "client_side")
            _cprint(f"  {_ACCENT}✓ Tool search: ON, mode=client_side (saved){_RST}")
            _cprint(f"  {_DIM}Takes effect on the next message — no restart needed.{_RST}")
            return
        if first in ("server_side", "server-side"):
            _cprint(
                f"  {_DIM}server_side was retired 2026-09-25 (fork Anthropic "
                f"server-tool removal); use /toolsearch client_side.{_RST}"
            )
            return

        if first in ("on", "true", "enable", "enabled", "yes", "1"):
            new_value = True
        elif first in ("off", "false", "disable", "disabled", "no", "0"):
            new_value = False
        else:
            _cprint(f"  {_DIM}(._.) Unknown argument: {first}{_RST}")
            _cprint(f"  {_DIM}Usage: /toolsearch [on|off|client_side|status|mode <m>]{_RST}")
            return

        if save_config_value("tool_search.enabled", new_value):
            mode = (ts_cfg.get("mode") or "client_side").strip().lower()
            _cprint(
                f"  {_ACCENT}✓ Tool search: {'ON' if new_value else 'OFF'} "
                f"(mode={mode}, saved){_RST}"
            )
            _cprint(
                f"  {_DIM}Takes effect on the next message — no restart needed.{_RST}"
            )
        else:
            _cprint(
                f"  {_ACCENT}✓ Tool search: {'ON' if new_value else 'OFF'} (session only){_RST}"
            )

    def _on_reasoning(self, reasoning_text: str):
        """Callback for intermediate reasoning display during tool-call loops."""
        if not reasoning_text:
            return
        self._reasoning_preview_buf = getattr(self, "_reasoning_preview_buf", "") + reasoning_text
        self._flush_reasoning_preview(force=False)

            # NOTE: We deliberately do NOT raise per-logger levels for
            # tools/run_agent/etc. in quiet mode. Setting logger.setLevel
            # above the file handler level filters records before they
            # reach handlers, so agent.log / errors.log lose visibility
            # into stream-retry events, credential rotations, etc.
            # Console quietness is enforced by hermes_logging not
            # installing a console StreamHandler in non-verbose mode.

    _DESTRUCTIVE_SKIP_TOKENS = frozenset({"now", "--yes", "-y"})


    def _chat_run_agent(self, turn, message):
        """FORK: capture the turn-start context baseline, then run upstream's agent-thread body.

        The status bar's signed per-turn Δ segment compares against
        ``_turn_start_context_tokens``. Baseline off the last REAL provider count
        (``display_prompt_tokens()``) so the Δ compares like with like. 0 is NOT a valid
        baseline: it is both a fresh session AND the clamped -1 "awaiting real usage"
        sentinel right after a compression — storing 0 made the delta math report the
        ENTIRE current context as new this turn (the phantom "Δ+115K new" balloon).
        """
        try:
            _comp = getattr(self.agent, "context_compressor", None)
            if _comp is not None and hasattr(_comp, "display_prompt_tokens"):
                _base = _comp.display_prompt_tokens()
            else:
                _base = getattr(_comp, "last_prompt_tokens", None) if _comp else None
            self._turn_start_context_tokens = _base if isinstance(_base, int) and _base > 0 else None
        except Exception:
            self._turn_start_context_tokens = None
        return super()._chat_run_agent(turn, message)

    def _fire_attention_signals(self, summary: str) -> None:
        """Get the user's attention when an interactive prompt opens.

        Approval / sudo / clarify prompts used to just appear silently in
        the TUI.  Users running multiple windows or SSH sessions routinely
        missed the prompt entirely until after it timed out.  This helper
        fires two attention signals, both gated on
        ``approvals.bell_on_prompt`` / ``approvals.notify_on_prompt``:

          * Terminal bell (``\a``).  Propagates through SSH to the local
            terminal, works in tmux, iTerm, Terminal.app, Ghostty, etc.
            Many emulators also flash the tab / Dock icon on bell.
          * macOS native notification via ``osascript`` (fire-and-forget
            subprocess).  Pops a banner with the default Hermes sound on
            the Mac that owns the GUI, even when the user is SSH'd in
            from elsewhere.  No-op on non-darwin platforms.

        Safe to call from any thread.  Failures are swallowed — never
        block or crash the prompt path because the bell didn't ring.
        """
        approvals_cfg = CLI_CONFIG.get("approvals", {}) if isinstance(CLI_CONFIG, dict) else {}
        if not isinstance(approvals_cfg, dict):
            approvals_cfg = {}

        # Terminal bell
        if approvals_cfg.get("bell_on_prompt", True):
            try:
                sys.stdout.write("\a")
                sys.stdout.flush()
            except Exception:
                pass

        # macOS native notification (banner + default sound)
        if approvals_cfg.get("notify_on_prompt", True) and sys.platform == "darwin":
            try:
                import subprocess as _subprocess
                # Escape double quotes and backslashes for AppleScript.
                _summary = (summary or "Hermes needs your attention").replace(
                    "\\", "\\\\"
                ).replace('"', '\\"')
                _title = "Hermes"
                _applescript = (
                    f'display notification "{_summary}" '
                    f'with title "{_title}" sound name "Submarine"'
                )
                _subprocess.Popen(
                    ["osascript", "-e", _applescript],
                    stdout=_subprocess.DEVNULL,
                    stderr=_subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                # Notification failure must never block the prompt.
                pass


    def run(self):
        """Run the interactive CLI loop with persistent input at bottom."""
        if not self._claim_active_session("cli"):
            return

        self._tui_print_startup()
        self._tui_init_run_state()
        kb = self._tui_build_key_bindings()
        layout, style = self._tui_build_layout(kb)

        app = self._tui_build_application(layout, kb, style)
        _disable_prompt_toolkit_cpr_warning(app)
        app.after_render += self._pet_flush_kitty_frame
        self._app = app

        # Ghost status-bar lines on resize: pt's renderer scrolls the terminal after each
        # paint, pushing chrome into scrollback where a column-shrink reflows it into
        # duplicates. Wrapping _output_screen_diff keeps its reserve-space branch from firing.
        try:
            # Background: prompt_toolkit's renderer (renderer.py L232-242) explicitly moves the cursor to
            # the bottom of the canvas after painting "to make sure the terminal scrolls up, even when the
            # lower lines of the canvas just contain whitespace". In non-fullscreen mode this scrolls chrome
            # content (status bar, input rules) into terminal scrollback on every render. When the terminal
            # column-shrinks, the emulator reflows the previously rendered full-width rows into multiple
            # narrower rows that get pushed up — leaving ghost duplicates AND polluting scrollback. Same
            # issue as pt #29 (open since 2014), #1675, #1933. Surgical fix: wrap _output_screen_diff so
            # that when its internal `if current_height > previous_screen.height` branch fires (the one that
            # does the bottom-cursor-move), we make it fall through by inflating previous_screen.height
            # first.
            import prompt_toolkit.renderer as _pt_renderer
            from prompt_toolkit.renderer import _output_screen_diff as _orig_osd

            if not getattr(_pt_renderer, "_hermes_osd_patched", False):
                _pt_renderer._output_screen_diff = functools.partial(
                    _hermes_call_output_screen_diff, _orig_osd
                )
                _pt_renderer._hermes_osd_patched = True
        except Exception:
            pass

        _apply_bracketed_paste_timeout_patch()

        self._install_resize_recovery(app)

        threading.Thread(target=self._tui_spinner_loop, daemon=True).start()
        threading.Thread(target=self._tui_process_loop, daemon=True).start()
        # Wake word listener off-thread so a first-run engine install never blocks the prompt.
        threading.Thread(target=self._tui_wake_startup, daemon=True, name="wake-startup").start()

        atexit.register(_run_cleanup)
        self._tui_install_signal_handlers()

        if not self._tui_stdin_usable():
            # FORK order: summary before cleanup — see _tui_shutdown and
            # tests/hermes_cli/test_exit_summary_before_cleanup_ordering.py.
            _fold_curator_cost_before_exit()
            _run_memory_confirm_before_exit()
            self._print_exit_summary()
            _run_cleanup()
            return

        try:
            with patch_stdout():
                try:
                    # run_in_terminal() may return either: • a coroutine / Future (prompt_toolkit ≥ 3.0) —
                    # must be scheduled via ensure_future so the coroutine is actually awaited; calling it
                    # bare would leave it unawaited and silently drop the output (fixes #23185 Bug A). •
                    # None (some mocks / older PT builds) — just call the inner function directly since PT
                    # already executed it synchronously. Do NOT fall back to a bare _pt_print when
                    # ensure_future raises, because run_in_terminal already invoked the lambda in that case
                    # (the mock path), which would double-print the line.
                    import asyncio as _aio
                    _aio.get_running_loop().set_exception_handler(self._tui_suppress_closed_loop_errors)
                except Exception:
                    pass  # no running loop -- nothing to patch
                # FORK: the fork's unconditional hermes_cli.keyboard_protocol.enable() used to
                # run here and pushed CSI >1u on ANY tty, defeating the Ghostty exception
                # (#87630); retired 2026-08-26 (FORK.md de-fork audit). Extended key reporting
                # is now pushed by the allowlist-gated _enable_extended_enter_keys() below.
                # Record that the app enables focus reporting + mouse tracking so _run_cleanup
                # resets them; extended key modes are popped by the same reset.
                # When multiline shortcuts are on, also ask supported terminals (e.g. iTerm2) to report
                # modified keys distinctly (kitty protocol + modifyOtherKeys); the cleanup reset pops both
                # modes. See #36823.
                _mark_tui_input_modes_active()
                if self._tui_multiline_shortcuts:
                    _enable_extended_enter_keys(app.output)
                app.run()
                # Terminal keyboard modes are restored by _run_cleanup's _reset_tui_input_modes()
                # (atexit-registered + driven from the SIGTERM/SIGHUP handler), which pops CSI <u
                # and resets modifyOtherKeys under the _tui_input_modes_active gate. The fork's
                # hermes_cli.keyboard_protocol.disable() used to run here; retired 2026-08-26
                # with its enable() counterpart (see FORK.md de-fork audit).
                self._pet_start_anim()
        except (EOFError, KeyboardInterrupt, BrokenPipeError):
            pass
        except (KeyError, OSError) as _stdin_err:
            # Selector registration failures from broken stdin and I/O errors from a
            # broken stdout during interrupt (EIO is suppressed).
            _errno = getattr(_stdin_err, "errno", None) if isinstance(_stdin_err, OSError) else None
            _msg = str(_stdin_err)
            if _errno == errno.EIO:
                pass
            elif _errno in {errno.EINVAL, errno.EBADF} or any(
                s in _msg for s in ("is not registered", "Bad file descriptor", "Invalid argument")
            ):
                print(
                    f"\nError: stdin is not usable ({_stdin_err}).\n"
                    "This can happen with certain Python installations (e.g. uv-managed cPython on macOS)\n"
                    "where kqueue cannot register fd 0.\n"
                    "Try reinstalling Python via pyenv or Homebrew, then re-run: hermes setup"
                )
            else:
                raise
        finally:
            # A resize right before exit leaves its recovery (and the paints it held) unrun.
            _release_paints()
            self._tui_shutdown()

            # FORK order: curator (near-instant) then the memory-confirm UI (interactive), then
            # the exit summary BEFORE cleanup. _run_cleanup() can block for tens of seconds and
            # the exit watchdog can guillotine it, which would swallow the cost report + resume
            # hint; both helpers also fold their LLM spend into session_estimated_cost_usd first.
            _fold_curator_cost_before_exit()
            _run_memory_confirm_before_exit()
            self._print_exit_summary()
            _run_cleanup()
            self._release_active_session()

        # /update relaunch happens here, after prompt_toolkit restored terminal modes, on the
        # main thread (the process_loop thread would skip cleanup / only exit itself on Windows).
        if self._pending_relaunch:
            from hermes_cli.relaunch import relaunch
            relaunch(self._pending_relaunch, preserve_inherited=False)


def _build_cli_from_args(model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget, verbose, compact, resume, checkpoints, pass_session_id, ignore_rules, skills):
    """Resolve the toolset list (explicit / coding posture / platform default), construct HermesCLI, and start the background skills preload."""
    toolsets_list = None
    if isinstance(toolsets, str) and toolsets:
        toolsets_list = [t.strip() for t in toolsets.split(",")]
    elif isinstance(toolsets, (list, tuple)) and toolsets:
        # Fire may pass multiple --toolsets as a tuple
        toolsets_list = []
        for t in toolsets:
            toolsets_list.extend([x.strip() for x in t.split(",")] if isinstance(t, str) else [str(t)])
    elif not toolsets:
        # Coding posture inside a code workspace, else the shared platform resolver.
        try:
            from agent.coding_context import coding_selection
            toolsets_list = coding_selection(platform="cli", config=CLI_CONFIG)
        except Exception:
            toolsets_list = None
        if toolsets_list is None:
            from hermes_cli.tools_config import _get_platform_tools
            toolsets_list = sorted(_get_platform_tools(CLI_CONFIG, "cli"))

    parsed_skills = _parse_skills_argument(skills)

    try:
        cli = HermesCLI(
            model=model,
            toolsets=toolsets_list,
            provider=provider,
            reasoning=reasoning,
            api_key=api_key,
            base_url=base_url,
            max_turns=max_turns,
            run_budget=run_budget,
            verbose=verbose,
            compact=compact,
            resume=resume,
            checkpoints=checkpoints,
            pass_session_id=pass_session_id,
            ignore_rules=ignore_rules,
        )
    except ImportError as e:
        # Direct `python cli.py` bypasses cmd_chat's partial-update ImportError handler.
        from hermes_constants import emit_partial_update_hint

        if emit_partial_update_hint(e):
            sys.exit(1)
        raise

    # skills.auto_load rides the same background preload as -s; --ignore-rules skips it with
    # the rest of the auto-injected context. Resolved here (not lazily in the agent) so the
    # session id is real for ${HERMES_SESSION_ID} and -s can dedupe against it.
    from agent.skill_commands import build_auto_load_prompt, resolve_auto_load_skills
    auto_load_names = [] if getattr(cli, "ignore_rules", ignore_rules) else resolve_auto_load_skills(CLI_CONFIG)
    if not auto_load_names:
        cli._auto_load_skills_result = ("", [], [])
    if parsed_skills or auto_load_names:
        # Load the skill payloads in the background: skill_view walks the full skills
        # tree per skill (~0.5s for a large library) and the result is only consumed
        # at agent init, not by the banner. finalize_preloaded_skills() joins the
        # thread before any consumer reads cli.system_prompt.
        def _load_preloaded_skills() -> None:
            try:
                if auto_load_names:
                    cli._auto_load_skills_result = build_auto_load_prompt(task_id=cli.session_id, user_config=CLI_CONFIG)
                if parsed_skills:
                    cli._preload_skills_result = build_preloaded_skills_prompt(
                        parsed_skills, task_id=cli.session_id, excluded_loaded_names=set(cli._auto_load_skills_result[1]))
            except Exception as exc:  # surfaced by finalize
                cli._preload_skills_error = exc

        cli._preload_skills_requested = [*auto_load_names, *(s for s in parsed_skills if s not in auto_load_names)]
        cli._preload_skills_thread = threading.Thread(target=_load_preloaded_skills, name="skills-preload", daemon=True)
        cli._preload_skills_thread.start()
    return cli


def _run_legacy_gateway():
    """Legacy `cli.py --gateway` entry: arm the startup watchdog (before importing the gateway graph), then run it."""
    import asyncio
    with suppress(Exception):
        from hermes_startup_watchdog import arm_startup_watchdog
        arm_startup_watchdog()
    from gateway.run import start_gateway
    print("Starting Hermes Gateway (messaging platforms)...")
    asyncio.run(start_gateway())


def _start_worktree_setup(list_tools, list_toolsets, worktree, w):
    """Start isolated-worktree creation (+ tool prewarm) in the background.

    Returns a join callable that publishes ``_active_worktree``/TERMINAL_CWD and
    schedules stale-worktree GC, or None when no worktree is wanted.
    """
    if list_tools or list_toolsets or not (worktree or w or CLI_CONFIG.get("worktree", False)):
        return None
    # Overlap tool discovery with the I/O-bound worktree setup so show_banner() hits a warm
    # cache (~0.4s). Only on the -w path: plain `hermes` has no I/O wait to hide.
    def _prewarm_tools() -> None:
        try:
            import model_tools as _mt
            _mt.get_tool_definitions(quiet_mode=True)
        except Exception:
            logger.debug("tool prewarm failed", exc_info=True)

    threading.Thread(target=_prewarm_tools, name="tool-prewarm", daemon=True).start()
    _sync_base = CLI_CONFIG.get("worktree_sync", True)
    _wt_result: dict = {}

    def _create_worktree() -> None:
        try:
            _wt_result["info"] = _setup_worktree(sync_base=_sync_base)
        except Exception:
            logger.debug("worktree setup failed", exc_info=True)
            _wt_result["info"] = None

    _wt_thread = threading.Thread(target=_create_worktree, name="worktree-setup", daemon=True)
    _wt_thread.start()

    def _worktree_maintenance(repo: str) -> None:
        _prune_stale_worktrees(repo)
        _maintain_pack_health(repo)

    def _join_worktree() -> Optional[Dict[str, str]]:
        _wt_thread.join(timeout=120)
        info = _wt_result.get("info")
        if not info:
            return info
        global _active_worktree
        _active_worktree = info
        os.environ["TERMINAL_CWD"] = info["path"]
        atexit.register(_cleanup_worktree, info)
        # GC stale worktrees AFTER _setup_worktree so they never race on git's worktree
        # metadata (the new tree is immune: <24h age gate + live pid lock); then repack
        # once refs are final so lookups stay fast on multi-agent boxes.
        _repo = _git_repo_root()
        if _repo:
            threading.Thread(target=_worktree_maintenance, args=(_repo,), name="worktree-prune", daemon=True).start()
        return info

    return _join_worktree


def main(
    query: str = None,
    q: str = None,
    oneshot: bool = False,
    image: str = None,
    toolsets: str = None,
    skills: str | list[str] | tuple[str, ...] = None,
    model: str = None,
    provider: str = None,
    reasoning: str = None,
    api_key: str = None,
    base_url: str = None,
    max_turns: int = None,
    run_budget: float = None,
    verbose: Optional[bool] = None,
    quiet: bool = False,
    compact: bool = False,
    list_tools: bool = False,
    list_toolsets: bool = False,
    gateway: bool = False,
    resume: str = None,
    worktree: bool = False,
    w: bool = False,
    checkpoints: bool = False,
    pass_session_id: bool = False,
    output_format: str = "text",
    ignore_user_config: bool = False,
    ignore_rules: bool = False,
):
    """
    Hermes Agent CLI - Interactive AI Assistant
    
    Args:
        query: Query to run. On a real TTY this seeds an interactive session
            (submitted literally as the first turn); with --oneshot/-Q or a
            non-TTY it answers and exits. Alias: -q
        q: Shorthand for --query
        oneshot: With -q: force the legacy answer-and-exit single-query mode
            even on a TTY.
        image: Optional local image path to attach to a single query
        toolsets: Comma-separated list of toolsets to enable (e.g., "web,terminal")
        skills: Comma-separated or repeated list of skills to preload for the session
        model: Model to use (default: anthropic/claude-opus-4-20250514)
        provider: Inference provider ("auto", "openrouter", "nous", "openai-codex", "zai", "kimi-coding", "minimax", "minimax-cn")
        reasoning: Reasoning effort for this run (none|minimal|low|medium|high|xhigh|max|ultra). Overrides agent.reasoning_effort.
        api_key: API key for authentication
        base_url: Base URL for the API
        max_turns: Maximum tool-calling iterations (default: 60)
        verbose: Enable verbose logging
        compact: Use compact display mode
        list_tools: List available tools and exit
        list_toolsets: List available toolsets and exit
        resume: Resume a previous session by its ID (e.g., 20260225_143052_a1b2c3)
        worktree: Run in an isolated git worktree (for parallel agents). Alias: -w
        w: Shorthand for --worktree
    
    Examples:
        python cli.py                            # Start interactive mode
        python cli.py --toolsets web,terminal    # Use specific toolsets
        python cli.py --skills hermes-agent-dev,github-auth
        python cli.py -q "What is Python?"       # Single query mode
        python cli.py -q "Describe this" --image ~/storage/shared/Pictures/cat.png
        python cli.py --list-tools               # List tools and exit
        python cli.py --resume 20260225_143052_a1b2c3  # Resume session
        python cli.py -w                         # Start in isolated git worktree
        python cli.py -w -q "Fix issue #123"     # Single query in worktree
    """
    # UTF-8 stdio on Windows before any print (Rich box-drawing would UnicodeEncodeError on cp1252).
    with suppress(Exception):
        from hermes_cli.stdio import configure_windows_stdio
        configure_windows_stdio()

    os.environ["HERMES_INTERACTIVE"] = "1"  # terminal_tool: interactive sudo prompts with timeout
    # The banner names affected plugins; the raw per-name compat warnings would only duplicate it on stderr.
    with suppress(Exception):
        from hermes_cli.plugin_compat import quiet_for_interactive
        quiet_for_interactive()

    if gateway:
        _run_legacy_gateway()
        return

    _join_worktree = _start_worktree_setup(list_tools, list_toolsets, worktree, w)
    query = query or q
    # ``hermes chat`` already validated this; the direct Fire entry point gets the same contract.
    if output_format == "stream-json":
        if not query:
            raise ValueError("--format stream-json requires -q/--query")
        quiet = True
    cli = _build_cli_from_args(model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget,
                               verbose, compact, resume, checkpoints, pass_session_id, ignore_rules, skills)

    # Join the background worktree creation before anything consumes TERMINAL_CWD.
    # A requested worktree whose setup failed aborts: never silently run without isolation.
    wt_info = _join_worktree() if _join_worktree is not None else None
    if _join_worktree is not None and not wt_info:
        return

    # Inject worktree context into agent's system prompt
    if wt_info:
        wt_note = (
            f"\n\n[System note: You are working in an isolated git worktree at "
            f"{wt_info['path']}. Your branch is `{wt_info['branch']}`. "
            f"Changes here do not affect the main working tree or other agents. "
            f"Remember to commit and push your changes, and create a PR if appropriate. "
            f"The original repo is at {wt_info['repo_root']}.]"
        )
        cli.system_prompt = (cli.system_prompt or "") + wt_note

    if list_tools or list_toolsets:
        cli.show_banner()
        (cli.show_tools if list_tools else cli.show_toolsets)()
        sys.exit(0)

    atexit.register(_run_cleanup)  # interactive mode registers again in run() (idempotent)
    _install_single_query_signal_handlers(cli)

    if query or image:
        _run_single_query_mode(cli, query, image, quiet, oneshot, stream_json=output_format == "stream-json")
        return
    cli.run()


if __name__ == "__main__":
    import fire

    fire.Fire(main)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from prompt_toolkit.layout.menus import CompletionsMenu  # noqa: F401,E402
from prompt_toolkit.filters import Condition  # noqa: F401,E402
from prompt_toolkit.layout import ConditionalContainer  # noqa: F401,E402
from prompt_toolkit.layout.processors import ConditionalProcessor  # noqa: F401,E402
from prompt_toolkit.layout.dimension import Dimension  # noqa: F401,E402
from prompt_toolkit.history import FileHistory  # noqa: F401,E402
from prompt_toolkit.layout import FormattedTextControl  # noqa: F401,E402
from prompt_toolkit.layout import HSplit  # noqa: F401,E402
from prompt_toolkit.key_binding import KeyBindings  # noqa: F401,E402
from prompt_toolkit.layout import Layout  # noqa: F401,E402
from prompt_toolkit.styles import Style as PTStyle  # noqa: F401,E402
from rich.panel import Panel  # noqa: F401,E402
from prompt_toolkit.layout.processors import PasswordProcessor  # noqa: F401,E402
from prompt_toolkit.layout.processors import Processor  # noqa: F401,E402
from prompt_toolkit.widgets import TextArea  # noqa: F401,E402
from prompt_toolkit.layout.processors import Transformation  # noqa: F401,E402
from prompt_toolkit.layout import Window  # noqa: F401,E402
from prompt_toolkit.layout import WindowAlign  # noqa: F401,E402
import base64  # noqa: F401,E402
import concurrent.futures  # noqa: F401,E402
import copy  # noqa: F401,E402
from rich import box as rich_box  # noqa: F401,E402
import tempfile  # noqa: F401,E402

def AIAgent(*args, **kwargs):
    from run_agent import AIAgent as _AIAgent

    return _AIAgent(*args, **kwargs)

def CanonicalUsage(*args, **kwargs):
    from agent.usage_pricing import CanonicalUsage as _CanonicalUsage

    return _CanonicalUsage(*args, **kwargs)


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_BROWSER_CDP_URL': ('hermes_cli.browser_connect', 'DEFAULT_BROWSER_CDP_URL'),
    'HERMES_AGENT_LOGO': ('hermes_cli.banner', 'HERMES_AGENT_LOGO'),
    'HERMES_CADUCEUS': ('hermes_cli.banner', 'HERMES_CADUCEUS'),
    'SlashCommandAutoSuggest': ('hermes_cli.commands_completion', 'SlashCommandAutoSuggest'),
    'SlashCommandCompleter': ('hermes_cli.commands_completion', 'SlashCommandCompleter'),
    'build_welcome_banner': ('hermes_cli.banner', 'build_welcome_banner'),
    'display_hermes_home': ('hermes_constants', 'display_hermes_home'),
    'estimate_usage_cost': ('agent.usage_pricing', 'estimate_usage_cost'),
    'get_all_toolsets': ('toolsets', 'get_all_toolsets'),
    'get_job': ('cron.jobs', 'get_job'),
    'get_toolset_for_tool': ('model_tools', 'get_toolset_for_tool'),
    'get_toolset_info': ('toolsets', 'get_toolset_info'),
    'init_skin_from_config': ('hermes_cli.skin_engine', 'init_skin_from_config'),
    'is_browser_debug_ready': ('hermes_cli.browser_connect', 'is_browser_debug_ready'),
    'is_table_divider': ('agent.markdown_tables', 'is_table_divider'),
    'looks_like_table_row': ('agent.markdown_tables', 'looks_like_table_row'),
    'manual_chrome_debug_command': ('hermes_cli.browser_connect', 'manual_chrome_debug_command'),
    'print_config_warnings': ('hermes_cli.config', 'print_config_warnings'),
    'prompt_for_secret': ('hermes_cli.callbacks', 'prompt_for_secret'),
    'set_friendly_tool_labels': ('agent.display', 'set_friendly_tool_labels'),
    'set_tool_preview_max_len': ('agent.display', 'set_tool_preview_max_len'),
    'setup_logging': ('hermes_logging', 'setup_logging'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
