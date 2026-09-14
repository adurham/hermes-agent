"""Build identity and process environment from an admitted preparation."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.bundles.desktop_prepare import PreparedDesktop


@contextmanager
def build_lock(source: Path):
    from hermes_cli.runtime_state import _lock

    directory = source / ".build"
    if directory.is_symlink():
        raise ValueError("desktop build scratch must not be a symlink")
    directory.mkdir(exist_ok=True)
    with (directory / ".desktop-build.lock").open("a+b") as lock:
        if not _lock(lock.fileno(), wait=False):
            raise ValueError("another desktop build or preparation is using this source checkout")
        yield


def build_environment(prepared: PreparedDesktop, variant: str, inherited: Mapping[str, str]) -> dict[str, str]:
    from pm.build_operations import verified_tools
    from pm.lock import Lockfile
    from scripts.bundles.desktop_toolchain import bootstrap_environment

    request = prepared.request
    env = bootstrap_environment(request.source, request.work, request.cache, inherited)
    selection = verified_tools(["python", "node", "npm"], source_store=request.cache / "tools",
                               target=request.target, lock=Lockfile(request.source / "pm/lock.json"))
    if prepared.python != selection.entries["python"].binary or prepared.node != selection.entries["node"].binary:
        raise ValueError("prepared Python/Node paths do not match selected tools; prepare again")
    env = selection.environment(env)
    env.update(CI="true", PYTHONUTF8="1", GITHUB_SHA=request.commit,
               HERMES_DESKTOP_VARIANT=variant, HERMES_PYTHON=str(prepared.python),
               HERMES_PAYLOAD_VERSION=request.version,
               HERMES_BUNDLE_ENV_JSON=json.dumps(request.bundle_env, sort_keys=True))
    env.pop("BUILD_NUMBER", None)
    env.pop("GITHUB_HEAD_REF", None)
    if request.tag is None:
        env["HERMES_BUILD_COMMIT"] = request.commit
        env.pop("HERMES_PAYLOAD_TAG", None)
        env.pop("GITHUB_REF_NAME", None)
    else:
        env.pop("HERMES_BUILD_COMMIT", None)
        env["HERMES_PAYLOAD_TAG"] = request.tag
        env["GITHUB_REF_NAME"] = request.tag
    return env


def packaging_environment(build: Mapping[str, str], inherited: Mapping[str, str],
                          target: str) -> dict[str, str]:
    env = dict(build)
    if target.startswith("darwin-"):
        # Security.framework needs the login HOME for both key import and signing,
        # even with an explicit keychain. Keep dependency preparation isolated.
        env["HOME"] = inherited.get("HERMES_REAL_HOME") or inherited.get("HOME") or str(Path.home())
    return env


def select_variant(prepared: PreparedDesktop, variant: str | None) -> str:
    from scripts.termux.deb_version import channel_for_tag

    request = prepared.request
    variant = variant or request.variant
    if variant != request.variant and not (
        {variant, request.variant} == {"bundled", "store"} and request.tag
        and channel_for_tag(request.tag) == "stable"
    ):
        raise ValueError("requested variant was not prepared")
    return variant
