"""Main-session status bar marks an active provider failover.

The bar's DATA source was already correct — ``_get_status_bar_snapshot``
reads ``agent.model`` live precisely so a failover isn't stale — but a
silently failed-over session was visually indistinguishable from a normal
one. This is the glyph half only, plus the structured fallback fields the
snapshot now exposes for renderers that want to style the degraded state
rather than string-match a glyph.

The main session reaches fallback through a different mechanism than
subagents (root-level ``fallback_providers`` → ``agent._fallback_chain``)
but converges on the same ``try_activate_fallback`` mutator and the same
``_fallback_activated`` flag, so the shared resolver covers both.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from cli import HermesCLI


@pytest.fixture(autouse=True)
def _reset_active_skin():
    """Pin the default skin — status_glyph is skin-overridable.

    Mirrors the fixture in test_cli_status_bar.py: without it these
    assertions are coupled to whatever skin the operator has configured.
    """
    from hermes_cli.skin_engine import get_active_skin_name, set_active_skin

    original = get_active_skin_name()
    set_active_skin("default")
    yield
    set_active_skin(original)


def _make_cli(model="glm-5.3"):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = model
    cli_obj.session_start = datetime.now() - timedelta(minutes=2)
    cli_obj.conversation_history = [{"role": "user", "content": "hi"}]
    cli_obj.agent = None
    return cli_obj


def _attach(cli_obj, *, model, provider, fallback=False, primary=None):
    agent = SimpleNamespace(
        model=model,
        provider=provider,
        base_url="",
        _fallback_activated=fallback,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_api_calls=0,
        get_rate_limit_state=lambda: None,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=0,
            context_length=200000,
            compression_count=0,
            last_input_tokens=0,
            last_cache_read_tokens=0,
            last_cache_write_tokens=0,
        ),
    )
    if primary is not None:
        agent._primary_runtime = primary
    cli_obj.agent = agent
    return cli_obj


class TestStatusBarFallbackGlyph:
    def test_healthy_session_has_no_marker(self):
        cli_obj = _attach(
            _make_cli(), model="glm-5.3", provider="ollama-cloud"
        )
        snap = cli_obj._get_status_bar_snapshot()
        assert snap["model_short"] == "glm-5.3"
        assert snap["fallback_active"] is False
        assert "⚠" not in cli_obj._build_status_bar_text(width=120)

    def test_failed_over_session_is_marked(self):
        cli_obj = _attach(
            _make_cli(),
            model="claude-opus-5",
            provider="anthropic",
            fallback=True,
            primary={"model": "glm-5.3", "provider": "ollama-cloud"},
        )
        snap = cli_obj._get_status_bar_snapshot()
        assert snap["model_short"] == "⚠ claude-opus-5"
        assert snap["fallback_active"] is True
        assert snap["primary_model"] == "glm-5.3"
        assert snap["primary_provider"] == "ollama-cloud"
        assert snap["provider"] == "anthropic"

    @pytest.mark.parametrize("width", [40, 60, 120])
    def test_marker_survives_every_width_breakpoint(self, width):
        """The three breakpoints all render the model from model_short, so
        folding the badge in there covers them without touching the width
        logic."""
        cli_obj = _attach(
            _make_cli(),
            model="claude-opus-5",
            provider="anthropic",
            fallback=True,
            primary={"model": "glm-5.3", "provider": "ollama-cloud"},
        )
        text = cli_obj._build_status_bar_text(width=width)
        assert "⚠" in text
        assert "claude-opus-5" in text

    def test_no_agent_does_not_break_the_bar(self):
        cli_obj = _make_cli()
        snap = cli_obj._get_status_bar_snapshot()
        assert snap["fallback_active"] is False
        assert "⚠" not in snap["model_short"]

    def test_truncation_budget_is_not_eaten_by_the_badge(self):
        """The badge is applied AFTER the 26-char truncation so a long model
        name keeps its full budget."""
        long_model = "a" * 40
        cli_obj = _attach(
            _make_cli(long_model),
            model=long_model,
            provider="anthropic",
            fallback=True,
            primary={"model": "glm-5.3", "provider": "ollama-cloud"},
        )
        snap = cli_obj._get_status_bar_snapshot()
        assert snap["model_short"].startswith("⚠ ")
        # 23 chars + "..." — the same budget a healthy long name gets.
        assert snap["model_short"] == "⚠ " + "a" * 23 + "..."
