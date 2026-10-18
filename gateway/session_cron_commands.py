"""``/cron`` on the shared gateway: the classic CLI's handler (hermes_cli/cli_cron_lines.py), run by the owner.

The profile's cron store belongs to the gateway whose ticker fires its jobs, so no client writes it. Bare
``/cron`` and ``list`` read, for every local session (main's TUI ran the same text in its slash worker). Every
other form writes the store: it runs here when the classic CLI sends it on its own command socket, and a
viewer's connection (Ink, Desktop, ACP) keeps main's refusal (gateway/session_commands.py).
"""
from __future__ import annotations


def cron_read(tokens):
    """``'overview'`` / ``'list'`` / ``'list-all'`` for the read forms of the shlex-split ``/cron`` argument;
    None for a write."""
    if not tokens:
        return 'overview'
    if tokens[0].lower() == 'list' and set(tokens[1:]) <= {'--all', '-a'}:
        return 'list-all' if tokens[1:] else 'list'
    return None


async def run_cron(authority, arg) -> dict:
    """``/cron <arg>`` off the owner loop in the owning profile: the store is written here, never by a client."""
    from gateway.session_info_reads import owner_lines
    from hermes_cli.cli_cron_lines import cron_command_lines
    return await owner_lines(authority, lambda: cron_command_lines(arg))
