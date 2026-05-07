"""Welcome banner, ASCII art, skills summary, and update check for the CLI.

Pure display functions with no HermesCLI state dependency.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from prompt_toolkit import print_formatted_text as _pt_print
from prompt_toolkit.formatted_text import ANSI as _PT_ANSI

logger = logging.getLogger(__name__)


# =========================================================================
# ANSI building blocks for conversation display
# =========================================================================

_GOLD = "\033[1;38;2;255;215;0m"  # True-color #FFD700 bold
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RST = "\033[0m"


def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""
    _pt_print(_PT_ANSI(text))


# =========================================================================
# Skin-aware color helpers
# =========================================================================

def _skin_color(key: str, fallback: str) -> str:
    """Get a color from the active skin, or return fallback."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_color(key, fallback)
    except Exception:
        return fallback


def _skin_branding(key: str, fallback: str) -> str:
    """Get a branding string from the active skin, or return fallback."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_branding(key, fallback)
    except Exception:
        return fallback


# =========================================================================
# ASCII Art & Branding
# =========================================================================

from hermes_cli import __version__ as VERSION, __release_date__ as RELEASE_DATE

HERMES_AGENT_LOGO = """[bold #FFD700]██╗  ██╗███████╗██████╗ ███╗   ███╗███████╗███████╗       █████╗  ██████╗ ███████╗███╗   ██╗████████╗[/]
[bold #FFD700]██║  ██║██╔════╝██╔══██╗████╗ ████║██╔════╝██╔════╝      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝[/]
[#FFBF00]███████║█████╗  ██████╔╝██╔████╔██║█████╗  ███████╗█████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║[/]
[#FFBF00]██╔══██║██╔══╝  ██╔══██╗██║╚██╔╝██║██╔══╝  ╚════██║╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║[/]
[#CD7F32]██║  ██║███████╗██║  ██║██║ ╚═╝ ██║███████╗███████║      ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║[/]
[#CD7F32]╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝      ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝[/]"""

HERMES_CADUCEUS = """[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀⠀⣀⣀⠀⢀⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⢀⣠⣴⣾⣿⣿⣇⠸⣿⣿⠇⣸⣿⣿⣷⣦⣄⡀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⢀⣠⣴⣶⠿⠋⣩⡿⣿⡿⠻⣿⡇⢠⡄⢸⣿⠟⢿⣿⢿⣍⠙⠿⣶⣦⣄⡀⠀[/]
[#FFBF00]⠀⠀⠉⠉⠁⠶⠟⠋⠀⠉⠀⢀⣈⣁⡈⢁⣈⣁⡀⠀⠉⠀⠙⠻⠶⠈⠉⠉⠀⠀[/]
[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣴⣿⡿⠛⢁⡈⠛⢿⣿⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFD700]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠿⣿⣦⣤⣈⠁⢠⣴⣿⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠻⢿⣿⣦⡉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#FFBF00]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢷⣦⣈⠛⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣴⠦⠈⠙⠿⣦⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#CD7F32]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⣿⣤⡈⠁⢤⣿⠇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠷⠄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⠑⢶⣄⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⠁⢰⡆⠈⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠳⠈⣡⠞⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]
[#B8860B]⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀[/]"""



# =========================================================================
# Skills scanning
# =========================================================================

def get_available_skills() -> Dict[str, List[str]]:
    """Return skills grouped by category, filtered by platform and disabled state.

    Delegates to ``_find_all_skills()`` from ``tools/skills_tool`` which already
    handles platform gating (``platforms:`` frontmatter) and respects the
    user's ``skills.disabled`` config list.
    """
    try:
        from tools.skills_tool import _find_all_skills
        all_skills = _find_all_skills()  # already filtered
    except Exception:
        return {}

    skills_by_category: Dict[str, List[str]] = {}
    for skill in all_skills:
        category = skill.get("category") or "general"
        skills_by_category.setdefault(category, []).append(skill["name"])
    return skills_by_category


# =========================================================================
# Update check
# =========================================================================

# Cache update check results for 6 hours to avoid repeated git fetches
_UPDATE_CHECK_CACHE_SECONDS = 6 * 3600

# Sentinel returned when we know an update exists but can't count commits
# (e.g. nix-built hermes — no local git history to count against).
UPDATE_AVAILABLE_NO_COUNT = -1

_UPSTREAM_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"


def _check_via_rev(local_rev: str) -> Optional[int]:
    """Compare an embedded git revision to upstream main via ls-remote.

    Returns 0 if up-to-date, ``UPDATE_AVAILABLE_NO_COUNT`` if behind,
    or ``None`` on failure.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", _UPSTREAM_REPO_URL, "refs/heads/main"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    upstream_rev = result.stdout.split()[0]
    if not upstream_rev:
        return None
    return 0 if upstream_rev == local_rev else UPDATE_AVAILABLE_NO_COUNT


def _check_via_local_git(repo_dir: Path) -> Optional[int]:
    """Count commits behind origin/main in a local checkout."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "--quiet"],
            capture_output=True, timeout=10,
            cwd=str(repo_dir),
        )
    except Exception:
        pass  # Offline or timeout — use stale refs, that's fine

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True, timeout=5,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def check_for_updates() -> Optional[int]:
    """Check whether a Hermes update is available.

    Two paths: if ``HERMES_REVISION`` is set (nix builds embed it), compare
    it to upstream main via ``git ls-remote``. Otherwise look for a local
    git checkout and count commits behind ``origin/main``.

    Returns the number of commits behind, ``UPDATE_AVAILABLE_NO_COUNT`` (-1)
    if behind but the count is unknown, ``0`` if up-to-date, or ``None`` if
    the check failed or doesn't apply. Cached for 6 hours.
    """
    hermes_home = get_hermes_home()
    cache_file = hermes_home / ".update_check"
    embedded_rev = os.environ.get("HERMES_REVISION") or None

    # Read cache — invalidate if the embedded rev has changed since last check
    now = time.time()
    try:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            if (
                now - cached.get("ts", 0) < _UPDATE_CHECK_CACHE_SECONDS
                and cached.get("rev") == embedded_rev
            ):
                return cached.get("behind")
    except Exception:
        pass

    if embedded_rev:
        behind = _check_via_rev(embedded_rev)
    else:
        repo_dir = _resolve_repo_dir()
        if repo_dir is None:
            return None
        behind = _check_via_local_git(repo_dir)

    try:
        cache_file.write_text(json.dumps({"ts": now, "behind": behind, "rev": embedded_rev}))
    except Exception:
        pass

    return behind


def _resolve_repo_dir() -> Optional[Path]:
    """Return the active Hermes git checkout, or None if this isn't a git install.

    Prefers the directory this module is loaded from (covers editable /
    `pip install -e` installs, which live outside ``~/.hermes``). Falls back
    to ``~/.hermes/hermes-agent`` for the managed-install layout. Reporting
    against the path actually imported keeps the banner honest when a
    developer ``pip install -e``'s a fork checkout.
    """
    code_dir = Path(__file__).parent.parent.resolve()
    if (code_dir / ".git").exists():
        return code_dir
    hermes_home = get_hermes_home()
    repo_dir = hermes_home / "hermes-agent"
    return repo_dir if (repo_dir / ".git").exists() else None


def _git_short_hash(repo_dir: Path, rev: str) -> Optional[str]:
    """Resolve a git revision to an 8-character short hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", rev],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_dir),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _git_count(repo_dir: Path, range_spec: str) -> int:
    """Return ``git rev-list --count <range_spec>`` or 0 on any failure."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", range_spec],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_dir),
        )
    except Exception:
        return 0
    if result.returncode != 0:
        return 0
    try:
        return max(int((result.stdout or "0").strip() or "0"), 0)
    except ValueError:
        return 0


def _git_head_date(repo_dir: Path) -> Optional[str]:
    """Return HEAD's committer date as ``YYYY-MM-DD``, or None on any failure.

    Used as the banner's release-date stand-in when running on a fork —
    a stale ``__release_date__`` constant is meaningless once the fork
    diverges from upstream tags.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_dir),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


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
    """
    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        return None

    local = _git_short_hash(repo_dir, "HEAD")
    if not local:
        return None

    origin = _git_short_hash(repo_dir, "origin/main")
    upstream = _git_short_hash(repo_dir, "upstream/main")

    carried = _git_count(repo_dir, "origin/main..HEAD") if origin else 0
    upstream_behind = _git_count(repo_dir, "HEAD..upstream/main") if upstream else 0

    return {
        "local": local,
        "origin": origin,
        "upstream": upstream,
        "carried": carried,
        "upstream_behind": upstream_behind,
    }


_CANONICAL_REPO = ("NousResearch", "hermes-agent")
_FALLBACK_RELEASE_URL_BASE = (
    f"https://github.com/{_CANONICAL_REPO[0]}/{_CANONICAL_REPO[1]}/releases/tag"
)
_latest_release_cache: Optional[tuple] = None  # (tag, url) once resolved
_origin_repo_cache: Optional[tuple] = None  # ((owner, repo) | None,) once resolved


def _parse_github_origin(repo_dir: Path) -> Optional[tuple]:
    """Return ``(owner, repo)`` parsed from origin's URL, or None.

    Handles both SSH (``git@github.com:owner/repo.git``) and HTTPS
    (``https://github.com/owner/repo[.git]``) forms. Non-GitHub origins
    return None — the banner falls back to the canonical
    NousResearch/hermes-agent links in that case.
    """
    global _origin_repo_cache
    if _origin_repo_cache is not None:
        return _origin_repo_cache[0]

    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=str(repo_dir),
        )
    except Exception:
        _origin_repo_cache = (None,)
        return None

    if result.returncode != 0:
        _origin_repo_cache = (None,)
        return None

    url = (result.stdout or "").strip()
    if not url:
        _origin_repo_cache = (None,)
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

    _origin_repo_cache = (parsed,)
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
    global _latest_release_cache
    if _latest_release_cache is not None:
        return _latest_release_cache or None

    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        _latest_release_cache = ()  # falsy sentinel — skip future lookups
        return None

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=3,
            cwd=str(repo_dir),
        )
    except Exception:
        _latest_release_cache = ()
        return None

    if result.returncode != 0:
        _latest_release_cache = ()
        return None

    tag = (result.stdout or "").strip()
    if not tag:
        _latest_release_cache = ()
        return None

    origin = _parse_github_origin(repo_dir)
    if origin == _CANONICAL_REPO:
        url = f"https://github.com/{origin[0]}/{origin[1]}/releases/tag/{tag}"
    elif origin is not None:
        # Fork: link to tree/<tag>. Works without a published GitHub Release
        # (tag-tree URLs resolve for any pushed tag).
        url = f"https://github.com/{origin[0]}/{origin[1]}/tree/{tag}"
    else:
        # Non-GitHub origin or unparseable — keep canonical link as a sane default.
        url = f"{_FALLBACK_RELEASE_URL_BASE}/{tag}"

    _latest_release_cache = (tag, url)
    return _latest_release_cache


