"""profiles.configure ``enabled_toolsets`` must pin the key the runtime reads.

Invariant: whatever ``_save_toolset_pin`` writes, ``_load_enabled_toolsets`` (via ``_get_platform_tools(cfg, "cli")``)
reads back as the enabled set, and ``_describe_toolsets`` reports the same pin. Before the fix the writer used a
``tools.enabled_toolsets`` key that nothing reads, so a pin of ``[web]`` still produced the full default set.
"""

import pytest

from tui_gateway.methods_profiles import _describe_toolsets, _save_toolset_pin


def _pin_and_resolve(cfg: dict, names: list[str]) -> tuple[set[str], set[str] | None]:
    from hermes_cli.tools_config import _get_platform_tools

    _save_toolset_pin(cfg, names, save_config=lambda _c: None)
    resolved = set(_get_platform_tools(cfg, "cli", include_default_mcp_servers=False))
    _toolsets, pinned = _describe_toolsets(cfg)
    return resolved, pinned


def test_toolset_pin_is_what_the_runtime_reads():
    cfg: dict = {}
    resolved, pinned = _pin_and_resolve(cfg, ["web"])
    # `resolved` may carry non-configurable core toolsets the runtime recovers
    # into every platform set (see _recover_platform_native_toolsets); the pin's
    # job is that `web` resolves and nothing *configurable* else does.
    assert "web" in resolved, f"runtime resolved {sorted(resolved)} for a [web] pin"
    from hermes_cli.tools_config import _configurable_keys
    leaked = resolved & (_configurable_keys() - {"web"})
    assert not leaked, f"configurable toolsets leaked past a [web] pin: {sorted(leaked)}"
    assert pinned == {"web"}
    assert "enabled_toolsets" not in (cfg.get("tools") or {}), "writer must not leave the phantom key behind"


@pytest.mark.parametrize("second", [["file", "terminal"], []])
def test_toolset_pin_round_trips_a_then_b_then_a(second):
    cfg: dict = {}
    first, _ = _pin_and_resolve(cfg, ["web"])
    _pin_and_resolve(cfg, second)
    again, pinned = _pin_and_resolve(cfg, ["web"])
    assert first == again, f"pin did not round-trip: {sorted(first)} -> {sorted(again)}"
    assert "web" in again
    assert pinned == {"web"}


def test_empty_pin_clears_the_pin_and_falls_back_to_the_platform_default():
    from hermes_cli.tools_config import _get_platform_tools

    cfg: dict = {}
    _pin_and_resolve(cfg, ["web"])
    _save_toolset_pin(cfg, [], save_config=lambda _c: None)
    _toolsets, pinned = _describe_toolsets(cfg)
    assert pinned is None
    assert set(_get_platform_tools(cfg, "cli", include_default_mcp_servers=False)) != {"web"}
