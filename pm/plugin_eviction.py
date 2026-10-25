"""Keep an update alive when an enabled plugin no longer fits the new core.

Admission refuses a plugin that does not fit, because the user is choosing and can
choose again. An update has nobody to ask and must never fail because of a plugin:
core moved (a newer Python, a bumped pin, a newer manifest contract) under a plugin
that was admitted against the old core. Such a plugin is disabled in every home that
enables it, the reason reaches the operator and the receipt, and the update continues
with the rest. Only a core that cannot build on its own still fails. The edit is recorded
in the home's config, so every later update retries the plugin and undoes the edit once it
fits again; a user who re-enables, disables or removes it takes the decision back.

Disabling needs evidence about the plugin itself: its requires-python against the pinned
interpreter, its manifest contract, a resolver proof, or its build failing. A fetch or
tooling failure could be the moment, so the plugin gets one retry before it is disabled.
requires_hermes is judged against a version identity that can lag (a checkout without its
release tags), so a misfit there only sits out: config is untouched, boot skips it the same
way, and it rejoins when the verdict flips. A secondary profile whose config cannot be read
sits out until its config is fixed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

from pm.environment import BuildFailure, ResolutionConflict
from pm.environments import install_state_dir, runtime_facts_path
from pm.filesystem import durable_write_bytes, file_digest, native, read_bytes_or_none
from pm.package import InstallError
from pm.plugins_state import RECORD

Entry = tuple[Path, str, Path]  # (home plugins dir, selection key, plugin dir)


def _interpreter_version() -> str:
    """The Python the generation is built for; a plugin's requires-python is judged against it."""
    from pm._uv import _toolchain

    tools = _toolchain(explicit=True)
    if tools is None:
        raise InstallError("venv", "PM's pinned toolchain is unavailable")
    probe = subprocess.run([native(tools[1]), "-I", "-c", "import platform; print(platform.python_version())"],
                           capture_output=True, text=True, check=True, timeout=60)
    return probe.stdout.strip()


def static_verdicts(entries: list[Entry], python_version: str) -> tuple[dict[Path, str], dict[Path, str]]:
    """``(disable, sit out)`` reasons found without a resolver, keyed by resolved dir."""
    from hermes_cli.plugins_manifest import requires_hermes_error
    from pm.plugin_declarations import manifest_version_error, read_python_declaration

    reasons: dict[Path, str] = {}
    waiting: dict[Path, str] = {}
    for _plugins_dir, _name, plugin_dir in entries:
        key = plugin_dir.resolve()
        if key in reasons or key in waiting:
            continue
        try:
            declaration = read_python_declaration(plugin_dir)
        except (OSError, ValueError, TypeError) as exc:
            reasons[key] = f"its dependency declaration is invalid: {exc}"
            continue
        # Mirrors enabled_member_dirs, so the recorded stamp is the one boot expects.
        hermes = requires_hermes_error(declaration.manifest)
        if hermes:
            waiting[key] = hermes
            continue
        manifest = manifest_version_error(declaration.manifest, plugin_dir.name)
        reason = (manifest.removeprefix(f"Plugin '{plugin_dir.name}' ") if manifest
                  else declaration.python_error(python_version))
        if reason:
            reasons[key] = reason
    return reasons, waiting


def _discard(names: list, spellings: set[str]) -> None:
    """Remove *spellings* from a round-tripped list in place, so order and comments survive."""
    for index in reversed(range(len(names))):
        if names[index] in spellings:
            del names[index]


def _list(plugins, key: str) -> list:
    if plugins.get(key) is None:
        plugins[key] = []
    return plugins[key]


def _disable(plugins, name: str, plugin_dir: Path, reason: str) -> None:
    """Park *name* in plugins.disabled, drop it from plugins.enabled and record what to undo."""
    from pm.plugins_state import plugin_spellings

    spellings = plugin_spellings(plugin_dir, name)
    disabled, enabled = _list(plugins, "disabled"), plugins.get("enabled")
    if not isinstance(plugins.get(RECORD), dict):
        plugins[RECORD] = {}
    earlier = plugins[RECORD].get(name)
    if name in disabled and isinstance(earlier, dict):
        was_enabled = bool(earlier.get("enabled"))  # a retry that still fails keeps the first record
    else:
        was_enabled = isinstance(enabled, list) and bool(spellings & set(enabled))
    if name not in disabled:
        disabled.append(name)
    if isinstance(enabled, list):
        _discard(enabled, spellings)
    plugins[RECORD][name] = {"enabled": was_enabled, "reason": " ".join(reason.split())[-300:]}


