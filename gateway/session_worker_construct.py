"""Agent-construction inputs the owner resolves for a managed worker (``construct_v1``).

The in-process turn passes these to ``AIAgent`` from the owner's runner state; a fresh worker
cannot reconstruct them (its config readers serve the frozen session snapshot, and a bypass
worker must never reload the profile), so the owner resolves them per turn, mode-appropriately,
and they ride the hydrated request beside ``turn_v1``. The child validates the closed shape.
"""
import json

KEYS = frozenset({'fallback_model'})


def construct_inputs(authority, policy):
    """Owner side, per turn (off the owner loop: config reads)."""
    return {'fallback_model': _fallback_chain(authority, policy)}


def _fallback_chain(authority, policy):
    """The chain ``_build_fresh_agent`` gives an in-process turn. Ordinary sessions keep the
    profile refresh semantics (re-read per turn, last-known-good per home); a config-only session
    takes its frozen explicit snapshot (never the profile); safe mode carries none."""
    if policy.safe_mode:
        return None
    if policy.ignore_user_config:
        from hermes_cli.fallback_config import get_fallback_chain
        return get_fallback_chain(policy.config(authority)) or None
    refresh = getattr(authority.runner, '_refresh_fallback_model', None)
    if refresh is None:
        return None
    from gateway.session_authorities import owner_scope
    with owner_scope(authority):
        return refresh()


def construct_kwargs(frame):
    """Child side: the validated ``AIAgent`` keyword arguments; a frame without the object
    (older owner) constructs exactly as before."""
    inputs = json.loads(frame['policy'].get('request_json') or '{}').get('construct_v1')
    if inputs is None:
        return {}
    chain = inputs.get('fallback_model') if isinstance(inputs, dict) else None
    if (not isinstance(inputs, dict) or set(inputs) != KEYS
            or not (chain is None or (isinstance(chain, list) and all(isinstance(e, dict) for e in chain)))):
        raise ValueError('invalid_managed_worker_bootstrap')
    return {'fallback_model': chain}
