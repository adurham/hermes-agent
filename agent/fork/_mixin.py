"""Mixin class providing thin forwarder methods for fork-only features.

``AIAgent`` inherits from this mixin so that fork-specific methods
(``_record_loaded_skill``, ``_maybe_skill_recall_hint``,
``_capture_rate_limits_from_headers``, etc.) appear as if they were
defined directly on ``AIAgent`` — preserving the public contract that
tests and call sites rely on.

The real implementations live in the sibling modules:

  ``agent.fork.skill_recall``        — skill-recall reminder
  ``agent.fork.memory_recall``       — memory-recall reminder (warm tier)
  ``agent.fork.consult_nudge``       — second-opinion (consult tool) reminder
  ``agent.fork.rate_limit_tracker``  — rate-limit observability
  ``agent.fork.anthropic_recovery``  — refusal retry + CC alias translation
  ``agent.fork.tool_search_lazy``    — lazy MCP tool gating
  ``agent.fork.diagnostics``         — usage history + tools sig + xAI hint

Each forwarder is a 4-line method that imports and dispatches. Why a
mixin instead of methods on ``AIAgent`` directly?

* Keeps ``run_agent.py`` free of fork-specific code, reducing upstream
  merge surface to zero in this area.
* The mixin lives under ``agent/fork/`` which is a hard-fork boundary
  upstream never modifies. Future upstream merges that touch
  ``run_agent.py`` cannot conflict with these forwarders.
* If a fork feature later needs to be removed, drop the
  corresponding ``def`` here — no surgery in ``run_agent.py``.

The ``_RISKY_TOOL_NAMES`` class attribute is also re-exported here so
``AIAgent._RISKY_TOOL_NAMES`` (used by tests and skill_recall logic)
resolves through MRO to the canonical value in ``agent.fork.skill_recall``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set


class ForkForwardersMixin:
    """See module docstring."""

    def _sanitize_messages_for_refusal_retry(self, messages: list) -> tuple:
        """Forwarder — see ``agent.fork.anthropic_recovery.sanitize_messages_for_refusal_retry``."""
        from agent.fork.anthropic_recovery import sanitize_messages_for_refusal_retry
        return sanitize_messages_for_refusal_retry(self, messages)

    def _is_anthropic_refusal(self, response) -> bool:
        """Forwarder — see ``agent.fork.anthropic_recovery.is_anthropic_refusal``."""
        from agent.fork.anthropic_recovery import is_anthropic_refusal
        return is_anthropic_refusal(self, response)

    def _record_usage_history(self, canonical_usage) -> None:
        """Forwarder — see ``agent.fork.diagnostics.record_usage_history``."""
        from agent.fork.diagnostics import record_usage_history
        return record_usage_history(self, canonical_usage)
    # Tool names that count as a "risky operation" for the skill-recall
    # reminder.  Source-of-truth lives in ``agent.fork.skill_recall``;
    # we re-export here so callers / tests reach it as ``AIAgent._RISKY_TOOL_NAMES``.
    from agent.fork.skill_recall import _RISKY_TOOL_NAMES

    def _record_loaded_skill(self, name: str, tool_result: str) -> None:
        """Forwarder — see ``agent.fork.skill_recall.record_loaded_skill``."""
        from agent.fork.skill_recall import record_loaded_skill
        return record_loaded_skill(self, name, tool_result)

    def _translate_cc_args_after_repair(self, tc, original_name: str) -> None:
        """Forwarder — see ``agent.fork.anthropic_recovery.translate_cc_args_after_repair``."""
        from agent.fork.anthropic_recovery import translate_cc_args_after_repair
        return translate_cc_args_after_repair(self, tc, original_name)

    def _tools_signature(self) -> str:
        """Forwarder — see ``agent.fork.diagnostics.tools_signature``."""
        from agent.fork.diagnostics import tools_signature
        return tools_signature(self)

    def _build_tool_search_config(self) -> Optional[Dict[str, Any]]:
        """Forwarder — see ``agent.fork.tool_search_lazy.build_tool_search_config``."""
        from agent.fork.tool_search_lazy import build_tool_search_config
        return build_tool_search_config(self)

    def _capture_rate_limits_from_headers(self, headers: Any) -> None:
        """Forwarder — see ``agent.fork.rate_limit_tracker.capture_rate_limits_from_headers``."""
        from agent.fork.rate_limit_tracker import capture_rate_limits_from_headers
        return capture_rate_limits_from_headers(self, headers)

    def _currently_deferred_names(self) -> Optional[Set[str]]:
        """Forwarder — see ``agent.fork.tool_search_lazy.currently_deferred_names``."""
        from agent.fork.tool_search_lazy import currently_deferred_names
        return currently_deferred_names(self)

    @staticmethod
    def _decorate_xai_entitlement_error(detail: str) -> str:
        """Forwarder — see ``agent.fork.diagnostics.decorate_xai_entitlement_error``."""
        from agent.fork.diagnostics import decorate_xai_entitlement_error
        return decorate_xai_entitlement_error(detail)

    def _log_rate_limit_first_capture(self, state: "RateLimitState") -> None:
        """Forwarder — see ``agent.fork.rate_limit_tracker.log_rate_limit_first_capture``."""
        from agent.fork.rate_limit_tracker import log_rate_limit_first_capture
        return log_rate_limit_first_capture(self, state)

    def _log_rate_limit_transitions(self, state: "RateLimitState") -> None:
        """Forwarder — see ``agent.fork.rate_limit_tracker.log_rate_limit_transitions``."""
        from agent.fork.rate_limit_tracker import log_rate_limit_transitions
        return log_rate_limit_transitions(self, state)

    def _maybe_skill_recall_hint(self, function_name: str) -> Optional[str]:
        """Forwarder — see ``agent.fork.skill_recall.maybe_skill_recall_hint``."""
        from agent.fork.skill_recall import maybe_skill_recall_hint
        return maybe_skill_recall_hint(self, function_name)

    def _maybe_memory_recall_hint(self, function_name: str) -> Optional[str]:
        """Forwarder — see ``agent.fork.memory_recall.maybe_memory_recall_hint``."""
        from agent.fork.memory_recall import maybe_memory_recall_hint
        return maybe_memory_recall_hint(self, function_name)

    def _record_voluntary_memory_recall(self) -> None:
        """Forwarder — see ``agent.fork.memory_recall.record_voluntary_recall``."""
        from agent.fork.memory_recall import record_voluntary_recall
        return record_voluntary_recall(self)

    def _maybe_consult_nudge(self, function_name: str) -> Optional[str]:
        """Forwarder — see ``agent.fork.consult_nudge.maybe_consult_nudge``."""
        from agent.fork.consult_nudge import maybe_consult_nudge
        return maybe_consult_nudge(self, function_name)

    def _record_voluntary_consult(self) -> None:
        """Forwarder — see ``agent.fork.consult_nudge.record_voluntary_consult``."""
        from agent.fork.consult_nudge import record_voluntary_consult
        return record_voluntary_consult(self)
