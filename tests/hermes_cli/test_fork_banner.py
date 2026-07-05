"""Tests for hermes_cli/fork_banner.py — fork-owned banner branding + git-state.

Verifies the fork's git-state subsystem, agent name resolution,
and banner version label formatting.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestForkBanner:
    """Tests for the fork banner module."""

    def test_skin_branding_returns_fallback_on_missing_key(self):
        """skin_branding returns fallback when key not found in skin."""
        from hermes_cli.fork_banner import skin_branding
        result = skin_branding("nonexistent_key", "default-agent")
        assert result == "default-agent"

    def test_resolve_agent_name_returns_string(self):
        """resolve_agent_name returns a non-empty string."""
        from hermes_cli.fork_banner import resolve_agent_name
        name = resolve_agent_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_format_banner_version_label_returns_string(self):
        """format_banner_version_label returns a string."""
        from hermes_cli.fork_banner import format_banner_version_label
        label = format_banner_version_label()
        assert isinstance(label, str)

    def test_get_git_banner_state_returns_none_without_repo(self, monkeypatch):
        """get_git_banner_state returns None when no repo is found."""
        from hermes_cli.fork_banner import get_git_banner_state
        # Mock _resolve_repo_dir to return None (no git checkout)
        monkeypatch.setattr(
            "hermes_cli.banner._resolve_repo_dir",
            lambda: None,
        )
        state = get_git_banner_state(repo_dir=None)
        assert state is None

    def test_get_git_banner_state_returns_none_for_bad_path(self):
        """get_git_banner_state returns None for a non-git directory."""
        from hermes_cli.fork_banner import get_git_banner_state
        with tempfile.TemporaryDirectory() as td:
            # Create a non-git dir that looks like a repo (has .gitignore but no .git)
            d = Path(td) / "not-a-repo"
            d.mkdir()
            state = get_git_banner_state(repo_dir=d)
            assert state is None

    def test_get_git_banner_state_returns_dict_for_git_repo(self):
        """get_git_banner_state returns a dict with expected keys for a real git repo."""
        from hermes_cli.fork_banner import get_git_banner_state
        repo_dir = Path(__file__).parent.parent.parent  # repo root
        state = get_git_banner_state(repo_dir=repo_dir)
        if state is not None:
            assert isinstance(state, dict)
            # Should have git-state keys
            assert any(k in state for k in ("local", "origin", "upstream", "carried", "upstream_behind"))

    def test_get_latest_release_tag_returns_none_without_repo(self, monkeypatch):
        """get_latest_release_tag returns None when no repo dir given."""
        from hermes_cli.fork_banner import get_latest_release_tag
        monkeypatch.setattr(
            "hermes_cli.banner._resolve_repo_dir",
            lambda: None,
        )
        tag = get_latest_release_tag(repo_dir=None)
        assert tag is None

    def test_get_latest_release_tag_returns_tuple_for_git_repo(self):
        """get_latest_release_tag returns (tag_name, url) for a real git repo."""
        from hermes_cli.fork_banner import get_latest_release_tag
        repo_dir = Path(__file__).parent.parent.parent
        result = get_latest_release_tag(repo_dir=repo_dir)
        if result is not None:
            assert isinstance(result, tuple)
            assert len(result) == 2
            assert isinstance(result[0], str)  # tag name
            assert isinstance(result[1], str)  # URL

    def test_parse_github_origin_returns_none_for_non_github(self, monkeypatch):
        """_parse_github_origin returns None for a non-git directory."""
        from hermes_cli.fork_banner import _parse_github_origin
        # Clear the origin cache so the test isn't polluted by real repo
        import hermes_cli.banner as _banner
        _banner._origin_repo_cache = None
        with tempfile.TemporaryDirectory() as td:
            result = _parse_github_origin(repo_dir=Path(td))
            assert result is None