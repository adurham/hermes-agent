"""Config keys the runtime reads that the DEFAULT_CONFIG walk in ``_validate_config_key`` cannot see.

Two owners supply them:

* Core selection keys (``browser.cloud_provider``, ``<category>.use_gateway``, ``stt.provider``):
  :func:`tools.tool_backend_helpers.selection_config_keys`, the table ``read_selection`` reads.
* Plugins: a ``config_keys`` list in ``plugin.yaml`` (or a dashboard plugin's
  ``dashboard/manifest.json``) names the core-section paths the plugin's code reads, e.g.
  ``memory.retaindb``. A declared path covers itself and everything below it.

Without these, ``hermes config get/set`` called live keys "not a recognized config key".
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import hermes_yaml as yaml
from utils import fast_safe_load

logger = logging.getLogger(__name__)

# Manifest locations relative to a plugins root: flat and category plugins, plus dashboard plugins.
_MANIFEST_GLOBS = ("*/plugin.yaml", "*/plugin.yml", "*/*/plugin.yaml", "*/*/plugin.yml", "*/dashboard/manifest.json")


def _manifest_config_keys(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text) if path.suffix == ".json" else fast_safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:  # one bad manifest must not break config get
        logger.debug("config_keys: skipping unreadable manifest %s: %s", path, exc)
        return []
    raw = data.get("config_keys") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


@lru_cache(maxsize=8)
def _declared_keys_under(roots: tuple[str, ...]) -> frozenset[str]:
    keys: set[str] = set()
    for root in map(Path, roots):
        if not root.is_dir():
            continue
        for pattern in _MANIFEST_GLOBS:
            for manifest in root.glob(pattern):
                keys.update(_manifest_config_keys(manifest))
    return frozenset(keys)


def plugin_declared_config_keys() -> frozenset[str]:
    """``config_keys`` declared by bundled and the active profile's user-installed plugins."""
    from hermes_cli.plugins import get_bundled_plugins_dir
    from hermes_constants import get_hermes_home

    return _declared_keys_under((str(get_bundled_plugins_dir()), str(get_hermes_home() / "plugins")))


def is_runtime_read_key(key: str) -> bool:
    """True when ``key`` (dotted) is a core selection key or sits at/under a plugin-declared path."""
    from tools.tool_backend_helpers import selection_config_keys

    if key in selection_config_keys():
        return True
    return any(key == declared or key.startswith(declared + ".") for declared in plugin_declared_config_keys())
