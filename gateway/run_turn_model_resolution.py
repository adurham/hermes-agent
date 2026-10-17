"""Tail phases of ``GatewayTurnMixin._resolve_session_agent_runtime``.

The facade resolves the session ``/model`` override and the default runtime; these helpers apply the
per-channel ``channel_overrides`` layer and the empty-model safety nets. Names that tests patch on
``gateway.run`` are late-imported from there so those patches keep biting.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Optional

logger = logging.getLogger("gateway.run")  # same logger as the facade: records stay identical


def apply_channel_override(runner, source, model: str, runtime_kwargs: dict) -> tuple[str, dict]:
    """Layer the source channel's ``channel_overrides`` entry (model and/or provider) over the runtime."""
    from gateway.run import _get_channel_override, _resolve_runtime_agent_kwargs_for_provider

    cfg = getattr(runner, "config", None)  # getattr: bare object.__new__ test runners
    if cfg and source is not None:
        ch = _get_channel_override(
            cfg, source.platform, str(source.chat_id) if source.chat_id else "",
            thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
            parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
        )
        if ch:
            if ch.model:
                model = ch.model
            if ch.provider:
                runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(ch.provider, target_model=model or None)
                ch_runtime_model = runtime_kwargs.pop("model", None)
                # Adopt the provider's bundled model only when the override named none.
                if ch_runtime_model and not ch.model:
                    model = ch_runtime_model
    return model, runtime_kwargs


def finalize_resolved_model(runner, skey: Optional[str], model: str, runtime_kwargs: dict) -> str:
    """Fill an empty model from the provider catalog or the last good resolution; cache a good one."""
    # Provider resolved but no model.default (`hermes auth add` without `hermes model`): use the
    # provider's first catalog model.
    if not model and runtime_kwargs.get("provider"):
        with suppress(Exception):
            from hermes_cli.models import get_default_model_for_provider
            model = get_default_model_for_provider(runtime_kwargs["provider"])
            if model:
                logger.info(
                    "No model configured — defaulting to %s for provider %s", model, runtime_kwargs["provider"],
                )

    # Final safety net: an empty model (transient config-cache miss) makes every API call 400 and
    # the session goes silent — reuse the last model resolved for this session, else process-wide.
    if not model:
        _lr_state = runner._peek_session_state(skey) if skey else None
        _lr_star = runner._peek_session_state("*")
        _recovered = (
            (_lr_state.conversation.last_resolved_model if _lr_state else "")
            or (_lr_star.conversation.last_resolved_model if _lr_star else "")
        )
        if _recovered:
            logger.warning(
                "Empty model resolved for session=%s — recovering "
                "last-known-good model %s (config read likely returned "
                "empty; see #35314)", skey or "", _recovered,
            )
            model = _recovered
    else:
        # Cache the good resolution for future recovery turns.
        if skey:
            runner._session_state(skey).conversation.last_resolved_model = model
        runner._session_state("*").conversation.last_resolved_model = model
    return model
