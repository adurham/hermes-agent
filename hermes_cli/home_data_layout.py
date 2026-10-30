"""Machine-specific state excluded from portable home transfers."""
from __future__ import annotations


# Dependency generations, downloaded tools, and partial transfers belong to this machine.
PM_RUNTIME_ROOT_DIRS = frozenset({"installs", "tools", "cache"})

# Bitwarden / 1Password secret-value caches and the Photon sidecar auth token, as POSIX paths relative to
# a home: at its top, where a sandbox can hide a file by path (a writable folder above a hidden file can be
# renamed), and at the cache/ and runtime/ paths earlier releases used, which the first read moves up.
# Credential stores for every guard that lists them; regenerated on use, so backups leave them out.
SECRET_CACHE_STORES = (
    "bws_cache.json", "bws_cache.enc.json", "op_cache.json", "photon-sidecar.json",
    "cache/bws_cache.json", "cache/bws_cache.enc.json", "cache/op_cache.json", "runtime/photon-sidecar.json",
)


def profile_root_entry(parts: tuple[str, ...]) -> str | None:
    """Locate a home-root entry without excluding same-named skill/plugin data."""
    if len(parts) >= 3 and parts[0] == "profiles":
        return parts[2]
    return parts[0] if parts else None