def _rejoin(plugins, name: str, plugin_dir: Path) -> None:
    """Undo an earlier update's _disable now that the plugin fits again."""
    from pm.plugins_state import plugin_spellings

    record = plugins.get(RECORD) if isinstance(plugins.get(RECORD), dict) else {}
    earlier = record.get(name)
    _discard(_list(plugins, "disabled"), plugin_spellings(plugin_dir, name))
    if isinstance(earlier, dict) and earlier.get("enabled") and name not in _list(plugins, "enabled"):
        plugins["enabled"].append(name)


def settle_record(plugins, settled=()) -> None:
    """A user's own selection takes the decision back: forget the record of every plugin they just
    enabled, disabled or removed (*settled*), so no later update turns it back on."""
    record = plugins.get(RECORD)
    for name in settled if isinstance(record, dict) else ():
        record.pop(name, None)
    _prune_record(plugins)


def _prune_record(plugins) -> None:
    """Forget records whose plugin left plugins.disabled (the user re-enabled or removed it)."""
    record = plugins.get(RECORD)
    if not isinstance(record, dict):
        plugins.pop(RECORD, None)
        return
    disabled = set(plugins.get("disabled") or ())
    for name in [name for name in record if name not in disabled]:
        del record[name]
    if not record:
        del plugins[RECORD]


def recorded_evictions() -> list[Entry]:
    """Plugins an earlier update disabled that their home still has disabled: retried each update."""
    from pm.plugins_state import dependency_homes, read_home_selection
    from pm.workspace import selected_plugin_dir

    entries: list[Entry] = []
    for home in dependency_homes():
        try:
            plugins = (read_home_selection(home) or {}).get("plugins") or {}
        except ValueError:
            continue  # sync_evicting already reports an unreadable profile
        record, disabled = plugins.get(RECORD), plugins.get("disabled") or []
        for name in record if isinstance(record, dict) else ():
            if not isinstance(name, str) or name not in disabled:
                continue
            try:
                plugin_dir = selected_plugin_dir(home / "plugins", name)
            except InstallError:
                continue  # a hand-edited key that names no plugin tree
            if plugin_dir is not None:
                entries.append((home / "plugins", name, plugin_dir))
    return entries


class PluginEviction:
    """One update's config edits: disable the plugins in *reasons*, re-enable *rejoined* ones.

    A disabled plugin leaves plugins.enabled under every spelling and is recorded under
    plugins.disabled_by_update (was it enabled, and why), so a later update can retry it and undo
    exactly this edit. memory.provider is left alone: plugins.disabled keeps a parked provider out
    of the environment and the loader, and re-enabling the plugin brings the provider back with it.
    Published like a plugin selection.
    """

    def __init__(self, entries: list[Entry], reasons: dict[Path, str], rejoined: list[Entry] = ()):
        from hermes_yaml import roundtrip_yaml
        from pm.publication import selection_snapshot

        self.configs = selection_snapshot()
        by_home: dict[Path, list[tuple[str, Path, str | None]]] = {}
        for plugins_dir, name, plugin_dir in entries:
            if plugin_dir.resolve() in reasons:
                by_home.setdefault(plugins_dir.parent, []).append((name, plugin_dir, reasons[plugin_dir.resolve()]))
        for plugins_dir, name, plugin_dir in rejoined:
            by_home.setdefault(plugins_dir.parent, []).append((name, plugin_dir, None))
        self.edits: list[tuple[Path, bytes | None, bytes]] = []
        for home, changes in by_home.items():
            path = home / "config.yaml"
            previous = read_bytes_or_none(path)
            yaml = roundtrip_yaml()
            config = (yaml.load(previous.decode("utf-8-sig")) if previous else None) or {}
            # read_home_selection already proved plugins is a mapping and the lists are lists.
            plugins = config.get("plugins")
            if plugins is None:
                plugins = config["plugins"] = {}
            for name, plugin_dir, reason in changes:
                if reason is None:
                    _rejoin(plugins, name, plugin_dir)
                else:
                    _disable(plugins, name, plugin_dir, reason)
            _prune_record(plugins)
            output = io.StringIO()
            yaml.dump(config, output)
            self.edits.append((path, previous, output.getvalue().encode("utf-8")))

    def publish(self, project: Path) -> None:
        from pm.publication import selection_snapshot

        if selection_snapshot() != self.configs:
            raise ValueError("plugin configuration changed while preparing publication; retry")
        row = {"configs": [{"config": str(path),
                            "previous": base64.b64encode(previous).decode() if previous is not None else None,
                            "config_after": hashlib.sha256(proposed).hexdigest()}
                           for path, previous, proposed in self.edits],
               "facts_before": file_digest(runtime_facts_path(project))}
        durable_write_bytes(install_state_dir(project) / "publication.json", json.dumps(row).encode())
        for path, _previous, proposed in self.edits:
            durable_write_bytes(path, proposed)


