"""Fork-owned banner branding + git-state subsystem.

This module is a HARD-FORK BOUNDARY (like ``agent/fork/*``): upstream
hermes-agent does not know it exists, so it never conflicts on merge. All
fork-specific banner logic lives here; ``hermes_cli/banner.py`` reaches it
through thin 2-line forwarders.

Why this exists
---------------
Upstream periodically rewrites ``hermes_cli/banner.py`` wholesale. The fork
adds a richer git-state line (carried-commits / upstream-behind nudge), a
fork-aware agent name parsed from the origin remote, HEAD-date version labels,
and a release-tag URL that points at the fork's own GitHub tree. When those
lived inline in ``banner.py`` an upstream rewrite would silently drop them
(a real bug we hit: ``_skin_branding`` vanished while its callers survived,
crashing the banner render). Relocating the whole cluster here means upstream
can rewrite ``banner.py`` freely; the worst that happens on merge is a
take-ours on the handful of forwarder lines.

Public entry points (called from banner.py forwarders):
  * ``format_banner_version_label()`` — the title line
  * ``get_git_banner_state(repo_dir=None)`` — the {local,origin,upstream,
    carried,upstream_behind} dict
  * ``get_latest_release_tag(repo_dir=None)`` — (tag, url) for the latest tag
  * ``resolve_agent_name()`` — display name (fork-aware)
  * ``skin_branding(key, fallback)`` — skin branding string lookup

``_resolve_repo_dir`` stays in ``banner.py`` (shared with the non-fork update
checker) and is imported here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hermes_cli import __version__ as VERSION, __release_date__ as RELEASE_DATE


# Constants (_CANONICAL_REPO, _FALLBACK_RELEASE_URL_BASE, _UPSTREAM_BEHIND_NUDGE)
# and the per-process caches live on the ``banner`` module — single source of
# truth, and the fork's tests reset/read them there. Referenced as _banner.X.


def skin_branding(key: str, fallback: str) -> str:
    """Get a branding string from the active skin, or return fallback."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_branding(key, fallback)
    except Exception:
        return fallback


def get_git_banner_state(repo_dir: Optional[Path] = None) -> Optional[dict]:
    """Return git state for the startup banner.

    Fields:
        local: short SHA of HEAD (always present)
        origin: short SHA of origin/main, or None if missing
        upstream: short SHA of upstream/main, or None if no upstream remote
        carried: commits on HEAD not on origin/main (your local-only commits)
        upstream_behind: commits on upstream/main not on HEAD (only set when
            an ``upstream`` remote exists; reflects how stale your fork is
            relative to the real NousResearch repo)

    Low-level git plumbing (``_git_short_hash`` / ``_git_count`` /
    ``_resolve_repo_dir``) is dispatched through the ``banner`` module so the
    fork's tests that patch ``hermes_cli.banner.*`` still intercept.
    """
    from hermes_cli import banner as _banner
    repo_dir = repo_dir or _banner._resolve_repo_dir()
    if repo_dir is None:
        # No git checkout (canonical case: the published Docker image, which
        # excludes ``.git`` from the build context).  Fall back to the baked
        # build SHA — a built image is pinned to one commit, so it is a frozen
        # ``local == origin`` state with no carried/behind counts.
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {"local": baked, "origin": baked, "upstream": None,
                        "carried": 0, "upstream_behind": 0}
        except Exception:
            pass
        return None

    local = _banner._git_short_hash(repo_dir, "HEAD")
    if not local:
        # Live-git lookup failed (e.g. shallow clone without HEAD resolvable).
        # Fall back to the baked build SHA if available.
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {"local": baked, "origin": baked, "upstream": None,
                        "carried": 0, "upstream_behind": 0}
        except Exception:
            pass
        return None

    origin = _banner._git_short_hash(repo_dir, "origin/main")
    upstream = _banner._git_short_hash(repo_dir, "upstream/main")

    carried = _banner._git_count(repo_dir, "origin/main..HEAD") if origin else 0
    upstream_behind = _banner._git_count(repo_dir, "HEAD..upstream/main") if upstream else 0

    return {
        "local": local,
        "origin": origin,
        "upstream": upstream,
        "carried": carried,
        "upstream_behind": upstream_behind,
    }


def _parse_github_origin(repo_dir: Path) -> Optional[tuple]:
    """Return ``(owner, repo)`` parsed from origin's URL, or None.

    Handles both SSH (``git@github.com:owner/repo.git``) and HTTPS
    (``https://github.com/owner/repo[.git]``) forms. Non-GitHub origins
    return None — the banner falls back to the canonical
    NousResearch/hermes-agent links in that case.
    """
    from hermes_cli import banner as _banner
    if _banner._origin_repo_cache is not None:
        return _banner._origin_repo_cache[0]

    try:
        result = _banner.subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=str(repo_dir),
        )
    except Exception:
        _banner._origin_repo_cache = (None,)
        return None

    if result.returncode != 0:
        _banner._origin_repo_cache = (None,)
        return None

    url = (result.stdout or "").strip()
    if not url:
        _banner._origin_repo_cache = (None,)
        return None

    # SSH form: git@github.com:owner/repo.git
    # HTTPS form: https://github.com/owner/repo(.git)?
    parsed: Optional[tuple] = None
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:"):]
    elif url.startswith("https://github.com/"):
        path = url[len("https://github.com/"):]
    elif url.startswith("ssh://git@github.com/"):
        path = url[len("ssh://git@github.com/"):]
    else:
        path = ""

    if path:
        if path.endswith(".git"):
            path = path[:-4]
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] and parts[1]:
            parsed = (parts[0], parts[1])

    _banner._origin_repo_cache = (parsed,)
    return parsed


