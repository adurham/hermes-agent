"""Plugin slash commands of the classic CLI on the shared gateway (``slash.exec``).

On main a plugin's ``/command`` ran in this process. Rewired, the plugin's hooks, tools and MCP servers
live in the gateway, so its command runs there too (``gateway/session_commands.py``, the messaging
gateway's own plugin dispatch): ``ctx.call_mcp`` reaches the servers the gateway connected, and this
process never imports the plugin to run it. The names come from the gateway's ``commands.catalog``,
read once per session and gateway life (``/new``, resume and a redial read again) and again once when
a typed name misses, so a plugin the gateway enabled since still dispatches. A catalog whose
``plugin_discovery_error`` is set lists no plugins because the gateway could not load them, not
because there are none: it is treated as a failed read and never cached. Off the gateway (tests,
the TUI slash worker's shadow CLI) main's dispatch applies.
"""
from __future__ import annotations

from hermes_cli.gateway_client import GatewayClientError

_CATEGORY = "Plugin commands"  # tui_gateway/command_discovery.py


class CatalogUnavailable(Exception):
    """The gateway's command list could not be read (already printed): run nothing for this input."""


def _boundary(cli, connection):
    return connection, getattr(connection, "epoch", None), getattr(cli, "session_id", None)


def _read(cli, connection):
    """Fetch the catalog's plugin commands and cache them for the session and gateway life that served
    them (raises GatewayClientError, also when the gateway reports its plugin discovery failed). The
    result is published only if neither moved during the call: a reply from epoch E that arrives after
    a concurrent redial is never relabelled E+1 (``call_tagged``)."""
    session = getattr(cli, "session_id", None)
    catalog, epoch = connection.call_tagged("commands.catalog")
    catalog = catalog or {}
    if catalog.get("plugin_discovery_error"):  # absent on older gateways: today's behaviour
        raise GatewayClientError(f"plugin command discovery failed: {catalog['plugin_discovery_error']}")
    listed = catalog.get("plugin_commands")
    if isinstance(listed, dict):  # every plugin command, also one a display row (Ink's /logs) hides
        found = {str(name).lstrip("/"): str(desc or "") for name, desc in listed.items()}
    else:  # older gateways: the display category
        found = {}
        for category in catalog.get("categories") or []:
            if isinstance(category, dict) and category.get("name") == _CATEGORY:
                for row in category.get("pairs") or []:
                    if isinstance(row, (list, tuple)) and len(row) == 2 and isinstance(row[0], str):
                        found[row[0].lstrip("/")] = str(row[1] or "")
    served = (connection, epoch, session)
    if _boundary(cli, connection) == served:
        cli._gateway_plugin_commands = (served, found)
    return found


def _cached(cli, connection):
    if getattr(connection, "closed", False):  # hung up: the next call redials, maybe to another life
        return None
    boundary, found = getattr(cli, "_gateway_plugin_commands", None) or (None, None)
    return found if boundary == _boundary(cli, connection) else None


def names(cli):
    """``{name: description}`` of the gateway's plugin commands for completion, or None off the
    gateway. A failed read degrades quietly to none; dispatch uses :func:`lookup`."""
    from hermes_cli.gateway_side import connection_for
    connection = connection_for(cli)
    if connection is None:
        return None
    found = _cached(cli, connection)
    if found is None:
        try:
            found = _read(cli, connection)
        except GatewayClientError:
            return {}  # not cached: the next read asks again
    return found


def lookup(cli, name):
    """Whether ``/name`` is one of the gateway's plugin commands; None off the gateway. A name the
    cached list lacks re-reads it once. A failed read is not evidence the name is no plugin: it is
    printed and :class:`CatalogUnavailable` raised, so no skill, bundle or expansion stands in."""
    from hermes_cli.gateway_side import connection_for
    connection = connection_for(cli)
    if connection is None:
        return None
    found = _cached(cli, connection)
    if found is not None and name in found:
        return True
    try:
        return name in _read(cli, connection)
    except GatewayClientError as exc:
        from cli import _cprint
        _cprint(f"  /{name}: the gateway's command list is unavailable ({exc}); nothing was run.")
        raise CatalogUnavailable(name) from exc


def run(cli, name, arg):
    """True when the gateway answered ``/name arg`` (the local handler must not run)."""
    from hermes_cli.gateway_side import connection_for
    connection = connection_for(cli)
    if connection is None:
        return False
    from cli import _cprint
    try:
        reply = connection.call("slash.exec", session_id=cli.session_id, command=name + (" " + arg if arg else ""))
    except GatewayClientError as exc:
        _cprint(f"  /{name}: {exc}")
        return True
    reply = reply or {}
    if reply.get("type", "exec") != "exec":  # it lost the plugin since lookup: a skill/prompt reply
        _cprint(f"  /{name}: the gateway no longer has this plugin command; nothing was run.")
        return True
    output = str(reply.get("output") or "")
    if output:
        _cprint(output)
    return True
