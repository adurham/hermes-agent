"""Regression tests for packaging metadata in pyproject.toml."""

from pathlib import Path
import tomllib

def _load_optional_dependencies():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return project["optional-dependencies"]


def test_wake_dependencies_and_runtime_gate_agree_on_supported_targets():
    from packaging.markers import Marker, default_environment
    from packaging.requirements import Requirement

    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
    optional = metadata["project"]["optional-dependencies"]
    gates = metadata["tool"]["hermes"]["extras-platforms"]
    targets = [
        ("darwin", "Darwin", "x86_64", False),
        ("darwin", "Darwin", "arm64", True),
        ("linux", "Linux", "x86_64", True),
        ("linux", "Linux", "aarch64", True),
        ("win32", "Windows", "AMD64", True),
        ("win32", "Windows", "ARM64", False),
    ]
    for system, platform_system, machine, supported in targets:
        environment = {**default_environment(), "sys_platform": system,
                       "platform_system": platform_system, "platform_machine": machine}
        for extra in ("wake", "wake-openwakeword"):
            requirement = next(req for spec in optional[extra]
                               if (req := Requirement(spec)).name == "pyopen-wakeword")
            assert requirement.marker.evaluate(environment) is supported, (extra, system, machine)
        assert Marker(gates["wake-openwakeword"]).evaluate(environment) is supported
        selected = {req.name for spec in optional["wake"]
                    if (req := Requirement(spec)).marker is None or req.marker.evaluate(environment)}
        assert {"sherpa-onnx", "pvporcupine", "sentencepiece", "pypinyin"} <= selected
        sherpa_deps = {req.name for spec in optional["wake-sherpa"]
                       if (req := Requirement(spec)).marker is None or req.marker.evaluate(environment)}
        assert {"sherpa-onnx", "sentencepiece", "pypinyin"} <= sherpa_deps
        for extra in ("wake-sherpa", "wake-porcupine"):
            assert all(req.marker is None or req.marker.evaluate(environment)
                       for req in map(Requirement, optional[extra]))
            gate = gates.get(extra)
            assert gate is None or Marker(gate).evaluate(environment)


def test_direct_overrides_preserve_the_declared_exact_version():
    from packaging.requirements import Requirement

    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8-sig"))
    direct = {req.name: req for spec in metadata["project"]["dependencies"]
              if (req := Requirement(spec)).specifier}
    for spec in metadata["tool"]["uv"].get("override-dependencies", []):
        override = Requirement(spec)
        requirement = direct.get(override.name)
        if requirement is None or not any(s.operator == "==" for s in requirement.specifier):
            continue
        assert override.specifier == requirement.specifier, (
            f"{override.name}: override {override.specifier} contradicts exact requirement {requirement.specifier}"
        )
        versions = {row["version"] for row in locked["package"] if row["name"] == override.name}
        assert versions and all(version in requirement.specifier for version in versions)


def test_opt_in_extras_stay_out_of_default_recursive_selection():
    from packaging.requirements import Requirement
    from pm.extras import ANCHORS

    optional = _load_optional_dependencies()
    # Deliberate eager surfaces: core Google integration, ACP launcher,
    # dashboard, transcript reader, and the no-op Pillow compatibility alias.
    eager = {"google", "acp", "web", "youtube", "vision"}
    selected, pending = set(), ["all"]
    while pending:
        extra = pending.pop()
        if extra in selected:
            continue
        selected.add(extra)
        for requirement in map(Requirement, optional[extra]):
            if requirement.name == "hermes-agent":
                pending.extend(requirement.extras)
    assert set(ANCHORS) <= optional.keys()
    assert not (selected & (set(ANCHORS) - eager))


def test_dingtalk_extra_includes_qrcode_for_qr_auth():
    """DingTalk's QR-code device-flow auth (hermes_cli/dingtalk_auth.py)
    needs the qrcode package."""
    optional_dependencies = _load_optional_dependencies()

    dingtalk_extra = optional_dependencies["dingtalk"]
    assert any(dep.startswith("qrcode") for dep in dingtalk_extra)
