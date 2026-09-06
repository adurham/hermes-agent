"""Behavioral contract: the lazy-install surface is pm, not the deleted tools.lazy_deps.

tools/lazy_deps.py was deleted by the pm migration; pm.extras (available /
ensure_import) is the only lazy-install surface. The migrated real modules
keep working call paths — verified here through their public functions with
the pm seam monkeypatched (DI at the real boundary, no source scanning).

Callers pass the pm extra (pyproject extra name) directly — there is no
legacy feature-id translation layer.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def pm_seam(monkeypatch):
    """Patch the pm module attributes the migrated modules import at call time."""
    import pm

    seen: list[str] = []
    monkeypatch.setattr(pm, "ensure_import", lambda extra: seen.append(extra))
    monkeypatch.setattr(pm, "sync_venv", lambda extras=None, explicit=False: seen.extend(extras or ()))
    monkeypatch.setattr(pm, "available", lambda extra: False)
    monkeypatch.setattr(pm, "lazy_installs_allowed", lambda: True)
    return seen


def test_web_common_lazy_ensure_passes_extra_through(pm_seam):
    from plugins.web import _common

    _common.lazy_ensure("exa")
    _common.lazy_ensure("parallel-web")
    assert pm_seam == ["exa", "parallel-web"]


def test_web_common_lazy_ensure_swallows_import_error(monkeypatch):
    import pm
    from plugins.web import _common

    def _boom(extra):
        raise ImportError("nope")

    monkeypatch.setattr(pm, "ensure_import", _boom)
    _common.lazy_ensure("exa")  # must not raise


def test_web_common_lazy_ensure_reraises_other_failures_as_import_error(monkeypatch):
    import pm
    from plugins.web import _common

    def _blocked(extra):
        raise RuntimeError("lazy installs disabled")

    monkeypatch.setattr(pm, "ensure_import", _blocked)
    with pytest.raises(ImportError):
        _common.lazy_ensure("exa")


def test_remote_common_ensures_declared_extras(pm_seam):
    from tools.environments import remote_common

    remote_common.ensure_lazy_dep("modal")
    remote_common.ensure_lazy_dep("daytona")
    assert pm_seam == ["modal", "daytona"]


def test_remote_common_reraises_failure_as_import_error(monkeypatch):
    from tools.environments import remote_common

    def _blocked(extra):
        raise RuntimeError("not supported on this platform")

    import pm
    monkeypatch.setattr(pm, "ensure_import", _blocked)
    with pytest.raises(ImportError):
        remote_common.ensure_lazy_dep("modal")


def test_transcription_common_quietly_ensures(pm_seam):
    from tools import transcription_common

    transcription_common._lazy_ensure_quietly("silk")
    transcription_common._lazy_ensure_quietly("mistral")
    assert pm_seam == ["silk", "mistral"]


def test_transcription_common_swallows_all_failures(monkeypatch):
    import pm
    from tools import transcription_common

    def _boom(extra):
        raise RuntimeError("offline")

    monkeypatch.setattr(pm, "ensure_import", _boom)
    transcription_common._lazy_ensure_quietly("silk")  # must not raise


def test_tts_lifecycle_warms_through_pm_extras(pm_seam, monkeypatch):
    from tools import tts_tool_lifecycle

    result = tts_tool_lifecycle.warm_tts_provider({"provider": "edge"})
    assert result == {"provider": "edge", "warmed": True, "action": "installed"}
    assert pm_seam == ["edge-tts"]


def test_tts_lifecycle_feature_map_targets_declared_extras():
    """Every mapped pm extra must be a real pyproject extra with an anchor."""
    from pm.extras import ANCHORS
    from tools import tts_tool_lifecycle

    assert tts_tool_lifecycle._LAZY_SDK_FEATURES
    for extra in tts_tool_lifecycle._LAZY_SDK_FEATURES.values():
        assert extra in ANCHORS, extra
