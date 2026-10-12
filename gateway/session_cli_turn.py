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
_cli_turn: ContextVar[dict | None] = ContextVar('cli_turn', default=None)


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
def cli_turn_scope(committed):
    token = _cli_turn.set(committed or None)
    try:
        yield
    finally:
        _cli_turn.reset(token)


def cli_turn():
    """The executing admission's committed CLI turn fields (``{}`` outside one)."""
    return _cli_turn.get() or {}
