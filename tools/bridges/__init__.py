"""Stdio MCP bridges.

These modules are MCP shims that proxy stdio to another MCP transport
(HTTP, SSE, or another stdio process). They are invoked as subprocesses
from ``mcp_servers:`` entries in ~/.hermes/config.yaml; they are not
imported by the agent itself.

Currently:
    - cc_proxy_mcp: proxies to Anthropic's claude.ai MCP proxy using
      Claude Code's OAuth credentials. Lets Hermes piggy-back on whichever
      connectors Claude Code already has wired (Slack, Notion, PagerDuty,
      Microsoft 365, Stack Overflow Teams, internal MCP gateways, etc.) without re-authenticating each one.
"""