# Threshold (in commits) before the banner nudges that upstream/main has
# moved on. Below this it's just routine drift; above it the fork is stale
# enough that you probably want to consider a sync.
_UPSTREAM_BEHIND_NUDGE = 10


def _resolve_agent_name() -> str:
    """Resolve the agent display name shown in the banner title.

    Priority:
      1. Active skin's ``branding.agent_name`` if set to something other
         than the built-in default ("Hermes Agent") — user customization wins.
      2. ``<owner>/<repo>`` parsed from origin remote when the fork isn't
         the canonical NousResearch/hermes-agent — auto fork-identification.
      3. Default "Hermes Agent" — canonical or unparseable cases.
    """
    custom = _skin_branding("agent_name", "Hermes Agent")
    if custom and custom != "Hermes Agent":
        return custom

    repo_dir = _resolve_repo_dir()
    if repo_dir is None:
        return "Hermes Agent"
    origin = _parse_github_origin(repo_dir)
    if origin and origin != _CANONICAL_REPO:
        return f"{origin[0]}/{origin[1]}"
    return "Hermes Agent"


def format_banner_version_label() -> str:
    """Return the version label shown in the startup banner title.

    On a fork, the date shown is HEAD's committer date — the hardcoded
    ``__release_date__`` only tracks canonical NousResearch releases and
    goes stale immediately on a fork that's been pulling from main.
    """
    repo_dir = _resolve_repo_dir()
    date_label = RELEASE_DATE
    if repo_dir is not None:
        origin = _parse_github_origin(repo_dir)
        if origin and origin != _CANONICAL_REPO:
            head_date = _git_head_date(repo_dir)
            if head_date:
                date_label = head_date
    base = f"{_resolve_agent_name()} v{VERSION} ({date_label})"
    state = get_git_banner_state()
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

    if upstream_behind >= _UPSTREAM_BEHIND_NUDGE:
        label += f" · upstream +{upstream_behind}"

    return label


