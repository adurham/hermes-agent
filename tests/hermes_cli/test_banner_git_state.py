from unittest.mock import MagicMock, patch


def test_format_banner_version_label_without_git_state():
    from hermes_cli import banner

    with patch.object(banner, "get_git_banner_state", return_value=None):
        value = banner.format_banner_version_label()

    assert value == f"Hermes Agent v{banner.VERSION} ({banner.RELEASE_DATE})"


def test_format_banner_version_label_clean_fork_in_sync():
    """HEAD == origin/main, upstream remote absent or in sync — show local SHA only."""
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={
            "local": "b2f477a3",
            "origin": "b2f477a3",
            "upstream": None,
            "carried": 0,
            "upstream_behind": 0,
        },
    ):
        value = banner.format_banner_version_label()

    assert value.endswith("· b2f477a3")
    assert "carried" not in value
    assert "upstream" not in value


def test_format_banner_version_label_with_carried_commits():
    """Commits on HEAD not yet on origin/main are surfaced as carried."""
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={
            "local": "af8aad31",
            "origin": "b2f477a3",
            "upstream": None,
            "carried": 3,
            "upstream_behind": 0,
        },
    ):
        value = banner.format_banner_version_label()

    assert "· af8aad31" in value
    assert "+3 carried commits" in value
    # No upstream nudge because upstream_behind == 0
    assert "upstream +" not in value


def test_format_banner_version_label_nudges_when_upstream_far_ahead():
    """When upstream/main is ≥ threshold ahead, append a nudge."""
    from hermes_cli import banner

    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={
            "local": "6239e6c1",
            "origin": "6239e6c1",
            "upstream": "deadbeef",
            "carried": 0,
            "upstream_behind": 673,
        },
    ):
        value = banner.format_banner_version_label()

    assert "· 6239e6c1" in value
    assert "· upstream +673" in value


def test_format_banner_version_label_no_nudge_below_threshold():
    """Small upstream lead is just routine drift — no nudge."""
    from hermes_cli import banner

    threshold = banner._UPSTREAM_BEHIND_NUDGE
    with patch.object(
        banner,
        "get_git_banner_state",
        return_value={
            "local": "6239e6c1",
            "origin": "6239e6c1",
            "upstream": "deadbeef",
            "carried": 0,
            "upstream_behind": max(threshold - 1, 0),
        },
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
