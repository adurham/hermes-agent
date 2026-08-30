"""Tests for `hermes curator run` CLI behavior."""

from __future__ import annotations

from types import SimpleNamespace


def _args(**kwargs):
    values = {
        "dry_run": False,
        "synchronous": False,
        "background": False,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_run_defaults_to_synchronous(monkeypatch, capsys):
    import agent.curator as curator_state
    import hermes_cli.curator as curator_cli

    calls = []
    monkeypatch.setattr(curator_state, "is_enabled", lambda: True)
    monkeypatch.setattr(
        curator_state,
        "run_curator_review",
        lambda **kwargs: calls.append(kwargs) or {"auto_transitions": {}},
    )

    assert curator_cli._cmd_run(_args()) == 0

    assert calls[0]["synchronous"] is True
    assert calls[0]["dry_run"] is False
    assert "background" not in capsys.readouterr().out


def test_dry_run_default_reports_synchronous_wording(monkeypatch, capsys):
    import agent.curator as curator_state
    import hermes_cli.curator as curator_cli

    monkeypatch.setattr(curator_state, "is_enabled", lambda: True)
    monkeypatch.setattr(
        curator_state,
        "run_curator_review",
        lambda **kwargs: {"auto_transitions": {}},
    )

    assert curator_cli._cmd_run(_args(dry_run=True)) == 0

    out = capsys.readouterr().out
    assert "When the report lands" not in out
    assert "Read the report with `hermes curator status`" in out


def test_run_skips_hot_tier_audit_when_disabled(monkeypatch, capsys):
    """When curator.hot_tier_audit is off (default), `hermes curator run`
    must not import or call hot_tier_audit at all — this was the exact gap
    fixed 2026-08-30 (see FORK.md): the audit previously only ever ran from
    the session-start auto-trigger, never from this manual CLI command."""
    import agent.curator as curator_state
    import hermes_cli.curator as curator_cli

    monkeypatch.setattr(curator_state, "is_enabled", lambda: True)
    monkeypatch.setattr(curator_state, "get_hot_tier_audit", lambda: False)
    monkeypatch.setattr(
        curator_state,
        "run_curator_review",
        lambda **kwargs: {"auto_transitions": {}},
    )

    assert curator_cli._cmd_run(_args()) == 0

    out = capsys.readouterr().out
    assert "hot-tier audit" not in out


def test_run_invokes_hot_tier_audit_when_enabled(monkeypatch, capsys):
    """When curator.hot_tier_audit is on, `hermes curator run` (no flags)
    must call hot_tier_audit.run_hot_tier_audit with the CLI's own dry_run
    state, not silently skip it."""
    import agent.curator as curator_state
    import hermes_cli.curator as curator_cli
    from agent import hot_tier_audit

    calls = []
    monkeypatch.setattr(curator_state, "is_enabled", lambda: True)
    monkeypatch.setattr(curator_state, "get_hot_tier_audit", lambda: True)
    monkeypatch.setattr(curator_state, "get_hot_tier_audit_dry_run", lambda: True)
    monkeypatch.setattr(curator_state, "get_consolidate", lambda: False)
    monkeypatch.setattr(
        curator_state,
        "run_curator_review",
        lambda **kwargs: {"auto_transitions": {}},
    )
    monkeypatch.setattr(
        hot_tier_audit,
        "run_hot_tier_audit",
        lambda **kwargs: calls.append(kwargs) or {
            "entries_checked": 5,
            "stale_path_candidates": [],
            "llm_classification": {"ran": False},
            "written_report_path": "/tmp/fake-report",
        },
    )

    assert curator_cli._cmd_run(_args()) == 0

    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "hot-tier audit: checked=5 stale-path-candidates=0" in out
    assert "/tmp/fake-report" in out


def test_run_dry_run_forces_hot_tier_dry_run_even_if_config_is_live(monkeypatch, capsys):
    """A user asking `hermes curator run --dry-run` must never see the
    hot-tier half go live just because curator.hot_tier_audit_dry_run
    happens to be false in config — --dry-run on the whole command means
    dry-run for both passes."""
    import agent.curator as curator_state
    import hermes_cli.curator as curator_cli
    from agent import hot_tier_audit

    calls = []
    monkeypatch.setattr(curator_state, "is_enabled", lambda: True)
    monkeypatch.setattr(curator_state, "get_hot_tier_audit", lambda: True)
    # Config says LIVE (dry_run False) — the CLI's own --dry-run must win.
    monkeypatch.setattr(curator_state, "get_hot_tier_audit_dry_run", lambda: False)
    monkeypatch.setattr(curator_state, "get_consolidate", lambda: False)
    monkeypatch.setattr(
        curator_state,
        "run_curator_review",
        lambda **kwargs: {"auto_transitions": {}},
    )
    monkeypatch.setattr(
        hot_tier_audit,
        "run_hot_tier_audit",
        lambda **kwargs: calls.append(kwargs) or {
            "entries_checked": 1,
            "stale_path_candidates": [],
            "llm_classification": {"ran": False},
        },
    )

    assert curator_cli._cmd_run(_args(dry_run=True)) == 0

    assert len(calls) == 1
    assert calls[0]["dry_run"] is True
    out = capsys.readouterr().out
    assert "dry-run: no MEMORY.md/USER.md changes applied" in out
