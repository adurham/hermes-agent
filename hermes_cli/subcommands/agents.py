"""``hermes agents`` subcommand parser.

Follows the cron/security/approvals pattern: parser construction lives here,
the handler is injected by ``main.py`` so this module never imports ``main``
(cycle avoidance).

Deliberately its own file rather than bolted onto ``approvals.py``. The two
resolve superficially similar "a human says yes or no" prompts, but they are
unrelated domains: ``hermes approvals`` mines *dangerous-command* decisions
out of session history to propose allowlist entries, while this resolves
*held cross-agent messages* in ``cross_session_inbox``. Sharing a parser
would couple two feature areas that have no data, storage, or lifecycle in
common.
"""

from __future__ import annotations

from typing import Callable


def build_agents_parser(subparsers, *, cmd_agents: Callable) -> None:
    """Attach the ``agents`` subcommand to ``subparsers``."""
    agents_parser = subparsers.add_parser(
        "agents",
        help="Local agent messaging (review held cross-session messages)",
        description=(
            "Tools for local agent-to-agent messaging. "
            "`hermes agents inbox` lists messages other Hermes sessions have "
            "sent this profile that are held awaiting your approval, and "
            "approves or denies them."
        ),
    )
    agents_subparsers = agents_parser.add_subparsers(
        dest="agents_command",
        metavar="<subcommand>",
    )

    inbox_parser = agents_subparsers.add_parser(
        "inbox",
        help="List or resolve held cross-session messages",
        description=(
            "With no flags, lists messages currently held for approval. "
            "Approving does NOT deliver the message directly — it returns "
            "the message to the pending queue so the recipient session "
            "claims it at its own next drain checkpoint, re-evaluating its "
            "inbound policy at that point. An unresolved hold expires on "
            "its own after an hour."
        ),
    )
    inbox_parser.add_argument(
        "--approve",
        dest="approve_id",
        metavar="ID",
        type=int,
        help="Return the held message with this id to the pending queue",
    )
    inbox_parser.add_argument(
        "--deny",
        dest="deny_id",
        metavar="ID",
        type=int,
        help="Mark the held message with this id denied; it is never delivered",
    )
    inbox_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        help="Only show messages addressed to this session id",
    )
    inbox_parser.add_argument(
        "--all",
        action="store_true",
        help="List every message regardless of status, not just held ones",
    )
    inbox_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text",
    )
    inbox_parser.set_defaults(func=cmd_agents)
    agents_parser.set_defaults(func=cmd_agents)