# =========================================================================
# Non-blocking update check
# =========================================================================

_update_result: Optional[int] = None
_update_check_done = threading.Event()


def prefetch_update_check():
    """Kick off update check in a background daemon thread."""
    def _run():
        global _update_result
        _update_result = check_for_updates()
        _update_check_done.set()
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def get_update_result(timeout: float = 0.5) -> Optional[int]:
    """Get result of prefetched check. Returns None if not ready."""
    _update_check_done.wait(timeout=timeout)
    return _update_result


# =========================================================================
# Welcome banner
# =========================================================================

def _format_context_length(tokens: int) -> str:
    """Format a token count for display (e.g. 128000 → '128K', 1048576 → '1M')."""
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}M"
        return f"{val:.1f}M"
    elif tokens >= 1_000:
        val = tokens / 1_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}K"
        return f"{val:.1f}K"
    return str(tokens)


def _display_toolset_name(toolset_name: str) -> str:
    """Normalize internal/legacy toolset identifiers for banner display."""
    if not toolset_name:
        return "unknown"
    return (
        toolset_name[:-6]
        if toolset_name.endswith("_tools")
        else toolset_name
    )


def build_welcome_banner(console: Console, model: str, cwd: str,
                         tools: List[dict] = None,
                         enabled_toolsets: List[str] = None,
                         disabled_toolsets: List[str] = None,
                         session_id: str = None,
                         get_toolset_for_tool=None,
                         context_length: int = None):
    """Build and print a welcome banner with caduceus on left and info on right.

    Args:
        console: Rich Console instance.
        model: Current model name.
        cwd: Current working directory.
        tools: List of tool definitions.
        enabled_toolsets: List of enabled toolset names.
        session_id: Session identifier.
        get_toolset_for_tool: Callable to map tool name -> toolset name.
        context_length: Model's context window size in tokens.
    """
    from model_tools import check_tool_availability, TOOLSET_REQUIREMENTS
    if get_toolset_for_tool is None:
        from model_tools import get_toolset_for_tool

    tools = tools or []
    enabled_toolsets = enabled_toolsets or []

    _, unavailable_toolsets = check_tool_availability(quiet=True)
    disabled_tools = set()
    # Tools whose toolset has a check_fn are lazy-initialized (e.g. honcho,
    # homeassistant) — they show as unavailable at banner time because the
    # check hasn't run yet, but they aren't misconfigured.
    lazy_tools = set()
    for item in unavailable_toolsets:
        toolset_name = item.get("name", "")
        ts_req = TOOLSET_REQUIREMENTS.get(toolset_name, {})
        tools_in_ts = item.get("tools", [])
        if ts_req.get("check_fn"):
            lazy_tools.update(tools_in_ts)
        else:
            disabled_tools.update(tools_in_ts)

    layout_table = Table.grid(padding=(0, 2))
    layout_table.add_column("left", justify="center")
    layout_table.add_column("right", justify="left")

    # Resolve skin colors once for the entire banner
    accent = _skin_color("banner_accent", "#FFBF00")
    dim = _skin_color("banner_dim", "#B8860B")
    text = _skin_color("banner_text", "#FFF8DC")
    session_color = _skin_color("session_border", "#8B8682")

    # Use skin's custom caduceus art if provided
    try:
        from hermes_cli.skin_engine import get_active_skin
        _bskin = get_active_skin()
        _hero = _bskin.banner_hero if hasattr(_bskin, 'banner_hero') and _bskin.banner_hero else HERMES_CADUCEUS
    except Exception:
        _bskin = None
        _hero = HERMES_CADUCEUS
    left_lines = ["", _hero, ""]
    model_short = model.split("/")[-1] if "/" in model else model
    if model_short.endswith(".gguf"):
        model_short = model_short[:-5]
    if len(model_short) > 28:
        model_short = model_short[:25] + "..."
    ctx_str = f" [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]" if context_length else ""
    # Vendor / provider attribution string shown next to the model name.
    # Default is "Nous Research" (canonical upstream attribution); skins
    # can override via ``branding.vendor_label`` to drop the attribution
    # entirely (set to empty string), or substitute a different label.
    _vendor_label = _skin_branding("vendor_label", "Nous Research")
    if _vendor_label:
        left_lines.append(f"[{accent}]{model_short}[/]{ctx_str} [dim {dim}]·[/] [dim {dim}]{_vendor_label}[/]")
    else:
        left_lines.append(f"[{accent}]{model_short}[/]{ctx_str}")
    left_lines.append(f"[dim {dim}]{cwd}[/]")
    if session_id:
        left_lines.append(f"[dim {session_color}]Session: {session_id}[/]")
    left_content = "\n".join(left_lines)

    right_lines = [f"[bold {accent}]Available Tools[/]"]
    toolsets_dict: Dict[str, list] = {}

    for tool in tools:
        tool_name = tool["function"]["name"]
        toolset = _display_toolset_name(get_toolset_for_tool(tool_name) or "other")
        toolsets_dict.setdefault(toolset, []).append(tool_name)

    # Toolsets the user has explicitly disabled in config shouldn't appear
    # at all — even as "unavailable" hints. Otherwise users who turn off a
    # toolset (discord, messaging, etc.) keep seeing it in the banner with
    # "missing env var" styling, which feels like a bug rather than the
    # configured intent.
    _disabled_set = set(disabled_toolsets or [])
    for item in unavailable_toolsets:
        toolset_id = item.get("id", item.get("name", "unknown"))
        if toolset_id in _disabled_set:
            continue
        display_name = _display_toolset_name(toolset_id)
        if display_name in _disabled_set:
            continue
        if display_name not in toolsets_dict:
            toolsets_dict[display_name] = []
        for tool_name in item.get("tools", []):
            if tool_name not in toolsets_dict[display_name]:
                toolsets_dict[display_name].append(tool_name)

    sorted_toolsets = sorted(toolsets_dict.keys())
    display_toolsets = sorted_toolsets[:8]
    remaining_toolsets = len(sorted_toolsets) - 8

    for toolset in display_toolsets:
        tool_names = toolsets_dict[toolset]
        colored_names = []
        for name in sorted(tool_names):
            if name in disabled_tools:
                colored_names.append(f"[red]{name}[/]")
            elif name in lazy_tools:
                colored_names.append(f"[yellow]{name}[/]")
            else:
                colored_names.append(f"[{text}]{name}[/]")

        tools_str = ", ".join(colored_names)
        if len(", ".join(sorted(tool_names))) > 45:
            short_names = []
            length = 0
            for name in sorted(tool_names):
                if length + len(name) + 2 > 42:
                    short_names.append("...")
                    break
                short_names.append(name)
                length += len(name) + 2
            colored_names = []
            for name in short_names:
                if name == "...":
                    colored_names.append("[dim]...[/]")
                elif name in disabled_tools:
                    colored_names.append(f"[red]{name}[/]")
                elif name in lazy_tools:
                    colored_names.append(f"[yellow]{name}[/]")
                else:
                    colored_names.append(f"[{text}]{name}[/]")
            tools_str = ", ".join(colored_names)

        right_lines.append(f"[dim {dim}]{toolset}:[/] {tools_str}")

    if remaining_toolsets > 0:
        right_lines.append(f"[dim {dim}](and {remaining_toolsets} more toolsets...)[/]")

    # MCP Servers section (only if configured)
    try:
        from tools.mcp_tool import get_mcp_status
        mcp_status = get_mcp_status()
    except Exception:
        mcp_status = []

    if mcp_status:
        right_lines.append("")
        right_lines.append(f"[bold {accent}]MCP Servers[/]")
        for srv in mcp_status:
            if srv["connected"]:
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [{text}]({srv['transport']})[/] "
                    f"[dim {dim}]—[/] [{text}]{srv['tools']} tool(s)[/]"
                )
            else:
                right_lines.append(
                    f"[red]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[red]— failed[/]"
                )

    right_lines.append("")
    right_lines.append(f"[bold {accent}]Available Skills[/]")
    skills_by_category = get_available_skills()
    total_skills = sum(len(s) for s in skills_by_category.values())

    if skills_by_category:
        for category in sorted(skills_by_category.keys()):
            skill_names = sorted(skills_by_category[category])
            if len(skill_names) > 8:
                display_names = skill_names[:8]
                skills_str = ", ".join(display_names) + f" +{len(skill_names) - 8} more"
            else:
                skills_str = ", ".join(skill_names)
            if len(skills_str) > 50:
                skills_str = skills_str[:47] + "..."
            right_lines.append(f"[dim {dim}]{category}:[/] [{text}]{skills_str}[/]")
    else:
        right_lines.append(f"[dim {dim}]No skills installed[/]")

    right_lines.append("")
    mcp_connected = sum(1 for s in mcp_status if s["connected"]) if mcp_status else 0
    summary_parts = [f"{len(tools)} tools", f"{total_skills} skills"]
    if mcp_connected:
        summary_parts.append(f"{mcp_connected} MCP servers")
    summary_parts.append("/help for commands")
    # Show active profile name when not 'default'
    try:
        from hermes_cli.profiles import get_active_profile_name
        _profile_name = get_active_profile_name()
        if _profile_name and _profile_name != "default":
            right_lines.append(f"[bold {accent}]Profile:[/] [{text}]{_profile_name}[/]")
    except Exception:
        pass  # Never break the banner over a profiles.py bug

    right_lines.append(f"[dim {dim}]{' · '.join(summary_parts)}[/]")

    # Update check — use prefetched result if available
    try:
        behind = get_update_result(timeout=0.5)
        if behind is not None and behind != 0:
            from hermes_cli.config import get_managed_update_command, recommended_update_command
            if behind > 0:
                commits_word = "commit" if behind == 1 else "commits"
                right_lines.append(
                    f"[bold yellow]⚠ {behind} {commits_word} behind[/]"
                    f"[dim yellow] — run [bold]{recommended_update_command()}[/bold] to update[/]"
                )
            else:
                # UPDATE_AVAILABLE_NO_COUNT: nix-built hermes; we know an update
                # exists but not by how much, and we don't know how the user
                # installed it (nix run, profile, system flake, home-manager).
                managed_cmd = get_managed_update_command()
                line = "[bold yellow]⚠ update available[/]"
                if managed_cmd:
                    line += f"[dim yellow] — run [bold]{managed_cmd}[/bold][/]"
                right_lines.append(line)
    except Exception:
        pass  # Never break the banner over an update check

    right_content = "\n".join(right_lines)
    layout_table.add_row(left_content, right_content)

    title_color = _skin_color("banner_title", "#FFD700")
    border_color = _skin_color("banner_border", "#CD7F32")
    version_label = format_banner_version_label()
    release_info = get_latest_release_tag()
    if release_info:
        _tag, _url = release_info
        title_markup = f"[bold {title_color}][link={_url}]{version_label}[/link][/]"
    else:
        title_markup = f"[bold {title_color}]{version_label}[/]"
    outer_panel = Panel(
        layout_table,
        title=title_markup,
        border_style=border_color,
        padding=(0, 2),
    )

    console.print()
    term_width = shutil.get_terminal_size().columns
    if term_width >= 95:
        _logo = _bskin.banner_logo if _bskin and hasattr(_bskin, 'banner_logo') and _bskin.banner_logo else HERMES_AGENT_LOGO
        console.print(_logo)
        console.print()
    console.print(outer_panel)
