from unittest.mock import MagicMock, patch


def test_format_banner_version_label_without_git_state():
    from hermes_cli import banner

    with (
        patch.object(banner, "get_git_banner_state", return_value=None),
        patch.object(banner, "_resolve_agent_name", return_value="Hermes Agent"),
        # Pretend we're on the canonical repo so the fork-date branch
        # doesn't kick in and override RELEASE_DATE with HEAD's commit-date.
        patch.object(banner, "_parse_github_origin", return_value=banner._CANONICAL_REPO),
    ):
        value = banner.format_banner_version_label()

    assert value == f"Hermes Agent v{banner.VERSION} ({banner.RELEASE_DATE})"


def test_format_banner_version_label_clean_fork_in_sync():
    """HEAD == origin/main, upstream remote absent or in sync — show local SHA only."""
    from hermes_cli import banner

    with (
        patch.object(
            banner,
            "get_git_banner_state",
            return_value={
                "local": "b2f477a3",
                "origin": "b2f477a3",
                "upstream": None,
                "carried": 0,
                "upstream_behind": 0,
            },
        ),
        patch.object(banner, "_resolve_agent_name", return_value="Hermes Agent"),
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· b2f477a3")
    assert "carried" not in value
    assert "upstream" not in value


def test_format_banner_version_label_with_carried_commits():
    """Commits on HEAD not yet on origin/main are surfaced as carried."""
    from hermes_cli import banner

    with (
        patch.object(
            banner,
            "get_git_banner_state",
            return_value={
                "local": "af8aad31",
                "origin": "b2f477a3",
                "upstream": None,
                "carried": 3,
                "upstream_behind": 0,
            },
        ),
        patch.object(banner, "_resolve_agent_name", return_value="Hermes Agent"),
    ):
        value = banner.format_banner_version_label()

    assert "· af8aad31" in value
    assert "+3 carried commits" in value
    # No upstream nudge because upstream_behind == 0
    assert "upstream +" not in value


def test_format_banner_version_label_nudges_when_upstream_far_ahead():
    """When upstream/main is ≥ threshold ahead, append a nudge."""
    from hermes_cli import banner

    with (
        patch.object(
            banner,
            "get_git_banner_state",
            return_value={
                "local": "6239e6c1",
                "origin": "6239e6c1",
                "upstream": "deadbeef",
                "carried": 0,
                "upstream_behind": 673,
            },
        ),
        patch.object(banner, "_resolve_agent_name", return_value="Hermes Agent"),
    ):
        value = banner.format_banner_version_label()

    assert "· 6239e6c1" in value
    assert "· upstream +673" in value


def test_format_banner_version_label_no_nudge_below_threshold():
    """Small upstream lead is just routine drift — no nudge."""
    from hermes_cli import banner

    threshold = banner._UPSTREAM_BEHIND_NUDGE
    with (
        patch.object(
            banner,
            "get_git_banner_state",
            return_value={
                "local": "6239e6c1",
                "origin": "6239e6c1",
                "upstream": "deadbeef",
                "carried": 0,
                "upstream_behind": max(threshold - 1, 0),
            },
        ),
        patch.object(banner, "_resolve_agent_name", return_value="Hermes Agent"),
    ):
        value = banner.format_banner_version_label()

    assert "· 6239e6c1" in value
    assert "upstream +" not in value


def test_get_git_banner_state_reads_head_origin_and_upstream(tmp_path):
    """Happy path: HEAD, origin/main, and upstream/main all resolve."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-parse", "--short=8", "origin/main"): MagicMock(returncode=0, stdout="b2f477a3\n"),
        ("git", "rev-parse", "--short=8", "upstream/main"): MagicMock(returncode=0, stdout="deadbeef\n"),
        ("git", "rev-list", "--count", "origin/main..HEAD"): MagicMock(returncode=0, stdout="3\n"),
        ("git", "rev-list", "--count", "HEAD..upstream/main"): MagicMock(returncode=0, stdout="42\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {
        "local": "af8aad31",
        "origin": "b2f477a3",
        "upstream": "deadbeef",
        "carried": 3,
        "upstream_behind": 42,
    }


def test_get_git_banner_state_without_upstream_remote(tmp_path):
    """Most users don't have an `upstream` remote — degrade gracefully."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-parse", "--short=8", "origin/main"): MagicMock(returncode=0, stdout="b2f477a3\n"),
        # upstream/main does not resolve
        ("git", "rev-parse", "--short=8", "upstream/main"): MagicMock(returncode=128, stdout=""),
        ("git", "rev-list", "--count", "origin/main..HEAD"): MagicMock(returncode=0, stdout="0\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    assert state == {
        "local": "af8aad31",
        "origin": "b2f477a3",
        "upstream": None,
        "carried": 0,
        "upstream_behind": 0,
    }


def test_parse_github_origin_ssh_form(tmp_path):
    """SSH-form origin URL parses to (owner, repo)."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    banner._origin_repo_cache = None  # clear cache

    with patch(
        "hermes_cli.banner.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="git@github.com:adurham/hermes-agent.git\n"),
    ):
        result = banner._parse_github_origin(repo_dir)

    assert result == ("adurham", "hermes-agent")


def test_parse_github_origin_https_form(tmp_path):
    """HTTPS-form origin URL parses to (owner, repo) with .git stripped."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    banner._origin_repo_cache = None

    with patch(
        "hermes_cli.banner.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="https://github.com/NousResearch/hermes-agent.git\n"),
    ):
        result = banner._parse_github_origin(repo_dir)

    assert result == ("NousResearch", "hermes-agent")


def test_parse_github_origin_non_github_returns_none(tmp_path):
    """Non-GitHub origin (e.g. internal GitLab) returns None — falls back to canonical."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    banner._origin_repo_cache = None

    with patch(
        "hermes_cli.banner.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="git@git.corp.example.com:team/repo.git\n"),
    ):
        result = banner._parse_github_origin(repo_dir)

    assert result is None


def test_get_latest_release_tag_canonical_uses_releases_path(tmp_path):
    """Canonical NousResearch/hermes-agent origin → releases/tag URL."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    banner._latest_release_cache = None
    banner._origin_repo_cache = None

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "describe", "--tags"]:
            return MagicMock(returncode=0, stdout="v2026.4.30\n")
        if cmd == ["git", "config", "--get", "remote.origin.url"]:
            return MagicMock(returncode=0, stdout="git@github.com:NousResearch/hermes-agent.git\n")
        raise AssertionError(f"unexpected: {cmd}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        tag, url = banner.get_latest_release_tag(repo_dir)

    assert tag == "v2026.4.30"
    assert url == "https://github.com/NousResearch/hermes-agent/releases/tag/v2026.4.30"


def test_get_latest_release_tag_fork_uses_tree_path(tmp_path):
    """Fork origin → tree/<tag> URL (works without a published Release)."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    banner._latest_release_cache = None
    banner._origin_repo_cache = None

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "describe", "--tags"]:
            return MagicMock(returncode=0, stdout="v2026.4.30\n")
        if cmd == ["git", "config", "--get", "remote.origin.url"]:
            return MagicMock(returncode=0, stdout="git@github.com:adurham/hermes-agent.git\n")
        raise AssertionError(f"unexpected: {cmd}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        tag, url = banner.get_latest_release_tag(repo_dir)

    assert tag == "v2026.4.30"
    assert url == "https://github.com/adurham/hermes-agent/tree/v2026.4.30"


def test_resolve_agent_name_canonical_origin_returns_hermes_agent(tmp_path):
    """Canonical origin → 'Hermes Agent' (preserves upstream branding)."""
    from hermes_cli import banner

    banner._origin_repo_cache = None
    with (
        patch.object(banner, "_resolve_repo_dir", return_value=tmp_path),
        patch.object(banner, "_parse_github_origin", return_value=("NousResearch", "hermes-agent")),
        patch.object(banner, "_skin_branding", return_value="Hermes Agent"),
    ):
        assert banner._resolve_agent_name() == "Hermes Agent"


def test_resolve_agent_name_fork_origin_uses_owner_repo(tmp_path):
    """Fork origin → '<owner>/<repo>' so the user immediately sees they're on a fork."""
    from hermes_cli import banner

    banner._origin_repo_cache = None
    with (
        patch.object(banner, "_resolve_repo_dir", return_value=tmp_path),
        patch.object(banner, "_parse_github_origin", return_value=("adurham", "hermes-agent")),
        patch.object(banner, "_skin_branding", return_value="Hermes Agent"),
    ):
        assert banner._resolve_agent_name() == "adurham/hermes-agent"


def test_resolve_agent_name_skin_branding_wins(tmp_path):
    """Active skin's branding.agent_name overrides fork-derived name."""
    from hermes_cli import banner

    banner._origin_repo_cache = None
    with (
        patch.object(banner, "_resolve_repo_dir", return_value=tmp_path),
        patch.object(banner, "_parse_github_origin", return_value=("adurham", "hermes-agent")),
        patch.object(banner, "_skin_branding", return_value="Ares Agent"),
    ):
        assert banner._resolve_agent_name() == "Ares Agent"
