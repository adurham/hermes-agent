"""A restored cache is usable only for the exact proven build inputs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.termux import wheelhouse_cache


def cache_tree(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    payload = tmp_path / "payload"
    wheelhouse = payload / "wheelhouse"
    work = payload / ".work"
    wheelhouse.mkdir(parents=True)
    work.mkdir()
    (wheelhouse / "example-1.0-py3-none-any.whl").write_bytes(b"cache artifact")
    (work / "resolved.txt").write_text("example\t==1.0\t\n", encoding="utf-8")
    (work / "build_set.txt").write_text("example\n", encoding="utf-8")
    identity = {"lock": "lock-a", "python": "python-a", "builder": "image-a"}
    return payload, identity


def test_cache_rejects_changed_inputs_and_modified_outputs(tmp_path):
    payload, identity = cache_tree(tmp_path)
    wheelhouse_cache.write_manifest(payload, identity)
    assert wheelhouse_cache.is_usable(payload, identity)
    for key in identity:
        assert not wheelhouse_cache.is_usable(payload, {**identity, key: "changed"})
    artifact = next((payload / "wheelhouse").glob("*.whl"))
    artifact.write_bytes(b"corrupt artifact")
    assert not wheelhouse_cache.is_usable(payload, identity)


@pytest.mark.parametrize("damage", ["empty", "missing", "extra", "requirements", "build-set", "traversal", "malformed"])
def test_cache_rejects_incomplete_or_inconsistent_manifests(tmp_path, damage):
    payload, identity = cache_tree(tmp_path)
    wheelhouse_cache.write_manifest(payload, identity)
    manifest_path = payload / "index.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "empty":
        manifest["wheels"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif damage == "missing":
        next((payload / "wheelhouse").glob("*.whl")).unlink()
    elif damage == "extra":
        (payload / "wheelhouse/foreign-9.0-py3-none-any.whl").write_bytes(b"foreign")
    elif damage == "requirements":
        (payload / ".work/resolved.txt").write_text("other\t==9.0\t\n", encoding="utf-8")
    elif damage == "build-set":
        (payload / ".work/build_set.txt").unlink()
    elif damage == "traversal":
        manifest["wheels"][0]["name"] = "../outside.whl"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        manifest_path.write_text("{broken", encoding="utf-8")
    assert not wheelhouse_cache.is_usable(payload, identity)
