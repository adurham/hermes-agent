#!/usr/bin/env python3
"""Hermes Agent CLI — interactive terminal interface (``python cli.py --help`` for usage)."""

# Must be the very first import (UTF-8 stdio on Windows). Missing only mid-``hermes update``.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    pass

import logging
import os
import functools
import shutil
import sys
import json
import re
import atexit
import errno
import time
import textwrap
from collections import deque
from dataclasses import dataclass
from urllib.parse import unquote, urlparse
from contextlib import contextmanager, suppress
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Mapping

logger = logging.getLogger(__name__)

os.environ["HERMES_QUIET"] = "1"  # suppress our modules' startup chatter

from hermes_cli.fallback_config import get_fallback_chain
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
from agent.interrupt_compat import request_hard_interrupt
from agent.pet import render as pet_render

from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.application import Application
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
from hermes_cli.banner import format_banner_version_label

_COMMAND_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


# ~/.hermes/.env first, project .env as dev fallback; user env files override stale shell exports.
from hermes_constants import get_hermes_home
from hermes_state_ids import new_session_id
from hermes_cli.env_loader import load_hermes_dotenv
from utils import base_url_host_matches, base_url_hostname, fast_safe_load

_hermes_home = get_hermes_home()
_project_env = Path(__file__).parent / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=_project_env)


_REASONING_TAGS = ("REASONING_SCRATCHPAD", "think", "thinking", "reasoning", "thought")
# FORK: "invoke"/"parameter" appended — some backends leak Anthropic-style tool XML.
_TOOL_CALL_TAGS = ("tool_call", "tool_calls", "tool_result", "function_call", "function_calls",
                   "invoke", "parameter")


