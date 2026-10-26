"""Executable lookup and launch-env policy for ``external_process`` (ACP) providers.

A GUI/service launch (LaunchAgent, Desktop backend, Desktop over SSH) has a bare PATH, so the
profile's command resolves through PATH, then the user-local bin, then Claude Code's install
prefixes."""

from __future__ import annotations

import os
from typing import Optional


def resolve_external_process_command(command: str) -> Optional[str]:
    """Absolute path of the provider CLI ``command``, or ``None`` when nothing executable matches."""
    if not command:
        return None
    from hermes_platform.resolver import locate_command
    from hermes_platform.resolver.known_dirs import user_local_bin

    lookup = command
    if os.sep in command or (os.altsep and os.altsep in command):
        # An operator-configured relative path (./bin/agent) means the launch cwd, as it did
        # under shutil.which; anchor it so the resolver's absolute-only explicit check accepts it.
        lookup = os.path.abspath(os.path.expandvars(os.path.expanduser(command)))
    found = locate_command(lookup, known_dirs=user_local_bin()).command
    if found:
        # Known dirs are written with "/" (%USERPROFILE%/.local/bin); normalize so a Windows hit
        # reads as one native path in status and in the spawned argv.
        return os.path.normpath(found[0])
    from agent.anthropic_adapter import find_claude_code_cli

    return find_claude_code_cli(command)


def external_process_launch_env_names() -> frozenset[str]:
    """Env names that choose an external-process provider's executable or argv, from every registered
    profile's ``process_command_env_vars`` and ``process_args_env_var``: executable authority, so the
    generic env writers (dashboard, MCP/connector installs, ``save_env_value``) must refuse them."""
    from providers import list_providers

    return frozenset(name for profile in list_providers() if profile.auth_type == "external_process"
                     for name in (*profile.process_command_env_vars, profile.process_args_env_var) if name)


def env_writer_launch_denylist() -> frozenset[str]:
    """:func:`external_process_launch_env_names` plus the ``profile_env`` a left-core row keeps for a provider
    plugin the active home lacks: that plugin's profile, which would declare them, is not registered."""
    from hermes_cli.left_core_migration import absent_plugin_profile_env
    from hermes_constants import get_hermes_home

    return external_process_launch_env_names() | absent_plugin_profile_env(get_hermes_home())
