"""Fork-specific modules for the adurham/hermes-agent fork.

These modules house features that are NOT in upstream
``NousResearch/hermes-agent`` and should never be sent upstream as PRs.

Each module exposes free functions that take ``agent`` (an
:class:`AIAgent` instance) as their first argument. ``AIAgent`` keeps
thin forwarder methods so existing call sites and tests work unchanged.
"""
