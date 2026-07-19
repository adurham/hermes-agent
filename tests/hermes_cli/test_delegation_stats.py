"""Unit tests for ``hermes_cli.delegation_stats``."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import delegation_stats as ds


def test_record_creates_file_and_appends(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    s = ds.DelegationStat(
        role="researcher",
        model="claude-haiku-4-5",
        status="completed",
        exit_reason="completed",
        duration_seconds=42.5,
        input_tokens=1000,
        output_tokens=200,
        cost_usd=0.012,
        api_calls=3,
        max_iterations=30,
    )
    assert ds.record(s) is True
    path = tmp_path / "delegation_stats.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["role"] == "researcher"
    assert data[0]["cost_usd"] == 0.012


def test_record_appends_to_existing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ds.record(ds.DelegationStat(role="r1", status="completed"))
    ds.record(ds.DelegationStat(role="r2", status="completed"))
    data = json.loads((tmp_path / "delegation_stats.json").read_text())
    assert [r["role"] for r in data] == ["r1", "r2"]


def test_record_disabled_via_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DELEGATION_STATS_DISABLED", "1")
    assert ds.record(ds.DelegationStat(role="r", status="completed")) is False
    assert not (tmp_path / "delegation_stats.json").exists()


def test_record_recovers_from_corrupt_file(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "delegation_stats.json").write_text("not json", encoding="utf-8")
    assert ds.record(ds.DelegationStat(role="recovered", status="completed")) is True
    data = json.loads((tmp_path / "delegation_stats.json").read_text())
    assert len(data) == 1
    assert data[0]["role"] == "recovered"


def test_load_all_returns_empty_when_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert ds.load_all() == []


def test_load_all_filters_unknown_fields(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw = [
        {"role": "r", "status": "completed", "future_field": "ignored"},
        {"not": "a real record"},
    ]
    (tmp_path / "delegation_stats.json").write_text(json.dumps(raw))
    out = ds.load_all()
    # Both reconstruct: dataclass is fully optional, so the second one
    # also reconstructs as an empty record. Confirm we got 2 back and
    # neither carries the unknown `future_field` attribute.
    assert len(out) == 2
    assert not hasattr(out[0], "future_field")


# ── aggregate ─────────────────────────────────────────────────────────────


def _stat(role, model, status="completed", **kwargs):
    return ds.DelegationStat(role=role, model=model, status=status, **kwargs)


def test_aggregate_groups_by_role_and_model():
    stats = [
        _stat("r", "haiku", duration_seconds=10, output_tokens=100, cost_usd=0.01),
        _stat("r", "haiku", duration_seconds=20, output_tokens=200, cost_usd=0.02),
        _stat("r", "sonnet", duration_seconds=30, output_tokens=300, cost_usd=0.10),
        _stat("c", "sonnet", duration_seconds=40, output_tokens=400, cost_usd=0.30),
    ]
    aggs = ds.aggregate(stats)
    assert [(a.role, a.model) for a in aggs] == [
        ("c", "sonnet"),
        ("r", "sonnet"),
        ("r", "haiku"),
    ]
    haiku = [a for a in aggs if a.model == "haiku"][0]
    assert haiku.n == 2
    assert haiku.total_cost == pytest.approx(0.03)
    assert haiku.avg_duration == 15.0
    assert haiku.avg_output == 150.0


def test_aggregate_filters_by_role():
    stats = [_stat("a", "h"), _stat("b", "h")]
    aggs = ds.aggregate(stats, role="a")
    assert len(aggs) == 1
    assert aggs[0].role == "a"


def test_aggregate_filters_by_since_ts():
    now = time.time()
    stats = [
        _stat("a", "h", ts=now - 86400 * 5),
        _stat("a", "h", ts=now - 60),
    ]
    aggs = ds.aggregate(stats, since_ts=now - 3600)
    assert len(aggs) == 1
    assert aggs[0].n == 1


def test_aggregate_buckets_untagged_separately():
    stats = [
        _stat("", "haiku"),
        _stat("", "haiku"),
        _stat("researcher", "haiku"),
    ]
    aggs = ds.aggregate(stats)
    roles = {a.role for a in aggs}
    assert "(untagged)" in roles
    assert "researcher" in roles


def test_aggregate_counts_status_correctly():
    stats = [
        _stat("r", "h", status="completed"),
        _stat("r", "h", status="completed"),
        _stat("r", "h", status="failed"),
        _stat("r", "h", status="interrupted"),
    ]
    agg = ds.aggregate(stats)[0]
    assert agg.n == 4
    assert agg.n_completed == 2
    assert agg.n_failed == 1
    assert agg.n_interrupted == 1
    assert agg.success_rate == 0.5


def test_aggregate_counts_hit_max_iter():
    stats = [
        _stat("r", "h", hit_max_iter=True),
        _stat("r", "h", hit_max_iter=True),
        _stat("r", "h", hit_max_iter=False),
    ]
    agg = ds.aggregate(stats)[0]
    assert agg.n_hit_max == 2
    assert agg.hit_max_rate == pytest.approx(2 / 3)


# ── suggest_retunes ───────────────────────────────────────────────────────


def test_suggest_promotes_on_hit_max():
    stats = [
        _stat("coder", "claude-sonnet-4-6", hit_max_iter=True),
        _stat("coder", "claude-sonnet-4-6", hit_max_iter=True),
        _stat("coder", "claude-sonnet-4-6", hit_max_iter=False),
        _stat("coder", "claude-sonnet-4-6", hit_max_iter=False),
        _stat("coder", "claude-sonnet-4-6", hit_max_iter=False),
    ]
    aggs = ds.aggregate(stats)
    sugs = ds.suggest_retunes(aggs)
    assert len(sugs) == 1
    assert sugs[0].role == "coder"
    assert sugs[0].suggested_model == "claude-opus-4-7"
    assert sugs[0].direction == "promote"


def test_suggest_promotes_on_low_success():
    stats = [
        _stat("r", "claude-haiku-4-5", status="completed"),
        _stat("r", "claude-haiku-4-5", status="failed"),
        _stat("r", "claude-haiku-4-5", status="failed"),
        _stat("r", "claude-haiku-4-5", status="failed"),
        _stat("r", "claude-haiku-4-5", status="completed"),
    ]
    aggs = ds.aggregate(stats)
    sugs = ds.suggest_retunes(aggs)
    assert len(sugs) == 1
    assert sugs[0].direction == "promote"
    assert sugs[0].suggested_model == "claude-sonnet-4-6"


def test_suggest_demotes_on_clean_low_output():
    stats = [
        _stat("r", "claude-sonnet-4-6", status="completed", output_tokens=100)
        for _ in range(10)
    ]
    aggs = ds.aggregate(stats)
    sugs = ds.suggest_retunes(aggs)
    assert len(sugs) == 1
    assert sugs[0].direction == "demote"
    assert sugs[0].suggested_model == "claude-haiku-4-5"


def test_suggest_skips_below_min_samples():
    stats = [_stat("r", "claude-sonnet-4-6", hit_max_iter=True) for _ in range(4)]
    aggs = ds.aggregate(stats)
    assert ds.suggest_retunes(aggs) == []


def test_suggest_skips_unknown_models():
    stats = [_stat("r", "weird-model", hit_max_iter=True) for _ in range(10)]
    aggs = ds.aggregate(stats)
    assert ds.suggest_retunes(aggs) == []


def test_suggest_skips_untagged():
    stats = [_stat("", "claude-sonnet-4-6", hit_max_iter=True) for _ in range(10)]
    aggs = ds.aggregate(stats)
    assert ds.suggest_retunes(aggs) == []


def test_suggest_haiku_cant_demote_below():
    stats = [
        _stat("r", "claude-haiku-4-5", status="completed", output_tokens=50)
        for _ in range(10)
    ]
    aggs = ds.aggregate(stats)
    sugs = ds.suggest_retunes(aggs)
    assert sugs == []


def test_suggest_opus_cant_promote_above():
    stats = [_stat("r", "claude-opus-4-7", hit_max_iter=True) for _ in range(10)]
    aggs = ds.aggregate(stats)
    sugs = ds.suggest_retunes(aggs)
    assert sugs == []
