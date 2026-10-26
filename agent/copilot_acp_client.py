"""Deprecated alias module: use :class:`agent.acp_client.ACPClient`.

Out-of-tree ACP providers (the ``kimchi-acp-provider`` catalog plugin) subclass
``CopilotACPClient`` from here. It keeps its historical launch fallbacks, error wording and
model-enablement filter so such subclasses behave as before. Remove no sooner than two minor releases after the deprecation (the plugin
compat contract in ``plugins/AGENTS.md``).
"""

from __future__ import annotations

import warnings
from typing import Any

from agent.acp_client import ACPClient

_warned = False


class _DeprecatedCopilotACPClient(ACPClient):
    """ACPClient with the historical launch defaults this module's callers relied on."""

    default_command = "copilot"
    default_args = ("--acp", "--stdio")
    command_env_vars = ("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH")
    args_env_var = "HERMES_COPILOT_ACP_ARGS"
    marker_base_url = "acp://copilot"
    placeholder_model = "copilot-acp"
    label = "Copilot ACP"
    missing_command_hint = "Install GitHub Copilot CLI or set HERMES_COPILOT_ACP_COMMAND/COPILOT_CLI_PATH."
    unsupported_transport_hint = (
        "Either install a CLI that ships with --acp support (e.g. `@github/copilot` late 2025+), or set "
        "HERMES_COPILOT_ACP_COMMAND / HERMES_COPILOT_ACP_ARGS to a working pair.")

    def model_entry_disabled(self, entry: dict[str, Any]) -> bool:
        return str((entry.get("_meta") or {}).get("copilotEnablement") or "").strip().lower() == "disabled"


_DeprecatedCopilotACPClient.__name__ = _DeprecatedCopilotACPClient.__qualname__ = "CopilotACPClient"


def __getattr__(name: str) -> Any:
    global _warned
    if name != "CopilotACPClient":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not _warned:
        _warned = True
        warnings.warn(
            "agent.copilot_acp_client.CopilotACPClient is deprecated; subclass agent.acp_client.ACPClient instead.",
            DeprecationWarning, stacklevel=2)
    return _DeprecatedCopilotACPClient