def _strip_reasoning_tags(text: str) -> str:
    """Strip reasoning blocks (closed, unterminated, orphan-close) and leaked tool-call XML from display text.

    Keep in sync with ``run_agent._strip_think_blocks`` and the stream consumer's think-tag sets.

    Also strips tool-call XML blocks some open models leak into visible content (``<tool_call>``,
    ``<function_calls>``, Gemma-style ``<function name="…">…</function>``). Ported from
    openclaw/openclaw#67318.
    """
    cleaned = text
    for tag in _REASONING_TAGS:
        cleaned = re.sub(rf"<{tag}>.*?</{tag}>\s*", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(rf"<{tag}>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(rf"</{tag}>\s*", "", cleaned, flags=re.IGNORECASE)
    # FORK: _TOOL_CALL_TAGS also carries the fork's "invoke"/"parameter" tags.
    for tc_tag in _TOOL_CALL_TAGS:
        cleaned = re.sub(rf"<{tc_tag}\b[^>]*>.*?</{tc_tag}>\s*", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    # <function name="..."> — boundary + attribute gated to avoid prose false positives.
    cleaned = re.sub(
        r'(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*<function\b[^>]*\bname\s*=[^>]*>(?:(?:(?!</function>).)*)</function>\s*',
        '', cleaned, flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(
        r'</(?:tool_call|tool_calls|tool_result|function_call|function_calls|function'
        r'|invoke|parameter)>\s*', '', cleaned,  # FORK: invoke|parameter
        flags=re.IGNORECASE,
    )
    # Unterminated opener / stray <arg_key>/<arg_value> markup = stream cut
    # mid tool-call serialization (#101899); strip to end of text.
    cleaned = re.sub(
        r'(?:^|\n)[ \t]*<(?:tool_call|tool_calls|tool_result|function_call|function_calls)\b[^>]*>.*$'
        r'|(?:^|\n)[^\n<]*</?arg_(?:key|value)\b.*$',
        '',
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return cleaned.strip()


def _assistant_content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return str(content)


def _assistant_copy_text(content: Any) -> str:
    return _strip_reasoning_tags(_assistant_content_as_text(content))


def _load_prefill_messages(file_path: str) -> List[Dict[str, Any]]:
    """Load prefill messages (JSON array) from *file_path*; relative to ~/.hermes/; missing/empty -> []."""
    if not file_path:
        return []
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = _hermes_home / path
    if not path.exists():
        logger.warning("Prefill messages file not found: %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("Prefill messages file must contain a JSON array: %s", path)
            return []
        return data
    except Exception as e:
        logger.warning("Failed to load prefill messages from %s: %s", path, e)
        return []


def _resolve_prefill_messages_file(config: Dict[str, Any]) -> str:
    """Prefill file path: env, then top-level ``prefill_messages_file``, then legacy ``agent.*``."""
    agent_cfg = config.get("agent", {})
    return (
        os.getenv("HERMES_PREFILL_MESSAGES_FILE", "").strip()
        or str(config.get("prefill_messages_file", "") or "").strip()
        or (str(agent_cfg.get("prefill_messages_file", "") or "").strip() if isinstance(agent_cfg, dict) else "")
    )


def _parse_reasoning_config(effort) -> dict | None:
    """Parse a reasoning effort level (string or YAML bool; ``false``/``off`` = disabled)."""
    from hermes_constants import parse_reasoning_effort
    result = parse_reasoning_effort(effort)
    if effort and str(effort).strip() and result is None:
        logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
    return result


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


def _parse_service_tier_config(raw: str) -> str | None:
    """Parse a persisted fast-mode preference: None, "priority", "auto", or "cold"."""
    value = str(raw or "").strip().lower()
    if not value or value in {"normal", "default", "standard", "off", "none"}:
        return None
    if value in {"fast", "priority", "on"}:
        return "priority"
    if value in {"auto", "cold"}:
        return value
    logger.warning("Unknown service_tier '%s', ignoring", raw)
    return None


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


def _mirror_config_to_env(defaults, _file_has_terminal_config):
    """Project config.yaml values into the env vars the tool modules read (terminal/browser/auxiliary/security/sessions). Env always wins when already set."""
    terminal_config = defaults.get("terminal", {})

    # "backend" (documented) and legacy "env_type" are both accepted; "backend" wins.
    if "backend" in terminal_config:
        terminal_config["env_type"] = terminal_config["backend"]

    # Local backend: cwd is always os.getcwd(). Non-local: a placeholder is popped so
    # terminal_tool uses its per-backend default; an explicit path is kept.
    effective_backend = terminal_config.get("env_type", "local")
    if effective_backend == "local":
        terminal_config["cwd"] = os.getcwd()
        defaults["terminal"]["cwd"] = terminal_config["cwd"]
    elif terminal_config.get("cwd") in _CWD_PLACEHOLDERS:
        terminal_config.pop("cwd", None)

    # TERMINAL_CWD is force-exported (beats stale .env) except inside a gateway process,
    # whose config bridge already set it.
    _is_gateway = os.environ.get("_HERMES_GATEWAY") == "1"
    for config_key, env_var in _TERMINAL_ENV_MAPPINGS.items():
        if config_key not in terminal_config:
            continue
        val = terminal_config[config_key]
        if env_var == "TERMINAL_CWD":
            if not _is_gateway:
                os.environ[env_var] = str(val)
        elif _file_has_terminal_config or env_var not in os.environ:
            os.environ[env_var] = json.dumps(val) if isinstance(val, (list, dict)) else str(val)

    browser_config = defaults.get("browser", {})
    if "inactivity_timeout" in browser_config:
        os.environ["BROWSER_INACTIVITY_TIMEOUT"] = str(browser_config["inactivity_timeout"])

    # Only non-empty / non-"auto" auxiliary values are bridged so auto-detection still works.
    auxiliary_config = defaults.get("auxiliary", {})
    for task_key, env_map in _AUXILIARY_TASK_ENV.items():
        task_cfg = auxiliary_config.get(task_key, {})
        if not isinstance(task_cfg, dict):
            continue
        for field, env_var in env_map.items():
            val = str(task_cfg.get(field, "")).strip()
            if val and not (field == "provider" and val == "auto"):
                os.environ[env_var] = val

    security_config = defaults.get("security", {})
    if isinstance(security_config, dict):
        redact = security_config.get("redact_secrets")
        if redact is not None:
            os.environ["HERMES_REDACT_SECRETS"] = str(redact).lower()

    # Session-search index knobs (hermes_state reads the env carriers).
    sessions_config = defaults.get("sessions", {})
    if isinstance(sessions_config, dict):
        if "cjk_fts" in sessions_config:
            os.environ["HERMES_CJK_FTS"] = str(sessions_config["cjk_fts"])
        if "search_slow_ms" in sessions_config:
            os.environ["HERMES_SEARCH_SLOW_MS"] = str(sessions_config["search_slow_ms"])


def _cli_config_defaults():
    """Built-in defaults for every config key the CLI reads (the file overlays these)."""
    img = "nikolaik/python-nodejs:python3.11-nodejs20"
    return {
        "model": {"default": "", "base_url": "", "provider": "auto"},
        "terminal": {
            "env_type": "local", "cwd": ".", "home_mode": "auto", "lifetime_seconds": 300,  # cwd "." -> os.getcwd()
            "docker_image": img, "docker_forward_env": [], "singularity_image": f"docker://{img}",
            "modal_image": img, "daytona_image": img, "docker_volumes": [],
            "docker_mount_cwd_to_workspace": False,  # opt-in only: sandbox isolation
            "docker_shared_container_key": "",
        },
        "browser": {
            "inactivity_timeout": 120, "record_sessions": False, "engine": "auto",  # auto (Chrome) | lightpanda | chrome
            "camofox": {"rewrite_loopback_urls": False, "loopback_host_alias": "host.docker.internal"},
        },
        # threshold: fraction of the model's context limit; min_tail: real user messages kept in the tail
        "compression": {"enabled": True, "threshold": 0.50, "min_tail_user_messages": 1},
        "agent": {
            "max_turns": 500, "verbose": False, "system_prompt": "", "prefill_messages_file": "",  # max_turns shared with subagents
            "reasoning_effort": "", "service_tier": "",
            # FORK: per-model effort map (/reasoning --global writes it), interleaved
            # thinking (Think-Act-Think-Act between tool calls; off by default), and the
            # overload cache-strip escape hatch.
            "reasoning_effort_by_model": {}, "interleaved_thinking": False,
            "strip_cache_on_overload": False,
            "personalities": {},  # user overrides merged by name over hermes_cli.personality builtins
        },
        "display": {
            "compact": False,
            # /resume recap tuning and show_reasoning: keep in sync with hermes_cli/config.py DEFAULT_CONFIG
            "resume_display": "full", "resume_exchanges": 10, "resume_max_user_chars": 300,
            "resume_max_assistant_chars": 200, "resume_max_assistant_lines": 3, "resume_skip_tool_only": True,
            "show_reasoning": True, "reasoning_full": False, "streaming": True, "busy_input_mode": "interrupt",
            "persistent_output": True, "persistent_output_max_lines": 200,
            # Also clear scrollback on redraw/resize recovery; off because users prefer history.
            "cli_rebuild_scrollback_on_redraw": False,
            "persist_prompts": True,  # one-line summary of resolved modal prompts into scrollback
            # FORK: which key interrupts a running agent — "ctrl-c" (default, legacy Hermes),
            # "escape" (claude-code parity; Ctrl+C becomes press-again-to-exit), or "both".
            # Esc carries a ~0.5s chord-flush delay so prompt_toolkit can disambiguate
            # Alt+Enter / Alt+G / Alt+V chords first.
            "interrupt_key": "ctrl-c",
            "skin": "default",
        },
        "clarify": {"timeout": 120},  # seconds before a clarify prompt auto-proceeds
        "code_execution": {"timeout": 300, "max_tool_calls": 50},
        "auxiliary": {"vision": {"provider": "auto", "model": "", "base_url": "", "api_key": ""}},
        # delegation: empty model/provider = inherit parent; api_key falls back to OPENAI_API_KEY
        "delegation": {"max_iterations": 45, "model": "", "provider": "", "base_url": "", "api_key": ""},
        "onboarding": {"seen": {}},  # first-touch hint flags (agent/onboarding.py), latched once shown
    }


def _merge_file_config(defaults: Dict[str, Any], file_config: Dict[str, Any]) -> None:
    """Overlay a parsed config file onto *defaults* in place (model normalization, deep merge, legacy keys)."""
    # model: string (new format) or dict (old format with default/base_url)
    if "model" in file_config:
        if isinstance(file_config["model"], str):
            defaults["model"]["default"] = file_config["model"]
        elif isinstance(file_config["model"], dict):
            defaults["model"].update(file_config["model"])
            # Promote model.model -> model.default (HermesCLI checks "default" first).
            if "model" in file_config["model"] and "default" not in file_config["model"]:
                defaults["model"]["default"] = file_config["model"]["model"]

    # Deep-merge dict sections, overwrite scalars; a None section keeps the defaults;
    # unknown keys (platform_toolsets, memory, ...) are carried over.
    for key, value in file_config.items():
        if key == "model":
            continue
        if isinstance(defaults.get(key), dict):
            if isinstance(value, dict):
                defaults[key].update(value)
            elif value is not None:
                defaults[key] = value
        else:
            defaults[key] = value

    # Legacy root-level max_turns -> agent.max_turns whenever the nested key is missing.
    agent_file_config = file_config.get("agent")
    if "max_turns" in file_config and not (
        isinstance(agent_file_config, dict) and agent_file_config.get("max_turns") is not None
    ):
        defaults["agent"]["max_turns"] = file_config["max_turns"]


def load_cli_config() -> Dict[str, Any]:
    """~/.hermes/config.yaml (else ./cli-config.yaml) over built-in defaults; env vars win.

    ``HERMES_IGNORE_USER_CONFIG=1`` skips the user config entirely (``.env`` still loads).
    """
    config_path = _hermes_home / 'config.yaml'
    if not config_path.exists() or os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1":
        config_path = Path(__file__).parent / 'cli-config.yaml'

    defaults = _cli_config_defaults()

    # Only a file's terminal section may overwrite terminal env vars already set by .env.
    _file_has_terminal_config = False

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                from hermes_cli.config import _normalize_root_model_keys

                file_config = _normalize_root_model_keys(fast_safe_load(f) or {})

            _file_has_terminal_config = "terminal" in file_config
            _merge_file_config(defaults, file_config)
        except Exception as e:
            logger.warning("Failed to load cli-config.yaml: %s", e)

    # Expand ${ENV_VAR} references before bridging to env vars.
    from hermes_cli.config import _expand_env_vars
    defaults = _expand_env_vars(defaults)

    # Administrator-pinned (managed scope) values overlay LAST; cli.py builds its config
    # independently of hermes_cli.config, so this keeps parity with `hermes config`. Fail-open.
    from hermes_cli import managed_scope

    defaults = managed_scope.apply_managed_overlay(defaults)

    _mirror_config_to_env(defaults, _file_has_terminal_config)

    return defaults

CLI_CONFIG = load_cli_config()


def _init_logging_and_display_from_config() -> None:
    """Best-effort startup side effects: logging, config warnings, skin, display knobs."""
    from importlib import import_module as _im

    def _display(key, default):
        return CLI_CONFIG.get("display", {}).get(key, default)

    for step in (
        lambda: _im("hermes_logging").setup_logging(mode="cli"),
        lambda: _im("hermes_cli.config").print_config_warnings(),
        lambda: _im("hermes_cli.skin_engine").init_skin_from_config(CLI_CONFIG),
        lambda: _im("agent.display").set_tool_preview_max_len(int(_display("tool_preview_length", 0) or 0)),
        lambda: _im("agent.display").set_friendly_tool_labels(bool(_display("friendly_tool_labels", True))),
    ):
        try:
            step()
        except Exception:
            pass


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

from rich.console import Console
from rich.markup import escape as _escape
from rich.text import Text as _RichText

# Agent/tool systems load lazily: bare startup only needs the prompt.
def get_tool_definitions(*args, **kwargs):
    from hermes_cli.mcp_startup import wait_for_mcp_discovery
    from model_tools import get_tool_definitions as _get_tool_definitions

    wait_for_mcp_discovery()
    return _get_tool_definitions(*args, **kwargs)


validate_toolset = _lazy_shim("toolsets", "validate_toolset")


def _sync_process_session_id(session_id: str) -> None:
    """Keep process-local session-id consumers aligned after CLI switches."""
    from gateway.session_context import set_current_session_id

    set_current_session_id(session_id)


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


def _flush_logging_and_stdio() -> None:
    """Best-effort ``logging.shutdown()`` + stdout/stderr flush before ``os._exit``."""
    with suppress(Exception):
        logging.shutdown()
    for _stream in (sys.stdout, sys.stderr):
        with suppress(Exception):
            _stream.flush()


def _float_env(name: str, default: float) -> float:
    """``float(os.getenv(name))``, or ``default`` when unset/unparseable."""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _exit_watchdog_timeout() -> float:
    """``HERMES_EXIT_WATCHDOG_S`` as a float (default 60; ``0`` disables).

    FORK: 60, not upstream's 30. The budget must exceed the worst-case sum of cleanup
    steps between arming and the process actually exiting: shutdown_mcp_servers() alone
    can block 15s (future.result(timeout=15)) and confirm_and_commit()'s memory-extraction
    LLM call defaults to 30s — 45s worst case before shutdown_memory_provider() even runs.
    A 30s watchdog guillotined that combination outright, os._exit(0)-ing before
    _print_exit_summary() (cost report + --resume hint) or the memory-confirm UI printed
    anything.
    """
    return _float_env("HERMES_EXIT_WATCHDOG_S", 60.0)


def _arm_exit_watchdog(timeout_s: float | None = None, *, from_signal: bool = False) -> None:
    """Daemon timer that ``os._exit(0)``s after ``timeout_s`` once shutdown has begun.

    Backstop for a cleanup step wedged on network I/O and for interpreter teardown
    blocked joining non-daemon threads (ThreadPoolExecutor's atexit join). The daemon
    timer survives ``Py_FinalizeEx``'s joins. ``HERMES_EXIT_WATCHDOG_S=0`` disables.

      1. A cleanup step wedged on network I/O (memory provider
         ``on_session_end``, MCP teardown, remote terminal cleanup).
      2. Interpreter teardown blocked joining non-daemon threads —
         stdlib ``ThreadPoolExecutor`` workers are joined unconditionally
         by ``concurrent.futures``' atexit hook even after
         ``shutdown(wait=False)``, so one tool thread wedged on a socket
         held the process open forever (#27563 class).

    The shared daemon pool (``tools.daemon_pool``) removes the main cause
    of (2); this watchdog is the backstop for both. It arms a daemon
    timer when ``_run_cleanup`` starts; if the process is still alive
    after ``timeout_s`` it flushes logging/stdio and calls ``os._exit(0)``.
    Daemon threads keep running through ``Py_FinalizeEx``'s thread joins,
    so the timer fires even when the main thread is stuck in teardown.

    Tune with ``HERMES_EXIT_WATCHDOG_S`` (seconds); ``0`` disables. An
    impatient user isn't stuck waiting for this full timeout either — see
    ``_install_cleanup_skip_handler``, which lets a Ctrl+C pressed during
    ``_run_cleanup`` exit immediately instead.
    """
    if timeout_s is None:
        timeout_s = _exit_watchdog_timeout()
    if timeout_s <= 0:
        return
    # Never under pytest: a delayed os._exit(0) would silently kill the test worker.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    def _watchdog():
        time.sleep(timeout_s)
        # The signal-armed watchdog yields to cleanup's own timer once cleanup is running.
        if from_signal and _cleanup_in_progress:
            return

        try:
            logger.warning(
                "Exit watchdog fired after %.0fs — forcing process exit "
                "(a cleanup step or non-daemon thread is wedged).",
                timeout_s,
            )
        except Exception:
            pass
        _flush_logging_and_stdio()
        os._exit(0)

    with suppress(Exception):  # never block shutdown on watchdog setup
        threading.Thread(target=_watchdog, daemon=True, name="exit-watchdog").start()


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


def _shutdown_agent_memory_provider(agent) -> None:
    """Memory-provider shutdown (on_session_end + shutdown_all) at the real session boundary."""
    if not (agent and hasattr(agent, 'shutdown_memory_provider')):
        return
    # A /new shortly before exit leaves an LLM-bound boundary task queued; shutdown_all()'s
    # ~5s drain would cancel it, so give it a bounded head start (watchdog is the backstop).
    _mm = getattr(agent, '_memory_manager', None)
    if _mm is not None and hasattr(_mm, 'flush_pending'):
        with suppress(Exception):
            _mm.flush_pending(timeout=10)
    # Forward the agent's transcript so on_session_end hooks see the real conversation;
    # no-arg fallback for stubs / partially-initialised agents.
    _session_msgs = getattr(agent, '_session_messages', None)
    _sid = getattr(agent, "session_id", None) or "<unknown>"
    # ``_session_messages`` is set on ``AIAgent.__init__`` and refreshed every turn via
    # ``_persist_session``. Fall back to no-arg on test stubs / partially-initialised agents where the
    # attribute is missing. See #15165.
    if isinstance(_session_msgs, list):
        logger.info("CLI cleanup calling memory shutdown for session %s with %d message(s)", _sid, len(_session_msgs))
        agent.shutdown_memory_provider(_session_msgs)
    else:
        logger.info("CLI cleanup calling memory shutdown for session %s without session message list", _sid)
        agent.shutdown_memory_provider()


def _stop_cli_wake_word() -> None:
    from tools.wake_word import stop_listening
    if _cli_wake_owner is not None:
        stop_listening(owner=_cli_wake_owner)


def _interrupt_async_delegations() -> None:
    from tools.async_delegation import interrupt_all
    interrupt_all(reason="CLI shutdown")


def _shutdown_mcp_servers() -> None:
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    shutdown_mcp_servers()


def _shutdown_cached_aux_clients() -> None:
    # Otherwise AsyncHttpxClientWrapper.__del__ fires on a closed loop ("Press ENTER to continue...").
    from agent.auxiliary_client import shutdown_cached_clients
    shutdown_cached_clients()


# Ordered teardown steps (attribute names, resolved at call time so tests can patch them)
# and the exception class each swallows.
_CLEANUP_STEPS = (
    ("_stop_cli_wake_word", Exception), ("_cleanup_all_terminals", Exception),
    ("_interrupt_async_delegations", Exception), ("_cleanup_all_browsers", Exception),
    ("_shutdown_mcp_servers", BaseException), ("_shutdown_cached_aux_clients", Exception),
)


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

def _should_emit_cleanup_session_finalize(session_id: str | None) -> bool:
    # A handed-off session is owned by the gateway process — never finalize it here.
    # The CLI must not finalize it on exit — that sets end_reason on a row the gateway reopened and is
    # actively writing to, causing the handoff leg to vanish from session history (#88234).
    if session_id is not None and session_id in _handed_off_session_ids:
        return False
    if not _single_query_finalize_attempted_session_ids:
        return True
    if session_id is None:
        return False
    return session_id not in _single_query_finalize_attempted_session_ids


def _notify_session_finalize(*, session_id: str | None, platform: str = "cli", reason: str = "shutdown") -> None:
    with suppress(Exception):
        from hermes_cli.lifecycle import finalize_session
        finalize_session(session_id=session_id, platform=platform, reason=reason)


def _oneshot_agent_and_session(cli):
    """``(agent, session_id)`` for a one-shot run; the agent's id wins over the CLI's."""
    agent = getattr(cli, "agent", None)
    return agent, getattr(agent, "session_id", None) or getattr(cli, "session_id", None)


def _invoke_interrupted_session_end(agent, session_id, reason: str, **extra) -> None:
    """Best-effort ``on_session_end`` hook for a turn cut short (never raises)."""
    with suppress(Exception):
        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
        _invoke_hook(
            "on_session_end", session_id=session_id, completed=False, interrupted=True,
            model=getattr(agent, "model", None), platform=getattr(agent, "platform", None) or "cli",
            reason=reason, **extra,
        )


def _emit_interrupted_session_end(cli, *, reason: str = "keyboard_interrupt") -> None:
    """Best-effort on_session_end hook for interrupted non-interactive runs."""
    agent, session_id = _oneshot_agent_and_session(cli)
    if agent is None:
        return

    with suppress(Exception):
        agent.interrupt(reason.replace("_", " "))

    if session_id in _handed_off_session_ids:  # gateway owns the lifecycle now
        return
    if session_id:
        with suppress(Exception):
            cli.session_id = session_id

    _invoke_interrupted_session_end(
        agent, session_id, reason,
        task_id=getattr(agent, "_current_task_id", "") or "",
        turn_id=getattr(agent, "_current_turn_id", "") or "",
        api_request_id=getattr(agent, "_current_api_request_id", "") or "",
    )


def _notify_single_query_session_finalize(cli, *, reason: str = "shutdown") -> None:
    agent, session_id = _oneshot_agent_and_session(cli)
    if session_id in _single_query_finalize_attempted_session_ids:
        return
    if session_id in _handed_off_session_ids:  # gateway owns the lifecycle now
        return

    try:
        _notify_session_finalize(session_id=session_id, platform=getattr(agent, "platform", None) or "cli", reason=reason)
    finally:
        _single_query_finalize_attempted_session_ids.add(session_id)


def _flush_one_shot_session_store(cli) -> None:
    """Durably flush + finalize the one-shot session row before exit (idempotent, best-effort).

    One-shot runs get a single turn, so nothing retries a transiently-failed transcript
    flush, closes the session row, or drains token deltas the kanban ``os._exit(0)``
    path skips. Handed-off sessions are left alone.

    - a turn whose in-loop ``_flush_messages_to_session_db`` failed under write-lock contention (e.g. a busy
    multiplex gateway sharing state.db) was silently lost — the reply reached stdout and agent.log but the
    resumed session's stored history never changed (#88583); - the resumed/created titled session row was
    left dangling open (``ended_at``/``end_reason`` NULL) on every one-shot exit; - queued async
    token-accounting deltas relied on interpreter-exit hooks, which the kanban SIGTERM path's
    ``os._exit(0)`` skips entirely.
    Idempotent and best-effort: ``_persist_session`` dedupes via the per-message ``_DB_PERSISTED_MARKER``
    stamps (already-written turns are not re-written) and ``end_session`` no-ops on an already-ended row.
    See #88234.
    """
    agent, session_id = _oneshot_agent_and_session(cli)
    if agent is None or not session_id or session_id in _handed_off_session_ids:
        return
    if getattr(agent, "_persist_disabled", False):
        return
    # Passing cli.conversation_history keeps resumed messages identity-skipped even when
    # the failed flush never stamped them.
    try:
        msgs = getattr(agent, "_session_messages", None)
        if isinstance(msgs, list) and msgs and hasattr(agent, "_persist_session"):
            agent._persist_session(msgs, getattr(cli, "conversation_history", None))
    except Exception:
        logger.debug("one-shot final session persist retry failed", exc_info=True)
    db = getattr(agent, "_session_db", None) or getattr(cli, "_session_db", None)
    if db is None:
        return
    try:
        db.flush_token_counts()
    except Exception:
        logger.debug("one-shot token-count drain failed", exc_info=True)
    try:
        db.end_session(session_id, "cli_close")
    except Exception:
        logger.debug("one-shot end_session failed", exc_info=True)


def _wait_for_oneshot_background_completions(cli) -> None:
    """Bounded linger for notify_on_complete background processes (children write to our pipes).

    Waits on the whole registry: a one-shot process hosts one agent, and task_id
    filtering would skip processes registered before the session id settled.

    Skipped when the quiet -Q notify-resume loop already consumed the run's linger
    budget: it calls wait_for_pending_completions with a shared deadline, so a
    re-wait here would double-block on the same stuck notify_on_complete child.

    See #90879.
    """
    from tools.process_registry import process_registry

    if getattr(cli, "_quiet_notify_linger_done", False):
        return
    _agent, task_id = _oneshot_agent_and_session(cli)
    result = process_registry.wait_for_pending_completions(None)
    if result.get("waited"):
        logger.info(
            "One-shot exit linger for session %s: completed=%s timed_out=%s",
            task_id or "<unknown>",
            result.get("completed"),
            result.get("timed_out"),
        )


def _finalize_single_query(cli) -> None:
    """Close one-shot CLI resources before releasing the active session lease."""
    try:
        # Order matters: linger for spawned background work BEFORE any teardown (the
        # parent owns those children's stdout pipes); then the durable flush, since
        # memory-provider shutdown inside _run_cleanup can issue aux-LLM calls and
        # nothing after it may fail in a way that loses the turn.
        for step, what in (
            (_wait_for_oneshot_background_completions, "background completion wait"),
            (_flush_one_shot_session_store, "session store flush"),
        ):
            try:
                step(cli)
            except Exception:
                logger.debug("one-shot %s failed", what, exc_info=True)
        _notify_single_query_session_finalize(cli)
        _run_cleanup(notify_session_finalize=False)
    finally:
        cli._release_active_session()


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

    # Unlock first so `remove` isn't blocked by the lock placed at creation. Fail-soft.
    _git_quiet(["worktree", "unlock", wt_path], repo_root, log="git worktree unlock failed (non-fatal)")
    _git_quiet(["worktree", "remove", wt_path, "--force"], repo_root, timeout=15, log="Failed to remove worktree")
    _git_quiet(["branch", "-D", branch], repo_root, log=f"Failed to delete branch {branch}")

    _active_worktree = None
    _cprint(f"\033[32m✓ Worktree cleaned up: {wt_path}\033[0m")


def _run_state_db_auto_maintenance(session_db) -> None:
    """One-time repairs + auto-archive/prune/vacuum per the ``sessions:`` config. Never raises."""
    if session_db is None:
        return
    try:
        from hermes_cli.config import load_config as _load_full_config
        from hermes_constants import get_hermes_home as _get_hermes_home  # lazy: tests patch it
        _hermes_home_maint = _get_hermes_home()

        # One-time repairs, each latched in state_meta once it has run.
        for meta_key, repair, done_msg, skip_msg in (
            (
                "ghost_session_prune_v1",
                lambda: session_db.prune_empty_ghost_sessions(sessions_dir=_hermes_home_maint / "sessions"),
                "Pruned %d empty TUI ghost sessions", "Ghost session prune skipped: %s",
            ),
            (
                "orphaned_compression_finalize_v1",
                session_db.finalize_orphaned_compression_sessions,
                "Finalized %d orphaned compression sessions", "Orphan compression finalize skipped: %s",
            ),
        ):
            try:
                if not session_db.get_meta(meta_key):
                    count = repair()
                    session_db.set_meta(meta_key, "1")
                    if count:
                        logger.info(done_msg, count)
            except Exception as _exc:
                logger.debug(skip_msg, _exc)

        cfg = (_load_full_config().get("sessions") or {})

        # Auto-archive is independent of auto_prune: run it before prune's early return.
        if cfg.get("auto_archive", False):
            session_db.maybe_auto_archive(
                idle_days=float(cfg.get("auto_archive_days", 3)),
                min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            )

        if not cfg.get("auto_prune", False):
            return
        session_db.maybe_auto_prune_and_vacuum(
            retention_days=int(cfg.get("retention_days", 90)),
            min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            min_vacuum_interval_days=int(cfg.get("min_vacuum_interval_days", 30)),
            vacuum=bool(cfg.get("vacuum_after_prune", True)),
            sessions_dir=_hermes_home_maint / "sessions",
        )
    except Exception as exc:
        logger.debug("state.db auto-maintenance skipped: %s", exc)


def _run_checkpoint_auto_maintenance() -> None:
    """Call ``maybe_auto_prune_checkpoints`` per the ``checkpoints:`` config. Never raises."""
    try:
        from hermes_cli.config import load_config as _load_full_config
        cfg = (_load_full_config().get("checkpoints") or {})
        if not cfg.get("auto_prune", False):
            return
        from tools.checkpoint_manager import maybe_auto_prune_checkpoints
        # delete_orphans stays False: a missing workdir at startup is ambiguous (unmounted
        # volume / VPN down); orphans are only reclaimed by `hermes checkpoints prune`.
        maybe_auto_prune_checkpoints(
            retention_days=int(cfg.get("retention_days", 7)),
            min_interval_hours=int(cfg.get("min_interval_hours", 24)),
            delete_orphans=False,
            max_total_size_mb=int(cfg.get("max_total_size_mb", 500)),
        )
    except Exception as exc:
        logger.debug("checkpoint auto-maintenance skipped: %s", exc)


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


def _hex_to_ansi(hex_color: str, *, bold: bool = False) -> str:
    """Convert '#RRGGBB' to a true-color ANSI escape, remapping dark-tuned colors in light mode."""
    hex_color = _maybe_remap_for_light_mode(hex_color)
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return f"\033[{'1;' if bold else ''}38;2;{r};{g};{b}m"
    except (ValueError, IndexError):
        return _ACCENT_ANSI_DEFAULT if bold else "\033[38;2;184;134;11m"


# Light/dark terminal detection (mirrors ui-tui/src/theme.ts detectLightMode()). Priority:
# HERMES_LIGHT/HERMES_TUI_LIGHT env, HERMES_TUI_THEME, HERMES_TUI_BACKGROUND, COLORFGBG
# (bg slot 7/15 = light), OSC 11 query, default dark. Cached so the terminal is queried once.
_LIGHT_MODE_CACHE: bool | None = None
_TRUE_RE = re.compile(r"^(1|true|on|yes|y)$")
_FALSE_RE = re.compile(r"^(0|false|off|no|n)$")
_LIGHT_DEFAULT_TERM_PROGRAMS = frozenset()  # Apple_Terminal isn't reliable; require explicit config


def _luminance_from_hex(hex_str: str) -> float | None:
    """Rec.709 luma in [0, 1] for '#RGB'/'#RRGGBB', or None when malformed."""
    s = (hex_str or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6 or not all(c in "0123456789abcdefABCDEF" for c in s):
        return None
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


_DA1_REPLY_RE = re.compile(rb"\x1b\[\?[0-9;]*c")


def _query_osc11_background() -> str | None:
    """Terminal background via OSC 11 as "#RRGGBB", or None.

    Fenced with a DA1 sentinel (``ESC[c``): terminals answer in order and virtually all
    answer DA1, so its reply proves our OSC 11 was processed — otherwise a late reply
    leaks into prompt_toolkit's stdin as typed text. Skipped over SSH (round-trip too
    slow; a late BEL reads as Ctrl+G). A 50 ms drain after TCSAFLUSH catches stragglers.

    The OSC 11 query is fenced with a DA1 sentinel (\x1b[c) — the same
    pattern the Ink TUI's TerminalQuerier uses.  Terminals answer queries
    in order and virtually every terminal answers DA1, so seeing the DA1
    reply proves the terminal already ignored our OSC 11 (multiplexers
    like herdr answer DA1 in <1ms while swallowing OSC 11).  Without the
    fence we can only wait out a blind timeout, and a reply that arrives
    AFTER we stop listening leaks into prompt_toolkit's stdin as typed
    text — the "gibberish ANSI characters" seen inside terminal managers
    that relay color queries slowly (herdr, WSL bridges, some tmux
    setups).

    Skipped over SSH: the round-trip routinely exceeds our budget, so a
    late reply lands after prompt_toolkit has grabbed the tty — its payload
    leaks in as typed text and the BEL terminator reads as Ctrl+G (open
    editor), trapping the user in a stray editor. Remote sessions fall back
    to COLORFGBG / env hints / the dark default instead.

    After the main read + TCSAFLUSH, a short drain window (50 ms) catches
    late-arriving bytes that slipped past the flush — a race observed on VPS
    and container terminals under load (#40250).

    Typeahead safety: this function runs at ``cli`` import, seconds before
    prompt_toolkit attaches — exactly when users type or paste ahead into a
    still-booting tab.  Its read loop, TCSAFLUSH, and drain window are all
    stdin *eaters*: any typeahead they consume (or tear mid-escape-sequence)
    later surfaces as literal ``[200~…``/``^[[99;5u`` garbage in the
    composer.  Three guards keep typeahead intact:
      1. If stdin already has pending bytes before the query is written,
         skip the query entirely (fall back to env hints / dark default).
      2. When the DA1 fence closes with a parsed OSC 11 payload (healthy
         terminal, in-order replies), restore with TCSADRAIN — never
         TCSAFLUSH — and skip the drain window; there is nothing left to
         scrub, and anything queued is the user's.
      3. TCSAFLUSH + drain only run on the degraded paths (fence closed
         without a payload, or deadline/read error), where a late reply
         leak is still possible and typeahead was already protected by 1.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    if any(os.environ.get(v) for v in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return None
    try:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        return None
    # "clean" = fence closed with a parsed payload → restore must preserve
    # any queued typeahead (TCSADRAIN, no drain window).  Every other exit
    # keeps the scrubbing restore (TCSAFLUSH + drain).
    clean = False
    try:
        try:
            tty.setcbreak(fd)
        except Exception:
            return None
        import select
        try:
            # Typeahead guard: if the user already typed/pasted into this
            # still-booting tab, do not write the query at all — the read
            # loop below would consume (and tear) their bytes, and the
            # scrubbing restore would discard the rest.  Style falls back
            # to COLORFGBG / env hints / the dark default.
            pending, _, _ = select.select([fd], [], [], 0)
            if pending:
                clean = True  # nothing of ours is in flight; keep their bytes
                return None
        except Exception:
            return None
        try:
            # One write so the OSC 11 query and DA1 fence cannot reorder.
            sys.stdout.write("\x1b]11;?\x1b\\\x1b[c")
            sys.stdout.flush()
        except Exception:
            return None
        # Read until the DA1 fence closes — proof the terminal has processed
        # everything up to and including our OSC 11, so nothing can arrive
        # late and leak into prompt_toolkit's stdin.  DA1 is answered by
        # effectively every terminal ever made (it predates color), and on
        # real terminals the fence closes in single-digit milliseconds
        # (herdr: <1ms, xterm/kitty/tmux: <5ms).  The 1s deadline is a
        # safety net for a hypothetical terminal that ignores DA1 — not a
        # window we ever expect to wait out.  A slow in-order relay that
        # delivers the OSC 11 reply at e.g. 400ms is handled correctly:
        # we keep listening until its DA1 reply follows, so the payload is
        # consumed here instead of leaking as typed input (the "gibberish
        deadline = time.monotonic() + 1.0
        buf = b""
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], deadline - time.monotonic())
            if not r:
                continue
            try:
                chunk = os.read(fd, 64)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if _DA1_REPLY_RE.search(buf):
                break
        # Reply: \x1b]11;rgb:RRRR/GGGG/BBBB\x1b\\ — components are 1-4 hex digits.
        m = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", buf)
        if not m:
            return None
        # Fence closed AND payload parsed: an in-order terminal answered
        # both queries and both were consumed above.  Nothing of ours can
        # still be in flight, so the scrubbing restore below must not run —
        # it would eat typeahead that arrived after the fence.
        clean = True
        # Each component is 1-4 hex digits — normalize to 8-bit
        def norm(h: bytes) -> int:
            v = int(h, 16)
            bits = len(h) * 4
            return (v * 255) // ((1 << bits) - 1) if bits else 0
        r, g, b = norm(m.group(1)), norm(m.group(2)), norm(m.group(3))
        return f"#{r:02X}{g:02X}{b:02X}"
    finally:
        if clean:
            # Preserve queued input: TCSADRAIN waits for our own pending output
            # but does NOT discard unread input (typeahead).
            with suppress(Exception):
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        else:
            # Degraded exit (fence without payload, deadline, read error): a slow/partial
            # reply may still be in flight. TCSAFLUSH discards unread input, scrubbing it
            # before prompt_toolkit reads it as keystrokes.
            with suppress(Exception):
                termios.tcsetattr(fd, termios.TCSAFLUSH, old)
            # Race guard: on slow terminals the OSC 11 reply can arrive after TCSAFLUSH.
            try:
                drain_deadline = time.monotonic() + 0.05
                while time.monotonic() < drain_deadline:
                    r, _, _ = select.select([fd], [], [], drain_deadline - time.monotonic())
                    if not r or not os.read(fd, 64):
                        break
            except Exception:
                pass


def _heal_cooked_mode_drift(fd: int) -> bool:
    """Re-apply raw mode on *fd* when termios drifted back to cooked (POSIX only).

    A lost ``run_in_terminal`` cooked_mode() restore makes the kernel line-buffer every
    keystroke and the CLI looks dead. Mirrors prompt_toolkit's raw_mode flag surgery in
    place. Returns True when healed; False when already raw or not inspectable.
    """
    try:
        import termios
        attrs = termios.tcgetattr(fd)
    except Exception:
        return False
    lflag = attrs[3]
    if not (lflag & (termios.ICANON | termios.ECHO)):
        return False  # still raw — nothing to do
    attrs[3] = lflag & ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
    attrs[0] = attrs[0] & ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR | termios.IGNCR)
    attrs[6][termios.VMIN] = 1
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        return False
    return True


def _detect_light_mode_uncached() -> bool:
    """The detection ladder documented above; may raise (caller maps errors to dark)."""
    for var in ("HERMES_LIGHT", "HERMES_TUI_LIGHT"):
        v = (os.environ.get(var) or "").strip().lower()
        if _TRUE_RE.match(v):
            return True
        if _FALSE_RE.match(v):
            return False
    theme = (os.environ.get("HERMES_TUI_THEME") or "").strip().lower()
    if theme == "light":
        return True
    if theme == "dark":
        return False
    bg_lum = _luminance_from_hex(os.environ.get("HERMES_TUI_BACKGROUND") or "")
    if bg_lum is not None:
        return bg_lum >= 0.5
    last = (os.environ.get("COLORFGBG") or "").strip().split(";")[-1]
    if last.isdigit() and 0 <= int(last) < 16:
        return int(last) in {7, 15}
    bg_color = _query_osc11_background()
    if bg_color:
        lum = _luminance_from_hex(bg_color)
        if lum is not None:
            return lum >= 0.5
    return (os.environ.get("TERM_PROGRAM") or "").strip() in _LIGHT_DEFAULT_TERM_PROGRAMS


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


# Light-mode equivalents of skin colors unreadable on cream backgrounds. Only colors used
# as STANDALONE foregrounds: ones paired with a dark bg (status bar text on #1a1a2e) would
# become invisible the other direction, hence #C0C0C0/#888888/#555555/#8B8682 are skipped.
_LIGHT_MODE_REMAP: dict[str, str] = {
    "#FFF8DC": "#1A1A1A", "#FFD700": "#9A6B00", "#FFBF00": "#8A5A00", "#B8860B": "#5C4500",
    "#DAA520": "#6B4F00", "#F1E6CF": "#1A1A1A", "#c9d1d9": "#24292F", "#EAF7FF": "#0F1B26",
    "#F5F5F5": "#1A1A1A", "#FFF0D4": "#1A1A1A", "#CD7F32": "#8A4F1A", "#FFEFB5": "#3A2A00",
}
_LIGHT_MODE_REMAP_UPPER = {k.upper(): v for k, v in _LIGHT_MODE_REMAP.items()}


def _maybe_remap_for_light_mode(hex_color: str) -> str:
    """In light mode, remap a dark-tuned color to its higher-contrast equivalent."""
    if not _detect_light_mode():
        return hex_color
    if not hex_color or not hex_color.startswith("#"):
        return hex_color
    return _LIGHT_MODE_REMAP_UPPER.get(hex_color.upper(), hex_color)


def _install_skin_light_mode_hook() -> None:
    """Wrap SkinConfig.get_color so EVERY skin color read goes through the light-mode remap. Idempotent."""
    try:
        from hermes_cli.skin_engine import SkinConfig  # type: ignore[import]
    except Exception:
        return
    if getattr(SkinConfig, "_hermes_light_mode_hook_installed", False):
        return
    _orig_get_color = SkinConfig.get_color

    def _wrapped_get_color(self, key, fallback=""):
        value = _orig_get_color(self, key, fallback)
        try:
            return _maybe_remap_for_light_mode(value)
        except Exception:
            return value

    SkinConfig.get_color = _wrapped_get_color  # type: ignore[method-assign]
    SkinConfig._hermes_light_mode_hook_installed = True  # type: ignore[attr-defined]


_install_skin_light_mode_hook()


# Prime the light-mode cache when interactive so OSC 11 happens before prompt_toolkit owns the tty.
with suppress(Exception):
    if sys.stdin.isatty() and sys.stdout.isatty():
        _detect_light_mode()


class _SkinAwareAnsi:
    """Lazy ANSI escape resolved from the skin on first use; ``.reset()`` after a ``/skin`` switch."""

    def __init__(self, skin_key: str, fallback_hex: str = "#FFD700", *, bold: bool = False):
        self._skin_key = skin_key
        self._fallback_hex = fallback_hex
        self._bold = bold
        self._cached: str | None = None

    def __str__(self) -> str:
        if self._cached is None:
            try:
                from hermes_cli.skin_engine import get_active_skin
                self._cached = _hex_to_ansi(
                    get_active_skin().get_color(self._skin_key, self._fallback_hex),
                    bold=self._bold,
                )
            except Exception:
                self._cached = _hex_to_ansi(self._fallback_hex, bold=self._bold)
        return self._cached

    def __add__(self, other: str) -> str:
        return str(self) + other

    def __radd__(self, other: str) -> str:
        return other + str(self)

    def reset(self) -> None:
        """Clear cache so the next access re-reads the skin."""
        self._cached = None


_ACCENT = _SkinAwareAnsi("response_border", "#FFD700", bold=True)
# dim+italic attributes (not a hex) so dim text inherits the terminal foreground in both modes.
_DIM = "\x1b[2;3m"


def _tty_wrap(s: str, sgr: str) -> str:
    """Wrap *s* in an SGR attribute when stdout is a real TTY; plain text otherwise."""
    try:
        return f"{sgr}{s}\x1b[0m" if sys.stdout.isatty() else str(s)
    except Exception:
        return str(s)


_b = functools.partial(_tty_wrap, sgr="\x1b[1m")  # bold when stdout is a real TTY
_d = functools.partial(_tty_wrap, sgr="\x1b[2;3m")  # dim-italic when stdout is a real TTY


def _accent_hex() -> str:
    """Return the active skin accent color for legacy CLI output lines."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_color("ui_accent", "#FFBF00")
    except Exception:
        return "#FFBF00"


def _rich_text_from_ansi(text: str) -> _RichText:
    """Rich Text from ANSI output; literal ``[brackets]`` are not treated as markup."""
    return _RichText.from_ansi(text or "")


def _strip_markdown_syntax(text: str) -> str:
    """Best-effort markdown marker removal for plain-text display."""
    plain = _rich_text_from_ansi(text or "").plain
    # HR markers: "-"/"_" runs of 3+, but "*" only when exactly 3 (cron schedules "* * * * *").
    plain = re.sub(r"^\s{0,3}(?:[-_]\s*){3,}$", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"^\s{0,3}(?:\*\s*){3}\s*$", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"^\s{0,3}#{1,6}\s+", "", plain, flags=re.MULTILINE)
    # Blockquotes, lists, and checkboxes are preserved because they carry structure.
    plain = re.sub(r"(```+|~~~+)", "", plain)
    plain = re.sub(r"`([^`]*)`", r"\1", plain)
    plain = re.sub(r"!\[([^\]]*)\]\([^\)]*\)", r"\1", plain)
    plain = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", plain)
    plain = re.sub(r"\*\*\*([^*]+)\*\*\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)___([^_]+)___(?!\w)", r"\1", plain)
    plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"\1", plain)
    # `*emphasis*` only when the inner text is non-whitespace (cron expressions again).
    plain = re.sub(r"\*([^\s*][^*]*?[^\s*])\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", plain)
    plain = re.sub(r"~~([^~]+)~~", r"\1", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip("\n")


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


def _preserve_windows_dot_segments_for_markdown(text: str) -> str:
    r"""Double the ``\`` before hidden dirs in Windows paths: CommonMark reads ``\.`` as an escaped dot."""
    if "\\." not in text:
        return text

    def _protect(match: re.Match[str]) -> str:
        return re.sub(r"(?<!\\)\\(?=\.)", r"\\\\", match.group(0))

    return _WINDOWS_PATH_WITH_DOT_SEGMENT_RE.sub(_protect, text)


def _terminal_columns() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


def _terminal_width_for_streaming() -> int:
    """Display cells available inside the streamed response box.

    FORK: the streaming path prefixes every line with ``_STREAM_PAD`` (configurable via
    ``display.response_indent_width``, default 4) and hard-wraps at this budget so the box
    gets a matching RIGHT margin, so ``_STREAM_PAD`` is subtracted on BOTH sides (upstream,
    running flush-left, only subtracts it once). Also the realigner's horizontal-table budget.
    """
    return max(20, _terminal_columns() - (2 * len(_STREAM_PAD)) - 2)


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


def _render_final_assistant_content(text: str, mode: str = "render"):
    """Render final assistant content as markdown, stripped text, or raw text."""
    from rich.markdown import Markdown

    # FORK: the Panel has len(_STREAM_PAD) cells of left+right padding (see
    # display.response_indent_width) + 1 border cell each side; margin so resize
    # races don't push a borderline table into soft-wrap.
    panel_width = max(20, _terminal_columns() - (2 * len(_STREAM_PAD)) - 4)

    normalized_mode = str(mode or "render").strip().lower()
    if normalized_mode == "strip":
        # Strip first (inline markdown changes cell width), then re-align padding.
        return _RichText(realign_markdown_tables(_strip_markdown_syntax(text), panel_width))
    if normalized_mode == "raw":
        return _rich_text_from_ansi(text or "")

    # Normalising under-padded tables up front gives narrow-panel fallbacks consistent input.
    plain = _rich_text_from_ansi(text or "").plain
    plain = _preserve_windows_dot_segments_for_markdown(plain)
    plain = realign_markdown_tables(plain, panel_width)
    return Markdown(plain)


def _post_stream_transform_output(response: str, result: dict | None) -> str:
    """Text still to display after a streamed response transform: the suffix, or the whole response when replaced."""
    if not result or not result.get("response_transformed"):
        return ""

    original = result.get("pre_transform_response") or ""
    if original and response.startswith(original):
        return response[len(original):]

    return f"\n[Response transformed after streaming]\n{response}"


_OUTPUT_HISTORY_ENABLED = True
_OUTPUT_HISTORY_REPLAYING = False
_OUTPUT_HISTORY_SUPPRESSED = False
_OUTPUT_HISTORY_MAX_LINES = 200
_OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


def _coerce_output_history_limit(value) -> int:
    try:
        return max(10, int(value))
    except (TypeError, ValueError):
        return 200


def _configure_output_history(enabled: bool, max_lines=200) -> None:
    """Configure recent CLI output replayed after terminal redraws."""
    global _OUTPUT_HISTORY_ENABLED, _OUTPUT_HISTORY_MAX_LINES, _OUTPUT_HISTORY
    _OUTPUT_HISTORY_ENABLED = bool(enabled)
    _OUTPUT_HISTORY_MAX_LINES = _coerce_output_history_limit(max_lines)
    _OUTPUT_HISTORY = deque(maxlen=_OUTPUT_HISTORY_MAX_LINES)


def _clear_output_history() -> None:
    _OUTPUT_HISTORY.clear()


@contextmanager
def _suspend_output_history():
    global _OUTPUT_HISTORY_SUPPRESSED
    old_value = _OUTPUT_HISTORY_SUPPRESSED
    _OUTPUT_HISTORY_SUPPRESSED = True
    try:
        yield
    finally:
        _OUTPUT_HISTORY_SUPPRESSED = old_value


def _output_history_recording() -> bool:
    return _OUTPUT_HISTORY_ENABLED and not _OUTPUT_HISTORY_REPLAYING and not _OUTPUT_HISTORY_SUPPRESSED


def _record_output_history_entry(entry) -> None:
    if _output_history_recording():
        _OUTPUT_HISTORY.append(entry)


def _record_output_history(text: str) -> None:
    if _output_history_recording():
        _OUTPUT_HISTORY.extend(str(text).replace("\r", "").rstrip("\n").splitlines())


def _replay_output_history() -> None:
    """Repaint recent output above the prompt after a full screen clear."""
    global _OUTPUT_HISTORY_REPLAYING
    if not _OUTPUT_HISTORY_ENABLED or not _OUTPUT_HISTORY:
        return
    _OUTPUT_HISTORY_REPLAYING = True
    try:
        rendered_lines = []
        for entry in tuple(_OUTPUT_HISTORY):
            lines = [entry]
            if callable(entry):
                try:
                    lines = entry()
                except Exception:
                    continue
                if isinstance(lines, str):
                    lines = lines.splitlines()
            rendered_lines.extend(str(line) for line in lines)
        if rendered_lines:
            # One payload: per-line pt prints each force a sync redraw (a waterfall of old output).
            _pt_print(_PT_ANSI("\n".join(rendered_lines)))
    except Exception:
        pass
    finally:
        _OUTPUT_HISTORY_REPLAYING = False


def _pt_print_ansi(text: str) -> None:
    """``_pt_print(ANSI(text))``, falling back to ``print`` when stdout is not a real console."""
    try:
        _pt_print(_PT_ANSI(text))
    except Exception:
        # NoConsoleScreenBufferError (Windows) / OSError when stdout is e.g. a worker log file.
        with suppress(Exception):
            print(text)


def _cprint(text: str):
    """Print ANSI text through prompt_toolkit's renderer (patch_stdout swallows raw ANSI).

    From a background thread while an Application runs, a direct print races the input
    redraw and gets buried, so those go through ``run_in_terminal`` via ``call_soon_threadsafe``.
    """
    _record_output_history(text)

    try:
        from prompt_toolkit.application import get_app_or_none, run_in_terminal
    except Exception:
        _pt_print(_PT_ANSI(text))
        return

    try:
        app = get_app_or_none()
    except Exception:
        app = None

    if app is None or not getattr(app, "_is_running", False):
        _pt_print_ansi(text)
        return

    import asyncio as _asyncio

    try:
        loop = app.loop  # type: ignore[attr-defined]
    except Exception:
        loop = None
    try:
        # get_running_loop(): get_event_loop() warns from threads with no current loop.
        # Use get_running_loop() instead of get_event_loop() to avoid the DeprecationWarning /
        # RuntimeWarning emitted by Python 3.10+ when get_event_loop() is called from a thread that has no
        # current event loop set (e.g. the process_loop background thread). Fixes #19285.
        current_loop = _asyncio.get_running_loop()
    except Exception:
        current_loop = None
    if loop is None or (current_loop is loop and loop.is_running()):
        _pt_print(_PT_ANSI(text))
        return

    def _schedule():
        # run_in_terminal() returns an awaitable (pt >= 3.0) that must be scheduled or the
        # output is dropped, or None (mocks / older pt) when it already ran synchronously.
        # Never fall back to a bare print on error: the sync path already printed.
        with suppress(Exception):
            import inspect as _inspect
            coro = run_in_terminal(lambda: _pt_print(_PT_ANSI(text)))
            if coro is not None and (_inspect.isawaitable(coro) or _inspect.iscoroutine(coro)):
                _asyncio.ensure_future(coro)

    try:
        loop.call_soon_threadsafe(_schedule)
    except Exception:
        _pt_print_ansi(text)


def _prepend_note_to_message(message, note: str):
    """Prepend a one-shot note to a user message (str, or content-part list when an image is attached).

    For lists the note is folded into the first text part or inserted as a leading one.
    Unknown shapes are returned unchanged.
    """
    note = str(note or "").strip()
    if not note:
        return message
    if isinstance(message, str):
        return f"{note}\n\n{message}" if message else note
    if isinstance(message, list):
        parts = list(message)
        for i, part in enumerate(parts):
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                parts[i] = {**part, "text": f"{note}\n\n{text}" if text else note}
                return parts
        return [{"type": "text", "text": note}, *parts]
    return message


def _pt_app_is_running() -> bool:
    """Whether a prompt_toolkit Application currently owns the live terminal."""
    try:
        from prompt_toolkit.application import get_app_or_none
        app = get_app_or_none()
    except Exception:
        return False
    return app is not None and bool(getattr(app, "_is_running", False))


def _cli_visible_print(text: str = "") -> None:
    """``print`` unless a prompt_toolkit Application owns the terminal (patch_stdout swallows bare prints)."""
    if _pt_app_is_running():
        _cprint(text)
    else:
        print(text)


_IMAGE_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.bmp', '.tiff', '.tif', '.svg', '.ico',
})


def _termux_example_image_path(filename: str = "cat.png") -> str:
    """Return a realistic example media path for the current Termux setup."""
    candidates = [
        os.path.expanduser("~/storage/shared"),
        "/sdcard",
        "/storage/emulated/0",
        "/storage/self/primary",
    ]
    # Literal "/" so the Android hint is right even on Windows.
    for root in candidates:
        if os.path.isdir(root):
            return f"{root}/Pictures/{filename}"
    return f"~/storage/shared/Pictures/{filename}"


def _split_path_input(raw: str) -> tuple[str, str]:
    r"""Split a leading path token (quoted or with ``\ `` escapes) from trailing free-form text."""
    raw = str(raw or "").strip()
    if not raw:
        return "", ""

    if raw[0] in {'"', "'"}:
        quote = raw[0]
        pos = 1
        while pos < len(raw):
            ch = raw[pos]
            if ch == '\\' and pos + 1 < len(raw):
                pos += 2
                continue
            if ch == quote:
                return raw[1:pos], raw[pos + 1 :].strip()
            pos += 1
        return raw[1:], ""

    pos = 0
    while pos < len(raw):
        ch = raw[pos]
        if ch == '\\' and pos + 1 < len(raw) and raw[pos + 1] == ' ':
            pos += 2
        elif ch == ' ':
            break
        else:
            pos += 1

    return raw[:pos].replace('\\ ', ' '), raw[pos:].strip()


def _resolve_attachment_path(raw_path: str) -> Path | None:
    """Resolve a user-supplied attachment path (quotes, ``~``, env vars, ``file://``; relative to TERMINAL_CWD).

    Returns ``None`` unless it resolves to an existing file.
    """
    token = str(raw_path or "").strip()
    if not token:
        return None

    if token[0] == token[-1] and token[0] in {'"', "'"}:
        token = token[1:-1].strip()
    token = token.replace('\\ ', ' ')
    if not token:
        return None

    expanded = token
    if token.startswith("file://"):
        try:
            parsed = urlparse(token)
            if parsed.scheme == "file":
                expanded = unquote(parsed.path or "")
                if parsed.netloc and os.name == "nt":
                    expanded = f"//{parsed.netloc}{expanded}"
                elif os.name == "nt" and len(expanded) >= 3 and expanded[0] == "/" and expanded[1].isalpha() and expanded[2] == ":":
                    # file:///C:/... parses to path "/C:/..." — drop the leading slash
                    # so it resolves as a drive-letter path.
                    expanded = expanded[1:]
        except Exception:
            expanded = token
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    if os.name != "nt":
        normalized = expanded.replace("\\", "/")
        if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/" and normalized[0].isalpha():
            expanded = f"/mnt/{normalized[0].lower()}/{normalized[3:]}"
    path = Path(expanded)
    if not path.is_absolute():
        base_dir = Path(os.getenv("TERMINAL_CWD", os.getcwd()))
        path = base_dir / path

    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    # ENAMETOOLONG for a pasted `/goal <long prose>` that passed the `/` prefilter
    # would otherwise reach process_loop and silently lose the input.
    try:
        if not resolved.exists() or not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


def _file_drop_result(path: Path, remainder: str) -> dict:
    return {"path": path, "is_image": path.suffix.lower() in _IMAGE_EXTENSIONS, "remainder": remainder}


def _detect_file_drop(user_input: str) -> "dict | None":
    """Detect a dragged/pasted file path at the start of *user_input* -> ``{path, is_image, remainder}`` or None."""
    if not isinstance(user_input, str):
        return None

    stripped = user_input.strip()
    if not stripped:
        return None

    # Optionally quoted; then /, ~, ./, ../, a Windows drive prefix, or (unquoted) file://.
    quoted = stripped[:1] in {"'", '"'}
    unquoted = stripped[1:] if quoted else stripped
    starts_like_path = (
        unquoted.startswith(("/", "~", "./", "../"))
        or (not quoted and unquoted.startswith("file://"))
        or (len(unquoted) >= 3 and unquoted[1] == ":" and unquoted[2] in {"\\", "/"} and unquoted[0].isalpha())
    )
    if not starts_like_path:
        return None

    direct_path = _resolve_attachment_path(stripped)
    if direct_path is not None:
        return _file_drop_result(direct_path, "")

    first_token, remainder = _split_path_input(stripped)
    drop_path = _resolve_attachment_path(first_token)
    if drop_path is None and " " in stripped and not quoted:
        for pos in reversed([idx for idx, ch in enumerate(stripped) if ch == " "]):
            drop_path = _resolve_attachment_path(stripped[:pos].rstrip())
            if drop_path is not None:
                remainder = stripped[pos + 1 :].strip()
                break
    if drop_path is None:
        return None
    return _file_drop_result(drop_path, remainder)


def _format_image_attachment_badges(attached_images: list[Path], image_counter: int, width: int | None = None) -> str:
    """Attached-image badge row: compact summary on narrow terminals, per-image badges otherwise."""
    if not attached_images:
        return ""

    width = width or shutil.get_terminal_size((80, 24)).columns

    def _trunc(name: str, limit: int) -> str:
        return name if len(name) <= limit else name[: max(1, limit - 3)] + "..."

    if width < 52:
        if len(attached_images) == 1:
            return f"[📎 {_trunc(attached_images[0].name, 20)}]"
        return f"[📎 {len(attached_images)} images attached]"

    if width < 80:
        if len(attached_images) == 1:
            return f"[📎 {_trunc(attached_images[0].name, 32)}]"
        return f"[📎 {_trunc(attached_images[0].name, 20)}] [+{len(attached_images) - 1}]"

    base = image_counter - len(attached_images) + 1
    return " ".join(f"[📎 Image #{base + i}]" for i in range(len(attached_images)))


def _should_auto_attach_clipboard_image_on_paste(pasted_text: str) -> bool:
    """Auto-attach clipboard images only for image-only paste gestures."""
    return not pasted_text.strip()


_strip_leaked_bracketed_paste_wrappers = _lazy_shim(
    "hermes_cli.input_sanitize", "strip_leaked_bracketed_paste_wrappers", "_strip_leaked_bracketed_paste_wrappers"
)


def _hermes_call_output_screen_diff(
    orig_osd, app, output, screen, current_pos, color_depth, previous_screen, last_style, is_done, full_screen,
    attrs_for_style_string, style_string_has_style, size, previous_width,
):
    """prompt_toolkit ``_output_screen_diff`` with resize guards.

    Inflates ``previous_screen.height`` when the new screen is taller so pt skips the
    cursor move that stamps chrome into scrollback; on a corrupt previous paint buffer
    (tmux re-attach) retries once as a first paint instead of crashing the loop.

    1. 2. On AttributeError/TypeError from a corrupt previous paint buffer (classic after tmux attach with
    same width), retry once with ``previous_screen=None`` so pt first-paints cleanly instead of crashing the
    event loop with ``'cell' object has no attribute 'char'``. See #26137.
    """
    try:
        if previous_screen is not None and hasattr(previous_screen, "height") and previous_screen.height < screen.height:
            previous_screen.height = screen.height
    except Exception:
        pass

    common = (app, output, screen, current_pos, color_depth)
    tail = (is_done, full_screen, attrs_for_style_string, style_string_has_style, size)
    try:
        return orig_osd(*common, previous_screen, last_style, *tail, previous_width)
    except (AttributeError, TypeError):
        # Corrupt previous_screen / row cells after client reattach: previous_screen=None
        # takes the first-paint erase path, previous_width=0 treats the width as changed.
        return orig_osd(*common, None, None, *tail, 0)


def _apply_bracketed_paste_timeout_patch() -> None:
    """Patch ``Vt100Parser.feed`` to flush a bracketed paste whose ESC[201~ end mark never arrives.

    Without it a dropped end mark (SSH glitch, sleep/wake) freezes input forever. Idempotent.
    """
    try:
        import prompt_toolkit.input.vt100_parser as _vt100_mod
        from prompt_toolkit.keys import Keys as _PtKeys
        from prompt_toolkit.key_binding.key_processor import KeyPress as _PtKeyPress

        if getattr(_vt100_mod, "_hermes_bp_timeout_patched", False):
            return

        _BP_TIMEOUT_S = 2.0

        def _patched_vt100_feed(self_parser, data: str) -> None:
            if self_parser._in_bracketed_paste:
                self_parser._paste_buffer += data
                end_mark = "\x1b[201~"

                if end_mark in self_parser._paste_buffer:
                    end_index = self_parser._paste_buffer.index(end_mark)
                    paste_content = self_parser._paste_buffer[:end_index]
                    self_parser.feed_key_callback(_PtKeyPress(_PtKeys.BracketedPaste, paste_content))
                    self_parser._in_bracketed_paste = False
                    remaining = self_parser._paste_buffer[end_index + len(end_mark):]
                    self_parser._paste_buffer = ""
                    self_parser._hermes_bp_start = None
                    if remaining:
                        _patched_vt100_feed(self_parser, remaining)
                else:
                    bp_start = getattr(self_parser, "_hermes_bp_start", None)
                    now = time.monotonic()
                    if bp_start is None:
                        self_parser._hermes_bp_start = now
                    elif now - bp_start > _BP_TIMEOUT_S:
                        paste_content = self_parser._paste_buffer
                        self_parser._in_bracketed_paste = False
                        self_parser._paste_buffer = ""
                        self_parser._hermes_bp_start = None
                        if paste_content:
                            self_parser.feed_key_callback(_PtKeyPress(_PtKeys.BracketedPaste, paste_content))
                            logger.warning(
                                "Bracketed-paste timeout (%.1fs) — flushed %d bytes "
                                "without end mark. Terminal may have dropped ESC[201~ "
                                "(see #16263).",
                                now - bp_start, len(paste_content),
                            )
            else:
                # Re-inlined: calling the original would double-buffer after entering paste mode.
                for i, c in enumerate(data):
                    if self_parser._in_bracketed_paste:
                        _patched_vt100_feed(self_parser, data[i:])
                        break
                    self_parser._input_parser.send(c)

        _vt100_mod.Vt100Parser.feed = _patched_vt100_feed
        _vt100_mod._hermes_bp_timeout_patched = True
        logger.debug("Applied Vt100Parser bracketed-paste timeout patch (#16263)")
    except Exception as exc:  # noqa: BLE001 — defensive: never break startup
        logger.debug("Bracketed-paste timeout patch skipped: %s", exc)


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


def _is_ghostty_terminal(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the terminal is Ghostty.

    Ghostty gets ONLY modifyOtherKeys: its Kitty disambiguate mode strips Alt from
    Backspace (upstream bug), breaking backward-kill-word.

    Ghostty implements modifyOtherKeys correctly (it then emits ``\\x1b[27;3;127~``, which the alias table
    also maps). See #87630.
    """
    env = os.environ if env is None else env
    return (env.get("TERM_PROGRAM") or "").strip() == "ghostty" or (env.get("TERM") or "").strip().lower() == "xterm-ghostty"


def _terminal_supports_extended_enter_keys(env: Optional[Mapping[str, str]] = None) -> bool:
    """Allowlist of terminals where requesting modified-Enter reporting is safe (aligned with the Ink TUI)."""
    env = os.environ if env is None else env
    term_program = (env.get("TERM_PROGRAM") or "").strip()
    term = (env.get("TERM") or "").strip().lower()
    return bool(
        env.get("WT_SESSION")
        or term_program in {"iTerm.app", "WezTerm", "ghostty", "vscode"}
        or env.get("KITTY_WINDOW_ID") or "kitty" in term
        or term == "xterm-ghostty"
        or term.startswith("tmux") or term_program.lower() == "tmux"
    )


def _enable_extended_enter_keys(output=None, env: Optional[Mapping[str, str]] = None) -> bool:
    """Ask allowlisted terminals to report modified keys distinctly.

    Pushes BOTH kitty keyboard protocol and xterm modifyOtherKeys (kitty dropped the
    latter; tmux/VS Code only accept it). Both re-encode modified keys as sequences
    stock prompt_toolkit barely maps (Ctrl+C once arrived as ``ESC[99;5u``), so
    ``install_modify_other_keys_aliases()`` must have run first. Ghostty gets only
    modifyOtherKeys. The exit reset pops both modes.

    Under either protocol the terminal re-encodes modified keys as escape sequences — Kitty disambiguate
    mode as ``ESC[<codepoint>;<mod>u`` (plus the Esc key as ``ESC[27u``), modifyOtherKeys=2 as
    ``ESC[27;<mod>;<codepoint>~``. Stock prompt_toolkit 3.x maps almost none of these, which is why the CSI
    >1u push was temporarily removed in 87074 (Ctrl+C arrived as ``ESC[99;5u`` and died, #56684).
    ``install_modify_other_keys_aliases()`` (called at CLI startup from ``hermes_cli.pt_input_extras``) now
    populates ``ANSI_SEQUENCES`` with the full Ctrl/Alt/Shift/multi-modifier and functional-key tables under
    BOTH formats, so every existing key binding continues to fire — including Ctrl+C, which is handled by
    prompt_toolkit's ``c-c`` binding (raw mode clears ISIG, so the kernel INTR path was never in play for
    the CLI).
    See #87630.
    """
    if not _terminal_supports_extended_enter_keys(env):
        return False
    seq = _MODIFY_OTHER_KEYS_SEQ if _is_ghostty_terminal(env) else _EXTENDED_ENTER_KEYS_SEQ
    try:
        if output is not None and hasattr(output, "write_raw"):
            output.write_raw(seq)
            output.flush()
            return True
        if sys.stdout is not None and sys.stdout.isatty():
            sys.stdout.write(seq)
            sys.stdout.flush()
            return True
    except Exception:
        pass
    return False


def _cli_multiline_shortcuts_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """``display.cli_multiline_shortcuts`` (default on: Ctrl+J = newline; off restores the legacy c-j submit)."""
    if config is None:
        config = CLI_CONFIG
    display = config.get("display") if isinstance(config, dict) else None
    value = display.get("cli_multiline_shortcuts", True) if isinstance(display, dict) else True
    if isinstance(value, bool):
        return value
    return not (isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off", "disabled"})


def _is_backslash_line_continuation(text: str) -> bool:
    """True when Enter should turn a trailing backslash into a newline."""
    return bool(_BACKSLASH_LINE_CONTINUATION_RE.search(text or ""))


def _apply_backslash_line_continuation(text: str) -> str:
    """Replace a trailing ``\\`` marker with an actual newline."""
    return _BACKSLASH_LINE_CONTINUATION_RE.sub("", text or "") + "\n"


def _preserve_ctrl_enter_newline() -> bool:
    """Environments delivering Ctrl+Enter as bare LF (Windows Terminal, WSL, SSH, Ghostty): c-j must stay newline.

    See issue #22379.
    """
    env = os.environ
    if (
        sys.platform == "win32"
        or any(env.get(v) for v in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "WT_SESSION",
                                    "GHOSTTY_RESOURCES_DIR", "GHOSTTY_BIN_DIR"))
        or env.get("TERM", "").lower() == "xterm-ghostty" or env.get("TERM_PROGRAM", "").lower() == "ghostty"
        or "microsoft" in env.get("WSL_DISTRO_NAME", "").lower()
    ):
        return True
    # WSL env vars can be scrubbed under sudo; also peek /proc.
    for p in ("/proc/version", "/proc/sys/kernel/osrelease"):
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                if "microsoft" in f.read().lower():
                    return True
        except OSError:
            continue
    return False


def _bind_prompt_submit_keys(kb, handler, *, multiline_shortcuts_enabled: Optional[bool] = None) -> None:
    """Enter always submits; c-j submits only with multiline shortcuts off AND where Ctrl+Enter isn't c-j.

    Even when the setting is disabled, environments where Ctrl+Enter is known to arrive as c-j (Windows,
    WSL, SSH, Windows Terminal, Ghostty) keep c-j reserved for newline; otherwise Ctrl+Enter submits instead
    of composing. See _preserve_ctrl_enter_newline() and issue #22379.
    """
    if multiline_shortcuts_enabled is None:
        multiline_shortcuts_enabled = _cli_multiline_shortcuts_enabled()
    kb.add("enter")(handler)
    if sys.platform != "win32" and not multiline_shortcuts_enabled and not _preserve_ctrl_enter_newline():
        kb.add("c-j")(handler)


def _disable_prompt_toolkit_cpr_warning(app) -> None:
    """Let prompt_toolkit fall back from CPR without printing into the prompt."""
    with suppress(Exception):
        app.renderer.cpr_not_supported_callback = None


def _terminal_may_leak_cpr() -> bool:
    """Suppress prompt_toolkit CPR queries (delayed replies leak into input); Windows keeps pt's default.

    Delayed CPR replies (``ESC[<row>;<col>R`` / visible ``^[[<row>;<col>R``) leak into the status line and
    can freeze input when the reply is slow (#13870 on SSH/slow PTYs). The same race hits local POSIX TTYs
    under heavy subagent / status-line load — see ``tests/hermes_cli/test_cpr_local_leak.py``.
    """
    return os.environ.get("PROMPT_TOOLKIT_NO_CPR", "") == "1" or sys.platform != "win32"


def _build_cpr_disabled_output(stdout):
    """Vt100_Output with ``enable_cpr=False`` (``from_pty()`` doesn't expose it), or None on failure.

    prompt_toolkit's renderer sends ``ESC[6n`` (Device Status Report) to learn the cursor row before
    painting in non-fullscreen mode; the terminal replies ``ESC[<row>;<col>R``. When that reply is delayed
    it races into the display as raw ``^[[39;1R`` and can stall the renderer's pending-CPR future (#13870;
    also local POSIX under heavy subagent load).
    """
    try:
        import io as _io
        from prompt_toolkit.output.vt100 import Vt100_Output, _get_size
        from prompt_toolkit.data_structures import Size

        def _get_term_size():
            rows = columns = None
            try:
                rows, columns = _get_size(stdout.fileno())
            except (OSError, _io.UnsupportedOperation, AttributeError, ValueError):
                pass
            return Size(rows=rows or 24, columns=columns or 80)

        return Vt100_Output(stdout, _get_term_size, enable_cpr=False)
    except Exception:
        return None


def _select_classic_cli_pt_output(stdout):
    """CPR-disabled ``Vt100_Output`` when CPR may leak, else None (Application keeps pt's default)."""
    return _build_cpr_disabled_output(stdout) if _terminal_may_leak_cpr() else None


def _strip_leaked_terminal_responses_with_meta(text: str) -> tuple[str, bool]:
    """Strip leaked CPR replies and mouse-report fragments -> ``(cleaned, had_mouse_reports)``."""
    if not text:
        return text, False

    had_mouse_reports = False
    for present, cpr_re, mouse_re in (
        ("\x1b[" in text, _DSR_CPR_ESC_RE, _SGR_MOUSE_ESC_RE),
        ("^[" in text, _DSR_CPR_VISIBLE_RE, _SGR_MOUSE_VISIBLE_RE),
        ("<" in text and ";" in text and ("M" in text or "m" in text), None, _SGR_MOUSE_BARE_RE),
    ):
        if not present:
            continue
        if cpr_re is not None:
            text = cpr_re.sub("", text)
        text, count = mouse_re.subn("", text)
        had_mouse_reports = had_mouse_reports or count > 0
    return text, had_mouse_reports


def _estimate_tui_input_height(
    lines: list[str] | tuple[str, ...], prompt_text: str, terminal_columns: int, *, max_height: int = 8,
) -> int:
    """Input rows from live terminal cells; the BeforeInput prompt consumes cells only on line 0.

    Never substitute a fake wide fallback: a mis-sized TextArea leaves stale cells at the bottom.
    """
    try:
        from agent.display import display_cwidth as get_cwidth
    except Exception:
        get_cwidth = lambda value: len(value or "")  # type: ignore[assignment]

    columns = max(1, _int_or(terminal_columns or 0, 0))
    prompt_width = max(0, get_cwidth(prompt_text or ""))

    visual_lines = 0
    for index, line in enumerate(lines or [""]):
        display_width = get_cwidth(line or "") + (prompt_width if index == 0 else 0)
        visual_lines += max(1, -(-display_width // columns))

    return min(max(visual_lines, 1), max(1, int(max_height or 1)))


def _status_bar_visible_from_display_config(display_config: object) -> bool:
    """Initial status-bar visibility; both YAML ``off`` (False) and strings like ``"hidden"`` mean off."""
    if not isinstance(display_config, dict):
        display_config = {}
    statusbar_config = display_config.get("statusbar", display_config.get("tui_statusbar", "top"))
    if isinstance(statusbar_config, str):
        return statusbar_config.strip().lower() not in {"0", "false", "hidden", "no", "off"}
    return statusbar_config is not False


def _collect_query_images(query: str | None, image_arg: str | None = None) -> tuple[str, list[Path]]:
    """Collect local image attachments for single-query CLI flows."""
    message = query or ""
    images: list[Path] = []

    if isinstance(message, str):
        dropped = _detect_file_drop(message)
        if dropped and dropped.get("is_image"):
            images.append(dropped["path"])
            message = dropped["remainder"] or f"[User attached image: {dropped['path'].name}]"

    if image_arg:
        explicit_path = _resolve_attachment_path(image_arg)
        if explicit_path is None:
            raise ValueError(f"Image file not found: {image_arg}")
        if explicit_path.suffix.lower() not in _IMAGE_EXTENSIONS:
            raise ValueError(f"Not a supported image file: {explicit_path}")
        images.append(explicit_path)

    return message, list(dict.fromkeys(images))


# OSC sequences (e.g. OSC-8 links): pt's ANSI parser strips the ESC but leaks the payload as text.
_OSC_ESCAPE_RE = re.compile(r"\x1b\][\s\S]*?(?:\x07|\x1b\\)")


class ChatConsole:
    """Rich Console drop-in routing rendered ANSI through ``_cprint`` so colors survive patch_stdout."""

    def __init__(self):
        from io import StringIO
        self._buffer = StringIO()
        self._inner = Console(file=self._buffer, force_terminal=True, color_system="truecolor", highlight=False)

    def print(self, *args, **kwargs):
        self._buffer.seek(0)
        self._buffer.truncate()
        self._inner.width = shutil.get_terminal_size((80, 24)).columns
        self._inner.print(*args, **kwargs)
        for line in _OSC_ESCAPE_RE.sub("", self._buffer.getvalue()).rstrip("\n").split("\n"):
            _cprint(line)

    @contextmanager
    def status(self, *_args, **_kwargs):
        """No-op ``console.status`` so slash helpers don't duplicate ``_busy_command()``'s indicator."""
        yield self



def _build_compact_banner() -> str:
    """Build a compact banner that fits the current terminal width."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        _skin = get_active_skin()
    except Exception:
        _skin = None

    def _color(key, default):
        return _skin.get_color(key, default) if _skin else default

    border_color = _color("banner_border", "#FFD700")
    title_color = _color("banner_title", "#FFBF00")
    dim_color = _color("banner_dim", "#B8860B")

    if (getattr(_skin, "name", "default") if _skin else "default") == "default":
        tiny_line = "☤ NOUS HERMES"
    else:
        tiny_line = _skin.get_branding("agent_name", "Hermes Agent") if _skin else "Hermes Agent"
    line1 = f"{tiny_line} - AI Agent Framework"

    if os.environ.get("HERMES_FAST_STARTUP_BANNER") == "1":
        from hermes_cli import __release_date__ as _release_date
        from hermes_cli import __version__ as _version

        version_line = f"Hermes Agent v{_version} ({_release_date})"
    else:
        version_line = format_banner_version_label()

    w = min(shutil.get_terminal_size().columns - 2, 88)
    if w < 30:
        return f"\n[{title_color}]{tiny_line}[/] [dim {dim_color}]- Nous Research[/]\n"

    inner = w - 2  # inside the box border
    bar = "═" * w
    content_width = inner - 2

    line1 = line1[:content_width].ljust(content_width)
    line2 = version_line[:content_width].ljust(content_width)

    return (
        f"\n[bold {border_color}]╔{bar}╗[/]\n"
        f"[bold {border_color}]║[/] [{title_color}]{line1}[/] [bold {border_color}]║[/]\n"
        f"[bold {border_color}]║[/] [dim {dim_color}]{line2}[/] [bold {border_color}]║[/]\n"
        f"[bold {border_color}]╚{bar}╝[/]\n"
    )


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
        config_path.parent.mkdir(parents=True, exist_ok=True)
        from utils import atomic_roundtrip_yaml_update
        atomic_roundtrip_yaml_update(config_path, key_path, value)
        try:  # owner-only: config files contain API keys
            os.chmod(config_path, 0o600)
        except (OSError, NotImplementedError):
            pass
        # Same unpinned-cron notice as `hermes config set` for every model switch.
        from hermes_cli.config import warn_unpinned_cron_jobs_after_model_config_change

        warn_unpinned_cron_jobs_after_model_config_change(key_path, value)
        return True
    except Exception as e:
        logger.error("Failed to save config: %s", e)
        return False


def _persist_global_model_switch(result) -> None:
    """Persist a ``/model --global`` switch to config.yaml, reconciling the
    endpoint credential fields when the provider changed.

    Writing only ``model.default`` + ``model.provider`` (the historical
    behavior) leaves the PREVIOUS provider's ``model.base_url`` /
    ``model.api_key`` behind. Switching e.g. exo → anthropic then left an
    anthropic provider pointed at the exo ``base_url`` with a dummy key —
    the main model still worked via OAuth (which hardcodes Anthropic's URL),
    but auxiliary tasks honor the literal base_url and 404'd against the exo
    box ("No instance found for model claude-haiku-4-5..."). This mirrors the
    ``_model_flow_anthropic`` path in model_setup_flows.py, which already
    clears these fields on a provider switch.

    When the new provider supplies an explicit endpoint (switch TO a custom
    endpoint), we persist those values; otherwise we blank them so built-in
    providers resolve credentials from OAuth / env / the credential pool.
    """
    save_config_value("model.default", result.new_model)
    if getattr(result, "provider_changed", False):
        save_config_value("model.provider", result.target_provider)
        # Reconcile inline endpoint fields so the new provider doesn't inherit
        # the previous provider's base_url/api_key/api_mode.
        save_config_value("model.base_url", getattr(result, "base_url", None) or None)
        save_config_value("model.api_key", getattr(result, "api_key", None) or None)
        save_config_value("model.api_mode", getattr(result, "api_mode", None) or None)
    else:
        # base_url/api_mode are always freshly resolved for the target model
        # (see model_switch.py), so sync them even without a provider change;
        # None clears a value the new model doesn't need (#25106).
        save_config_value("model.base_url", getattr(result, "base_url", None) or None)
        save_config_value("model.api_mode", getattr(result, "api_mode", None) or None)


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


def _panel_box_width(title: str, content_lines: list[str], min_width: int = 46, max_width: int = 76) -> int:
    """Stable TUI panel width wide enough for the title and content (incl. borders)."""
    term_cols = shutil.get_terminal_size((100, 20)).columns
    longest = max([len(title)] + [len(line) for line in content_lines] + [min_width - 4])
    inner = min(max(longest + 4, min_width - 2), max_width - 2, max(24, term_cols - 6))
    return inner + 2  # leading/trailing space inside the borders


def _wrap_panel_text(text: str, width: int, subsequent_indent: str = "", *, keep_ws: bool = False) -> list[str]:
    """Wrap panel text; ``keep_ws`` preserves whitespace (command/detail previews)."""
    kw = dict(replace_whitespace=False, drop_whitespace=False) if keep_ws else dict(break_long_words=False, break_on_hyphens=False)
    wrapped = textwrap.wrap(text, width=max(8, width), subsequent_indent=subsequent_indent, **kw)
    return wrapped or [""]


_wrap_panel_text_keep_ws = functools.partial(_wrap_panel_text, keep_ws=True)


def _append_panel_line(lines, border_style: str, content_style: str, text: str, box_width: int) -> None:
    lines.extend(((border_style, "│ "), (content_style, text.ljust(max(0, box_width - 2))), (border_style, " │\n")))


def _append_blank_panel_line(lines, border_style: str, box_width: int) -> None:
    lines.append((border_style, "│" + (" " * box_width) + "│\n"))


@dataclass
class _ChatTurn:
    """Per-turn state shared by the ``chat()`` phases and the agent worker thread.

    ``result`` is written by the worker and read after the join; ``tts_normal_exit`` is
    set only when the TTS worker drained on its own so the last sentence is never cut.
    """

    result: Optional[dict] = None
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


class HermesCLI(CLIProcessNotificationsMixin, CLIAgentSetupMixin, CLICommandsMixin, CLIBillingMixin, CLITuiMixin, CLIStatusBarMixin, CLIVoiceMixin, CLIModelSwitchMixin, CLISessionMixin, CLIStreamMixin, CLIModalMixin, CLITerminalMixin, CLIInfoMixin, CLILoopsMixin, CLIChatTurnMixin):
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

    def _init_display_options(self, verbose, compact):
        """Display-related config: compact/tool-progress/focus view, bells, streaming, previews, stream buffers."""
        self.console = Console()
        self.config = CLI_CONFIG
        display = CLI_CONFIG["display"]
        self.compact = compact if compact is not None else display.get("compact", False)
        # tool_progress: "off" | "new" | "all" | "verbose"; YAML 1.1 parses bare `off` as False.
        _raw_tp = display.get("tool_progress", "all")
        self.tool_progress_mode = "off" if _raw_tp is False else str(_raw_tp)
        # focus_view (/focus) is display-only: snaps tool_progress to "off" (stashing the
        # pre-focus mode for /focus off); never changes what is sent to the model.
        self._focus_view_enabled = bool(display.get("focus_view", False))
        self._focus_saved_tool_progress = self._focus_last_counted_tool = None
        self._focus_hidden_lines = 0
        if self._focus_view_enabled:
            from hermes_cli.focus_view import FOCUS_TOOL_PROGRESS_MODE, normalize_tool_progress_mode

            self._focus_saved_tool_progress = normalize_tool_progress_mode(self.tool_progress_mode)
            self.tool_progress_mode = FOCUS_TOOL_PROGRESS_MODE
        self.resume_display = display.get("resume_display", "full")  # "full" | "minimal"
        self.bell_on_complete = display.get("bell_on_complete", False)
        self.bell_on_prompt = display.get("bell_on_prompt", False)  # bell when a blocking modal opens
        self.show_reasoning = display.get("show_reasoning", True)
        self.reasoning_full = display.get("reasoning_full", False)
        _configure_output_history(
            enabled=display.get("persistent_output", True),
            max_lines=display.get("persistent_output_max_lines", 200),
        )
        # busy_input_mode: "interrupt" (redirect the run) | "queue" (next turn) | "steer" (inject mid-run).
        _bim = str(display.get("busy_input_mode", "interrupt")).strip().lower()
        self.busy_input_mode = _bim if _bim in ("queue", "steer") else "interrupt"

        # verbose ONLY controls global DEBUG logging; tool_progress="verbose" is independent
        # (coupling them spewed every module's DEBUG logs to the console).
        self.verbose = bool(verbose) if verbose is not None else False

        self.streaming_enabled = display.get("streaming", False)
        self.show_timestamps = display.get("timestamps", False)
        self.timestamp_format = display.get("timestamp_format", "%H:%M")
        _frm = str(display.get("final_response_markdown", "strip")).strip().lower()
        self.final_response_markdown = _frm if _frm in {"render", "strip", "raw"} else "strip"

        self._inline_diffs_enabled = display.get("inline_diffs", True)

        # Per-turn accounting: CLI-only chrome riding the tool-progress feed.
        self._turn_summary_enabled = bool(display.get("turn_summary", True))
        self._spinner_token_flow_enabled = bool(display.get("spinner_token_flow", True))
        self._turn_summary_collector = None
        self._turn_summary_start = 0.0
        self._turn_token_baseline = 0
        self._interactive_turn = False  # only run()-loop turns; keeps the summary line off -Q

        _ump = display.get("user_message_preview", {})
        _ump = _ump if isinstance(_ump, dict) else {}
        self.user_message_preview_first_lines = max(1, _int_or(_ump.get("first_lines", 2), 2))
        self.user_message_preview_last_lines = max(0, _int_or(_ump.get("last_lines", 2), 2))

        # Streaming display state
        self._stream_buf = ""  # partial line buffer
        self._reasoning_preview_buf = ""  # coalesces tiny reasoning chunks
        self._stream_started = self._stream_box_opened = self._stream_box_live = False
        self._held_status_lines: list[str] = []  # agent status lines parked while a box streams
        # FORK: messages queued while the response box is open (e.g. "Queued for the next
        # turn") are drained by _flush_stream() AFTER the box closes so they do not
        # interleave with streamed text. Lock: producer is the UI thread, consumer the
        # agent thread. _stream_drained latches once _flush_stream has drained them.
        self._stream_drained = False
        self._post_stream_messages: list[str] = []
        self._post_stream_lock = threading.Lock()
        # Possible markdown-table lines held until the block ends for wcwidth-aware re-padding.
        self._stream_table_buf: list[str] = []
        self._in_stream_table = False
        self._pending_edit_snapshots = {}
        self._last_input_mode_recovery = self._last_termios_drift_check = 0.0
        self._input_mode_recovery_notice_shown = self._termios_drift_notice_shown = False

    def _init_model_routing(self, model, toolsets, provider, reasoning, api_key, base_url, max_turns, run_budget, checkpoints, pass_session_id, ignore_rules):
        """Resolve model/provider/base_url, turn limits, toolsets, checkpoints, prompt/personality, reasoning + routing config."""
        self._init_model_and_provider(model, provider, api_key, base_url)
        self._init_turn_limits(max_turns, run_budget)
        self._init_toolsets(toolsets)
        self._init_checkpoints_and_rules(checkpoints, pass_session_id, ignore_rules)
        self._init_prompt_and_reasoning(reasoning)

    def _init_model_and_provider(self, model, provider, api_key, base_url):
        """Priority: CLI args > env vars > config file."""
        # LLM_MODEL/OPENAI_MODEL env vars are deliberately NOT checked (multi-agent setups
        # would stomp each other through the environment).
        _model_config = CLI_CONFIG["model"]
        # A dict-valued default carries its own provider, which must feed requested_provider
        # instead of being replaced by the merged model.provider (typically "auto").
        _config_model, _nested_provider = _split_model_config_default(
            _model_config.get("default") or _model_config.get("model") or ""
        )
        # resume must not clobber an explicit -m with the session's stored model.
        self._explicit_model_override = bool(model)
        self.model = model or _config_model or ""
        _cfg_provider = _model_config.get("provider") or os.getenv("HERMES_INFERENCE_PROVIDER")
        _startup_provider_override = _startup_base_url_override = _startup_api_key_override = ""
        if self.model:
            from hermes_cli.model_switch import resolve_startup_model_route

            _startup_route = resolve_startup_model_route(
                self.model,
                explicit_provider=provider or "",
                current_provider=(provider or _nested_provider or _cfg_provider or ""),
                user_providers=CLI_CONFIG.get("providers"),
                custom_providers=CLI_CONFIG.get("custom_providers"),
            )
            if _startup_route is not None:
                self.model = _startup_route.model
                _startup_provider_override = _startup_route.provider
                _startup_base_url_override = _startup_route.base_url
                _startup_api_key_override = _startup_route.api_key
        # ``moa:<preset>`` selects the MoA virtual provider before provider resolution so the
        # real provider never sees the unknown model; the prefix wins over --provider.
        # A ``moa:<preset>`` model string selects the MoA virtual provider in one shot (parity with
        # interactive ``/moa`` and the model picker). See #56828.
        _moa_provider_override, self.model = _normalize_moa_model(self.model)

        if self.model == "":  # auto-detect from a local server
            _base_url = _model_config.get("base_url") or ""
            if base_url_hostname(_base_url) in ("localhost", "127.0.0.1"):
                from hermes_cli.runtime_provider import _auto_detect_local_model
                self.model = _auto_detect_local_model(_base_url) or self.model
        # Provider normalisation may silently override the default but must warn for an
        # explicit choice (a config model equal to the global fallback is NOT explicit).
        self._model_is_default = not model and not _config_model

        # --api-key wins; otherwise a URL-bearing startup alias carries its own credential.
        # See #28660.
        self._explicit_api_key = api_key or _startup_api_key_override or None
        self._explicit_base_url = base_url

        # Resolved lazily at use-time via _ensure_runtime_credentials().
        self.requested_provider = (
            _moa_provider_override or provider or _startup_provider_override or _nested_provider
            or _cfg_provider or "auto"
        )
        # `--provider <custom>` without `-m` uses that entry's default_model, else the global
        # default goes to the custom endpoint and the compressor gets the wrong context length.
        # Explicit `-m` still wins. See #86978.
        if not model and provider:
            try:
                from hermes_cli.runtime_provider import _get_named_custom_provider

                _named_custom = _get_named_custom_provider(provider)
            except Exception as exc:
                logger.warning(
                    "Could not resolve --provider %s default model; keeping global model.default (%s)",
                    provider, exc,
                )
                _named_custom = None
            _provider_default = str((_named_custom or {}).get("model") or "").strip()
            if _provider_default:
                self.model = _provider_default
                self._model_is_default = False
        self._provider_source: Optional[str] = None
        self.provider = self.requested_provider
        self.api_mode = "chat_completions"
        self.acp_command: Optional[str] = None
        self.acp_args: list[str] = []
        self.base_url = (
            base_url or _startup_base_url_override or _model_config.get("base_url", "")
            or os.getenv("OPENROUTER_BASE_URL", "")
        ) or None
        # Key matches the resolved base_url; re-resolved by _ensure_runtime_credentials().
        _keys = ("OPENROUTER_API_KEY", "OPENAI_API_KEY")
        if not (self.base_url and base_url_host_matches(self.base_url, "openrouter.ai")):
            _keys = _keys[::-1]
        self.api_key = api_key or os.getenv(_keys[0]) or os.getenv(_keys[1])

    def _init_turn_limits(self, max_turns, run_budget):
        """max_turns: CLI arg > config > env var > default; run budget: CLI flag > config."""
        # resolve_turn_limit() accepts "none"/"unlimited" (-> sys.maxsize) alongside ints.
        # KEEP the root-level CLI_CONFIG["max_turns"] fallback: it is never migrated on disk
        # and other config paths may bypass the load-time fold.
        from hermes_cli.config import resolve_turn_limit as _resolve_turn_limit
        self.max_turns = _resolve_turn_limit(next(
            (v for v in (max_turns, CLI_CONFIG["agent"].get("max_turns"), CLI_CONFIG.get("max_turns")) if v is not None),
            os.getenv("HERMES_MAX_ITERATIONS"),
        ))
        self.run_budget_seconds = run_budget if run_budget is not None else CLI_CONFIG["agent"].get("run_budget_seconds")

    def _init_toolsets(self, toolsets):
        self.enabled_toolsets = toolsets
        from agent.skill_utils import parse_config_string_list

        self.disabled_toolsets = parse_config_string_list(CLI_CONFIG["agent"].get("disabled_toolsets"))

        if toolsets and "all" not in toolsets and "*" not in toolsets:
            # Plugin-registered toolsets (e.g. the bundled `a2a` platform
            # plugin's client tools) only become known to validate_toolset()
            # after discover_plugins() has run. Plugin discovery normally
            # happens lazily as a side effect of importing model_tools.py,
            # which may not have happened yet at this point in startup —
            # causing a false-positive "Unknown toolsets" warning for valid
            # plugin toolsets. Ensure discovery has run before validating.
            try:
                from hermes_cli.plugins import discover_plugins

                discover_plugins()
            except Exception:
                logger.debug("Plugin discovery failed before toolset validation", exc_info=True)
            # MCP server names only resolve after discover_mcp_tools runs; skip them here.
            mcp_names = set((CLI_CONFIG.get("mcp_servers") or {}).keys())
            invalid = [t for t in toolsets if not validate_toolset(t) and t not in mcp_names]
            if invalid:
                self._console_print(f"[bold red]Warning: Unknown toolsets: {', '.join(invalid)}[/]")

    def _init_checkpoints_and_rules(self, checkpoints, pass_session_id, ignore_rules):
        cp_cfg = CLI_CONFIG.get("checkpoints", {})
        if isinstance(cp_cfg, bool):
            cp_cfg = {"enabled": cp_cfg}
        self.checkpoints_enabled = checkpoints or cp_cfg.get("enabled", False)
        self.checkpoint_max_snapshots = cp_cfg.get("max_snapshots", 20)
        self.checkpoint_max_total_size_mb = cp_cfg.get("max_total_size_mb", 500)
        self.checkpoint_max_file_size_mb = cp_cfg.get("max_file_size_mb", 10)
        self.pass_session_id = pass_session_id
        # --ignore-rules: AIAgent skips context files (AGENTS.md/SOUL.md/...) and memory.
        self.ignore_rules = ignore_rules or os.environ.get("HERMES_IGNORE_RULES") == "1"

    def _init_prompt_and_reasoning(self, reasoning):
        """Ephemeral system prompt/prefill, reasoning + service tier, OpenRouter routing knobs, fallback chain."""
        # Env var wins, then hermes_cli.personality (single owner of overlay resolution).
        from hermes_cli.personality import available_personalities, resolve_ephemeral_system_prompt

        self.system_prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "") or resolve_ephemeral_system_prompt(CLI_CONFIG)
        self.personalities = available_personalities(CLI_CONFIG)

        # Ephemeral prefill messages (few-shot priming, never persisted)
        self.prefill_messages = _load_prefill_messages(_resolve_prefill_messages_file(CLI_CONFIG))

        # Reasoning config. Upstream resolves through the shared hermes_constants chokepoint
        # (Closes #21256), which reads agent.reasoning_overrides. The fork additionally keeps
        # its own per-model map (agent.reasoning_effort_by_model) because /reasoning --global
        # and _apply_reasoning_for_new_model read/write it — when that map is populated it
        # wins; otherwise resolution goes through upstream's chokepoint unchanged.
        from hermes_constants import resolve_reasoning_config
        self._reasoning_effort_by_model: dict = (
            CLI_CONFIG["agent"].get("reasoning_effort_by_model", {}) or {}
        )
        if self._reasoning_effort_by_model:
            self.reasoning_config = _resolve_reasoning_for_model(
                self.model or "",
                self._reasoning_effort_by_model,
                CLI_CONFIG["agent"].get("reasoning_effort", ""),
            )
        else:
            self.reasoning_config = resolve_reasoning_config(CLI_CONFIG, self.model)
        # --reasoning wins for this run only (never persisted); unparseable -> warn and ignore.
        if reasoning is not None and str(reasoning).strip():
            _cli_reasoning = _parse_reasoning_config(reasoning)
            if _cli_reasoning is None:
                logger.warning("Unknown --reasoning '%s', keeping the configured level", reasoning)
            else:
                self.reasoning_config = _cli_reasoning
        self.service_tier = _parse_service_tier_config(CLI_CONFIG["agent"].get("service_tier", ""))
        # FORK: one tool call per turn so the model emits a fresh <think> block before each
        # tool. See AIAgent.__init__ for the full rationale.
        self.interleaved_thinking = bool(CLI_CONFIG["agent"].get("interleaved_thinking", False))

        # OpenRouter provider routing preferences
        pr = CLI_CONFIG.get("provider_routing", {}) or {}
        self._provider_sort = pr.get("sort")
        self._providers_only = pr.get("only")
        self._providers_ignore = pr.get("ignore")
        self._providers_order = pr.get("order")
        self._provider_require_params = pr.get("require_parameters", False)
        self._provider_data_collection = pr.get("data_collection")

        # OpenRouter Pareto Code router coding-score floor; out-of-range = unset.
        _raw_score = (CLI_CONFIG.get("openrouter", {}) or {}).get("min_coding_score")
        self._openrouter_min_coding_score: Optional[float] = None
        if _raw_score not in {None, ""}:
            try:
                _f = float(_raw_score)
                if 0.0 <= _f <= 1.0:
                    self._openrouter_min_coding_score = _f
            except (TypeError, ValueError):
                pass

        self._fallback_model = get_fallback_chain(CLI_CONFIG)

    def _init_runtime_state(self, resume):
        """Session store + all per-run mutable state (queues, overlays, pet/voice/status-bar fields)."""
        # A signature change across turns (/model, credential rotation) rebuilds the agent.
        self._active_agent_route_signature = None
        self.agent: Optional[Any] = None  # initialized on first use
        self._tool_callbacks_installed = self._tirith_security_checked = False
        self._app = None  # prompt_toolkit Application (set in run())

        self.conversation_history: List[Dict[str, Any]] = []
        self.session_start = datetime.now()
        # Per-prompt elapsed timer shown in the status bar.
        self._prompt_start_time: Optional[float] = None
        self._prompt_duration: float = 0.0
        self._last_turn_finished_at: Optional[float] = None
        # FORK: context_tokens at the start of the current turn, so the status bar can show a
        # signed per-turn delta (new content vs a post-idle cache refresh). None until the
        # first turn establishes a baseline.
        self._turn_start_context_tokens: Optional[int] = None
        self._init_session_store()
        self._pending_title: Optional[str] = None
        self._resumed = bool(resume)
        self.session_id = resume or new_session_id(self.session_start)
        getattr(self, "_write_terminal_breadcrumb", lambda: None)()

        self._history_file = _hermes_home / ".hermes_history"
        self._last_invalidate: float = 0.0  # throttles UI repaints
        self._init_ui_state()

    def _init_session_store(self):
        """Open the session store early (so /title works before the first message) + opportunistic maintenance."""
        self._session_db = None
        self._session_db_unavailable = False
        try:
            # Registry handle, not a bare SessionDB(): goals/loops/heartbeat acquire the same
            # path a moment later from the REPL thread, and a second writer repeats the full
            # open (the /proc-wide deleted-WAL scan, ~4k readlinks) while the render thread
            # holds the GIL — that repeat was the post-banner freeze before the first prompt.
            from hermes_state_registry import acquire
            self._session_db = acquire()
        except Exception as e:
            # Without a store the transcript is NOT persisted while the chat looks healthy,
            # so surface it prominently rather than only logging.
            # #41386: a failed session store means the transcript is NOT persisted to state.db — the live
            # chat looks healthy but resume later shows a truncated/empty session. A buried log line is not
            # enough; surface it prominently so the user knows persistence is off for this run and can fix
            # the store before relying on resume.
            self._session_db_unavailable = True
            logger.warning("Failed to initialize SessionDB — session will NOT be indexed for search: %s", e)
            try:
                Console(stderr=True).print(
                    "[bold yellow]⚠ Session store unavailable[/bold yellow] — "
                    "this conversation will [bold]NOT be saved[/bold] to disk and "
                    "cannot be resumed later. Searching past sessions is also disabled.\n"
                    f"  Reason: {e}\n"
                    "  Fix the state.db store (e.g. `hermes update` to rebuild the venv) to restore persistence."
                )
            except Exception:
                print(
                    "WARNING: Session store unavailable — this conversation will NOT be "
                    f"saved to disk and cannot be resumed later. Reason: {e}"
                )
        _run_state_db_auto_maintenance(self._session_db)
        _run_checkpoint_auto_maintenance()

    def _init_ui_state(self):
        """Per-run mutable UI state; must exist before any chat() call since -q never goes through run()."""
        self._pending_input = queue.Queue()
        self._interrupt_queue = queue.Queue()
        self._agent_running = self._should_exit = False
        self._last_turn_interrupted = False  # /goal never auto-queues on a Ctrl+C'd turn
        self._terminal_io_broken = False  # stdout EIO: freeze UI paints instead of spinning
        self._delete_session_on_exit = False  # /exit --delete
        # /update: relaunch() runs from run() after prompt_toolkit restored terminal modes.
        # /exit --delete: when True, the current session's SQLite history and on-disk transcripts are
        # deleted during shutdown. Set by process_command() when the user runs /exit --delete or /quit
        # --delete. Ported from google-gemini/gemini-cli#19332.
        self._pending_relaunch: list[str] | None = None
        self._last_ctrl_c_time = 0
        # Blocking-prompt overlays (clarify / sudo / approval / slash-confirm / model picker).
        self._clarify_state = self._clarify_multi_base = None
        self._clarify_freetext = False
        self._clarify_prefill = ""
        self._sudo_state = self._modal_input_snapshot = self._approval_state = None
        self._slash_confirm_state = self._model_picker_state = None
        self._clarify_deadline = self._sudo_deadline = self._approval_deadline = self._slash_confirm_deadline = 0
        self._approval_lock = threading.Lock()
        # FORK: active /reasoning picker state — same dict-based modal pattern as the /model
        # picker and upstream's Ctrl+P command palette. None when the picker is closed.
        self._reasoning_picker_state: dict | None = None
        try:  # composer placeholder chosen once so it stays stable on screen
            from hermes_cli.tips import get_random_composer_placeholder
            self._composer_placeholder = get_random_composer_placeholder()
        except Exception:
            self._composer_placeholder = ""
        self._command_palette_state = self._secret_state = None
        self._pending_resume_sessions = None  # armed by a bare `/resume`; the next bare number selects
        self._pending_agent_seed = None  # one-shot seed from a slash handler
        self._secret_deadline = 0
        self._tool_start_time: float = 0.0
        self._pending_tool_info: dict = {}  # function_name -> [(preview, args)] for stacked scrollback
        self._spinner_text = self._command_status = ""
        self._last_scrollback_tool: str = ""  # "new" mode dedup
        self._command_running = self._command_blocks_input = False
        # Petdex mascot (display.pet): kitty placeholders on kitty/Ghostty, half-blocks elsewhere.
        self._pet_renderer = self._pet_anim_thread = None
        self._pet_slug = self._pet_kitty_pending = ""
        self._pet_enabled = self._pet_anim_running = False
        self._pet_cols: int = 18
        self._pet_scale: float = 0.7
        self._pet_frames_cache: dict = {}
        self._pet_kitty_cache: dict = {}
        self._pet_kitty_image_id = self._pet_frame_idx = 0
        self._pet_lock = threading.Lock()
        self._pet_cfg_checked = self._pet_event_until = 0.0
        self._pet_event: str = ""
        self._pet_reasoning = self._pet_turn_error = False
        self._attached_images: list[Path] = []
        self._image_counter = 0
        # Ctrl+S prompt stash; in-memory only because drafts routinely contain secrets.
        from hermes_cli.prompt_stash import PromptStash as _PromptStash
        self._prompt_stash = _PromptStash()
        self.preloaded_skills: list[str] = []
        self._startup_skills_line_shown = False
        # Background --skills preload, joined by finalize_preloaded_skills before any agent is built.
        self._preload_skills_thread: Optional[threading.Thread] = None
        self._preload_skills_result: Optional[tuple] = None
        self._preload_skills_error: Optional[BaseException] = None
        self._preload_skills_requested: list = []
        self._preload_skills_finalized = False
        self._active_session_lease = None

        # Voice mode state (also reinitialized inside run() for interactive TUI).
        self._voice_lock = threading.Lock()
        self._voice_mode = self._voice_tts = self._voice_recording = False
        self._voice_processing = self._voice_continuous = False
        self._voice_recorder = self._voice_tts_stop = None
        self._voice_tts_done = threading.Event()
        self._voice_tts_done.set()
        self._voice_barge_capture = threading.Event()  # barge monitor is capturing the interruption
        self._voice_last_tts_text = ""  # echo guard
        self._voice_barge_phase = None  # "generation" | "playback"

        self._status_bar_visible = _status_bar_visible_from_display_config(CLI_CONFIG.get("display"))
        self._battery_visible = bool(CLI_CONFIG["display"].get("battery", False))
        # FORK: session-title badge (the yellow right-aligned chip) in the status bar. On by
        # default (upstream behaviour); display.status_bar_session_title: false hides it.
        self._status_bar_session_title_visible = bool(
            CLI_CONFIG["display"].get("status_bar_session_title", True)
        )
        # Vi/vim editing mode for the input composer (display.vim_mode, config-only).
        # Off by default: prompt_toolkit's standard emacs bindings.
        self._vim_mode = bool(CLI_CONFIG["display"].get("vim_mode", False))
        # Hide rules + status bar until the next input after a resize, so SIGWINCH cannot
        # stamp a fresh status bar over one the terminal just reflowed into scrollback.
        self._status_bar_suppressed_after_resize = self._resize_recovery_pending = False
        self._resize_recovery_lock = threading.Lock()
        self._resize_recovery_timer = self._status_bar_unsuppress_timer = None  # latter: debounced un-suppress
        self._last_resize_width = None  # width change (reflow, needs viewport clear) vs rows-only

        self._background_tasks: Dict[str, threading.Thread] = {}
        self._background_task_counter = 0

        # Cache-hit baseline, reset on model switch / compression so the bar shows the current regime.
        self._cache_hit_baseline_prompt = self._cache_hit_baseline_read = self._cache_hit_baseline_compressions = 0
        self._cache_hit_baseline_model: Optional[str] = None

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

    def _get_status_bar_snapshot(self) -> Dict[str, Any]:
        # Prefer the agent's model name — it updates on fallback.
        # self.model reflects the originally configured model and never
        # changes mid-session, so the TUI would show a stale name after
        # _try_activate_fallback() switches provider/model.
        agent = getattr(self, "agent", None)
        model_name = (getattr(agent, "model", None) or self.model or "unknown")
        # Friendly display: prefer reverse-alias from config.yaml ``model_aliases:``
        # before slash/length truncation. This turns long Palantir RIDs like
        # ``ri.language-model-service..language-model.anthropic-claude-4-7-opus``
        # into the user's chosen short name (e.g. ``opus-4.7``) in the status bar.
        model_short = _reverse_alias_for_display(model_name)
        if model_short == model_name:
            model_short = model_name.split("/")[-1] if "/" in model_name else model_name
            # Strip Palantir RID prefixes via the shared display formatter so
            # this site and ``ModelSwitchResult`` confirmation can't drift.
            from hermes_cli.model_switch import format_model_for_display
            model_short = format_model_for_display(model_short)
        if model_short.endswith(".gguf"):
            model_short = model_short[:-5]
        if len(model_short) > 26:
            model_short = f"{model_short[:23]}..."

        # Failover marker. The DATA above is already correct — model_name is
        # read live off the agent precisely so failover isn't stale — but a
        # silent switch was indistinguishable from a normal run. ``⚠`` is the
        # bar's established degraded marker (see the "⚠ YOLO" badge below).
        #
        # Folded into ``model_short`` itself rather than added as a new
        # segment: the model segment is rendered from ``model_short`` at ~10
        # sites across three width breakpoints plus the fragment builder, and
        # threading a separate field through all of them would mean touching
        # every branch of the width logic. Applied AFTER truncation so the
        # model name keeps its full 26-char budget; the badge costs the same
        # 2 columns as the existing YOLO/steer badges.
        #
        # Compact form ("primary→effective") is deliberately NOT used here:
        # the bar is far tighter than a subagent dock row, and the primary is
        # already recoverable from the fallback_* fields below.
        _fallback_state = {"fallback_active": False, "primary_model": None,
                           "primary_provider": None, "provider": None}
        try:
            from agent.failover_state import resolve_effective_model

            _fallback_state = resolve_effective_model(agent)
            if _fallback_state["fallback_active"]:
                from agent.failover_state import FALLBACK_GLYPH

                model_short = f"{FALLBACK_GLYPH} {model_short}"
        except Exception:
            pass

        elapsed_seconds = max(0.0, (datetime.now() - self.session_start).total_seconds())

        # Effort label for the status bar — pulled from the same source the
        # /reasoning command reads/writes so the bar always reflects the
        # active level. None when reasoning_config is unset (defaults apply).
        rc = getattr(self, "reasoning_config", None)
        if rc is None:
            effort_label = None
        elif rc.get("enabled") is False:
            effort_label = "off"
        else:
            effort_label = rc.get("effort") or None

        snapshot = {
            "model_name": model_name,
            "model_short": model_short,
            # Structured failover state alongside the glyph baked into
            # model_short — a renderer that wants to style the degraded case
            # (or name the primary) shouldn't have to string-match a glyph.
            "provider": _fallback_state["provider"],
            "fallback_active": _fallback_state["fallback_active"],
            "primary_model": _fallback_state["primary_model"],
            "primary_provider": _fallback_state["primary_provider"],
            "effort": effort_label,
            "duration": format_duration_compact(elapsed_seconds),
            "session_title": self._get_status_bar_session_title(),
            "prompt_elapsed": self._format_prompt_elapsed(
                getattr(self, "_prompt_start_time", None),
                getattr(self, "_prompt_duration", 0.0),
                live=getattr(self, "_prompt_start_time", None) is not None,
            ),
            "idle_since": self._format_idle_since(
                getattr(self, "_last_turn_finished_at", None),
                turn_live=getattr(self, "_prompt_start_time", None) is not None,
            ),
            "context_tokens": 0,
            "context_length": None,
            "context_percent": None,
            "session_input_tokens": 0,
            "session_output_tokens": 0,
            "session_cache_read_tokens": 0,
            "session_cache_write_tokens": 0,
            "session_prompt_tokens": 0,
            "session_completion_tokens": 0,
            "session_total_tokens": 0,
            "session_api_calls": 0,
            "compressions": 0,
            "active_background_tasks": 0,
            "active_background_processes": 0,
            "active_background_subagents": 0,
            "battery_label": "",
            "battery_category": "dim",
            # Focus view badge (/focus). Persistent indicator so the reduced
            # output mode is never invisible. Display-only.
            "focus_label": "",
            # Queued /steer note pending delivery on the next tool result.
            "steer_pending": False,
        }

        try:
            from hermes_cli.focus_view import focus_statusbar_segment

            snapshot["focus_label"] = focus_statusbar_segment(
                bool(getattr(self, "_focus_view_enabled", False))
            )
        except Exception:
            pass

        # Battery read-out (first status-bar element when enabled). Reads are
        # memoised for a few seconds inside agent.battery, so polling it on
        # every status-bar repaint is cheap.
        if getattr(self, "_battery_visible", False):
            try:
                from agent.battery import (
                    battery_category,
                    format_battery,
                    read_battery,
                )

                _batt = read_battery()
                snapshot["battery_label"] = format_battery(_batt)
                snapshot["battery_category"] = battery_category(_batt)
            except Exception:
                pass

        # Count live /bg tasks. The dict entry is removed in the
        # task thread's finally block, so len() reflects truly-running tasks.
        # len() on a CPython dict is atomic; safe to read without a lock.
        try:
            bg_tasks = getattr(self, "_background_tasks", None)
            if bg_tasks:
                snapshot["active_background_tasks"] = len(bg_tasks)
        except Exception:
            pass

        # Count live background terminal processes (terminal tool background
        # sessions tracked by tools.process_registry). Cheap O(1) read.
        try:
            from tools.process_registry import process_registry
            snapshot["active_background_processes"] = process_registry.count_running()
        except Exception:
            pass

        # Count live background/async subagents (delegate_task batches and
        # background single delegations tracked by tools.async_delegation).
        # active_task_count() expands a batch to its actual child count (a
        # 3-task fan-out batch contributes 3, not 1) so the ⛓ badge reflects
        # how many subagents are truly working right now, not how many pool
        # slots are occupied. Iterates an in-memory records dict under a
        # lock — cheap and only counts records still running/finalizing.
        try:
            from tools.async_delegation import active_task_count as _async_active_count
            snapshot["active_background_subagents"] = _async_active_count()
        except Exception:
            pass

        # Standing /goal state (Ralph loop). GoalManager is cached on self and
        # keeps its state in memory, so this is a cheap attribute read — no DB
        # hit per repaint. Only an *active* goal earns a segment; paused/done
        # goals stay out of the bar (matching the desktop's active-first row).
        snapshot["goal_active"] = False
        snapshot["goal_turns_used"] = 0
        snapshot["goal_max_turns"] = 0
        try:
            goal_mgr = self._get_goal_manager()
            if goal_mgr is not None and goal_mgr.is_active():
                goal_state = goal_mgr.state
                snapshot["goal_active"] = True
                snapshot["goal_turns_used"] = int(getattr(goal_state, "turns_used", 0) or 0)
                snapshot["goal_max_turns"] = int(getattr(goal_state, "max_turns", 0) or 0)
        except Exception:
            pass


        if not agent:
            return snapshot

        # Queued /steer note — a note injected mid-turn that will land on the
        # next tool result. This can otherwise get lost scrolling past in the
        # confirmation line printed at queue-time, so surface it persistently
        # in the bar (mirrors the YOLO badge convention) until it's drained.
        try:
            _steer_lock = getattr(agent, "_pending_steer_lock", None)
            if _steer_lock is not None:
                with _steer_lock:
                    snapshot["steer_pending"] = bool(getattr(agent, "_pending_steer", None))
            else:
                snapshot["steer_pending"] = bool(getattr(agent, "_pending_steer", None))
        except Exception:
            pass

        snapshot["session_input_tokens"] = getattr(agent, "session_input_tokens", 0) or 0
        snapshot["session_output_tokens"] = getattr(agent, "session_output_tokens", 0) or 0
        snapshot["session_cache_read_tokens"] = getattr(agent, "session_cache_read_tokens", 0) or 0
        snapshot["session_cache_write_tokens"] = getattr(agent, "session_cache_write_tokens", 0) or 0
        snapshot["session_prompt_tokens"] = getattr(agent, "session_prompt_tokens", 0) or 0
        snapshot["session_completion_tokens"] = getattr(agent, "session_completion_tokens", 0) or 0
        snapshot["session_total_tokens"] = getattr(agent, "session_total_tokens", 0) or 0
        snapshot["session_api_calls"] = getattr(agent, "session_api_calls", 0) or 0

        compressor = getattr(agent, "context_compressor", None)
        if compressor:
            # Show the last REAL provider prompt count, not last_prompt_tokens
            # — the latter is ratcheted up to the rough preflight estimate by
            # turn_context.py so preflight compression can fire, which made the
            # bar spike to the (over)estimate mid-turn then snap back to the
            # real number (the phantom "Δ+57K new" balloon). display_prompt_tokens()
            # returns the honest count and clamps the post-compression -1
            # sentinel to 0 for the one transitional turn.
            if hasattr(compressor, "display_prompt_tokens"):
                context_tokens = compressor.display_prompt_tokens()
            else:
                context_tokens = getattr(compressor, "last_prompt_tokens", 0) or 0
            if context_tokens < 0:
                context_tokens = 0
            context_length = getattr(compressor, "context_length", 0) or 0
            if context_length < 0:
                context_length = 0
            snapshot["context_tokens"] = context_tokens
            snapshot["context_length"] = context_length or None
            snapshot["compressions"] = getattr(compressor, "compression_count", 0) or 0
            # Per-turn breakdown so consumers can show ``cached / new``
            # instead of just the sum. A cache flush (tools[] mutation,
            # session resume, etc.) doubles ``context_tokens`` without
            # any new content; surfacing the split prevents misreading
            # that as a real balloon.
            snapshot["context_input_tokens"] = getattr(compressor, "last_input_tokens", 0) or 0
            snapshot["context_cache_read_tokens"] = getattr(compressor, "last_cache_read_tokens", 0) or 0
            snapshot["context_cache_write_tokens"] = getattr(compressor, "last_cache_write_tokens", 0) or 0
            if context_length:
                snapshot["context_percent"] = max(0, min(100, round((context_tokens / context_length) * 100)))

            # Per-turn context delta + cause classification. The user observes
            # the context counter jumping 20K+ on a follow-up; this surfaces
            # *why*. From real session logs there are two mechanisms:
            #   1. New content this turn — a fat tool result (big file read,
            #      web_extract, verbose stdout). Shows up as cache_write
            #      (freshly-written tokens) on top of a flat cached prefix.
            #   2. Post-idle cache refresh — the prompt cache expired during a
            #      gap, so the same prefix re-accounts as full-price input
            #      instead of cache_read. Total is correct; only the
            #      cached/new split changed. cache_write stays small while
            #      input balloons.
            # Heuristic: if the jump is mostly cache_write -> "new"; if it's
            # mostly fresh input with little cache_write -> "cache" (refresh).
            base = getattr(self, "_turn_start_context_tokens", None)
            # Defense in depth: base should never be stored as 0 (see the
            # capture site in the turn-start handler), but guard here too so
            # a 0 baseline can never masquerade as "context_tokens - 0" —
            # i.e. the entire current context reported as this turn's delta.
            if base is not None and base > 0 and context_tokens:
                delta = context_tokens - base
                snapshot["context_delta"] = delta
                # Classify cause for any positive growth, however small — the
                # segment is always shown alongside the other always-on status
                # bar pieces (session tokens, spinner_token_flow's live
                # ``↓ Nk tok`` counter), not gated behind an arbitrary
                # "meaningful" floor. Was previously gated to delta>=2000 to
                # avoid clutter on small turns; user explicitly asked for
                # parity with the always-visible token counters instead.
                if delta > 0:
                    cw = snapshot.get("context_cache_write_tokens", 0) or 0
                    inp = snapshot.get("context_input_tokens", 0) or 0
                    # Cause = the dominant contributor to the *current* prompt:
                    #   - cache refresh: the prefix wasn't cached this turn, so
                    #     it was re-charged as fresh ``input``. Tell-tale is a
                    #     large input share of the total (cache_read collapsed).
                    #     Real logs show ~55% input/total on a cold-cache turn
                    #     vs. ~0% (input==2) on a warm one.
                    #   - new content: prefix stayed cached (input tiny), and
                    #     the growth shows up as freshly-written ``cache_write``.
                    if inp >= context_tokens * 0.40:
                        snapshot["context_delta_cause"] = "cache"
                    elif cw >= delta * 0.5:
                        snapshot["context_delta_cause"] = "new"
                    else:
                        snapshot["context_delta_cause"] = "new"
        # -- Cache-hit ratio (delta since last reset) --
        # Reset baseline on model switch and on compression — both invalidate
        # the prompt cache. Formula verified against live logs:
        #   hit = cache_read / prompt_tokens  (prompt = input+cache_read+cache_write)
        #   see agent/conversation_loop.py:4314  cache=read/prompt (87%)
        #   and CanonicalUsage.prompt_tokens = input+read+write
        try:
            base_model = getattr(self, "_cache_hit_baseline_model", None)
            base_prompt = int(getattr(self, "_cache_hit_baseline_prompt", 0) or 0)
            base_read = int(getattr(self, "_cache_hit_baseline_read", 0) or 0)
            base_comps = int(getattr(self, "_cache_hit_baseline_compressions", 0) or 0)
            cur_model = snapshot.get("model_name") or model_name
            cur_comps = int(snapshot.get("compressions", 0) or 0)
            cur_prompt = int(snapshot.get("session_prompt_tokens", 0) or 0)
            cur_read = int(snapshot.get("session_cache_read_tokens", 0) or 0)
            if base_model is None:
                self._cache_hit_baseline_model = cur_model
                self._cache_hit_baseline_compressions = cur_comps
                base_model = cur_model
                base_comps = cur_comps
            if cur_model != base_model:
                self._cache_hit_baseline_model = cur_model
                self._cache_hit_baseline_prompt = cur_prompt
                self._cache_hit_baseline_read = cur_read
                self._cache_hit_baseline_compressions = cur_comps
                base_prompt = cur_prompt
                base_read = cur_read
                base_comps = cur_comps
            if cur_comps != base_comps:
                self._cache_hit_baseline_compressions = cur_comps
                self._cache_hit_baseline_prompt = cur_prompt
                self._cache_hit_baseline_read = cur_read
                base_prompt = cur_prompt
                base_read = cur_read
            delta_prompt = cur_prompt - base_prompt
            delta_read = cur_read - base_read
            # A zero-read regime hides the segment entirely (no cache data
            # is not the same as a 0% hit worth alarming about), and the pct
            # stays a float so renderers control their own precision.
            if delta_prompt > 0 and delta_read > 0:
                pct = max(0.0, min(100.0, (delta_read / delta_prompt) * 100))
                snapshot["cache_hit_pct"] = pct
                snapshot["cache_hit_label"] = f"{pct:.0f}%"
            elif cur_prompt > 0 and cur_read > 0 and base_prompt == 0 and base_read == 0:
                pct = max(0.0, min(100.0, (cur_read / cur_prompt) * 100))
                snapshot["cache_hit_pct"] = pct
                snapshot["cache_hit_label"] = f"{pct:.0f}%"
            else:
                snapshot["cache_hit_pct"] = None
                snapshot["cache_hit_label"] = ""
        except Exception:
            snapshot["cache_hit_pct"] = None
            snapshot["cache_hit_label"] = ""

        # -- Rolling avg latency / velocity / TTFT (last 10 calls) --
        # Reads the deques maintained in agent/conversation_loop.py (and
        # agent_init). Codex app-server has no latency, so it stays hidden there.
        # `_api_latency_history` is DECODE-ONLY (full duration minus TTFT), so
        # avg_velocity here is true decode throughput; full-wall latency lives
        # in `_api_full_latency_history` and drives avg_latency unchanged.
        try:
            agent_obj = getattr(self, "agent", None)
            lhist = list(getattr(agent_obj, "_api_latency_history", []) or []) if agent_obj else []
            ohist = list(getattr(agent_obj, "_api_output_history", []) or []) if agent_obj else []
            flhist = list(getattr(agent_obj, "_api_full_latency_history", []) or []) if agent_obj else []
            thist = list(getattr(agent_obj, "_api_ttft_history", []) or []) if agent_obj else []
            # Keep the velocity/output histories aligned (appended together).
            n = min(len(lhist), len(ohist))
            if n:
                lhist = lhist[-n:]
                ohist = ohist[-n:]
                # Simple mean for latency (full wall); sum/sum for velocity
                # (true decode throughput, not mean of ratios).
                avg_lat = sum(flhist) / len(flhist) if flhist else None
                total_out = sum(ohist)
                total_lat = sum(lhist)
                avg_vel = (total_out / total_lat) if total_lat > 0 else None
                avg_ttft = (sum(thist) / len(thist)) if thist else None
                # Guard against NaN / inf from weird provider timings (e.g. -0.8s in logs).
                if avg_lat is not None and (avg_lat != avg_lat or avg_lat < 0 or avg_lat > 1e6):
                    avg_lat = None
                if avg_vel is not None and (avg_vel != avg_vel or avg_vel < 0 or avg_vel > 1e6):
                    avg_vel = None
                if avg_ttft is not None and (avg_ttft != avg_ttft or avg_ttft < 0 or avg_ttft > 1e6):
                    avg_ttft = None
                snapshot["avg_latency"] = float(avg_lat) if avg_lat is not None else None
                snapshot["avg_latency_label"] = f"{avg_lat:.1f}s" if avg_lat is not None else ""
                snapshot["avg_velocity"] = float(avg_vel) if avg_vel is not None else None
                snapshot["avg_velocity_label"] = f"{avg_vel:.0f} t/s" if avg_vel is not None else ""
                snapshot["avg_ttft"] = float(avg_ttft) if avg_ttft is not None else None
                snapshot["avg_ttft_label"] = f"{avg_ttft:.1f}s" if avg_ttft is not None else ""
            else:
                snapshot["avg_latency"] = None
                snapshot["avg_latency_label"] = ""
                snapshot["avg_velocity"] = None
                snapshot["avg_velocity_label"] = ""
                snapshot["avg_ttft"] = None
                snapshot["avg_ttft_label"] = ""
        except Exception:
            snapshot["avg_latency"] = None
            snapshot["avg_latency_label"] = ""
            snapshot["avg_velocity"] = None
            snapshot["avg_velocity_label"] = ""
            snapshot["avg_ttft"] = None
            snapshot["avg_ttft_label"] = ""
        return snapshot

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
    def _status_bar_display_width(text: str) -> int:
        """Return terminal cell width for status-bar text.

        len() is not enough for prompt_toolkit layout decisions because some
        glyphs can render wider than one Python codepoint. Keeping the status
        bar within the real display width prevents it from wrapping onto a
        second line and leaving behind duplicate rows.

        Delegates to ``agent.display.display_cwidth()`` rather than calling
        ``get_cwidth`` directly: several registered tool emoji (e.g.
        process's "⚙️") are an emoji base codepoint + VARIATION SELECTOR-16,
        which plain ``get_cwidth`` undercounts by 1 cell. Fed into this
        status bar's wrap-height math (``_spinner_widget_height``), that
        1-cell undercount lands the reserved ``Window`` height 1 row short
        exactly at a wrap boundary — the wrapped continuation then overlaps
        the row below instead of getting its own, producing the recurring
        "garbled/duplicated digit" live-timer corruption (e.g.
        ``process(action="wait")``'s duration rendering as "4m170s" instead
        of "4m17s"). See ``display_cwidth``'s docstring for the full
        analysis of why this specific glyph shape was missed by the two
        earlier ``len()`` vs ``get_cwidth()`` fixes in this same file.
        """
        from agent.display import display_cwidth
        return display_cwidth(text)

    @classmethod
    def _trim_status_bar_text(cls, text: str, max_width: int) -> str:
        """Trim status-bar text to a single terminal row."""
        if max_width <= 0:
            return ""
        from agent.display import display_cwidth

        if cls._status_bar_display_width(text) <= max_width:
            return text

        ellipsis = "..."
        ellipsis_width = cls._status_bar_display_width(ellipsis)
        if max_width <= ellipsis_width:
            return ellipsis[:max_width]

        out = []
        width = 0
        for ch in text:
            ch_width = display_cwidth(ch)
            if width + ch_width + ellipsis_width > max_width:
                break
            out.append(ch)
            width += ch_width
        return "".join(out).rstrip() + ellipsis

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

    def _render_spinner_text(self) -> str:
        """Return the live spinner/status text exactly as rendered in the TUI."""
        txt = getattr(self, "_spinner_text", "")
        if not txt:
            return ""
        flow = self._spinner_token_flow()
        t0 = getattr(self, "_tool_start_time", 0) or 0
        if t0 > 0:
            elapsed = time.monotonic() - t0
            if elapsed >= 60:
                _m, _s = int(elapsed // 60), int(elapsed % 60)
                # Fixed-width timer to avoid status-line wrap jitter while
                # scrolling/repainting (e.g. 1m05s, 12m09s).
                # Minutes are NOT zero-padded — "02m" looks wrong (#user-feedback).
                # Left-pad to the same 6-char width as the <60s branch below
                # so the exact 60s rollover (e.g. "59.9s" -> "1m00s") doesn't
                # itself cause a one-character width jitter — the single-digit
                # minute case ("1m05s", 5 chars) was falling one char short.
                elapsed_str = f"{_m}m{_s:02d}s".rjust(6)
            else:
                # Keep width stable before the 60s rollover as well.
                elapsed_str = f"{elapsed:5.1f}s"
            if flow:
                return f"  {txt}  ({elapsed_str} · {flow})"
            return f"  {txt}  ({elapsed_str})"
        if flow:
            return f"  {txt}  ({flow})"
        return f"  {txt}"

    # ── Per-turn accounting (display.turn_summary / spinner_token_flow) ──
    #
    # Both features are CLI-only chrome. The tally is observed from the
    # tool-progress callback this class already receives on every tool call,
    # so nothing is threaded through the agent loop. Token flow reads the
    # agent's cumulative session counters (bumped per API call in
    # agent/conversation_loop.py) and subtracts a per-turn baseline.

    _PET_FRAME_INTERVAL = 0.16
    _PET_CFG_INTERVAL = 2.5

    def _get_status_bar_field_set(self) -> Optional[frozenset]:
        """Return the set of visible status-bar fields from config.

        Reads ``display.status_bar.fields`` from the module-level
        ``CLI_CONFIG`` (no per-render YAML parse — the status bar repaints
        every frame). Returns ``None`` when the user has not customized the
        bar (use built-in defaults, i.e. show everything), or a
        ``frozenset`` of field names when the list is non-empty.

        Available fields: model, context_detail, context_pct, cache_hit,
        latency, tps, ttft, compressions, bg_tasks, bg_processes, bg_subagents,
        goal, duration, prompt_elapsed, idle_since, focus, yolo, stash,
        battery, title, total_tokens.
        ``total_tokens`` is opt-in only (never shown by default).
        The field order is fixed; the config controls visibility only.
        """
        if hasattr(self, "_status_bar_field_set_cache"):
            return self._status_bar_field_set_cache
        result = None
        try:
            display = CLI_CONFIG.get("display") if isinstance(CLI_CONFIG, dict) else None
            status_bar = (display or {}).get("status_bar") if isinstance(display, dict) else None
            fields = status_bar.get("fields") if isinstance(status_bar, dict) else None
            if isinstance(fields, list) and fields:
                result = frozenset(str(f) for f in fields)
        except Exception:
            result = None
        self._status_bar_field_set_cache = result
        return result

    def _build_status_bar_text(self, width: Optional[int] = None) -> str:
        """Return a compact one-line session status string for the TUI footer."""
        from hermes_cli.banner import _format_context_length  # FORK-kept override
        # Leading status-bar glyph — skin-overridable. Default ``⚕``
        # (caduceus, Hermes branding); skins set ``branding.status_glyph``
        # to swap (e.g. Tanium fork uses ``Ⓣ``).
        try:
            from hermes_cli.skin_engine import get_active_skin
            _glyph = get_active_skin().get_branding("status_glyph", "⚕")
        except Exception:
            _glyph = "⚕"
        try:
            snapshot = self._get_status_bar_snapshot()
            if width is None:
                width = self._get_tui_terminal_width()
            percent = snapshot["context_percent"]
            percent_label = f"{percent}%" if percent is not None else "--"
            duration_label = snapshot["duration"]
            battery_label = snapshot.get("battery_label") or ""
            battery_prefix = f"{battery_label} │ " if battery_label else ""
            focus_label = snapshot.get("focus_label") or ""
            session_title = snapshot.get("session_title") or ""

            yolo_active = self._is_session_yolo_active()
            steer_pending = bool(snapshot.get("steer_pending"))
            goal_segment = self._status_bar_goal_segment(snapshot)
            field_set = self._get_status_bar_field_set()

            def _ok(name: str) -> bool:
                return field_set is None or name in field_set

            if not _ok("title"):
                session_title = ""

            if not _ok("goal"):
                goal_segment = ""
            if not _ok("focus"):
                focus_label = ""
            if width < 52:
                segs = []
                if _ok("model"):
                    segs.append(f"{_glyph} {snapshot['model_short']}")
                if _ok("duration"):
                    segs.append(duration_label)
                if goal_segment:
                    segs.append(goal_segment)
                if focus_label:
                    segs.append(focus_label)
                if steer_pending:
                    segs.append("⏩ steer")
                if yolo_active and _ok("yolo"):
                    segs.append("⚠ YOLO")
                text = battery_prefix + " · ".join(segs) if segs else f"{battery_prefix}{_glyph} {snapshot['model_short']}"
                return self._right_align_status_title(text, session_title, width)
            if width < 76:
                parts = []
                if _ok("model"):
                    parts.append(f"{_glyph} {snapshot['model_short']}")
                if _ok("context_pct"):
                    parts.append(percent_label)
                cache = self._cache_hit_rate(snapshot, precision=0)
                if cache and _ok("cache_hit"):
                    parts.append(cache[1])
                if battery_label:
                    parts.insert(0, battery_label)
                compressions = snapshot.get("compressions", 0)
                if compressions and _ok("compressions"):
                    parts.append(f"🗜️ {compressions}")
                bg_count = snapshot.get("active_background_tasks", 0)
                if bg_count and _ok("bg_tasks"):
                    parts.append(f"▶ {bg_count}")
                bg_proc_count = snapshot.get("active_background_processes", 0)
                if bg_proc_count and _ok("bg_processes"):
                    parts.append(f"⚙ {bg_proc_count}")
                bg_subagent_count = snapshot.get("active_background_subagents", 0)
                if bg_subagent_count and _ok("bg_subagents"):
                    parts.append(f"⛓ {bg_subagent_count}")
                if goal_segment:
                    parts.append(goal_segment)
                if _ok("duration"):
                    parts.append(duration_label)
                if focus_label:
                    parts.append(focus_label)
                if steer_pending:
                    parts.append("⏩ steer")
                if yolo_active and _ok("yolo"):
                    parts.append("⚠ YOLO")
                if not parts:
                    parts = [f"⚕ {snapshot['model_short']}"]
                return self._right_align_status_title(" · ".join(parts), session_title, width)

            parts = []
            if _ok("model"):
                parts.append(f"{_glyph} {snapshot['model_short']}")
            if _ok("context_detail"):
                if snapshot["context_length"]:
                    ctx_total = _format_context_length(snapshot["context_length"])
                    ctx_used = format_token_count_compact(snapshot["context_tokens"])
                    context_label = f"{ctx_used}/{ctx_total}"
                else:
                    context_label = "ctx --"
                parts.append(context_label)
            if _ok("context_pct"):
                parts.append(percent_label)
            if battery_label:
                parts.insert(0, battery_label)
            compressions = snapshot.get("compressions", 0)
            cache = self._cache_hit_rate(snapshot)
            if cache and _ok("cache_hit"):
                parts.append(cache[1])
            _avg_lat = snapshot.get("avg_latency_label") or ""
            if _avg_lat and _ok("latency"):
                parts.append(f"◷ {_avg_lat}")
            _avg_vel = snapshot.get("avg_velocity_label") or ""
            if _avg_vel and _ok("tps"):
                parts.append(f"↑ {_avg_vel}")
            _avg_ttft = snapshot.get("avg_ttft_label") or ""
            if _avg_ttft and _ok("ttft"):
                parts.append(f"⚡ {_avg_ttft} TTFT")
            if compressions and _ok("compressions"):
                parts.append(f"🗜️ {compressions}")
            delta_label = self._format_context_delta(snapshot)
            if delta_label:
                parts.append(delta_label)
            bg_count = snapshot.get("active_background_tasks", 0)
            if bg_count and _ok("bg_tasks"):
                parts.append(f"▶ {bg_count}")
            bg_proc_count = snapshot.get("active_background_processes", 0)
            if bg_proc_count and _ok("bg_processes"):
                parts.append(f"⚙ {bg_proc_count}")
            bg_subagent_count = snapshot.get("active_background_subagents", 0)
            if bg_subagent_count and _ok("bg_subagents"):
                parts.append(f"⛓ {bg_subagent_count}")
            if goal_segment:
                parts.append(goal_segment)
            if _ok("duration"):
                parts.append(duration_label)
            prompt_elapsed = snapshot.get("prompt_elapsed")
            if prompt_elapsed and _ok("prompt_elapsed"):
                parts.append(prompt_elapsed)
            idle_since = snapshot.get("idle_since")
            if idle_since and _ok("idle_since"):
                parts.append(idle_since)
            if focus_label:
                parts.append(focus_label)
            if steer_pending:
                parts.append("⏩ steer")
            if yolo_active and _ok("yolo"):
                parts.append("⚠ YOLO")
            # Session token total (Σ) — opt-in only via an explicit fields
            # list, so default bars never widen.
            total_tokens = snapshot.get("session_total_tokens", 0)
            if total_tokens and field_set is not None and "total_tokens" in field_set:
                parts.append(f"Σ{format_token_count_compact(total_tokens)}")
            if not parts:
                parts = [f"⚕ {snapshot['model_short']}"]
            return self._right_align_status_title(" │ ".join(parts), session_title, width)
        except Exception:
            return f"{_glyph} {self.model if getattr(self, 'model', None) else 'Hermes'}"

    def _get_status_bar_fragments(self):
        from hermes_cli.banner import _format_context_length  # FORK-kept override
        if not self._status_bar_visible or getattr(self, '_model_picker_state', None) or getattr(self, '_command_palette_state', None):
            return []
        try:
            from hermes_cli.skin_engine import get_active_skin
            _glyph = get_active_skin().get_branding("status_glyph", "⚕")
        except Exception:
            _glyph = "⚕"
        _glyph_padded = f" {_glyph} "
        try:
            snapshot = self._get_status_bar_snapshot()
            # Use prompt_toolkit's own terminal width when running inside the
            # TUI — shutil.get_terminal_size() can return stale or fallback
            # values (especially on SSH) that differ from what prompt_toolkit
            # actually renders, causing the fragments to overflow to a second
            # line and produce duplicated status bar rows over long sessions.
            width = self._get_tui_terminal_width()
            duration_label = snapshot["duration"]
            yolo_active = self._is_session_yolo_active()
            steer_pending = bool(snapshot.get("steer_pending"))
            goal_segment = self._status_bar_goal_segment(snapshot)
            battery_label = snapshot.get("battery_label") or ""
            battery_style = self._battery_status_style(snapshot.get("battery_category", "dim"))
            focus_label = snapshot.get("focus_label") or ""
            session_title = snapshot.get("session_title") or ""
            field_set = self._get_status_bar_field_set()

            def _ok(name: str) -> bool:
                return field_set is None or name in field_set

            if not _ok("title"):
                session_title = ""

            if not _ok("goal"):
                goal_segment = ""
            if not _ok("focus"):
                focus_label = ""

            def _append(frag_list, sep, *pieces):
                if frag_list:
                    frag_list.append(("class:status-bar-dim", sep))
                frag_list.extend(pieces)

            effort_label = snapshot.get("effort")
            if width < 52:
                frags = []
                if _ok("model"):
                    frags.append(("class:status-bar", f" {_glyph} "))
                    frags.append(("class:status-bar-strong", snapshot["model_short"]))
                if _ok("duration"):
                    _append(frags, " · ", ("class:status-bar-dim", duration_label))
                if goal_segment:
                    _append(frags, " · ", ("class:status-bar-strong", goal_segment))
                if focus_label:
                    _append(frags, " · ", ("class:status-bar-strong", focus_label))
                if steer_pending:
                    _append(frags, " · ", ("class:status-bar-steer", "⏩ steer"))
                if yolo_active and _ok("yolo"):
                    _append(frags, " · ", ("class:status-bar-yolo", "⚠ YOLO"))
                if not frags:
                    frags = [
                        ("class:status-bar", f" {_glyph} "),
                        ("class:status-bar-strong", snapshot["model_short"]),
                    ]
                frags.append(("class:status-bar", " "))
            else:
                percent = snapshot["context_percent"]
                percent_label = f"{percent}%" if percent is not None else "--"
                if width < 76:
                    compressions = snapshot.get("compressions", 0)
                    bg_count = snapshot.get("active_background_tasks", 0)
                    bg_proc_count = snapshot.get("active_background_processes", 0)
                    bg_subagent_count = snapshot.get("active_background_subagents", 0)
                    frags = []
                    if _ok("model"):
                        frags.append(("class:status-bar", f" {_glyph} "))
                        frags.append(("class:status-bar-strong", snapshot["model_short"]))
                    if effort_label:
                        _append(frags, " · ", ("class:status-bar-dim", effort_label))
                    if _ok("context_pct"):
                        _append(frags, " · ", (self._status_bar_context_style(percent), percent_label))
                    cache = self._cache_hit_rate(snapshot, precision=0)
                    if cache and _ok("cache_hit"):
                        _append(frags, " · ", (self._cache_hit_rate_style(cache[0]), cache[1]))
                    if compressions and _ok("compressions"):
                        _append(frags, " · ", (self._compression_count_style(compressions), f"🗜️ {compressions}"))
                    if bg_count and _ok("bg_tasks"):
                        _append(frags, " · ", ("class:status-bar-strong", f"▶ {bg_count}"))
                    if bg_proc_count and _ok("bg_processes"):
                        _append(frags, " · ", ("class:status-bar-strong", f"⚙ {bg_proc_count}"))
                    if bg_subagent_count and _ok("bg_subagents"):
                        _append(frags, " · ", ("class:status-bar-strong", f"⛓ {bg_subagent_count}"))
                    if goal_segment:
                        _append(frags, " · ", ("class:status-bar-strong", goal_segment))
                    if _ok("duration"):
                        _append(frags, " · ", ("class:status-bar-dim", duration_label))
                    if focus_label:
                        _append(frags, " · ", ("class:status-bar-strong", focus_label))
                    if steer_pending:
                        _append(frags, " · ", ("class:status-bar-steer", "⏩ steer"))
                    if yolo_active and _ok("yolo"):
                        _append(frags, " · ", ("class:status-bar-yolo", "⚠ YOLO"))
                    if not frags:
                        frags = [
                            ("class:status-bar", f" {_glyph} "),
                            ("class:status-bar-strong", snapshot["model_short"]),
                        ]
                    frags.append(("class:status-bar", " "))
                else:
                    bar_style = self._status_bar_context_style(percent)
                    compressions = snapshot.get("compressions", 0)
                    bg_count = snapshot.get("active_background_tasks", 0)
                    bg_proc_count = snapshot.get("active_background_processes", 0)
                    bg_subagent_count = snapshot.get("active_background_subagents", 0)
                    frags = []
                    if _ok("model"):
                        frags.append(("class:status-bar", f" {_glyph} "))
                        frags.append(("class:status-bar-strong", snapshot["model_short"]))
                    if effort_label:
                        _append(frags, " · ", ("class:status-bar-dim", effort_label))
                    if _ok("context_detail"):
                        if snapshot["context_length"]:
                            ctx_total = _format_context_length(snapshot["context_length"])
                            ctx_used = format_token_count_compact(snapshot["context_tokens"])
                            context_label = f"{ctx_used}/{ctx_total}"
                        else:
                            context_label = "ctx --"
                        _append(frags, " │ ", ("class:status-bar-dim", context_label))
                    if _ok("context_pct"):
                        _append(
                            frags,
                            " │ ",
                            (bar_style, self._build_context_bar(percent)),
                            ("class:status-bar-dim", " "),
                            (bar_style, percent_label),
                        )
                    cache = self._cache_hit_rate(snapshot)
                    if cache and _ok("cache_hit"):
                        _append(frags, " │ ", (self._cache_hit_rate_style(cache[0]), cache[1]))
                    _avg_lat = snapshot.get("avg_latency_label") or ""
                    if _avg_lat and _ok("latency"):
                        _append(frags, " │ ", ("class:status-bar-dim", f"◷ {_avg_lat}"))
                    _avg_vel = snapshot.get("avg_velocity_label") or ""
                    if _avg_vel and _ok("tps"):
                        _append(frags, " │ ", ("class:status-bar-dim", f"↑ {_avg_vel}"))
                    _avg_ttft = snapshot.get("avg_ttft_label") or ""
                    if _avg_ttft and _ok("ttft"):
                        _append(frags, " │ ", ("class:status-bar-dim", f"⚡ {_avg_ttft} TTFT"))
                    if compressions and _ok("compressions"):
                        _append(frags, " │ ", (self._compression_count_style(compressions), f"🗜️ {compressions}"))
                    delta_label = self._format_context_delta(snapshot)
                    if delta_label:
                        _append(frags, " │ ", ("class:status-bar-dim", delta_label))
                    if bg_count and _ok("bg_tasks"):
                        _append(frags, " │ ", ("class:status-bar-strong", f"▶ {bg_count}"))
                    if bg_proc_count and _ok("bg_processes"):
                        _append(frags, " │ ", ("class:status-bar-strong", f"⚙ {bg_proc_count}"))
                    if bg_subagent_count and _ok("bg_subagents"):
                        _append(frags, " │ ", ("class:status-bar-strong", f"⛓ {bg_subagent_count}"))
                    if goal_segment:
                        _append(frags, " │ ", ("class:status-bar-strong", goal_segment))
                    if _ok("duration"):
                        _append(frags, " │ ", ("class:status-bar-dim", duration_label))
                    # Position 7: per-prompt elapsed timer (live or frozen)
                    prompt_elapsed = snapshot.get("prompt_elapsed")
                    if prompt_elapsed and _ok("prompt_elapsed"):
                        _append(frags, " │ ", ("class:status-bar-dim", prompt_elapsed))
                    # Position 8: idle time since the last final agent response
                    idle_since = snapshot.get("idle_since")
                    if idle_since and _ok("idle_since"):
                        _append(frags, " │ ", ("class:status-bar-dim", idle_since))
                    # Persistent focus-view badge — so the reduced-output mode
                    # is never invisible (mirrors the YOLO badge convention).
                    if focus_label:
                        _append(frags, " │ ", ("class:status-bar-strong", focus_label))
                    if steer_pending:
                        _append(frags, " │ ", ("class:status-bar-steer", "⏩ steer"))
                    if yolo_active and _ok("yolo"):
                        _append(frags, " │ ", ("class:status-bar-yolo", "⚠ YOLO"))
                    total_tokens = snapshot.get("session_total_tokens", 0)
                    if total_tokens and field_set is not None and "total_tokens" in field_set:
                        _append(frags, " │ ", ("class:status-bar-dim", f"Σ{format_token_count_compact(total_tokens)}"))
                    if not frags:
                        frags = [
                            ("class:status-bar", f" {_glyph} "),
                            ("class:status-bar-strong", snapshot["model_short"]),
                        ]
                    frags.append(("class:status-bar", " "))

            # Stash indicator (📌 N) — appended after all width tiers so the
            # user always knows a parked draft exists, even on narrow
            # terminals.  Placed before the battery prepend so it stays at the
            # right edge, and it is the first thing the width trim below drops
            # if the bar genuinely cannot fit.
            try:
                stash_indicator = self._prompt_stash.indicator()
            except Exception:
                stash_indicator = ""
            if stash_indicator and _ok("stash"):
                # Insert before the trailing pad fragment so the bar keeps its
                # one-cell right margin.
                if frags and frags[-1] == ("class:status-bar", " "):
                    frags[-1:-1] = [
                        ("class:status-bar-dim", " · "),
                        ("class:status-bar-strong", stash_indicator),
                    ]
                else:
                    frags.append(("class:status-bar-dim", " · "))
                    frags.append(("class:status-bar-strong", stash_indicator))

            # Battery is the first status-bar element when enabled: prepend it
            # ahead of the leading ⚕ marker in whichever width tier ran above.
            if battery_label and _ok("battery"):
                frags[0:0] = [
                    ("class:status-bar", " "),
                    (battery_style, battery_label),
                    ("class:status-bar-dim", " │"),
                ]

            frags = self._right_align_status_title_fragments(frags, session_title, width)

            total_width = sum(self._status_bar_display_width(text) for _, text in frags)
            if total_width > width:
                plain_text = "".join(text for _, text in frags)
                trimmed = self._trim_status_bar_text(plain_text, width)
                return [("class:status-bar", trimmed)]
            return frags
        except Exception:
            return [("class:status-bar", f" {self._build_status_bar_text()} ")]

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

    def _stream_reasoning_delta(self, text: str) -> None:
        """Stream reasoning/thinking tokens into a dim box above the response.

        Opens a dim reasoning box on first token, streams line-by-line.
        The box is closed automatically when content tokens start arriving
        (via _stream_delta → _emit_stream_text).

        Once the response box is open, suppress any further reasoning
        rendering — a late thinking block (e.g. after an interrupt) would
        otherwise draw a reasoning box inside the response box.
        """
        if not text:
            return
        self._reasoning_shown_this_turn = True
        if getattr(self, "_stream_box_opened", False):
            return

        # Open reasoning box on first reasoning token
        if not getattr(self, "_reasoning_box_opened", False):
            self._reasoning_box_opened = True
            w = self._scrollback_box_width()
            r_label = " Reasoning "
            r_fill = w - 2 - len(r_label)
            _cprint(f"\n{_DIM}┌─{r_label}{'─' * max(r_fill - 1, 0)}┐{_RST}")

        self._reasoning_buf = getattr(self, "_reasoning_buf", "") + text

        # Emit complete lines, and force-flush long partial lines so
        # reasoning is visible in real-time even without newlines.
        # Indent by _STREAM_PAD so reasoning text sits inside the box frame,
        # matching the response box (which pads content the same way) instead
        # of rendering flush against the left border. Also hard-wrap to the
        # same box width for a symmetric right margin (2026-08-10).
        while "\n" in self._reasoning_buf:
            line, self._reasoning_buf = self._reasoning_buf.split("\n", 1)
            for sub_line in _wrap_stream_line(line):
                _cprint(f"{_STREAM_PAD}{_DIM}{sub_line}{_RST}")
        if len(self._reasoning_buf) > 80:
            for sub_line in _wrap_stream_line(self._reasoning_buf):
                _cprint(f"{_STREAM_PAD}{_DIM}{sub_line}{_RST}")
            self._reasoning_buf = ""

    def _close_reasoning_box(self) -> None:
        """Close the live reasoning box if it's open."""
        if getattr(self, "_reasoning_box_opened", False):
            # Flush remaining reasoning buffer
            buf = getattr(self, "_reasoning_buf", "")
            if buf:
                for sub_line in _wrap_stream_line(buf):
                    _cprint(f"{_STREAM_PAD}{_DIM}{sub_line}{_RST}")
                self._reasoning_buf = ""
            w = self._scrollback_box_width()
            _cprint(f"{_DIM}└{'─' * (w - 2)}┘{_RST}")
            self._reasoning_box_opened = False

            # Reasoning box closed: if no RESPONSE box is live, release any agent
            # status lines _agent_status_print parked (it holds on either box).
            # Guarded, because when a response box is still live the release
            # belongs at ITS footer, in _flush_stream.
            if not getattr(self, "_stream_box_live", False):
                self._release_held_status_lines()

            # Flush any content that was deferred while reasoning was rendering.
            deferred = getattr(self, "_deferred_content", "")
            if deferred:
                self._deferred_content = ""
                self._emit_stream_text(deferred)

    def _stream_delta(self, text) -> None:
        """Line-buffered streaming callback for real-time token rendering.

        Receives text deltas from the agent as tokens arrive. Buffers
        partial lines and emits complete lines via _cprint to work
        reliably with prompt_toolkit's patch_stdout.

        Reasoning/thinking blocks (<REASONING_SCRATCHPAD>, <think>, etc.)
        are suppressed during streaming since they'd display raw XML tags.
        The agent strips them from the final response anyway.

        A ``None`` value signals an intermediate turn boundary (tools are
        about to execute).  Flushes any open boxes and resets state so
        tool feed lines render cleanly between turns.
        """
        if text is None:
            self._flush_stream()
            self._reset_stream_state()
            return
        if not text:
            return

        self._stream_started = True

        # ── Tag-based reasoning suppression ──
        # Track whether we're inside a reasoning/thinking block.
        # These tags are model-generated (system prompt tells the model
        # to use them) and get stripped from final_response. We must
        # suppress them during streaming too — unless show_reasoning is
        # enabled, in which case we route the inner content to the
        # reasoning display box instead of discarding it.
        _OPEN_TAGS = ("<REASONING_SCRATCHPAD>", "<think>", "<reasoning>", "<THINKING>", "<thinking>", "<thought>")
        _CLOSE_TAGS = ("</REASONING_SCRATCHPAD>", "</think>", "</reasoning>", "</THINKING>", "</thinking>", "</thought>")

        # Append to a pre-filter buffer first
        self._stream_prefilt = getattr(self, "_stream_prefilt", "") + text

        # Check if we're entering a reasoning block.
        # Only match tags that appear at a "block boundary": start of the
        # stream, after a newline (with optional whitespace), or when nothing
        # but whitespace has been emitted on the current line.
        # This prevents false positives when models *mention* tags in prose
        # like "(/think not producing <think> tags)".
        #
        # _stream_last_was_newline tracks whether the last character emitted
        # (or the start of the stream) is a line boundary.  It's True at
        # stream start and set True whenever emitted text ends with '\n'.
        if not hasattr(self, "_stream_last_was_newline"):
            self._stream_last_was_newline = True  # start of stream = boundary

        if not getattr(self, "_in_reasoning_block", False):
            # Case-insensitive matching against a lowercased view so
            # mixed-case tag variants (<Think>, <THINKING>, …) are caught.
            prefilt_lower = self._stream_prefilt.lower()
            for tag in _OPEN_TAGS:
                tag_lower = tag.lower()
                search_start = 0
                while True:
                    idx = prefilt_lower.find(tag_lower, search_start)
                    if idx == -1:
                        break
                    # Check if this is a block boundary position
                    preceding = self._stream_prefilt[:idx]
                    if idx == 0:
                        # At buffer start — only a boundary if we're at
                        # a line start (stream start or last emit ended
                        # with newline)
                        is_block_boundary = getattr(self, "_stream_last_was_newline", True)
                    else:
                        # Find last newline in the buffer before the tag
                        last_nl = preceding.rfind("\n")
                        if last_nl == -1:
                            # No newline in buffer — boundary only if
                            # last emit was a newline AND only whitespace
                            # has accumulated before the tag
                            is_block_boundary = (
                                getattr(self, "_stream_last_was_newline", True)
                                and preceding.strip() == ""
                            )
                        else:
                            # Text between last newline and tag must be
                            # whitespace-only
                            is_block_boundary = preceding[last_nl + 1:].strip() == ""
                    if is_block_boundary:
                        # Emit everything before the tag
                        if preceding:
                            self._emit_stream_text(preceding)
                            self._stream_last_was_newline = preceding.endswith("\n")
                        self._in_reasoning_block = True
                        self._stream_prefilt = self._stream_prefilt[idx + len(tag):]
                        break
                    # Not a block boundary — keep searching after this occurrence
                    search_start = idx + 1
                if getattr(self, "_in_reasoning_block", False):
                    break

            # Could also be a partial open tag at the end — hold it back
            if not getattr(self, "_in_reasoning_block", False):
                # Check for partial tag match at the end (case-insensitive)
                safe = self._stream_prefilt
                for tag in _OPEN_TAGS:
                    tag_lower = tag.lower()
                    for i in range(1, len(tag)):
                        if prefilt_lower.endswith(tag_lower[:i]):
                            safe = self._stream_prefilt[:-i]
                            break
                if safe:
                    self._emit_stream_text(safe)
                    self._stream_last_was_newline = safe.endswith("\n")
                    self._stream_prefilt = self._stream_prefilt[len(safe):]
                return

        # Inside a reasoning block — look for close tag.
        # Keep accumulating _stream_prefilt because close tags can arrive
        # split across multiple tokens (e.g. "</REASONING_SCRATCH" + "PAD>...").
        if getattr(self, "_in_reasoning_block", False):
            prefilt_lower = self._stream_prefilt.lower()
            for tag in _CLOSE_TAGS:
                idx = prefilt_lower.find(tag.lower())
                if idx != -1:
                    self._in_reasoning_block = False
                    # When show_reasoning is on, route inner content to
                    # the reasoning display box instead of discarding.
                    if self.show_reasoning:
                        inner = self._stream_prefilt[:idx]
                        if inner:
                            self._stream_reasoning_delta(inner)
                    # Close the reasoning box NOW — content after the close
                    # tag should not be deferred while the box is still open.
                    # _close_reasoning_box() flushes _reasoning_buf, closes
                    # the box frame, and drains _deferred_content so reply
                    # tokens flow straight to _emit_stream_text.
                    self._close_reasoning_box()
                    after = self._stream_prefilt[idx + len(tag):]
                    self._stream_prefilt = ""
                    # Process remaining text after close tag through full
                    # filtering (it could contain another open tag)
                    if after:
                        self._stream_delta(after)
                    return
            # When show_reasoning is on, stream reasoning content live
            # instead of silently accumulating. Keep only the tail that
            # could be a partial close tag prefix.
            max_tag_len = max(len(t) for t in _CLOSE_TAGS)
            if len(self._stream_prefilt) > max_tag_len:
                if self.show_reasoning:
                    # Route the safe prefix to reasoning display
                    safe_reasoning = self._stream_prefilt[:-max_tag_len]
                    self._stream_reasoning_delta(safe_reasoning)
                self._stream_prefilt = self._stream_prefilt[-max_tag_len:]
            return

    def _emit_stream_text(self, text: str) -> None:
        """Emit filtered text to the streaming display."""
        from agent.markdown_tables import is_table_divider, looks_like_table_row
        if not text:
            return

        # The arrival of content text is the signal that reasoning is done.
        # Close the live reasoning box now — this flushes the trailing partial
        # reasoning line, draws the box closer, and drains any deferred
        # content — then fall through to stream this content live.
        #
        # Previously content was buffered into _deferred_content while the
        # reasoning box stayed open, relying on a later </think> close tag (or
        # end-of-stream) to close the box and flush. That works for tag-based
        # reasoning but NOT for providers that stream reasoning as structured
        # reasoning_content (e.g. DeepSeek V4 via exo): there is no close tag,
        # so the box stayed open and every content token was deferred until
        # _flush_stream — the last reasoning line and the entire response then
        # printed together at end-of-stream (the 2nd-to-last-line hang).
        # _stream_reasoning_delta already suppresses any late reasoning once
        # the response box is open, so closing on first content is safe.
        self._close_reasoning_box()

        # Open the response box header on the very first visible text
        if not self._stream_box_opened:
            # Strip leading whitespace/newlines before first visible content
            text = text.lstrip("\n")
            if not text:
                return
            self._stream_box_opened = True
            # Held-status-line gate: _agent_status_print (cli_stream_mixin) parks
            # agent status lines while a box is LIVE and releases them at the
            # footer, so a "✓ [set n · i/N]" from another thread never lands
            # between two paragraphs of the reply. Cleared in _flush_stream.
            self._stream_box_live = True
            try:
                from hermes_cli.skin_engine import get_active_skin
                _skin = get_active_skin()
                label = _skin.get_branding("response_label", "⚕ Hermes")
                _text_hex = _skin.get_color("banner_text", "#FFF8DC")
            except Exception:
                label = "⚕ Hermes"
                _text_hex = "#FFF8DC"
            # Build a true-color ANSI escape for the response text color
            # so streamed content matches the Rich Panel appearance.
            try:
                _r = int(_text_hex[1:3], 16)
                _g = int(_text_hex[3:5], 16)
                _b = int(_text_hex[5:7], 16)
                self._stream_text_ansi = f"\033[38;2;{_r};{_g};{_b}m"
            except (ValueError, IndexError):
                self._stream_text_ansi = ""
            if self.show_timestamps:
                label = f"{label} {datetime.now().strftime(getattr(self, 'timestamp_format', '%H:%M'))}"
            w = self._scrollback_box_width()
            fill = w - 2 - HermesCLI._status_bar_display_width(label)
            _cprint(f"\n{_ACCENT}╭─{label}{'─' * max(fill - 1, 0)}╮{_RST}")

        self._stream_buf += text

        # Emit complete lines, keep partial remainder in buffer
        _tc = getattr(self, "_stream_text_ansi", "")

        def _emit_one(printed_line: str, wrap: bool = True) -> None:
            # Safety net: strip any leaked model control-token markup (e.g.
            # DeepSeek ``<｜DSML｜…>``) so a backend tool-call-parser leak never
            # paints raw special tokens into the response box.
            printed_line = _strip_special_token_markup(printed_line)
            # Hard-wrap prose to the box width so the right edge gets a
            # blank margin matching _STREAM_PAD's left indent (2026-08-10).
            # Table rows come in pre-aligned by realign_markdown_tables and
            # must print with wrap=False — word-wrapping would break their
            # column padding.
            for sub_line in (_wrap_stream_line(printed_line) if wrap else [printed_line]):
                _cprint(f"{_STREAM_PAD}{_tc}{sub_line}{_RST}" if _tc else f"{_STREAM_PAD}{sub_line}")

        def _flush_table_buf() -> None:
            buf = self._stream_table_buf
            self._stream_table_buf = []
            self._in_stream_table = False
            if not buf:
                return
            # Strip cell-level markdown (`code`, **bold**, ~~strike~~) FIRST
            # so the realigner pads to the final visible cell width, not
            # the marker-decorated source width.  Otherwise a body row
            # like `` | Bold | `**bold**` | `` lands narrower than its
            # header column once the markers are removed.
            joined = "\n".join(buf)
            if self.final_response_markdown == "strip":
                joined = _strip_markdown_syntax(joined)
            block = realign_markdown_tables(joined, _terminal_width_for_streaming())
            for ln in block.split("\n"):
                _emit_one(ln, wrap=False)

        while "\n" in self._stream_buf:
            line, self._stream_buf = self._stream_buf.split("\n", 1)

            # Hold table-shaped lines in a side-buffer so we can re-pad
            # the whole block once it ends.  Streaming line-by-line, we
            # cannot re-align mid-table without reflowing already-printed
            # rows; the cost is that the user sees the table appear in a
            # single batch when the block closes instead of row-by-row.
            if self._in_stream_table:
                if looks_like_table_row(line) or is_table_divider(line):
                    self._stream_table_buf.append(line)
                    continue
                # Block ended — flush the realigned table, then fall
                # through to print the current (non-table) line.
                _flush_table_buf()
            elif looks_like_table_row(line):
                self._stream_table_buf.append(line)
                self._in_stream_table = True
                continue

            if self.final_response_markdown == "strip":
                line = _strip_markdown_syntax(line)
            _emit_one(line)

        # Long partial lines are emitted ONLY at real newlines — we don't
        # hard-wrap an in-flight, still-growing paragraph ourselves (that
        # would force a re-wrap of already-printed lines every time a new
        # word arrives). Each logical line still lands in scrollback as one
        # PRINTED line via _emit_one() above, which now hard-wraps it to
        # the box's text width for a symmetric right margin (2026-08-10) —
        # so wrapping does happen, just only once the line is complete
        # (at a real '\n', or via the sentence-boundary early-flush below).
        #
        # TTFT perception: while a long opening paragraph accumulates
        # without a newline, mirror its tail into the status-bar spinner
        # line so the user sees tokens arriving instead of a blank box.
        if (
            self._stream_buf
            and not self._in_stream_table
            and not self._stream_buf.lstrip().startswith("|")
            and len(self._stream_buf) >= 80
        ):
            preview = self._stream_buf[-int(_STREAM_PARTIAL_PREVIEW_LEN):]
            cut = preview.find(" ")
            if 0 < cut < len(preview) - 1:
                preview = preview[cut + 1:]
            try:
                self._spinner_text = f"… {preview}"
                self._invalidate()
            except Exception:
                pass

        # FORK — sentence-boundary early flush (2026-07-15). Between tool
        # calls the model can generate a short sentence/paragraph and then
        # go silent on this content block for a while (streaming tool-call
        # arguments instead) — that already-generated text sits invisible
        # in _stream_buf until a newline shows up or the turn ends and
        # _flush_stream() drains it. From the user's side this looks
        # exactly like the display froze mid-sentence even though the
        # model already produced more.  Mirrors the same natural-boundary
        # idea _flush_reasoning_preview() already uses for the dim
        # reasoning box — flush what's already a complete sentence even
        # though no newline has arrived yet.  Gated BELOW the terminal
        # width: once a paragraph exceeds wrap_w it stays buffered as one
        # logical line (upstream's no-hard-wrap contract above — the
        # terminal soft-wraps it and the spinner mirrors its tail), so
        # this early flush only covers the short-sentence-then-tool-call
        # shape it was built for.
        if (
            self._stream_buf
            and not self._in_stream_table
            and not self._stream_buf.lstrip().startswith("|")
        ):
            wrap_w = max(40, _terminal_width_for_streaming())
            min_sentence_flush = max(24, wrap_w // 3)
            if min_sentence_flush <= len(self._stream_buf) < wrap_w:
                cut = -1
                for boundary in (". ", "! ", "? ", ": "):
                    idx = self._stream_buf.rfind(boundary)
                    if idx != -1:
                        cut = max(cut, idx + len(boundary) - 1)
                if cut > 0:
                    chunk, self._stream_buf = (
                        self._stream_buf[: cut + 1],
                        self._stream_buf[cut + 1 :].lstrip(" "),
                    )
                    if self.final_response_markdown == "strip":
                        chunk = _strip_markdown_syntax(chunk)
                    _emit_one(chunk)

    def _flush_stream(self) -> None:
        """Emit any remaining partial line from the stream buffer and close the box."""
        from agent.markdown_tables import is_table_divider, looks_like_table_row
        # If we're still inside a "reasoning block" at end-of-stream, it was
        # a false positive — the model mentioned a tag like <think> in prose
        # but never closed it.  Recover the buffered content as regular text.
        if getattr(self, "_in_reasoning_block", False) and getattr(self, "_stream_prefilt", ""):
            self._in_reasoning_block = False
            self._emit_stream_text(self._stream_prefilt)
            self._stream_prefilt = ""

        # Close reasoning box if still open (in case no content tokens arrived)
        self._close_reasoning_box()

        _tc = getattr(self, "_stream_text_ansi", "")

        # If the stream buffer has a trailing partial line that looks like
        # a table row, fold it into the table buffer so the whole block
        # gets re-aligned together.  Otherwise the final row prints raw
        # (with the model's original under-padded spacing) while the rows
        # above it are aligned.
        if (
            self._stream_buf
            and getattr(self, "_in_stream_table", False)
            and (looks_like_table_row(self._stream_buf) or is_table_divider(self._stream_buf))
        ):
            self._stream_table_buf.append(self._stream_buf)
            self._stream_buf = ""

        # Flush any buffered table rows first so their padding is
        # finalised before the stream remainder lands.
        if getattr(self, "_stream_table_buf", None):
            joined = "\n".join(self._stream_table_buf)
            self._stream_table_buf = []
            self._in_stream_table = False
            if self.final_response_markdown == "strip":
                joined = _strip_markdown_syntax(joined)
            block = realign_markdown_tables(joined, _terminal_width_for_streaming())
            for ln in block.split("\n"):
                ln = _strip_special_token_markup(ln)
                # Table rows are pre-aligned by the realigner above — do
                # NOT word-wrap them, it would break column padding.
                _cprint(f"{_STREAM_PAD}{_tc}{ln}{_RST}" if _tc else f"{_STREAM_PAD}{ln}")

        if self._stream_buf:
            line = _strip_markdown_syntax(self._stream_buf) if self.final_response_markdown == "strip" else self._stream_buf
            line = _strip_special_token_markup(line)
            for sub_line in _wrap_stream_line(line):
                _cprint(f"{_STREAM_PAD}{_tc}{sub_line}{_RST}" if _tc else f"{_STREAM_PAD}{sub_line}")
            self._stream_buf = ""

        # Close the response box.  Note: _stream_box_opened stays True
        # past this point so the post-stream "already_streamed" check
        # downstream can see the box was rendered and skip the Rich
        # Panel duplicate; _reset_stream_state() clears it next turn.
        if self._stream_box_opened:
            w = self._scrollback_box_width()
            _cprint(f"{_ACCENT}╰{'─' * (w - 2)}╯{_RST}")

        # Box is closed: release any agent status lines parked by
        # _agent_status_print while it was live, so they print AFTER the footer
        # instead of between two paragraphs of the reply.
        self._stream_box_live = False
        self._release_held_status_lines()

        # Drain any messages that were queued while the box was open
        # (e.g. "Queued for the next turn" confirmations the user
        # triggered mid-stream).  Now that the box is closed they can
        # render without breaking the frame.  Flip _stream_drained
        # FIRST so concurrent producers from the UI thread don't queue
        # a new message after we've already drained — they'll see the
        # flag and print directly.
        self._stream_drained = True
        try:
            with self._post_stream_lock:
                pending, self._post_stream_messages = self._post_stream_messages, []
        except Exception:
            pending = []
        for _msg in pending:
            try:
                _cprint(_msg)
            except Exception:
                pass

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

    def _reset_stream_state(self) -> None:
        """Reset streaming state before each agent invocation."""
        self._stream_buf = ""
        self._stream_started = False
        self._stream_box_opened = False
        self._stream_drained = False
        self._stream_text_ansi = ""
        self._stream_prefilt = ""
        # Don't drop _post_stream_messages here — _flush_stream() drains
        # them after closing the box.  Resetting at turn-start would
        # silently swallow a confirmation that arrived between
        # _flush_stream and the next turn's reset.
        self._in_reasoning_block = False
        self._stream_last_was_newline = True
        self._reasoning_box_opened = False
        self._reasoning_buf = ""
        self._reasoning_preview_buf = ""
        self._deferred_content = ""
        self._stream_table_buf = []
        self._in_stream_table = False
        # No box is live at turn start; also clears any status lines still
        # parked from a turn that errored out before its footer.
        self._stream_box_live = False
        self._held_status_lines = []

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
                self._console_print(f"[yellow]⚠ {notice}[/yellow]")
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
            self.preloaded_skills = loaded_skills

    def show_banner(self):
        """Display the welcome banner in Claude Code style."""
        from hermes_cli.banner import build_welcome_banner
        self.console.clear()
        ctx_len = None
        if hasattr(self, 'agent') and self.agent and hasattr(self.agent, 'context_compressor'):
            ctx_len = self.agent.context_compressor.context_length
        
        # Auto-compact for narrow terminals — the full banner with caduceus
        # + tool list needs ~80 columns minimum to render without wrapping.
        term_width = shutil.get_terminal_size().columns
        use_compact = self.compact or term_width < 80
        
        if use_compact:
            self._console_print(_build_compact_banner())
            self._show_status()
        else:
            # Warm-launch fast path: replay last launch's tool panel when the
            # snapshot fingerprint (config.yaml + .env + checkout rev +
            # toolsets) is unchanged, skipping the ~0.5-0.9s cold
            # get_tool_definitions walk. The agent's REAL tool list is still
            # computed fresh at first message; a background refresh below
            # re-verifies the snapshot so any drift self-heals next launch.
            from hermes_cli.banner import (
                compute_toolset_availability,
                load_banner_snapshot,
                save_banner_snapshot,
            )

            snapshot = None
            try:
                snapshot = load_banner_snapshot(self.enabled_toolsets)
            except Exception:
                snapshot = None

            # Get terminal working directory (where commands will execute)
            cwd = os.getenv("TERMINAL_CWD", os.getcwd())

            if snapshot is not None:
                self._defer_tool_warnings = True
                toolset_map = snapshot["toolset_map"]
                build_welcome_banner(
                    console=self.console,
                    model=self.model,
                    cwd=cwd,
                    tools=snapshot["tools"],
                    enabled_toolsets=self.enabled_toolsets,
                    disabled_toolsets=self.disabled_toolsets,
                    session_id=self.session_id,
                    get_toolset_for_tool=lambda name: toolset_map.get(name),
                    context_length=ctx_len,
                    provider=self.provider,
                    availability=snapshot["availability"],
                    skills_by_category=snapshot.get("skills_by_category"),
                )

                def _refresh_banner_snapshot() -> None:
                    try:
                        from model_tools import get_toolset_for_tool
                        tools = get_tool_definitions(
                            enabled_toolsets=self.enabled_toolsets, quiet_mode=True
                        )
                        availability = compute_toolset_availability(self.enabled_toolsets)
                        tmap = {
                            t["function"]["name"]: get_toolset_for_tool(t["function"]["name"])
                            for t in tools
                        }
                        for item in availability.get("unavailable_toolsets", []):
                            for name in item.get("tools", []):
                                tmap.setdefault(
                                    name, item.get("id", item.get("name", ""))
                                )
                        save_banner_snapshot(
                            tools, self.enabled_toolsets, availability, tmap
                        )
                    except Exception:
                        logger.debug("banner snapshot refresh failed", exc_info=True)

                threading.Thread(
                    target=_refresh_banner_snapshot,
                    name="banner-snapshot-refresh",
                    daemon=True,
                ).start()
            else:
                # Cold path: compute everything live, then persist the snapshot
                # so the next launch replays it.
                from model_tools import get_toolset_for_tool
                tools = get_tool_definitions(enabled_toolsets=self.enabled_toolsets, quiet_mode=True)
                availability = compute_toolset_availability(self.enabled_toolsets)

                build_welcome_banner(
                    console=self.console,
                    model=self.model,
                    cwd=cwd,
                    tools=tools,
                    enabled_toolsets=self.enabled_toolsets,
                    disabled_toolsets=self.disabled_toolsets,
                    session_id=self.session_id,
                    context_length=ctx_len,
                    provider=self.provider,
                    availability=availability,
                )
                try:
                    tmap = {
                        t["function"]["name"]: get_toolset_for_tool(t["function"]["name"])
                        for t in tools
                    }
                    for item in availability.get("unavailable_toolsets", []):
                        for name in item.get("tools", []):
                            tmap.setdefault(name, item.get("id", item.get("name", "")))
                    save_banner_snapshot(tools, self.enabled_toolsets, availability, tmap)
                except Exception:
                    logger.debug("banner snapshot save failed", exc_info=True)
        
        # Tool discovery is intentionally deferred on the Termux bare prompt
        # path; availability warnings are shown once tools are initialized.
        # On the snapshot fast path (warm launch), the check walks every
        # check_fn (~180ms) — run it in the background refresh thread instead
        # and let its output land above the prompt (patch_stdout-safe).
        if os.environ.get("HERMES_DEFER_AGENT_STARTUP") != "1":
            if getattr(self, "_defer_tool_warnings", False):
                threading.Thread(
                    target=self._show_tool_availability_warnings,
                    name="tool-availability-warnings",
                    daemon=True,
                ).start()
            else:
                self._show_tool_availability_warnings()

        # Warn about low context lengths (common with local servers). Keep
        # this tied to the runtime guard so guidance cannot drift again.
        from agent.model_metadata import MINIMUM_CONTEXT_LENGTH
        if ctx_len and ctx_len < MINIMUM_CONTEXT_LENGTH:
            self._console_print()
            self._console_print(
                f"[yellow]⚠️  Context length is only {ctx_len:,} tokens — "
                f"this is likely too low for agent use with tools.[/]"
            )
            self._console_print(
                f"[dim]   Hermes needs at least {MINIMUM_CONTEXT_LENGTH:,} tokens. Tool schemas + system prompt use a large fixed prefix.[/]"
            )
            base_url = getattr(self, "base_url", "") or ""
            from urllib.parse import urlparse as _urlparse
            try:
                _parsed = _urlparse(base_url if "://" in base_url else f"//{base_url}")
                _port = _parsed.port
            except ValueError:
                _port = None
            _host = base_url_hostname(base_url)
            if _port == 11434 or "ollama" in _host:
                self._console_print(
                    f"[dim]   Ollama fix: OLLAMA_CONTEXT_LENGTH={MINIMUM_CONTEXT_LENGTH} ollama serve[/]"
                )
            elif _port == 1234:
                self._console_print(
                    "[dim]   LM Studio fix: Set context length in model settings → reload model[/]"
                )
            else:
                self._console_print(
                    "[dim]   Fix: Set model.context_length in config.yaml, or increase your server's context setting[/]"
                )

        # Warn if the configured model is a Nous Hermes LLM (not agentic)
        from hermes_cli.model_switch import is_nous_hermes_non_agentic

        model_name = getattr(self, "model", "") or ""
        if is_nous_hermes_non_agentic(model_name):
            self._console_print()
            self._console_print(
                "[bold yellow]⚠  Nous Research Hermes 3 & 4 models are NOT agentic and are not "
                "designed for use with Hermes Agent.[/]"
            )
            self._console_print(
                "[dim]   They lack tool-calling capabilities required for agent workflows. "
                "Consider using an agentic model (Claude, GPT, Gemini, DeepSeek, etc.).[/]"
            )
            self._console_print(
                "[dim]   Switch with: /model sonnet  or  /model gpt5[/]"
            )

        # Project-local skills: one-line status. Trusted → show count;
        # untrusted-with-skills → point at `hermes skills trust`. Never raises.
        try:
            from agent.skill_utils import (
                get_project_skills_dirs,
                get_untrusted_project_skills_root,
                iter_skill_index_files,
            )
            _proj_dirs = get_project_skills_dirs()
            if _proj_dirs:
                _n = sum(
                    sum(1 for _ in iter_skill_index_files(d, "SKILL.md"))
                    for d in _proj_dirs
                )
                if _n:
                    self._console_print(
                        f"[dim]◆ {_n} project skill(s) loaded from this repo[/]"
                    )
            else:
                _untrusted = get_untrusted_project_skills_root()
                if _untrusted is not None:
                    _root, _n = _untrusted
                    self._console_print(
                        f"[yellow]◆ {_n} project skill(s) found in {_root} but not "
                        f"loaded — run `hermes skills trust` to enable them.[/]"
                    )
        except Exception:
            logger.debug("project skills banner notice failed", exc_info=True)

        self._console_print()

    def _show_tool_availability_warnings(self):
        """Warn about tools disabled by missing API keys (not system deps)."""
        try:
            from model_tools import check_tool_availability

            available, unavailable = check_tool_availability()
            api_key_missing = [u for u in unavailable if u["missing_vars"]]

            if api_key_missing:
                self._console_print()
                self._console_print("[yellow]⚠️  Some tools disabled (missing API keys):[/]")
                for item in api_key_missing:
                    self._console_print(f"   [dim]• {item['name']}[/] [dim italic]({', '.join(item['missing_vars'])})[/]")
                self._console_print("[dim]   Run 'hermes setup' to configure[/]")
        except Exception:
            pass
    def _show_status(self):
        """Show compact startup status line."""
        # Avoid pulling the full tool registry into the bare Termux prompt path.
        if os.environ.get("HERMES_DEFER_AGENT_STARTUP") == "1":
            tool_status = "tools deferred"
        else:
            tools = get_tool_definitions(enabled_toolsets=self.enabled_toolsets, disabled_toolsets=self.disabled_toolsets, quiet_mode=True)
            tool_count = len(tools) if tools else 0
            tool_status = f"{tool_count} tools"

        # Format model name (shorten if needed)
        model_short = self.model.split("/")[-1] if "/" in self.model else self.model
        if len(model_short) > 30:
            model_short = model_short[:27] + "..."

        # Get API status indicator
        if self.api_key:
            api_indicator = "[green bold]●[/]"
        else:
            api_indicator = "[red bold]●[/]"

        # Build status line with proper markup — skin-aware colors
        try:
            from hermes_cli.skin_engine import get_active_skin
            skin = get_active_skin()
            separator_color = skin.get_color("banner_dim", "#B8860B")
            accent_color = skin.get_color("ui_accent", "#FFBF00")
            label_color = skin.get_color("ui_label", "#DAA520")
        except Exception:
            separator_color, accent_color, label_color = "#B8860B", "#FFBF00", "cyan"
        toolsets_info = ""
        if self.enabled_toolsets and "all" not in self.enabled_toolsets:
            toolsets_info = f" [dim {separator_color}]·[/] [{label_color}]toolsets: {', '.join(self.enabled_toolsets)}[/]"

        provider_info = f" [dim {separator_color}]·[/] [dim]provider: {self.provider}[/]"
        if self._provider_source:
            provider_info += f" [dim {separator_color}]·[/] [dim]auth: {self._provider_source}[/]"

        self._console_print(
            f"  {api_indicator} [{accent_color}]{model_short}[/] "
            f"[dim {separator_color}]·[/] [bold {label_color}]{tool_status}[/]"
            f"{toolsets_info}{provider_info}"
        )

    def show_tools(self):
        """Display available tools with kawaii ASCII art."""
        from model_tools import get_toolset_for_tool
        # Pre-assembly list: /tools is a discovery/inspection surface, so it
        # must show the full catalog including tools deferred behind the
        # tool_search bridge (users check this to verify an MCP installed).
        tools = get_tool_definitions(enabled_toolsets=self.enabled_toolsets, disabled_toolsets=self.disabled_toolsets, quiet_mode=True,
                                     skip_tool_search_assembly=True)
        
        if not tools:
            print("(;_;) No tools available")
            return
        
        # Header
        print()
        title = "(^_^)/ Available Tools"
        width = 78
        pad = width - len(title)
        print("+" + "-" * width + "+")
        print("|" + " " * (pad // 2) + title + " " * (pad - pad // 2) + "|")
        print("+" + "-" * width + "+")
        print()
        
        # Group tools by toolset
        toolsets = {}
        for tool in sorted(tools, key=lambda t: t["function"]["name"]):
            name = tool["function"]["name"]
            toolset = get_toolset_for_tool(name) or "unknown"
            if toolset not in toolsets:
                toolsets[toolset] = []
            desc = tool["function"].get("description", "")
            # First sentence: split on ". " (period+space) to avoid breaking on "e.g." or "v2.0"
            desc = desc.split("\n")[0]
            if ". " in desc:
                desc = desc[:desc.index(". ") + 1]
            toolsets[toolset].append((name, desc))
        
        # Display by toolset
        for toolset in sorted(toolsets.keys()):
            print(f"  [{toolset}]")
            for name, desc in toolsets[toolset]:
                print(f"    * {name:<20} - {desc}")
            print()
        
        print(f"  Total: {len(tools)} tools  ヽ(^o^)ノ")
        print()


    def show_toolsets(self):
        """Display available toolsets with kawaii ASCII art."""
        from toolsets import get_all_toolsets, get_toolset_info
        # The hermes-<platform> composites for OTHER platforms (e.g.
        # hermes-discord, hermes-feishu, hermes-yuanbao) all mirror
        # _HERMES_CORE_TOOLS and only matter when running as that bot.
        # Skip them here so the cli's `/toolsets` listing isn't padded
        # with messenger-bot composites the user can't actually use.
        from hermes_cli.platforms import PLATFORMS as _PLATFORMS
        other_platform_composites = {
            info.default_toolset
            for key, info in _PLATFORMS.items()
            if key != "cli"
        }

        all_toolsets = get_all_toolsets()

        # Header
        print()
        title = "(^_^)b Available Toolsets"
        width = 58
        pad = width - len(title)
        print("+" + "-" * width + "+")
        print("|" + " " * (pad // 2) + title + " " * (pad - pad // 2) + "|")
        print("+" + "-" * width + "+")
        print()

        for name in sorted(all_toolsets.keys()):
            if name in other_platform_composites:
                continue
            info = get_toolset_info(name)
            if info:
                tool_count = info["tool_count"]
                desc = info["description"]

                # Mark if currently enabled
                marker = "(*)" if self.enabled_toolsets and name in self.enabled_toolsets else "   "
                print(f"  {marker} {name:<18} [{tool_count:>2} tools] - {desc}")

        print()
        print("  (*) = currently enabled")
        print()
        print("  Tip: Use 'all' or '*' to enable all toolsets")
        print("  Example: python cli.py --toolsets web,terminal")
        print()
    


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

    def _get_slash_confirm_display_fragments(self):
        """Render the /new-/clear-style confirmation panel."""
        state = self._slash_confirm_state
        if not state:
            return []

        title = state.get("title") or "Confirm action"
        detail = state.get("detail") or ""
        choices = state.get("choices") or []
        selected = state.get("selected", 0)

        def _panel_box_width(title_text: str, content_lines: list[str], min_width: int = 56, max_width: int = 86) -> int:
            term_cols = shutil.get_terminal_size((100, 20)).columns
            longest = max([HermesCLI._panel_cwidth(title_text)] + [HermesCLI._panel_cwidth(line) for line in content_lines] + [min_width - 4])
            inner = min(max(longest + 4, min_width - 2), max_width - 2, max(24, term_cols - 6))
            return inner + 2

        def _wrap_panel_text(text: str, width: int, subsequent_indent: str = "") -> list[str]:
            wrapped = textwrap.wrap(
                text,
                width=max(8, width),
                replace_whitespace=False,
                drop_whitespace=False,
                subsequent_indent=subsequent_indent,
            )
            return wrapped or [""]

        def _append_panel_line(lines, border_style: str, content_style: str, text: str, box_width: int) -> None:
            inner_width = max(0, box_width - 2)
            lines.append((border_style, "│ "))
            lines.append((content_style, HermesCLI._panel_ljust(text, inner_width)))
            lines.append((border_style, " │\n"))

        def _append_blank_panel_line(lines, border_style: str, box_width: int) -> None:
            lines.append((border_style, "│" + (" " * box_width) + "│\n"))

        preview_lines = []
        for line in detail.splitlines():
            preview_lines.extend(_wrap_panel_text(line, 72))
        for idx, (_value, label, desc) in enumerate(choices):
            marker = "❯" if idx == selected else " "
            preview_lines.extend(_wrap_panel_text(f"{marker} [{idx + 1}] {label} — {desc}", 72, subsequent_indent="    "))
        preview_lines.append("Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.")

        box_width = _panel_box_width(title, preview_lines)
        inner_text_width = max(8, box_width - 2)
        detail_wrapped = []
        for line in detail.splitlines():
            detail_wrapped.extend(_wrap_panel_text(line, inner_text_width))
        choice_wrapped: list[tuple[int, str]] = []
        for idx, (_value, label, desc) in enumerate(choices):
            marker = "❯" if idx == selected else " "
            for wrapped in _wrap_panel_text(f"{marker} [{idx + 1}] {label} — {desc}", inner_text_width, subsequent_indent="    "):
                choice_wrapped.append((idx, wrapped))

        term_rows = shutil.get_terminal_size((100, 24)).lines
        reserved_below = 6
        chrome_full = 6
        available = max(0, term_rows - reserved_below)
        max_detail_rows = max(1, available - chrome_full - len(choice_wrapped))
        max_detail_rows = min(max_detail_rows, 8)
        if len(detail_wrapped) > max_detail_rows:
            keep = max(1, max_detail_rows - 1)
            detail_wrapped = detail_wrapped[:keep] + ["… (detail truncated)"]

        lines = []
        lines.append(('class:approval-border', '╭' + ('─' * box_width) + '╮\n'))
        _append_panel_line(lines, 'class:approval-border', 'class:approval-title', title, box_width)
        _append_blank_panel_line(lines, 'class:approval-border', box_width)
        for wrapped in detail_wrapped:
            _append_panel_line(lines, 'class:approval-border', 'class:approval-desc', wrapped, box_width)
        _append_blank_panel_line(lines, 'class:approval-border', box_width)
        for idx, wrapped in choice_wrapped:
            style = 'class:approval-selected' if idx == selected else 'class:approval-choice'
            _append_panel_line(lines, 'class:approval-border', style, wrapped, box_width)
        _append_blank_panel_line(lines, 'class:approval-border', box_width)
        _append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', 'Type 1/2/3 or use ↑/↓ then Enter. ESC/Ctrl+C cancels.', box_width)
        lines.append(('class:approval-border', '╰' + ('─' * box_width) + '╯\n'))
        return lines

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
        self, result, persist_global: bool, custom_providers=None
    ) -> None:
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
        self, result, persist_global: bool, one_turn: bool, custom_provs=None
    ) -> None:
        """Confirm an expensive model switch and apply it to CLI state.

        Runs on a worker thread when the TUI is active (see
        _handle_model_switch) so the confirmation modal can render.
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

    def _handle_effort_command(self, cmd_original: str) -> None:
        """FORK: ``/effort`` is an alias for ``/reasoning`` (Claude Code parity).

        Upstream's ``_SLASH_DISPATCH`` has no ``effort`` entry and the naming-convention
        fallback in ``_slash_handler`` looks for exactly this method name, so the alias
        has to exist as a real handler. Rewrites the verb so the /reasoning parser sees
        the form it expects.
        """
        self._handle_reasoning_command(cmd_original.replace("/effort", "/reasoning", 1))

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
            _cprint(f"\033[1;31mUnknown command: {cmd_lower}{_RST}")
            _cprint(f"{_DIM}{_ACCENT}Type /help for available commands{_RST}")
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

    def _apply_reasoning_arg(self, arg: str) -> None:
        """Shared apply path for both the picker and the typed CLI form."""
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

        # Save to global config
        saved_global = save_config_value("agent.reasoning_effort", arg)

        # Also save per-model so switching back to this model restores it
        current_model = (self.model or "").strip()
        if current_model:
            by_model = dict(self._reasoning_effort_by_model)
            by_model[current_model] = arg
            save_config_value("agent.reasoning_effort_by_model", by_model)
            self._reasoning_effort_by_model = by_model

        if saved_global:
            _cprint(f"  {_ACCENT}✓ Reasoning effort set to '{arg}' (saved to config){_RST}")
        else:
            _cprint(f"  {_ACCENT}✓ Reasoning effort set to '{arg}' (session only){_RST}")

    def _handle_reasoning_command(self, cmd: str):
        """Handle /reasoning — interactive picker or typed effort/display.

        Usage:
            /reasoning              Open picker (interactive menu)
            /reasoning <level>      Set reasoning effort directly
            /reasoning show|on      Show model thinking inline
            /reasoning hide|off     Hide model thinking
        """
        parts = cmd.strip().split(maxsplit=1)

        if len(parts) < 2:
            # No arg → open the picker.
            self._open_reasoning_picker()
            return

        # Typed form preserved — delegate to the shared apply path.
        self._apply_reasoning_arg(parts[1])

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

        Two modes available:
          client_side (default) — Hermes-side hermes_load_tools tool.
            Each discovery is one normal API round-trip, billed once.
            No prompt-token multiplier.
          server_side — Anthropic's tool_search_tool_<variant>_20251119
            server tool. Each server-tool iteration re-bills the full
            prompt within one API call; observed multipliers of 2x-4x.
            Useful only for OAuth/Claude-subscription users whose
            billing classifier scores wire bytes.

        Reads/writes ``tool_search.enabled`` and ``tool_search.mode`` in
        config.yaml. The agent reads this fresh on every API call, so
        toggles take effect on the very next turn — no restart needed.

        Usage:
            /toolsearch                       Alias for /toolsearch status
            /toolsearch status                Show current state
            /toolsearch on                    Enable (uses current mode)
            /toolsearch off                   Disable
            /toolsearch client_side           Enable + set mode=client_side
            /toolsearch server_side           Enable + set mode=server_side
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
            mode = (ts_cfg.get("mode") or "client_side").strip().lower()
            variant = ts_cfg.get("variant", "regex")
            defer_mcp = bool(ts_cfg.get("defer_mcp_tools", True))
            state = "ON" if enabled else "OFF"
            _cprint(f"  {_ACCENT}Tool search: {state} (mode={mode}){_RST}")
            _cprint(f"  {_DIM}variant={variant}, defer_mcp_tools={defer_mcp}{_RST}")
            if mode == "client_side":
                _cprint(
                    f"  {_DIM}Discovery via Hermes-side hermes_load_tools tool. "
                    f"Each schema-load is one normal round-trip; no multiplier.{_RST}"
                )
            else:
                _cprint(
                    f"  {_DIM}Discovery via Anthropic tool_search_tool_{variant}"
                    f"_20251119. Re-bills full prompt per server-tool iteration "
                    f"(2x-4x multipliers observed).{_RST}"
                )
            _cprint(
                f"  {_DIM}Usage: /toolsearch [on|off|client_side|server_side|status|mode <m>]{_RST}"
            )

        if not argv or argv[0].lower() in ("status", "show"):
            _show_status()
            return

        first = argv[0].lower()

        # /toolsearch mode <client_side|server_side>
        if first == "mode":
            if len(argv) < 2:
                _cprint(f"  {_DIM}Usage: /toolsearch mode [client_side|server_side]{_RST}")
                return
            new_mode = argv[1].lower()
            if new_mode not in ("client_side", "server_side"):
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
            save_config_value("tool_search.enabled", True)
            save_config_value("tool_search.mode", "server_side")
            _cprint(f"  {_ACCENT}✓ Tool search: ON, mode=server_side (saved){_RST}")
            _cprint(f"  {_DIM}WARNING: server_side has known 2x-4x prompt multipliers on stacked tool_search calls.{_RST}")
            return

        if first in ("on", "true", "enable", "enabled", "yes", "1"):
            new_value = True
        elif first in ("off", "false", "disable", "disabled", "no", "0"):
            new_value = False
        else:
            _cprint(f"  {_DIM}(._.) Unknown argument: {first}{_RST}")
            _cprint(f"  {_DIM}Usage: /toolsearch [on|off|client_side|server_side|status|mode <m>]{_RST}")
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

    def _handle_busy_command(self, cmd: str):
        """Handle /busy — control what Enter does while Hermes is working.

        Usage:
            /busy               Show current busy input mode
            /busy status        Show current busy input mode
            /busy queue         Queue input for the next turn instead of interrupting
            /busy steer         Inject Enter mid-run via /steer (after next tool call)
            /busy interrupt     Interrupt the current run on Enter (default)
        """
        parts = cmd.strip().split(maxsplit=1)
        if len(parts) < 2 or parts[1].strip().lower() == "status":
            _cprint(f"  {_ACCENT}Busy input mode: {self.busy_input_mode}{_RST}")
            if self.busy_input_mode == "queue":
                _behavior = "queues for next turn"
            elif self.busy_input_mode == "steer":
                _behavior = "steers into current run (after next tool call)"
            else:
                _behavior = "interrupts current run"
            _cprint(f"  {_DIM}Enter while busy: {_behavior}{_RST}")
            _cprint(f"  {_DIM}Usage: /busy [queue|steer|interrupt|status]{_RST}")
            return

        arg = parts[1].strip().lower()
        if arg not in {"queue", "interrupt", "steer"}:
            _cprint(f"  {_DIM}(._.) Unknown argument: {arg}{_RST}")
            _cprint(f"  {_DIM}Usage: /busy [queue|steer|interrupt|status]{_RST}")
            return

        self.busy_input_mode = arg
        if save_config_value("display.busy_input_mode", arg):
            if arg == "queue":
                behavior = "Enter will queue follow-up input while Hermes is busy."
            elif arg == "steer":
                behavior = "Enter will steer your message into the current run (after the next tool call)."
            else:
                behavior = "Enter will interrupt the current run while Hermes is busy."
            _cprint(f"  {_ACCENT}✓ Busy input mode set to '{arg}' (saved to config){_RST}")
            _cprint(f"  {_DIM}{behavior}{_RST}")
        else:
            _cprint(f"  {_ACCENT}✓ Busy input mode set to '{arg}' (session only){_RST}")

    def _on_reasoning(self, reasoning_text: str):
        """Callback for intermediate reasoning display during tool-call loops."""
        if not reasoning_text:
            return
        self._reasoning_preview_buf = getattr(self, "_reasoning_preview_buf", "") + reasoning_text
        self._flush_reasoning_preview(force=False)

    def _show_usage(self):
        """Rate limits + session token usage (when a live agent exists) + Nous credits.

        The Nous credits block is agent-independent (a portal fetch), so it runs even
        with no live agent — important for the TUI, where /usage runs in a slash-worker
        subprocess that resumes the session WITHOUT building an agent (self.agent is None),
        which would otherwise early-return before any credits showed.
        """
        from agent.usage_pricing import estimate_usage_cost
        if not self.agent:
            if self._print_nous_credits_block():
                self._print_usage_cta()
            else:
                print("(._.) No active agent -- send a message first.")
            return

        agent = self.agent
        calls = agent.session_api_calls

        if calls == 0:
            if self._print_nous_credits_block():
                self._print_usage_cta()
            else:
                print("(._.) No API calls made yet in this session.")
            return

        # ── Rate limits (shown first when available) ────────────────
        rl_state = agent.get_rate_limit_state()
        if rl_state and rl_state.has_data:
            from agent.rate_limit_tracker import format_rate_limit_display
            print()
            print(format_rate_limit_display(rl_state))
            print()

        # ── Session token usage ─────────────────────────────────────
        input_tokens = getattr(agent, "session_input_tokens", 0) or 0
        output_tokens = getattr(agent, "session_output_tokens", 0) or 0
        reasoning_tokens = getattr(agent, "session_reasoning_tokens", 0) or 0
        cache_read_tokens = getattr(agent, "session_cache_read_tokens", 0) or 0
        cache_write_tokens = getattr(agent, "session_cache_write_tokens", 0) or 0
        prompt = agent.session_prompt_tokens
        completion = agent.session_completion_tokens
        total = agent.session_total_tokens

        compressor = agent.context_compressor
        # Real provider count for display (not the preflight-inflated
        # last_prompt_tokens). See ContextCompressor.display_prompt_tokens.
        if hasattr(compressor, "display_prompt_tokens"):
            last_prompt = compressor.display_prompt_tokens()
        else:
            last_prompt = compressor.last_prompt_tokens
        ctx_len = compressor.context_length
        pct = min(100, (last_prompt / ctx_len * 100)) if ctx_len else 0
        compressions = compressor.compression_count

        msg_count = len(self.conversation_history)
        cost_result = estimate_usage_cost(
            agent.model,
            CanonicalUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            provider=getattr(agent, "provider", None),
            base_url=getattr(agent, "base_url", None),
        )
        # Subagent rollup (delegate_task children) — folded into
        # session_estimated_cost_usd by tools/delegate_tool.py; broken out
        # here via dedicated counters so we can show parent vs children
        # without double-counting. cost_result.amount_usd above only covers
        # the parent's own tokens.
        sub_cost = float(getattr(agent, "session_subagent_cost_usd", 0.0) or 0.0)
        sub_in = int(getattr(agent, "session_subagent_input_tokens", 0) or 0)
        sub_out = int(getattr(agent, "session_subagent_output_tokens", 0) or 0)
        sub_n = int(getattr(agent, "session_subagent_count", 0) or 0)
        # Authoritative session total: parent (computed) + children (rolled).
        parent_cost = (
            float(cost_result.amount_usd) if cost_result.amount_usd is not None else 0.0
        )
        session_cost_total = parent_cost + sub_cost
        elapsed = format_duration_compact((datetime.now() - self.session_start).total_seconds())

        print("  📊 Session Token Usage")
        print(f"  {'─' * 40}")
        print(f"  Model:                     {agent.model}")
        print(f"  Input tokens:              {input_tokens:>10,}")
        print(f"  Output tokens:             {output_tokens:>10,}")
        if reasoning_tokens:
            print(f"  ↳ Reasoning (subset):      {reasoning_tokens:>10,}")
        print(f"  Prompt tokens (total):     {prompt:>10,}")
        print(f"  Completion tokens:         {completion:>10,}")
        print(f"  Total tokens:              {total:>10,}")
        print(f"  API calls:                 {calls:>10,}")
        print(f"  Session duration:          {elapsed:>10}")
        print(f"  Cost status:              {cost_result.status:>10}")
        print(f"  Cost source:              {cost_result.source:>10}")
        if session_cost_total > 0:
            # Parent vs subagent breakdown when we have BOTH (otherwise just
            # show the single Total cost line below for backward compat).
            if parent_cost > 0 and sub_cost > 0:
                print(f"  Parent cost:             ${parent_cost:>10.4f}")
                print(
                    f"  Subagent cost:           ${sub_cost:>10.4f}  "
                    f"({sub_n} child{'ren' if sub_n != 1 else ''}, "
                    f"{sub_in:,}↓/{sub_out:,}↑ tok)"
                )
                prefix = "~" if cost_result.status == "estimated" else ""
                print(f"  Total cost:              {prefix}${session_cost_total:>10.4f}")
            elif sub_cost > 0:
                # No parent cost (rare — parent did nothing but delegate)
                print(
                    f"  Subagent cost:           ${sub_cost:>10.4f}  "
                    f"({sub_n} child{'ren' if sub_n != 1 else ''}, "
                    f"{sub_in:,}↓/{sub_out:,}↑ tok)"
                )
                prefix = "~"
                print(f"  Total cost:              {prefix}${session_cost_total:>10.4f}")
            else:
                # Parent only — original single-line shape
                prefix = "~" if cost_result.status == "estimated" else ""
                print(f"  Total cost:              {prefix}${parent_cost:>10.4f}")
        elif cost_result.status == "included":
            print(f"  Total cost:              {'included':>10}")
        else:
            print(f"  Total cost:              {'n/a':>10}")
        print(f"  {'─' * 40}")
        print(f"  Current context:  {last_prompt:,} / {ctx_len:,} ({pct:.0f}%)")
        print(f"  Messages:         {msg_count}")
        print(f"  Compressions:     {compressions}")

        # Account limits -- fetched off-thread with a hard timeout so slow
        # provider APIs don't hang the prompt.
        provider = getattr(agent, "provider", None) or getattr(self, "provider", None)
        base_url = getattr(agent, "base_url", None) or getattr(self, "base_url", None)
        api_key = getattr(agent, "api_key", None) or getattr(self, "api_key", None)
        # Lazy import — pulls the OpenAI SDK chain, only needed here.
        from agent.account_usage import fetch_account_usage, render_account_usage_lines
        account_snapshot = None
        if provider:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                try:
                    account_snapshot = _pool.submit(
                        fetch_account_usage, provider,
                        base_url=base_url, api_key=api_key,
                    ).result(timeout=10.0)
                except (concurrent.futures.TimeoutError, Exception):
                    account_snapshot = None
        account_lines = [f"  {line}" for line in render_account_usage_lines(account_snapshot)]
        if account_lines:
            print()
            for line in account_lines:
                print(line)

        # Nous credits magnitudes + monthly-grant gauge (agent-independent — also
        # runs at the no-agent / no-calls early-returns above). See the helper.
        if self._print_nous_credits_block():
            self._print_usage_cta()

        if self.verbose:
            logging.getLogger().setLevel(logging.DEBUG)
            for noisy in ('openai', 'openai._base_client', 'httpx', 'httpcore', 'asyncio', 'hpack', 'grpc', 'modal'):
                logging.getLogger(noisy).setLevel(logging.WARNING)
        else:
            logging.getLogger().setLevel(logging.INFO)
            # NOTE: We deliberately do NOT raise per-logger levels for
            # tools/run_agent/etc. in quiet mode. Setting logger.setLevel
            # above the file handler level filters records before they
            # reach handlers, so agent.log / errors.log lose visibility
            # into stream-retry events, credential rotations, etc.
            # Console quietness is enforced by hermes_logging not
            # installing a console StreamHandler in non-verbose mode.

    _DESTRUCTIVE_SKIP_TOKENS = frozenset({"now", "--yes", "-y"})

    def _tui_process_loop(self):
        """REPL worker thread: drain ``_pending_input``, run idle housekeeping, dispatch each input."""
        while not self._should_exit:
            try:
                try:
                    user_input = self._pending_input.get(timeout=0.1)
                except queue.Empty:
                    if not self._agent_running:
                        self._tui_idle_tick()
                    continue
                self._tui_process_one_input(user_input)
            except Exception as e:
                if isinstance(e, OSError) and e.errno == errno.EIO:
                    self._mark_terminal_io_broken("process_loop")
                    logger.warning("process_loop EIO — freezing UI paints (#81521): %s", e)
                    continue
                logger.warning("process_loop unhandled error (msg may be lost): %s", e)

    def _tui_idle_tick(self):
        """Idle housekeeping between inputs (agent not running)."""
        self._check_config_mcp_changes()  # auto-reload MCP on mcp_servers change
        # Termios drift heal first: a drifted tty makes the CLI look dead while the loop is healthy.
        for step in (
            self._check_termios_drift,
            lambda: self._drain_process_notifications("cli-idle"),
            self._maybe_fire_loop_tick,
            self._maybe_resume_parked_goal,
        ):
            with suppress(Exception):
                step()

    def _tui_process_one_input(self, user_input):
        """Route one submitted input: file drop, /resume pick, ! shell, slash command, or a chat turn."""
        from tools.process_registry_notifications import TimelineNotification
        user_input, is_voice_input, is_seeded_query = self._tui_unwrap_input(user_input)
        if not user_input:
            return
        notification_preview = user_input if isinstance(user_input, TimelineNotification) else None
        self._status_bar_suppressed_after_resize = False  # input ends post-resize suppression

        submit_images = []
        if isinstance(user_input, tuple):
            user_input, submit_images = user_input

        if isinstance(user_input, str):
            user_input = _strip_leaked_bracketed_paste_wrappers(user_input)
            user_input, _had_mouse_reports = _strip_leaked_terminal_responses_with_meta(user_input)
            if _had_mouse_reports:
                self._recover_terminal_input_modes(reason="mouse reports leaked into submitted input")

        # A typed bare stop phrase ends an active voice chat (transcripts are checked earlier).
        if not is_voice_input and self._typed_voice_stop(user_input):
            return

        # File drops are detected before any dispatch; seeded -q prompts are literal text.
        _file_drop = _detect_file_drop(user_input) if isinstance(user_input, str) and not is_seeded_query else None
        if _file_drop:
            _drop_path = _file_drop["path"]
            _remainder = _file_drop["remainder"]
            if _file_drop["is_image"]:
                submit_images.append(_drop_path)
                user_input = _remainder or f"[User attached image: {_drop_path.name}]"
                _cprint(f"  📎 Auto-attached image: {_drop_path.name}")
            else:
                _cprint(f"  📄 Detected file: {_drop_path.name}")
                user_input = f"[User attached file: {_drop_path}]" + (f"\n{_remainder}" if _remainder else "")
        elif isinstance(user_input, str):
            # A bare number right after a bare `/resume` selects that session (never sent to the agent).
            if self._pending_resume_sessions and self._consume_pending_resume_selection(user_input):
                return
            if not is_seeded_query:
                if self.handle_bang_shell(user_input):
                    return
                if _looks_like_slash_command(user_input):
                    user_input = self._tui_run_slash_input(user_input)
                    if user_input is None:
                        return

        if isinstance(user_input, str) and _PASTE_REF_RE.search(user_input):
            user_input = self._expand_paste_references(user_input)
        print()
        self._print_user_message_preview(notification_preview or user_input)

        if submit_images:
            n = len(submit_images)
            _cprint(f"  {_DIM}📎 {n} image{'s' if n > 1 else ''} attached{_RST}")

        self._agent_running = self._interactive_turn = True
        self._pet_turn_error = self._pet_reasoning = False
        self._turn_summary_begin()
        self._app.invalidate()
        try:
            self.chat(notification_preview or user_input, images=submit_images or None, voice_input=is_voice_input)
        finally:
            self._tui_after_turn()

    def _tui_run_slash_input(self, user_input: str):
        """Dispatch a slash command. Returns the pending agent seed to run as a chat turn, else None."""
        _cprint(f"\n⚙️  {user_input}")
        try:
            if not self.process_command(user_input):
                self._should_exit = True
                if self._app.is_running:
                    self._app.exit()
        except KeyboardInterrupt:
            # Ctrl+C during a slow slash command returns to the prompt instead of exiting.
            _cprint("\n[dim]Command interrupted.[/dim]")
            return None
        _seed, self._pending_agent_seed = self._pending_agent_seed, None
        return _seed or None

    def _tui_after_turn(self):
        """Post-turn bookkeeping after chat() returns (normal, error, or interrupt)."""
        self._agent_running = self._pet_reasoning = False
        self._spinner_text = self._last_scrollback_tool = ""
        self._tool_start_time = 0.0
        self._pending_tool_info.clear()
        self._pet_react_turn_end()
        self._turn_summary_emit()
        self._interactive_turn = False
        self._app.invalidate()

        # After an interrupt the renderer may have drifted (leaked CPR text, VT100 parser
        # stalled mid-escape): drain stray bytes and force a clean redraw.
        if self._last_turn_interrupted:
            self._recover_terminal_after_interrupt()

        # Re-queue any messages that arrived in _interrupt_queue while the agent was running and were never
        # claimed by the explicit interrupt path. See _drain_interrupt_queue_to_pending_input for the full
        # rationale. Regression of #17666 / #18760 — the drain block from the original PR #17939 was
        # deferred as "worth its own review" and never re-landed (#20271).
        self._drain_interrupt_queue_to_pending_input()

        # /goal continuation (queued user input still preempts), then /loop tick completion.
        for hook, what in (
            (self._maybe_continue_goal_after_turn, "goal continuation"),
            (self._maybe_complete_loop_tick_after_turn, "loop completion"),
        ):
            try:
                hook()
            except Exception as _exc:
                logging.debug("%s hook failed: %s", what, _exc)

        # Continuous voice: restart recording off-thread (beep + recorder start would block process_loop).
        if self._voice_mode and self._voice_continuous and not self._voice_recording:
            def _restart_recording():
                try:
                    if self._voice_tts:
                        self._voice_tts_done.wait(timeout=60)
                        time.sleep(0.3)
                    # A barge-in capture already owns the mic and submits the interruption itself.
                    if self._voice_barge_capture.is_set():
                        return
                    self._voice_start_recording()
                    self._app.invalidate()
                except Exception as e:
                    _cprint(f"{_DIM}Voice auto-restart failed: {e}{_RST}")
            threading.Thread(target=_restart_recording, daemon=True).start()

        with suppress(Exception):
            self._drain_process_notifications("cli-post-turn")

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

    def _on_tool_complete(self, tool_call_id: str, function_name: str, function_args: dict, function_result: str):
        """Render file edits with inline diff after write-capable tools complete."""
        # A top-level delegate_task dispatches in the background and re-enters as
        # a fresh turn when done. Say so once — no spinner, nothing to poll — so
        # the idle prompt doesn't read as "nothing happened" (⛓ tracks the work).
        if function_name == "delegate_task":
            try:
                parsed = json.loads(function_result) if isinstance(function_result, str) else (function_result or {})
            except Exception:
                parsed = {}
            if isinstance(parsed, dict) and parsed.get("status") == "dispatched" and parsed.get("mode") == "background":
                n = parsed.get("count") or 1
                noun, tail = ("task", "it finishes") if n == 1 else (f"{n} tasks", "they finish")
                did = parsed.get("delegation_id") or ""
                did_suffix = f" [{did}]" if did else ""
                try:
                    _cprint(
                        f"\033[2m\u21a9 Background {noun} running{did_suffix} — I'll resume when {tail}. "
                        f"Keep chatting. (/stop {did} to cancel)\033[0m"
                        if did else
                        f"\033[2m\u21a9 Background {noun} running — I'll resume when {tail}. Keep chatting.\033[0m"
                    )
                except Exception:
                    pass
        snapshot = self._pending_edit_snapshots.pop(tool_call_id, None)
        try:
            from agent.display import render_edit_diff_with_delta

            render_edit_diff_with_delta(
                function_name,
                function_result,
                function_args=function_args,
                snapshot=snapshot,
                print_fn=_cprint,
            )
        except Exception:
            logger.debug("Edit diff preview failed for %s", function_name, exc_info=True)

    # ====================================================================
    # Voice mode methods
    # ====================================================================

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

    def _clarify_callback(self, question, choices, multi_select=False, questions=None):
        """
        Platform callback for the clarify tool. Called from the agent thread.

        Sets up the interactive selection UI (or freetext prompt for open-ended
        questions), then blocks until the user responds via the prompt_toolkit
        key bindings.  If no response arrives within the configured timeout the
        question is dismissed and the agent is told to decide on its own.

        When ``multi_select`` is True, shows checkboxes and the user can
        select multiple options with Space, confirming with Enter.

        When ``questions`` is a non-empty list (batch clarify, issue #18450),
        the panel switches to the A-compact multi-question layout and the
        return value is a dict ``{"answers": {qid: raw_answer}}`` (plus
        ``"timed_out": True`` when the deadline expired with only partial
        answers). The single-question path below is unchanged.
        """
        import time as _time

        from tools.clarify_gateway import resolve_clarify_timeout

        if questions:
            return self._clarify_callback_batch(questions)

        # Canonical clarify timeout, shared with the gateway/TUI path. `<= 0`
        # means unlimited (never auto-skip mid-think) → a null deadline.
        timeout = resolve_clarify_timeout(CLI_CONFIG)
        response_queue = queue.Queue()
        is_open_ended = not choices
        # multi-select support: only active when multi_select is True and choices exist
        effective_multi = multi_select and not is_open_ended

        self._clarify_state = {
            "question": question,
            "choices": choices if not is_open_ended else [],
            "selected": 0,
            # multi-select support
            "multi_select": effective_multi,
            "selected_indices": set() if effective_multi else None,
            "response_queue": response_queue,
        }
        self._clarify_deadline = None if timeout <= 0 else _time.monotonic() + timeout
        # Open-ended questions skip straight to freetext input
        self._clarify_freetext = is_open_ended
        self._clarify_multi_base = None

        # Bell + native notification — clarify questions can sit on the
        # screen for a long time before the user notices.
        _clarify_summary = question if question else "Hermes is asking a question"
        if len(_clarify_summary) > 120:
            _clarify_summary = _clarify_summary[:117] + "..."
        self._fire_attention_signals(_clarify_summary)

        # Trigger an immediate prompt_toolkit repaint from this (non-main)
        # thread. Modal prompts must paint at once and must not be gated by the
        # _invalidate throttle / resize guard — see _paint_now / _invalidate (#41098).
        self._paint_now()

        # Poll for the user's response. The countdown in the hint line updates
        # on each repaint; refresh it once a second so the timer stays visible
        # while we wait. Selection changes (↑/↓) trigger instant repaints via
        # the key bindings.
        _last_countdown_refresh = _time.monotonic()
        while True:
            try:
                result = response_queue.get(timeout=1)
                self._clarify_deadline = None
                self._persist_prompt_summary("?", "Clarify", question, str(result))
                return result
            except queue.Empty:
                # None deadline = unlimited: never auto-skip, just keep polling.
                if self._clarify_deadline is not None:
                    remaining = self._clarify_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                now = _time.monotonic()
                if now - _last_countdown_refresh >= 1.0:
                    _last_countdown_refresh = now
                    self._paint_now()

        # Timed out — tear down the UI and let the agent decide
        self._clarify_state = None
        self._clarify_freetext = False
        self._clarify_deadline = None
        self._clarify_multi_base = None
        self._paint_now()
        _cprint(f"\n{_DIM}(clarify timed out after {timeout}s — agent will decide){_RST}")
        return (
            "The user did not provide a response within the time limit. "
            "Use your best judgement to make the choice and proceed."
        )

    # --- Batch clarify (multi-question, issue #18450) -----------------------

    def _clarify_callback_batch(self, questions):
        """Batch clarify panel (A-compact): all questions, one active.

        Blocks on the response queue like the single-question path. Returns
        ``{"answers": {qid: raw_answer}}`` when every question is locked, the
        same dict plus ``"timed_out": True`` when the deadline expires with
        partial (or zero) answers, and passes a cancel string through
        unchanged so the tool core resolves the batch empty.
        """
        import time as _time

        from tools.clarify_gateway import resolve_clarify_timeout

        timeout = resolve_clarify_timeout(CLI_CONFIG)
        response_queue = queue.Queue()

        state = {
            "questions": list(questions),
            "answers": {},
            "answer_meta": {},
            "active": 0,
            "response_queue": response_queue,
            # Flat keys mirroring the active question — filled by
            # _clarify_batch_set_active below.
            "question": "",
            "choices": [],
            "selected": 0,
            "multi_select": False,
            "selected_indices": None,
        }
        self._clarify_state = state
        self._clarify_batch_set_active(state, 0)
        self._clarify_deadline = None if timeout <= 0 else _time.monotonic() + timeout

        # Bell + native notification — clarify questions can sit on the
        # screen for a long time before the user notices. Mirror the
        # single-question path's summary construction: first question text,
        # plus a count suffix when the batch has more than one.
        _first = state["questions"][0]["question"] if state["questions"] else ""
        _batch_summary = _first if _first else "Hermes is asking a question"
        if len(_batch_summary) > 120:
            _batch_summary = _batch_summary[:117] + "..."
        if len(state["questions"]) > 1:
            _batch_summary += f" (+{len(state['questions']) - 1} more questions)"
        self._fire_attention_signals(_batch_summary)

        self._paint_now()

        _last_countdown_refresh = _time.monotonic()
        while True:
            try:
                result = response_queue.get(timeout=1)
                self._clarify_deadline = None
                if isinstance(result, dict):
                    return {"answers": result}
                # Cancel path (Ctrl+C teardown) posts a plain string — pass
                # it through so the tool core resolves the batch empty.
                return result
            except queue.Empty:
                if self._clarify_deadline is not None:
                    remaining = self._clarify_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                now = _time.monotonic()
                if now - _last_countdown_refresh >= 1.0:
                    _last_countdown_refresh = now
                    self._paint_now()

        # Timed out — keep the answers locked so far and flag the timeout.
        partial = dict(state["answers"])
        self._clarify_state = None
        self._clarify_freetext = False
        self._clarify_deadline = None
        self._clarify_multi_base = None
        self._paint_now()
        _cprint(f"\n{_DIM}(clarify timed out after {timeout}s — locked answers returned){_RST}")
        return {"answers": partial, "timed_out": True}

    def _sudo_password_callback(self) -> str:
        """
        Prompt for sudo password through the prompt_toolkit UI.
        
        Called from the agent thread when a sudo command is encountered.
        Uses the same clarify-style mechanism: sets UI state, waits on a
        queue for the user's response via the Enter key binding.
        """
        import time as _time

        # Honor approvals.timeout for sudo too — same reasoning as the
        # main approval prompt.  Previously hardcoded to 45s.
        try:
            from tools.approval import _get_approval_timeout
            timeout = _get_approval_timeout()
        except Exception:
            timeout = 300
        response_queue = queue.Queue()

        self._capture_modal_input_snapshot()
        self._sudo_state = {
            "response_queue": response_queue,
        }
        self._sudo_deadline = _time.monotonic() + timeout

        # Bell + native notification — sudo prompts are easy to miss.
        self._fire_attention_signals("Sudo password requested")

        # Modal prompt — paint immediately, bypassing the throttle/resize guard
        # so the prompt can't be dropped and time out unseen (#41098).
        self._paint_now()

        while True:
            try:
                result = response_queue.get(timeout=1)
                self._sudo_state = None
                self._sudo_deadline = 0
                self._restore_modal_input_snapshot()
                self._paint_now()
                if result:
                    _cprint(f"\n{_DIM}  ✓ Password received (cached for session){_RST}")
                else:
                    _cprint(f"\n{_DIM}  ⏭ Skipped{_RST}")
                return result
            except queue.Empty:
                remaining = self._sudo_deadline - _time.monotonic()
                if remaining <= 0:
                    break
                self._paint_now()

        self._sudo_state = None
        self._sudo_deadline = 0
        self._restore_modal_input_snapshot()
        self._paint_now()
        _cprint(f"\n{_DIM}  ⏱ Timeout — continuing without sudo{_RST}")
        return ""

    def _approval_callback(self, command: str, description: str,
                           *, allow_permanent: bool = True,
                           allow_session: bool = True,
                           smart_denied: bool = False) -> str:
        """
        Prompt for dangerous command approval through the prompt_toolkit UI.

        Called from the agent thread. Shows a selection UI similar to clarify
        with choices: once / session / always / deny. Smart DENY owner
        overrides show only once / deny, as do gates that re-ask every time
        (allow_session=False). When allow_permanent is False for another
        reason (for example tirith), only 'always' is hidden.
        Long commands also get a 'view' option so the full command can be
        expanded before deciding.

        Uses _approval_lock to serialize concurrent requests (e.g. from
        parallel delegation subtasks) so each prompt gets its own turn
        and the shared _approval_state / _approval_deadline aren't clobbered.
        """
        import time as _time

        with self._approval_lock:
            # Honor approvals.timeout from config (default 300s).  Previously
            # this was hardcoded to 60s, which silently overrode the user's
            # config setting.  See `_get_approval_timeout()` in
            # tools/approval.py for the single source of truth.
            try:
                from tools.approval import _get_approval_timeout
                timeout = _get_approval_timeout()
            except Exception:
                timeout = 300
            response_queue = queue.Queue()

            self._approval_state = {
                "command": command,
                "description": description,
                "choices": self._approval_choices(
                    command,
                    allow_permanent=allow_permanent,
                    allow_session=allow_session,
                    smart_denied=smart_denied,
                ),
                "selected": 0,
                "response_queue": response_queue,
            }
            self._approval_deadline = _time.monotonic() + timeout

            # Bell + native notification so the user notices the prompt
            # even when they're in a different window or SSH'd in from
            # another machine.  Gated by approvals.bell_on_prompt /
            # approvals.notify_on_prompt (both default True).
            _approval_summary = description if description else "Approval required"
            # Trim long descriptions for the banner.
            if len(_approval_summary) > 120:
                _approval_summary = _approval_summary[:117] + "..."
            self._fire_attention_signals(f"Approval needed: {_approval_summary}")

            # Modal prompt — paint immediately, bypassing the throttle/resize
            # guard. A throttled paint here can be silently dropped (250ms
            # window collision or in-flight resize), leaving the panel unseen so
            # the command is denied on timeout without the user ever seeing it
            # (#41098). The countdown refreshes below paint the same way.
            self._paint_now()

            _last_countdown_refresh = _time.monotonic()
            while True:
                try:
                    result = response_queue.get(timeout=1)
                    self._approval_state = None
                    self._approval_deadline = 0
                    self._paint_now()
                    _outcome_labels = {
                        "once": "allowed once",
                        "session": "allowed for session",
                        "always": "added to allowlist",
                        "deny": "denied",
                    }
                    self._persist_prompt_summary(
                        "⚠", "Approval", command,
                        _outcome_labels.get(result, str(result)),
                    )
                    return result
                except queue.Empty:
                    remaining = self._approval_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    now = _time.monotonic()
                    if now - _last_countdown_refresh >= 1.0:
                        _last_countdown_refresh = now
                        self._paint_now()

            self._approval_state = None
            self._approval_deadline = 0
            self._paint_now()
            _cprint(f"\n{_DIM}  ⏱ Timeout — denying command{_RST}")
            self._persist_prompt_summary(
                "⚠", "Approval", command, "timed out (no response)",
            )
            return "timeout"

    def _get_approval_display_fragments(self):
        """Render the dangerous-command approval panel for the prompt_toolkit UI.

        Layout priority: title + command + choices must always render, even if
        the terminal is short or the description is long. Description is placed
        at the bottom of the panel and gets truncated to fit the remaining row
        budget. This prevents HSplit from clipping approve/deny off-screen when
        tirith findings produce multi-paragraph descriptions or when the user
        runs in a compact terminal pane.
        """
        state = self._approval_state
        if not state:
            return []

        def _panel_box_width(title_text: str, content_lines: list[str], min_width: int = 46, max_width: int = 76) -> int:
            term_cols = shutil.get_terminal_size((100, 20)).columns
            longest = max([HermesCLI._panel_cwidth(title_text)] + [HermesCLI._panel_cwidth(line) for line in content_lines] + [min_width - 4])
            inner = min(max(longest + 4, min_width - 2), max_width - 2, max(24, term_cols - 6))
            return inner + 2

        def _wrap_panel_text(text: str, width: int, subsequent_indent: str = "") -> list[str]:
            wrapped = textwrap.wrap(
                text,
                width=max(8, width),
                replace_whitespace=False,
                drop_whitespace=False,
                subsequent_indent=subsequent_indent,
            )
            return wrapped or [""]

        def _append_panel_line(lines, border_style: str, content_style: str, text: str, box_width: int) -> None:
            inner_width = max(0, box_width - 2)
            lines.append((border_style, "│ "))
            lines.append((content_style, HermesCLI._panel_ljust(text, inner_width)))
            lines.append((border_style, " │\n"))

        def _append_blank_panel_line(lines, border_style: str, box_width: int) -> None:
            lines.append((border_style, "│" + (" " * box_width) + "│\n"))

        command = state["command"]
        description = state["description"]
        choices = state["choices"]
        selected = state.get("selected", 0)
        show_full = state.get("show_full", False)

        title = "⚠️  Dangerous Command"
        cmd_display = command
        choice_labels = {
            "once": "Allow once",
            "session": "Allow for this session",
            "always": "Add to permanent allowlist",
            "deny": "Deny",
            "view": "Show full command",
        }

        preview_lines = _wrap_panel_text(description, 60)
        preview_lines.extend(_wrap_panel_text(cmd_display, 60))
        for i, choice in enumerate(choices):
            prefix = '❯ ' if i == selected else '  '
            preview_lines.extend(_wrap_panel_text(
                f"{prefix}{choice_labels.get(choice, choice)}",
                60,
                subsequent_indent="  ",
            ))

        box_width = _panel_box_width(title, preview_lines)
        inner_text_width = max(8, box_width - 2)

        # Pre-wrap the mandatory content — command + choices must always render.
        cmd_wrapped = _wrap_panel_text(cmd_display, inner_text_width)
        if not show_full and "view" in choices and len(cmd_wrapped) > 4:
            cmd_wrapped = cmd_wrapped[:3] + _wrap_panel_text(
                "… (choose Show full command)",
                inner_text_width,
            )

        # (choice_index, wrapped_line) so we can re-apply selected styling below
        choice_wrapped: list[tuple[int, str]] = []
        for i, choice in enumerate(choices):
            label = choice_labels.get(choice, choice)
            # Show number prefix for quick selection (1-9 for items 1-9, 0 for 10th item)
            if i < 9:
                num_prefix = str(i + 1)
            elif i == 9:
                num_prefix = '0'
            else:
                num_prefix = ' '  # No number for items beyond 10th
            if i == selected:
                prefix = f'❯ {num_prefix}. '
            else:
                prefix = f'  {num_prefix}. '
            for wrapped in _wrap_panel_text(f"{prefix}{label}", inner_text_width, subsequent_indent="    "):
                choice_wrapped.append((i, wrapped))

        # Budget vertical space so HSplit never clips the command or choices.
        # Panel chrome (full layout with separators):
        #   top border + title + blank_after_title
        #   + blank_between_cmd_choices + bottom border = 5 rows.
        # In tight terminals we collapse to:
        #   top border + title + bottom border = 3 rows (no blanks).
        #
        # reserved_below: rows consumed below the approval panel by the
        # spinner/tool-progress line, status bar, input area, separators, and
        # prompt symbol. Measured at ~6 rows during live PTY approval prompts;
        # budget 6 so we don't overestimate the panel's room.
        term_rows = shutil.get_terminal_size((100, 24)).lines
        chrome_full = 5
        chrome_tight = 3
        reserved_below = 6

        available = max(0, term_rows - reserved_below)
        mandatory_full = chrome_full + len(cmd_wrapped) + len(choice_wrapped)

        # If the full-chrome panel doesn't fit, drop the separator blanks.
        # This keeps the command and every choice on-screen in compact terminals.
        use_compact_chrome = mandatory_full > available
        chrome_rows = chrome_tight if use_compact_chrome else chrome_full

        # If the command itself is too long to leave room for choices (e.g. user
        # hit "view" on a multi-hundred-character command), truncate it so the
        # approve/deny buttons still render. Keep at least 1 row of command.
        max_cmd_rows = max(1, available - chrome_rows - len(choice_wrapped))
        if len(cmd_wrapped) > max_cmd_rows:
            keep = max(1, max_cmd_rows - 1) if max_cmd_rows > 1 else 1
            cmd_wrapped = cmd_wrapped[:keep] + _wrap_panel_text(
                "… (command truncated — use /logs or /debug for full text)",
                inner_text_width,
            )

        # Allocate any remaining rows to description. The extra -1 in full mode
        # accounts for the blank separator between choices and description.
        mandatory_no_desc = chrome_rows + len(cmd_wrapped) + len(choice_wrapped)
        desc_sep_cost = 0 if use_compact_chrome else 1
        available_for_desc = available - mandatory_no_desc - desc_sep_cost
        # Even on huge terminals, cap description height so the panel stays compact.
        available_for_desc = max(0, min(available_for_desc, 10))

        desc_wrapped = _wrap_panel_text(description, inner_text_width) if description else []
        if available_for_desc < 1 or not desc_wrapped:
            desc_wrapped = []
        elif len(desc_wrapped) > available_for_desc:
            keep = max(1, available_for_desc - 1)
            desc_wrapped = desc_wrapped[:keep] + ["… (description truncated)"]

        # Render: title → command → choices → description (description last so
        # any remaining overflow clips from the bottom of the least-critical
        # content, never from the command or choices). Use compact chrome (no
        # blank separators) when the terminal is tight.
        lines = []
        lines.append(('class:approval-border', '╭' + ('─' * box_width) + '╮\n'))
        _append_panel_line(lines, 'class:approval-border', 'class:approval-title', title, box_width)
        if not use_compact_chrome:
            _append_blank_panel_line(lines, 'class:approval-border', box_width)

        for wrapped in cmd_wrapped:
            _append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', wrapped, box_width)
        if not use_compact_chrome:
            _append_blank_panel_line(lines, 'class:approval-border', box_width)

        for i, wrapped in choice_wrapped:
            style = 'class:approval-selected' if i == selected else 'class:approval-choice'
            _append_panel_line(lines, 'class:approval-border', style, wrapped, box_width)

        if desc_wrapped:
            if not use_compact_chrome:
                _append_blank_panel_line(lines, 'class:approval-border', box_width)
            for wrapped in desc_wrapped:
                _append_panel_line(lines, 'class:approval-border', 'class:approval-desc', wrapped, box_width)

        lines.append(('class:approval-border', '╰' + ('─' * box_width) + '╯\n'))
        return lines

    def _run_on_app_loop(self, fn) -> None:
        """Execute ``fn`` on the prompt_toolkit event loop and block until done.

        Buffer mutations from a non-main thread (process_loop dispatches
        slash commands like /clear off the UI thread) corrupt prompt_toolkit's
        input pipeline and freeze every keybinding — including Ctrl+C.
        Route the mutation through the app's loop via call_soon_threadsafe
        and wait on a threading.Event so the caller still sees synchronous
        semantics.
        """
        import threading as _threading
        app = getattr(self, "_app", None)
        loop = getattr(app, "loop", None) if app is not None else None
        on_main = _threading.current_thread() is _threading.main_thread()
        if loop is None or not getattr(loop, "is_running", lambda: False)() or on_main:
            try:
                fn()
            except Exception:
                pass
            return
        done = _threading.Event()
        def _runner():
            try:
                fn()
            except Exception:
                pass
            finally:
                done.set()
        try:
            loop.call_soon_threadsafe(_runner)
        except Exception:
            try:
                fn()
            except Exception:
                pass
            return
        done.wait(timeout=2.0)

    def _capture_modal_input_snapshot(self) -> None:
        """Temporarily clear the input buffer and save the user's in-progress draft."""
        if self._modal_input_snapshot is not None or not getattr(self, "_app", None):
            return
        def _do():
            try:
                buf = self._app.current_buffer
                self._modal_input_snapshot = {
                    "text": buf.text,
                    "cursor_position": buf.cursor_position,
                }
                buf.reset()
            except Exception:
                self._modal_input_snapshot = None
        self._run_on_app_loop(_do)

    def _restore_modal_input_snapshot(self) -> None:
        """Restore any draft text that was present before a modal prompt opened."""
        snapshot = self._modal_input_snapshot
        self._modal_input_snapshot = None
        if not snapshot or not getattr(self, "_app", None):
            return
        def _do():
            try:
                buf = self._app.current_buffer
                buf.text = snapshot.get("text", "")
                buf.cursor_position = min(snapshot.get("cursor_position", 0), len(buf.text))
            except Exception:
                pass
        self._run_on_app_loop(_do)

    @staticmethod
    def _combine_interrupt_parts(parts: list):
        """Merge queued interrupt messages into one ``_pending_input`` payload.

        Each part is either a plain ``str`` or a ``(text, [Path, ...])`` tuple
        (the latter when the user attached one or more images before hitting
        Enter). Text is joined with newlines; images from every part are
        concatenated in order. Returns a ``(text, images)`` tuple when any
        images are present, otherwise a plain ``str`` — exactly the two shapes
        the process loop's ``_pending_input`` consumer already unpacks.

        This is the structural replacement for the old ``"\\n".join(parts)``,
        which raised ``TypeError`` the instant a part was a tuple (image
        attached), silently dropping the interrupting prompt.
        """
        texts: list = []
        images: list = []
        for part in parts:
            if isinstance(part, tuple):
                p_text = part[0] if len(part) > 0 else ""
                p_images = part[1] if len(part) > 1 and part[1] else []
                if p_text:
                    texts.append(p_text)
                if p_images:
                    images.extend(p_images)
            elif part:
                texts.append(part)
        combined_text = "\n".join(texts)
        if images:
            return (combined_text, images)
        return combined_text


    def _tui_signal_handler(self, signum, frame):
        """SIGHUP/SIGTERM -> graceful shutdown.

        The agent is hard-interrupted first so its daemon thread can kill the tool's
        setsid subprocess group before the main thread unwinds (else an orphan child).
        ``logger.debug`` is guarded: logging is not reentrant-safe and a shutdown race
        can raise ``KeyError`` inside the handler, bypassing prompt_toolkit's unwind.
        """
        with suppress(Exception):
            logger.debug("Received signal %s, triggering graceful shutdown", signum)
        # Arm the backstop IMMEDIATELY: if the unwind wedges, _run_cleanup never arms its own.
        # Shutdown intent is now unambiguous — arm the exit backstop IMMEDIATELY, before the graceful unwind
        # below. If any step of that unwind wedges (main thread parked in a syscall, prompt_toolkit teardown
        # never returning), _run_cleanup never runs and would never arm its own watchdog — leaving a "dead"
        # CLI alive for minutes (#65998 class).
        # Arm the exit backstop now that shutdown intent is unambiguous — covers wedges in the unwind below
        # that would otherwise leave the process alive with no watchdog (#65998 class).
        _arm_exit_watchdog_on_shutdown_signal()
        if self._agent_running:
            _interrupt_agent_for_signal(self.agent, signum)
        # Prefer app.exit() over raising KeyboardInterrupt: a KBI from a signal handler
        # lands in a pt Task ("Unhandled exception in event loop" + "Press ENTER to
        # continue..."); call_soon_threadsafe lets the loop unwind normally.
        try:
            from prompt_toolkit.application.current import get_app_or_none
            _app = get_app_or_none()
            _loop = getattr(_app, "loop", None)
            if _loop is not None:
                _loop.call_soon_threadsafe(_app.exit)
                return  # clean unwind — no traceback, no ENTER pause
        except Exception:
            pass
        raise KeyboardInterrupt()  # fallback for non-prompt_toolkit contexts

    def _build_tui_layout_children(
        self,
        *,
        sudo_widget,
        secret_widget,
        approval_widget,
        slash_confirm_widget=None,
        clarify_widget,
        model_picker_widget=None,
        reasoning_picker_widget=None,
        command_palette_widget=None,
        spinner_widget=None,
        todo_board_widget=None,
        spacer,
        status_bar,
        input_rule_top,
        image_bar,
        input_area,
        input_rule_bot,
        voice_status_bar,
        completions_menu,
    ) -> list:
        """Assemble the ordered list of children for the root ``HSplit``.

        Wrapper CLIs typically override ``_get_extra_tui_widgets`` instead of
        this method.  Override this only when you need full control over widget
        ordering.

        ``_subagent_dock_widget`` follows the same "built elsewhere, hung off
        ``self``" pattern the stash panel and pet widget use: ``install_dock``
        populates it before the layout is built, and a direct call on a CLI that
        never built a layout simply filters the missing ones out.
        """
        return [
            item for item in [
                Window(height=0),
                sudo_widget,
                secret_widget,
                approval_widget,
                slash_confirm_widget,
                clarify_widget,
                model_picker_widget,
                reasoning_picker_widget,
                command_palette_widget,
                todo_board_widget,
                spinner_widget,
                spacer,
                *self._get_extra_tui_widgets(),
                getattr(self, "_pet_widget", None),
                getattr(self, "_stash_panel_widget", None),
                getattr(self, "_subagent_dock_widget", None),
                status_bar,
                input_rule_top,
                image_bar,
                input_area,
                input_rule_bot,
                voice_status_bar,
                completions_menu,
            ] if item is not None
        ]

    def _tui_print_startup(self):
        """Startup output: light-mode probe, banner, advisories, resume/welcome lines, tips."""
        with suppress(Exception):  # light-mode probe before pt grabs the tty (cached)
            _detect_light_mode()
        # Scroll the cursor to the last row so banner, responses and prompt pin to the bottom.
        with suppress(Exception):
            _term_lines = shutil.get_terminal_size().lines
            if _term_lines > 2:
                print("\n" * (_term_lines - 1), end="", flush=True)

        self.show_banner()
        self._show_security_advisories()
        self._show_browser_backend_notice()

        # First-run: an unconfigured install routes into provider onboarding instead of
        # a chat that spins ~30s and fails with a provider-specific error. TTY only.
        try:
            if sys.stdin.isatty() and not self._runtime_credentials_ready():
                self._offer_first_run_setup()
        except Exception:
            logger.debug("first-run setup offer failed", exc_info=True)

        if self._resumed and self._preload_resumed_session():
            self._display_resumed_history()

        _welcome_skin = None  # stays None when the skin engine failed
        _welcome_text = "Welcome to Hermes Agent! Type your message or /help for commands."
        _welcome_color = "#FFF8DC"
        try:
            from hermes_cli.skin_engine import get_active_skin
            _welcome_skin = get_active_skin()
            _welcome_text = _welcome_skin.get_branding("welcome", _welcome_text)
            _welcome_color = _welcome_skin.get_color("banner_text", _welcome_color)
        except Exception:
            pass
        self._console_print(f"[{_welcome_color}]{_welcome_text}[/]")

        self._tui_startup_prewarm_and_warnings(_welcome_skin)
        self._print_random_tip()

        self._tui_startup_background_maintenance()
        # Before the background preload is folded in (at agent init), show the REQUESTED names.
        _skills_for_line = self.preloaded_skills or list(self._preload_skills_requested or [])
        if _skills_for_line and not self._startup_skills_line_shown:
            self._console_print(f"[bold {_accent_hex()}]Activated skills:[/] {', '.join(_skills_for_line)}")
            self._startup_skills_line_shown = True
        self._console_print()

    def _tui_startup_prewarm_and_warnings(self, _welcome_skin):
        """Idle-window prewarms (picker cache, agent runtime imports) plus the redaction-off and OpenClaw-residue banners."""
        # Warm the /model picker cache off-thread (else its first open blocks ~1-2s).
        with suppress(Exception):
            from hermes_cli.model_switch_providers import prewarm_picker_cache_async
            prewarm_picker_cache_async()

        # Pre-import the agent runtime (~1.5s: run_agent + OpenAI SDK) off-thread; the import
        # lock makes an early submit block on the remaining work rather than redo it.
        # Skipped when Termux defers agent startup on purpose.
        if os.environ.get("HERMES_DEFER_AGENT_STARTUP") != "1":
            def _prewarm_agent_runtime() -> None:
                try:
                    import run_agent  # noqa: F401  (imports model_tools + tool registry)
                    import openai  # noqa: F401
                except Exception:
                    logger.debug("agent runtime pre-import failed", exc_info=True)

            threading.Thread(target=_prewarm_agent_runtime, name="agent-runtime-prewarm", daemon=True).start()

        # Redaction is ON by default; be loud when the operator turned it off.
        with suppress(Exception):
            # The redactor snapshots its state at import time so any toggle now won't affect the running
            # process — we just want the operator to see that they're running without the safety net. See
            # #17691.
            _redact_raw = os.getenv("HERMES_REDACT_SECRETS", "true")
            if _redact_raw.lower() not in {"1", "true", "yes", "on"}:
                self._console_print(
                    "[bold red]⚠  Secret redaction is DISABLED[/] "
                    f"(HERMES_REDACT_SECRETS={_redact_raw}). "
                    "API keys and tokens may appear verbatim in chat output, "
                    "session JSONs, and logs. Set "
                    "[cyan]security.redact_secrets: true[/] in config.yaml "
                    "to re-enable."
                )
        # One-time banner when ~/.openclaw/ is left over from a migration.
        try:
            from agent.onboarding import (
                OPENCLAW_RESIDUE_FLAG, detect_openclaw_residue, is_seen, mark_seen, openclaw_residue_hint_cli,
            )
            if not is_seen(self.config, OPENCLAW_RESIDUE_FLAG) and detect_openclaw_residue():
                try:
                    _resid_color = _welcome_skin.get_color("banner_dim", "#B8860B")
                except Exception:
                    _resid_color = "#B8860B"
                self._console_print(f"[{_resid_color}]{openclaw_residue_hint_cli()}[/]")
                try:
                    from hermes_cli.config import get_config_path as _get_cfg_path_resid
                    mark_seen(_get_cfg_path_resid(), OPENCLAW_RESIDUE_FLAG)
                except Exception:
                    pass  # banner fires again next session
        except Exception:
            pass

    def _tui_startup_background_maintenance(self):
        """Best-effort startup passes: curator skill maintenance, personal + org skill sync."""
        with suppress(Exception):
            from agent.curator import maybe_run_curator
            maybe_run_curator(
                idle_for_seconds=float("inf"),  # CLI startup = fully idle
                on_summary=lambda msg: self._console_print(f"[dim #6b7684]💾 {msg}[/]"),
            )

        # Skill sync (personal, then org-shared): inert unless the access gate is open
        # and a sync base URL is configured. The org pull is gated on a real org role on
        # the token (only issued for multi-member orgs), so a solo account never hits
        # the network here. Both fail-quiet.
        try:
            from tools.skills_sync_client import maybe_pull_skills
            from tools.skills_sync_client_org import maybe_pull_org_skills
        except Exception:
            return
        for pull in (maybe_pull_skills, maybe_pull_org_skills):
            with suppress(Exception):
                pull()

    def _tui_build_application(self, layout, kb, style):
        """Construct the prompt_toolkit Application for the REPL."""
        _cpr_disabled_output = _select_classic_cli_pt_output(sys.stdout)

        # Kitty placeholders encode the image id in exact foreground RGB, so the whole app
        # runs 24-bit there. ColorDepth is imported lazily for tests that stub prompt_toolkit.
        extra_kw = {}
        if pet_render.supports_kitty_placeholders():
            from prompt_toolkit.output import ColorDepth

            extra_kw["color_depth"] = ColorDepth.DEPTH_24_BIT
        if _cpr_disabled_output is not None:
            extra_kw["output"] = _cpr_disabled_output
        if _STEADY_CURSOR is not None:
            extra_kw["cursor"] = _STEADY_CURSOR
        if EditingMode is not None:
            # Vi editing mode when display.vim_mode is on.
            # EMACS is prompt_toolkit's own default, so non-opted-in behaviour is unchanged.
            extra_kw["editing_mode"] = EditingMode.VI if self._vim_mode else EditingMode.EMACS
        return Application(
            layout=layout,
            key_bindings=kb,
            style=style,
            full_screen=False,
            mouse_support=False,
            # 0 (default) avoids fighting terminal auto-scroll in non-fullscreen mode.
            refresh_interval=float(CLI_CONFIG.get("display", {}).get("cli_refresh_interval", 0)),
            # Erase the bottom chrome on exit instead of freezing a copy into scrollback.
            # Without this, prompt_toolkit's render_as_done teardown repaints the chrome one last time and
            # leaves it stranded above the exit summary — so a dead status bar + empty prompt sit between
            # the conversation transcript and the "Resume this session" block, and stack with the next
            # session's UI on resume (#38252). The actual conversation transcript is printed through
            # patch_stdout into normal scrollback and is unaffected; only the managed chrome is erased.
            # Applies to every exit path (/exit, /quit, EOF, Ctrl+C).
            erase_when_done=True,
            **extra_kw,
        )

    def _tui_install_signal_handlers(self):
        """SIGTERM/SIGHUP -> graceful shutdown; Windows absorbs SIGINT (see body)."""
        try:
            import signal as _signal
            _signal.signal(_signal.SIGTERM, self._tui_signal_handler)
            if hasattr(_signal, 'SIGHUP'):
                _signal.signal(_signal.SIGHUP, self._tui_signal_handler)

            # Windows: absorb SIGINT. Win32 delivers spurious CTRL_C_EVENT when children spawn
            # from background threads, which would unwind app.run() mid-turn. Real Ctrl+C is
            # bound by prompt_toolkit. Never call agent.interrupt() here (fake user message).
            if sys.platform == "win32":
                _signal.signal(_signal.SIGINT, lambda signum, frame: None)
        except Exception:
            pass  # restricted environments

    def _tui_stdin_usable(self) -> bool:
        """Validate fd 0 before prompt_toolkit starts; on macOS fall back to a select() loop when kqueue can't watch it (uv-managed Python)."""
        try:
            os.fstat(0)
        except OSError:
            print(
                "Error: stdin (fd 0) is not available.\n"
                "This can happen with certain Python installations (e.g. uv-managed cPython on macOS).\n"
                "Try reinstalling Python via pyenv or Homebrew, then re-run: hermes setup"
            )
            # FORK order: curator (near-instant) then the memory-confirm UI, then the exit
            # summary BEFORE cleanup — _run_cleanup() can block for tens of seconds and is
            # guillotined by the exit watchdog, which would swallow the cost report + resume
            # hint entirely. Both helpers fold their LLM spend into session_estimated_cost_usd
            # first so the printed total includes it.
            _fold_curator_cost_before_exit()
            _run_memory_confirm_before_exit()
            self._print_exit_summary()
            _run_cleanup()
            return False
        if sys.platform == "darwin":
            import selectors as _selectors
            try:
                if hasattr(_selectors, "KqueueSelector"):
                    _kq = _selectors.KqueueSelector()
                    try:
                        _kq.register(0, _selectors.EVENT_READ)
                        _kq.unregister(0)
                    finally:
                        _kq.close()
            except (OSError, ValueError, KeyError):
                import asyncio as _aio_probe

                class _SelectEventLoopPolicy(_aio_probe.DefaultEventLoopPolicy):
                    def new_event_loop(self):
                        return _aio_probe.SelectorEventLoop(_selectors.SelectSelector())

                _aio_probe.set_event_loop_policy(_SelectEventLoopPolicy())
        return True

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
            _run_cleanup()
            self._print_exit_summary()
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

    def _tui_shutdown(self):
        """Teardown after the app exits: interrupt agent, stop voice/pet, persist + close session, cleanup, exit summary."""
        self._should_exit = True
        self._pet_stop_anim()
        # Without this line the terminal sits silent through the whole cleanup window.
        with suppress(Exception):
            print(f"{_DIM}Shutting down… (finalizing session){_RST}", flush=True)
        if self.agent and self._agent_running:
            with suppress(Exception):
                request_hard_interrupt(self.agent)
        if self._voice_recorder:
            with suppress(Exception):
                self._voice_recorder.shutdown()
            self._voice_recorder = None
        with suppress(Exception):
            from tools.voice_mode import cleanup_temp_recordings
            cleanup_temp_recordings()
        from agent.vault_backends.unlock import (lock as _vault_lock, set_code_prompt_callback,
                                                 set_save_login_prompt_callback, set_unlock_prompt_callback)
        for _unset in (set_sudo_password_callback, set_approval_callback, set_secret_capture_callback,
                       set_unlock_prompt_callback, set_save_login_prompt_callback, set_code_prompt_callback):
            _unset(None)
        _vault_lock()  # session tokens for external password managers die with the session
        # On SIGHUP/SIGTERM the agent thread may be reaped before its own persistence runs.
        self._persist_active_session_before_close()

        if self._session_db and self.agent:
            try:
                self._session_db.end_session(self.agent.session_id, "cli_close")
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Could not close session in DB: %s", e)
            if not self._delete_session_on_exit:
                # Drop the empty row of a start-and-quit session so /resume stays clean.
                try:
                    self._discard_session_if_empty(self.agent.session_id)
                except (Exception, KeyboardInterrupt) as e:
                    logger.debug("Could not prune empty session: %s", e)
            else:
                # /exit --delete: remove transcripts + SQLite history.
                try:
                    _sid = self.agent.session_id
                    if self._session_db.delete_session(_sid, sessions_dir=get_hermes_home() / "sessions"):
                        _cprint(f"  {_DIM}✓ Session {_escape(_sid)} deleted{_RST}")
                    else:
                        _cprint(f"  {_DIM}✗ Session {_escape(_sid)} not found for deletion{_RST}")
                except (Exception, KeyboardInterrupt) as e:
                    logger.debug("Could not delete session on exit: %s", e)
        # run_conversation() fires on_session_end on normal completion; only fire here mid-turn.
        if self.agent and self._agent_running:
            _invoke_interrupted_session_end(self.agent, self.agent.session_id, "shutdown")
        _run_cleanup()
        self._print_exit_summary()
        self._release_active_session()


def _int_or(value, default: int) -> int:
    """``int(value)``, or ``default`` when it does not parse."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _interrupt_agent_for_signal(agent, signum) -> None:
    """Hard-interrupt ``agent`` for a shutdown signal, then sleep ``HERMES_SIGTERM_GRACE`` (1.5 s).

    The grace lets the agent thread kill the tool's setsid subprocess group before the
    main thread unwinds (else an orphan child). Never raises.
    """
    try:
        if agent is not None:
            request_hard_interrupt(agent, f"received signal {signum}")
            _grace = _float_env("HERMES_SIGTERM_GRACE", 1.5)
            if _grace > 0:
                time.sleep(_grace)
    except Exception:
        pass  # never block signal handling


def _run_kanban_goal_loop_q(cli: "HermesCLI", first_response: str) -> None:
    """Drive a kanban goal_mode worker through ``goals.run_kanban_goal_loop`` after its first turn.

    The caller swallows all errors: a broken loop must never wedge a worker.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return
    raw_run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    worker_run_id = _int_or(raw_run_id, None) if raw_run_id else None
    if raw_run_id and worker_run_id is None:
        logger.warning("invalid HERMES_KANBAN_RUN_ID=%r", raw_run_id)

    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli.goals import run_kanban_goal_loop as _run_loop, DEFAULT_MAX_TURNS as _DEF_TURNS

    # Goal text = title + body (the acceptance criteria the judge evaluates against).
    with _kbc.connect_closing() as conn:
        task = _kb.get_task(conn, task_id)
    if task is None:
        return

    goal_text = "\n\n".join(p for p in (task.title or "", task.body) if p).strip()
    if not goal_text:
        return

    def _run_turn(prompt: str) -> str:
        result = cli.agent.run_conversation(user_message=prompt, conversation_history=cli.conversation_history)
        _sync_cli_session_id_from_agent(cli)
        resp = result.get("final_response", "") if isinstance(result, dict) else str(result)
        if resp:
            print(resp)
        return resp or ""

    def _task_status() -> "str | None":
        with _kbc.connect_closing() as c:
            return _kb.goal_run_status(c, task_id, worker_run_id)

    def _block(reason: str) -> None:
        with _kbc.connect_closing() as c:
            _kb.block_task(c, task_id, reason=reason, expected_run_id=worker_run_id)

    _run_loop(
        task_id=task_id, goal_text=goal_text, run_turn=_run_turn, task_status_fn=_task_status, block_fn=_block,
        max_turns=task.goal_max_turns or _DEF_TURNS, first_response=first_response or "",
        log=lambda m: logger.info("%s", m),
    )


def _sync_cli_session_id_from_agent(cli) -> None:
    """Keep ``cli.session_id`` in sync when mid-run compression rotated the agent's session."""
    if getattr(cli.agent, "session_id", None) and cli.agent.session_id != cli.session_id:
        cli.session_id = cli.agent.session_id


def _run_quiet_single_query(cli, effective_query):
    """Quiet (-Q) one-shot turn: run, print the response (stderr for errors/session_id), then sys.exit with the automation exit code.
    HERMES_TURN_AUTHOR (set only by a bot-to-bot dispatcher) is consumed here so tool subprocesses do not inherit it.
    Nested Bot Mode notifies bind this session's key (not the dispatcher's) and resume in-process
    before stdout is printed, so a teammate reply is the quiet run's final answer rather than a
    stranded receipt."""
    from agent.interrupt_compat import _accepts_keyword
    from agent.turn_author import take_turn_author_from_env
    from hermes_cli.quiet_single_query import (
        bind_quiet_session_key, continue_quiet_notify_completions, quiet_notify_linger_seconds,
    )

    author = take_turn_author_from_env()
    author_kwargs = {"turn_author": author} if author is not None and _accepts_keyword(cli.agent.run_conversation, "turn_author") else {}
    with bind_quiet_session_key(getattr(cli, "session_id", "") or "default"):
        try:
            result = cli.agent.run_conversation(
                user_message=effective_query, conversation_history=cli.conversation_history, **author_kwargs,
            )
        except KeyboardInterrupt:
            _emit_interrupted_session_end(cli, reason="keyboard_interrupt")
            print(f"\nsession_id: {cli.session_id}", file=sys.stderr)
            sys.exit(130)
        # The exit line below reports session_id to stderr for automation wrappers;
        # without this sync it would point at the ended parent after compression.
        _sync_cli_session_id_from_agent(cli)
        if isinstance(result, dict) and not result.get("failed"):
            history = result.get("messages") or cli.conversation_history

            def _follow_up(text):
                nonlocal history
                follow = cli.agent.run_conversation(
                    user_message=text, conversation_history=history, **author_kwargs,
                )
                if isinstance(follow, dict) and follow.get("messages"):
                    history = follow["messages"]
                # Same sync contract as the main turn: a compression rotation during a
                # follow-up must not leave a stale id on the exit line / drain key.
                _sync_cli_session_id_from_agent(cli)
                return follow

            # One shared linger budget for the whole run: the loop below and the later
            # _wait_for_oneshot_background_completions pass must not each wait the full
            # oneshot_completion_wait_seconds on the same stuck notify_on_complete child.
            # Flagged after the loop (finally-equivalent): the wait is the loop's first
            # statement, so anything raising past that point has consumed budget the
            # finalize pass must not re-wait.
            try:
                continued = continue_quiet_notify_completions(
                    getattr(cli, "session_id", "") or "",
                    _follow_up,
                    owns_event=getattr(cli, "_owns_process_notification", None),
                    linger_budget=quiet_notify_linger_seconds(),
                )
            finally:
                cli._quiet_notify_linger_done = True
            if isinstance(continued, dict):
                result = continued
        response = result.get("final_response", "") if isinstance(result, dict) else str(result)
    # Surface backend errors that produced no visible output (e.g. invalid model slug
    # -> provider 4xx) on stderr so piped stdout stays clean.
    if (
        not response and isinstance(result, dict) and result.get("error")
        and (result.get("failed") or result.get("partial"))
    ):
        print(f"Error: {result['error']}", file=sys.stderr)
    elif response:
        print(response)

    # Kanban goal_mode: keep working in THIS session until a judge agrees the card is
    # done, the worker terminates it, or the turn budget runs out (sticky block).
    if os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1":
        try:
            _run_kanban_goal_loop_q(cli, response)
        except Exception as _goal_exc:
            logger.debug("kanban goal loop failed: %s", _goal_exc)

    print(f"\nsession_id: {cli.session_id}", file=sys.stderr)

    # Exit code 0/1 for automation wrappers. Kanban workers that failed purely on
    # rate-limit/billing exit with the EX_TEMPFAIL sentinel so the dispatcher releases
    # the task without counting a failure (a quota window must not trip the breaker).
    _exit_code = 0
    if isinstance(result, dict) and result.get("failed"):
        _exit_code = 1
        if os.environ.get("HERMES_KANBAN_TASK") and result.get("failure_reason") in ("rate_limit", "billing"):
            try:
                from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE as _RL_CODE
                _exit_code = _RL_CODE
            except Exception:
                _exit_code = 1
    sys.exit(_exit_code)


def _route_single_query_images(cli, query, effective_query, single_query_images, single_query_image_urls):
    """Attach one-shot images natively when the model supports vision, else pre-describe them as text."""
    if not (single_query_images or single_query_image_urls):
        return effective_query
    # Same image-routing decision as the interactive path: a vision-capable model
    # (incl. custom-provider models declaring `model.supports_vision: true`) gets
    # native image_url parts; otherwise the text pipeline (vision_analyze
    # pre-description).
    _img_mode = "text"
    _build_parts = None
    try:
        from agent.image_routing import build_native_content_parts as _build_parts  # noqa: F811
        from agent.image_routing import decide_image_input_mode
        from hermes_cli.config import load_config

        _img_mode = decide_image_input_mode(
            (cli.provider or "").strip(), (cli.model or "").strip(), load_config(),
            requested_provider=(cli.requested_provider or "").strip(),
        )
    except Exception:
        _img_mode = "text"

    def _text_fallback():
        # ``_preprocess_images_with_vision`` only knows local files; when only URLs
        # were supplied keep the original query text intact.
        if single_query_images:
            return cli._preprocess_images_with_vision(query, single_query_images, announce=False)
        return effective_query

    if _img_mode != "native" or _build_parts is None:
        return _text_fallback()
    try:
        _parts, _skipped = _build_parts(
            query if isinstance(query, str) else "",
            [str(p) for p in single_query_images],
            image_urls=list(single_query_image_urls) or None,
        )
        if any(p.get("type") == "image_url" for p in _parts):
            return _parts
        return _text_fallback()  # all images unreadable
    except Exception:
        return _text_fallback()


def _collect_kanban_task_images(single_query_images):
    """Kanban workers: image paths/URLs in the task body join the first turn's attachments."""
    single_query_image_urls: list[str] = []
    _kanban_task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not _kanban_task_id:
        return single_query_image_urls
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        from agent.image_routing import extract_image_refs as _extract_refs

        with _kbc.connect_closing() as _conn:
            _task = _kb.get_task(_conn, _kanban_task_id)
        _body = getattr(_task, "body", "") if _task is not None else ""
        if _body:
            _kb_paths, _kb_urls = _extract_refs(_body)
            # Dedupe against any --image the user already passed.
            _seen = {str(p) for p in single_query_images}
            for _p in _kb_paths:
                if _p not in _seen:
                    _seen.add(_p)
                    single_query_images.append(Path(_p))
            single_query_image_urls.extend(_kb_urls)
    except Exception as _exc:
        # Best-effort enrichment; never block worker startup on it.
        logger.debug("kanban image-ref extraction failed: %s", _exc)
    return single_query_image_urls


def _install_single_query_signal_handlers(cli):
    """Route SIGINT/SIGTERM/SIGHUP through agent.interrupt() before unwinding; kanban workers hard-exit.

    A plain KeyboardInterrupt only unwinds the main thread, so tool worker threads
    would orphan the setsid child; the interrupt + grace window lets them kill it.
    """
    import signal as _signal

    def _signal_handler_q(signum, frame):
        logger.debug("Received signal %s in single-query mode", signum)
        _arm_exit_watchdog_on_shutdown_signal()  # covers wedges in the unwind below
        _interrupt_agent_for_signal(getattr(cli, "agent", None), signum)
        # Kanban: a non-daemon worker blocked in _wait_for_process survives KeyboardInterrupt
        # and the dispatcher sees 'running' forever, so os._exit(0) (SIGALRM deadman guards
        # a blocking flush). That skips atexit + the token-drain hook, hence the explicit flush.
        # Kanban worker exit path (#28181): SIGTERM hits a dispatcher-spawned worker that's likely in a
        # non-daemon thread waiting on a child subprocess in _wait_for_process. Raising KeyboardInterrupt
        # only unwinds the main thread; the worker thread keeps running, the process gets reparented to
        # init, and the dispatcher's _pid_alive check returns True forever — task stuck in 'running'
        # indefinitely. Skip the controlled-unwind dance and call os._exit(0) so the kernel reclaims the PID
        # immediately and detect_crashed_workers can reclaim the stale claim on the next tick. Flush logging
        # + stdout/stderr first so the final debug trace isn't lost; SIGALRM deadman guards the flush
        # against any rare blocking-I/O case (the reporter measured flush in <1ms; the alarm is a failsafe,
        # not the common path).
        if os.environ.get("HERMES_KANBAN_TASK"):
            with suppress(Exception):
                if hasattr(_signal, "SIGALRM"):
                    _signal.signal(_signal.SIGALRM, lambda *_: os._exit(0))
                    _signal.alarm(5)
            with suppress(Exception):
                # Durable flush FIRST: memory-provider shutdown inside _run_cleanup can issue aux-LLM calls,
                # and nothing after it may fail in a way that loses the turn (#88583).
                # os._exit(0) skips atexit AND SessionDB's token-drain hook, so flush + finalize the session
                # store here or the worker's turn (and its usage deltas) never become durable (#88583 /
                # #50881 class). Best-effort under the SIGALRM deadman above.
                _flush_one_shot_session_store(cli)
            _flush_logging_and_stdio()
            os._exit(0)
        raise KeyboardInterrupt()
    with suppress(Exception):  # restricted environments
        for _name in ("SIGINT", "SIGTERM", "SIGHUP"):
            if hasattr(_signal, _name):
                _signal.signal(getattr(_signal, _name), _signal_handler_q)


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

    if parsed_skills:
        # Load the skill payloads in the background: skill_view walks the full skills
        # tree per skill (~0.5s for a large library) and the result is only consumed
        # at agent init, not by the banner. finalize_preloaded_skills() joins the
        # thread before any consumer reads cli.system_prompt.
        def _load_preloaded_skills() -> None:
            try:
                cli._preload_skills_result = build_preloaded_skills_prompt(parsed_skills, task_id=cli.session_id)
            except Exception as exc:  # surfaced by finalize
                cli._preload_skills_error = exc

        cli._preload_skills_requested = parsed_skills
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


def _configure_quiet_agent(agent) -> None:
    """Neutralize every stdout-writing callback so -Q stdout carries only the final response."""
    agent.quiet_mode = True
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None
    agent.reasoning_callback = None
    # The diff/progress callbacks print directly and are gated by neither quiet_mode nor
    # tool_progress_mode, so they must go too; "off" also covers the executor's direct prints.
    agent.tool_progress_callback = None
    agent.tool_start_callback = None
    agent.tool_complete_callback = None
    agent.tool_progress_mode = "off"


def _run_single_query_mode(cli, query, image, quiet, oneshot):
    """``-q``/``--image`` entry: seed an interactive session on a TTY, else run the one-shot turn and exit."""
    if _should_seed_interactive(query, image, quiet, oneshot):
        seeded_query, seeded_images = _collect_query_images(query, image)
        logger.info(
            "Seeding interactive session with -q prompt (%d chars, %d images)",
            len(seeded_query or ""), len(seeded_images),
        )
        cli._seeded_first_message = _SeededQueryMessage(seeded_query, seeded_images)
        return cli.run()
    cli._single_query_mode = True  # agent waits the full MCP cold-start before its only tool snapshot
    # No user can answer approval prompts: the approval gate takes the deterministic path.
    # One-shot mode: no between-turns MCP late-binding refresh, so the agent must wait the full MCP
    # cold-start bound before its first (and only) tool snapshot. See #51316.
    # Mark single-query for the approval gate. cli.py sets HERMES_INTERACTIVE earlier for interactive sudo
    # prompts, but a -q run has NO user waiting to answer approval prompts. The gate reads this marker (via
    # gateway.session_context.get_session_env, which falls back to os.environ when the session-context layer
    # isn't engaged) and takes the deterministic approvals.single_query_mode path instead of waiting the
    # full timeout. See #86878.
    os.environ["HERMES_SINGLE_QUERY_SESSION"] = "1"
    if not cli._claim_active_session("cli", stderr=bool(quiet)):
        sys.exit(1)
    try:
        query, single_query_images = _collect_query_images(query, image)
        single_query_image_urls = _collect_kanban_task_images(single_query_images)
        if quiet:
            # Quiet mode: suppress banner, spinner, tool previews.
            cli.tool_progress_mode = "off"
            if cli._ensure_runtime_credentials():
                effective_query: Any = _route_single_query_images(
                    cli, query, query, single_query_images, single_query_image_urls
                )
                turn_route = cli._resolve_turn_agent_config(effective_query)
                if turn_route["signature"] != cli._active_agent_route_signature:
                    cli.agent = None
                if cli._init_agent(
                    model_override=turn_route["model"],
                    runtime_override=turn_route["runtime"],
                    request_overrides=turn_route.get("request_overrides"),
                ):
                    _configure_quiet_agent(cli.agent)
                    _run_quiet_single_query(cli, effective_query)

            sys.exit(1)  # credentials or agent init failed
        # No welcome banner (~420 ms cold); session id / resume hint come from _print_exit_summary().
        _query_label = query or ("[image attached]" if single_query_images else "")
        if _query_label:
            cli.console.print(f"[bold blue]Query:[/] {_query_label}")
        cli._show_security_advisories()
        cli.chat(query, images=single_query_images or None)
        cli._print_exit_summary(clear_screen=False)
    finally:
        _finalize_single_query(cli)


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
        _run_single_query_mode(cli, query, image, quiet, oneshot)
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