def _trial(package, enabled, explicit: bool, plugin_dirs: list[Path]) -> str | None:
    """Why the last of *plugin_dirs* cannot join the build, or None when it builds."""
    cause = ""
    # A fetch or tooling failure can be the moment rather than the plugin: one more try.
    for _attempt in range(2):
        try:
            package.apply(enabled, explicit=explicit, plugin_dirs=plugin_dirs, skip_invalid_secondary=True)
            return None
        except (ResolutionConflict, BuildFailure) as exc:
            return f"the dependency environment no longer builds with it: {exc.cause[-400:]}"
        except InstallError as exc:
            cause = exc.cause
    return f"its dependencies could not be prepared, twice: {cause[-400:]}"


def sync_evicting(package, facts, fact: dict, *, extras, shipped, frozen, explicit: bool) -> None:
    """Build the discovered selection, disabling whatever plugin keeps it from building.

    Static misfits go first (no resolver needed). If the rest still fails, core alone is
    built to prove the plugins are the cause, then members are re-added in config order
    and each one that breaks the build is disabled too. Plugins an earlier update disabled
    are retried after the enabled ones and re-enabled when they fit.
    """
    from pm import receipt
    from pm.install import _commit_selection, _runtime_state_matches, _target_selection
    from pm.plugins_state import dependency_homes, read_home_selection
    from pm.workspace import _is_member_candidate, enabled_plugin_entries

    notices: list[str] = []
    for home in dependency_homes()[1:]:
        try:
            read_home_selection(home)
        except ValueError as exc:
            notices.append(f"Skipped the plugins of profile {home}: {exc}; they rejoin once its config.yaml is fixed")
    entries = enabled_plugin_entries(skip_invalid_secondary=True)
    retrying = recorded_evictions()
    reasons, waiting = static_verdicts([*entries, *retrying], _interpreter_version())

    def fits(plugin_dir: Path) -> bool:
        return plugin_dir.resolve() not in reasons and plugin_dir.resolve() not in waiting

    def members() -> list[Path]:
        return list(dict.fromkeys(plugin_dir for _plugins_dir, _name, plugin_dir in [*entries, *retrying]
                                  if fits(plugin_dir) and _is_member_candidate(plugin_dir)))

    def commit() -> None:
        enabled, stamp, inputs = _target_selection(package, fact, extras=extras, inputs={"plugin_dirs": members()},
                                                   repair=False, shipped=shipped, frozen=frozen)
        receipt.record_feature_list(enabled)
        rejoined = [entry for entry in retrying if fits(entry[2])]
        edit = PluginEviction([*entries, *retrying], reasons, rejoined) if reasons or rejoined else None
        _commit_selection(package, facts, edit, enabled=enabled, stamp=stamp, inputs=inputs,
                          current=_runtime_state_matches(fact, stamp), repair=False, explicit=explicit,
                          skip_invalid_secondary=True)

    kept = members()
    try:
        commit()
    except InstallError as failure:
        if not kept:
            raise
        enabled = _target_selection(package, fact, extras=extras, inputs={"plugin_dirs": []},
                                    repair=False, shipped=shipped, frozen=frozen)[0]
        try:
            package.apply(enabled, explicit=explicit, plugin_dirs=[], skip_invalid_secondary=True)
        except InstallError:
            raise failure from None
        fitting: list[Path] = []
        for member in kept:
            reason = _trial(package, enabled, explicit, [*fitting, member])
            if reason:
                reasons[member.resolve()] = reason
            else:
                fitting.append(member)
        commit()
    notices += _outcome_notices(entries, retrying, reasons, waiting)
    for message in notices:
        print(f"⚠ {message}", file=sys.stderr, flush=True)
        receipt.record_warning(message)


def _outcome_notices(entries: list[Entry], retrying: list[Entry], reasons: dict[Path, str],
                     waiting: dict[Path, str]) -> list[str]:
    notices = []
    for plugins_dir, name, plugin_dir in entries:
        key = plugin_dir.resolve()
        if key in reasons:
            notices.append(f"Disabled plugin '{name}' in {plugins_dir.parent}: {reasons[key]}; every update retries "
                           f"it and turns it back on once it fits, or run `hermes plugins enable {name}` sooner")
        elif key in waiting:
            notices.append(f"Left plugin '{name}' in {plugins_dir.parent} out of this update: {waiting[key]}; "
                           "it stays enabled and rejoins once Hermes reports a version it accepts")
    for plugins_dir, name, plugin_dir in retrying:
        why = reasons.get(plugin_dir.resolve()) or waiting.get(plugin_dir.resolve())
        notices.append(f"Plugin '{name}' in {plugins_dir.parent} is still disabled: {why}; the next update retries it"
                       if why else f"Turned plugin '{name}' in {plugins_dir.parent} back on: it fits this Hermes again")
    return notices
