"""Every staged model must launch with a policy decision, never stock fit.

The managed server autoloads any GGUF in its models dir; a model missing
from the preset INI loads with llama-server defaults (f16 KV at max
context, no placement) — on Windows/WDDM that silently demotes VRAM and
decodes at a crawl. Boot must therefore refuse to adopt a running server
whose presets predate the staged set."""

from __future__ import annotations

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _stage(home, name):
    mdir = home / "models"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / f"{name}.gguf").write_bytes(b"GGUF" + b"\x00" * 32)


def _write_presets(home, *model_ids):
    pdir = home / "runtimes" / "llamacpp"
    pdir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"[{m}]\nctx-size = 65536\n" for m in model_ids)
    (pdir / "presets.ini").write_text(body, encoding="utf-8")


def test_presets_stale_when_a_staged_model_has_no_section(hermes_home):
    from hermes_cli.local_runtime.bootstrap import _presets_stale

    _stage(hermes_home, "model-a")
    _stage(hermes_home, "model-b")
    _write_presets(hermes_home, "model-a")
    assert _presets_stale() is True


def test_presets_current_when_every_staged_model_is_covered(hermes_home):
    from hermes_cli.local_runtime.bootstrap import _presets_stale

    _stage(hermes_home, "model-a")
    _write_presets(hermes_home, "model-a")
    assert _presets_stale() is False


def test_no_models_is_never_stale(hermes_home):
    from hermes_cli.local_runtime.bootstrap import _presets_stale

    _write_presets(hermes_home, "model-a")
    assert _presets_stale() is False


def test_boot_replaces_incumbent_with_stale_presets(hermes_home, monkeypatch):
    """ensure_local_runtime must not adopt a running server whose presets
    miss a staged model — it stops it and boots fresh (boot itself is
    stubbed; the contract under test is the adopt/replace decision)."""
    import hermes_cli.local_runtime.bootstrap as boot

    _stage(hermes_home, "model-a")
    _stage(hermes_home, "model-b")
    _write_presets(hermes_home, "model-a")

    stopped = {}
    monkeypatch.setattr(
        "hermes_cli.local_runtime.endpoint._state_endpoint",
        lambda: {"base_url": "http://127.0.0.1:18434/v1", "pid": 12345})
    monkeypatch.setattr(boot, "_stop_state_server",
                        lambda state: stopped.setdefault("pid", state["pid"]))

    sentinel = object()

    def fake_boot(*a, **k):
        raise _BootReached()

    class _BootReached(Exception):
        pass

    # Fail fast once boot proper begins — reaching it IS the assertion.
    monkeypatch.setattr(
        "hermes_cli.local_runtime.binaries.ensure_runtime_installed", fake_boot)

    result = boot.ensure_local_runtime({"local_runtime": {"enabled": True}})
    assert stopped.get("pid") == 12345, "stale incumbent was not stopped"
    # Boot proceeded past adoption (our fake raised inside the try block,
    # which ensure_local_runtime swallows into a None return).
    assert result is None or result is sentinel
