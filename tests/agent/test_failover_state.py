"""``agent.failover_state`` — live failover identity + its display label.

The bug these guard: ``try_activate_fallback()`` swaps ``agent.model`` /
``agent.provider`` in place mid-run, so any surface that snapshots the model
STRING at dispatch time reports a model the child is no longer using. These
tests pin the two halves of the shared fix — the live read and the label —
including the total-function contract (never raises) that lets both run on
display paths and inside a lock-adjacent registry read.
"""
from __future__ import annotations

import unittest
import weakref
from unittest.mock import MagicMock

from agent.failover_state import (
    EFFECTIVE_MODEL_KEYS,
    FALLBACK_GLYPH,
    effective_model_fields,
    format_model_label,
    resolve_effective_model,
)


class _FakeAgent:
    """Minimal stand-in shaped like a real AIAgent's failover surface."""

    def __init__(
        self,
        model="glm-5.3",
        provider="ollama-cloud",
        fallback_activated=False,
        primary_runtime=None,
    ):
        self.model = model
        self.provider = provider
        self._fallback_activated = fallback_activated
        if primary_runtime is not None:
            self._primary_runtime = primary_runtime

    def activate_fallback(self, model, provider):
        """Mirror try_activate_fallback's in-place mutation."""
        self._primary_runtime = {"model": self.model, "provider": self.provider}
        self.model = model
        self.provider = provider
        self._fallback_activated = True


# ---------------------------------------------------------------------------
# resolve_effective_model
# ---------------------------------------------------------------------------


class TestResolveEffectiveModel(unittest.TestCase):
    def test_returns_the_full_key_set(self):
        state = resolve_effective_model(_FakeAgent())
        assert set(state) == set(EFFECTIVE_MODEL_KEYS)

    def test_no_fallback_reports_live_identity(self):
        state = resolve_effective_model(_FakeAgent())
        assert state["model"] == "glm-5.3"
        assert state["provider"] == "ollama-cloud"
        assert state["fallback_active"] is False
        # Primary is only reported while a fallback is active — on the
        # primary it is the same value and would invite a pointless x→x swap.
        assert state["primary_model"] is None
        assert state["primary_provider"] is None

    def test_fallback_reports_the_post_swap_identity(self):
        agent = _FakeAgent()
        agent.activate_fallback("claude-opus-5", "anthropic")
        state = resolve_effective_model(agent)
        assert state["model"] == "claude-opus-5"
        assert state["provider"] == "anthropic"
        assert state["fallback_active"] is True
        assert state["primary_model"] == "glm-5.3"
        assert state["primary_provider"] == "ollama-cloud"

    def test_reads_live_not_cached(self):
        """The core contract: failover is reversible, so nothing may cache."""
        agent = _FakeAgent()
        assert resolve_effective_model(agent)["model"] == "glm-5.3"
        agent.activate_fallback("claude-opus-5", "anthropic")
        assert resolve_effective_model(agent)["model"] == "claude-opus-5"
        # restore_primary_runtime's effect — back to the primary.
        agent.model = "glm-5.3"
        agent.provider = "ollama-cloud"
        agent._fallback_activated = False
        state = resolve_effective_model(agent)
        assert state["model"] == "glm-5.3"
        assert state["fallback_active"] is False

    def test_none_agent_is_all_empty(self):
        assert resolve_effective_model(None) == {
            "model": None,
            "provider": None,
            "fallback_active": False,
            "primary_model": None,
            "primary_provider": None,
        }

    def test_dead_weakref_is_all_empty(self):
        agent = _FakeAgent()
        ref = weakref.ref(agent)
        assert resolve_effective_model(ref)["model"] == "glm-5.3"
        del agent
        assert resolve_effective_model(ref)["model"] is None

    def test_live_weakref_is_dereferenced(self):
        agent = _FakeAgent()
        agent.activate_fallback("claude-opus-5", "anthropic")
        state = resolve_effective_model(weakref.ref(agent))
        assert state["model"] == "claude-opus-5"
        assert state["fallback_active"] is True

    def test_object_missing_every_attribute(self):
        class Bare:
            pass

        state = resolve_effective_model(Bare())
        assert state["model"] is None
        assert state["fallback_active"] is False

    def test_missing_primary_runtime_while_in_fallback(self):
        agent = _FakeAgent(model="claude-opus-5", fallback_activated=True)
        state = resolve_effective_model(agent)
        assert state["model"] == "claude-opus-5"
        assert state["fallback_active"] is True
        assert state["primary_model"] is None

    def test_non_dict_primary_runtime_is_ignored(self):
        agent = _FakeAgent(fallback_activated=True, primary_runtime=None)
        agent._primary_runtime = "not-a-dict"
        assert resolve_effective_model(agent)["primary_model"] is None

    def test_magicmock_does_not_leak_into_the_payload(self):
        """A MagicMock auto-vivifies attributes; none may reach the wire.

        Matches the isinstance-guard precedent in delegate_tool's registry
        and result builds — a stringified Mock in a user-visible label is
        the failure mode being prevented.
        """
        state = resolve_effective_model(MagicMock())
        assert state["model"] is None
        assert state["provider"] is None
        # bool(MagicMock()) is True — must not be read as "failed over".
        assert state["fallback_active"] is False

    def test_exploding_attributes_never_raise(self):
        class Hostile:
            @property
            def model(self):
                raise RuntimeError("boom")

            @property
            def provider(self):
                raise RuntimeError("boom")

            @property
            def _fallback_activated(self):
                raise RuntimeError("boom")

        state = resolve_effective_model(Hostile())
        assert state["model"] is None
        assert state["fallback_active"] is False

    def test_blank_strings_normalise_to_none(self):
        state = resolve_effective_model(_FakeAgent(model="   ", provider=""))
        assert state["model"] is None
        assert state["provider"] is None


