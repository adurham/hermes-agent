"""Per-admission turn inputs the classic CLI sends with ``prompt.submit`` (CALLMAP W2).

Main's HermesCLI calls ``AIAgent.run_conversation`` with an API-only message (voice prefix,
model-switch / skills-reload notes) while persisting the clean text, names the turn's author for
memory attribution, and runs ``/model --once`` and ``/moa`` as a one-turn runtime swap. Over the
session authority those are facts about ONE admitted turn, committed with the admission row as
``cli_turn_v1`` so a retry or a queued drain runs exactly what was admitted, and bound for that
admission's execution only, never for a later turn.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from hermes_state_runtime import RuntimeStoreError

_SUBMIT_FIELDS = ('turn_author', 'api_text', 'model_override')
_API_TEXT_LIMIT = 1_000_000
_cli_turn: ContextVar[tuple | None] = ContextVar('cli_turn', default=None)


def submit_cli_turn_fields(params):
    """The raw per-turn CLI fields of a ``prompt.submit`` request, forwarded to admission."""
    return {key: params[key] for key in _SUBMIT_FIELDS if key in params}


def admit_cli_turn(params):
    """Wire fields -> committed ``cli_turn_v1`` (``{}`` when absent); malformed values are refused."""
    committed = {}
    if params.get('turn_author') is not None:
        from agent.turn_author import parse_turn_author
        author = parse_turn_author(params['turn_author'])
        if author is None:
            raise RuntimeStoreError('invalid_params')
        committed['turn_author'] = author
    api_text = params.get('api_text')
    if api_text is not None:
        if not isinstance(api_text, str) or len(api_text) > _API_TEXT_LIMIT:
            raise RuntimeStoreError('invalid_params')
        if api_text:
            committed['api_text'] = api_text
    override = params.get('model_override')
    if override is not None:
        if (not isinstance(override, dict) or set(override) - {'model', 'provider'}
                or not isinstance(override.get('model'), str) or not override['model'].strip()
                or not isinstance(override.get('provider', ''), str)):
            raise RuntimeStoreError('invalid_params')
        committed['model_override'] = {'model': override['model'].strip(),
                                       **({'provider': override['provider']} if override.get('provider') else {})}
    return {'cli_turn_v1': committed} if committed else {}


@contextmanager
def cli_turn_scope(admission_id, committed):
    token = _cli_turn.set((admission_id, committed) if committed else None)
    try:
        yield
    finally:
        _cli_turn.reset(token)


def cli_turn(message_id):
    """The committed CLI turn fields of the admission whose event id is ``message_id``; ``{}`` for
    any other turn, so a follow-up drained on the same task never inherits them."""
    bound = _cli_turn.get()
    if bound is None or message_id is None or str(message_id) != bound[0]:
        return {}
    return bound[1]


def one_turn_runtime(override, model, runtime_kwargs, config):
    """``(model, runtime_kwargs)`` of a committed ``model_override`` for this one turn, resolved by the
    same ``switch_model`` pipeline ``/model`` uses against the session's current route. Nothing is
    written: the session's route, policy and cached agent are untouched, so the next turn runs its own
    route again (main's ``/model --once`` restore). Raises ``RuntimeError`` with the switch's own
    sentence when the target cannot be resolved; the caller fails the turn with it."""
    from hermes_cli.config import get_compatible_custom_providers
    from hermes_cli.model_switch import switch_model
    result = switch_model(
        raw_input=override['model'], current_provider=runtime_kwargs.get('provider') or '',
        current_model=model or '', current_base_url=runtime_kwargs.get('base_url') or '',
        current_api_key=runtime_kwargs.get('api_key') or '', is_global=False,
        explicit_provider=override.get('provider', ''), user_providers=(config or {}).get('providers'),
        custom_providers=get_compatible_custom_providers(config or {}))
    if not result.success:
        raise RuntimeError(result.error_message or f"model {override['model']!r} could not be resolved")
    runtime = dict(runtime_kwargs)
    if result.target_provider != runtime_kwargs.get('provider'):
        # The previous provider's pool and ACP command belong to that provider only.
        runtime.update(credential_pool=None, command=None, args=[])
    runtime.update(provider=result.target_provider, requested_provider=result.target_provider,
                   api_key=result.api_key, base_url=result.base_url, api_mode=result.api_mode or None,
                   request_overrides=dict(result.request_overrides or {}),
                   capabilities=dict(result.runtime_capabilities or {}))
    return result.new_model, runtime
