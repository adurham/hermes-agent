"""``save_config``'s managed-scope rule: a save never changes what the USER file says about a managed leaf.

The dict a caller saves is usually ``load_config()`` output, so it carries the managed overlay's
values. Writing them would copy administrator policy into the user's own file; deleting them (the
old behaviour) erased the user's own values, so a process started without the overlay (a service
unit missing ``HERMES_MANAGED_DIR``, a plain venv) silently fell back to wider defaults. Every leaf
the managed scope pins is therefore written back exactly as the file on disk has it: present keeps
its on-disk value, absent stays absent. Everything else saves normally.
"""

from __future__ import annotations

import copy
import sys
from typing import Any

_ABSENT = object()


def _get(tree: Any, parts: list[str]) -> Any:
    for part in parts:
        if not isinstance(tree, dict) or part not in tree:
            return _ABSENT
        tree = tree[part]
    return tree


def _restore_leaf(config: dict, on_disk: dict, parts: list[str]) -> None:
    """Make *config* hold at *parts* what *on_disk* holds there (in place)."""
    node: dict = config
    disk: Any = on_disk
    for depth, part in enumerate(parts):
        disk_has = isinstance(disk, dict) and part in disk
        if depth == len(parts) - 1:
            if disk_has:
                node[part] = copy.deepcopy(disk[part])
            else:
                node.pop(part, None)
            return
        if disk_has and not isinstance(disk[part], dict):
            node[part] = copy.deepcopy(disk[part])  # the file holds this branch as a non-mapping: keep it so
            return
        if not isinstance(node.get(part), dict):
            if not disk_has:
                return  # neither the save nor the file holds the branch: nothing to put back
            node[part] = {}
        node, disk = node[part], (disk[part] if disk_has else _ABSENT)


def _prune_new_empty_branches(config: dict, on_disk: dict, parts: list[str]) -> None:
    """Drop the mappings along *parts* that removing the leaf left empty and that the file never had."""
    for depth in range(len(parts) - 1, 0, -1):
        prefix = parts[:depth]
        parent = _get(config, prefix[:-1])
        if isinstance(parent, dict) and parent.get(prefix[-1]) == {} and _get(on_disk, prefix) is _ABSENT:
            del parent[prefix[-1]]


def _leaves(tree: dict, prefix: tuple = ()) -> list[list[str]]:
    """Leaf paths, a non-empty mapping being a branch (``managed_scope.managed_config_keys`` rules)."""
    out: list[list[str]] = []
    for key, value in tree.items():
        path = (*prefix, str(key))
        out.extend(_leaves(value, path) if isinstance(value, dict) and value else [list(path)])
    return out


def keep_managed_leaves_as_on_disk(config: dict[str, Any], on_disk: dict[str, Any]) -> dict[str, Any]:
    """A copy of *config* whose managed leaves hold exactly what *on_disk* (the raw user file) holds.

    A leaf the caller set to a value that is neither the managed one nor the on-disk one is a
    refused change: it is not saved, and the user is told so on stderr.
    """
    from hermes_cli import managed_scope

    # The overlay as load_config applies it (a bare managed ``model: x`` pins ``model.default``).
    managed_values = managed_scope.apply_managed_overlay({})
    if not managed_values:
        return config
    on_disk = on_disk if isinstance(on_disk, dict) else {}
    config = copy.deepcopy(config)
    refused = []
    for parts in sorted(_leaves(managed_values)):
        dotted = ".".join(parts)
        wanted, before = _get(config, parts), _get(on_disk, parts)
        if wanted is not _ABSENT and wanted != before and wanted != _get(managed_values, parts):
            refused.append(dotted)
        _restore_leaf(config, on_disk, parts)
        _prune_new_empty_branches(config, on_disk, parts)
    if refused:
        print(f"Note: {len(refused)} managed setting(s) were not saved "
              f"(managed by your administrator): {', '.join(refused)}", file=sys.stderr)
    return config