# ---------------------------------------------------------------------------
# format_model_label
# ---------------------------------------------------------------------------


class TestFormatModelLabel(unittest.TestCase):
    def test_no_fallback_is_the_bare_slug(self):
        """Byte-identical to the pre-fix output for a healthy run."""
        assert format_model_label("claude-opus-5") == "claude-opus-5"
        assert (
            format_model_label("claude-opus-5", fallback_active=False)
            == "claude-opus-5"
        )

    def test_compact_fallback_renders_the_swap(self):
        assert (
            format_model_label(
                "claude-opus-5",
                fallback_active=True,
                primary_model="glm-5.3",
                compact=True,
            )
            == "⚠ glm-5.3→claude-opus-5"
        )

    def test_verbose_fallback_names_the_primary(self):
        assert (
            format_model_label(
                "claude-opus-5", fallback_active=True, primary_model="glm-5.3"
            )
            == "⚠ claude-opus-5 (fallback from glm-5.3)"
        )

    def test_missing_primary_degrades_not_dangles(self):
        assert (
            format_model_label("claude-opus-5", fallback_active=True)
            == "⚠ claude-opus-5 (fallback)"
        )
        assert (
            format_model_label(
                "claude-opus-5", fallback_active=True, compact=True
            )
            == "⚠ claude-opus-5"
        )

    def test_primary_equal_to_effective_is_not_drawn_as_a_swap(self):
        assert (
            format_model_label(
                "claude-opus-5",
                fallback_active=True,
                primary_model="claude-opus-5",
                compact=True,
            )
            == "⚠ claude-opus-5"
        )

    def test_missing_model_renders_the_unknown_marker(self):
        assert format_model_label(None) == "?"
        assert format_model_label("") == "?"

    def test_non_string_inputs_never_raise(self):
        assert format_model_label(MagicMock()) == "?"
        assert format_model_label(12345) == "?"
        # bool(MagicMock()) is True; must not be read as an active fallback.
        assert format_model_label("m", fallback_active=MagicMock()) == "m"

    def test_glyph_constant_is_what_gets_rendered(self):
        label = format_model_label("m", fallback_active=True)
        assert label.startswith(FALLBACK_GLYPH)


# ---------------------------------------------------------------------------
# effective_model_fields — resolver + label, the six-key wire payload
# ---------------------------------------------------------------------------


class TestEffectiveModelFields(unittest.TestCase):
    def test_carries_the_label_alongside_the_state(self):
        agent = _FakeAgent()
        agent.activate_fallback("claude-opus-5", "anthropic")
        fields = effective_model_fields(agent)
        assert fields["model"] == "claude-opus-5"
        assert fields["provider"] == "anthropic"
        assert fields["fallback_active"] is True
        assert fields["primary_model"] == "glm-5.3"
        assert fields["primary_provider"] == "ollama-cloud"
        assert fields["model_label"] == "⚠ claude-opus-5 (fallback from glm-5.3)"

    def test_compact_switches_the_label_form_only(self):
        agent = _FakeAgent()
        agent.activate_fallback("claude-opus-5", "anthropic")
        fields = effective_model_fields(agent, compact=True)
        assert fields["model"] == "claude-opus-5"
        assert fields["model_label"] == "⚠ glm-5.3→claude-opus-5"

    def test_dead_agent_degrades_to_the_snapshot(self):
        """A stale-but-plausible model beats a blank one.

        A dead agent can no longer fail over, so the dispatch-time snapshot
        is the last thing that was true about it.
        """
        fields = effective_model_fields(
            None, snapshot_model="glm-5.3", snapshot_provider="ollama-cloud"
        )
        assert fields["model"] == "glm-5.3"
        assert fields["provider"] == "ollama-cloud"
        assert fields["fallback_active"] is False
        assert fields["model_label"] == "glm-5.3"

    def test_live_agent_wins_over_the_snapshot(self):
        """The whole point: the live value overrides the stale one."""
        agent = _FakeAgent()
        agent.activate_fallback("claude-opus-5", "anthropic")
        fields = effective_model_fields(agent, snapshot_model="glm-5.3")
        assert fields["model"] == "claude-opus-5"

    def test_no_agent_and_no_snapshot_is_the_unknown_marker(self):
        fields = effective_model_fields(None)
        assert fields["model"] is None
        assert fields["model_label"] == "?"


if __name__ == "__main__":
    unittest.main()