def get_latest_release_tag(repo_dir: Optional[Path] = None) -> Optional[tuple]:
    """Return ``(tag, release_url)`` for the latest git tag, or None.

    Local-only — runs ``git describe --tags --abbrev=0`` against the
    Hermes checkout. Cached per-process. Release URL targets the origin
    repo: ``releases/tag/<tag>`` for canonical NousResearch/hermes-agent,
    ``tree/<tag>`` for any other GitHub fork (works without a published
    Release on the fork — tag-tree URLs are valid for any pushed tag).
    Falls back to the NousResearch release URL when origin isn't a
    parseable GitHub remote.
    """
    from hermes_cli import banner as _banner
    if _banner._latest_release_cache is not None:
        return _banner._latest_release_cache or None

    repo_dir = repo_dir or _banner._resolve_repo_dir()
    if repo_dir is None:
        _banner._latest_release_cache = ()  # falsy sentinel — skip future lookups
        return None

    try:
        result = _banner.subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=3,
            cwd=str(repo_dir),
        )
    except Exception:
        _banner._latest_release_cache = ()
        return None

    if result.returncode != 0:
        _banner._latest_release_cache = ()
        return None

    tag = (result.stdout or "").strip()
    if not tag:
        _banner._latest_release_cache = ()
        return None

    origin = _banner._parse_github_origin(repo_dir)
    if origin == _banner._CANONICAL_REPO:
        url = f"https://github.com/{origin[0]}/{origin[1]}/releases/tag/{tag}"
    elif origin is not None:
        # Fork: link to tree/<tag>. Works without a published GitHub Release
        # (tag-tree URLs resolve for any pushed tag).
        url = f"https://github.com/{origin[0]}/{origin[1]}/tree/{tag}"
    else:
        # Non-GitHub origin or unparseable — keep canonical link as a sane default.
        url = f"{_banner._FALLBACK_RELEASE_URL_BASE}/{tag}"

    _banner._latest_release_cache = (tag, url)
    return _banner._latest_release_cache


def resolve_agent_name() -> str:
    """Resolve the agent display name shown in the banner title.

    Priority:
      1. Active skin's ``branding.agent_name`` if set to something other
         than the built-in default ("Hermes Agent") — user customization wins.
      2. ``<owner>/<repo>`` parsed from origin remote when the fork isn't
         the canonical NousResearch/hermes-agent — auto fork-identification.
      3. Default "Hermes Agent" — canonical or unparseable cases.
    """
    from hermes_cli import banner as _banner
    custom = _banner._skin_branding("agent_name", "Hermes Agent")
    if custom and custom != "Hermes Agent":
        return custom

    repo_dir = _banner._resolve_repo_dir()
    if repo_dir is None:
        return "Hermes Agent"
    origin = _banner._parse_github_origin(repo_dir)
    if origin and origin != _banner._CANONICAL_REPO:
        return f"{origin[0]}/{origin[1]}"
    return "Hermes Agent"


def format_banner_version_label() -> str:
    """Return the version label shown in the startup banner title.

    On a fork, the date shown is HEAD's committer date — the hardcoded
    ``__release_date__`` only tracks canonical NousResearch releases and
    goes stale immediately on a fork that's been pulling from main.

    NOTE: the public symbols this calls (``get_git_banner_state``,
    ``_resolve_agent_name``, ``_parse_github_origin``) are dispatched through
    the ``banner`` module namespace, NOT the local copies, so that tests which
    ``patch.object(banner, "get_git_banner_state", ...)`` still intercept. This
    keeps the (shared-with-upstream) test surface on ``banner.*`` intact while
    the implementation lives here.
    """
    from hermes_cli import banner as _banner
    repo_dir = _banner._resolve_repo_dir()
    date_label = RELEASE_DATE
    if repo_dir is not None:
        origin = _banner._parse_github_origin(repo_dir)
        if origin and origin != _banner._CANONICAL_REPO:
            head_date = _banner._git_head_date(repo_dir)
            if head_date:
                date_label = head_date
    base = f"{_banner._resolve_agent_name()} v{VERSION} ({date_label})"
    state = _banner.get_git_banner_state()
    if not state:
        return base

    local = state.get("local")
    if not local:
        return base

    carried = int(state.get("carried") or 0)
    upstream_behind = int(state.get("upstream_behind") or 0)

    label = f"{base} · {local}"
    if carried > 0:
        word = "commit" if carried == 1 else "commits"
        label += f" (+{carried} carried {word})"

    if upstream_behind >= _banner._UPSTREAM_BEHIND_NUDGE:
        label += f" · upstream +{upstream_behind}"

    return label
