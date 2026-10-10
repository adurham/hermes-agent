"""The dashboard's built-in auxiliary slots cover every built-in auxiliary task.

``_AUX_TASK_SLOTS`` gates ``GET /api/model/auxiliary`` rows, ``POST /api/model/set``
validation (``unknown auxiliary task``), ``__reset__`` and the stale-pin nudge. It is a
hand-maintained mirror of ``DEFAULT_CONFIG["auxiliary"]`` and had drifted: goal_judge, monitor,
background_review, memory_query_rewrite and tts_audio_tags were configurable in config.yaml and
in ``hermes model`` but not addressable from the dashboard / Desktop Models page.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import hermes_cli.plugins as plugins_mod
from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.web_server_config import (
    _AUX_NON_SLOT_TASKS, _AUX_TASK_SLOTS, _apply_aux_assignment_sync, _stale_aux_pins,
)

# Per-task blocks only; scalar knobs (free_only, transient_retries, ...) are not tasks.
_BUILTIN_TASKS = {key for key, value in DEFAULT_CONFIG["auxiliary"].items() if isinstance(value, dict)}
_ADDED = ("memory_query_rewrite", "tts_audio_tags", "goal_judge", "monitor", "background_review")


def test_slots_cover_every_builtin_aux_task():
    missing = _BUILTIN_TASKS - _AUX_NON_SLOT_TASKS - set(_AUX_TASK_SLOTS)
    assert not missing, f"DEFAULT_CONFIG['auxiliary'] tasks missing from _AUX_TASK_SLOTS: {sorted(missing)}"


def test_slots_name_only_real_builtin_tasks():
    assert len(set(_AUX_TASK_SLOTS)) == len(_AUX_TASK_SLOTS)
    assert set(_AUX_TASK_SLOTS) <= _BUILTIN_TASKS
    assert _AUX_NON_SLOT_TASKS <= _BUILTIN_TASKS
    assert not _AUX_NON_SLOT_TASKS & set(_AUX_TASK_SLOTS)


@pytest.fixture
def no_plugins(monkeypatch):
    monkeypatch.setattr(plugins_mod, "get_plugin_auxiliary_tasks", lambda: [])
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)


@pytest.mark.parametrize("task", _ADDED)
def test_added_task_is_assignable_and_reported_stale(no_plugins, task):
    cfg: dict = {"auxiliary": {}}
    out = _apply_aux_assignment_sync(cfg, "openrouter", "vendor/small", task, "", "")
    assert out["tasks"] == [task]
    assert cfg["auxiliary"][task] == {"provider": "openrouter", "model": "vendor/small"}
    assert {"task": task, "provider": "openrouter", "model": "vendor/small"} in _stale_aux_pins(cfg, "nous")

    _apply_aux_assignment_sync(cfg, "", "", "__reset__", "", "")
    assert cfg["auxiliary"][task] == {"provider": "auto", "model": ""}


@pytest.mark.parametrize("task", sorted(_AUX_NON_SLOT_TASKS))
def test_moa_blocks_stay_unassignable(no_plugins, task):
    # MoA call sites pass the preset's provider/model explicitly; a pin here would be ignored.
    with pytest.raises(HTTPException) as exc:
        _apply_aux_assignment_sync({"auxiliary": {}}, "openrouter", "m", task, "", "")
    assert exc.value.status_code == 400 and "unknown auxiliary task" in exc.value.detail
